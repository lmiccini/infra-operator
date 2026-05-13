# InstanceHA Operator Guide

## What is InstanceHA?

InstanceHA provides automatic high availability for OpenStack instances running on bare-metal compute nodes. When a compute host fails, InstanceHA detects the failure, powers off the host via out-of-band management (fencing), and evacuates all eligible instances to healthy hosts using the Nova API.

InstanceHA runs as a Kubernetes-managed workload alongside the OpenStack control plane. A Go-based Kubernetes controller manages the lifecycle of the InstanceHA deployment, while a Python agent inside the pod continuously monitors compute services and responds to failures.

## Architecture Overview

```
┌──────────────────────────────────────────────────────────────────┐
│                    Kubernetes Cluster                            │
│                                                                  │
│  ┌─────────────────────┐        ┌──────────────────────────────┐ │
│  │  InstanceHa CR      │        │  InstanceHA Controller (Go)  │ │
│  │  (Custom Resource)  │◄──────►│  - Watches CR changes        │ │
│  │                     │        │  - Manages Deployment        │ │
│  └─────────────────────┘        │  - Validates inputs          │ │
│                                 │  - Mounts config & secrets   │ │
│                                 └──────────────────────────────┘ │
│                                           │                      │
│                                           ▼                      │
│  ┌───────────────────────────────────────────────────────────┐   │
│  │  InstanceHA Pod                                           │   │
│  │  ┌──────────────────────────────────────────────────────┐ │   │
│  │  │  instanceha.py (Python Agent)                        │ │   │
│  │  │                                                      │ │   │
│  │  │  Poll Loop ──► Nova API ──► Detect Failures          │ │   │
│  │  │       │                           │                  │ │   │
│  │  │       │                    ┌──────┴──────┐           │ │   │
│  │  │       │                    ▼             ▼           │ │   │
│  │  │       │              Fence Host    Evacuate VMs      │ │   │
│  │  │       │             (IPMI/Redfish   (Nova API)       │ │   │
│  │  │       │              /Metal3)                        │ │   │
│  │  │       │                                              │ │   │
│  │  │  UDP Listener ◄── Kdump messages from compute nodes  │ │   │
│  │  │  Health Server ──► :8080 liveness probe              │ │   │
│  │  └──────────────────────────────────────────────────────┘ │   │
│  └───────────────────────────────────────────────────────────┘   │
│                                                                  │
│  ConfigMap: clouds.yaml    Secret: secure.yaml                   │
│  ConfigMap: config.yaml    Secret: fencing.yaml                  │
└──────────────────────────────────────────────────────────────────┘
```

### How It Works

1. **Detection**: The agent polls the Nova API every `POLL` seconds (default: 45). It queries all `nova-compute` services and checks each one's `updated_at` timestamp and `state` field. A service is considered failed if it is reported as `down` or if its heartbeat is staler than `DELTA` seconds (default: 30).

2. **Safety checks**: Before acting, the agent verifies that the percentage of failed hosts does not exceed the `THRESHOLD` (default: 50%) and that at least one `nova-scheduler` is running. These checks prevent cascading evacuations during infrastructure-wide outages.

3. **Fencing**: The failed host is powered off via its baseboard management controller (BMC) using IPMI, Redfish, or the Metal3 Kubernetes API. Fencing ensures the host is truly offline before evacuation begins, preventing split-brain scenarios where the old and new copies of an instance run simultaneously.

4. **Evacuation**: The agent marks the compute service as `forced_down` and `disabled` in Nova, then calls the evacuate API for each eligible instance. Nova rebuilds each instance on a healthy host using the same image, flavor, and network configuration.

5. **Recovery**: After evacuation completes, the agent updates the service's `disabled_reason` marker. When the host comes back online and its `nova-compute` service reports `up`, the agent automatically re-enables it (unless `LEAVE_DISABLED` is set).

### State Machine

Each compute service transitions through the following states during a failure and recovery cycle:

```
Normal ──► Failed ──► Fenced ──► Evacuating ──► Complete ──► Re-enabled
                                                    │
                                        (LEAVE_DISABLED=true)
                                                    │
                                                    ▼
                                            Remains Disabled
                                          (manual intervention)
```

The agent tracks state via the Nova service `disabled_reason` field:

