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
│  │ amqp-proxy        │  Port: 5671(TLS) │
│  │                   │  or 5672 (plain) │
│  └─────────┬─────────┘                  │
│            │ localhost:5673             │
│            ↓ (no TLS)                   │
│  ┌───────────────────┐                  │
│  │ rabbitmq          │  Port: 5673      │
│  │                   │  (localhost)     │
│  └───────────────────┘                  │
└─────────────────────────────────────────┘
         ↑ Port 5671 (TLS) or 5672 (plain)
         │
   External clients
```

## How It Works

1. **Automatic Activation**: Proxy sidecar is automatically added for **3.x → 4.x upgrades** when:
   - Upgrading from RabbitMQ 3.x to 4.x with Quorum queue migration
   - `Status.ProxyRequired=true` is set and persists after upgrade completes

   The proxy is **disabled** when:
   - Annotation `rabbitmq.openstack.org/clients-reconfigured=true` is set

2. **TLS Handling**:
   - Proxy terminates TLS on port 5671 (using certs from `Spec.TLS.SecretName`), or listens on port 5672 without TLS
   - RabbitMQ listens on localhost:5673 without TLS
   - External clients connect to the proxy port (5671 with TLS, 5672 without)

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

## Proxy Activation Logic

The `shouldEnableProxy()` function determines when the proxy should be active:

```go
func shouldEnableProxy(instance *RabbitMq) bool {
    // ProxyRequired is set during 3.x → 4.x upgrades and cleared when
    // the clients-reconfigured annotation is set (handled early in reconcile).
    return instance.Status.ProxyRequired
}
```

The `clients-reconfigured` annotation is handled early in the reconciler (before
`shouldEnableProxy` is called), which sets `ProxyRequired=false` and removes the
annotation. So `shouldEnableProxy` only needs to check `ProxyRequired`.

## Key Functions

### proxy.go

- `ensureProxyConfigMap(ctx, instance, helper)` - Creates ConfigMap with embedded proxy script
- `addProxySidecar(ctx, instance, cluster)` - Adds proxy container, configures backend port, and updates readiness probe
- `buildProxySidecarContainer(instance)` - Builds container spec with TLS support
- `shouldEnableProxy(instance)` - Determines when proxy should be enabled (see logic above)

## Container Spec

- **Image**: `instancehav1beta1.InstanceHaContainerImage` (shared with InstanceHa)
- **Command**: `python3 /scripts/proxy.py`
- **Args**:
  - `--backend localhost:5673`
  - `--listen 0.0.0.0:5671` (TLS) or `0.0.0.0:5672` (plain)
  - `--log-level INFO`
  - `--stats-interval 300`
  - `--tls-cert /etc/rabbitmq-tls/tls.crt` (if TLS enabled)
  - `--tls-key /etc/rabbitmq-tls/tls.key` (if TLS enabled)
  - Note: `--tls-ca` is NOT passed (no client certificate verification)
- **Resources**:
  - Requests: 512Mi memory, 500m CPU
  - Limits: 2Gi memory, 2000m CPU
- **Probes**: TCP liveness and readiness on the proxy listen port (5671 or 5672)

## Usage

### Automatic (during 3.x → 4.x upgrade)

The proxy is automatically enabled when upgrading from RabbitMQ 3.x to 4.x:

```yaml
apiVersion: rabbitmq.openstack.org/v1beta1
kind: RabbitMq
metadata:
  name: rabbitmq
