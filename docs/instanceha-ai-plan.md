# AI-Augmented InstanceHA - Implementation Plan

## Status

| Phase | Description | Status | Tests |
|-------|-------------|--------|-------|
| Phase 1 | Refactor instanceha.py into a package | COMPLETE | 11/11 suites |
| Phase 2 | Tool abstraction layer (tools, safety, approval, audit) | COMPLETE | 12/12 suites |
| Phase 3 | Chat interface (structured commands over Unix socket) | COMPLETE | 13/13 suites |
| Phase 4 | LLM integration (local + remote model support) | COMPLETE | 14/14 suites |
| Phase 5 | Intelligent monitoring (event bus, pattern detection) | COMPLETE | 15/15 suites |
| Phase 6 | MCP server (Model Context Protocol interface) | COMPLETE | 16/16 suites |

**Current test count**: 16 test suites (28 new tests for Phase 6), all passing. Go build succeeds.

**CRD changes** (AIConfig/AIStatus types, webhook validation): NOT YET DONE. These were planned as part of Phase 4 but deferred -- the Python-side LLM integration is complete and functional without them. The AI layer currently reads config from environment variables. CRD changes should be done before merging to main.

---

## Context

InstanceHA currently uses a purely rule-based Python control loop (~2860 lines in `instanceha.py`) that polls Nova every 45s, detects down compute services, fences hosts, and evacuates VMs. While effective, it lacks the ability to reason about complex failure scenarios, diagnose root causes, or let operators interact conversationally. This plan introduces an AI layer that augments (not replaces) the existing logic, plus a natural language chat interface accessible via `oc rsh`.

---

## Architecture Overview

```
┌─────────────────────────────────────────────────────┐
│  InstanceHA Pod                                      │
│                                                      │
│  ┌──────────────┐     ┌───────────────────────────┐ │
│  │ Control Loop  │────▶│ Event Bus (in-process)    │ │
│  │ (existing)    │     │  - service state changes  │ │
│  │ Poll → Detect │     │  - fencing events         │ │
│  │ → Fence →     │     │  - evacuation results     │ │
│  │ Evacuate →    │     │  - kdump notifications    │ │
│  │ Re-enable     │     └──────────┬────────────────┘ │
│  └──────────────┘                 │                  │
│                                   ▼                  │
│  ┌──────────────────────────────────────────────────┐│
│  │ AI Agent Layer                                    ││
│  │  ┌─────────┐  ┌───────┐  ┌────────────────┐     ││
│  │  │ LLM     │  │ Tools │  │ Safety/Approval│     ││
│  │  │ Engine  │  │ (R/W) │  │ + Audit Log    │     ││
│  │  └─────────┘  └───────┘  └────────────────┘     ││
│  └──────────────────────┬───────────────────────────┘│
│                         │                            │
│  ┌──────────────────────▼───────────────────────┐   │
│  │ Unix Domain Socket (/var/run/instanceha/)     │   │
│  └──────────────────────┬───────────────────────┘   │
│                         │                            │
│  ┌──────────────────────▼───────────────────────┐   │
│  │ Chat CLI (instanceha-chat)                    │   │
│  │ Accessed via: oc rsh <pod> instanceha-chat    │   │
│  └──────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────┘
```

**Key principle**: The existing control loop runs unmodified as the primary safety mechanism. The AI is a parallel capability -- observer, advisor, and operator-supervised actor.

---

## File Inventory

### Python package: `templates/instanceha/bin/instanceha/`

| File | Phase | Description |
|------|-------|-------------|
| `__init__.py` | 1 | Package init, imports from all submodules, re-exports for `_pkg` pattern |
| `main.py` | 1, 3, 5 | Entry point, main loop, chat server startup, observer startup, event publishing |
| `config.py` | 1 | ConfigManager class |
| `service.py` | 1 | InstanceHAService class |
| `nova.py` | 1 | Nova API connection management |
| `evacuation.py` | 1 | Evacuation logic (smart + traditional) |
| `fencing.py` | 1 | IPMI, Redfish, BMH fencing agents |
| `monitoring.py` | 1 | Service categorization, health check, kdump |
| `reserved_hosts.py` | 1 | Reserved host pool management |
| `validation.py` | 1 | Input validation, credential sanitization |
| `models.py` | 1 | Dataclasses and enums |

