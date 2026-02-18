# AMQP Proxy Sidecar Integration

## Overview

The RabbitMQ controller automatically deploys an AMQP durability proxy as a sidecar container during RabbitMQ 3.x → 4.x upgrades. This proxy enables seamless migration from mirrored queues (RabbitMQ 3.x) to quorum queues (RabbitMQ 4.x) without requiring immediate OpenStack dataplane reconfiguration.

The proxy persists after the upgrade completes (via `Status.ProxyRequired=true`) until explicitly removed by setting the `clients-reconfigured=true` annotation.

## Problem Solved

When migrating from RabbitMQ 3.9 (mirrored queues) to 4.2 (quorum queues), external OpenStack services have `amqp_durable_queues=false` configured. However, quorum queues require `durable=True`, causing `PRECONDITION_FAILED` errors. The proxy transparently rewrites AMQP frames to force durability, allowing non-durable clients to work with durable quorum queues.

## Architecture

```
┌─────────────────────────────────────────┐
│ RabbitMQ Pod                            │
│                                         │
│  ┌───────────────────┐                  │
│  │ amqp-proxy        │  Port: 5672      │
│  │ (with TLS)        │  (external)      │
│  └─────────┬─────────┘                  │
│            │ localhost:5673             │
│            ↓ (no TLS)                   │
│  ┌───────────────────┐                  │
│  │ rabbitmq          │  Port: 5673      │
│  │                   │  (localhost)     │
│  └───────────────────┘                  │
└─────────────────────────────────────────┘
         ↑ Port 5672 (TLS)
         │
   External clients
```

## How It Works

1. **Automatic Activation**: Proxy sidecar is automatically added for **3.x → 4.x upgrades** when:
   - Upgrading from RabbitMQ 3.x to 4.x with Quorum queue migration
   - `Status.ProxyRequired=true` is set and persists after upgrade completes
   - Manual override via annotation `rabbitmq.openstack.org/enable-proxy=true`

   The proxy is **disabled** when:
   - Annotation `rabbitmq.openstack.org/clients-reconfigured=true` is set

2. **TLS Handling**:
   - Proxy terminates TLS on port 5672 (using certs from `Spec.TLS.SecretName`)
   - RabbitMQ listens on localhost:5673 without TLS
   - External clients connect to proxy with TLS

3. **Frame Rewriting**: Proxy intercepts AMQP frames and:
   - Rewrites `queue.declare(durable=False)` → `durable=True` + `x-queue-type=quorum`
   - Rewrites `exchange.declare(durable=False)` → `durable=True`
   - Skips reply queues and system queues

4. **Automatic Removal**: Proxy is removed after:
   - Dataplane is reconfigured with `amqp_durable_queues=true`
   - User sets annotation `rabbitmq.openstack.org/clients-reconfigured=true`

5. **Persistent State Tracking**: Uses `Status.ProxyRequired` field:
   - Set to `true` during 3.x → 4.x upgrade with Quorum migration
   - Persists across reconciliations, pod restarts, and operator restarts
   - Ensures proxy remains active after upgrade completes
   - Cleared only when `clients-reconfigured=true` annotation is set

## Files Created/Modified

### New Files
- `internal/controller/rabbitmq/proxy.go` - Proxy sidecar integration logic
- `internal/controller/rabbitmq/data/proxy.py` - AMQP proxy script (embedded)
- `internal/controller/rabbitmq/data/README.md` - Documentation for data files
- `PROXY_INTEGRATION.md` - Complete integration documentation

### Modified Files
- `internal/controller/rabbitmq/rabbitmq_controller.go` (lines 867-894):
  - Added proxy sidecar logic after `ConfigureCluster`
  - Manages `Status.ProxyRequired` flag for persistent proxy tracking
  - Calls `ensureProxyConfigMap`, `addProxySidecar`, `configureRabbitMQBackendPort`
  - Removes proxy when `shouldEnableProxy()` returns false

