"""Structured command parser for the InstanceHA chat interface.

Parses operator commands into tool calls with parameters.
No LLM required -- this is the deterministic command layer.
"""

import shlex
from dataclasses import dataclass, field
from typing import Dict, List, Optional


@dataclass
class ParsedCommand:
    """Result of parsing a chat command."""
    tool_name: str
    parameters: Dict
    raw: str
    help_text: str = ""


@dataclass
class CommandDefinition:
    """Definition of a chat command."""
    name: str
    tool_name: str
    usage: str
    description: str
    aliases: List[str] = field(default_factory=list)
    min_args: int = 0
    max_args: int = 0
    arg_names: List[str] = field(default_factory=list)


# Command definitions mapping chat commands to tool calls
COMMANDS: List[CommandDefinition] = [
    # Read-only commands
    CommandDefinition(
        name="status",
        tool_name="get_compute_services",
        usage="status [host]",
        description="Show compute service status (all or specific host)",
        aliases=["st"],
        max_args=1,
        arg_names=["host"],
    ),
    CommandDefinition(
        name="servers",
        tool_name="get_servers_on_host",
        usage="servers <host>",
        description="List VMs on a host",
        aliases=["vms"],
        min_args=1,
        max_args=1,
        arg_names=["host"],
    ),
    CommandDefinition(
        name="history",
        tool_name="get_evacuation_history",
        usage="history [minutes]",
        description="Show recent evacuation events",
        aliases=["hist"],
        max_args=1,
        arg_names=["minutes"],
    ),
    CommandDefinition(
        name="summary",
        tool_name="get_cluster_summary",
        usage="summary",
        description="Show overall cluster health summary",
        aliases=["health"],
    ),
    CommandDefinition(
        name="migration",
        tool_name="get_migration_status",
        usage="migration <server_id>",
        description="Show migration status for a server",
        aliases=["mig"],
        min_args=1,
        max_args=1,
        arg_names=["server_id"],
    ),
    CommandDefinition(
        name="config",
        tool_name="get_config",
        usage="config",
        description="Show current configuration",
        aliases=["cfg"],
    ),
    CommandDefinition(
        name="kdump",
        tool_name="get_kdump_hosts",
        usage="kdump",
        description="Show hosts with kdump events",
    ),
    # Write commands (require approval)
    CommandDefinition(
        name="fence",
        tool_name="fence_host",
        usage="fence <host> <on|off>",
        description="Fence (power on/off) a host [CRITICAL - requires approval]",
        min_args=2,
        max_args=2,
        arg_names=["host", "action"],
    ),
    CommandDefinition(
        name="evacuate",
        tool_name="evacuate_host",
        usage="evacuate <host> [target_host]",
        description="Evacuate all VMs from a host [CRITICAL - requires approval]",
        aliases=["evac"],
        min_args=1,
        max_args=2,
        arg_names=["host", "target_host"],
    ),
    CommandDefinition(
        name="disable",
        tool_name="disable_host",
        usage="disable <host>",
        description="Force-down a compute service [HIGH - requires approval]",
        min_args=1,
        max_args=1,
        arg_names=["host"],
    ),
    CommandDefinition(
        name="enable",
        tool_name="enable_host",
        usage="enable <host>",
        description="Re-enable a compute service [MEDIUM]",
        min_args=1,
        max_args=1,
        arg_names=["host"],
    ),
    CommandDefinition(
        name="evacuate-server",
        tool_name="evacuate_server",
        usage="evacuate-server <server_id> [target_host]",
        description="Evacuate a single VM [HIGH - requires approval]",
        aliases=["evac-vm"],
        min_args=1,
        max_args=2,
        arg_names=["server_id", "target_host"],
    ),
    # Diagnostic commands
    CommandDefinition(
        name="diagnose",
        tool_name="diagnose_evacuation_failure",
        usage="diagnose <host>",
        description="Analyze why evacuation failed for a host",
        aliases=["diag"],
        min_args=1,
        max_args=1,
        arg_names=["host"],
    ),
    CommandDefinition(
        name="capacity",
        tool_name="check_cluster_capacity",
        usage="capacity [host]",
        description="Check if the cluster can absorb an evacuation",
        aliases=["cap"],
        max_args=1,
        arg_names=["host"],
    ),
    CommandDefinition(
        name="correlate",
        tool_name="correlate_events",
        usage="correlate [minutes]",
        description="Correlate fencing/evacuation/state changes",
        aliases=["corr"],
        max_args=1,
        arg_names=["minutes"],
    ),
    # Observer / AI monitoring commands
    CommandDefinition(
        name="alerts",
        tool_name="_alerts",
        usage="alerts [severity] [minutes]",
        description="Show AI observer alerts (severity: info/warning/critical)",
        max_args=2,
        arg_names=["severity", "minutes"],
    ),
    CommandDefinition(
        name="acknowledge",
        tool_name="_acknowledge",
        usage="acknowledge <index>",
        description="Acknowledge an alert by index",
        aliases=["ack"],
        min_args=1,
        max_args=1,
        arg_names=["index"],
    ),
    CommandDefinition(
        name="cluster-health",
        tool_name="_cluster_health",
        usage="cluster-health",
        description="Show AI-assessed cluster health overview",
        aliases=["chealth"],
    ),
    CommandDefinition(
        name="host-analysis",
        tool_name="_host_analysis",
        usage="host-analysis <host>",
        description="Show AI analysis for a specific host",
        aliases=["hanalysis"],
        min_args=1,
        max_args=1,
        arg_names=["host"],
    ),
    CommandDefinition(
        name="events",
        tool_name="_events",
        usage="events [minutes] [host]",
        description="Show recent event bus events",
        max_args=2,
        arg_names=["minutes", "host"],
    ),
    # Approval management commands (handled directly, not via tools)
    CommandDefinition(
        name="approve",
        tool_name="_approve",
        usage="approve <id>",
        description="Approve a pending action",
        min_args=1,
        max_args=1,
        arg_names=["approval_id"],
    ),
    CommandDefinition(
        name="deny",
        tool_name="_deny",
        usage="deny <id>",
        description="Deny a pending action",
        min_args=1,
        max_args=1,
        arg_names=["approval_id"],
    ),
    CommandDefinition(
        name="pending",
        tool_name="_pending",
        usage="pending",
        description="Show pending approval requests",
    ),
    CommandDefinition(
        name="audit",
        tool_name="_audit",
        usage="audit [count]",
        description="Show recent audit log entries",
        max_args=1,
        arg_names=["count"],
    ),
]