### AI subpackage: `templates/instanceha/bin/instanceha/ai/`

| File | Phase | Description |
|------|-------|-------------|
| `__init__.py` | 2 | Package init, triggers @tool decorator registration |
| `tools.py` | 2 | ToolRegistry, @tool decorator, ApprovalLevel enum, ToolResult |
| `safety.py` | 2, 3 | ApprovalManager, AuditLogger, RateLimiter. `execute_tool()` supports `inject_kwargs` for separating serializable params from live objects |
| `read_tools.py` | 2 | 8 read-only tools (get_compute_services, get_service_status, etc.) |
| `write_tools.py` | 2 | 5 write tools (fence_host, evacuate_host, etc.) with approval levels |
| `diagnostic_tools.py` | 2 | 3 diagnostic tools (diagnose_evacuation_failure, check_cluster_capacity, correlate_events) |
| `command_parser.py` | 3, 5 | Parses structured commands into tool calls. 23 commands with aliases (18 original + 5 observer commands). |
| `chat_server.py` | 3, 4, 5 | Unix socket server at `/var/run/instanceha/agent.sock`. Falls back to LLM agent when structured parsing fails. Handles observer commands (alerts, ack, health, analysis, events). |
| `chat_client.py` | 3 | Interactive REPL client with readline, colored output |
| `engine.py` | 4 | LLM engine abstraction: `LocalEngine` (llama-cpp-python/GGUF), `RemoteEngine` (OpenAI-compatible API), `create_engine()` factory |
| `agent.py` | 4 | Tool-calling agent loop: query → context → LLM → tool call → result → LLM → final answer. Max 5 iterations. Stops on approval-required. |
| `prompts.py` | 4 | System prompt template with safety rules, dynamic tool schema injection, context template |
| `context.py` | 4 | Cluster state summarizer: `summarize_cluster()`, `summarize_recent_events()`, `build_context()` |
| `event_bus.py` | 5 | In-process pub/sub event bus with bounded history, EventType enum, thread-safe |
| `observer.py` | 5 | AI observer: pattern detection (repeated fencing, oscillation, cascade, evacuation failure, threshold, kdump), alerts with deduplication, host analysis, cluster health |
| `mcp_server.py` | 6 | MCP server wrapping ToolRegistry tools + Observer resources. Streamable-http transport on port 8081. Read-only by default, write access configurable. |

### Go files modified:

| File | Changes |
|------|---------|
| `internal/instanceha/funcs.go` | ConfigMap items for all Python modules (13 AI items), emptyDir volumes for `/var/run/instanceha` and `/var/log/instanceha` |
| `internal/controller/instanceha/instanceha_controller.go` | AdditionalTemplate entries for all Python modules (16 AI entries) |

### Test files:

| File | Phase | Test count |
|------|-------|------------|
| `test/instanceha/test_unit_core.py` | 1 | (existing) |
| `test/instanceha/test_fencing_agents.py` | 1 | (existing) |
| `test/instanceha/test_kdump_detection.py` | 1 | (existing) |
| `test/instanceha/test_security_validation.py` | 1 | (existing) |
| `test/instanceha/test_critical_error_paths.py` | 1 | (existing) |
| `test/instanceha/test_evacuation_workflow.py` | 1 | (existing) |
| `test/instanceha/test_config_features.py` | 1 | (existing) |
| `test/instanceha/test_helper_functions.py` | 1 | (existing) |
| `test/instanceha/functional_test.py` | 1 | (existing) |
| `test/instanceha/integration_test.py` | 1 | (existing) |
| `test/instanceha/test_region_isolation.py` | 1 | (existing) |
| `test/instanceha/test_ai_tools.py` | 2 | 49 tests |
| `test/instanceha/test_chat_interface.py` | 3 | 59 tests |
| `test/instanceha/test_llm_integration.py` | 4 | 39 tests |
| `test/instanceha/test_intelligent_monitoring.py` | 5 | 60 tests |
| `test/instanceha/test_mcp_server.py` | 6 | 28 tests |

### Other files:

| File | Description |
|------|-------------|
| `Containerfile.instanceha-ai` | CentOS Stream 9 based container with llama-cpp-python, novaclient, keystoneauth1 |
| `test/instanceha/run_tests.sh` | Test runner, 16 suites |

