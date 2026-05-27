#!/usr/bin/env python3
"""Tests for the InstanceHA chat interface."""

import json
import os
import socket
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, MagicMock

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

from instanceha_ai.command_parser import parse, get_help_text, ParsedCommand
from instanceha_ai.chat_server import ChatServer, ChatSession
from instanceha_ai.safety import ApprovalManager, AuditLogger
from instanceha_ai.tools import ApprovalLevel, ToolResult, registry


class TestCommandParser(unittest.TestCase):
    def test_parse_empty_input(self):
        self.assertIsNone(parse(""))
        self.assertIsNone(parse("   "))

    def test_parse_unknown_command(self):
        self.assertIsNone(parse("foobar"))

    def test_parse_status_no_args(self):
        result = parse("status")
        self.assertIsNotNone(result)
        self.assertEqual(result.tool_name, "get_compute_services")

    def test_parse_status_with_host(self):
        result = parse("status compute-1")
        self.assertIsNotNone(result)
        self.assertEqual(result.tool_name, "get_service_status")
        self.assertEqual(result.parameters, {"host": "compute-1"})

    def test_parse_status_alias(self):
        result = parse("st compute-1")
        self.assertEqual(result.tool_name, "get_service_status")

    def test_parse_servers(self):
        result = parse("servers compute-2")
        self.assertEqual(result.tool_name, "get_servers_on_host")

    def test_parse_servers_missing_arg(self):
        with self.assertRaises(ValueError):
            parse("servers")

    def test_parse_fence(self):
        result = parse("fence compute-3 off")
        self.assertEqual(result.tool_name, "fence_host")
        self.assertEqual(result.parameters, {"host": "compute-3", "action": "off"})

    def test_parse_fence_missing_action(self):
        with self.assertRaises(ValueError):
            parse("fence compute-3")

    def test_parse_evacuate(self):
        result = parse("evacuate compute-4")
        self.assertEqual(result.tool_name, "evacuate_host")

    def test_parse_evacuate_with_target(self):
        result = parse("evacuate compute-4 compute-5")
        self.assertEqual(result.parameters, {"host": "compute-4", "target_host": "compute-5"})

    def test_parse_disable(self):
        result = parse("disable compute-6")
        self.assertEqual(result.tool_name, "disable_host")

    def test_parse_enable(self):
        result = parse("enable compute-7")
        self.assertEqual(result.tool_name, "enable_host")

    def test_parse_evacuate_server(self):
        result = parse("evacuate-server abc-123")
        self.assertEqual(result.tool_name, "evacuate_server")

    def test_parse_diagnose(self):
        result = parse("diagnose compute-9")
        self.assertEqual(result.tool_name, "diagnose_evacuation_failure")

    def test_parse_capacity(self):
        result = parse("capacity")
        self.assertEqual(result.tool_name, "check_cluster_capacity")

    def test_parse_correlate_with_minutes(self):
        result = parse("correlate 60")
        self.assertEqual(result.parameters, {"minutes": 60})

    def test_parse_correlate_bad_minutes(self):
        with self.assertRaises(ValueError):
            parse("correlate abc")

    def test_parse_history(self):
        result = parse("history")
        self.assertEqual(result.tool_name, "get_evacuation_history")

    def test_parse_summary(self):
        result = parse("summary")
        self.assertEqual(result.tool_name, "get_cluster_summary")

    def test_parse_config(self):
        result = parse("config")
        self.assertEqual(result.tool_name, "get_config")

    def test_parse_kdump(self):
        result = parse("kdump")
        self.assertEqual(result.tool_name, "get_kdump_hosts")

    def test_parse_approve(self):
        result = parse("approve abc123")
        self.assertEqual(result.tool_name, "_approve")

    def test_parse_deny(self):
        result = parse("deny abc123")
        self.assertEqual(result.tool_name, "_deny")

    def test_parse_pending(self):
        result = parse("pending")
        self.assertEqual(result.tool_name, "_pending")

    def test_parse_audit(self):
        result = parse("audit")
        self.assertEqual(result.tool_name, "_audit")

    def test_parse_too_many_args(self):
        with self.assertRaises(ValueError):
            parse("status host1 host2")

    def test_parse_case_insensitive(self):
        result = parse("STATUS")
        self.assertIsNotNone(result)

    def test_parse_quoted_args(self):
        result = parse('servers "compute node 1"')
        self.assertEqual(result.parameters, {"host": "compute node 1"})

    def test_parse_alerts(self):
        result = parse("alerts")
        self.assertEqual(result.tool_name, "_alerts")

    def test_parse_alerts_with_severity(self):
        result = parse("alerts warning")
        self.assertEqual(result.parameters["severity"], "warning")

    def test_parse_cluster_health(self):
        result = parse("cluster-health")
        self.assertEqual(result.tool_name, "_cluster_health")

    def test_parse_host_analysis(self):
        result = parse("host-analysis compute-1")
        self.assertEqual(result.tool_name, "_host_analysis")

    def test_parse_events(self):
        result = parse("events 30")
        self.assertEqual(result.tool_name, "_events")
        self.assertEqual(result.parameters["minutes"], 30)