## Proxy Activation Logic

The `shouldEnableProxy()` function determines when the proxy should be active:

```go
func shouldEnableProxy(instance *RabbitMq) bool {
    // 1. Disable if clients are reconfigured
    if annotations["clients-reconfigured"] == "true" {
        return false
    }

    // 2. Enable if manual override is set
    if annotations["enable-proxy"] == "true" {
        return true
    }

    // 3. Enable if ProxyRequired status flag is set (persists after upgrade)
    if instance.Status.ProxyRequired {
        return true
    }

    // 4. Enable during 3.x → 4.x upgrade with Quorum migration
    if instance.Status.UpgradePhase != "" &&
       instance.Spec.QueueType == "Quorum" &&
       currentVersion starts with "3." &&
       targetVersion starts with "4." {
        return true
    }

    return false
}
```

**Priority Order**:
1. `clients-reconfigured=true` → **Always disable** (highest priority)
2. `enable-proxy=true` → **Force enable** (manual override)
3. `Status.ProxyRequired=true` → **Enable** (automatic, persists after upgrade)
4. Active 3.x → 4.x upgrade → **Enable** (sets ProxyRequired=true)

## Key Functions

### proxy.go

- `ensureProxyConfigMap(ctx, instance, helper)` - Creates ConfigMap with embedded proxy script
- `addProxySidecar(instance, cluster)` - Adds proxy container to StatefulSet
- `buildProxySidecarContainer(instance)` - Builds container spec with TLS support
- `removeProxySidecar(cluster)` - Removes proxy sidecar
- `shouldEnableProxy(instance)` - Determines when proxy should be enabled (see logic above)
- `configureRabbitMQBackendPort(instance, cluster)` - Configures RabbitMQ to listen on localhost:5673

## Container Spec

- **Image**: `quay.io/openstack-k8s-operators/openstack-operator-client:latest`
- **Command**: `python3 /scripts/proxy.py`
- **Args**:
  - `--backend localhost:5673`
  - `--listen 0.0.0.0:5672`
  - `--tls-cert /etc/rabbitmq-tls/tls.crt` (if TLS enabled)
  - `--tls-key /etc/rabbitmq-tls/tls.key` (if TLS enabled)
  - `--tls-ca /etc/rabbitmq-tls-ca/ca.crt` (if CA specified)
- **Resources**:
  - Requests: 128Mi memory, 100m CPU
  - Limits: 256Mi memory, 500m CPU
- **Probes**: TCP liveness and readiness on port 5672

## Usage

### Automatic (during 3.x → 4.x upgrade)

The proxy is automatically enabled when upgrading from RabbitMQ 3.x to 4.x:

```yaml
apiVersion: rabbitmq.openstack.org/v1beta1
kind: RabbitMq
metadata:
  name: rabbitmq
  annotations:
    rabbitmq.openstack.org/target-version: "4.2"
spec:
  queueType: Quorum  # Automatically set when upgrading 3.x → 4.x
  tls:
    secretName: rabbitmq-tls
status:
  currentVersion: "3.9"  # Detected from existing cluster
  proxyRequired: false   # Will be set to true during upgrade
```

After the upgrade completes:

```yaml
status:
  currentVersion: "4.2"
  proxyRequired: true    # Keeps proxy active until clients reconfigured
  upgradePhase: ""       # Upgrade complete
```

### Manual activation

Force proxy activation with annotation:

```bash
kubectl annotate rabbitmq rabbitmq \
  rabbitmq.openstack.org/enable-proxy=true
```

### Manual deactivation

After dataplane reconfiguration:

```bash
kubectl annotate rabbitmq rabbitmq \
  rabbitmq.openstack.org/clients-reconfigured=true
```

## Monitoring

Check if proxy is active:
```bash
kubectl get rabbitmq rabbitmq -o jsonpath='{.status.proxyRequired}'
# true = proxy is active
# false or empty = proxy is not active
```