---

## Phase 1: Refactor instanceha.py into a Package (COMPLETE)

**Goal**: Decompose the monolith into testable modules. No behavior change.

### Key implementation detail:
All submodules use the `_pkg = sys.modules[__package__]` pattern so that `mock.patch('instanceha.X')` in tests correctly intercepts cross-module function calls. This includes exception classes in `except` clauses (e.g., `except (_pkg.DiscoveryFailure, _pkg.Unauthorized)`).

---

## Phase 2: Tool Abstraction Layer (COMPLETE)

**Goal**: Wrap existing functions as structured, auditable, approval-gated tools.

### Safety architecture:
- **ApprovalLevel hierarchy**: NONE < MEDIUM < HIGH < CRITICAL
- **CRITICAL always requires approval** regardless of auto_approve setting
- **Dry-run by default** for write operations
- **Rate limiting**: 3 CRITICAL/min, 5 HIGH/min, 10 MEDIUM/min
- **Audit log**: JSON-lines at `/var/log/instanceha/ai-audit.log` with parameter sanitization
- **`inject_kwargs`**: Connection/service objects are injected at execution time, not stored in approval requests (they are not JSON-serializable)

---

## Phase 3: Chat Interface (COMPLETE)

**Goal**: Working CLI chat over Unix domain socket, initially with structured commands.

### Access:
```
oc rsh <pod> python3 /var/lib/instanceha/scripts/instanceha/ai/chat_client.py
```

### Protocol: JSON-lines over Unix socket at `/var/run/instanceha/agent.sock`

### Supported commands (18 total, with aliases):
- **Read-only**: `status [host]`, `servers <host>`, `history [min]`, `summary`, `migration <id>`, `config`, `kdump`
- **Write**: `fence <host> <on|off>`, `evacuate <host> [target]`, `disable <host>`, `enable <host>`, `evacuate-server <id> [target]`
- **Diagnostic**: `diagnose <host>`, `capacity [host]`, `correlate [min]`
- **Management**: `approve <id>`, `deny <id>`, `pending`, `audit [count]`

### Integration with main loop:
- `_start_chat_server()` called from `main()` after service initialization
- Socket server runs as a daemon thread
- Each client gets its own handler thread
- All tool calls go through the shared `ApprovalManager` and `AuditLogger`

---

## Phase 4: LLM Integration (COMPLETE)

**Goal**: Natural language understanding via local or remote LLM.

### LLM backends:

**LocalEngine** (air-gap compatible):
- Uses `llama-cpp-python` with GGUF model files
- Model delivered via PVC mount
- Configurable context window (`n_ctx`) and thread count (`n_threads`)
- Uses `chatml-function-calling` chat format for tool calls

**RemoteEngine**:
- Any OpenAI-compatible API (vLLM, Ollama, LiteLLM, etc.)
- Uses `urllib` (no external HTTP library dependency)
- Configurable endpoint, API key, model name, timeout

**Factory**: `create_engine(config)` creates the appropriate backend from config dict.

### Agent loop:
1. Build system prompt with tool schemas (filtered: connection/service params hidden from LLM)
2. Build cluster context (service states, recent events, kdump, processing)
3. Send user query + context + tool specs to LLM
4. If LLM returns tool call: execute through approval manager, feed result back
5. Repeat until text response or max iterations (5) or approval-required
6. Maintains conversation history (last 10 turns per session)

### Chat server integration:
- When structured command parsing returns `None`, falls back to LLM agent if available
- Each `ChatSession` creates its own `Agent` instance with independent conversation history
- `ChatServer` accepts an optional `llm_engine` parameter

### Container image:
`Containerfile.instanceha-ai` at repo root, based on CentOS Stream 9, includes:
- python3, pip, ipmitool, openssh-clients
- python-novaclient, keystoneauth1, python-openstackclient, PyYAML
- llama-cpp-python (CPU-only, built with `CMAKE_ARGS="-DGGML_NATIVE=OFF"`)

### NOT YET DONE (deferred):
- CRD changes (`AIConfig`/`AIStatus` types in `apis/instanceha/v1beta1/instanceha_types.go`)
- Webhook validation for new fields
- Controller watching for AIConfig resources
- PVC volume mount for local model files
- Environment variable passthrough for AI config