class TestHelpText(unittest.TestCase):
    def test_help_text_contains_all_commands(self):
        text = get_help_text()
        for cmd in ["status", "servers", "fence", "evacuate", "diagnose",
                     "capacity", "approve", "deny", "pending"]:
            self.assertIn(cmd, text)

    def test_help_text_contains_categories(self):
        text = get_help_text()
        self.assertIn("Read-Only", text)
        self.assertIn("Write", text)
        self.assertIn("Diagnostic", text)
        self.assertIn("Approval Management", text)
        self.assertIn("AI Monitoring", text)


class TestChatServerProtocol(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.socket_path = os.path.join(self.tmpdir, "test.sock")
        self.audit_logger = AuditLogger(os.path.join(self.tmpdir, "audit.log"))
        self.approval_manager = ApprovalManager(
            auto_approve_level=ApprovalLevel.NONE,
            audit_logger=self.audit_logger,
            dry_run=True,
        )
        self.mock_conn = Mock()
        self.mock_service = Mock()
        self.mock_service.config.get_config_value.return_value = 50
        self.server = ChatServer(
            nova_connection=self.mock_conn,
            service=self.mock_service,
            approval_manager=self.approval_manager,
            socket_path=self.socket_path,
        )
        self.server_thread = self.server.start()
        time.sleep(0.1)

    def tearDown(self):
        self.server.stop()
        time.sleep(0.1)
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _connect(self):
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.connect(self.socket_path)
        sock.settimeout(2.0)
        return sock

    def _send(self, sock, msg):
        sock.sendall((json.dumps(msg) + "\n").encode("utf-8"))

    def _recv(self, sock):
        buf = b""
        while b"\n" not in buf:
            data = sock.recv(65536)
            if not data:
                raise ConnectionError("Server closed connection")
            buf += data
        line = buf.split(b"\n")[0]
        return json.loads(line.decode("utf-8"))

    def test_connect_receives_welcome(self):
        sock = self._connect()
        try:
            response = self._recv(sock)
            self.assertIn("InstanceHA", response["message"])
        finally:
            sock.close()

    def test_help_command(self):
        sock = self._connect()
        try:
            self._recv(sock)
            self._send(sock, {"type": "query", "message": "help"})
            response = self._recv(sock)
            self.assertIn("Available commands", response["message"])
        finally:
            sock.close()

    def test_unknown_command(self):
        sock = self._connect()
        try:
            self._recv(sock)
            self._send(sock, {"type": "query", "message": "foobar"})
            response = self._recv(sock)
            self.assertIn("Unknown command", response["error"])
        finally:
            sock.close()

    def test_invalid_json(self):
        sock = self._connect()
        try:
            self._recv(sock)
            sock.sendall(b"not json\n")
            response = self._recv(sock)
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

    def test_write_command_requires_approval(self):
        sock = self._connect()
        try:
            self._recv(sock)
            self._send(sock, {"type": "query", "message": "fence compute-1 off"})
            response = self._recv(sock)
            self.assertTrue(response.get("requires_approval"))
        finally:
            sock.close()

    def test_quit_command(self):
        sock = self._connect()
        try:
            self._recv(sock)
            self._send(sock, {"type": "query", "message": "quit"})
            response = self._recv(sock)
            self.assertIn("Goodbye", response["message"])
        finally:
            sock.close()

    def test_multiple_clients(self):
        sock1 = self._connect()
        sock2 = self._connect()
        try:
            r1 = self._recv(sock1)
            r2 = self._recv(sock2)
            self.assertIn("InstanceHA", r1["message"])
            self.assertIn("InstanceHA", r2["message"])
        finally:
            sock1.close()
            sock2.close()

    def test_server_update_connection(self):
        new_conn = Mock()
        self.server.update_connection(new_conn)
        self.assertEqual(self.server._nova_connection, new_conn)


class TestChatSessionFormatting(unittest.TestCase):
    def setUp(self):
        self.mock_socket = Mock()
        self.tmpdir = tempfile.mkdtemp()
        audit_logger = AuditLogger(os.path.join(self.tmpdir, "audit.log"))
        self.session = ChatSession(
            conn=self.mock_socket, addr=None,
            nova_connection=Mock(), service=Mock(),
            approval_manager=ApprovalManager(audit_logger=audit_logger, dry_run=True),
        )

    def test_format_dry_run_result(self):
        result = ToolResult(
            success=True,
            data={"dry_run": True, "would_execute": "fence_host", "parameters": {"host": "h1"}},
        )
        formatted = self.session._format_result("fence_host", result)
        self.assertIn("[DRY-RUN]", formatted)

    def test_format_normal_result(self):
        result = ToolResult(
            success=True,
            data={"total_computes": 5, "available_targets": 4},
            duration_seconds=0.05,
        )
        formatted = self.session._format_result("check_cluster_capacity", result)
        self.assertIn("total_computes: 5", formatted)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)


if __name__ == '__main__':
    unittest.main()
