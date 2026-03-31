#!/usr/bin/env python3
"""Tests for the InstanceHA AI tool layer."""

import json
import os
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch, MagicMock

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

import instanceha  # noqa: E402
from instanceha.ai.tools import ApprovalLevel, Tool, ToolRegistry, ToolResult, tool, registry
from instanceha.ai.safety import ApprovalManager, AuditLogger, RateLimiter, RateLimit, _sanitize_params


class TestApprovalLevel(unittest.TestCase):
    """Tests for ApprovalLevel enum."""

    def test_approval_levels_exist(self):
        self.assertEqual(ApprovalLevel.NONE.value, "none")
        self.assertEqual(ApprovalLevel.MEDIUM.value, "medium")
        self.assertEqual(ApprovalLevel.HIGH.value, "high")
        self.assertEqual(ApprovalLevel.CRITICAL.value, "critical")


class TestToolResult(unittest.TestCase):
    """Tests for ToolResult dataclass."""

    def test_success_result(self):
        result = ToolResult(success=True, data={"key": "value"})
        self.assertTrue(result.success)
        self.assertEqual(result.data, {"key": "value"})
        self.assertIsNone(result.error)

    def test_error_result(self):
        result = ToolResult(success=False, error="something broke")
        self.assertFalse(result.success)
        self.assertEqual(result.error, "something broke")

    def test_to_dict(self):
        result = ToolResult(success=True, data=[1, 2, 3], duration_seconds=0.123)
        d = result.to_dict()
        self.assertTrue(d["success"])
        self.assertEqual(d["data"], [1, 2, 3])
        self.assertEqual(d["duration_seconds"], 0.123)
        self.assertNotIn("error", d)

    def test_to_dict_with_error(self):
        result = ToolResult(success=False, error="fail")
        d = result.to_dict()
        self.assertFalse(d["success"])
        self.assertEqual(d["error"], "fail")


class TestToolRegistry(unittest.TestCase):
    """Tests for ToolRegistry."""

    def setUp(self):
        self.registry = ToolRegistry()

    def test_register_and_get(self):
        def dummy():
            return "hello"

        t = Tool(name="test_tool", description="A test", func=dummy,
                 approval_level=ApprovalLevel.NONE, category="test")
        self.registry.register(t)
        self.assertEqual(self.registry.get("test_tool"), t)

    def test_register_duplicate_raises(self):
        t = Tool(name="dup", description="", func=lambda: None,
                 approval_level=ApprovalLevel.NONE, category="test")
        self.registry.register(t)
        with self.assertRaises(ValueError):
            self.registry.register(t)

    def test_get_unknown_returns_none(self):
        self.assertIsNone(self.registry.get("nonexistent"))

    def test_list_tools(self):
        for name in ["b_tool", "a_tool", "c_tool"]:
            t = Tool(name=name, description="", func=lambda: None,
                     approval_level=ApprovalLevel.NONE, category="test")
            self.registry.register(t)
        tools = self.registry.list_tools()
        self.assertEqual([t.name for t in tools], ["a_tool", "b_tool", "c_tool"])

    def test_list_tools_by_category(self):
        t1 = Tool(name="read1", description="", func=lambda: None,
                  approval_level=ApprovalLevel.NONE, category="read-only")
        t2 = Tool(name="write1", description="", func=lambda: None,
                  approval_level=ApprovalLevel.HIGH, category="write")
        self.registry.register(t1)
        self.registry.register(t2)
        read_tools = self.registry.list_tools(category="read-only")
        self.assertEqual(len(read_tools), 1)
        self.assertEqual(read_tools[0].name, "read1")

    def test_execute_success(self):
        t = Tool(name="adder", description="", func=lambda a, b: a + b,
                 approval_level=ApprovalLevel.NONE, category="test")
        self.registry.register(t)
        result = self.registry.execute("adder", a=2, b=3)
        self.assertTrue(result.success)
        self.assertEqual(result.data, 5)
        self.assertGreater(result.duration_seconds, 0)

    def test_execute_returns_tool_result(self):
        def returns_result():
            return ToolResult(success=True, data="custom")

        t = Tool(name="custom_result", description="", func=returns_result,
                 approval_level=ApprovalLevel.NONE, category="test")
        self.registry.register(t)
        result = self.registry.execute("custom_result")
        self.assertTrue(result.success)
        self.assertEqual(result.data, "custom")

    def test_execute_handles_exception(self):
        def fails():
            raise RuntimeError("boom")

        t = Tool(name="failing", description="", func=fails,
                 approval_level=ApprovalLevel.NONE, category="test")
        self.registry.register(t)
        result = self.registry.execute("failing")
        self.assertFalse(result.success)
        self.assertIn("boom", result.error)

    def test_execute_unknown_tool(self):
        result = self.registry.execute("does_not_exist")
        self.assertFalse(result.success)
        self.assertIn("Unknown tool", result.error)

    def test_get_schemas(self):
        t = Tool(name="schema_test", description="A desc", func=lambda: None,
                 approval_level=ApprovalLevel.HIGH, category="write",
                 parameters={"host": "str"})
        self.registry.register(t)
        schemas = self.registry.get_schemas()
        self.assertEqual(len(schemas), 1)
        self.assertEqual(schemas[0]["name"], "schema_test")
        self.assertEqual(schemas[0]["approval_level"], "high")
        self.assertEqual(schemas[0]["parameters"], {"host": "str"})


