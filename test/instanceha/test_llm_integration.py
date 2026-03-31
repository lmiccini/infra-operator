#!/usr/bin/env python3
"""Tests for the InstanceHA LLM integration (Phase 4).

Covers engine abstraction, prompts, context summarization, and the agent loop.
All tests use mock LLM backends -- no actual model loading.
"""

import json
import os
import sys
import tempfile
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

from instanceha.ai.engine import (
    LLMEngine, LLMResponse, LocalEngine, RemoteEngine, ToolCall, create_engine,
)
from instanceha.ai.prompts import build_system_prompt, build_context_message
from instanceha.ai.context import summarize_cluster, summarize_recent_events, build_context
from instanceha.ai.agent import Agent, AgentResponse
from instanceha.ai.safety import ApprovalManager, AuditLogger
from instanceha.ai.tools import ApprovalLevel, ToolResult, registry


# ─── Engine Tests ────────────────────────────────────────────────────


class TestLLMResponse(unittest.TestCase):
    """Tests for LLMResponse dataclass."""

    def test_no_tool_calls(self):
        resp = LLMResponse(content="Hello")
        self.assertFalse(resp.has_tool_calls)

    def test_with_tool_calls(self):
        resp = LLMResponse(
            content="",
            tool_calls=[ToolCall(name="get_config", arguments={})],
        )
        self.assertTrue(resp.has_tool_calls)

    def test_usage_optional(self):
        resp = LLMResponse(content="test")
        self.assertIsNone(resp.usage)


class TestToolCall(unittest.TestCase):
    """Tests for ToolCall dataclass."""

    def test_basic(self):
        tc = ToolCall(name="fence_host", arguments={"host": "h1", "action": "off"})
        self.assertEqual(tc.name, "fence_host")
        self.assertEqual(tc.arguments["host"], "h1")
        self.assertEqual(tc.call_id, "")


class TestLocalEngine(unittest.TestCase):
    """Tests for LocalEngine (without actual model loading)."""

    def test_not_available_before_load(self):
        engine = LocalEngine(model_path="/nonexistent.gguf")
        self.assertFalse(engine.is_available())

    def test_model_info(self):
        engine = LocalEngine(model_path="/models/test.gguf", n_ctx=2048, n_threads=2)
        info = engine.model_info()
        self.assertEqual(info["backend"], "local")
        self.assertEqual(info["model_path"], "/models/test.gguf")
        self.assertFalse(info["loaded"])
        self.assertEqual(info["n_ctx"], 2048)
        self.assertEqual(info["n_threads"], 2)

    def test_chat_without_load_returns_error(self):
        engine = LocalEngine(model_path="/nonexistent.gguf")
        resp = engine.chat([{"role": "user", "content": "hello"}])
        self.assertEqual(resp.finish_reason, "error")
        self.assertIn("not loaded", resp.content)

    def test_load_missing_file(self):
        engine = LocalEngine(model_path="/nonexistent.gguf")
        # llama_cpp not installed, should return False
        result = engine.load()
        self.assertFalse(result)


