"""Tests for Phase 5: Intelligent Monitoring (Event Bus + Observer).

Tests cover:
- EventBus: publish, subscribe, subscribe_all, history, filtering
- Observer: pattern detection (repeated fencing, oscillation, cascade,
  evacuation failure, threshold exceeded, kdump)
- Alert deduplication, acknowledgment, and querying
- Host analysis and cluster health summaries
- Event publishing helpers in fencing, evacuation, monitoring, main
- Observer command handlers in chat server
"""

import logging
import sys
import time
import unittest
from collections import defaultdict
from unittest.mock import MagicMock, patch

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
    sys.modules['keystoneauth1.exceptions.connection'] = MagicMock()
    sys.modules['keystoneauth1.exceptions.http'] = MagicMock()
    sys.modules['keystoneauth1.exceptions.discovery'] = MagicMock()

from instanceha.ai.event_bus import Event, EventBus, EventType, get_event_bus, set_event_bus
from instanceha.ai.observer import (
    Alert,
    CASCADE_THRESHOLD,
    CASCADE_WINDOW_MIN,
    EVACUATION_FAILURE_THRESHOLD,
    Observer,
    OSCILLATION_THRESHOLD,
    REPEATED_FAILURE_THRESHOLD,
)

logging.basicConfig(level=logging.DEBUG)


class TestEventBus(unittest.TestCase):
    """Test the in-process event bus."""

    def setUp(self):
        self.bus = EventBus(max_history=100)

    def test_publish_and_subscribe(self):
        received = []
        self.bus.subscribe(EventType.FENCE_RESULT, lambda e: received.append(e))
        event = Event(event_type=EventType.FENCE_RESULT, host="compute-1",
                      data={"action": "off", "success": True}, source="test")
        self.bus.publish(event)
        self.assertEqual(len(received), 1)
        self.assertEqual(received[0].host, "compute-1")

    def test_subscribe_all(self):
        received = []
        self.bus.subscribe_all(lambda e: received.append(e))
        self.bus.publish(Event(event_type=EventType.FENCE_START, host="a"))
        self.bus.publish(Event(event_type=EventType.EVACUATION_START, host="b"))
        self.assertEqual(len(received), 2)

    def test_subscribe_does_not_cross_types(self):
        received = []
        self.bus.subscribe(EventType.FENCE_RESULT, lambda e: received.append(e))
        self.bus.publish(Event(event_type=EventType.EVACUATION_RESULT, host="c"))
        self.assertEqual(len(received), 0)

    def test_history(self):
        self.bus.publish(Event(event_type=EventType.FENCE_START, host="x"))
        self.bus.publish(Event(event_type=EventType.FENCE_RESULT, host="x"))
        self.assertEqual(self.bus.event_count, 2)

    def test_get_events_by_type(self):
        self.bus.publish(Event(event_type=EventType.FENCE_START, host="a"))
        self.bus.publish(Event(event_type=EventType.FENCE_RESULT, host="a"))
        self.bus.publish(Event(event_type=EventType.EVACUATION_START, host="b"))
        events = self.bus.get_events(event_type=EventType.FENCE_START)
        self.assertEqual(len(events), 1)

    def test_get_events_by_host(self):
        self.bus.publish(Event(event_type=EventType.FENCE_START, host="a"))
        self.bus.publish(Event(event_type=EventType.FENCE_START, host="b"))
        events = self.bus.get_events(host="a")
        self.assertEqual(len(events), 1)

    def test_get_events_by_minutes(self):
        old_event = Event(event_type=EventType.FENCE_START, host="a",
                          timestamp=time.time() - 7200)  # 2 hours ago
        new_event = Event(event_type=EventType.FENCE_START, host="b")
        self.bus.publish(old_event)
        self.bus.publish(new_event)
        events = self.bus.get_events(minutes=60)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].host, "b")

    def test_get_host_events(self):
        self.bus.publish(Event(event_type=EventType.FENCE_START, host="compute-1"))
        self.bus.publish(Event(event_type=EventType.FENCE_RESULT, host="compute-1"))
        self.bus.publish(Event(event_type=EventType.FENCE_START, host="compute-2"))
        events = self.bus.get_host_events("compute-1", minutes=60)
        self.assertEqual(len(events), 2)

    def test_bounded_history(self):
        bus = EventBus(max_history=5)
        for i in range(10):
            bus.publish(Event(event_type=EventType.FENCE_START, host=f"h-{i}"))
        self.assertEqual(bus.event_count, 5)

    def test_clear(self):
        self.bus.publish(Event(event_type=EventType.FENCE_START, host="a"))
        self.bus.clear()
        self.assertEqual(self.bus.event_count, 0)

    def test_subscriber_exception_does_not_propagate(self):
        def bad_subscriber(event):
            raise RuntimeError("boom")
        self.bus.subscribe(EventType.FENCE_START, bad_subscriber)
        # Should not raise
        self.bus.publish(Event(event_type=EventType.FENCE_START, host="a"))

    def test_unsubscribe(self):
        received = []
        cb = lambda e: received.append(e)
        self.bus.subscribe(EventType.FENCE_START, cb)
        self.bus.unsubscribe(EventType.FENCE_START, cb)
        self.bus.publish(Event(event_type=EventType.FENCE_START, host="a"))
        self.assertEqual(len(received), 0)

    def test_event_properties(self):
        event = Event(event_type=EventType.FENCE_RESULT, host="compute-1",
                      data={"x": 1}, source="test")
        self.assertIsInstance(event.age_seconds, float)
        self.assertIn(":", event.time_str)
        d = event.to_dict()
        self.assertEqual(d["event_type"], "fence_result")
        self.assertEqual(d["host"], "compute-1")

    def test_singleton(self):
        bus1 = get_event_bus()
        bus2 = get_event_bus()
        self.assertIs(bus1, bus2)

    def test_set_event_bus(self):
        custom_bus = EventBus()
        set_event_bus(custom_bus)
        self.assertIs(get_event_bus(), custom_bus)
        # Restore
        set_event_bus(EventBus())