class TestToolDecorator(unittest.TestCase):
    """Tests for @tool decorator."""

    def test_global_registry_has_read_tools(self):
        """The module-level registry should have tools registered via @tool."""
        # read_tools.py registers tools at import time
        t = registry.get("get_compute_services")
        self.assertIsNotNone(t)
        self.assertEqual(t.approval_level, ApprovalLevel.NONE)
        self.assertEqual(t.category, "read-only")

    def test_global_registry_has_write_tools(self):
        t = registry.get("fence_host")
        self.assertIsNotNone(t)
        self.assertEqual(t.approval_level, ApprovalLevel.CRITICAL)

    def test_global_registry_has_diagnostic_tools(self):
        t = registry.get("diagnose_evacuation_failure")
        self.assertIsNotNone(t)
        self.assertEqual(t.category, "diagnostic")

    def test_tool_decorator_preserves_function(self):
        t = registry.get("get_config")
        self.assertIsNotNone(t)
        self.assertTrue(callable(t.func))


class TestRateLimiter(unittest.TestCase):
    """Tests for RateLimiter."""

    def test_allows_within_limit(self):
        limiter = RateLimiter({
            ApprovalLevel.HIGH: RateLimit(max_calls=3, window_seconds=60),
        })
        self.assertTrue(limiter.check(ApprovalLevel.HIGH))
        self.assertTrue(limiter.check(ApprovalLevel.HIGH))
        self.assertTrue(limiter.check(ApprovalLevel.HIGH))

    def test_blocks_over_limit(self):
        limiter = RateLimiter({
            ApprovalLevel.HIGH: RateLimit(max_calls=2, window_seconds=60),
        })
        self.assertTrue(limiter.check(ApprovalLevel.HIGH))
        self.assertTrue(limiter.check(ApprovalLevel.HIGH))
        self.assertFalse(limiter.check(ApprovalLevel.HIGH))

    def test_none_level_always_allowed(self):
        limiter = RateLimiter()
        for _ in range(100):
            self.assertTrue(limiter.check(ApprovalLevel.NONE))

    def test_time_until_available(self):
        limiter = RateLimiter({
            ApprovalLevel.CRITICAL: RateLimit(max_calls=1, window_seconds=10),
        })
        limiter.check(ApprovalLevel.CRITICAL)
        wait = limiter.time_until_available(ApprovalLevel.CRITICAL)
        self.assertGreater(wait, 0)
        self.assertLessEqual(wait, 10)


