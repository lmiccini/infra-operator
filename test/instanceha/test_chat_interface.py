#!/usr/bin/env python3
"""Tests for the InstanceHA chat interface (Phase 3).

Covers command parsing, chat server/client protocol, and approval flow.
"""

import json
import os
import socket
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, MagicMock, patch

# Mock OpenStack dependencies before importing instanceha
if 'novaclient' not in sys.modules:
    sys.modules['novaclient'] = MagicMock()
    sys.modules['novaclient.client'] = MagicMock()

    class NotFound(Exception):
        pass
    class Conflict(Exception):
        pass
    class Forbidden(Exception):
        pass
    class Unauthorized(Exception):
        pass

    novaclient_exceptions = MagicMock()
    novaclient_exceptions.NotFound = NotFound
    novaclient_exceptions.Conflict = Conflict
    novaclient_exceptions.Forbidden = Forbidden
    novaclient_exceptions.Unauthorized = Unauthorized
    sys.modules['novaclient.exceptions'] = novaclient_exceptions

if 'keystoneauth1' not in sys.modules:
    sys.modules['keystoneauth1'] = MagicMock()
    sys.modules['keystoneauth1.loading'] = MagicMock()
    sys.modules['keystoneauth1.session'] = MagicMock()
    sys.modules['keystoneauth1.exceptions'] = MagicMock()
    sys.modules['keystoneauth1.exceptions.discovery'] = MagicMock()

instanceha_path = os.path.join(os.path.dirname(__file__), '..', '..', 'templates', 'instanceha', 'bin')
sys.path.insert(0, os.path.abspath(instanceha_path))

from instanceha.ai.command_parser import parse, get_help_text, ParsedCommand
from instanceha.ai.chat_server import ChatServer, ChatSession, SOCKET_PATH
from instanceha.ai.safety import ApprovalManager, AuditLogger
from instanceha.ai.tools import ApprovalLevel, ToolResult, registry


# ─── Command Parser Tests ────────────────────────────────────────────


