"""Diagnostic tools for analyzing cluster state and failures. No approval required."""

import logging
from datetime import datetime, timedelta
from typing import Optional

from .tools import ApprovalLevel, ToolResult, tool


@tool("diagnose_evacuation_failure", "Analyze why an evacuation failed for a host",
      category="diagnostic",
      parameters={"connection": "OpenStackClient", "host": "str", "service": "InstanceHAService"})
def diagnose_evacuation_failure(connection, host: str, service) -> ToolResult:
    """Analyze evacuation failure for a host by checking migrations, services, and capacity."""
    try:
        findings = []
        recommendations = []

        # Check service state
        services = connection.services.list(host=host, binary="nova-compute")
        if not services:
            findings.append(f"No compute service found for {host}")
            recommendations.append("Verify the hostname is correct and the service is registered")
        else:
            svc = services[0]
            findings.append(f"Service state: {svc.state}, status: {svc.status}, forced_down: {svc.forced_down}")
            if svc.forced_down:
                findings.append(f"Disabled reason: {getattr(svc, 'disabled_reason', 'none')}")

        # Check recent migrations
        query_time = (datetime.now() - timedelta(minutes=120)).isoformat()
        try:
            migrations = connection.migrations.list(
                source_compute=host,
                migration_type="evacuation",
                changes_since=query_time,
                limit="50",
            )
            if migrations:
                status_counts = {}
                for m in migrations:
                    status = getattr(m, "status", "unknown")
                    status_counts[status] = status_counts.get(status, 0) + 1
                findings.append(f"Recent evacuation migrations: {status_counts}")

                errors = [m for m in migrations if getattr(m, "status", "") == "error"]
                if errors:
                    findings.append(f"{len(errors)} migration(s) in error state")
                    recommendations.append("Check Nova conductor logs for migration error details")
            else:
                findings.append("No recent evacuation migrations found")
                recommendations.append("Evacuation may not have been triggered - check fencing status")
        except Exception as e:
            findings.append(f"Could not query migrations: {e}")

        # Check available capacity
        all_services = connection.services.list(binary="nova-compute")
        available_hosts = [s for s in all_services
                          if s.state == "up" and "enabled" in s.status and s.host != host]
        findings.append(f"Available target hosts: {len(available_hosts)}")
        if not available_hosts:
            findings.append("CRITICAL: No available hosts for evacuation")
            recommendations.append("Enable or add compute hosts before retrying evacuation")

        # Check servers on the failed host
        try:
            servers = connection.servers.list(search_opts={"host": host, "all_tenants": 1})
            server_statuses = {}
            for s in servers:
                server_statuses[s.status] = server_statuses.get(s.status, 0) + 1
            findings.append(f"Servers on {host}: {server_statuses}")

            active = [s for s in servers if s.status == "ACTIVE"]
            if active:
                recommendations.append(f"{len(active)} ACTIVE server(s) still on {host} - evacuation incomplete")
        except Exception as e:
            findings.append(f"Could not list servers on {host}: {e}")

        # Check if host is kdump-fenced
        if hasattr(service, "kdump_fenced_hosts"):
            from ..validation import _extract_hostname
            hostname = _extract_hostname(host)
            if hostname in service.kdump_fenced_hosts:
                findings.append(f"{host} is kdump-fenced (host experienced kernel crash)")
                recommendations.append("Host needs manual intervention after kdump recovery")

        return ToolResult(success=True, data={
            "host": host,
            "findings": findings,
            "recommendations": recommendations,
        })
    except Exception as e:
        return ToolResult(success=False, error=str(e))


@tool("check_cluster_capacity", "Check if the cluster can absorb an evacuation",
      category="diagnostic",
      parameters={"connection": "OpenStackClient", "host": "str (optional)",
                   "service": "InstanceHAService"})
def check_cluster_capacity(connection, service, host: Optional[str] = None) -> ToolResult:
    """Check if the cluster has capacity to absorb an evacuation."""
    try:
        all_services = connection.services.list(binary="nova-compute")
        total = len(all_services)
        up_enabled = [s for s in all_services
                      if s.state == "up" and "enabled" in s.status]

        # If a host is specified, exclude it from available capacity
        if host:
            up_enabled = [s for s in up_enabled if s.host != host]
            servers = connection.servers.list(search_opts={"host": host, "all_tenants": 1})
            server_count = len(servers)
        else:
            server_count = 0

        # Check threshold
        down_count = total - len(up_enabled)
        threshold = service.config.get_config_value("THRESHOLD")
        threshold_pct = (down_count / total * 100) if total > 0 else 0

        data = {
            "total_computes": total,
            "available_targets": len(up_enabled),
            "available_hosts": [s.host for s in up_enabled],
            "servers_to_evacuate": server_count,
            "current_down_percent": round(threshold_pct, 1),
            "threshold_percent": threshold,
            "can_evacuate": threshold_pct <= threshold and len(up_enabled) > 0,
        }

        if not data["can_evacuate"]:
            if len(up_enabled) == 0:
                data["reason"] = "No available target hosts"
            else:
                data["reason"] = f"Down percentage ({threshold_pct:.1f}%) exceeds threshold ({threshold}%)"

        return ToolResult(success=True, data=data)
    except Exception as e:
        return ToolResult(success=False, error=str(e))


