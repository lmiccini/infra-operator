"""Standalone test mode for the InstanceHA AI layer.

Starts the chat server, observer, and optionally the MCP server with a
mock Nova connection so the AI layer can be exercised without a live
OpenStack deployment.

Usage (inside container)::

    python3 -m instanceha_ai
    # then in another shell:
    INSTANCEHA_SOCKET=/tmp/instanceha-test.sock instanceha-chat

Usage (local development)::

    cd templates/instanceha/bin
    python3 -m instanceha_ai --socket /tmp/iha.sock -v
    # then:
    INSTANCEHA_SOCKET=/tmp/iha.sock python3 -m instanceha_ai.chat_client
"""

import argparse
import logging
import signal
import sys
import threading
import types
from datetime import datetime, timedelta


def _install_stubs():
    """Install stub modules so the package can import without real dependencies."""
    if "instanceha" not in sys.modules:
        stub = types.ModuleType("instanceha")
        stub._host_fence = lambda *a, **kw: True
        stub._host_evacuate = lambda *a, **kw: True
        stub._host_disable = lambda *a, **kw: True
        stub._host_enable = lambda *a, **kw: True
        _evac_result = types.SimpleNamespace(accepted=True, reason="standalone dry-run")
        stub._server_evacuate = lambda *a, **kw: _evac_result
        stub._extract_hostname = lambda h: h.split(".")[0]
        sys.modules["instanceha"] = stub

    if "novaclient" not in sys.modules:
        sys.modules["novaclient"] = types.ModuleType("novaclient")
        sys.modules["novaclient.client"] = types.ModuleType("novaclient.client")
        exc = types.ModuleType("novaclient.exceptions")
        exc.NotFound = type("NotFound", (Exception,), {})
        exc.Conflict = type("Conflict", (Exception,), {})
        exc.Forbidden = type("Forbidden", (Exception,), {})
        exc.Unauthorized = type("Unauthorized", (Exception,), {})
        sys.modules["novaclient.exceptions"] = exc

    if "keystoneauth1" not in sys.modules:
        for mod in (
            "keystoneauth1",
            "keystoneauth1.loading",
            "keystoneauth1.session",
            "keystoneauth1.exceptions",
            "keystoneauth1.exceptions.discovery",
            "keystoneauth1.exceptions.connection",
            "keystoneauth1.exceptions.http",
        ):
            sys.modules[mod] = types.ModuleType(mod)


def _make_compute_service(host, state, status, forced_down=False,
                          disabled_reason="", zone="nova"):
    return types.SimpleNamespace(
        host=host, state=state, status=status,
        forced_down=forced_down, disabled_reason=disabled_reason,
        updated_at=datetime.now().isoformat(), zone=zone, binary="nova-compute",
    )


def _make_server(server_id, name, status, host,
                 flavor_id="m1.small", image_id="img-001"):
    return types.SimpleNamespace(
        id=server_id, name=name, status=status, host=host,
        flavor={"id": flavor_id}, image={"id": image_id},
    )


def _make_migration(mid, instance_uuid, source, dest, status, created_at=None):
    if created_at is None:
        created_at = datetime.now().isoformat()
    return types.SimpleNamespace(
        id=mid, instance_uuid=instance_uuid,
        source_compute=source, dest_compute=dest,
        status=status, created_at=created_at, updated_at=created_at,
    )


