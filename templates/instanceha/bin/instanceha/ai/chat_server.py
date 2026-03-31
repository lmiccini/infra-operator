"""Unix domain socket server for the InstanceHA chat interface.

Runs as a daemon thread inside the InstanceHA pod, accepting JSON-lines
connections over /var/run/instanceha/agent.sock.  Each connected client
(typically ``oc rsh <pod> instanceha-chat``) gets its own handler thread.
"""

import json
import logging
import os
import socket
import threading
from typing import Optional

from .command_parser import get_help_text, parse
from .safety import ApprovalManager, AuditLogger
from .tools import ApprovalLevel, ToolResult, registry as tool_registry


SOCKET_PATH = "/var/run/instanceha/agent.sock"
MAX_MESSAGE_SIZE = 65536


class ChatSession:
    """Handles a single client connection over the Unix socket."""

    def __init__(self, conn: socket.socket, addr,
                 nova_connection, service,
                 approval_manager: ApprovalManager,
                 llm_engine=None):
        self._conn = conn
        self._addr = addr
        self._nova_connection = nova_connection
        self._service = service
        self._approval_manager = approval_manager
        self._llm_engine = llm_engine
        self._agent = None

        # Create an Agent instance if an LLM engine is available
        if self._llm_engine and self._llm_engine.is_available():
            from .agent import Agent
            self._agent = Agent(
                engine=self._llm_engine,
                approval_manager=self._approval_manager,
                nova_connection=self._nova_connection,
                service=self._service,
            )

    def handle(self):
        """Main loop for a single client session."""
        try:
            self._send_response(
                message="InstanceHA Chat Interface. Type 'help' for commands.",
                prompt=True,
            )

            buf = b""
            while True:
                data = self._conn.recv(MAX_MESSAGE_SIZE)
                if not data:
                    break

                buf += data
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    line = line.strip()
                    if not line:
                        continue

                    try:
                        request = json.loads(line.decode("utf-8"))
                    except (json.JSONDecodeError, UnicodeDecodeError):
                        self._send_response(error="Invalid JSON")
                        continue

                    self._handle_request(request)

        except (ConnectionResetError, BrokenPipeError):
            logging.debug("Chat client disconnected")
        except Exception as e:
            logging.warning("Chat session error: %s", e)
        finally:
            try:
                self._conn.close()
            except OSError:
                pass

    def _handle_request(self, request: dict):
        """Route a JSON request to the appropriate handler."""
        msg_type = request.get("type", "query")
        message = request.get("message", "").strip()

        if msg_type == "query":
            self._handle_query(message)
        elif msg_type == "approval":
            self._handle_approval(request)
        else:
            self._send_response(error=f"Unknown message type: {msg_type}")

    def _handle_query(self, message: str):
        """Handle a query/command message."""
        if not message:
            self._send_response(message="", prompt=True)
            return

        lower = message.lower()
        if lower in ("help", "?"):
            self._send_response(message=get_help_text(), prompt=True)
            return

        if lower in ("quit", "exit", "q"):
            self._send_response(message="Goodbye.")
            try:
                self._conn.close()
            except OSError:
                pass
            return

        try:
            parsed = parse(message)
        except ValueError as e:
            self._send_response(error=str(e), prompt=True)
            return

        if parsed is None:
            # Fall back to LLM agent if available
            if self._agent:
                self._handle_ai_query(message)
                return

            self._send_response(
                error=f"Unknown command: '{message.split()[0]}'. Type 'help' for available commands.",
                prompt=True,
            )
            return

        # Handle approval management commands directly
        if parsed.tool_name.startswith("_"):
            self._handle_management_command(parsed.tool_name, parsed.parameters)
            return

        # Execute tool through the approval manager
        self._execute_tool(parsed.tool_name, parsed.parameters)

    def _handle_ai_query(self, message: str):
        """Handle a natural-language query via the LLM agent."""
        try:
            agent_response = self._agent.query(message)

            self._send_response(
                message=agent_response.message,
                requires_approval=agent_response.requires_approval,
                actions_proposed=(
                    [{"tool": agent_response.pending_tool,
                      "params": agent_response.pending_params,
                      "approval_id": agent_response.pending_approval_id}]
                    if agent_response.requires_approval else None
                ),
                prompt=True,
            )
        except Exception as e:
            logging.error("AI query failed: %s", e)
            self._send_response(error=f"AI query failed: {e}", prompt=True)

    def _handle_management_command(self, cmd: str, params: dict):
        """Handle approval management commands (_approve, _deny, _pending, _audit)."""
        if cmd == "_pending":
            pending = self._approval_manager.get_pending()
            if not pending:
                self._send_response(message="No pending approvals.", prompt=True)
                return

            lines = ["Pending approvals:"]
            for aid, req in pending.items():
                lines.append(
                    f"  [{aid}] {req['tool_name']}({req['parameters']}) "
                    f"level={req['approval_level'].value}"
                )
            self._send_response(message="\n".join(lines), prompt=True)

        elif cmd == "_approve":
            aid = params.get("approval_id", "")
            request = self._approval_manager.approve(aid)
            if request is None:
                self._send_response(error=f"No pending approval with id '{aid}'", prompt=True)
                return

            # Execute the approved tool
            self._send_response(
                message=f"Approved. Executing {request['tool_name']}...",
            )
            self._execute_tool(
                request["tool_name"],
                request["parameters"],
                force_approve=True,
            )

        elif cmd == "_deny":
            aid = params.get("approval_id", "")
            request = self._approval_manager.deny(aid)
            if request is None:
                self._send_response(error=f"No pending approval with id '{aid}'", prompt=True)
            else:
                self._send_response(
                    message=f"Denied {request['tool_name']}({request['parameters']})",
                    prompt=True,
                )

        elif cmd == "_audit":
            count = int(params.get("count", 20))
            entries = self._approval_manager.audit_logger.get_recent(count)
            if not entries:
                self._send_response(message="No audit log entries.", prompt=True)
                return

            lines = [f"Last {len(entries)} audit entries:"]
            for entry in entries:
                approved = "APPROVED" if entry.get("approved") else "DENIED"
                success = "OK" if entry.get("result", {}).get("success") else "FAIL"
                lines.append(
                    f"  [{entry.get('timestamp', '?')}] "
                    f"{entry.get('tool', '?')} {approved} {success} "
                    f"user={entry.get('user', '?')}"
                )
            self._send_response(message="\n".join(lines), prompt=True)

        elif cmd == "_alerts":
            self._handle_alerts_command(params)

        elif cmd == "_acknowledge":
            self._handle_acknowledge_command(params)

        elif cmd == "_cluster_health":
            self._handle_cluster_health_command()

        elif cmd == "_host_analysis":
            self._handle_host_analysis_command(params)

        elif cmd == "_events":
            self._handle_events_command(params)

    def _get_observer(self):
        """Get the global observer instance (if running)."""
        try:
            from .event_bus import get_event_bus
            bus = get_event_bus()
            # The observer registers itself on the bus; we need a reference.
            # By convention, main.py stores it; we access via the bus subscribers.
            # Simpler: import the module-level observer if it exists.
            from . import _observer
            return _observer
        except (ImportError, AttributeError):
            return None

    def _handle_alerts_command(self, params: dict):
        """Show AI observer alerts."""
        observer = self._get_observer()
        if observer is None:
            self._send_response(message="AI observer is not running.", prompt=True)
            return

        severity = params.get("severity")
        minutes = params.get("minutes")
        if isinstance(minutes, str):
            try:
                minutes = int(minutes)
            except ValueError:
                minutes = None

        alerts = observer.get_alerts(severity=severity, minutes=minutes)
        if not alerts:
            self._send_response(message="No alerts.", prompt=True)
            return

        lines = [f"Alerts ({len(alerts)}):"]
        for i, alert in enumerate(alerts):
            ack = " [ACK]" if alert.acknowledged else ""
            lines.append(
                f"  [{i}] [{alert.severity.upper()}] {alert.time_str} "
                f"{alert.pattern}: {alert.message}{ack}"
            )
            if alert.suggestion:
                lines.append(f"      Suggestion: {alert.suggestion}")
        self._send_response(message="\n".join(lines), prompt=True)

    def _handle_acknowledge_command(self, params: dict):
        """Acknowledge an alert by index."""
        observer = self._get_observer()
        if observer is None:
            self._send_response(message="AI observer is not running.", prompt=True)
            return

        try:
            index = int(params.get("index", -1))
        except (ValueError, TypeError):
            self._send_response(error="Index must be a number.", prompt=True)
            return

        if observer.acknowledge_alert(index):
            self._send_response(message=f"Alert {index} acknowledged.", prompt=True)
        else:
            self._send_response(error=f"Invalid alert index: {index}", prompt=True)

    def _handle_cluster_health_command(self):
        """Show AI-assessed cluster health."""
        observer = self._get_observer()
        if observer is None:
            self._send_response(message="AI observer is not running.", prompt=True)
            return

        health = observer.get_cluster_health()
        lines = [f"Cluster Health: {health['health'].upper()}"]
        lines.append(f"  Total alerts: {health['total_alerts']} ({health['unacknowledged']} unacknowledged)")
        if health['severity']:
            lines.append(f"  By severity: {health['severity']}")
        lines.append(f"  Recent down events: {health['recent_down_events']}")
        if health['problematic_hosts']:
            lines.append("  Problematic hosts:")
            for h in health['problematic_hosts']:
                lines.append(f"    {h['host']}: {h['alerts']} alerts")
        self._send_response(message="\n".join(lines), prompt=True)

    def _handle_host_analysis_command(self, params: dict):
        """Show AI analysis for a specific host."""
        observer = self._get_observer()
        if observer is None:
            self._send_response(message="AI observer is not running.", prompt=True)
            return

        host = params.get("host", "")
        if not host:
            self._send_response(error="Host parameter required.", prompt=True)
            return

        analysis = observer.get_host_analysis(host)
        lines = [f"Host Analysis: {host}"]
        lines.append(f"  Recent fencing events: {analysis['fence_count_recent']}")
        lines.append(f"  State transitions: {analysis['state_transitions']}")
        lines.append(f"  Evacuation success: {analysis['evacuation_success']}")
        lines.append(f"  Evacuation failures: {analysis['evacuation_failures']}")
        lines.append(f"  Active alerts: {analysis['active_alerts']}")
        if analysis['alerts']:
            lines.append("  Recent alerts:")
            for a in analysis['alerts']:
                lines.append(f"    [{a['severity']}] {a['time_str']} {a['message']}")
        self._send_response(message="\n".join(lines), prompt=True)

    def _handle_events_command(self, params: dict):
        """Show recent event bus events."""
        try:
            from .event_bus import get_event_bus
            bus = get_event_bus()
        except Exception:
            self._send_response(message="Event bus is not available.", prompt=True)
            return

        minutes = params.get("minutes")
        if isinstance(minutes, str):
            try:
                minutes = int(minutes)
            except ValueError:
                minutes = None
        if minutes is None:
            minutes = 60

        host = params.get("host")
        events = bus.get_events(minutes=minutes, host=host)

        if not events:
            self._send_response(message="No events in the specified time window.", prompt=True)
            return

        lines = [f"Events (last {minutes} min, {len(events)} total):"]
        for event in events[-50:]:  # Show last 50
            host_str = f" host={event.host}" if event.host else ""
            lines.append(
                f"  [{event.time_str}] {event.event_type.value}{host_str} "
                f"source={event.source} data={event.data}"
            )
        if len(events) > 50:
            lines.append(f"  ... ({len(events) - 50} more events not shown)")
        self._send_response(message="\n".join(lines), prompt=True)

    def _execute_tool(self, tool_name: str, params: dict, force_approve: bool = False):
        """Execute a tool through the approval manager, injecting connection/service."""
        tool_obj = tool_registry.get(tool_name)
        if tool_obj is None:
            self._send_response(error=f"Unknown tool: {tool_name}", prompt=True)
            return

        # Pass only user-supplied params to the approval manager (these get
        # logged/serialized). Connection and service are injected at execution
        # time only -- they are not serializable and should not be persisted.
        result = self._approval_manager.execute_tool(
            tool_registry, tool_name, params,
            user="chat", force_approve=force_approve,
            inject_kwargs=self._build_inject_kwargs(tool_obj),
        )

        if result.error == "approval_required":
            aid = result.data.get("approval_id", "?")
            level = result.data.get("approval_level", "?")
            self._send_response(
                message=f"Action requires approval (level={level}).\n"
                        f"  Approval ID: {aid}\n"
                        f"  Type 'approve {aid}' to proceed or 'deny {aid}' to cancel.",
                requires_approval=True,
                actions_proposed=[{"tool": tool_name, "params": params, "approval_id": aid}],
                prompt=True,
            )
            return

        if not result.success:
            self._send_response(error=result.error or "Tool execution failed", prompt=True)
            return

        self._send_response(
            message=self._format_result(tool_name, result),
            prompt=True,
        )

    def _build_inject_kwargs(self, tool_obj) -> dict:
        """Build kwargs to inject at execution time (connection, service)."""
        inject = {}
        expected = tool_obj.parameters or {}
        if "connection" in expected:
            inject["connection"] = self._nova_connection
        if "service" in expected:
            inject["service"] = self._service
        return inject

    def _format_result(self, tool_name: str, result: ToolResult) -> str:
        """Format a ToolResult for human-readable display."""
        data = result.data

        if isinstance(data, dict) and data.get("dry_run"):
            return (
                f"[DRY-RUN] Would execute: {data.get('would_execute')}\n"
                f"  Parameters: {data.get('parameters')}\n"
                f"  Set dry_run=False in ApprovalManager to execute for real."
            )

        lines = [f"Result ({tool_name}):"]

        if isinstance(data, list):
            for item in data:
                if isinstance(item, dict):
                    parts = [f"{k}={v}" for k, v in item.items()]
                    lines.append(f"  - {', '.join(parts)}")
                else:
                    lines.append(f"  - {item}")
        elif isinstance(data, dict):
            for key, value in data.items():
                if isinstance(value, list):
                    lines.append(f"  {key}:")
                    for item in value:
                        lines.append(f"    - {item}")
                elif isinstance(value, dict):
                    lines.append(f"  {key}:")
                    for k, v in value.items():
                        lines.append(f"    {k}: {v}")
                else:
                    lines.append(f"  {key}: {value}")
        elif data is not None:
            lines.append(f"  {data}")

        if result.duration_seconds is not None:
            lines.append(f"  (completed in {result.duration_seconds:.2f}s)")

        return "\n".join(lines)

    def _send_response(self, message: str = "", error: str = "",
                       requires_approval: bool = False,
                       actions_proposed: Optional[list] = None,
                       prompt: bool = False):
        """Send a JSON-lines response to the client."""
        response = {
            "type": "response",
            "message": message,
            "requires_approval": requires_approval,
        }
        if error:
            response["error"] = error
        if actions_proposed:
            response["actions_proposed"] = actions_proposed
        if prompt:
            response["prompt"] = True

        try:
            line = json.dumps(response) + "\n"
            self._conn.sendall(line.encode("utf-8"))
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass


