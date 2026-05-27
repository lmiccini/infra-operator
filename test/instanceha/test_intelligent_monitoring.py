"""Tests for Intelligent Monitoring (Event Bus + Observer)."""

import logging
import sys
import time
import unittest
from unittest.mock import MagicMock

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

import os
instanceha_path = os.path.join(os.path.dirname(__file__), '..', '..', 'templates', 'instanceha', 'bin')
sys.path.insert(0, os.path.abspath(instanceha_path))

from instanceha_ai.event_bus import Event, EventBus, EventType, get_event_bus, set_event_bus
from instanceha_ai.observer import (
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
        events = self.bus.get_events(event_type=EventType.FENCE_START)
        self.assertEqual(len(events), 1)

    def test_get_events_by_host(self):
        self.bus.publish(Event(event_type=EventType.FENCE_START, host="a"))
        self.bus.publish(Event(event_type=EventType.FENCE_START, host="b"))
        events = self.bus.get_events(host="a")
        self.assertEqual(len(events), 1)

    def test_get_events_by_minutes(self):
        old_event = Event(event_type=EventType.FENCE_START, host="a",
                          timestamp=time.time() - 7200)
        new_event = Event(event_type=EventType.FENCE_START, host="b")
        self.bus.publish(old_event)
        self.bus.publish(new_event)
        events = self.bus.get_events(minutes=60)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].host, "b")

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

    def test_singleton(self):
        bus1 = get_event_bus()
        bus2 = get_event_bus()
        self.assertIs(bus1, bus2)

    def test_set_event_bus(self):
        custom_bus = EventBus()
        set_event_bus(custom_bus)
        self.assertIs(get_event_bus(), custom_bus)
        set_event_bus(EventBus())