def _build_mock_connection():
    compute_services = [
        _make_compute_service("compute-0", "up", "enabled"),
        _make_compute_service("compute-1", "up", "enabled"),
        _make_compute_service("compute-2", "up", "enabled"),
        _make_compute_service("compute-3", "down", "disabled", forced_down=True,
                              disabled_reason="instanceha: evacuation in progress"),
        _make_compute_service("compute-4", "up", "disabled", forced_down=False,
                              disabled_reason="maintenance"),
    ]

    servers = {
        "compute-0": [
            _make_server("vm-001", "web-frontend-1", "ACTIVE", "compute-0"),
            _make_server("vm-002", "web-frontend-2", "ACTIVE", "compute-0"),
        ],
        "compute-1": [
            _make_server("vm-003", "db-primary", "ACTIVE", "compute-1", "m1.xlarge"),
            _make_server("vm-004", "db-replica", "ACTIVE", "compute-1", "m1.xlarge"),
            _make_server("vm-005", "cache-01", "ACTIVE", "compute-1"),
        ],
        "compute-2": [
            _make_server("vm-006", "worker-1", "ACTIVE", "compute-2"),
            _make_server("vm-007", "worker-2", "ACTIVE", "compute-2"),
        ],
        "compute-3": [
            _make_server("vm-008", "api-gateway", "ACTIVE", "compute-3"),
            _make_server("vm-009", "monitoring", "ACTIVE", "compute-3"),
        ],
        "compute-4": [],
    }

    migrations = [
        _make_migration(1, "vm-010", "compute-3", "compute-0", "completed",
                        (datetime.now() - timedelta(minutes=25)).isoformat()),
        _make_migration(2, "vm-011", "compute-3", "compute-1", "error",
                        (datetime.now() - timedelta(minutes=20)).isoformat()),
    ]

    conn = types.SimpleNamespace()
    conn.services = types.SimpleNamespace()
    conn.servers = types.SimpleNamespace()
    conn.migrations = types.SimpleNamespace()

    def services_list(binary=None, host=None):
        result = compute_services
        if host:
            result = [s for s in result if s.host == host]
        return result

    def servers_list(search_opts=None):
        if search_opts and "host" in search_opts:
            return servers.get(search_opts["host"], [])
        all_servers = []
        for svr_list in servers.values():
            all_servers.extend(svr_list)
        return all_servers

    def migrations_list(**kwargs):
        result = migrations
        source = kwargs.get("source_compute")
        if source:
            result = [m for m in result if m.source_compute == source]
        instance = kwargs.get("instance_uuid")
        if instance:
            result = [m for m in result if m.instance_uuid == instance]
        return result

    conn.services.list = services_list
    conn.servers.list = servers_list
    conn.migrations.list = migrations_list
    return conn


def _build_mock_service():
    config_values = {
        "THRESHOLD": 50,
        "AI_ENABLED": "true",
        "AI_BACKEND": "",
        "AI_ENDPOINT": "",
        "AI_MODEL": "",
        "AI_API_KEY": "",
        "AI_MODEL_PATH": "",
        "AI_MCP_ENABLED": "false",
        "AI_CHAT_ENABLED": "true",
        "AI_OBSERVER_ENABLED": "true",
    }
    config_map = {}
    for key, val in config_values.items():
        config_map[key] = types.SimpleNamespace(type=type(val).__name__, value=val)

    config = types.SimpleNamespace()
    config.get_config_value = lambda k: config_values.get(k)
    config._config_map = config_map

    svc = types.SimpleNamespace()
    svc.config = config
    svc.kdump_fenced_hosts = set()
    svc.hosts_processing = {}
    svc.kdump_hosts_timestamp = {}
    svc.kdump_hosts_checking = {}
    return svc


def _inject_sample_events(bus, EventType, Event):
    events = [
        Event(event_type=EventType.SERVICE_STATE_CHANGE, host="compute-3",
              data={"state": "down", "previous_state": "up"}, source="standalone"),
        Event(event_type=EventType.FENCE_START, host="compute-3",
              data={"action": "off"}, source="standalone"),
        Event(event_type=EventType.FENCE_RESULT, host="compute-3",
              data={"action": "off", "success": True}, source="standalone"),
        Event(event_type=EventType.HOST_DISABLED, host="compute-3",
              data={"reason": "instanceha: evacuation in progress"},
              source="standalone"),
        Event(event_type=EventType.EVACUATION_START, host="compute-3",
              data={"server_count": 2}, source="standalone"),
        Event(event_type=EventType.EVACUATION_RESULT, host="compute-3",
              data={"success": True, "server_count": 1, "server_id": "vm-010"},
              source="standalone"),
        Event(event_type=EventType.EVACUATION_RESULT, host="compute-3",
              data={"success": False, "server_count": 1, "server_id": "vm-011",
                    "error": "NoValidHost"}, source="standalone"),
    ]
    for event in events:
        bus.publish(event)