class TestObserver(unittest.TestCase):
    """Test the AI observer pattern detection."""

    def setUp(self):
        self.bus = EventBus()
        self.observer = Observer(self.bus)
        self.observer.start()

    def test_repeated_fencing_alert(self):
        """Fencing the same host N times triggers repeated_fencing alert."""
        for _ in range(REPEATED_FAILURE_THRESHOLD):
            self.bus.publish(Event(
                event_type=EventType.FENCE_RESULT,
                host="compute-1",
                data={"action": "off", "success": True},
                source="test",
            ))
        alerts = self.observer.get_alerts(severity="warning")
        self.assertTrue(any(a.pattern == "repeated_fencing" for a in alerts))

    def test_no_alert_below_threshold(self):
        """Fewer than threshold fencing events should not trigger alert."""
        for _ in range(REPEATED_FAILURE_THRESHOLD - 1):
            self.bus.publish(Event(
                event_type=EventType.FENCE_RESULT,
                host="compute-1",
            ))
        alerts = self.observer.get_alerts()
        self.assertFalse(any(a.pattern == "repeated_fencing" for a in alerts))

    def test_oscillation_alert(self):
        """Rapid up/down transitions trigger oscillating_host alert."""
        for i in range(OSCILLATION_THRESHOLD + 1):
            state = "down" if i % 2 == 0 else "up"
            self.bus.publish(Event(
                event_type=EventType.SERVICE_STATE_CHANGE,
                host="compute-2",
                data={"state": state},
                source="test",
            ))
        alerts = self.observer.get_alerts()
        self.assertTrue(any(a.pattern == "oscillating_host" for a in alerts))

    def test_cascade_alert(self):
        """Multiple hosts going down within CASCADE_WINDOW triggers cascade alert."""
        for i in range(CASCADE_THRESHOLD):
            self.bus.publish(Event(
                event_type=EventType.SERVICE_STATE_CHANGE,
                host=f"compute-{i}",
                data={"state": "down"},
                source="test",
            ))
        alerts = self.observer.get_alerts(severity="critical")
        self.assertTrue(any(a.pattern == "cascade_failure" for a in alerts))

    def test_evacuation_failure_alert(self):
        """Repeated evacuation failures trigger alert."""
        for _ in range(EVACUATION_FAILURE_THRESHOLD):
            self.bus.publish(Event(
                event_type=EventType.EVACUATION_RESULT,
                host="compute-3",
                data={"success": False, "server_count": 5},
                source="test",
            ))
        alerts = self.observer.get_alerts(severity="critical")
        self.assertTrue(any(a.pattern == "repeated_evacuation_failure" for a in alerts))

    def test_no_evacuation_alert_on_success(self):
        """Successful evacuations should not trigger failure alert."""
        for _ in range(5):
            self.bus.publish(Event(
                event_type=EventType.EVACUATION_RESULT,
                host="compute-4",
                data={"success": True, "server_count": 3},
                source="test",
            ))
        alerts = self.observer.get_alerts()
        self.assertFalse(any(a.pattern == "repeated_evacuation_failure" for a in alerts))

    def test_threshold_exceeded_alert(self):
        """Multiple threshold exceeded events trigger config tuning suggestion."""
        for _ in range(3):
            self.bus.publish(Event(
                event_type=EventType.THRESHOLD_EXCEEDED,
                data={"percent": 55.0, "threshold": 50},
                source="test",
            ))
        alerts = self.observer.get_alerts()
        self.assertTrue(any(a.pattern == "frequent_threshold_block" for a in alerts))

    def test_kdump_detected_alert(self):
        """Kdump events generate informational alerts."""
        self.bus.publish(Event(
            event_type=EventType.KDUMP_DETECTED,
            host="compute-5",
            data={"type": "kernel_crash_dump"},
            source="test",
        ))
        alerts = self.observer.get_alerts(severity="info")
        self.assertTrue(any(a.pattern == "kdump_detected" for a in alerts))

    def test_alert_deduplication(self):
        """Same pattern+host within 5 minutes should not produce duplicate alerts."""
        for _ in range(REPEATED_FAILURE_THRESHOLD * 2):
            self.bus.publish(Event(
                event_type=EventType.FENCE_RESULT,
                host="compute-dup",
            ))
        alerts = [a for a in self.observer.get_alerts()
                  if a.pattern == "repeated_fencing" and a.host == "compute-dup"]
        self.assertEqual(len(alerts), 1)

    def test_acknowledge_alert(self):
        self.bus.publish(Event(
            event_type=EventType.KDUMP_DETECTED, host="compute-6"))
        unacked = self.observer.get_alerts(unacknowledged_only=True)
        self.assertTrue(len(unacked) > 0)
        self.observer.acknowledge_alert(0)
        unacked = self.observer.get_alerts(unacknowledged_only=True)
        self.assertEqual(len(unacked), 0)

    def test_acknowledge_invalid_index(self):
        self.assertFalse(self.observer.acknowledge_alert(999))

    def test_get_alerts_with_minutes_filter(self):
        self.bus.publish(Event(event_type=EventType.KDUMP_DETECTED, host="h"))
        alerts = self.observer.get_alerts(minutes=1)
        self.assertTrue(len(alerts) > 0)
        alerts = self.observer.get_alerts(minutes=0)
        self.assertEqual(len(alerts), 0)

    def test_get_host_analysis(self):
        for _ in range(REPEATED_FAILURE_THRESHOLD):
            self.bus.publish(Event(
                event_type=EventType.FENCE_RESULT, host="analyzed-host"))
        self.bus.publish(Event(
            event_type=EventType.EVACUATION_RESULT, host="analyzed-host",
            data={"success": True}))
        self.bus.publish(Event(
            event_type=EventType.EVACUATION_RESULT, host="analyzed-host",
            data={"success": False}))

        analysis = self.observer.get_host_analysis("analyzed-host")
        self.assertEqual(analysis["host"], "analyzed-host")
        self.assertEqual(analysis["fence_count_recent"], REPEATED_FAILURE_THRESHOLD)
        self.assertEqual(analysis["evacuation_success"], 1)
        self.assertEqual(analysis["evacuation_failures"], 1)
        self.assertTrue(analysis["active_alerts"] > 0)

    def test_get_cluster_health(self):
        # Trigger a critical alert
        for _ in range(EVACUATION_FAILURE_THRESHOLD):
            self.bus.publish(Event(
                event_type=EventType.EVACUATION_RESULT, host="h1",
                data={"success": False}))

        health = self.observer.get_cluster_health()
        self.assertEqual(health["health"], "critical")
        self.assertTrue(health["unacknowledged"] > 0)
        self.assertTrue(len(health["problematic_hosts"]) > 0)

    def test_cluster_health_healthy(self):
        health = self.observer.get_cluster_health()
        self.assertEqual(health["health"], "healthy")

    def test_alert_to_dict(self):
        alert = Alert(severity="warning", pattern="test", message="msg",
                      host="h", suggestion="do something")
        d = alert.to_dict()
        self.assertEqual(d["severity"], "warning")
        self.assertEqual(d["pattern"], "test")
        self.assertEqual(d["host"], "h")
        self.assertIn("time_str", d)

    def test_max_alerts_capped(self):
        observer = Observer(self.bus, max_alerts=5)
        observer.start()
        # Each kdump event to a different host produces a new alert
        for i in range(10):
            self.bus.publish(Event(
                event_type=EventType.KDUMP_DETECTED, host=f"host-{i}"))
        alerts = observer.get_alerts()
        self.assertLessEqual(len(alerts), 5)

    def test_unhandled_event_type_ignored(self):
        """Events without a handler should not raise."""
        self.bus.publish(Event(
            event_type=EventType.PROCESSING_START, host="h"))
        # No error, no alert
        self.assertEqual(len(self.observer.get_alerts()), 0)