class TestCommandParser(unittest.TestCase):
    """Tests for the command_parser module."""

    def test_parse_empty_input(self):
        self.assertIsNone(parse(""))
        self.assertIsNone(parse("   "))

    def test_parse_unknown_command(self):
        self.assertIsNone(parse("foobar"))

    def test_parse_status_no_args(self):
        result = parse("status")
        self.assertIsNotNone(result)
        self.assertEqual(result.tool_name, "get_compute_services")
        self.assertEqual(result.parameters, {})

    def test_parse_status_with_host(self):
        result = parse("status compute-1")
        self.assertIsNotNone(result)
        self.assertEqual(result.tool_name, "get_service_status")
        self.assertEqual(result.parameters, {"host": "compute-1"})

    def test_parse_status_alias(self):
        result = parse("st compute-1")
        self.assertIsNotNone(result)
        self.assertEqual(result.tool_name, "get_service_status")
        self.assertEqual(result.parameters, {"host": "compute-1"})

    def test_parse_servers(self):
        result = parse("servers compute-2")
        self.assertIsNotNone(result)
        self.assertEqual(result.tool_name, "get_servers_on_host")
        self.assertEqual(result.parameters, {"host": "compute-2"})

    def test_parse_servers_alias(self):
        result = parse("vms compute-2")
        self.assertIsNotNone(result)
        self.assertEqual(result.tool_name, "get_servers_on_host")

    def test_parse_servers_missing_arg(self):
        with self.assertRaises(ValueError) as ctx:
            parse("servers")
        self.assertIn("Too few arguments", str(ctx.exception))

    def test_parse_fence(self):
        result = parse("fence compute-3 off")
        self.assertIsNotNone(result)
        self.assertEqual(result.tool_name, "fence_host")
        self.assertEqual(result.parameters, {"host": "compute-3", "action": "off"})

    def test_parse_fence_missing_action(self):
        with self.assertRaises(ValueError):
            parse("fence compute-3")

    def test_parse_evacuate(self):
        result = parse("evacuate compute-4")
        self.assertIsNotNone(result)
        self.assertEqual(result.tool_name, "evacuate_host")
        self.assertEqual(result.parameters, {"host": "compute-4"})

    def test_parse_evacuate_with_target(self):
        result = parse("evacuate compute-4 compute-5")
        self.assertEqual(result.parameters, {"host": "compute-4", "target_host": "compute-5"})

    def test_parse_evacuate_alias(self):
        result = parse("evac compute-4")
        self.assertEqual(result.tool_name, "evacuate_host")

    def test_parse_disable(self):
        result = parse("disable compute-6")
        self.assertEqual(result.tool_name, "disable_host")
        self.assertEqual(result.parameters, {"host": "compute-6"})

    def test_parse_enable(self):
        result = parse("enable compute-7")
        self.assertEqual(result.tool_name, "enable_host")
        self.assertEqual(result.parameters, {"host": "compute-7"})

    def test_parse_evacuate_server(self):
        result = parse("evacuate-server abc-123")
        self.assertEqual(result.tool_name, "evacuate_server")
        self.assertEqual(result.parameters, {"server_id": "abc-123"})

    def test_parse_evacuate_server_with_target(self):
        result = parse("evacuate-server abc-123 compute-8")
        self.assertEqual(result.parameters, {"server_id": "abc-123", "target_host": "compute-8"})

    def test_parse_diagnose(self):
        result = parse("diagnose compute-9")
        self.assertEqual(result.tool_name, "diagnose_evacuation_failure")

    def test_parse_diagnose_alias(self):
        result = parse("diag compute-9")
        self.assertEqual(result.tool_name, "diagnose_evacuation_failure")

    def test_parse_capacity(self):
        result = parse("capacity")
        self.assertEqual(result.tool_name, "check_cluster_capacity")
        self.assertEqual(result.parameters, {})

    def test_parse_capacity_with_host(self):
        result = parse("capacity compute-10")
        self.assertEqual(result.parameters, {"host": "compute-10"})

    def test_parse_correlate(self):
        result = parse("correlate")
        self.assertEqual(result.tool_name, "correlate_events")
        self.assertEqual(result.parameters, {})

    def test_parse_correlate_with_minutes(self):
        result = parse("correlate 60")
        self.assertEqual(result.parameters, {"minutes": 60})

    def test_parse_correlate_bad_minutes(self):
        with self.assertRaises(ValueError) as ctx:
            parse("correlate abc")
        self.assertIn("must be a number", str(ctx.exception))

    def test_parse_history(self):
        result = parse("history")
        self.assertEqual(result.tool_name, "get_evacuation_history")

    def test_parse_history_with_minutes(self):
        result = parse("history 120")
        self.assertEqual(result.parameters, {"minutes": 120})

    def test_parse_summary(self):
        result = parse("summary")
        self.assertEqual(result.tool_name, "get_cluster_summary")

    def test_parse_summary_alias(self):
        result = parse("health")
        self.assertEqual(result.tool_name, "get_cluster_summary")

    def test_parse_config(self):
        result = parse("config")
        self.assertEqual(result.tool_name, "get_config")

    def test_parse_config_alias(self):
        result = parse("cfg")
        self.assertEqual(result.tool_name, "get_config")

    def test_parse_kdump(self):
        result = parse("kdump")
        self.assertEqual(result.tool_name, "get_kdump_hosts")

    def test_parse_migration(self):
        result = parse("migration abc-def")
        self.assertEqual(result.tool_name, "get_migration_status")
        self.assertEqual(result.parameters, {"server_id": "abc-def"})

    def test_parse_approve(self):
        result = parse("approve abc123")
        self.assertEqual(result.tool_name, "_approve")
        self.assertEqual(result.parameters, {"approval_id": "abc123"})

    def test_parse_deny(self):
        result = parse("deny abc123")
        self.assertEqual(result.tool_name, "_deny")
        self.assertEqual(result.parameters, {"approval_id": "abc123"})

    def test_parse_pending(self):
        result = parse("pending")
        self.assertEqual(result.tool_name, "_pending")

    def test_parse_audit(self):
        result = parse("audit")
        self.assertEqual(result.tool_name, "_audit")
        self.assertEqual(result.parameters, {})

    def test_parse_audit_with_count(self):
        result = parse("audit 50")
        self.assertEqual(result.parameters, {"count": 50})

    def test_parse_too_many_args(self):
        with self.assertRaises(ValueError) as ctx:
            parse("status host1 host2")
        self.assertIn("Too many arguments", str(ctx.exception))

    def test_parse_case_insensitive(self):
        result = parse("STATUS")
        self.assertIsNotNone(result)
        self.assertEqual(result.tool_name, "get_compute_services")

    def test_parse_preserves_raw(self):
        result = parse("status compute-1")
        self.assertEqual(result.raw, "status compute-1")

    def test_parse_quoted_args(self):
        result = parse('servers "compute node 1"')
        self.assertEqual(result.parameters, {"host": "compute node 1"})


class TestHelpText(unittest.TestCase):
    """Tests for help text generation."""

    def test_help_text_contains_all_commands(self):
        text = get_help_text()
        for cmd in ["status", "servers", "fence", "evacuate", "diagnose",
                     "capacity", "approve", "deny", "pending"]:
            self.assertIn(cmd, text, f"Help text missing command: {cmd}")

    def test_help_text_contains_categories(self):
        text = get_help_text()
        self.assertIn("Read-Only", text)
        self.assertIn("Write", text)
        self.assertIn("Diagnostic", text)
        self.assertIn("Approval Management", text)