class TestAuditLogger(unittest.TestCase):
    """Tests for AuditLogger."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.log_path = os.path.join(self.tmpdir, "audit.log")
        self.logger = AuditLogger(self.log_path)

    def test_log_and_read(self):
        result = ToolResult(success=True, duration_seconds=0.5)
        self.logger.log("test_tool", {"host": "h1"}, result, "none", True)

        entries = self.logger.get_recent()
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["tool"], "test_tool")
        self.assertTrue(entries[0]["approved"])

    def test_log_sanitizes_params(self):
        result = ToolResult(success=True)
        self.logger.log("test", {"password": "secret123", "host": "h1"},
                        result, "high", True)

        entries = self.logger.get_recent()
        self.assertEqual(entries[0]["parameters"]["password"], "***")
        self.assertEqual(entries[0]["parameters"]["host"], "h1")

    def test_get_recent_limit(self):
        result = ToolResult(success=True)
        for i in range(10):
            self.logger.log(f"tool_{i}", {}, result, "none", True)

        entries = self.logger.get_recent(count=3)
        self.assertEqual(len(entries), 3)

    def test_get_recent_empty_file(self):
        logger = AuditLogger(os.path.join(self.tmpdir, "nonexistent.log"))
        entries = logger.get_recent()
        self.assertEqual(entries, [])


class TestSanitizeParams(unittest.TestCase):
    """Tests for parameter sanitization."""

    def test_sanitizes_password(self):
        result = _sanitize_params({"password": "secret", "host": "h1"})
        self.assertEqual(result["password"], "***")
        self.assertEqual(result["host"], "h1")

    def test_sanitizes_nested(self):
        result = _sanitize_params({"auth": {"token": "abc123", "user": "admin"}})
        self.assertEqual(result["auth"]["token"], "***")
        self.assertEqual(result["auth"]["user"], "admin")

    def test_sanitizes_various_keys(self):
        for key in ["password", "passwd", "token", "secret", "credential"]:
            result = _sanitize_params({key: "value"})
            self.assertEqual(result[key], "***", f"Failed to sanitize {key}")


class TestApprovalManager(unittest.TestCase):
    """Tests for ApprovalManager."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.audit_logger = AuditLogger(os.path.join(self.tmpdir, "audit.log"))
        self.manager = ApprovalManager(
            auto_approve_level=ApprovalLevel.NONE,
            audit_logger=self.audit_logger,
            dry_run=False,
        )

    def test_none_level_no_approval(self):
        self.assertFalse(self.manager.needs_approval(ApprovalLevel.NONE))

    def test_critical_always_needs_approval(self):
        # Even with auto_approve at HIGH
        manager = ApprovalManager(
            auto_approve_level=ApprovalLevel.HIGH,
            audit_logger=self.audit_logger,
        )
        self.assertTrue(manager.needs_approval(ApprovalLevel.CRITICAL))

    def test_medium_needs_approval_when_auto_approve_none(self):
        self.assertTrue(self.manager.needs_approval(ApprovalLevel.MEDIUM))

    def test_medium_auto_approved_when_auto_approve_medium(self):
        manager = ApprovalManager(
            auto_approve_level=ApprovalLevel.MEDIUM,
            audit_logger=self.audit_logger,
        )
        self.assertFalse(manager.needs_approval(ApprovalLevel.MEDIUM))

    def test_request_and_approve(self):
        approval_id = self.manager.request_approval(
            "fence_host", {"host": "h1"}, ApprovalLevel.CRITICAL)
        self.assertIsNotNone(approval_id)

        pending = self.manager.get_pending()
        self.assertIn(approval_id, pending)

        request = self.manager.approve(approval_id)
        self.assertIsNotNone(request)
        self.assertEqual(request["tool_name"], "fence_host")

        # Should be gone after approval
        self.assertIsNone(self.manager.approve(approval_id))

    def test_deny(self):
        approval_id = self.manager.request_approval(
            "fence_host", {"host": "h1"}, ApprovalLevel.CRITICAL)
        request = self.manager.deny(approval_id)
        self.assertIsNotNone(request)
        self.assertEqual(len(self.manager.get_pending()), 0)

    def test_execute_read_only_no_approval(self):
        reg = ToolRegistry()
        reg.register(Tool(name="reader", description="", func=lambda: "data",
                          approval_level=ApprovalLevel.NONE, category="read-only"))

        result = self.manager.execute_tool(reg, "reader", {})
        self.assertTrue(result.success)
        self.assertEqual(result.data, "data")

    def test_execute_critical_requires_approval(self):
        reg = ToolRegistry()
        reg.register(Tool(name="fencer", description="",
                          func=lambda host: True,
                          approval_level=ApprovalLevel.CRITICAL, category="write"))

        result = self.manager.execute_tool(reg, "fencer", {"host": "h1"})
        self.assertFalse(result.success)
        self.assertEqual(result.error, "approval_required")
        self.assertIn("approval_id", result.data)

    def test_execute_force_approve_bypasses_check(self):
        reg = ToolRegistry()
        reg.register(Tool(name="fencer2", description="",
                          func=lambda host: f"fenced {host}",
                          approval_level=ApprovalLevel.CRITICAL, category="write"))

        result = self.manager.execute_tool(reg, "fencer2", {"host": "h1"},
                                           force_approve=True)
        self.assertTrue(result.success)
        self.assertEqual(result.data, "fenced h1")

    def test_execute_rate_limited(self):
        reg = ToolRegistry()
        reg.register(Tool(name="limited", description="",
                          func=lambda: True,
                          approval_level=ApprovalLevel.HIGH, category="write"))

        manager = ApprovalManager(
            auto_approve_level=ApprovalLevel.HIGH,
            rate_limiter=RateLimiter({
                ApprovalLevel.HIGH: RateLimit(max_calls=1, window_seconds=60),
            }),
            audit_logger=self.audit_logger,
            dry_run=False,
        )

        # First call succeeds (auto-approved, within rate limit)
        # But CRITICAL always needs approval, HIGH with auto_approve=HIGH doesn't
        result = manager.execute_tool(reg, "limited", {})
        self.assertTrue(result.success)

        # Second call rate-limited
        result = manager.execute_tool(reg, "limited", {})
        self.assertFalse(result.success)
        self.assertIn("Rate limited", result.error)

    def test_dry_run_mode(self):
        reg = ToolRegistry()
        reg.register(Tool(name="writer", description="",
                          func=lambda: "executed",
                          approval_level=ApprovalLevel.MEDIUM, category="write"))

        manager = ApprovalManager(
            auto_approve_level=ApprovalLevel.MEDIUM,
            audit_logger=self.audit_logger,
            dry_run=True,
        )

        result = manager.execute_tool(reg, "writer", {})
        self.assertTrue(result.success)
        self.assertTrue(result.data["dry_run"])
        self.assertEqual(result.data["would_execute"], "writer")

    def test_audit_log_written(self):
        reg = ToolRegistry()
        reg.register(Tool(name="audited", description="",
                          func=lambda: "ok",
                          approval_level=ApprovalLevel.NONE, category="read-only"))

        self.manager.execute_tool(reg, "audited", {}, user="operator1")

        entries = self.audit_logger.get_recent()
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["user"], "operator1")
        self.assertEqual(entries[0]["tool"], "audited")


