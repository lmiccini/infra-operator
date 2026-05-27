"""Cluster state summarization for LLM context."""

import logging
from datetime import datetime, timedelta


def summarize_cluster(connection, service) -> str:
    lines = []

    try:
        services = connection.services.list(binary="nova-compute")
        if not services:
            return "No compute services found."

        total = len(services)
        up_enabled = []
        down = []
        disabled = []
        forced_down = []

        for svc in services:
            if svc.state == "up" and "enabled" in svc.status:
                up_enabled.append(svc.host)
            elif svc.state == "down":
                down.append(svc.host)
                if svc.forced_down:
                    forced_down.append(svc.host)
            elif "disabled" in svc.status:
                disabled.append(svc.host)
                reason = getattr(svc, "disabled_reason", "")
                if reason:
                    disabled[-1] = f"{svc.host} ({reason})"

        lines.append(f"Computes: {total} total, {len(up_enabled)} up+enabled, "
                      f"{len(down)} down, {len(disabled)} disabled")

        if down:
            lines.append(f"Down hosts: {', '.join(down)}")
        if forced_down:
            lines.append(f"Forced-down: {', '.join(forced_down)}")
        if disabled:
            lines.append(f"Disabled: {', '.join(disabled)}")

    except Exception as e:
        lines.append(f"Could not query compute services: {e}")

    if hasattr(service, "kdump_fenced_hosts") and service.kdump_fenced_hosts:
        lines.append(f"Kdump-fenced hosts: {', '.join(service.kdump_fenced_hosts)}")

    if hasattr(service, "hosts_processing"):
        processing = [h for h in service.hosts_processing]
        if processing:
            lines.append(f"Currently processing: {', '.join(processing)}")

    return "\n".join(lines)


def summarize_recent_events(connection, service, minutes: int = 30) -> str:
    lines = []
    cutoff = datetime.now() - timedelta(minutes=minutes)

    try:
        query_time = cutoff.isoformat()
        migrations = connection.migrations.list(
            migration_type="evacuation",
            changes_since=query_time,
            limit="20",
        )
        if migrations:
            status_counts = {}
            for m in migrations:
                status = getattr(m, "status", "unknown")
                status_counts[status] = status_counts.get(status, 0) + 1
            parts = [f"{s}: {c}" for s, c in sorted(status_counts.items())]
            lines.append(f"Evacuations (last {minutes}m): {', '.join(parts)}")
        else:
            lines.append(f"No evacuations in the last {minutes}m")
    except Exception as e:
        lines.append(f"Could not query migrations: {e}")

    if hasattr(service, "kdump_hosts_timestamp"):
        cutoff_ts = cutoff.timestamp()
        recent_kdump = {h: datetime.fromtimestamp(ts).strftime("%H:%M:%S")
                        for h, ts in service.kdump_hosts_timestamp.items()
                        if ts >= cutoff_ts}
        if recent_kdump:
            parts = [f"{h} at {t}" for h, t in recent_kdump.items()]
            lines.append(f"Recent kdump: {', '.join(parts)}")

    if not lines:
        lines.append("No notable events in the recent period.")

    return "\n".join(lines)


def build_context(connection, service, minutes: int = 30) -> str:
    from .prompts import build_context_message

    cluster = summarize_cluster(connection, service)
    events = summarize_recent_events(connection, service, minutes)
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    return build_context_message(cluster, events, timestamp)