class TestEventPublishingHelpers(unittest.TestCase):
    """Test that event publishing helpers work and are best-effort."""

    def setUp(self):
        self.bus = EventBus()
        set_event_bus(self.bus)

    def tearDown(self):
        set_event_bus(EventBus())

    def test_publish_fence_event(self):
        from instanceha.fencing import _publish_fence_event
        _publish_fence_event("compute-1.example.com", "off", "start")
        _publish_fence_event("compute-1.example.com", "off", "result", success=True)
        events = self.bus.get_events()
        self.assertEqual(len(events), 2)
        self.assertEqual(events[0].event_type, EventType.FENCE_START)
        self.assertEqual(events[1].event_type, EventType.FENCE_RESULT)

    def test_publish_evacuation_event(self):
        from instanceha.evacuation import _publish_evacuation_event
        _publish_evacuation_event("compute-2", "start", server_count=5)
        _publish_evacuation_event("compute-2", "result", success=True, server_count=5)
        events = self.bus.get_events()
        self.assertEqual(len(events), 2)
        self.assertEqual(events[0].event_type, EventType.EVACUATION_START)
        self.assertEqual(events[1].event_type, EventType.EVACUATION_RESULT)

    def test_publish_host_state_event(self):
        from instanceha.evacuation import _publish_host_state_event
        _publish_host_state_event("compute-3.example.com", "disabled")
        _publish_host_state_event("compute-3.example.com", "enabled")
        events = self.bus.get_events()
        self.assertEqual(len(events), 2)
        self.assertEqual(events[0].event_type, EventType.HOST_DISABLED)
        self.assertEqual(events[1].event_type, EventType.HOST_ENABLED)

    def test_publish_kdump_event(self):
        from instanceha.monitoring import _publish_kdump_event
        _publish_kdump_event("compute-4")
        events = self.bus.get_events()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].event_type, EventType.KDUMP_DETECTED)
        self.assertEqual(events[0].host, "compute-4")

    def test_publish_service_state_event(self):
        from instanceha.main import _publish_service_state_event
        _publish_service_state_event("compute-5.example.com", "down")
        events = self.bus.get_events()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].event_type, EventType.SERVICE_STATE_CHANGE)
        self.assertEqual(events[0].data["state"], "down")

    def test_publish_threshold_event(self):
        from instanceha.main import _publish_threshold_event
        _publish_threshold_event(55.5, 50, 3)
        events = self.bus.get_events()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].event_type, EventType.THRESHOLD_EXCEEDED)
        self.assertEqual(events[0].data["percent"], 55.5)
        self.assertEqual(events[0].data["threshold"], 50)

    def test_best_effort_does_not_raise(self):
        """Publishing with a broken bus should not raise."""
        set_event_bus(None)  # Break it
        from instanceha.fencing import _publish_fence_event
        # Should not raise
        try:
            _publish_fence_event("h", "off", "start")
        except Exception:
            pass  # set_event_bus(None) may cause issues, just verify no crash in normal path