# Build lookup tables
_COMMAND_MAP: Dict[str, CommandDefinition] = {}
for cmd in COMMANDS:
    _COMMAND_MAP[cmd.name] = cmd
    for alias in cmd.aliases:
        _COMMAND_MAP[alias] = cmd


def parse(input_text: str) -> Optional[ParsedCommand]:
    """Parse a chat input string into a ParsedCommand.

    Returns None if the input is empty or unrecognized.
    Raises ValueError for argument count mismatches.
    """
    input_text = input_text.strip()
    if not input_text:
        return None

    try:
        parts = shlex.split(input_text)
    except ValueError:
        parts = input_text.split()

    if not parts:
        return None

    cmd_name = parts[0].lower()
    args = parts[1:]

    # Special: "status <host>" should map to get_service_status instead of get_compute_services
    if cmd_name in ("status", "st") and len(args) == 1:
        return ParsedCommand(
            tool_name="get_service_status",
            parameters={"host": args[0]},
            raw=input_text,
        )

    cmd_def = _COMMAND_MAP.get(cmd_name)
    if cmd_def is None:
        return None

    if len(args) < cmd_def.min_args:
        raise ValueError(
            f"Too few arguments for '{cmd_def.name}'. Usage: {cmd_def.usage}"
        )
    if len(args) > cmd_def.max_args:
        raise ValueError(
            f"Too many arguments for '{cmd_def.name}'. Usage: {cmd_def.usage}"
        )

    # Build parameters dict from positional args
    parameters = {}
    for i, arg_value in enumerate(args):
        if i < len(cmd_def.arg_names):
            key = cmd_def.arg_names[i]
            # Auto-convert numeric arguments
            if key in ("minutes", "count"):
                try:
                    arg_value = int(arg_value)
                except ValueError:
                    raise ValueError(f"'{key}' must be a number, got '{arg_value}'")
            parameters[key] = arg_value

    return ParsedCommand(
        tool_name=cmd_def.tool_name,
        parameters=parameters,
        raw=input_text,
    )


def get_help_text() -> str:
    """Generate help text listing all available commands."""
    lines = ["Available commands:", ""]

    # Group by category
    categories = {
        "Read-Only": [],
        "Write (approval required)": [],
        "Diagnostic": [],
        "AI Monitoring": [],
        "Approval Management": [],
    }

    write_tools = {"fence_host", "evacuate_host", "disable_host", "enable_host", "evacuate_server"}
    diag_tools = {"diagnose_evacuation_failure", "check_cluster_capacity", "correlate_events"}
    observer_tools = {"_alerts", "_acknowledge", "_cluster_health", "_host_analysis", "_events"}
    mgmt_tools = {"_approve", "_deny", "_pending", "_audit"}

    for cmd in COMMANDS:
        if cmd.tool_name in mgmt_tools:
            categories["Approval Management"].append(cmd)
        elif cmd.tool_name in observer_tools:
            categories["AI Monitoring"].append(cmd)
        elif cmd.tool_name in write_tools:
            categories["Write (approval required)"].append(cmd)
        elif cmd.tool_name in diag_tools:
            categories["Diagnostic"].append(cmd)
        else:
            categories["Read-Only"].append(cmd)

    for category, cmds in categories.items():
        if not cmds:
            continue
        lines.append(f"  {category}:")
        for cmd in cmds:
            alias_str = f" (alias: {', '.join(cmd.aliases)})" if cmd.aliases else ""
            lines.append(f"    {cmd.usage:<35} {cmd.description}{alias_str}")
        lines.append("")

    lines.append("Type 'help' for this message, 'quit' or 'exit' to disconnect.")
    return "\n".join(lines)