class ChatServer:
    """Unix domain socket server for the InstanceHA chat interface.

    Designed to run as a daemon thread.  Call ``start()`` to launch, or
    call ``serve_forever()`` directly if you manage threading yourself.
    """

    def __init__(self, nova_connection, service,
                 approval_manager: Optional[ApprovalManager] = None,
                 socket_path: str = SOCKET_PATH,
                 llm_engine=None):
        self._nova_connection = nova_connection
        self._service = service
        self._socket_path = socket_path
        self._approval_manager = approval_manager or ApprovalManager(
            auto_approve_level=ApprovalLevel.NONE,
            dry_run=True,
        )
        self._llm_engine = llm_engine
        self._server_socket: Optional[socket.socket] = None
        self._running = False

    def start(self) -> threading.Thread:
        """Start the server in a daemon thread. Returns the thread."""
        thread = threading.Thread(target=self.serve_forever, name="chat-server")
        thread.daemon = True
        thread.start()
        logging.info("Chat server started on %s", self._socket_path)
        return thread

    def serve_forever(self):
        """Main server loop -- blocks until stop() is called."""
        # Clean up stale socket
        try:
            os.unlink(self._socket_path)
        except FileNotFoundError:
            pass

        # Ensure directory exists
        sock_dir = os.path.dirname(self._socket_path)
        if sock_dir:
            os.makedirs(sock_dir, exist_ok=True)

        self._server_socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._server_socket.bind(self._socket_path)
        self._server_socket.listen(5)
        self._server_socket.settimeout(1.0)
        self._running = True

        logging.info("Chat server listening on %s", self._socket_path)

        while self._running:
            try:
                conn, addr = self._server_socket.accept()
            except socket.timeout:
                continue
            except OSError:
                if self._running:
                    logging.warning("Chat server socket error")
                break

            session = ChatSession(
                conn, addr,
                self._nova_connection, self._service,
                self._approval_manager,
                llm_engine=self._llm_engine,
            )
            client_thread = threading.Thread(
                target=session.handle,
                name="chat-client",
                daemon=True,
            )
            client_thread.start()

        self._cleanup()

    def stop(self):
        """Signal the server to stop."""
        self._running = False
        self._cleanup()

    def _cleanup(self):
        """Clean up the server socket."""
        if self._server_socket:
            try:
                self._server_socket.close()
            except OSError:
                pass
        try:
            os.unlink(self._socket_path)
        except (FileNotFoundError, OSError):
            pass

    def update_connection(self, nova_connection):
        """Update the Nova connection reference (e.g. after reconnection)."""
        self._nova_connection = nova_connection