---

## Phase 5: Intelligent Monitoring (COMPLETE)

**Goal**: AI observes the control loop and provides proactive insights.

### Event Bus (`event_bus.py`)
Lightweight in-process pub/sub with bounded history (deque, maxlen=1000):
- **EventType** enum: SERVICE_STATE_CHANGE, FENCE_START/RESULT, EVACUATION_START/RESULT, HOST_DISABLED/ENABLED, KDUMP_DETECTED, THRESHOLD_EXCEEDED, PROCESSING_START/COMPLETE
- **EventBus**: thread-safe subscribe/subscribe_all/publish/unsubscribe, get_events() with filters (minutes, event_type, host)
- Module-level singleton via `get_event_bus()` / `set_event_bus()`
- All event publishing is best-effort (try/except pass) -- never affects the control loop

### Observer (`observer.py`)
Subscribes to all events via dispatch table, detects patterns, generates alerts:

| Pattern | Threshold | Severity | Description |
|---------|-----------|----------|-------------|
| repeated_fencing | 3 in 30min | warning | Same host fenced repeatedly |
| oscillating_host | 4 transitions in 60min | warning | Host flapping up/down |
| cascade_failure | 3 hosts in 5min | critical | Multiple hosts failing together |
| repeated_evacuation_failure | 2 in 30min | critical | Evacuation keeps failing |
| frequent_threshold_block | 3 in 60min | warning | Evacuation blocked by threshold |
| kdump_detected | any | info | Kernel crash dump on host |

Features: alert deduplication (same pattern+host within 5min), acknowledgment, host analysis, cluster health assessment (healthy/warning/critical).

### Instrumented files
- **`fencing.py`**: `_publish_fence_event()` -- FENCE_START/FENCE_RESULT on every fence call
- **`evacuation.py`**: `_publish_evacuation_event()` -- EVACUATION_START/RESULT; `_publish_host_state_event()` -- HOST_DISABLED/ENABLED
- **`monitoring.py`**: `_publish_kdump_event()` -- KDUMP_DETECTED on kdump UDP message
- **`main.py`**: `_publish_service_state_event()` -- SERVICE_STATE_CHANGE for down hosts; `_publish_threshold_event()` -- THRESHOLD_EXCEEDED when evacuation blocked; `_start_observer()` starts observer and stores reference in `ai._observer`

### Chat commands added (5 new commands):
- `alerts [severity] [minutes]` -- show AI observer alerts
- `acknowledge <index>` (alias: `ack`) -- acknowledge an alert
- `cluster-health` (alias: `chealth`) -- AI-assessed cluster health overview
- `host-analysis <host>` (alias: `hanalysis`) -- per-host AI analysis
- `events [minutes] [host]` -- show raw event bus events

---

## Phase 6: MCP Server (COMPLETE)

**Goal**: Expose InstanceHA tools and observer data via the Model Context Protocol, enabling external MCP clients (Claude Desktop, Cursor, etc.) to interact with the cluster.

### Architecture

The MCP server is an *additional* interface alongside the existing Unix socket chat server. It reuses the same ToolRegistry, ApprovalManager, and AuditLogger.

```
External MCP clients ──▶ streamable-http :8081 ──▶ InstanceHAMCPServer
                                                      │
                                                      ├── MCP Tools (from ToolRegistry)
                                                      │     └── inject_kwargs for connection/service
                                                      ├── MCP Resources (from Observer/EventBus)
                                                      └── ApprovalManager (same safety layer)
```

### MCP Tools

All tools from the ToolRegistry are auto-registered as MCP tools. Parameters `connection` and `service` are filtered out (injected at execution time, not exposed to clients).

- **Read-only tools**: Always available (11 tools)
- **Write tools**: Only registered when `allow_writes=True` (5 tools). Currently disabled by default.
- Write tools go through the ApprovalManager -- CRITICAL operations always require interactive approval.

### MCP Resources

| URI | Description |
|-----|-------------|
| `instanceha://health` | Cluster health assessment from AI observer |
| `instanceha://alerts` | Unacknowledged alerts |
| `instanceha://alerts/all` | All alerts (including acknowledged) |
| `instanceha://events` | Recent events from event bus (last 60 min) |
| `instanceha://host/{host}` | Per-host AI analysis |