class TestRemoteEngine(unittest.TestCase):
    """Tests for RemoteEngine."""

    def test_model_info(self):
        engine = RemoteEngine(
            endpoint="http://localhost:8000",
            model="llama-3.1-8b",
        )
        info = engine.model_info()
        self.assertEqual(info["backend"], "remote")
        self.assertEqual(info["endpoint"], "http://localhost:8000")
        self.assertEqual(info["model"], "llama-3.1-8b")

    def test_endpoint_trailing_slash_stripped(self):
        engine = RemoteEngine(endpoint="http://localhost:8000/")
        self.assertEqual(engine._endpoint, "http://localhost:8000")

    @patch("urllib.request.urlopen")
    def test_chat_success(self, mock_urlopen):
        """Test remote chat with a mocked HTTP response."""
        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps({
            "choices": [{
                "message": {
                    "content": "The cluster is healthy.",
                    "role": "assistant",
                },
                "finish_reason": "stop",
            }],
            "usage": {"prompt_tokens": 100, "completion_tokens": 20},
        }).encode("utf-8")
        mock_response.__enter__ = Mock(return_value=mock_response)
        mock_response.__exit__ = Mock(return_value=False)
        mock_urlopen.return_value = mock_response

        engine = RemoteEngine(endpoint="http://localhost:8000", model="test")
        resp = engine.chat([{"role": "user", "content": "cluster status?"}])

        self.assertEqual(resp.content, "The cluster is healthy.")
        self.assertEqual(resp.finish_reason, "stop")
        self.assertFalse(resp.has_tool_calls)
        self.assertEqual(resp.usage["prompt_tokens"], 100)

    @patch("urllib.request.urlopen")
    def test_chat_with_tool_call(self, mock_urlopen):
        """Test remote chat returning a tool call."""
        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps({
            "choices": [{
                "message": {
                    "content": "",
                    "role": "assistant",
                    "tool_calls": [{
                        "id": "call_123",
                        "function": {
                            "name": "get_compute_services",
                            "arguments": "{}",
                        },
                    }],
                },
                "finish_reason": "tool_calls",
            }],
        }).encode("utf-8")
        mock_response.__enter__ = Mock(return_value=mock_response)
        mock_response.__exit__ = Mock(return_value=False)
        mock_urlopen.return_value = mock_response

        engine = RemoteEngine(endpoint="http://localhost:8000", model="test")
        resp = engine.chat(
            [{"role": "user", "content": "show me all computes"}],
            tools=[{"type": "function", "function": {"name": "get_compute_services"}}],
        )

        self.assertTrue(resp.has_tool_calls)
        self.assertEqual(resp.tool_calls[0].name, "get_compute_services")
        self.assertEqual(resp.tool_calls[0].call_id, "call_123")

    @patch("urllib.request.urlopen")
    def test_chat_connection_error(self, mock_urlopen):
        """Test remote chat handles connection errors."""
        mock_urlopen.side_effect = Exception("Connection refused")

        engine = RemoteEngine(endpoint="http://localhost:8000", model="test")
        resp = engine.chat([{"role": "user", "content": "hello"}])

        self.assertEqual(resp.finish_reason, "error")
        self.assertIn("Connection refused", resp.content)


class TestCreateEngine(unittest.TestCase):
    """Tests for the create_engine factory."""

    def test_disabled(self):
        engine = create_engine({"ai_enabled": "False"})
        self.assertIsNone(engine)

    def test_no_config(self):
        engine = create_engine({})
        self.assertIsNone(engine)

    def test_remote_engine_created(self):
        engine = create_engine({
            "ai_enabled": "True",
            "ai_endpoint": "http://localhost:8000",
            "ai_model": "llama-3.1-8b",
            "ai_api_key": "test-key",
        })
        self.assertIsInstance(engine, RemoteEngine)
        self.assertEqual(engine._model, "llama-3.1-8b")

    def test_local_engine_missing_file(self):
        engine = create_engine({
            "ai_enabled": "True",
            "ai_model_path": "/nonexistent/model.gguf",
        })
        self.assertIsNone(engine)

    def test_enabled_no_backend(self):
        engine = create_engine({"ai_enabled": "True"})
        self.assertIsNone(engine)


# ─── Prompts Tests ───────────────────────────────────────────────────


