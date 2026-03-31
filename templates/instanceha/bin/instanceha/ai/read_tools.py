"""Read-only tools for cluster introspection. No approval required."""

import logging
from datetime import datetime, timedelta
from typing import Optional

from .tools import ApprovalLevel, ToolResult, tool


@tool("get_compute_services", "List all Nova compute services with status",
      category="read-only",
      parameters={"connection": "OpenStackClient", "service": "InstanceHAService"})
def get_compute_services(connection, service) -> ToolResult:
    """List all compute services with their status."""
    try:
        services = connection.services.list(binary="nova-compute")
        data = []
        for svc in services:
            data.append({
                "host": svc.host,
                "state": svc.state,
                "status": svc.status,
                "forced_down": svc.forced_down,
                "disabled_reason": getattr(svc, "disabled_reason", ""),
                "updated_at": svc.updated_at,
                "zone": getattr(svc, "zone", ""),
            })
        return ToolResult(success=True, data=data)
    except Exception as e:
        return ToolResult(success=False, error=str(e))


@tool("get_service_status", "Get detailed status of a specific compute host",
      category="read-only",
      parameters={"connection": "OpenStackClient", "host": "str"})
def get_service_status(connection, host: str) -> ToolResult:
    """Get detailed status for a specific host."""
    try:
        services = connection.services.list(host=host, binary="nova-compute")
        if not services:
            return ToolResult(success=False, error=f"No compute service found for {host}")

        svc = services[0]
        servers = connection.servers.list(search_opts={"host": host, "all_tenants": 1})

        data = {
            "host": svc.host,
            "state": svc.state,
            "status": svc.status,
            "forced_down": svc.forced_down,
            "disabled_reason": getattr(svc, "disabled_reason", ""),
            "updated_at": svc.updated_at,
            "zone": getattr(svc, "zone", ""),
            "server_count": len(servers),
            "servers": [
                {"id": s.id, "name": s.name, "status": s.status}
                for s in servers[:50]  # Cap at 50 to avoid overwhelming output
            ],
        }
        return ToolResult(success=True, data=data)
    except Exception as e:
        return ToolResult(success=False, error=str(e))


@tool("get_servers_on_host", "List all VMs on a specific host",
      category="read-only",
      parameters={"connection": "OpenStackClient", "host": "str"})
def get_servers_on_host(connection, host: str) -> ToolResult:
    """List all servers on a host with their status."""
    try:
        servers = connection.servers.list(search_opts={"host": host, "all_tenants": 1})
        data = [
            {
                "id": s.id,
                "name": s.name,
                "status": s.status,
                "flavor": getattr(s, "flavor", {}).get("id", "unknown") if isinstance(getattr(s, "flavor", None), dict) else "unknown",
                "image": _get_image_id(s),
            }
            for s in servers
        ]
        return ToolResult(success=True, data=data)
    except Exception as e:
        return ToolResult(success=False, error=str(e))


@tool("get_evacuation_history", "Get recent evacuation events from migration history",
      category="read-only",
      parameters={"connection": "OpenStackClient", "minutes": "int"})
def get_evacuation_history(connection, minutes: int = 60) -> ToolResult:
    """Get recent evacuation migrations."""
    try:
        query_time = (datetime.now() - timedelta(minutes=minutes)).isoformat()
        migrations = connection.migrations.list(
            migration_type="evacuation",
            changes_since=query_time,
            limit="100",
        )
        data = []
        for m in migrations:
            data.append({
                "id": getattr(m, "id", None),
                "instance_uuid": getattr(m, "instance_uuid", None),
                "source_compute": getattr(m, "source_compute", None),
                "dest_compute": getattr(m, "dest_compute", None),
                "status": getattr(m, "status", None),
                "created_at": getattr(m, "created_at", None),
                "updated_at": getattr(m, "updated_at", None),
            })
        return ToolResult(success=True, data=data)
    except Exception as e:
        return ToolResult(success=False, error=str(e))


@tool("get_cluster_summary", "Get overall cluster health summary",
      category="read-only",
      parameters={"connection": "OpenStackClient", "service": "InstanceHAService"})