### Transport & Configuration

- **Transport**: Streamable HTTP on port 8081 (standard MCP remote transport)
- **SDK**: `mcp>=1.25,<2` (Python MCP SDK with FastMCP)
- **Graceful degradation**: If the `mcp` package is not installed, the server simply doesn't start. No impact on existing functionality.
- **Container port**: 8081/TCP exposed in Deployment spec

### Key Design Decisions

1. **No replacement of existing tools**: The MCP server wraps the same ToolRegistry, not a separate implementation.
2. **Read-only by default**: `allow_writes=False` prevents any state-modifying operations through MCP.
3. **Same safety layer**: All MCP tool calls go through ApprovalManager + AuditLogger.
4. **Optional dependency**: The `mcp` package is only needed for the MCP server. All other InstanceHA features work without it.

---

## Safety Architecture

| Level | Actions | Default Behavior |
|-------|---------|-----------------|
| CRITICAL | fence, evacuate host | Always requires interactive approval |
| HIGH | disable host, evacuate VM | Approval required, configurable |
| MEDIUM | enable host | Auto-approved, configurable |
| READ-ONLY | all queries | No approval needed |

**Dry-run by default**: Write operations show what would happen before executing.

**Audit log**: All AI actions logged to `/var/log/instanceha/ai-audit.log` with timestamp, query, reasoning, tool called, approval status, result.

**Rate limiting**: Max 3 fence ops/min, max 5 disable ops/min, max 10 enable ops/min (configurable).

---

## Deferred CRD Changes (for Phase 4/5 completion)

```go
// In InstanceHaSpec:
AIConfig *AIConfig `json:"aiConfig,omitempty"`

type AIConfig struct {
    Enabled        string `json:"enabled"`           // "True"/"False", default "False"
    ModelConfigMap string `json:"modelConfigMap,omitempty"`
    ModelPVC       string `json:"modelPVC,omitempty"`
    Endpoint       string `json:"endpoint,omitempty"`
    EndpointSecret string `json:"endpointSecret,omitempty"`
    AutoApprove    string `json:"autoApprove,omitempty"`  // "True"/"False"
}

// In InstanceHaStatus:
AIStatus *AIStatus `json:"aiStatus,omitempty"`

type AIStatus struct {
    ModelLoaded   bool   `json:"modelLoaded,omitempty"`
    ModelName     string `json:"modelName,omitempty"`
    LastQueryTime string `json:"lastQueryTime,omitempty"`
}
```

### Files to modify:
- `apis/instanceha/v1beta1/instanceha_types.go` - add AIConfig/AIStatus
- `internal/controller/instanceha/instanceha_controller.go` - watch new resources, pass AI config to pod env
- `internal/instanceha/funcs.go` - PVC volume mount for model files, AI env vars
- `internal/webhook/instanceha/v1beta1/` - validate new fields

---

## Risks & Mitigations

| Risk | Mitigation |
|------|-----------|
| LLM hallucination -> wrong action | Approval workflow; dry-run default; audit log |
| Resource exhaustion from local model | AI is opt-in; resource limits; model loaded only when enabled |
| Air-gap model delivery | GGUF via PVC; no network download |
| Chat as attack vector | Unix socket only (no network); pod exec requires K8s RBAC |
| Backward compatibility | AI entirely additive; existing behavior unchanged when disabled |
| Non-serializable objects in approval | inject_kwargs pattern separates live objects from logged params |

---

## Verification Plan

1. **Phase 1**: Run existing `make test-instanceha` tests -- all must pass with refactored package ✅
2. **Phase 2**: Unit tests for each tool wrapper; verify approval gates block without confirmation ✅
3. **Phase 3**: Manual test via `oc rsh` into pod; verify structured commands work ✅ (unit tests pass)
4. **Phase 4**: Integration test with small local model; verify tool-calling works end-to-end ✅ (unit tests with MockEngine pass)
5. **Phase 5**: 60 unit tests covering event bus, observer patterns, alert dedup/ack, chat commands, publishing helpers ✅
6. **Phase 6**: 28 unit tests covering MCP server construction, tool registration/filtering, resource registration, write access control, graceful degradation ✅
