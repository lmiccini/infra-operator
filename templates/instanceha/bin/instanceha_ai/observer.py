"""AI observer for InstanceHA — detects failure patterns from the event bus."""

import logging
import threading
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional

from .event_bus import Event, EventBus, EventType


@dataclass
class Alert:
    severity: str
    pattern: str
    message: str
    host: str = ""
    suggestion: str = ""
    timestamp: float = field(default_factory=time.time)
    acknowledged: bool = False

    @property
    def time_str(self) -> str:
        return datetime.fromtimestamp(self.timestamp).strftime("%Y-%m-%d %H:%M:%S")

    def to_dict(self) -> dict:
        return {
            "severity": self.severity,
            "pattern": self.pattern,
            "message": self.message,
            "host": self.host,
            "suggestion": self.suggestion,
            "timestamp": self.timestamp,
            "time_str": self.time_str,
            "acknowledged": self.acknowledged,
        }


REPEATED_FAILURE_THRESHOLD = 3
REPEATED_FAILURE_WINDOW_MIN = 30
OSCILLATION_THRESHOLD = 4
OSCILLATION_WINDOW_MIN = 60
CASCADE_THRESHOLD = 3
CASCADE_WINDOW_MIN = 5
EVACUATION_FAILURE_THRESHOLD = 2
EVACUATION_FAILURE_WINDOW_MIN = 30


