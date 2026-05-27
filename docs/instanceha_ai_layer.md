# InstanceHA AI Layer

## Overview

The InstanceHA AI layer (`instanceha_ai`) adds intelligent monitoring, a chat
interface, LLM-assisted diagnostics, and an MCP server on top of the existing
InstanceHA monolith.  It is deployed as a separate Python package baked into the
`instanceha-ai` container image while the monolith (`instanceha.py`) remains
unchanged and continues to be mounted via ConfigMap.

### Key capabilities

- **Event bus** -- in-process pub/sub that captures fencing, evacuation, and
  state-change events emitted by the monolith.
- **Observer** -- pattern detection for cascade failures, oscillating hosts,
  repeated fencing, and evacuation failures, with configurable alert thresholds.
- **Tool registry** -- 16 tools (read-only, write, diagnostic) exposed through
  the chat interface and MCP server, with approval gates for destructive actions.
- **Chat server** -- Unix domain socket server with 23 structured commands, plus
  free-text LLM queries when an LLM backend is configured.
- **MCP server** -- Model Context Protocol server on port 8081 exposing tools
  and cluster resources via the `instanceha://` URI scheme.
- **LLM engine** -- pluggable backend supporting local GGUF models (via
  llama-cpp-python) or remote OpenAI-compatible APIs.
- **Safety layer** -- approval manager, audit logging, rate limiting, and
  dry-run mode for all write operations.

## Architecture

```
instanceha.py  (monolith, ConfigMap-mounted)
    |
    +-- publishes events to EventBus at ~13 call sites
    +-- calls start_ai_services() in main() if instanceha_ai is available
    |
instanceha_ai/  (baked into container image)
    +-- event_bus.py        in-process pub/sub
    +-- observer.py         pattern detection + alerts
    +-- tools.py            tool registry + @tool decorator
    +-- read_tools.py       8 read-only cluster query tools
    +-- write_tools.py      5 state-modifying tools (deferred import of monolith)
    +-- diagnostic_tools.py 3 analysis tools
    +-- safety.py           approval gates, audit log, rate limiter
    +-- engine.py           LLM backend (local GGUF / remote API)
    +-- agent.py            agentic tool-calling loop
    +-- prompts.py          system prompts for LLM
    +-- context.py          cluster state summarization
    +-- chat_server.py      Unix socket chat server
    +-- chat_client.py      CLI REPL client
    +-- command_parser.py   structured command parsing
    +-- mcp_server.py       MCP server (streamable-http)
    +-- __main__.py         standalone test mode
```

The monolith degrades gracefully -- if `instanceha_ai` is not installed, the
`_AI_AVAILABLE` flag is `False` and all event publishing and AI startup are
no-ops.

## Configuration

AI configuration keys are added to the InstanceHA config.yaml with the `AI_`
prefix.  All features are disabled by default:

| Key                   | Default   | Description                                          |
|-----------------------|-----------|------------------------------------------------------|
| `AI_ENABLED`          | `"false"` | Master switch for the AI layer                       |
| `AI_BACKEND`          | `""`      | LLM backend type: `"remote"` or `"local"`            |
| `AI_ENDPOINT`         | `""`      | Remote LLM API endpoint (OpenAI-compatible)          |
| `AI_MODEL`            | `""`      | Model name for remote backend                        |
| `AI_API_KEY`          | `""`      | API key for remote backend                           |
| `AI_MODEL_PATH`       | `""`      | Path to local GGUF model file                        |
| `AI_MCP_ENABLED`      | `"false"` | Enable the MCP server on port 8081                   |
| `AI_CHAT_ENABLED`     | `"true"`  | Enable the Unix socket chat server                   |
| `AI_OBSERVER_ENABLED` | `"true"`  | Enable the event observer and alerting                |

## Container Image

Build the AI-enabled image:

```bash
podman build -f Containerfile.instanceha-ai -t instanceha-ai:latest .
```

The image includes:
- Python 3, OpenStack clients, PyYAML, prometheus\_client
- llama-cpp-python (CPU-only GGUF inference)
- MCP SDK (`mcp>=1.25,<2`)
- `instanceha_ai` installed in site-packages
- `instanceha-chat` wrapper script in PATH

Use it by setting `spec.containerImage` in the InstanceHa CR to point to this
image.

## Standalone Test Mode

The standalone mode starts the chat server, observer, and MCP server with a
mock Nova connection, so the AI layer can be exercised without a live OpenStack
deployment.  All write operations run in dry-run mode.

### Running inside the container

```bash
# Start the standalone server
podman run -it --rm -p 8081:8081 instanceha-ai:latest \
    python3 -m instanceha_ai

# In another terminal, connect via the chat client
podman exec -it <container_id> \
    env INSTANCEHA_SOCKET=/tmp/instanceha-test.sock instanceha-chat
```