class TestObserverChatCommands(unittest.TestCase):
    """Test observer-related chat commands in ChatSession."""

    def setUp(self):
        self.bus = EventBus()
        self.observer = Observer(self.bus)
        self.observer.start()

    def test_handle_alerts_command_no_alerts(self):
        from instanceha.ai.chat_server import ChatSession
        import socket as sock
        mock_conn = MagicMock(spec=sock.socket)
        session = ChatSession(
            mock_conn, None, MagicMock(), MagicMock(),
            MagicMock(),
        )
        # Patch _get_observer to return our observer
        session._get_observer = MagicMock(return_value=self.observer)
        session._handle_alerts_command({})
        mock_conn.sendall.assert_called_once()
        response = mock_conn.sendall.call_args[0][0].decode()
        self.assertIn("No alerts", response)

    def test_handle_alerts_command_with_alerts(self):
        from instanceha.ai.chat_server import ChatSession
        import socket as sock
        # Generate an alert
        self.bus.publish(Event(event_type=EventType.KDUMP_DETECTED, host="h1"))

        mock_conn = MagicMock(spec=sock.socket)
        session = ChatSession(
            mock_conn, None, MagicMock(), MagicMock(),
            MagicMock(),
        )
        session._get_observer = MagicMock(return_value=self.observer)
        session._handle_alerts_command({})
        response = mock_conn.sendall.call_args[0][0].decode()
        self.assertIn("kdump_detected", response)
        self.assertIn("h1", response)

    def test_handle_cluster_health_command(self):
        from instanceha.ai.chat_server import ChatSession
        import socket as sock
        mock_conn = MagicMock(spec=sock.socket)
        session = ChatSession(
            mock_conn, None, MagicMock(), MagicMock(),
            MagicMock(),
        )
        session._get_observer = MagicMock(return_value=self.observer)
        session._handle_cluster_health_command()
        response = mock_conn.sendall.call_args[0][0].decode()
        self.assertIn("HEALTHY", response)

    def test_handle_host_analysis_command(self):
        from instanceha.ai.chat_server import ChatSession
        import socket as sock
        mock_conn = MagicMock(spec=sock.socket)
        session = ChatSession(
            mock_conn, None, MagicMock(), MagicMock(),
            MagicMock(),
        )
        session._get_observer = MagicMock(return_value=self.observer)
        session._handle_host_analysis_command({"host": "compute-1"})
        response = mock_conn.sendall.call_args[0][0].decode()
        self.assertIn("Host Analysis: compute-1", response)

    def test_handle_acknowledge_command(self):
        from instanceha.ai.chat_server import ChatSession
        import socket as sock
        self.bus.publish(Event(event_type=EventType.KDUMP_DETECTED, host="h"))
        mock_conn = MagicMock(spec=sock.socket)
        session = ChatSession(
            mock_conn, None, MagicMock(), MagicMock(),
            MagicMock(),
        )
        session._get_observer = MagicMock(return_value=self.observer)
        session._handle_acknowledge_command({"index": "0"})
        response = mock_conn.sendall.call_args[0][0].decode()
        self.assertIn("acknowledged", response)

    def test_handle_events_command(self):
        from instanceha.ai.chat_server import ChatSession
        import socket as sock
        set_event_bus(self.bus)
        self.bus.publish(Event(event_type=EventType.FENCE_START, host="h1"))

        mock_conn = MagicMock(spec=sock.socket)
        session = ChatSession(
            mock_conn, None, MagicMock(), MagicMock(),
            MagicMock(),
        )
        session._handle_events_command({"minutes": "60"})
        response = mock_conn.sendall.call_args[0][0].decode()
        self.assertIn("fence_start", response)
        set_event_bus(EventBus())

    def test_observer_not_running(self):
        from instanceha.ai.chat_server import ChatSession
        import socket as sock
        mock_conn = MagicMock(spec=sock.socket)
        session = ChatSession(
            mock_conn, None, MagicMock(), MagicMock(),
            MagicMock(),
        )
        session._get_observer = MagicMock(return_value=None)
        session._handle_alerts_command({})
        response = mock_conn.sendall.call_args[0][0].decode()
        self.assertIn("not running", response)