def main():
    _install_stubs()

    parser = argparse.ArgumentParser(
        prog="python3 -m instanceha_ai",
        description="InstanceHA AI layer — standalone test mode",
    )
    parser.add_argument(
        "--socket", default="/tmp/instanceha-test.sock",
        help="Unix socket path for chat server (default: /tmp/instanceha-test.sock)",
    )
    parser.add_argument(
        "--mcp-port", type=int, default=8081,
        help="MCP server port (default: 8081)",
    )
    parser.add_argument("--no-mcp", action="store_true", help="Disable MCP server")
    parser.add_argument(
        "--no-sample-events", action="store_true",
        help="Don't inject sample events into the event bus",
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true", help="Enable debug logging",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    import instanceha_ai
    from instanceha_ai.event_bus import Event, EventBus, EventType, set_event_bus
    from instanceha_ai.observer import Observer
    from instanceha_ai.safety import ApprovalManager, AuditLogger
    from instanceha_ai.tools import ApprovalLevel
    from instanceha_ai.chat_server import ChatServer

    conn = _build_mock_connection()
    service = _build_mock_service()

    bus = EventBus()
    set_event_bus(bus)

    observer = Observer(bus)
    observer.start()
    instanceha_ai._observer = observer
    logging.info("Observer started")

    if not args.no_sample_events:
        _inject_sample_events(bus, EventType, Event)
        logging.info("Injected sample events")

    audit_logger = AuditLogger(log_path="/tmp/instanceha-standalone-audit.log")
    approval_manager = ApprovalManager(
        auto_approve_level=ApprovalLevel.NONE,
        audit_logger=audit_logger,
        dry_run=True,
    )

    chat = ChatServer(
        nova_connection=conn,
        service=service,
        approval_manager=approval_manager,
        socket_path=args.socket,
    )
    chat.start()

    mcp_thread = None
    if not args.no_mcp:
        try:
            from instanceha_ai.mcp_server import InstanceHAMCPServer, _mcp_available
            if _mcp_available:
                mcp = InstanceHAMCPServer(
                    nova_connection=conn,
                    service=service,
                    approval_manager=approval_manager,
                    port=args.mcp_port,
                    allow_writes=False,
                )
                mcp_thread = mcp.start()
                if mcp_thread:
                    logging.info("MCP server listening on port %d", args.mcp_port)
            else:
                logging.info("MCP SDK not installed, MCP server disabled")
        except Exception as e:
            logging.warning("MCP server failed to start: %s", e)

    print()
    print("=" * 60)
    print("  InstanceHA AI — Standalone Test Mode")
    print("=" * 60)
    print()
    print(f"  Chat socket:  {args.socket}")
    if mcp_thread:
        print(f"  MCP server:   http://0.0.0.0:{args.mcp_port}")
    print("  Audit log:    /tmp/instanceha-standalone-audit.log")
    print("  Dry-run mode: enabled (write operations are simulated)")
    print()
    print("  Mock cluster: 5 computes (3 up, 1 down, 1 maintenance)")
    print("  Sample events: %s" % ("injected" if not args.no_sample_events else "none"))
    print()
    print("  Connect with:")
    print(f"    INSTANCEHA_SOCKET={args.socket} python3 -m instanceha_ai.chat_client")
    print()
    print("  Press Ctrl+C to stop.")
    print("=" * 60)

    stop = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    stop.wait()

    print("\nShutting down...")
    chat.stop()


if __name__ == "__main__":
    main()