| Marker | Meaning |
|--------|---------|
| `instanceha evacuation: <timestamp>` | Evacuation in progress |
| `instanceha evacuation complete: <timestamp>` | Evacuation finished, awaiting re-enable |
| `instanceha evacuation FAILED: <timestamp>` | Evacuation failed, requires manual intervention |
| `instanceha evacuation (kdump): <timestamp>` | Kdump-triggered evacuation in progress |
| `instanceha evacuation complete (kdump): <timestamp>` | Kdump evacuation finished |

If the InstanceHA agent is restarted mid-evacuation, it detects services with the `instanceha evacuation:` marker (without `complete` or `FAILED`) and resumes evacuation without re-fencing.

---

## Deployment

### Prerequisites

- An OpenStack control plane deployed via openstack-k8s-operators
- OpenStack admin credentials (clouds.yaml and secure.yaml) stored as a ConfigMap and Secret
- Fencing credentials for each compute node's BMC stored as a Secret
- Network connectivity from the InstanceHA pod to Nova API, Keystone, and all compute BMCs

### Creating the InstanceHa Resource

```yaml
apiVersion: instanceha.openstack.org/v1beta1
kind: InstanceHa
metadata:
  name: instanceha
  namespace: openstack
spec:
  # OpenStack cloud name (must match clouds.yaml)
  openStackCloud: default

  # ConfigMap containing clouds.yaml
  openStackConfigMap: openstack-config

  # Secret containing secure.yaml (passwords)
  openStackConfigSecret: openstack-config-secret

  # Secret containing fencing.yaml (BMC credentials)
  fencingSecret: fencing-secret

  # ConfigMap containing InstanceHA configuration
  instanceHaConfigMap: instanceha-config

  # UDP port for kdump messages (default: 7410)
  instanceHaKdumpPort: 7410

  # Optional: CA certificates for TLS connections
  caBundleSecretName: combined-ca-bundle

  # Optional: node placement constraints
  nodeSelector:
    node-role.kubernetes.io/worker: ""

  # Optional: additional network attachments
  networkAttachments:
    - internalapi

  # Optional: disable without removing the resource
  disabled: "False"
```

### Required Secrets and ConfigMaps

**clouds.yaml** (ConfigMap): OpenStack endpoint and authentication configuration.

```yaml
clouds:
  default:
    auth:
      username: admin
      project_name: admin
      auth_url: https://keystone-public.openstack.svc:5000/v3
      user_domain_name: Default
      project_domain_name: Default
    region_name: regionOne
```

**secure.yaml** (Secret): Password for the cloud defined in clouds.yaml.

```yaml
clouds:
  default:
    auth:
      password: <admin-password>
```

**fencing.yaml** (Secret): Per-host fencing credentials. Each key is the compute host's short hostname as reported by `nova-compute`.

```yaml
FencingConfig:
  compute-0:
    agent: fence_ipmilan
    ipaddr: 10.0.0.10
    ipport: "623"
    login: admin
    passwd: <ipmi-password>

  compute-1:
    agent: fence_redfish
    ipaddr: 10.0.0.11
    ipport: "443"
    login: root
    passwd: <redfish-password>
    tls: "true"
    uuid: System.Embedded.1

  compute-2:
    agent: fence_metal3
    host: compute-2-bmh
    namespace: openshift-machine-api
    token: <service-account-token>
```