spec:
  targetVersion: "4.2"
  queueType: Quorum
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
# DeletingResources = Deleting ha-all policy and StatefulSet
# WaitingForCluster = Waiting for cluster to become ready with new version
# (empty) = Upgrade complete
```

## Upgrade Workflow

### Complete 3.x → 4.x Upgrade Process

1. **Start upgrade**: Set `spec.targetVersion: "4.2"` on RabbitMq CR
2. **Storage wipe phase 1** (DeletingResources):
   - Delete ha-all mirrored queue policy
   - Update RabbitmqCluster spec (adds wipe-data init container)
   - Delete StatefulSet (cluster operator recreates it from updated spec)
3. **Storage wipe phase 2** (WaitingForCluster):
   - Init container wipes `/var/lib/rabbitmq/*` and creates version marker
   - `Status.ProxyRequired=true` is set during upgrade
   - Proxy sidecar is automatically added
   - Wait for cluster to become ready
   - Update `Status.CurrentVersion=4.2`
   - Clear `UpgradePhase` (upgrade complete)
4. **External services**: Continue using `amqp_durable_queues=false`
   - Proxy remains active because `Status.ProxyRequired=true`
5. **Proxy rewrites**: Frames rewritten to use durable queues
6. **Dataplane reconfig**: Update OpenStack services to `amqp_durable_queues=true`
7. **Set annotation**: `clients-reconfigured=true` → Clears `Status.ProxyRequired`
8. **Proxy removed**: Next reconciliation removes proxy sidecar
9. **Direct connection**: Services connect directly to RabbitMQ 4.2

## Proxy Script

The proxy script (`internal/controller/rabbitmq/data/proxy.py`) is embedded at compile time using Go's `embed` directive and deployed as a ConfigMap.

When updating the proxy:
1. Edit `internal/controller/rabbitmq/data/proxy.py`
2. Rebuild: `make`
3. Test: `make test`

### Performance

- Proxy adds ~0.05ms latency (localhost forwarding)
- Memory: 512Mi requested, 2Gi limit per RabbitMQ pod
- CPU: 500m requested, 2000m limit per RabbitMQ pod

### Security

- Runs as non-root
- No privilege escalation
- Drops all capabilities
- TLS certificates mounted read-only

## Testing

### Python Unit Tests

Unit tests (`internal/controller/rabbitmq/data/test_proxy_unit.py`) verify the AMQP protocol parsing and rewriting logic in `proxy.py` without requiring a running RabbitMQ instance. They cover:

- AMQPFrame parsing and serialization
- AMQPMethodParser field encoding/decoding (shortstr, table, all AMQP types)
- QueueDeclareRewriter (durable flag, x-queue-type injection)
- ExchangeDeclareRewriter (durable flag)
- DurabilityProxy frame routing and statistics
- `parse_host_port` for IPv4, IPv6, and hostname inputs
- End-to-end rewrite cycles

```bash
cd internal/controller/rabbitmq/data
python3 -m pytest test_proxy_unit.py -v

# Or without pytest
python3 -m unittest test_proxy_unit.py -v
```

### Go Functional Tests

Functional tests (`test/functional/rabbitmq_proxy_test.go`) verify that the operator correctly manages the proxy sidecar:

- Proxy sidecar is added during upgrade phase
- Proxy container has correct image and command
- TLS certificates are mounted properly
- Backend port is configured (localhost:5673)
- ConfigMap is created with proxy script
- Proxy is removed when `clients-reconfigured` annotation is set

```bash
# Run all tests
make test

# Run only proxy tests
make test GINKGO_ARGS='--focus="RabbitMQ Proxy"'
```

### Python Integration Test

The integration test (`internal/controller/rabbitmq/data/proxy_test.py`) simulates an Oslo messaging client with `amqp_durable_queues=false` connecting through the proxy to RabbitMQ with quorum queues.

#### Prerequisites

```bash
pip install pika
```

#### Local Testing

```bash
# Terminal 1: Start RabbitMQ with quorum queues
podman run -d --name rabbitmq \
  -p 5673:5672 \
  -p 15672:15672 \
  -e RABBITMQ_SERVER_ADDITIONAL_ERL_ARGS="-rabbit default_queue_type quorum" \
  rabbitmq:4.0-management

# Terminal 2: Start the proxy
cd internal/controller/rabbitmq/data
python3 proxy.py --backend localhost:5673 --listen 0.0.0.0:5672 --log-level DEBUG

# Terminal 3: Run the test
python3 proxy_test.py --host localhost --port 5672
```

#### Kubernetes Testing

```bash
# Port-forward to RabbitMQ with proxy
kubectl port-forward -n openstack rabbitmq-server-0 5672:5672

# Run test
python3 proxy_test.py --host localhost --port 5672 --tls
```

#### Expected Behavior Without Proxy

Running directly against RabbitMQ (bypassing the proxy) produces a `PRECONDITION_FAILED` error, confirming the proxy is required:

```bash
python3 proxy_test.py --host localhost --port 5673 --no-wait
# FAILURE: Channel closed by broker: (406, 'PRECONDITION_FAILED')
```

### Load Testing

The load test script (`test/functional/rabbitmq_proxy_load_test.py`) validates proxy performance with large numbers of concurrent connections (simulating 1000+ compute nodes).

#### Prerequisites

```bash
pip install pika psutil
```

#### Quick Start

```bash
# Get RabbitMQ credentials
kubectl get secret rabbitmq-default-user -n openstack -o jsonpath='{.data.username}' | base64 -d
kubectl get secret rabbitmq-default-user -n openstack -o jsonpath='{.data.password}' | base64 -d

# Run from a pod inside the cluster
kubectl run load-test -n openstack -it --rm --image=python:3.11 -- bash
pip install pika psutil

# 100-client test
python3 rabbitmq_proxy_load_test.py \
  --host rabbitmq.openstack.svc --port 5671 \
  --username <username> --password <password> \
  --clients 100 --messages 10

# 1000-client test
python3 rabbitmq_proxy_load_test.py \
  --host rabbitmq.openstack.svc --port 5671 \
  --username <username> --password <password> \
  --clients 1000 --messages 50 --ramp-up 10
```

#### Monitoring During Tests

```bash
# Monitor proxy resources (CSV output)
chmod +x test/functional/monitor_proxy.sh
./test/functional/monitor_proxy.sh rabbitmq-server-0 openstack 5

# Watch proxy logs
kubectl logs -n openstack rabbitmq-server-0 -c amqp-proxy -f

# Check RabbitMQ connections
kubectl exec -n openstack rabbitmq-server-0 -c rabbitmq -- \
  rabbitmqctl list_connections --formatter json | jq '.[] | .peer_host' | sort | uniq -c
```

#### Test Scenarios

| Scenario | Command | Purpose |
|----------|---------|---------|
| Connection storm | `--clients 1000 --ramp-up 0 --max-workers 200` | Worst case: all nodes restart simultaneously |
| Gradual ramp-up | `--clients 1000 --ramp-up 60 --max-workers 100` | Typical: nodes booting over 60 seconds |
| High throughput | `--clients 500 --messages 500 --ramp-up 30` | High messaging workload (e.g., Ceilometer) |

#### Success Criteria

- Success rate > 95%
- Mean connection time < 1s
- Peak proxy memory < 1.5GB (with 2GB limit)
- No connection timeouts or errors

#### Scaling Recommendations

| Compute Nodes | RabbitMQ Pods | Proxy Memory | Proxy CPU |
|---------------|---------------|--------------|-----------|
| 100-300       | 3             | 512Mi        | 500m      |
| 300-600       | 3-5           | 1Gi          | 1000m     |
| 600-1000      | 5             | 2Gi          | 2000m     |
| 1000+         | 5-7           | 2Gi          | 2000m     |

## Troubleshooting

### Proxy not rewriting frames (PRECONDITION_FAILED)

```bash
# Enable DEBUG logging on the proxy
python3 proxy.py --log-level DEBUG ...
# Should see: DEBUG - Rewriting queue.declare: ... (durable=False -> True, quorum)
```

### Connection refused

```bash
# Check if proxy is running
kubectl logs -n openstack rabbitmq-server-0 -c amqp-proxy

# Verify proxy is listening
kubectl exec rabbitmq-server-0 -c amqp-proxy -- netstat -tlnp | grep 5671
```

### Proxy crashing (OOMKilled)

Increase memory limits in `proxy.go` (`buildProxySidecarContainer`), or reduce concurrent clients.

### RabbitMQ backend unreachable

```bash
# From proxy container
kubectl exec rabbitmq-server-0 -c amqp-proxy -- timeout 1 bash -c '</dev/tcp/127.0.0.1/5673'
```

## References

- Proxy implementation: `internal/controller/rabbitmq/data/proxy.py`
- Proxy integration: `internal/controller/rabbitmq/proxy.go`
- Go functional tests: `test/functional/rabbitmq_proxy_test.go`
- Python unit tests: `internal/controller/rabbitmq/data/test_proxy_unit.py`
- Python integration test: `internal/controller/rabbitmq/data/proxy_test.py`
- Load test script: `test/functional/rabbitmq_proxy_load_test.py`
- Load test monitor: `test/functional/monitor_proxy.sh`
- AMQP 0-9-1 spec: https://www.rabbitmq.com/resources/specs/amqp0-9-1.pdf