### Running from a local checkout

No OpenStack credentials or dependencies are needed -- the standalone mode
installs stub modules automatically.

```bash
# Terminal 1: start the server
cd templates/instanceha/bin
python3 -m instanceha_ai --socket /tmp/iha.sock -v

# Terminal 2: connect the chat client
INSTANCEHA_SOCKET=/tmp/iha.sock python3 -m instanceha_ai.chat_client
```

### CLI options

```
python3 -m instanceha_ai [OPTIONS]

  --socket PATH         Unix socket path (default: /tmp/instanceha-test.sock)
  --mcp-port PORT       MCP server port (default: 8081)
  --no-mcp              Disable the MCP server
  --no-sample-events    Don't inject sample events into the event bus
  --endpoint URL        Remote LLM endpoint (OpenAI-compatible API)
  --model NAME          Model name for the remote endpoint
  --api-key KEY         API key for the remote endpoint (optional)
  --verbose, -v         Enable debug logging
```

### Natural language queries with an LLM

By default the standalone mode only supports structured commands (e.g.
`status`, `servers compute-1`).  To enable free-text natural language queries,
point the standalone mode at an OpenAI-compatible LLM endpoint.

The easiest local option is [ollama](https://ollama.com):

```bash
# Install and start ollama (see https://ollama.com/download)
ollama serve &
ollama pull llama3.1:8b

# Start standalone with LLM
cd templates/instanceha/bin
python3 -m instanceha_ai \
    --endpoint http://localhost:11434 \
    --model llama3.1:8b \
    --socket /tmp/iha.sock

# Connect and ask natural language questions
INSTANCEHA_SOCKET=/tmp/iha.sock python3 -m instanceha_ai.chat_client
```

Any OpenAI-compatible API works as the endpoint:

| Backend              | Endpoint example                       | Notes                        |
|----------------------|----------------------------------------|------------------------------|
| ollama               | `http://localhost:11434`               | Local, no API key needed     |
| llama.cpp server     | `http://localhost:8080`                | Local, no API key needed     |
| vLLM                 | `http://localhost:8000`                | Local or remote              |
| OpenAI               | `https://api.openai.com`              | Requires `--api-key`         |
| Azure OpenAI         | `https://<name>.openai.azure.com`     | Requires `--api-key`         |
| Any compatible proxy | varies                                 | Anything with `/v1/chat/completions` |

When an LLM is connected, any input that doesn't match a structured command is
sent to the LLM as a natural language query.  The LLM can use tools (read-only
cluster queries, diagnostics) autonomously and will request approval for write
operations.

Example session:

```
instanceha> what's the cluster status?
The cluster has 5 compute nodes: 3 are up and enabled, 1 (compute-3) is
down and being evacuated, and 1 (compute-4) is in maintenance. There was
a failed evacuation for vm-011 due to NoValidHost.

instanceha> why did the evacuation of vm-011 fail?
Looking at the migration history, vm-011's evacuation from compute-3
failed with NoValidHost. The cluster has 3 available targets (compute-0,
compute-1, compute-2), so this is likely a resource constraint on the
specific VM rather than a cluster-wide capacity issue.

instanceha> status compute-3
Result (get_service_status):
  host: compute-3
  state: down
  status: disabled
  ...
```

### Mock cluster

The standalone mode creates a realistic mock cluster:

| Host        | State | Status   | Notes                           |
|-------------|-------|----------|---------------------------------|
| compute-0   | up    | enabled  | 2 VMs (web-frontend-1, web-frontend-2) |
| compute-1   | up    | enabled  | 3 VMs (db-primary, db-replica, cache-01) |
| compute-2   | up    | enabled  | 2 VMs (worker-1, worker-2)      |
| compute-3   | down  | disabled | Forced down, evacuation in progress, 2 VMs |
| compute-4   | up    | disabled | Maintenance, no VMs             |

Sample events are injected by default (disable with `--no-sample-events`):
a service state change for compute-3 going down, fencing, disabling, and
evacuation results (one success, one failure with `NoValidHost`).

## Chat Interface

### Connecting

From inside the pod:

```bash
oc rsh <pod> instanceha-chat
```

From a standalone session:

```bash
INSTANCEHA_SOCKET=/tmp/instanceha-test.sock python3 -m instanceha_ai.chat_client
```

### Command Reference

#### Read-Only Commands

| Command                         | Description                               | Alias     |
|---------------------------------|-------------------------------------------|-----------|
| `status`                        | Show all compute service status            | `st`      |
| `status <host>`                 | Show detailed status of a specific host    | `st`      |
| `servers <host>`                | List VMs on a host                         | `vms`     |
| `history [minutes]`             | Show recent evacuation events              | `hist`    |
| `summary`                       | Show overall cluster health summary        | `health`  |
| `migration <server_id>`         | Show migration status for a server         | `mig`     |
| `config`                        | Show current configuration                 | `cfg`     |
| `kdump`                         | Show hosts with kdump events               |           |

#### Write Commands (approval required)

| Command                                    | Approval | Description                       |
|--------------------------------------------|----------|-----------------------------------|
| `fence <host> <on\|off>`                   | CRITICAL | Power on/off a host via fencing   |
| `evacuate <host> [target]`                 | CRITICAL | Evacuate all VMs from a host      |
| `disable <host>`                           | HIGH     | Force-down a compute service      |
| `enable <host>`                            | MEDIUM   | Re-enable a compute service       |
| `evacuate-server <server_id> [target]`     | HIGH     | Evacuate a single VM              |

#### Diagnostic Commands

| Command                     | Description                                        | Alias  |
|-----------------------------|----------------------------------------------------|--------|
| `diagnose <host>`           | Analyze why evacuation failed                      | `diag` |
| `capacity [host]`           | Check if the cluster can absorb an evacuation      | `cap`  |
| `correlate [minutes]`       | Correlate fencing/evacuation/state changes          | `corr` |

#### AI Monitoring Commands

| Command                           | Description                                | Alias      |
|-----------------------------------|--------------------------------------------|------------|
| `alerts [severity] [minutes]`     | Show observer alerts                       |            |
| `acknowledge <index>`             | Acknowledge an alert by index              | `ack`      |
| `cluster-health`                  | Show AI-assessed cluster health overview   | `chealth`  |
| `host-analysis <host>`            | Show AI analysis for a specific host       | `hanalysis`|
| `events [minutes] [host]`         | Show recent event bus events               |            |

#### Approval Management

| Command            | Description                        |
|--------------------|------------------------------------|
| `approve <id>`     | Approve a pending action           |
| `deny <id>`        | Deny a pending action              |
| `pending`          | Show pending approval requests     |
| `audit [count]`    | Show recent audit log entries      |

### Approval Workflow

Write commands go through the approval manager.  When a write command is
issued, the chat server responds with an approval ID:

```
instanceha> fence compute-3 off
Action requires approval (level=critical).
  Approval ID: a1b2c3d4
  Type 'approve a1b2c3d4' to proceed or 'deny a1b2c3d4' to cancel.
```

Use `approve <id>` or `deny <id>` to act on the request.  Use `pending` to
list all pending approvals and `audit` to review the audit log.

In standalone/dry-run mode, approved actions report what they would do without
executing.

## MCP Server

The MCP server runs on port 8081 using streamable-http transport when
`AI_MCP_ENABLED` is `"true"` (or in standalone mode unless `--no-mcp` is
passed).

### Tools

All read-only and diagnostic tools from the tool registry are exposed as MCP
tools.  Write tools are only exposed when `allow_writes=True` (disabled by
default in MCP).

### Resources

| URI                          | Description                                  |
|------------------------------|----------------------------------------------|
| `instanceha://health`        | Cluster health assessment from the observer  |
| `instanceha://alerts`        | Unacknowledged alerts                        |
| `instanceha://alerts/all`    | All alerts (including acknowledged)          |
| `instanceha://events`        | Recent events from the event bus (60 min)    |
| `instanceha://host/{host}`   | AI analysis for a specific compute host      |

### Connecting an MCP client

Any MCP-compatible client can connect to the server.  For example, with the
OpenStack Assistant's Goose agent, configure it to connect to
`http://<pod-ip>:8081` as an MCP endpoint.

## Running Tests

```bash
cd test/instanceha

# Run all AI tests
python3 -m pytest test_ai_tools.py test_chat_interface.py \
    test_intelligent_monitoring.py test_llm_integration.py \
    test_mcp_server.py -v

# Run a specific test file
python3 -m pytest test_ai_tools.py -v

# Run existing InstanceHA tests (unaffected by AI layer)
make -C ../.. test-instanceha
```

The test suite mocks all external dependencies (Nova, Keystone) and does not
require a live OpenStack environment.

## Graceful Degradation

The AI layer is fully optional.  If `instanceha_ai` is not installed (e.g.
using the standard container image without AI), the monolith behaves identically
to before:

```python
try:
    from instanceha_ai.event_bus import get_event_bus, EventType, Event
    _AI_AVAILABLE = True
except ImportError:
    _AI_AVAILABLE = False
```

All event publishing calls are guarded by `if not _AI_AVAILABLE: return` and
wrapped in a bare `except Exception: pass` to prevent any AI issue from
affecting core HA functionality.