**config.yaml** (ConfigMap): InstanceHA runtime parameters. All fields are optional; defaults are applied automatically. See the [Configuration Reference](#configuration-reference) section for details.

```yaml
config:
  POLL: "45"
  DELTA: "30"
  THRESHOLD: "50"
  SMART_EVACUATION: "true"
  WORKERS: "4"
  LOGLEVEL: "INFO"
```

---

## Fencing

Fencing (also called STONITH — Shoot The Other Node In The Head) ensures that a failed compute host is truly powered off before its instances are rebuilt elsewhere. Without fencing, a host that appears down but is actually partitioned could continue running instances, leading to data corruption or IP conflicts.

### Supported Fencing Agents

| Agent | Protocol | Use Case |
|-------|----------|----------|
| `fence_ipmilan` | IPMI over LAN | Standard BMC on most server hardware |
| `fence_redfish` | Redfish (HTTPS) | Modern BMCs (iDRAC, iLO, OpenBMC) |
| `fence_metal3` | Kubernetes API | Bare-metal hosts managed by Metal3/Ironic |
| `noop` | None | Testing and development only |

### IPMI Configuration

```yaml
compute-0:
  agent: fence_ipmilan
  ipaddr: 10.0.0.10    # BMC IP address
  ipport: "623"         # IPMI port (default: 623)
  login: admin          # BMC username
  passwd: <password>    # BMC password (passed via env var, not CLI)
```

### Redfish Configuration

```yaml
compute-1:
  agent: fence_redfish
  ipaddr: 10.0.0.11     # BMC IP/hostname
  ipport: "443"          # Redfish port
  login: root            # BMC username
  passwd: <password>     # BMC password
  tls: "true"            # Enable TLS (recommended)
  uuid: System.Embedded.1  # Redfish system ID
```

Redfish operations retry up to 3 times with per-request timeout of `FENCING_TIMEOUT / 3`. All URLs are validated to prevent SSRF attacks (localhost and link-local addresses are blocked).

### Metal3 (BMH) Configuration

```yaml
compute-2:
  agent: fence_metal3
  host: compute-2-bmh      # BareMetalHost CR name
  namespace: openshift-machine-api  # BMH namespace
  token: <sa-token>        # ServiceAccount bearer token
```

Metal3 fencing patches the `BareMetalHost` custom resource in the Kubernetes API to trigger a power state change, then waits for the power-off to be confirmed.

---

## Evacuability: Controlling Which Instances Are Protected

By default, InstanceHA evaluates three tag-based filters to decide which instances to evacuate. The filters use **OR logic**: an instance is evacuable if it matches any enabled filter.

### Tagging Methods

**Flavor tagging** (`TAGGED_FLAVORS: true`): Only evacuate instances whose flavor has the evacuable tag.

```bash
openstack flavor set --property evacuable=true m1.large
```

**Image tagging** (`TAGGED_IMAGES: true`): Only evacuate instances whose image has the evacuable tag.

```bash
openstack image set --property evacuable=true rhel-9.4
```

**Aggregate tagging** (`TAGGED_AGGREGATES: true`): Only evacuate instances on hosts that belong to an aggregate with the evacuable tag.

```bash
openstack aggregate set --property evacuable=true production-hosts
```

The tag name defaults to `evacuable` and can be changed with `EVACUABLE_TAG`.

### Behavior Summary

| TAGGED_FLAVORS | TAGGED_IMAGES | TAGGED_AGGREGATES | Effect |
|:-:|:-:|:-:|--------|
| false | false | false | All instances are evacuated |
| true | false | false | Only instances with tagged flavors |
| false | true | false | Only instances with tagged images |
| true | true | false | Instances with tagged flavor OR tagged image |
| false | false | true | Only instances on hosts in tagged aggregates |
| true | true | true | Tagged flavor OR tagged image, AND host must be in tagged aggregate |

When all three filters are enabled but no resources carry the tag, all instances are evacuated (fail-open behavior ensures evacuation works out of the box before tagging is configured).

---

## Evacuation Strategies

### Traditional (Default)

```yaml
config:
  SMART_EVACUATION: "false"
```

The agent submits evacuation requests to Nova and moves on. It does not track whether individual migrations succeed or fail. This mode has lower API overhead but provides less visibility.

### Smart Evacuation

```yaml
config:
  SMART_EVACUATION: "true"
  WORKERS: "8"
```

The agent tracks each migration to completion using a thread pool. It polls Nova for migration status every 10 seconds, retries up to 5 times on transient errors, and times out after 300 seconds. Failed evacuations are recorded in the service's `disabled_reason`. This is the recommended mode for production.

---

## Kdump Integration

When `CHECK_KDUMP` is enabled, InstanceHA can detect kernel crashes on compute hosts and coordinate evacuation with the kernel dump process.

### How It Works

1. A background thread listens for UDP packets on the configured port (default: 7410).
2. When the Linux kernel on a compute node panics, `fence_kdump` sends a magic-number packet to the InstanceHA pod.
3. InstanceHA identifies the sending host via reverse DNS lookup.
4. Instead of waiting for the host's heartbeat to expire, InstanceHA immediately marks the host for evacuation, skipping the power-off fencing step (the host is already crashing).
5. After evacuation, InstanceHA waits 60 seconds after the last kdump packet before re-enabling the host, giving the kernel dump process time to complete.

### Compute Node Configuration

Each compute node needs `fence_kdump` configured in `/etc/kdump.conf`:

```
fence_kdump_nodes <instanceha-pod-ip>
fence_kdump_args -p 7410
```

### InstanceHA Configuration

```yaml
config:
  CHECK_KDUMP: "true"
  KDUMP_TIMEOUT: "300"   # Seconds to wait for kdump before normal evacuation
```

If a host goes down and no kdump packet arrives within `KDUMP_TIMEOUT` seconds, InstanceHA proceeds with the standard fencing and evacuation workflow.

---

## Reserved Hosts

Reserved hosts act as standby capacity. They are compute nodes that are pre-disabled in Nova with a `disabled_reason` containing "reserved". When a compute host fails, InstanceHA can automatically enable a matching reserved host to replace the lost capacity.

### Matching Strategies

- **Aggregate-based** (when `TAGGED_AGGREGATES: true`): The reserved host must be in the same host aggregate as the failed host, and the aggregate must have the evacuable tag.
- **Zone-based** (when `TAGGED_AGGREGATES: false`): The reserved host must be in the same availability zone as the failed host.

### Forced Evacuation to Reserved Host

When `FORCE_RESERVED_HOST_EVACUATION` is enabled, InstanceHA passes the reserved host as the explicit target to the Nova evacuate API, bypassing the Nova scheduler. When disabled, the scheduler chooses the best destination from all available hosts (including the newly-enabled reserved host).

```yaml
config:
  RESERVED_HOSTS: "true"
  FORCE_RESERVED_HOST_EVACUATION: "false"  # Let scheduler choose
  TAGGED_AGGREGATES: "true"
```

---

## Threshold Protection

To prevent runaway evacuations during datacenter-wide failures (network partitions, power outages), InstanceHA checks what percentage of compute services are down before proceeding.

```yaml
config:
  THRESHOLD: "50"         # Maximum percentage of failed hosts
  TAGGED_AGGREGATES: "true"
```

When `TAGGED_AGGREGATES` is enabled, the percentage is calculated against only the hosts in evacuable aggregates (not all hosts). This prevents non-HA hosts from diluting the ratio.

If the threshold is exceeded, InstanceHA logs an error and skips evacuation for that poll cycle, waiting for the situation to resolve or for an operator to intervene.

Setting `THRESHOLD` to `0` disables the check entirely.

---

## Multi-Region Deployments

InstanceHA operates within a single OpenStack region, scoped by the `region_name` in `clouds.yaml`. For deployments spanning multiple regions, deploy one InstanceHa CR per region, each with its own `clouds.yaml` pointing to the correct region.

Each instance is fully independent — no cross-region communication or coordination occurs. A failure in one region does not affect InstanceHA operations in other regions.

---

## Configuration Reference

### InstanceHa Custom Resource Fields

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `containerImage` | string | (env-based) | Container image for the InstanceHA agent |
| `openStackCloud` | string | `"default"` | Cloud name matching an entry in `clouds.yaml` |
| `openStackConfigMap` | string | `"openstack-config"` | ConfigMap with `clouds.yaml` |
| `openStackConfigSecret` | string | `"openstack-config-secret"` | Secret with `secure.yaml` |
| `fencingSecret` | string | `"fencing-secret"` | Secret with `fencing.yaml` |
| `instanceHaConfigMap` | string | `"instanceha-config"` | ConfigMap with `config.yaml` |
| `instanceHaKdumpPort` | int32 | `7410` | UDP port for kdump messages |
| `nodeSelector` | map | none | Kubernetes node placement constraints |
| `networkAttachments` | []string | none | Additional Multus network attachments |
| `caBundleSecretName` | string | none | Secret with CA certificates for TLS |
| `disabled` | string | `"False"` | `"True"` to disable fencing and evacuation |
| `topologyRef` | object | none | Reference to a Topology CR for pod placement |

### Agent Configuration (config.yaml)

All values are strings in the ConfigMap. The agent converts and validates them at startup.

#### Timing

| Parameter | Type | Default | Range | Description |
|-----------|------|---------|-------|-------------|
| `POLL` | int | 45 | 15–600 | Seconds between Nova API polls |
| `DELTA` | int | 30 | 10–300 | Seconds of heartbeat staleness before a service is considered failed |
| `DELAY` | int | 0 | 0–300 | Seconds to wait after fencing before starting evacuation (allows storage lock release) |
| `HASH_INTERVAL` | int | 60 | 30–300 | Seconds between health-check hash updates |
| `FENCING_TIMEOUT` | int | 30 | 5–120 | Seconds allowed for fencing operations |
| `KDUMP_TIMEOUT` | int | 30 | 5–300 | Seconds to wait for kdump messages before normal evacuation |

#### Evacuation Behavior

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `SMART_EVACUATION` | bool | false | Track migrations to completion instead of fire-and-forget |
| `WORKERS` | int | 4 (range: 1–50) | Thread pool size for concurrent smart evacuations |
| `THRESHOLD` | int | 50 (range: 0–100) | Max percentage of failed hosts before evacuation is blocked. Set to 0 to disable |
| `EVACUABLE_TAG` | string | `"evacuable"` | Metadata tag name used to identify evacuable resources |
| `TAGGED_IMAGES` | bool | true | Filter by image tag |
| `TAGGED_FLAVORS` | bool | true | Filter by flavor tag |
| `TAGGED_AGGREGATES` | bool | true | Filter by aggregate tag (also affects threshold calculation and reserved host matching) |

#### Host Recovery

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `LEAVE_DISABLED` | bool | false | Keep hosts disabled after evacuation (requires manual re-enable) |
| `FORCE_ENABLE` | bool | false | Re-enable hosts immediately without waiting for migrations to complete |

#### Reserved Hosts

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `RESERVED_HOSTS` | bool | false | Enable automatic reserved host management |
| `FORCE_RESERVED_HOST_EVACUATION` | bool | false | Direct evacuations to the reserved host instead of using the Nova scheduler |

#### Kdump

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `CHECK_KDUMP` | bool | false | Enable kdump detection via UDP listener |
| `KDUMP_TIMEOUT` | int | 30 (range: 5–300) | Seconds to wait for kdump packets after detecting a down host |

#### Security

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `SSL_VERIFY` | bool | true | Verify TLS certificates for Redfish and Kubernetes API connections |

#### Service Control

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `DISABLED` | bool | false | Skip all evacuation logic (health checks continue). Overridden by CR `spec.disabled` |
| `LOGLEVEL` | string | `"INFO"` | Log verbosity: `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL` |

---

## Operational Procedures

### Disabling InstanceHA Temporarily

For planned maintenance, disable evacuations without removing the resource:

```bash
oc patch instanceha instanceha -n openstack --type merge \
  -p '{"spec": {"disabled": "True"}}'
```

The agent continues running and reporting health but takes no fencing or evacuation actions. Re-enable by setting `disabled` back to `"False"`.

Alternatively, set `DISABLED: "true"` in the ConfigMap for the same effect at the agent level.

### Investigating a Failed Evacuation

Services with `disabled_reason` containing `FAILED` require manual intervention:

```bash
# Find failed services
openstack compute service list --long | grep FAILED

# After investigating and resolving the issue, re-enable:
openstack compute service set --enable <host> nova-compute
openstack compute service set --unset-forced-down <host> nova-compute
```

### Adding a New Compute Host

1. Add fencing credentials for the new host to `fencing.yaml` and update the Secret.
2. If using aggregate-based tagging, add the host to an aggregate with `evacuable=true`.
3. No InstanceHA restart is needed — the agent discovers new hosts automatically on its next poll.

### Tuning Detection Speed vs. False Positives

- **Faster detection**: Lower `DELTA` (minimum 10s) and `POLL` (minimum 15s). Risk: transient network issues may trigger false evacuations.
- **Fewer false positives**: Raise `DELTA` (up to 300s). Risk: longer time before instances are recovered.
- A reasonable starting point for production is `DELTA: 30` with `POLL: 45`.

### Checking InstanceHA Health

The agent exposes an HTTP health endpoint on port 8080, used by the Kubernetes liveness probe. The endpoint returns the latest health-check hash, updated every `HASH_INTERVAL` seconds.

```bash
# Check pod health
oc get pods -n openstack -l app=instanceha

# View agent logs
oc logs -n openstack deployment/instanceha-instanceha
```