class TestObserver(unittest.TestCase):
    def setUp(self):
        self.bus = EventBus()
        self.observer = Observer(self.bus)
        self.observer.start()

    def test_repeated_fencing_alert(self):
        for _ in range(REPEATED_FAILURE_THRESHOLD):
            self.bus.publish(Event(
                event_type=EventType.FENCE_RESULT, host="compute-1",
                data={"action": "off", "success": True}, source="test"))
        alerts = self.observer.get_alerts(severity="warning")
        self.assertTrue(any(a.pattern == "repeated_fencing" for a in alerts))

    def test_no_alert_below_threshold(self):
        for _ in range(REPEATED_FAILURE_THRESHOLD - 1):
            self.bus.publish(Event(event_type=EventType.FENCE_RESULT, host="compute-1"))
        alerts = self.observer.get_alerts()
        self.assertFalse(any(a.pattern == "repeated_fencing" for a in alerts))

    def test_oscillation_alert(self):
        for i in range(OSCILLATION_THRESHOLD + 1):
            state = "down" if i % 2 == 0 else "up"
            self.bus.publish(Event(
                event_type=EventType.SERVICE_STATE_CHANGE, host="compute-2",
                data={"state": state}, source="test"))
        alerts = self.observer.get_alerts()
        self.assertTrue(any(a.pattern == "oscillating_host" for a in alerts))

    def test_cascade_alert(self):
        for i in range(CASCADE_THRESHOLD):
            self.bus.publish(Event(
                event_type=EventType.SERVICE_STATE_CHANGE, host=f"compute-{i}",
                data={"state": "down"}, source="test"))
        alerts = self.observer.get_alerts(severity="critical")
        self.assertTrue(any(a.pattern == "cascade_failure" for a in alerts))

    def test_evacuation_failure_alert(self):
        for _ in range(EVACUATION_FAILURE_THRESHOLD):
            self.bus.publish(Event(
                event_type=EventType.EVACUATION_RESULT, host="compute-3",
                data={"success": False, "server_count": 5}, source="test"))
        alerts = self.observer.get_alerts(severity="critical")
        self.assertTrue(any(a.pattern == "repeated_evacuation_failure" for a in alerts))

    def test_no_evacuation_alert_on_success(self):
        for _ in range(5):
            self.bus.publish(Event(
                event_type=EventType.EVACUATION_RESULT, host="compute-4",
                data={"success": True}, source="test"))
        alerts = self.observer.get_alerts()
        self.assertFalse(any(a.pattern == "repeated_evacuation_failure" for a in alerts))

    def test_threshold_exceeded_alert(self):
        for _ in range(3):
            self.bus.publish(Event(
                event_type=EventType.THRESHOLD_EXCEEDED,
                data={"percent": 55.0, "threshold": 50}, source="test"))
        alerts = self.observer.get_alerts()
        self.assertTrue(any(a.pattern == "frequent_threshold_block" for a in alerts))

    def test_kdump_detected_alert(self):
        self.bus.publish(Event(
            event_type=EventType.KDUMP_DETECTED, host="compute-5", source="test"))
        alerts = self.observer.get_alerts(severity="info")
        self.assertTrue(any(a.pattern == "kdump_detected" for a in alerts))

    def test_alert_deduplication(self):
        for _ in range(REPEATED_FAILURE_THRESHOLD * 2):
            self.bus.publish(Event(event_type=EventType.FENCE_RESULT, host="compute-dup"))
        alerts = [a for a in self.observer.get_alerts()
                  if a.pattern == "repeated_fencing" and a.host == "compute-dup"]
        self.assertEqual(len(alerts), 1)

    def test_acknowledge_alert(self):
        self.bus.publish(Event(event_type=EventType.KDUMP_DETECTED, host="compute-6"))
        unacked = self.observer.get_alerts(unacknowledged_only=True)
        self.assertTrue(len(unacked) > 0)
        self.observer.acknowledge_alert(0)
        unacked = self.observer.get_alerts(unacknowledged_only=True)
        self.assertEqual(len(unacked), 0)

    def test_acknowledge_invalid_index(self):
        self.assertFalse(self.observer.acknowledge_alert(999))

    def test_get_host_analysis(self):
        for _ in range(REPEATED_FAILURE_THRESHOLD):
            self.bus.publish(Event(event_type=EventType.FENCE_RESULT, host="analyzed-host"))
        self.bus.publish(Event(event_type=EventType.EVACUATION_RESULT, host="analyzed-host",
                               data={"success": True}))
        self.bus.publish(Event(event_type=EventType.EVACUATION_RESULT, host="analyzed-host",
                               data={"success": False}))
        analysis = self.observer.get_host_analysis("analyzed-host")
        self.assertEqual(analysis["host"], "analyzed-host")
        self.assertEqual(analysis["fence_count_recent"], REPEATED_FAILURE_THRESHOLD)
        self.assertEqual(analysis["evacuation_success"], 1)
        self.assertEqual(analysis["evacuation_failures"], 1)

    def test_get_cluster_health(self):
        for _ in range(EVACUATION_FAILURE_THRESHOLD):
            self.bus.publish(Event(
                event_type=EventType.EVACUATION_RESULT, host="h1",
                data={"success": False}))
        health = self.observer.get_cluster_health()
        self.assertEqual(health["health"], "critical")

    def test_cluster_health_healthy(self):
        health = self.observer.get_cluster_health()
        self.assertEqual(health["health"], "healthy")

    def test_alert_to_dict(self):
        alert = Alert(severity="warning", pattern="test", message="msg",
                      host="h", suggestion="do something")
        d = alert.to_dict()
        self.assertEqual(d["severity"], "warning")
        self.assertEqual(d["pattern"], "test")

    def test_max_alerts_capped(self):
        observer = Observer(self.bus, max_alerts=5)
        observer.start()
        for i in range(10):
            self.bus.publish(Event(event_type=EventType.KDUMP_DETECTED, host=f"host-{i}"))
        alerts = observer.get_alerts()
        self.assertLessEqual(len(alerts), 5)


if __name__ == '__main__':
    unittest.main()