class TestPrompts(unittest.TestCase):
    """Tests for system prompt generation."""

    def test_build_system_prompt_contains_tools(self):
        schemas = [
            {"name": "get_config", "description": "Get config",
             "approval_level": "none", "category": "read-only",
             "parameters": {"service": "InstanceHAService"}},
            {"name": "fence_host", "description": "Fence a host",
             "approval_level": "critical", "category": "write",
             "parameters": {"host": "str", "action": "str", "service": "InstanceHAService"}},
        ]
        prompt = build_system_prompt(schemas)

        self.assertIn("get_config", prompt)
        self.assertIn("fence_host", prompt)
        self.assertIn("[CRITICAL]", prompt)
        # service param should be filtered out (injected, not for LLM)
        self.assertNotIn("InstanceHAService", prompt)

    def test_build_system_prompt_safety_rules(self):
        prompt = build_system_prompt([])
        self.assertIn("Safety Rules", prompt)
        self.assertIn("NEVER execute a write operation", prompt)

    def test_build_context_message(self):
        msg = build_context_message(
            cluster_summary="5 computes, all up",
            recent_events="No events",
            timestamp="2026-03-30 10:00:00",
        )
        self.assertIn("5 computes", msg)
        self.assertIn("2026-03-30", msg)
        self.assertIn("No events", msg)


# ─── Context Tests ───────────────────────────────────────────────────


class TestContext(unittest.TestCase):
    """Tests for cluster state summarization."""

    def _make_service(self, host, state, status, forced_down=False, disabled_reason=""):
        svc = Mock(host=host, state=state, status=status,
                   forced_down=forced_down, disabled_reason=disabled_reason)
        return svc

    def test_summarize_cluster_all_up(self):
        conn = Mock()
        conn.services.list.return_value = [
            self._make_service("h1", "up", "enabled"),
            self._make_service("h2", "up", "enabled"),
        ]
        service = Mock()
        service.kdump_fenced_hosts = set()
        service.hosts_processing = {}

        summary = summarize_cluster(conn, service)
        self.assertIn("2 total", summary)
        self.assertIn("2 up+enabled", summary)
        self.assertIn("0 down", summary)

    def test_summarize_cluster_with_down_hosts(self):
        conn = Mock()
        conn.services.list.return_value = [
            self._make_service("h1", "up", "enabled"),
            self._make_service("h2", "down", "disabled", forced_down=True),
        ]
        service = Mock()
        service.kdump_fenced_hosts = {"h2"}
        service.hosts_processing = {}

        summary = summarize_cluster(conn, service)
        self.assertIn("1 down", summary)
        self.assertIn("Down hosts: h2", summary)
        self.assertIn("Forced-down: h2", summary)
        self.assertIn("Kdump-fenced hosts: h2", summary)

    def test_summarize_cluster_processing(self):
        conn = Mock()
        conn.services.list.return_value = [
            self._make_service("h1", "up", "enabled"),
        ]
        service = Mock()
        service.kdump_fenced_hosts = set()
        service.hosts_processing = {"h3": 12345}

        summary = summarize_cluster(conn, service)
        self.assertIn("Currently processing: h3", summary)

    def test_summarize_cluster_no_services(self):
        conn = Mock()
        conn.services.list.return_value = []
        service = Mock()
        service.kdump_fenced_hosts = set()

        summary = summarize_cluster(conn, service)
        self.assertIn("No compute services", summary)

    def test_summarize_cluster_api_error(self):
        conn = Mock()
        conn.services.list.side_effect = Exception("API down")
        service = Mock()
        service.kdump_fenced_hosts = set()
        service.hosts_processing = {}

        summary = summarize_cluster(conn, service)
        self.assertIn("Could not query", summary)

    def test_summarize_recent_events_no_migrations(self):
        conn = Mock()
        conn.migrations.list.return_value = []
        service = Mock()
        service.kdump_hosts_timestamp = {}

        events = summarize_recent_events(conn, service, minutes=10)
        self.assertIn("No evacuations", events)

    def test_summarize_recent_events_with_migrations(self):
        conn = Mock()
        mig1 = Mock(status="completed")
        mig2 = Mock(status="completed")
        mig3 = Mock(status="error")
        conn.migrations.list.return_value = [mig1, mig2, mig3]
        service = Mock()
        service.kdump_hosts_timestamp = {}

        events = summarize_recent_events(conn, service, minutes=30)
        self.assertIn("completed: 2", events)
        self.assertIn("error: 1", events)