# ─── Chat Server Protocol Tests ──────────────────────────────────────


class TestChatServerProtocol(unittest.TestCase):
    """Tests for the chat server using a real Unix socket."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.socket_path = os.path.join(self.tmpdir, "test.sock")
        self.audit_log_path = os.path.join(self.tmpdir, "audit.log")

        self.mock_conn = Mock()
        self.mock_service = Mock()
        self.mock_service.config.get_config_value.return_value = 50

        self.audit_logger = AuditLogger(self.audit_log_path)
        self.approval_manager = ApprovalManager(
            auto_approve_level=ApprovalLevel.NONE,
            audit_logger=self.audit_logger,
            dry_run=True,
        )

        self.server = ChatServer(
            nova_connection=self.mock_conn,
            service=self.mock_service,
            approval_manager=self.approval_manager,
            socket_path=self.socket_path,
        )
        self.server_thread = self.server.start()
        # Give the server a moment to bind
        time.sleep(0.1)

    def tearDown(self):
        self.server.stop()
        time.sleep(0.1)
        try:
            import shutil
            shutil.rmtree(self.tmpdir)
        except OSError:
            pass

    def _connect(self) -> socket.socket:
        """Connect a client socket."""
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.connect(self.socket_path)
        sock.settimeout(2.0)
        return sock

    def _send(self, sock: socket.socket, msg: dict):
        """Send a JSON message."""
        sock.sendall((json.dumps(msg) + "\n").encode("utf-8"))

    def _recv(self, sock: socket.socket) -> dict:
        """Receive and parse a JSON response."""
        buf = b""
        while b"\n" not in buf:
            data = sock.recv(65536)
            if not data:
                raise ConnectionError("Server closed connection")
            buf += data
        line = buf.split(b"\n")[0]
        return json.loads(line.decode("utf-8"))

    def _recv_all(self, sock: socket.socket, count: int) -> list:
        """Receive multiple JSON responses."""
        results = []
        buf = b""
        while len(results) < count:
            data = sock.recv(65536)
            if not data:
                break
            buf += data
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                line = line.strip()
                if line:
                    results.append(json.loads(line.decode("utf-8")))
        return results

    def test_connect_receives_welcome(self):
        sock = self._connect()
        try:
            response = self._recv(sock)
            self.assertEqual(response["type"], "response")
            self.assertIn("InstanceHA", response["message"])
        finally:
            sock.close()

    def test_help_command(self):
        sock = self._connect()
        try:
            # Consume welcome
            self._recv(sock)
            # Send help
            self._send(sock, {"type": "query", "message": "help"})
            response = self._recv(sock)
            self.assertIn("Available commands", response["message"])
            self.assertTrue(response.get("prompt"))
        finally:
            sock.close()

    def test_unknown_command(self):
        sock = self._connect()
        try:
            self._recv(sock)
            self._send(sock, {"type": "query", "message": "foobar"})
            response = self._recv(sock)
            self.assertIn("error", response)
            self.assertIn("Unknown command", response["error"])
        finally:
            sock.close()

    def test_empty_query(self):
        sock = self._connect()
        try:
            self._recv(sock)
            self._send(sock, {"type": "query", "message": ""})
            response = self._recv(sock)
            self.assertTrue(response.get("prompt"))
        finally:
            sock.close()

    def test_invalid_json(self):
        sock = self._connect()
        try:
            self._recv(sock)
            sock.sendall(b"not json\n")
            response = self._recv(sock)
            self.assertIn("error", response)
            self.assertIn("Invalid JSON", response["error"])
        finally:
            sock.close()

    def test_pending_no_approvals(self):
        sock = self._connect()
        try:
            self._recv(sock)
            self._send(sock, {"type": "query", "message": "pending"})
            response = self._recv(sock)
            self.assertIn("No pending", response["message"])
        finally:
            sock.close()

    def test_status_calls_tool(self):
        """Test that 'status' command executes the get_compute_services tool."""
        svc1 = Mock(host="h1", state="up", status="enabled", forced_down=False,
                    disabled_reason="", updated_at="2026-01-01T00:00:00", zone="nova")
        svc2 = Mock(host="h2", state="down", status="disabled", forced_down=True,
                    disabled_reason="test", updated_at="2026-01-01T00:00:00", zone="nova")
        self.mock_conn.services.list.return_value = [svc1, svc2]

        sock = self._connect()
        try:
            self._recv(sock)
            self._send(sock, {"type": "query", "message": "status"})
            response = self._recv(sock)
            # Should get a successful result
            self.assertNotIn("error", response)
            self.assertIn("message", response)
        finally:
            sock.close()

    def test_write_command_requires_approval(self):
        """Test that write commands trigger approval workflow."""
        sock = self._connect()
        try:
            self._recv(sock)
            self._send(sock, {"type": "query", "message": "fence compute-1 off"})
            response = self._recv(sock)
            self.assertTrue(response.get("requires_approval"))
            self.assertIn("actions_proposed", response)
            self.assertIn("approval_id", response["actions_proposed"][0])
        finally:
            sock.close()

    def test_approve_deny_flow(self):
        """Test the full approve/deny workflow."""
        sock = self._connect()
        try:
            self._recv(sock)

            # Trigger a write command (needs approval)
            self._send(sock, {"type": "query", "message": "fence compute-1 off"})
            response = self._recv(sock)
            self.assertTrue(response.get("requires_approval"))
            approval_id = response["actions_proposed"][0]["approval_id"]

            # Check pending
            self._send(sock, {"type": "query", "message": "pending"})
            response = self._recv(sock)
            self.assertIn(approval_id, response["message"])

            # Deny it
            self._send(sock, {"type": "query", "message": f"deny {approval_id}"})
            response = self._recv(sock)
            self.assertIn("Denied", response["message"])

            # Pending should be empty now
            self._send(sock, {"type": "query", "message": "pending"})
            response = self._recv(sock)
            self.assertIn("No pending", response["message"])
        finally:
            sock.close()

    def test_quit_command(self):
        """Test that quit closes the connection."""
        sock = self._connect()
        try:
            self._recv(sock)
            self._send(sock, {"type": "query", "message": "quit"})
            response = self._recv(sock)
            self.assertIn("Goodbye", response["message"])
        finally:
            sock.close()

    def test_multiple_clients(self):
        """Test that multiple clients can connect simultaneously."""
        sock1 = self._connect()
        sock2 = self._connect()
        try:
            r1 = self._recv(sock1)
            r2 = self._recv(sock2)
            self.assertIn("InstanceHA", r1["message"])
            self.assertIn("InstanceHA", r2["message"])

            self._send(sock1, {"type": "query", "message": "help"})
            self._send(sock2, {"type": "query", "message": "help"})

            r1 = self._recv(sock1)
            r2 = self._recv(sock2)
            self.assertIn("Available commands", r1["message"])
            self.assertIn("Available commands", r2["message"])
        finally:
            sock1.close()
            sock2.close()

    def test_audit_command(self):
        """Test that audit command returns log entries."""
        sock = self._connect()
        try:
            self._recv(sock)
            self._send(sock, {"type": "query", "message": "audit"})
            response = self._recv(sock)
            # Should succeed (may have no entries yet)
            self.assertIn("message", response)
        finally:
            sock.close()

    def test_server_update_connection(self):
        """Test that update_connection updates the nova connection reference."""
        new_conn = Mock()
        self.server.update_connection(new_conn)
        self.assertEqual(self.server._nova_connection, new_conn)


# ─── Chat Session Unit Tests ─────────────────────────────────────────


class TestChatSessionFormatting(unittest.TestCase):
    """Tests for ChatSession result formatting."""

    def setUp(self):
        self.mock_socket = Mock()
        self.mock_socket.sendall = Mock()
        self.tmpdir = tempfile.mkdtemp()

        audit_logger = AuditLogger(os.path.join(self.tmpdir, "audit.log"))
        self.session = ChatSession(
            conn=self.mock_socket,
            addr=None,
            nova_connection=Mock(),
            service=Mock(),
            approval_manager=ApprovalManager(
                audit_logger=audit_logger,
                dry_run=True,
            ),
        )

    def test_format_dry_run_result(self):
        result = ToolResult(
            success=True,
            data={"dry_run": True, "would_execute": "fence_host", "parameters": {"host": "h1"}},
        )
        formatted = self.session._format_result("fence_host", result)
        self.assertIn("[DRY-RUN]", formatted)
        self.assertIn("fence_host", formatted)

    def test_format_normal_result(self):
        result = ToolResult(
            success=True,
            data={"total_computes": 5, "available_targets": 4},
            duration_seconds=0.05,
        )
        formatted = self.session._format_result("check_cluster_capacity", result)
        self.assertIn("total_computes: 5", formatted)
        self.assertIn("available_targets: 4", formatted)
        self.assertIn("0.05s", formatted)

    def test_format_list_result(self):
        result = ToolResult(
            success=True,
            data={"hosts": ["h1", "h2", "h3"]},
        )
        formatted = self.session._format_result("get_compute_services", result)
        self.assertIn("h1", formatted)
        self.assertIn("h2", formatted)
        self.assertIn("h3", formatted)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)


if __name__ == '__main__':
    unittest.main()