class Observer:

    def __init__(self, event_bus: EventBus, max_alerts: int = 200):
        self._bus = event_bus
        self._alerts: List[Alert] = []
        self._alerts_lock = threading.Lock()
        self._max_alerts = max_alerts

        self._host_fence_times: Dict[str, List[float]] = defaultdict(list)
        self._host_state_changes: Dict[str, List[dict]] = defaultdict(list)
        self._host_evacuation_results: Dict[str, List[dict]] = defaultdict(list)

        self._recent_down_events: List[dict] = []
        self._threshold_events: List[float] = []

        self._lock = threading.Lock()

    def start(self) -> None:
        self._bus.subscribe_all(self._on_event)
        logging.info("AI observer started")

    def stop(self) -> None:
        logging.info("AI observer stopped")

    def _on_event(self, event: Event) -> None:
        try:
            handler = self._handlers.get(event.event_type)
            if handler:
                handler(self, event)
        except Exception as e:
            logging.debug("Observer error handling %s: %s", event.event_type.value, e)

    def _on_fence_result(self, event: Event) -> None:
        with self._lock:
            self._host_fence_times[event.host].append(event.timestamp)
            cutoff = time.time() - (REPEATED_FAILURE_WINDOW_MIN * 60)
            self._host_fence_times[event.host] = [
                t for t in self._host_fence_times[event.host] if t > cutoff
            ]

        count = len(self._host_fence_times[event.host])
        if count >= REPEATED_FAILURE_THRESHOLD:
            self._add_alert(Alert(
                severity="warning",
                pattern="repeated_fencing",
                message=f"Host {event.host} has been fenced {count} times "
                        f"in the last {REPEATED_FAILURE_WINDOW_MIN} minutes",
                host=event.host,
                suggestion=f"Investigate hardware health of {event.host}. "
                           f"Consider removing from service if failures persist.",
            ))

    def _on_service_state_change(self, event: Event) -> None:
        state = event.data.get("state", "")
        with self._lock:
            self._host_state_changes[event.host].append({
                "time": event.timestamp,
                "state": state,
            })
            cutoff = time.time() - (OSCILLATION_WINDOW_MIN * 60)
            self._host_state_changes[event.host] = [
                e for e in self._host_state_changes[event.host] if e["time"] > cutoff
            ]

        changes = self._host_state_changes[event.host]
        if len(changes) >= OSCILLATION_THRESHOLD:
            transitions = 0
            for i in range(1, len(changes)):
                if changes[i]["state"] != changes[i - 1]["state"]:
                    transitions += 1
            if transitions >= OSCILLATION_THRESHOLD:
                self._add_alert(Alert(
                    severity="warning",
                    pattern="oscillating_host",
                    message=f"Host {event.host} is oscillating: {transitions} state "
                            f"transitions in {OSCILLATION_WINDOW_MIN} minutes",
                    host=event.host,
                    suggestion="The host may have intermittent hardware issues. "
                               "Consider disabling and investigating before re-enabling.",
                ))

        if state == "down":
            with self._lock:
                self._recent_down_events.append({
                    "host": event.host,
                    "time": event.timestamp,
                })
                cutoff = time.time() - (CASCADE_WINDOW_MIN * 60)
                self._recent_down_events = [
                    e for e in self._recent_down_events if e["time"] > cutoff
                ]

            unique_hosts = set(e["host"] for e in self._recent_down_events)
            if len(unique_hosts) >= CASCADE_THRESHOLD:
                self._add_alert(Alert(
                    severity="critical",
                    pattern="cascade_failure",
                    message=f"{len(unique_hosts)} hosts went down within "
                            f"{CASCADE_WINDOW_MIN} minutes: "
                            f"{', '.join(sorted(unique_hosts))}",
                    suggestion="Check shared infrastructure: network switches, "
                               "storage backends, power distribution. This pattern "
                               "suggests a common upstream failure.",
                ))

    def _on_evacuation_result(self, event: Event) -> None:
        success = event.data.get("success", False)
        with self._lock:
            self._host_evacuation_results[event.host].append({
                "time": event.timestamp,
                "success": success,
            })
            cutoff = time.time() - (EVACUATION_FAILURE_WINDOW_MIN * 60)
            self._host_evacuation_results[event.host] = [
                e for e in self._host_evacuation_results[event.host] if e["time"] > cutoff
            ]

        if not success:
            failures = [e for e in self._host_evacuation_results[event.host]
                        if not e["success"]]
            if len(failures) >= EVACUATION_FAILURE_THRESHOLD:
                self._add_alert(Alert(
                    severity="critical",
                    pattern="repeated_evacuation_failure",
                    message=f"Evacuation failed {len(failures)} times for "
                            f"{event.host} in the last {EVACUATION_FAILURE_WINDOW_MIN} minutes",
                    host=event.host,
                    suggestion="Run 'diagnose " + event.host + "' to analyze the failure. "
                               "Check target host capacity and Nova conductor logs.",
                ))

    def _on_threshold_exceeded(self, event: Event) -> None:
        with self._lock:
            self._threshold_events.append(event.timestamp)
            cutoff = time.time() - 3600
            self._threshold_events = [t for t in self._threshold_events if t > cutoff]

        if len(self._threshold_events) >= 3:
            pct = event.data.get("percent", "?")
            threshold = event.data.get("threshold", "?")
            self._add_alert(Alert(
                severity="warning",
                pattern="frequent_threshold_block",
                message=f"Threshold exceeded {len(self._threshold_events)} times in the "
                        f"last hour (current: {pct}%, threshold: {threshold}%)",
                suggestion="Consider increasing the THRESHOLD configuration value, "
                           "or investigate why so many hosts are failing simultaneously.",
            ))

    def _on_kdump_detected(self, event: Event) -> None:
        self._add_alert(Alert(
            severity="info",
            pattern="kdump_detected",
            message=f"Kernel crash dump detected on {event.host}",
            host=event.host,
            suggestion=f"Host {event.host} experienced a kernel panic. "
                       f"Review kdump output after host recovery.",
        ))

    _handlers = {
        EventType.FENCE_RESULT: _on_fence_result,
        EventType.SERVICE_STATE_CHANGE: _on_service_state_change,
        EventType.EVACUATION_RESULT: _on_evacuation_result,
        EventType.THRESHOLD_EXCEEDED: _on_threshold_exceeded,
        EventType.KDUMP_DETECTED: _on_kdump_detected,
    }

    def _add_alert(self, alert: Alert) -> None:
        with self._alerts_lock:
            cutoff = time.time() - 300
            for existing in reversed(self._alerts):
                if existing.timestamp < cutoff:
                    break
                if (existing.pattern == alert.pattern and
                        existing.host == alert.host):
                    return

            self._alerts.append(alert)
            if len(self._alerts) > self._max_alerts:
                self._alerts = self._alerts[-self._max_alerts:]

        logging.info("AI Alert [%s] %s: %s", alert.severity.upper(),
                     alert.pattern, alert.message)

    def get_alerts(self, severity: Optional[str] = None,
                   unacknowledged_only: bool = False,
                   minutes: Optional[int] = None) -> List[Alert]:
        with self._alerts_lock:
            alerts = list(self._alerts)

        if severity:
            alerts = [a for a in alerts if a.severity == severity]
        if unacknowledged_only:
            alerts = [a for a in alerts if not a.acknowledged]
        if minutes is not None:
            cutoff = time.time() - (minutes * 60)
            alerts = [a for a in alerts if a.timestamp >= cutoff]

        return alerts

    def acknowledge_alert(self, index: int) -> bool:
        with self._alerts_lock:
            unacked = [a for a in self._alerts if not a.acknowledged]
            if 0 <= index < len(unacked):
                unacked[index].acknowledged = True
                return True
            return False

    def get_host_analysis(self, host: str) -> dict:
        with self._lock:
            fence_count = len(self._host_fence_times.get(host, []))
            state_changes = self._host_state_changes.get(host, [])
            evac_results = self._host_evacuation_results.get(host, [])

        alerts = [a for a in self.get_alerts() if a.host == host]

        evac_success = sum(1 for e in evac_results if e["success"])
        evac_fail = sum(1 for e in evac_results if not e["success"])

        return {
            "host": host,
            "fence_count_recent": fence_count,
            "state_transitions": len(state_changes),
            "evacuation_success": evac_success,
            "evacuation_failures": evac_fail,
            "active_alerts": len([a for a in alerts if not a.acknowledged]),
            "alerts": [a.to_dict() for a in alerts[-5:]],
        }

    def get_cluster_health(self) -> dict:
        alerts = self.get_alerts()
        unacked = [a for a in alerts if not a.acknowledged]

        severity_counts = defaultdict(int)
        for a in unacked:
            severity_counts[a.severity] += 1

        host_issues = defaultdict(int)
        for a in unacked:
            if a.host:
                host_issues[a.host] += 1
        problematic = sorted(host_issues.items(), key=lambda x: -x[1])[:5]

        with self._lock:
            recent_down = len(self._recent_down_events)

        return {
            "total_alerts": len(alerts),
            "unacknowledged": len(unacked),
            "severity": dict(severity_counts),
            "recent_down_events": recent_down,
            "problematic_hosts": [{"host": h, "alerts": c} for h, c in problematic],
            "health": (
                "critical" if severity_counts.get("critical", 0) > 0
                else "warning" if severity_counts.get("warning", 0) > 0
                else "healthy"
            ),
        }