@tool("correlate_events", "Correlate fencing, evacuation, and state changes over a time range",
      category="diagnostic",
      parameters={"connection": "OpenStackClient", "service": "InstanceHAService",
                   "minutes": "int"})
def correlate_events(connection, service, minutes: int = 30) -> ToolResult:
    """Correlate events across fencing, evacuation, and service state changes."""
    try:
        events = []
        cutoff = datetime.now() - timedelta(minutes=minutes)

        # Gather service state info
        all_services = connection.services.list(binary="nova-compute")
        for svc in all_services:
            updated = datetime.fromisoformat(svc.updated_at) if svc.updated_at else None
            if updated and updated >= cutoff:
                event = {
                    "type": "service_state",
                    "host": svc.host,
                    "time": svc.updated_at,
                    "state": svc.state,
                    "status": svc.status,
                    "forced_down": svc.forced_down,
                }
                if hasattr(svc, "disabled_reason") and svc.disabled_reason:
                    event["disabled_reason"] = svc.disabled_reason
                events.append(event)

        # Gather migration events
        try:
            query_time = cutoff.isoformat()
            migrations = connection.migrations.list(
                migration_type="evacuation",
                changes_since=query_time,
                limit="100",
            )
            for m in migrations:
                events.append({
                    "type": "evacuation_migration",
                    "time": getattr(m, "updated_at", getattr(m, "created_at", "")),
                    "instance": getattr(m, "instance_uuid", ""),
                    "source": getattr(m, "source_compute", ""),
                    "dest": getattr(m, "dest_compute", ""),
                    "status": getattr(m, "status", ""),
                })
        except Exception as e:
            events.append({"type": "error", "message": f"Could not query migrations: {e}"})

        # Add kdump events
        if hasattr(service, "kdump_hosts_timestamp"):
            cutoff_ts = cutoff.timestamp()
            for host, ts in service.kdump_hosts_timestamp.items():
                if ts >= cutoff_ts:
                    events.append({
                        "type": "kdump",
                        "host": host,
                        "time": datetime.fromtimestamp(ts).isoformat(),
                    })

        # Add processing state
        if hasattr(service, "hosts_processing"):
            for host, ts in service.hosts_processing.items():
                events.append({
                    "type": "processing",
                    "host": host,
                    "started": datetime.fromtimestamp(ts).isoformat(),
                })

        # Sort by time
        events.sort(key=lambda e: e.get("time", ""))

        # Identify patterns
        patterns = _detect_patterns(events)

        return ToolResult(success=True, data={
            "time_range_minutes": minutes,
            "event_count": len(events),
            "events": events,
            "patterns": patterns,
        })
    except Exception as e:
        return ToolResult(success=False, error=str(e))


def _detect_patterns(events: list) -> list:
    """Detect notable patterns in correlated events."""
    patterns = []

    # Check for repeated failures on the same host
    host_failures = {}
    for e in events:
        host = e.get("host", e.get("source", ""))
        if not host:
            continue
        if e.get("type") == "evacuation_migration" and e.get("status") == "error":
            host_failures[host] = host_failures.get(host, 0) + 1
        elif e.get("type") == "service_state" and e.get("state") == "down":
            host_failures[host] = host_failures.get(host, 0) + 1

    for host, count in host_failures.items():
        if count >= 3:
            patterns.append({
                "pattern": "repeated_failure",
                "host": host,
                "count": count,
                "suggestion": f"Host {host} has {count} failure events - may need hardware investigation",
            })

    # Check for cascade (multiple hosts going down close together)
    down_events = [e for e in events
                   if e.get("type") == "service_state" and e.get("state") == "down"]
    if len(down_events) >= 3:
        patterns.append({
            "pattern": "cascade_failure",
            "host_count": len(down_events),
            "suggestion": "Multiple hosts down simultaneously - check shared infrastructure (network, storage, power)",
        })

    return patterns