# ─── Agent Tests ─────────────────────────────────────────────────────


class MockEngine(LLMEngine):
    """A mock LLM engine that returns pre-configured responses."""

    def __init__(self, responses=None):
        self._responses = responses or []
        self._call_count = 0

    def chat(self, messages, tools=None, temperature=0.1, max_tokens=2048):
        if self._call_count < len(self._responses):
            resp = self._responses[self._call_count]
            self._call_count += 1
            return resp
        return LLMResponse(content="No more responses configured", finish_reason="stop")

    def is_available(self):
        return True

    def model_info(self):
        return {"backend": "mock"}


class TestAgent(unittest.TestCase):
    """Tests for the tool-calling agent loop."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.audit_logger = AuditLogger(os.path.join(self.tmpdir, "audit.log"))
        self.approval_manager = ApprovalManager(
            auto_approve_level=ApprovalLevel.NONE,
            audit_logger=self.audit_logger,
            dry_run=True,
        )
        self.mock_conn = Mock()
        self.mock_service = Mock()
        self.mock_service.config.get_config_value.return_value = 50
        self.mock_service.kdump_fenced_hosts = set()
        self.mock_service.hosts_processing = {}
        self.mock_service.kdump_hosts_timestamp = {}
        self.mock_conn.services.list.return_value = []
        self.mock_conn.migrations.list.return_value = []

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_simple_text_response(self):
        """Test agent returns LLM text directly when no tool calls."""
        engine = MockEngine(responses=[
            LLMResponse(content="The cluster looks healthy."),
        ])

        agent = Agent(engine, self.approval_manager,
                      self.mock_conn, self.mock_service)
        result = agent.query("How is the cluster?")

        self.assertEqual(result.message, "The cluster looks healthy.")
        self.assertEqual(len(result.tool_calls_made), 0)
        self.assertFalse(result.requires_approval)

    def test_tool_call_then_response(self):
        """Test agent calls a tool, feeds result back, then gets final response."""
        svc1 = Mock(host="h1", state="up", status="enabled", forced_down=False,
                    disabled_reason="", updated_at="2026-01-01", zone="nova")
        self.mock_conn.services.list.return_value = [svc1]

        engine = MockEngine(responses=[
            # First response: tool call
            LLMResponse(
                content="Let me check the compute services.",
                tool_calls=[ToolCall(
                    name="get_compute_services",
                    arguments={},
                    call_id="call_1",
                )],
                finish_reason="tool_calls",
            ),
            # Second response: final answer after seeing tool result
            LLMResponse(
                content="There is 1 compute service (h1), it is up and enabled.",
            ),
        ])

        agent = Agent(engine, self.approval_manager,
                      self.mock_conn, self.mock_service)
        result = agent.query("Show me the compute services")

        self.assertIn("1 compute service", result.message)
        self.assertEqual(len(result.tool_calls_made), 1)
        self.assertEqual(result.tool_calls_made[0]["tool"], "get_compute_services")

    def test_tool_call_needing_approval(self):
        """Test agent stops when a tool requires approval."""
        engine = MockEngine(responses=[
            LLMResponse(
                content="I'll fence compute-1 for you.",
                tool_calls=[ToolCall(
                    name="fence_host",
                    arguments={"host": "compute-1", "action": "off"},
                    call_id="call_1",
                )],
                finish_reason="tool_calls",
            ),
        ])

        agent = Agent(engine, self.approval_manager,
                      self.mock_conn, self.mock_service)
        result = agent.query("Fence compute-1")

        self.assertTrue(result.requires_approval)
        self.assertIn("approval", result.message.lower())
        self.assertEqual(result.pending_tool, "fence_host")
        self.assertEqual(result.pending_params["host"], "compute-1")

    def test_max_iterations_reached(self):
        """Test agent stops after max iterations."""
        # Create an engine that always returns tool calls
        responses = []
        for i in range(10):
            responses.append(LLMResponse(
                content=f"Calling tool {i}",
                tool_calls=[ToolCall(
                    name="get_config",
                    arguments={},
                    call_id=f"call_{i}",
                )],
                finish_reason="tool_calls",
            ))

        engine = MockEngine(responses=responses)
        agent = Agent(engine, self.approval_manager,
                      self.mock_conn, self.mock_service, max_iterations=3)
        result = agent.query("Keep calling tools")

        self.assertIn("maximum", result.message.lower())
        self.assertEqual(len(result.tool_calls_made), 3)

    def test_error_response(self):
        """Test agent handles LLM error."""
        engine = MockEngine(responses=[
            LLMResponse(content="Model error occurred", finish_reason="error"),
        ])

        agent = Agent(engine, self.approval_manager,
                      self.mock_conn, self.mock_service)
        result = agent.query("hello")

        self.assertEqual(result.message, "Model error occurred")

    def test_unknown_tool_call(self):
        """Test agent handles LLM calling a nonexistent tool."""
        engine = MockEngine(responses=[
            LLMResponse(
                content="",
                tool_calls=[ToolCall(
                    name="nonexistent_tool",
                    arguments={},
                    call_id="call_1",
                )],
                finish_reason="tool_calls",
            ),
            LLMResponse(content="That tool doesn't exist, sorry."),
        ])

        agent = Agent(engine, self.approval_manager,
                      self.mock_conn, self.mock_service)
        result = agent.query("do something impossible")

        # Agent should have made a tool call that failed
        self.assertIn("doesn't exist", result.message)

    def test_conversation_history(self):
        """Test that conversation history is maintained."""
        engine = MockEngine(responses=[
            LLMResponse(content="Reply 1"),
            LLMResponse(content="Reply 2"),
        ])

        agent = Agent(engine, self.approval_manager,
                      self.mock_conn, self.mock_service)

        agent.query("Question 1")
        self.assertEqual(len(agent._history), 2)  # user + assistant

        agent.query("Question 2")
        self.assertEqual(len(agent._history), 4)  # 2 turns

    def test_clear_history(self):
        """Test clearing conversation history."""
        engine = MockEngine(responses=[LLMResponse(content="hi")])
        agent = Agent(engine, self.approval_manager,
                      self.mock_conn, self.mock_service)

        agent.query("hello")
        self.assertTrue(len(agent._history) > 0)

        agent.clear_history()
        self.assertEqual(len(agent._history), 0)

    def test_build_tool_specs(self):
        """Test tool spec conversion to OpenAI format."""
        engine = MockEngine()
        agent = Agent(engine, self.approval_manager,
                      self.mock_conn, self.mock_service)

        schemas = [
            {"name": "test_tool", "description": "A test",
             "parameters": {"host": "str", "connection": "OpenStackClient"},
             "approval_level": "none", "category": "test"},
        ]
        specs = agent._build_tool_specs(schemas)

        self.assertEqual(len(specs), 1)
        self.assertEqual(specs[0]["function"]["name"], "test_tool")
        # connection should be filtered out
        self.assertNotIn("connection", specs[0]["function"]["parameters"]["properties"])
        self.assertIn("host", specs[0]["function"]["parameters"]["properties"])


# ─── Agent Response Tests ────────────────────────────────────────────


class TestAgentResponse(unittest.TestCase):
    """Tests for AgentResponse dataclass."""

    def test_defaults(self):
        resp = AgentResponse(message="test")
        self.assertFalse(resp.requires_approval)
        self.assertEqual(resp.pending_approval_id, "")
        self.assertEqual(resp.tool_calls_made, [])

    def test_with_approval(self):
        resp = AgentResponse(
            message="needs approval",
            requires_approval=True,
            pending_approval_id="abc123",
            pending_tool="fence_host",
            pending_params={"host": "h1"},
        )
        self.assertTrue(resp.requires_approval)
        self.assertEqual(resp.pending_tool, "fence_host")


if __name__ == '__main__':
    unittest.main()
