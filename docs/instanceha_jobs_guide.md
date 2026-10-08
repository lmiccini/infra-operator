# InstanceHA Administrator Guide: Jobs to Be Done

This guide is organized around the tasks OpenStack and IT administrators need to accomplish when deploying and operating InstanceHA for compute instance high availability.

## Table of Contents

1. [Protect Virtual Machine Instances from Host Failures](#protect-virtual-machine-instances-from-host-failures)
2. [Configure Failure Detection](#configure-failure-detection)
3. [Control Which Instances Are Protected](#control-which-instances-are-protected)
4. [Set Up Host Fencing](#set-up-host-fencing)
5. [Configure Evacuation Behavior](#configure-evacuation-behavior)
6. [Monitor InstanceHA Operations](#monitor-instanceha-operations)
7. [Respond to Failures and Incidents](#respond-to-failures-and-incidents)
8. [Scale to Large Deployments](#scale-to-large-deployments)
9. [Deploy and Upgrade InstanceHA](#deploy-and-upgrade-instanceha)
10. [Secure and Authenticate](#secure-and-authenticate)

---

## Protect Virtual Machine Instances from Host Failures

**Job:** Automatically recover virtual machine instances when a compute host fails, minimizing downtime and manual intervention.

### What InstanceHA Does

When a compute host fails, InstanceHA:

1. Detects the failure by monitoring Nova compute service status
2. Powers off the failed host via out-of-band management (fencing)
3. Evacuates eligible instances to healthy hosts using the Nova API
4. Re-enables the host when it recovers

### Minimum Required Configuration

Deploy an InstanceHa custom resource with OpenStack credentials and fencing configuration:

```yaml
apiVersion: instanceha.openstack.org/v1beta1
kind: InstanceHa
metadata:
  name: instanceha
  namespace: openstack
spec:
  openStackCloud: default
  openStackConfigMap: openstack-config        # Contains clouds.yaml
  openStackConfigSecret: openstack-config-secret  # Contains secure.yaml
  fencingSecret: fencing-secret               # Contains BMC credentials
  instanceHaConfigMap: instanceha-config      # Contains operational parameters
```

Required Kubernetes resources:

- **ConfigMap** (`openstack-config`): OpenStack endpoint and authentication configuration
- **Secret** (`openstack-config-secret`): OpenStack admin password
- **Secret** (`fencing-secret`): BMC credentials for each compute host
- **ConfigMap** (`instanceha-config`): Detection, evacuation, and safety parameters

### Expected Behavior

With default settings:

- Detection latency: 30-75 seconds (DELTA=30s + up to POLL=45s)
- Fencing timeout: 30 seconds per host
- Evacuation: Fire-and-forget (traditional mode)
- Threshold protection: Blocks evacuation when more than 50% of hosts are down
- Re-enable: Automatic when host recovers

---

## Configure Failure Detection

**Job:** Detect compute host failures accurately while avoiding false positives caused by transient network issues or control plane disruptions.

### Detection Mechanisms

InstanceHA offers two detection channels:

#### Nova Service Poll (Required)

InstanceHA polls the Nova API every `POLL` seconds and marks a compute service as failed when:

- The service `state` is `down`, **OR**
- The service `updated_at` timestamp is older than `DELTA` seconds

Configuration:

```yaml
config:
  POLL: "45"      # Seconds between Nova API polls (range: 15-600)
  DELTA: "30"     # Staleness threshold in seconds (range: 10-300)
```

Detection latency: Up to `POLL + DELTA` seconds (worst case: 75 seconds with defaults).

#### Heartbeat Verification (Optional)

Enable a second detection channel using UDP heartbeat packets from compute nodes. A host is only fenced when **both** the Nova poll and heartbeat channels agree it is unreachable.

Configuration:

```yaml
# In InstanceHa CR
spec:
  instanceHaHeartbeatPort: 7411

# In config.yaml
config:
  CHECK_HEARTBEAT: "true"
  HEARTBEAT_TIMEOUT: "120"   # Seconds without heartbeat before marking down
```

Heartbeat requires:

1. The `instanceha-monitoring` EDPM service deployed on compute nodes
2. Compute nodes configured to send heartbeat packets to the InstanceHA pod IP
3. Network connectivity between compute nodes and the InstanceHA pod on the heartbeat port

See [Set Up Heartbeat Detection](#set-up-heartbeat-detection) for deployment details.

### Safety Gates Against False Positives

InstanceHA evaluates multiple gates before fencing a host:

| Gate | Purpose | Configuration |
|------|---------|---------------|
| K8s API connectivity | Blocks fencing when the pod is network-isolated | `K8S_API_CHECK_INTERVAL` (auto-enabled) |
| Heartbeat cliff detection | Blocks fencing when many hosts go silent simultaneously | `HEARTBEAT_CLIFF_THRESHOLD`, `HEARTBEAT_CLIFF_MAX_CYCLES` |
| All-services-stale check | Blocks fencing when all compute services appear down | Automatic (no config) |
| Global threshold | Blocks fencing when percentage of failed hosts exceeds limit | `THRESHOLD` |
| Per-cycle rate limit | Caps number of hosts fenced per poll cycle | `MAX_HOSTS_PER_CYCLE` |

### Set Up Heartbeat Detection

Heartbeat verification significantly reduces false positives. Configure it when:

- The control plane network (internalapi) experiences occasional congestion or packet loss
- You need faster detection than Nova service polling provides
- You operate more than 50 compute nodes

**Step 1:** Add the heartbeat service to your EDPM NodeSet:

```yaml
apiVersion: dataplane.openstack.org/v1beta1
kind: OpenStackDataPlaneNodeSet
metadata:
  name: openstack-edpm
spec:
  services:
    - nova
    - instanceha-monitoring  # Add this
  nodeTemplate:
    ansible:
      ansibleVars:
        edpm_instanceha_monitoring_heartbeat_enabled: true
        edpm_instanceha_monitoring_heartbeat_ip: "<instanceha-pod-ip>"
        edpm_instanceha_monitoring_heartbeat_port: 7411
        edpm_instanceha_monitoring_heartbeat_interval: 30
```

**Step 2:** Provide a stable IP for the heartbeat listener by creating a MetalLB LoadBalancer Service:

```yaml
apiVersion: v1
kind: Service
metadata:
  name: instanceha-heartbeat
  namespace: openstack
  annotations:
    metallb.universe.tf/address-pool: internalapi
    metallb.universe.tf/allow-shared-ip: internalapi
    metallb.universe.tf/loadBalancerIPs: 172.17.0.80
spec:
  type: LoadBalancer
  selector:
    service: instanceha
  ports:
  - name: heartbeat
    port: 7411
    targetPort: 7411
    protocol: UDP
```

Use the Service IP as `edpm_instanceha_monitoring_heartbeat_ip` in the NodeSet configuration.

**Step 3:** Enable heartbeat checking:

```yaml
config:
  CHECK_HEARTBEAT: "true"
  HEARTBEAT_TIMEOUT: "120"
```

**Step 4:** Deploy the NodeSet. Compute nodes will begin sending heartbeat packets after the `instanceha-monitoring` service runs.

**Verification:**

```bash
# Check heartbeat listener is running
oc logs deployment/instanceha | grep "Heartbeat listener"

# Verify packets are being received
oc exec deployment/instanceha -- curl -s http://localhost:8080/metrics | grep heartbeat
```

### Tune Detection for Your Environment

| Scenario | Recommended Settings | Rationale |
|----------|---------------------|-----------|
| Fast detection, stable network | `POLL: 30`, `DELTA: 20`, `CHECK_HEARTBEAT: false` | Detect failures in 30-50 seconds |
| Balanced (default) | `POLL: 45`, `DELTA: 30`, `CHECK_HEARTBEAT: true` | Good balance of speed and accuracy |
| Minimize false positives | `POLL: 60`, `DELTA: 60`, `CHECK_HEARTBEAT: true`, `HEARTBEAT_TIMEOUT: 180` | Tolerate longer network disruptions |
| Very large scale (500+ nodes) | `POLL: 120`, `DELTA: 60`, `CHECK_HEARTBEAT: true` | Reduce Nova API load |

---

## Control Which Instances Are Protected

**Job:** Define which virtual machine instances should be automatically evacuated and which should be excluded from InstanceHA operations.

### Tag-Based Evacuability Filters

InstanceHA supports three independent tag-based filters that use **OR logic**: an instance is evacuable if it matches **any** enabled filter.

#### Filter by Flavor

Only evacuate instances whose flavor has the evacuable tag:

```bash
# Tag a flavor as evacuable
openstack flavor set --property evacuable=true m1.large

# Enable the filter
config:
  TAGGED_FLAVORS: "true"
  EVACUABLE_TAG: "evacuable"
```

#### Filter by Image

Only evacuate instances whose image has the evacuable tag:

```bash
# Tag an image as evacuable
openstack image set --property evacuable=true rhel-9.4

# Enable the filter
config:
  TAGGED_IMAGES: "true"
  EVACUABLE_TAG: "evacuable"
```

#### Filter by Host Aggregate

Only evacuate instances on hosts that belong to an aggregate with the evacuable tag:

```bash
# Tag an aggregate as evacuable
openstack aggregate set --property evacuable=true production-hosts

# Add hosts to the aggregate
openstack aggregate add host production-hosts compute-0

# Enable the filter
config:
  TAGGED_AGGREGATES: "true"
  EVACUABLE_TAG: "evacuable"
```

### Filter Behavior

| TAGGED_FLAVORS | TAGGED_IMAGES | TAGGED_AGGREGATES | Result |
|:-:|:-:|:-:|--------|
| false | false | false | All instances are evacuated |
| true | false | false | Instances with tagged flavors only |
| false | true | false | Instances with tagged images only |
| true | true | false | Instances with tagged flavor **OR** tagged image |
| false | false | true | Instances on hosts in tagged aggregates only |
| true | true | true | (Tagged flavor **OR** tagged image) **AND** host in tagged aggregate |

**Important:** `TAGGED_AGGREGATES` is fail-closed. If enabled but no aggregates carry the tag, no evacuations occur. The flavor and image filters are fail-open: if no flavors or images carry the tag, all instances pass those filters.

### Exclude Instances by Name Pattern

Exclude instances by name regardless of tags:

```yaml
config:
  SKIP_SERVERS_WITH_NAME: "router-*, dvr-*, test-*"
```

Patterns use glob syntax (`*` = any characters, `?` = single character, `[abc]` = character set).

### Practical Tagging Strategies

**Production workloads only:**

1. Create a `production-hosts` aggregate containing production compute nodes
2. Tag it: `openstack aggregate set --property evacuable=true production-hosts`
3. Enable: `TAGGED_AGGREGATES: true`
4. Disable flavor/image filters: `TAGGED_FLAVORS: false`, `TAGGED_IMAGES: false`

**Specific workload types:**

1. Tag production flavors: `openstack flavor set --property evacuable=true <flavor>`
2. Tag production images: `openstack image set --property evacuable=true <image>`
3. Enable both: `TAGGED_FLAVORS: true`, `TAGGED_IMAGES: true`
4. Leave aggregate filter disabled: `TAGGED_AGGREGATES: false`

**Exclude infrastructure VMs:**

1. Use consistent naming: `router-ctlplane-0`, `router-external-1`
2. Add exclusion: `SKIP_SERVERS_WITH_NAME: "router-*"`

---

## Set Up Host Fencing

**Job:** Configure out-of-band management credentials so InstanceHA can power off failed compute hosts before evacuating instances.

### Why Fencing Is Required

Fencing prevents split-brain scenarios where the same instance runs on two hosts simultaneously. Without confirmed power-off, a host that appears down but is actually network-partitioned could continue running instances, causing data corruption when those instances are rebuilt elsewhere.

### Supported Fencing Mechanisms

| Agent | Protocol | Use Case |
|-------|----------|----------|
| `ipmi` | IPMI over LAN | Standard BMC on most server hardware |
| `redfish` | Redfish HTTPS API | Modern BMCs (iDRAC, iLO, OpenBMC) |
| `bmh` | Kubernetes API | Hosts managed by Metal3/Ironic |

### Configure IPMI Fencing

Create a `fencing.yaml` secret with BMC credentials for each compute host:

```yaml
FencingConfig:
  compute-0:
    agent: ipmi
    ipaddr: 10.0.0.10    # BMC IP address
    ipport: "623"        # IPMI port (default: 623)
    login: admin         # BMC username
    passwd: <password>   # BMC password
  
  compute-1:
    agent: ipmi
    ipaddr: 10.0.0.11
    ipport: "623"
    login: admin
    passwd: <password>
```

**Key:** The hostname key (`compute-0`) must match the Nova compute service hostname exactly.

Create the Kubernetes secret:

```bash
kubectl create secret generic fencing-secret \
  --from-file=fencing.yaml=fencing.yaml \
  -n openstack
```

Configure fencing timeout:

```yaml
config:
  FENCING_TIMEOUT: "30"   # Seconds (range: 5-120)
```

### Configure Redfish Fencing

For BMCs that support Redfish (Dell iDRAC, HP iLO, Supermicro):

```yaml
FencingConfig:
  compute-0:
    agent: redfish
    ipaddr: 10.0.0.10     # BMC IP or hostname
    ipport: "443"         # Redfish port (usually 443)
    login: root           # BMC username
    passwd: <password>    # BMC password
    tls: "true"           # Enable TLS verification
    uuid: System.Embedded.1  # Redfish system ID
```

**Finding the Redfish system UUID:**

```bash
# Query the Redfish API
curl -k -u root:password https://10.0.0.10/redfish/v1/Systems/

# The response contains the system ID
```

### Configure Metal3 (BareMetalHost) Fencing

For compute hosts managed by Metal3/Ironic:

```yaml
FencingConfig:
  compute-0:
    agent: bmh
    host: compute-0-bmh           # BareMetalHost CR name
    namespace: openshift-machine-api
    token: <service-account-token>
```

**Create a service account with power management permissions:**

```bash
# Create ServiceAccount
oc create sa instanceha-fencing -n openshift-machine-api

# Grant permissions
oc create role bmh-power-manager \
  --verb=get,list,patch,update \
  --resource=baremetalhosts \
  -n openshift-machine-api

oc create rolebinding instanceha-bmh-power \
  --role=bmh-power-manager \
  --serviceaccount=openshift-machine-api:instanceha-fencing \
  -n openshift-machine-api

# Get the token
TOKEN=$(oc create token instanceha-fencing -n openshift-machine-api --duration=8760h)
```

Use the token in `fencing.yaml`.

### Verify Fencing Configuration

Test fencing manually before deployment:

```bash
# For IPMI
ipmitool -I lanplus -H 10.0.0.10 -U admin -P <password> power status

# For Redfish
curl -k -u root:password -X POST \
  https://10.0.0.10/redfish/v1/Systems/System.Embedded.1/Actions/ComputerSystem.Reset \
  -H "Content-Type: application/json" \
  -d '{"ResetType": "ForceOff"}'

# For Metal3
oc patch baremetalhost compute-0-bmh \
  -n openshift-machine-api \
  --type merge \
  -p '{"spec":{"online":false}}'
```

---

## Configure Evacuation Behavior

**Job:** Control how instances are evacuated, including tracking, prioritization, and control plane load management.

### Evacuation Strategies

#### Traditional Evacuation (Default)

Submit evacuation requests to Nova and proceed immediately without tracking completion.

```yaml
config:
  SMART_EVACUATION: "false"
  ORCHESTRATED_RESTART: "false"
```

**Use when:** You have a small deployment and prefer simplicity over visibility.

**Tradeoffs:** No per-instance tracking. Cannot detect partial evacuation failures.

#### Smart Evacuation (Recommended)

Track each migration to completion, retry transient errors, and report per-instance results.

```yaml
config:
  SMART_EVACUATION: "true"
  WORKERS: "8"                  # Parallel fencing operations
  EVACUATION_RETRIES: "5"       # Per-instance retry attempts
  EVACUATION_MAX_THREADS: "32"  # Total evacuation thread budget
```

**Use when:** You need visibility into evacuation progress and want automatic retry on transient Nova API errors.

**Tradeoffs:** Higher Nova API call volume. More complex tracking.

With `WORKERS: 8` and `EVACUATION_MAX_THREADS: 32`, each host evacuates up to 4 instances concurrently (32/8=4).

#### Orchestrated Evacuation

Evacuate instances in priority-ordered phases based on metadata, useful for applications with startup dependencies.

```yaml
config:
  ORCHESTRATED_RESTART: "true"
  WORKERS: "8"
```

Set priority and restart group on instances:

```bash
# Databases evacuated first (priority 1000)
openstack server set --property instanceha:restart_group=1 db-server
openstack server set --property instanceha:restart_priority=1000 db-server

# Application servers evacuated second (priority 500)
openstack server set --property instanceha:restart_group=2 app-server
openstack server set --property instanceha:restart_priority=500 app-server
```

Instances within the same group evacuate concurrently (up to `WORKERS` threads). Groups execute sequentially in descending priority order.

**Use when:** Instances have startup dependencies that must be respected during recovery.

### Manage Control Plane Load

When evacuating many instances from a single host, concurrent evacuation requests can overwhelm the Nova API.

**Limit concurrent evacuations per host:**

```yaml
config:
  EVACUATION_MAX_THREADS: "16"  # Lower total thread budget
  WORKERS: "4"                  # 16/4 = 4 concurrent per host
```

**Stagger evacuation submissions:**

```yaml
config:
  EVACUATION_STAGGER: "2"  # 2-second delay between each instance
```

With `EVACUATION_STAGGER: 2` and 30 instances, submissions spread over 60 seconds.

### Configure Reserved Hosts

Reserved hosts provide standby capacity. When a compute host fails, InstanceHA can automatically enable a matching reserved host to replace the lost capacity.

**Step 1:** Pre-disable reserved hosts in Nova:

```bash
openstack compute service set --disable \
  --disable-reason "reserved for InstanceHA" \
  compute-spare-0 nova-compute
```

**Step 2:** Enable reserved host management:

```yaml
config:
  RESERVED_HOSTS: "true"
  TAGGED_AGGREGATES: "true"  # Reserved host must be in same aggregate as failed host
  FORCE_RESERVED_HOST_EVACUATION: "false"  # Let Nova scheduler choose target
```

Matching logic:

- When `TAGGED_AGGREGATES: true`: Reserved host must be in the same aggregate as the failed host
- When `TAGGED_AGGREGATES: false`: Reserved host must be in the same availability zone

When `FORCE_RESERVED_HOST_EVACUATION: true`, instances are directed to the reserved host explicitly. When `false`, the Nova scheduler chooses from all available hosts (including the newly-enabled reserved host).

---

## Monitor InstanceHA Operations

**Job:** Observe InstanceHA behavior, detect anomalies, and troubleshoot failures using metrics, events, and logs.

### Monitoring Tools

InstanceHA provides three observability mechanisms:

| Tool | Use Case | Access Method |
|------|----------|---------------|
| Kubernetes Events | Audit trail of fencing and evacuation lifecycle | `oc describe instanceha <name>` |
| Prometheus Metrics | Time-series data for dashboards and alerting | `http://<pod-ip>:8080/metrics` |
| Logs | Detailed operational logs and error messages | `oc logs deployment/instanceha` |

### Key Kubernetes Events

| Event | Type | Indicates |
|-------|------|-----------|
| `HostDown` | Warning | Compute host detected as down |
| `FencingSucceeded` | Normal | Host successfully powered off |
| `EvacuationSucceeded` | Normal | All VMs evacuated from host |
| `HostReenabled` | Normal | Host recovered and re-enabled |
| `ThresholdExceeded` | Warning | Too many hosts down, evacuation blocked |
| `HeartbeatCliff` | Warning | Mass heartbeat loss, possible network issue |

View events:

```bash
oc describe instanceha instanceha -n openstack | tail -50

# Filter to specific event types
oc get events -n openstack \
  --field-selector involvedObject.name=instanceha,reason=FencingSucceeded
```

### Essential Prometheus Metrics

#### Operational Health

```promql
# Poll cycle success rate
rate(instanceha_poll_cycles_total{result="success"}[5m]) /
rate(instanceha_poll_cycles_total[5m])

# Number of hosts currently being processed
instanceha_hosts_processing
```

#### Fencing Operations

```promql
# Fencing success rate
rate(instanceha_fencing_total{result="succeeded"}[1h]) /
rate(instanceha_fencing_total{result="started"}[1h])

# Hosts fenced in last hour
increase(instanceha_fencing_total{result="succeeded"}[1h])
```

#### Safety Gate Activations

```promql
# Threshold protection triggered
increase(instanceha_threshold_exceeded_total[1h])

# Heartbeat cliff detection triggered
increase(instanceha_heartbeat_cliff_total[1h])

# Rate limiter triggered
increase(instanceha_fencing_rate_limited_total[1h])
```

### Set Up Prometheus Scraping

InstanceHA automatically creates a Service for metrics when deployed. Configure Prometheus to scrape it:

```yaml
apiVersion: monitoring.coreos.com/v1
kind: PodMonitor
metadata:
  name: instanceha-metrics
  namespace: openstack
spec:
  selector:
    matchLabels:
      service: instanceha
  podMetricsEndpoints:
    - port: metrics
      path: /metrics
      interval: 30s
```

### Recommended Alerts

**Critical:**

```yaml
# Mass fencing event
- alert: InstanceHAMassFencing
  expr: increase(instanceha_fencing_total{result="succeeded"}[30m]) > 10
  for: 0m
  annotations:
    summary: "InstanceHA fenced {{ $value }} hosts in 30 minutes"
```

**Warning:**

```yaml
# Poll cycle failures
- alert: InstanceHAPollFailures
  expr: instanceha_poll_consecutive_failures > 2
  for: 5m
  annotations:
    summary: "InstanceHA cannot reach Nova API"

# Fencing failures
- alert: InstanceHAFencingFailed
  expr: increase(instanceha_fencing_total{result="failed"}[1h]) > 0
  for: 0m
  annotations:
    summary: "InstanceHA fencing operations failing"
```

---

## Respond to Failures and Incidents

**Job:** Diagnose and remediate InstanceHA operational issues, false positives, and evacuation failures.

### Investigate a False-Positive Evacuation

A false positive occurs when InstanceHA fences a healthy host due to a transient network issue.

**Indicators:**

- `FencingSucceeded` event for a host that was actually reachable
- BMC power status shows host was on when fencing occurred
- Multiple hosts fenced simultaneously

**Diagnosis:**

```bash
# Check recent fencing events
oc get events -n openstack --field-selector reason=FencingSucceeded \
  --sort-by='.lastTimestamp' | tail -20

# Check heartbeat cliff detection
oc get events -n openstack --field-selector reason=HeartbeatCliff

# Check metrics for control plane issues
oc exec deployment/instanceha -- curl -s http://localhost:8080/metrics | \
  grep -E "(all_services_stale|heartbeat_cliff|k8s_api_reachable)"
```

**Immediate Remediation:**

```bash
# Re-enable the falsely fenced host
openstack compute service set --enable --up <hostname> nova-compute

# If cascade detected, disable InstanceHA immediately
oc patch instanceha instanceha --type merge \
  -p '{"spec": {"disabled": "True"}}'
```

**Prevention:**

| If False Positives Occur Due To | Adjust |
|--------------------------------|--------|
| Control plane network congestion | Enable `CHECK_HEARTBEAT` on a separate network |
| Nova API latency spikes | Increase `DELTA` (e.g., 30→60) |
| Spanning tree reconvergence | Increase `HEARTBEAT_CLIFF_MAX_CYCLES` (e.g., 3→5) |
| Many hosts failing together | Lower `THRESHOLD` (e.g., 50→25) or `MAX_HOSTS_PER_CYCLE` (e.g., 10→3) |

### Recover from a Failed Evacuation

Failed evacuations leave hosts in a disabled state with `disabled_reason` containing `FAILED`.

**Find failed services:**

```bash
openstack compute service list --long | grep FAILED
```

**Check why evacuation failed:**

```bash
# Check InstanceHA events
oc get events -n openstack \
  --field-selector involvedObject.name=instanceha,reason=EvacuationFailed

# Check Nova API errors
openstack server list --host <hostname> --all-projects
openstack server show <server-id>
```

**Common causes:**

| Failure | Cause | Resolution |
|---------|-------|------------|
| Fencing timeout | BMC unreachable or slow | Verify BMC network, increase `FENCING_TIMEOUT` |
| No valid hosts available | Insufficient capacity | Add compute capacity or reduce `reserved_host_ram_mb` |
| Volume attach failure | Encrypted volumes without Barbican access | Grant `key-manager:secret-reader` role to InstanceHA user |
| Migration pre-check failed | Flavor incompatibility | Ensure destination hosts support the instance's flavor |

**Manual recovery:**

```bash
# After resolving the underlying issue, re-enable the host
openstack compute service set --enable --up <hostname> nova-compute

# If instances are still on the failed host, evacuate manually
openstack server evacuate <server-id>
```

### Handle Threshold Protection Activation

When `THRESHOLD` is exceeded, InstanceHA emits a `ThresholdExceeded` event and blocks all fencing.

**Check current state:**

```bash
# Count down hosts
openstack compute service list | grep down | wc -l

# Check threshold events
oc get events -n openstack --field-selector reason=ThresholdExceeded
```

**Resolution:**

1. **Datacenter-wide outage:** Wait for infrastructure recovery. Do not raise the threshold.
2. **Cascade due to infrastructure issue:** Disable InstanceHA, resolve the root cause, then re-enable.
3. **Legitimate multi-host failure within threshold:** Threshold is working correctly. Investigate and resolve individual host issues.

**Temporarily override threshold (use with caution):**

```bash
# Raise threshold to allow processing
oc patch instanceha instanceha --type merge \
  -p '{"spec": {"instanceHaConfigMap": "instanceha-config-emergency"}}'
```

Create `instanceha-config-emergency` with `THRESHOLD: 100` (disables check), then revert after the incident.

---

## Scale to Large Deployments

**Job:** Configure InstanceHA for reliable operation in large OpenStack deployments (500+ compute nodes).

### Architecture at Scale

At 1000 compute nodes, InstanceHA faces:

- Nova API latency (1-3 seconds per poll across multiple cells)
- High UDP packet rate for heartbeat (33 packets/second at 30s interval)
- Potential for multi-rack failures affecting hundreds of hosts

### Recommended Configuration for 1000 Nodes

```yaml
config:
  # Detection timing - balance load vs latency
  POLL: "120"                      # 2-minute poll interval
  DELTA: "60"                      # 60-second staleness threshold
  
  # Heartbeat verification - essential at scale
  CHECK_HEARTBEAT: "true"
  HEARTBEAT_TIMEOUT: "180"         # 3-minute tolerance
  HEARTBEAT_CLIFF_THRESHOLD: "15"  # 15% drop triggers cliff detection
  HEARTBEAT_CLIFF_MAX_CYCLES: "5"  # 10-minute cliff window
  
  # Safety thresholds - conservative limits
  THRESHOLD: "10"                  # 10% global threshold (100 hosts at 1000-node scale)
  MAX_HOSTS_PER_CYCLE: "5"         # Fence at most 5 hosts per cycle
  TAGGED_AGGREGATES: "true"        # Use per-aggregate controls
  
  # Evacuation - managed load
  SMART_EVACUATION: "true"
  WORKERS: "8"                     # 8 parallel fencing operations
  EVACUATION_RETRIES: "3"          # Lower retries to avoid pileup
  EVACUATION_STAGGER: "2"          # 2-second stagger between instances
```

### Why These Values

**`POLL: 120`** - Reduces Nova API load. At 1000 nodes, safety gates must process 1000+ service records each cycle.

**`HEARTBEAT_CLIFF_THRESHOLD: 15`** - At 1000 nodes, 15% = 150 hosts. The default 50% (500 hosts) is unlikely to trigger. A single rack failure (40 nodes) is 4%, two racks is 8%, requiring the per-cycle cap rather than cliff detection.

**`THRESHOLD: 10`** - Blocks fencing when 100+ hosts are down. Conservative for large scale where blast radius is significant.

**`MAX_HOSTS_PER_CYCLE: 5`** - A 40-node rack failure takes 8 cycles (16 minutes at `POLL: 120`), giving operators time to observe and intervene.

### Use Per-Aggregate Thresholds

Model failure domains (racks, rows, cells) using Nova aggregates and set per-aggregate limits:

```bash
# Create rack aggregates with absolute failure limits
openstack aggregate create \
  --property evacuable=true \
  --property instanceha:max_failures=5 \
  rack-01

# Add hosts to the aggregate
openstack aggregate add host rack-01 compute-001
# ... repeat for all hosts in rack

# Or cell-level aggregates
openstack aggregate create \
  --property evacuable=true \
  --property instanceha:max_failures=30 \
  cell-1
```

At 1000 nodes across 3 cells (~333 per cell), `instanceha:max_failures=30` on each cell aggregate means no single cell can lose more than ~9% of its hosts in one cycle.

### Monitor at Scale

Deploy Prometheus alerting for mass fencing events:

```yaml
- alert: InstanceHAMassFencing
  expr: increase(instanceha_fencing_total[30m]) > 20
  annotations:
    summary: "{{ $value }} hosts fenced in 30 minutes"

- alert: InstanceHAFencingBacklog
  expr: increase(instanceha_fencing_rate_limited_total[10m]) > 3
  annotations:
    summary: "Fencing rate limiter active - failures queued"
```

### Heartbeat Network Isolation

At scale, isolate heartbeat traffic from the Nova API network:

```yaml
spec:
  networkAttachments:
    - internalapi       # Nova API, Keystone
    - heartbeat-net     # Dedicated heartbeat network
```

This prevents Nova API congestion from causing heartbeat loss.

---

## Deploy and Upgrade InstanceHA

**Job:** Install InstanceHA in a production OpenStack environment and manage version upgrades with minimal disruption.

### Initial Deployment

**Prerequisites:**

- OpenStack control plane deployed via openstack-k8s-operators
- OpenStack admin credentials available
- BMC credentials for all compute hosts
- Network connectivity from Kubernetes pods to Nova API and BMCs

**Step 1:** Create required secrets and configmaps:

```bash
# OpenStack credentials (clouds.yaml)
oc create configmap openstack-config \
  --from-file=clouds.yaml=clouds.yaml \
  -n openstack

# OpenStack password (secure.yaml)
oc create secret generic openstack-config-secret \
  --from-file=secure.yaml=secure.yaml \
  -n openstack

# BMC credentials (fencing.yaml)
oc create secret generic fencing-secret \
  --from-file=fencing.yaml=fencing.yaml \
  -n openstack

# InstanceHA configuration (config.yaml)
oc create configmap instanceha-config \
  --from-file=config.yaml=config.yaml \
  -n openstack
```

**Step 2:** Deploy the InstanceHa CR:

```yaml
apiVersion: instanceha.openstack.org/v1beta1
kind: InstanceHa
metadata:
  name: instanceha
  namespace: openstack
spec:
  openStackCloud: default
  openStackConfigMap: openstack-config
  openStackConfigSecret: openstack-config-secret
  fencingSecret: fencing-secret
  instanceHaConfigMap: instanceha-config
  networkAttachments:
    - internalapi
```

```bash
oc apply -f instanceha.yaml
```

**Step 3:** Verify deployment:

```bash
# Check pod is running
oc get pods -l service=instanceha -n openstack

# Check logs for successful startup
oc logs deployment/instanceha | grep -i "initialization\|login successful"

# Verify first poll completed
oc logs deployment/instanceha | grep "Poll cycle completed"

# Check readiness
oc get deployment instanceha -o jsonpath='{.status.conditions[?(@.type=="Available")].status}'
# Should return: True
```

### Upgrade Procedure

InstanceHA is automatically upgraded when you update the OpenStack operators and controlplane.

InstanceHA uses a Recreate deployment strategy (single replica). During upgrade, the old pod terminates before the new pod starts, creating a brief monitoring gap (typically 60-120 seconds).

**Before upgrading the OpenStack control plane, verify no evacuations are in progress:**

```bash
# Check for recent evacuation events
oc get events -n openstack \
  --field-selector involvedObject.name=instanceha,reason=EvacuationStarted \
  --sort-by='.lastTimestamp' | tail -10

# Check in-flight work
oc exec deployment/instanceha -- \
  curl -s http://localhost:8080/metrics | grep instanceha_hosts_processing
# Should be 0
```

**If evacuations are in progress:**

- Wait for them to complete before proceeding with the upgrade
- Check event timestamps to determine if the evacuation is recent or stale
- Review logs to confirm evacuation status

**Upgrade the OpenStack control plane:**

```bash
# Update the OpenStackVersion CR
oc patch openstackversion openstack --type merge \
  -p '{"spec": {"targetVersion": "0.5.0"}}'
```

The openstack-operator will update all component operators, including infra-operator, which triggers the InstanceHA pod restart.

**Monitor the upgrade:**

```bash
# Watch the deployment rollout
oc get deployment instanceha -n openstack -w

# Verify startup reconciliation after new pod starts
oc logs deployment/instanceha | grep -i "orphan\|reconcil"

# Verify poll cycle running
oc logs deployment/instanceha | tail -20
```

**Monitoring gap:** Failures occurring during the 60-120 second upgrade window will be detected on the first poll after startup. Orphan reconciliation recovers any partially-completed fencing operations from the previous run.

### Configuration Changes

The InstanceHA controller watches the configuration ConfigMap and automatically restarts the pod when changes are detected. Changes take effect within 1-2 minutes.

To change operational settings:

```bash
# Edit the configmap
oc edit configmap instanceha-config

# The controller detects the change and restarts the pod automatically
# Monitor the restart
oc get pods -l service=instanceha -w
```

To verify the new configuration is active:

```bash
# Check pod restart timestamp
oc get pod -l service=instanceha -o jsonpath='{.items[0].status.startTime}'

# Verify configuration was loaded
oc logs deployment/instanceha | grep -i "config\|initialized"
```

**Note:** Changes to secrets (fencing credentials, OpenStack passwords) also trigger automatic pod restart when the secret content hash changes.

---

## Secure and Authenticate

**Job:** Configure secure authentication and communications for InstanceHA in production environments.

### Authentication Methods

#### Password Authentication (Default)

Uses username and password from `clouds.yaml` and `secure.yaml`:

```yaml
# clouds.yaml
clouds:
  default:
    auth:
      username: admin
      project_name: admin
      auth_url: https://keystone-public.openstack.svc:5000/v3
      user_domain_name: Default
      project_domain_name: Default
    region_name: regionOne

# secure.yaml
clouds:
  default:
    auth:
      password: <admin-password>
```

#### Application Credentials (Recommended for Production)

Application Credentials provide scoped, time-limited access that can be rotated independently:

**Step 1:** Create a KeystoneApplicationCredential CR:

```yaml
apiVersion: keystone.openstack.org/v1beta1
kind: KeystoneApplicationCredential
metadata:
  name: ac-instanceha
  namespace: openstack
spec:
  secret: osp-secret
  passwordSelector: InstanceHaPassword
  userName: instanceha
  roles:
    - admin
  expirationDays: 365
  gracePeriodDays: 182
```

The keystone-operator creates a secret (`ac-instanceha-secret`) with `AC_ID` and `AC_SECRET`.

**Step 2:** Configure the InstanceHa CR:

```yaml
spec:
  auth:
    applicationCredentialSecret: ac-instanceha-secret
```

The controller mounts the secret and sets `AC_ENABLED=True`. The agent uses the `v3applicationcredential` plugin with automatic fallback to password auth.

**Credential rotation:** The keystone-operator handles rotation automatically based on expiration settings. When rotated, the controller detects the secret change and restarts the pod.

### TLS Configuration

#### Nova API TLS

When OpenStack services use TLS, provide the CA bundle:

```yaml
spec:
  caBundleSecretName: combined-ca-bundle
```

The secret must contain a `tls-ca-bundle.pem` key with the CA certificate chain.

#### Metrics Endpoint TLS

When `OpenStackControlPlane` has pod-level TLS enabled:

```yaml
# OpenStackControlPlane
spec:
  tls:
    podLevel:
      enabled: true
```

The openstack-operator provisions a certificate for the metrics endpoint. The infra-operator auto-detects the secret (`cert-instanceha-metrics`) and enables HTTPS automatically.

Override the auto-detected secret:

```yaml
spec:
  metricsTLS:
    secretName: my-custom-metrics-cert
```

Configure TLS version and ciphers:

```yaml
spec:
  metricsTLS:
    minTLSVersion: "1.3"
    cipherSuites: "ECDHE-ECDSA-AES256-GCM-SHA384:ECDHE-RSA-AES256-GCM-SHA384"
```

### BMC Credential Security

Store BMC credentials in Kubernetes secrets with restricted RBAC:

```bash
# Create secret with restricted permissions
oc create secret generic fencing-secret \
  --from-file=fencing.yaml=fencing.yaml \
  -n openstack

# Verify only the InstanceHA ServiceAccount can read it
oc auth can-i get secret/fencing-secret \
  --as=system:serviceaccount:openstack:instanceha
# Should return: yes
```

For Redfish with self-signed certificates:

```yaml
FencingConfig:
  compute-0:
    agent: redfish
    tls: "true"
    # ... other settings
```

Set `SSL_VERIFY: true` (default) to validate certificates. Only disable for development:

```yaml
config:
  SSL_VERIFY: "false"  # NOT recommended for production
```

### Required Nova Permissions

InstanceHA requires the `admin` role on the service project. Minimum required operations:

| Operation | API Endpoint | Permission |
|-----------|--------------|------------|
| List compute services | `GET /os-services` | `admin` |
| Enable/disable services | `PUT /os-services/*` | `admin` |
| List servers (all projects) | `GET /servers?all_tenants=True` | `admin` |
| Evacuate servers | `POST /servers/*/action` (evacuate) | `admin` |
| List aggregates | `GET /os-aggregates` | `admin` |
| List flavors | `GET /flavors` | `admin` |
| List images | `GET /v2/images` (Glance) | `admin` |

### Barbican Integration for Encrypted Volumes

When instances use LUKS-encrypted volumes, grant the InstanceHA user permission to retrieve encryption keys:

```bash
# Create role
openstack role create key-manager:secret-reader

# Grant to InstanceHA user
openstack role add \
  --user <instanceha-user> \
  --project service \
  key-manager:secret-reader
```

Configure Barbican policy in the OpenStackControlPlane CR:

```yaml
spec:
  barbican:
    customServiceConfig: |
      [oslo_policy]
      policy_file = /etc/barbican/policy.yaml
    defaultConfigOverwrite:
      policy.yaml: |
        "secret:get": "role:admin or rule:secret_project_admin or rule:secret_acl_read or role:key-manager:secret-reader"
        "secret:decrypt": "rule:secret_project_admin or rule:secret_acl_read or role:key-manager:secret-reader"
```

Without this, evacuations of instances with encrypted volumes fail with Barbican 403 errors.

---

## Reference

### Configuration Parameters

| Parameter | Type | Default | Range | Description |
|-----------|------|---------|-------|-------------|
| **Detection** | | | | |
| `POLL` | int | 45 | 15-600 | Seconds between Nova API polls |
| `DELTA` | int | 30 | 10-300 | Staleness threshold in seconds |
| `CHECK_HEARTBEAT` | bool | false | - | Enable heartbeat verification |
| `HEARTBEAT_TIMEOUT` | int | 120 | 30-600 | Heartbeat staleness threshold |
| `CHECK_KDUMP` | bool | false | - | Enable kdump detection |
| `KDUMP_TIMEOUT` | int | 30 | 5-300 | Kdump wait timeout |
| **Safety** | | | | |
| `THRESHOLD` | int | 50 | 0-100 | Global failure percentage threshold |
| `MAX_HOSTS_PER_CYCLE` | int | 10 | 1-200 | Maximum hosts fenced per cycle |
| `HEARTBEAT_CLIFF_THRESHOLD` | int | 50 | 10-100 | Heartbeat drop percentage for cliff detection |
| `HEARTBEAT_CLIFF_MAX_CYCLES` | int | 3 | 1-20 | Cycles before cliff detection expires |
| `K8S_API_CHECK_INTERVAL` | int | 15 | 5-120 | K8s API connectivity check interval |
| **Fencing** | | | | |
| `FENCING_TIMEOUT` | int | 30 | 5-120 | Per-host fencing timeout |
| `DELAY` | int | 0 | 0-300 | Delay between fencing and evacuation |
| **Evacuation** | | | | |
| `SMART_EVACUATION` | bool | false | - | Enable migration tracking |
| `ORCHESTRATED_RESTART` | bool | false | - | Enable priority-ordered evacuation |
| `WORKERS` | int | 4 | 1-100 | Parallel fencing operations |
| `EVACUATION_MAX_THREADS` | int | 32 | 1-512 | Total evacuation thread budget |
| `EVACUATION_RETRIES` | int | 5 | 1-20 | Per-instance retry attempts |
| `EVACUATION_STAGGER` | int | 0 | 0-60 | Seconds between instance evacuations |
| **Filters** | | | | |
| `TAGGED_FLAVORS` | bool | true | - | Filter by flavor tag |
| `TAGGED_IMAGES` | bool | true | - | Filter by image tag |
| `TAGGED_AGGREGATES` | bool | true | - | Filter by aggregate tag |
| `EVACUABLE_TAG` | string | evacuable | - | Tag name for evacuable resources |
| `SKIP_SERVERS_WITH_NAME` | list | [] | - | Glob patterns to exclude |
| **Recovery** | | | | |
| `LEAVE_DISABLED` | bool | false | - | Keep hosts disabled after evacuation |
| `FORCE_ENABLE` | bool | false | - | Re-enable without waiting for migrations |
| `RESERVED_HOSTS` | bool | false | - | Enable reserved host management |
| `FORCE_RESERVED_HOST_EVACUATION` | bool | false | - | Direct evacuations to reserved host |
| **Operations** | | | | |
| `DISABLED` | bool | false | - | Disable fencing and evacuation |
| `LOGLEVEL` | string | INFO | - | Log level (DEBUG, INFO, WARNING, ERROR) |
| `SSL_VERIFY` | bool | true | - | Verify TLS certificates |

### Kubernetes Events

| Event | Type | Trigger |
|-------|------|---------|
| `HostDown` | Warning | Host detected as down |
| `FencingStarted` | Normal | Fencing initiated |
| `FencingSucceeded` | Normal | Host powered off successfully |
| `FencingFailed` | Warning | Fencing operation failed |
| `EvacuationStarted` | Normal | Evacuation started |
| `EvacuationSucceeded` | Normal | Evacuation completed |
| `EvacuationFailed` | Warning | Evacuation failed |
| `HostReenabled` | Normal | Host re-enabled after recovery |
| `ThresholdExceeded` | Warning | Global threshold breached |
| `AggregateThresholdExceeded` | Warning | Per-aggregate threshold breached |
| `HeartbeatCliff` | Warning | Mass heartbeat loss detected |
| `AllServicesStale` | Warning | All services appear down |
| `FencingRateLimited` | Warning | Per-cycle cap hit |
| `OrphanedHostRecovered` | Warning | Startup reconciliation recovered host |
