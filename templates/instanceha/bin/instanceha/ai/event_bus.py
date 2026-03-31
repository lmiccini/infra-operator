"""In-process event bus for InstanceHA.

Lightweight pub/sub system that decouples the control loop from the AI
observer layer.  Events are published by the existing fencing, evacuation,
and monitoring code; the AI observer subscribes to detect patterns and
generate proactive insights.

Thread-safe, bounded history, zero external dependencies.
"""

import logging
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Callable, Dict, List, Optional


class EventType(Enum):
    """Categories of events published by the control loop."""
    SERVICE_STATE_CHANGE = "service_state_change"
    FENCE_START = "fence_start"
    FENCE_RESULT = "fence_result"
    EVACUATION_START = "evacuation_start"
    EVACUATION_RESULT = "evacuation_result"
    HOST_DISABLED = "host_disabled"
    HOST_ENABLED = "host_enabled"
    KDUMP_DETECTED = "kdump_detected"
    THRESHOLD_EXCEEDED = "threshold_exceeded"
    PROCESSING_START = "processing_start"
    PROCESSING_COMPLETE = "processing_complete"


@dataclass
class Event:
    """A single event published to the bus."""
    event_type: EventType
    timestamp: float = field(default_factory=time.time)
    host: str = ""
    data: Dict[str, Any] = field(default_factory=dict)
    source: str = ""

    @property
    def age_seconds(self) -> float:
        return time.time() - self.timestamp

    @property
    def time_str(self) -> str:
        return datetime.fromtimestamp(self.timestamp).strftime("%H:%M:%S")

    def to_dict(self) -> dict:
        return {
            "event_type": self.event_type.value,
            "timestamp": self.timestamp,
            "time_str": self.time_str,
            "host": self.host,
            "data": self.data,
            "source": self.source,
        }


# Type alias for subscriber callbacks
Subscriber = Callable[[Event], None]


class EventBus:
    """Thread-safe in-process event bus with bounded history.

    Usage::

        bus = EventBus()

        # Subscribe
        def on_fence(event):
            print(f"Fenced {event.host}: {event.data}")
        bus.subscribe(EventType.FENCE_RESULT, on_fence)

        # Publish
        bus.publish(Event(
            event_type=EventType.FENCE_RESULT,
            host="compute-1",
            data={"action": "off", "success": True},
            source="fencing",
        ))

        # Query history
        recent = bus.get_events(minutes=30)
    """

    def __init__(self, max_history: int = 1000):
        self._subscribers: Dict[EventType, List[Subscriber]] = {}
        self._history: deque = deque(maxlen=max_history)
        self._lock = threading.Lock()
        self._all_subscribers: List[Subscriber] = []

    def subscribe(self, event_type: EventType, callback: Subscriber) -> None:
        """Subscribe to a specific event type."""
        with self._lock:
            if event_type not in self._subscribers:
                self._subscribers[event_type] = []
            self._subscribers[event_type].append(callback)

    def subscribe_all(self, callback: Subscriber) -> None:
        """Subscribe to all event types."""
        with self._lock:
            self._all_subscribers.append(callback)

    def unsubscribe(self, event_type: EventType, callback: Subscriber) -> None:
        """Remove a subscriber."""
        with self._lock:
            if event_type in self._subscribers:
                try:
                    self._subscribers[event_type].remove(callback)
                except ValueError:
                    pass

    def publish(self, event: Event) -> None:
        """Publish an event to all matching subscribers.

        Subscribers are called synchronously in the publisher's thread.
        Exceptions in subscribers are caught and logged -- they never
        propagate to the publisher.
        """
        with self._lock:
            self._history.append(event)
            # Copy subscriber lists to release the lock before calling them
            type_subs = list(self._subscribers.get(event.event_type, []))
            all_subs = list(self._all_subscribers)

        for callback in type_subs + all_subs:
            try:
                callback(event)
            except Exception as e:
                logging.warning("Event subscriber error for %s: %s",
                                event.event_type.value, e)

    def get_events(self, minutes: Optional[int] = None,
                   event_type: Optional[EventType] = None,
                   host: Optional[str] = None) -> List[Event]:
        """Query event history with optional filters."""
        with self._lock:
            events = list(self._history)

        if minutes is not None:
            cutoff = time.time() - (minutes * 60)
            events = [e for e in events if e.timestamp >= cutoff]

        if event_type is not None:
            events = [e for e in events if e.event_type == event_type]

        if host is not None:
            events = [e for e in events if e.host == host]

        return events

    def get_host_events(self, host: str, minutes: int = 60) -> List[Event]:
        """Get all events for a specific host within a time window."""
        return self.get_events(minutes=minutes, host=host)

    def clear(self) -> None:
        """Clear all event history."""
        with self._lock:
            self._history.clear()

    @property
    def event_count(self) -> int:
        """Total events in history."""
        with self._lock:
            return len(self._history)


# Module-level singleton, created lazily or by main.py
_bus: Optional[EventBus] = None
_bus_lock = threading.Lock()


def get_event_bus() -> EventBus:
    """Get the global event bus singleton."""
    global _bus
    with _bus_lock:
        if _bus is None:
            _bus = EventBus()
        return _bus


def set_event_bus(bus: EventBus) -> None:
    """Set the global event bus (for testing or custom configuration)."""
    global _bus
    with _bus_lock:
        _bus = bus