class TestReadTools(unittest.TestCase):
    """Tests for read-only tool implementations."""

    def test_get_compute_services(self):
        from instanceha.ai.read_tools import get_compute_services

        mock_conn = Mock()
        mock_svc = Mock()
        mock_svc.host = "compute-0"
        mock_svc.state = "up"
        mock_svc.status = "enabled"
        mock_svc.forced_down = False
        mock_svc.disabled_reason = ""
        mock_svc.updated_at = "2024-01-01T00:00:00"
        mock_svc.zone = "nova"
        mock_conn.services.list.return_value = [mock_svc]

        mock_service = Mock()
        result = get_compute_services(mock_conn, mock_service)
        self.assertTrue(result.success)
        self.assertEqual(len(result.data), 1)
        self.assertEqual(result.data[0]["host"], "compute-0")

    def test_get_config(self):
        from instanceha.ai.read_tools import get_config

        mock_service = Mock()
        mock_service.config._config_map = {}
        result = get_config(mock_service)
        self.assertTrue(result.success)

    def test_get_kdump_hosts(self):
        from instanceha.ai.read_tools import get_kdump_hosts

        mock_service = Mock()
        mock_service.kdump_fenced_hosts = {"host1"}
        mock_service.kdump_hosts_timestamp = {"host1": time.time()}
        mock_service.kdump_hosts_checking = {}
        result = get_kdump_hosts(mock_service)
        self.assertTrue(result.success)
        self.assertIn("host1", result.data["kdump_fenced"])


class TestWriteTools(unittest.TestCase):
    """Tests for write tool implementations."""

    def test_fence_host_invalid_action(self):
        from instanceha.ai.write_tools import fence_host
        result = fence_host("h1", "reboot", Mock())
        self.assertFalse(result.success)
        self.assertIn("Invalid action", result.error)

    @patch.dict('sys.modules', {'instanceha': Mock()})
    def test_fence_host_success(self):
        import sys
        sys.modules['instanceha']._host_fence = Mock(return_value=True)

        from instanceha.ai.write_tools import fence_host
        result = fence_host("h1", "off", Mock())
        self.assertTrue(result.success)

    @patch.dict('sys.modules', {'instanceha': Mock()})
    def test_evacuate_server(self):
        import sys
        mock_result = Mock()
        mock_result.accepted = True
        mock_result.reason = "OK"
        sys.modules['instanceha']._server_evacuate = Mock(return_value=mock_result)

        from instanceha.ai.write_tools import evacuate_server
        result = evacuate_server(Mock(), "server-123")
        self.assertTrue(result.success)


class TestDiagnosticTools(unittest.TestCase):
    """Tests for diagnostic tool implementations."""

    def test_check_cluster_capacity(self):
        from instanceha.ai.diagnostic_tools import check_cluster_capacity

        mock_conn = Mock()
        svc1 = Mock(host="h1", state="up", status="enabled")
        svc2 = Mock(host="h2", state="up", status="enabled")
        svc3 = Mock(host="h3", state="down", status="disabled")
        mock_conn.services.list.return_value = [svc1, svc2, svc3]
        mock_conn.servers.list.return_value = []

        mock_service = Mock()
        mock_service.config.get_config_value.return_value = 50

        result = check_cluster_capacity(mock_conn, mock_service, host="h3")
        self.assertTrue(result.success)
        self.assertEqual(result.data["available_targets"], 2)
        self.assertTrue(result.data["can_evacuate"])


if __name__ == '__main__':
    unittest.main()