Check proxy logs:
```bash
kubectl logs rabbitmq-server-0 -c amqp-proxy
```

Proxy prints statistics every 5 minutes:
```
=== Proxy Statistics ===
Total connections: 25
Queue rewrites: 120
Exchange rewrites: 15
Bytes forwarded: 5,432,100
```

Check upgrade progress:
```bash
kubectl get rabbitmq rabbitmq -o jsonpath='{.status.upgradePhase}'
# DeletingResources = Deleting old cluster
# WaitingForPods = Waiting for pod termination
# WaitingForCluster = Recreating cluster with new version
# (empty) = Upgrade complete
```

## Upgrade Workflow

### Complete 3.x → 4.x Upgrade Process

1. **Start upgrade**: Set `target-version: "4.2"` annotation on RabbitMq CR
2. **Storage wipe phase 1** (DeletingResources):
   - Backup default user credentials
   - Delete ha-all mirrored queue policy
   - Delete RabbitMQCluster (triggers StatefulSet deletion)
3. **Storage wipe phase 2** (WaitingForPods):
   - Wait for all pods to terminate (respects 600s grace period)
4. **Storage wipe phase 3** (WaitingForCluster):
   - Controller recreates RabbitMQCluster with version 4.2
   - Temporary annotation `storage-wipe-needed=true` is set
   - Init container wipes `/var/lib/rabbitmq/*` and creates version marker
   - `Status.ProxyRequired=true` is set during upgrade
   - Proxy sidecar is automatically added
   - Wait for cluster to become ready
   - Restore default user credentials via RabbitMQUser
   - Update `Status.CurrentVersion=4.2`
   - Clear `UpgradePhase=""` (upgrade complete)
   - Remove `storage-wipe-needed` annotation
5. **External services**: Continue using `amqp_durable_queues=false`
   - Proxy remains active because `Status.ProxyRequired=true`
6. **Proxy rewrites**: Frames rewritten to use durable queues
7. **Dataplane reconfig**: Update OpenStack services to `amqp_durable_queues=true`
8. **Set annotation**: `clients-reconfigured=true` → Clears `Status.ProxyRequired`
9. **Proxy removed**: Next reconciliation removes proxy sidecar
10. **Direct connection**: Services connect directly to RabbitMQ 4.2

## Important Notes

### Proxy Script Location

The proxy script is located at:
- `internal/controller/rabbitmq/data/proxy.py`

When updating the proxy:
1. Edit `internal/controller/rabbitmq/data/proxy.py`
2. Rebuild: `make`
3. Test: `make test`

### Performance

- Proxy adds ~0.05ms latency (localhost forwarding)
- Memory overhead: ~64Mi per RabbitMQ pod
- CPU overhead: ~20m per RabbitMQ pod

### Security

- Runs as non-root
- No privilege escalation
- Drops all capabilities
- TLS certificates mounted read-only

## Testing

### Go Unit Tests

Proxy sidecar integration tests verify:
- ✅ Proxy is added during upgrades
- ✅ TLS configuration is correct
- ✅ Proxy is removed after client reconfiguration
- ✅ Manual activation via annotations

Run tests:
```bash
make test
# Test Suite Passed
# composite coverage: 72.7% of statements

# Run only proxy tests
cd test/functional
ginkgo -focus="RabbitMQ Proxy"
```

### Python Integration Test

Simulates Oslo messaging client with `amqp_durable_queues=false` connecting through proxy to RabbitMQ with quorum queues.

Quick test:
```bash
cd internal/controller/rabbitmq/data
python3 proxy_test.py --host localhost --port 5672
```

For complete testing documentation, see [TESTING.md](internal/controller/rabbitmq/data/TESTING.md).

## References

- Proxy implementation: `internal/controller/rabbitmq/data/proxy.py`
- Proxy integration: `internal/controller/rabbitmq/proxy.go`
- AMQP 0-9-1 spec: https://www.rabbitmq.com/resources/specs/amqp0-9-1.pdf
