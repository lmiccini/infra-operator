"""Tool-calling agent loop for InstanceHA AI.

Orchestrates the conversation between the operator, the LLM, and the tool
registry.  The agent:

  1. Receives the operator's natural-language query.
  2. Builds a context-enriched prompt with cluster state.
  3. Sends it to the LLM engine.
  4. If the LLM returns a tool call, executes it through the approval
     manager and feeds the result back to the LLM.
  5. Repeats until the LLM produces a final text response (no more tool
     calls) or a hard iteration limit is reached.
"""

import json
import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from .context import build_context
from .engine import LLMEngine, LLMResponse
from .prompts import build_system_prompt
from .safety import ApprovalManager
from .tools import ToolResult, registry as tool_registry


MAX_TOOL_ITERATIONS = 5


@dataclass
class AgentResponse:
    """Final response from the agent to the operator."""
    message: str
    tool_calls_made: List[Dict] = field(default_factory=list)
    requires_approval: bool = False
    pending_approval_id: str = ""
    pending_tool: str = ""
    pending_params: Dict = field(default_factory=dict)


class Agent:
    """Tool-calling AI agent for InstanceHA.

    Maintains a conversation history and manages the LLM <-> tools loop.
    Each chat session should have its own Agent instance.
    """

    def __init__(self, engine: LLMEngine, approval_manager: ApprovalManager,
                 nova_connection, service,
                 max_iterations: int = MAX_TOOL_ITERATIONS):
        self._engine = engine
        self._approval_manager = approval_manager
        self._nova_connection = nova_connection
        self._service = service
        self._max_iterations = max_iterations
        self._history: List[Dict] = []

    def query(self, user_message: str) -> AgentResponse:
        """Process a natural-language query from the operator.

        Returns an AgentResponse with the final text and any tool calls made.
        """
        # Build system prompt with tool schemas
        schemas = tool_registry.get_schemas()
        system_prompt = build_system_prompt(schemas)

        # Build context with current cluster state
        try:
            context = build_context(self._nova_connection, self._service)
        except Exception as e:
            logging.warning("Failed to build cluster context: %s", e)
            context = "(cluster context unavailable)"

        # Assemble messages
        messages = [
            {"role": "system", "content": system_prompt + "\n\n" + context},
        ]
        # Include recent conversation history (keep last 10 turns)
        messages.extend(self._history[-10:])
        messages.append({"role": "user", "content": user_message})

        # Build tool schemas for the LLM
        tools = self._build_tool_specs(schemas)

        tool_calls_made = []

        for iteration in range(self._max_iterations):
            response = self._engine.chat(messages, tools=tools)

            if response.finish_reason == "error":
                return AgentResponse(message=response.content)

            if not response.has_tool_calls:
                # Final text response
                self._history.append({"role": "user", "content": user_message})
                self._history.append({"role": "assistant", "content": response.content})
                return AgentResponse(
                    message=response.content,
                    tool_calls_made=tool_calls_made,
                )

            # Process tool calls (one at a time)
            tc = response.tool_calls[0]

            # Add assistant message with tool call
            messages.append({
                "role": "assistant",
                "content": response.content,
                "tool_calls": [{"id": tc.call_id, "function": {
                    "name": tc.name, "arguments": json.dumps(tc.arguments)
                }}],
            })

            # Execute the tool
            result = self._execute_tool_call(tc.name, tc.arguments)

            if result.error == "approval_required":
                # Stop the loop -- operator must approve
                self._history.append({"role": "user", "content": user_message})
                approval_id = result.data.get("approval_id", "")
                level = result.data.get("approval_level", "")
                return AgentResponse(
                    message=(
                        f"{response.content}\n\n"
                        f"This action requires approval (level={level}).\n"
                        f"Approval ID: {approval_id}\n"
                        f"Type 'approve {approval_id}' to proceed or "
                        f"'deny {approval_id}' to cancel."
                    ),
                    tool_calls_made=tool_calls_made,
                    requires_approval=True,
                    pending_approval_id=approval_id,
                    pending_tool=tc.name,
                    pending_params=tc.arguments,
                )

            # Feed result back to LLM
            tool_calls_made.append({
                "tool": tc.name,
                "arguments": tc.arguments,
                "success": result.success,
            })

            messages.append({
                "role": "tool",
                "tool_call_id": tc.call_id,
                "content": json.dumps(result.to_dict()),
            })

        # Exhausted iterations
        self._history.append({"role": "user", "content": user_message})
        return AgentResponse(
            message="Reached maximum tool call iterations. Please try a simpler query.",
            tool_calls_made=tool_calls_made,
        )

    def _execute_tool_call(self, tool_name: str, arguments: Dict) -> ToolResult:
        """Execute a tool call, injecting connection/service as needed."""
        tool_obj = tool_registry.get(tool_name)
        if tool_obj is None:
            return ToolResult(success=False, error=f"Unknown tool: {tool_name}")

        # Inject connection/service
        inject = {}
        expected = tool_obj.parameters or {}
        if "connection" in expected:
            inject["connection"] = self._nova_connection
        if "service" in expected:
            inject["service"] = self._service

        return self._approval_manager.execute_tool(
            tool_registry, tool_name, arguments,
            user="ai-agent", inject_kwargs=inject,
        )

    def _build_tool_specs(self, schemas: List[Dict]) -> List[Dict]:
        """Convert tool schemas to OpenAI function-calling format."""
        tools = []
        for schema in schemas:
            # Filter out injected params
            params = {k: v for k, v in schema.get("parameters", {}).items()
                      if k not in ("connection", "service")}

            # Build JSON Schema properties
            properties = {}
            required = []
            for pname, ptype in params.items():
                prop = {"type": "string", "description": ptype}
                if "optional" not in ptype.lower():
                    required.append(pname)
                properties[pname] = prop

            tools.append({
                "type": "function",
                "function": {
                    "name": schema["name"],
                    "description": schema["description"],
                    "parameters": {
                        "type": "object",
                        "properties": properties,
                        "required": required,
                    },
                },
            })
        return tools

    def clear_history(self):
        """Clear conversation history."""
        self._history.clear()