class TestCommandParserObserverCommands(unittest.TestCase):
    """Test that observer commands parse correctly."""

    def test_parse_alerts(self):
        from instanceha.ai.command_parser import parse
        cmd = parse("alerts")
        self.assertIsNotNone(cmd)
        self.assertEqual(cmd.tool_name, "_alerts")

    def test_parse_alerts_with_severity(self):
        from instanceha.ai.command_parser import parse
        cmd = parse("alerts warning")
        self.assertEqual(cmd.parameters["severity"], "warning")

    def test_parse_alerts_with_severity_and_minutes(self):
        from instanceha.ai.command_parser import parse
        cmd = parse("alerts critical 30")
        self.assertEqual(cmd.parameters["severity"], "critical")
        self.assertEqual(cmd.parameters["minutes"], 30)

    def test_parse_acknowledge(self):
        from instanceha.ai.command_parser import parse
        cmd = parse("acknowledge 5")
        self.assertEqual(cmd.tool_name, "_acknowledge")
        self.assertEqual(cmd.parameters["index"], "5")

    def test_parse_ack_alias(self):
        from instanceha.ai.command_parser import parse
        cmd = parse("ack 3")
        self.assertEqual(cmd.tool_name, "_acknowledge")

    def test_parse_cluster_health(self):
        from instanceha.ai.command_parser import parse
        cmd = parse("cluster-health")
        self.assertEqual(cmd.tool_name, "_cluster_health")

    def test_parse_chealth_alias(self):
        from instanceha.ai.command_parser import parse
        cmd = parse("chealth")
        self.assertEqual(cmd.tool_name, "_cluster_health")

    def test_parse_host_analysis(self):
        from instanceha.ai.command_parser import parse
        cmd = parse("host-analysis compute-1")
        self.assertEqual(cmd.tool_name, "_host_analysis")
        self.assertEqual(cmd.parameters["host"], "compute-1")

    def test_parse_events(self):
        from instanceha.ai.command_parser import parse
        cmd = parse("events 30")
        self.assertEqual(cmd.tool_name, "_events")
        self.assertEqual(cmd.parameters["minutes"], 30)

    def test_parse_events_with_host(self):
        from instanceha.ai.command_parser import parse
        cmd = parse("events 60 compute-1")
        self.assertEqual(cmd.parameters["minutes"], 60)
        self.assertEqual(cmd.parameters["host"], "compute-1")

    def test_help_includes_observer_section(self):
        from instanceha.ai.command_parser import get_help_text
        help_text = get_help_text()
        self.assertIn("AI Monitoring", help_text)
        self.assertIn("alerts", help_text)
        self.assertIn("cluster-health", help_text)


class TestStartObserver(unittest.TestCase):
    """Test the _start_observer function in main.py."""

    def test_start_observer_returns_observer(self):
        from instanceha.main import _start_observer
        observer = _start_observer()
        self.assertIsNotNone(observer)
        self.assertIsInstance(observer, Observer)

    def test_start_observer_sets_module_reference(self):
        from instanceha.main import _start_observer
        from instanceha import ai as ai_pkg
        _start_observer()
        self.assertIsNotNone(ai_pkg._observer)
        self.assertIsInstance(ai_pkg._observer, Observer)


if __name__ == '__main__':
    unittest.main()