def get_cluster_summary(connection, service) -> ToolResult:
    """Produce a cluster health summary."""
    try:
        services = connection.services.list(binary="nova-compute")
        total = len(services)
        up = sum(1 for s in services if s.state == "up" and "enabled" in s.status)
        down = sum(1 for s in services if s.state == "down")
        disabled = sum(1 for s in services if "disabled" in s.status)
        forced_down = sum(1 for s in services if s.forced_down)
        evacuating = sum(1 for s in services
                         if s.forced_down and "evacuation" in getattr(s, "disabled_reason", "")
                         and "complete" not in getattr(s, "disabled_reason", ""))

        # Count kdump hosts
        kdump_count = len(service.kdump_fenced_hosts) if hasattr(service, "kdump_fenced_hosts") else 0
        processing_count = len(service.hosts_processing) if hasattr(service, "hosts_processing") else 0

        data = {
            "total_computes": total,
            "up": up,
            "down": down,
            "disabled": disabled,
            "forced_down": forced_down,
            "actively_evacuating": evacuating,
            "kdump_fenced": kdump_count,
            "being_processed": processing_count,
            "health": "healthy" if down == 0 else ("degraded" if down <= total * 0.1 else "critical"),
        }
        return ToolResult(success=True, data=data)
    except Exception as e:
        return ToolResult(success=False, error=str(e))


@tool("get_migration_status", "Check migration progress for a specific server",
      category="read-only",
      parameters={"connection": "OpenStackClient", "server_id": "str"})
def get_migration_status(connection, server_id: str) -> ToolResult:
    """Check migration status for a server."""
    try:
        query_time = (datetime.now() - timedelta(minutes=60)).isoformat()
        migrations = connection.migrations.list(
            instance_uuid=server_id,
            migration_type="evacuation",
            changes_since=query_time,
            limit="10",
        )
        if not migrations:
            return ToolResult(success=True, data={"status": "no_recent_migrations"})

        latest = migrations[0]
        data = {
            "status": getattr(latest, "status", "unknown"),
            "source": getattr(latest, "source_compute", None),
            "dest": getattr(latest, "dest_compute", None),
            "created_at": getattr(latest, "created_at", None),
            "updated_at": getattr(latest, "updated_at", None),
        }
        return ToolResult(success=True, data=data)
    except Exception as e:
        return ToolResult(success=False, error=str(e))


@tool("get_config", "Get current InstanceHA configuration values",
      category="read-only",
      parameters={"service": "InstanceHAService"})
def get_config(service) -> ToolResult:
    """Return current configuration values."""
    try:
        config_map = service.config._config_map
        data = {}
        for key, item in config_map.items():
            val = service.config.get_config_value(key)
            data[key] = {"value": val, "type": item.type}
        return ToolResult(success=True, data=data)
    except Exception as e:
        return ToolResult(success=False, error=str(e))


@tool("get_kdump_hosts", "Get hosts with recent kdump events",
      category="read-only",
      parameters={"service": "InstanceHAService"})
def get_kdump_hosts(service) -> ToolResult:
    """Return hosts with kdump activity."""
    try:
        data = {
            "kdump_fenced": list(service.kdump_fenced_hosts),
            "kdump_timestamps": {
                host: datetime.fromtimestamp(ts).isoformat()
                for host, ts in service.kdump_hosts_timestamp.items()
                if ts > 0
            },
            "kdump_checking": {
                host: datetime.fromtimestamp(ts).isoformat()
                for host, ts in service.kdump_hosts_checking.items()
                if ts > 0
            },
        }
        return ToolResult(success=True, data=data)
    except Exception as e:
        return ToolResult(success=False, error=str(e))


def _get_image_id(server) -> str:
    """Extract image ID from server object."""
    try:
        image = getattr(server, "image", None)
        if isinstance(image, dict):
            return image.get("id", "unknown")
        if isinstance(image, str):
            return image
        return getattr(image, "id", "unknown") if image else "none"
    except (AttributeError, TypeError):
        return "unknown"
