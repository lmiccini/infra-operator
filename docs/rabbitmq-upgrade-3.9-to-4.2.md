# RabbitMQ Upgrade: 3.9 to 4.2 and Mirrored-to-Quorum Queue Migration

## Overview

The upgrade from RabbitMQ 3.9 to 4.2 involves two major changes:

1. **Version upgrade**: RabbitMQ 3.9 -> 4.2 (major version change)
2. **Queue type migration**: Mirrored (classic HA) queues -> Quorum queues (mandatory for 4.x, which dropped mirrored queue support)

These changes are managed across two operators:

- **openstack-operator**: Top-level orchestrator that sets `TargetVersion` and container images on the `RabbitMq` CR
- **infra-operator**: Owns the `RabbitMq` CRD and controller, manages the actual StatefulSet, storage wipe, proxy sidecar, and queue type transitions

## Architecture

### Key CRDs and Fields

```
OpenStackVersion (openstack-operator)
  Status.ServiceDefaults.RabbitmqVersion  -> "4.2" (default for new deployments)
  Status.ContainerImages.RabbitmqImage    -> container image URL

OpenStackControlPlane (openstack-operator)
  Spec.Rabbitmq.Templates                 -> map of RabbitMqSpecCore per cluster

RabbitMq (infra-operator, rabbitmq.openstack.org/v1beta1)
  Spec.TargetVersion      -> desired version (e.g., "4.2"), set by openstack-operator
  Spec.QueueType           -> "Mirrored" or "Quorum"
  Spec.ContainerImage      -> RabbitMQ container image

  Status.CurrentVersion    -> currently deployed version
  Status.QueueType         -> active queue type
  Status.UpgradePhase      -> "", "DeletingResources", or "WaitingForCluster"
  Status.WipeReason        -> "", "VersionUpgrade", or "QueueTypeMigration"
  Status.ProxyRequired     -> "True" when AMQP proxy sidecar is needed
```

## How the Version is Propagated

1. **openstack-operator** initializes `OpenStackVersion.Status.ServiceDefaults.RabbitmqVersion` to `"4.2"` for new deployments (FR5 default) in `InitializeOpenStackVersionServiceDefaults()`.

2. When reconciling each RabbitMQ cluster, `reconcileRabbitMQ()` in `openstack-operator/internal/openstack/rabbitmq.go` sets:
   ```go
   if version.Status.ServiceDefaults.RabbitmqVersion != nil {
       rabbitmq.Spec.TargetVersion = version.Status.ServiceDefaults.RabbitmqVersion
   } else {
       rabbitmq.Spec.TargetVersion = ptr.To("3.9")
   }
   rabbitmq.Spec.ContainerImage = *version.Status.ContainerImages.RabbitmqImage
   ```

3. The **infra-operator** RabbitMq controller detects the version change and manages the upgrade.

## Upgrade Flow (Step by Step)

### Phase 0: Version Initialization

When the infra-operator controller first reconciles a `RabbitMq` CR with an empty `Status.CurrentVersion`:

- If an existing StatefulSet is found (pre-migration cluster), `CurrentVersion` is set to `"3.9"` (all pre-migration clusters ran 3.9).
- If no StatefulSet exists (new deployment), `CurrentVersion` is set from `Spec.TargetVersion` or defaults to `"4.2"`.

This is handled at the top of `Reconcile()` in `rabbitmq_controller.go:178-202`.

### Phase 1: Detecting the Need for Storage Wipe

The controller checks if a storage wipe is required:

```go
needsWipe, _ := rabbitmq.RequiresStorageWipe(instance.Status.CurrentVersion, *instance.Spec.TargetVersion)
```

`RequiresStorageWipe()` returns `true` for any major or minor version change (e.g., 3.9 -> 4.2). Patch-only changes (e.g., 4.2.0 -> 4.2.1) do not require a wipe.

A separate check handles standalone Mirrored -> Quorum migration (same version):
```go
if instance.Status.QueueType == QueueTypeMirrored && *instance.Spec.QueueType == QueueTypeQuorum {
    requiresWipe = true
    instance.Status.WipeReason = WipeReasonQueueTypeMigration
}
```

### Phase 2: Queue Type Forcing via Webhook

The defaulting webhook (`rabbitmq_webhook.go:186-195`) automatically forces `QueueType` from `Mirrored` to `Quorum` when `TargetVersion` is 4.x+:

```go
if *spec.QueueType == QueueTypeMirrored && IsVersion4OrLater(*spec.TargetVersion) {
    queueType := QueueTypeQuorum
    spec.QueueType = &queueType
}
```

The validation webhook rejects any attempt to set `QueueType=Mirrored` with a 4.x+ target version as a safety net.

### Phase 3: Storage Wipe - DeletingResources

When a wipe is needed, the controller sets `Status.UpgradePhase = "DeletingResources"` and performs:

1. **Deletes the `ha-all` policy** (if migrating from Mirrored queues) via the `RabbitMQPolicy` CR.

2. **Deletes the StatefulSet** (not an update -- full deletion to avoid partial wipes from rolling updates).

3. **Labels pods with `skipPreStopChecks=true`** so the PreStop hook exits immediately (bypassing the `rabbitmq-upgrade await_online_quorum_plus_one` checks that would block shutdown).

4. **Re-deletes pods with 30s grace period** to override the StatefulSet's `terminationGracePeriodSeconds` (default 60s).

5. **Enables the AMQP proxy** if migrating to Quorum queues:
   ```go
   if isVersionUpgradeWithMigration || isQueueTypeMigration {
       instance.Status.ProxyRequired = "True"
   }
   ```

6. **Updates `Status.QueueType`** to match the new spec.

7. **Advances to `Status.UpgradePhase = "WaitingForCluster"`**.

### Phase 4: Storage Wipe - WaitingForCluster

On the next reconcile, the controller enters the normal StatefulSet creation path. The key difference is:

- A **`wipe-data` init container** is prepended to the pod spec. This container:
  - Runs before the `setup-container` init container
  - Deletes all contents of `/var/lib/rabbitmq`
  - Creates a version-specific marker file (`.operator-wipe-4.2`) to prevent re-wipes on pod restarts

  ```bash
  MARKER="${WIPE_DIR}/.operator-wipe-4.2"
  if [ -f "$MARKER" ]; then exit 0; fi
  rm -rf "${WIPE_DIR}"/*
  rm -rf "${WIPE_DIR}"/.[!.]*
  touch "$MARKER"
  ```

- The **AMQP proxy sidecar** is included in the pod spec if `Status.ProxyRequired == "True"`.

- **Version-specific configuration** is applied (e.g., `queue_leader_locator` instead of `queue_master_locator` for 4.x).

When all StatefulSet replicas become ready:
- `Status.CurrentVersion` is updated to the target version
- `Status.UpgradePhase` is cleared to `""` (None)
- `Status.WipeReason` is cleared

### Phase 5: Post-Upgrade (Proxy Active)

After the cluster is ready with RabbitMQ 4.2 and Quorum queues, the AMQP proxy sidecar remains active until dataplane clients are reconfigured.

## The AMQP Proxy Sidecar

### Purpose

The proxy solves a critical compatibility problem: existing OpenStack services (both controlplane and dataplane) declare queues with `durable=False` (non-durable), which is incompatible with quorum queues (which are always durable). Without the proxy, clients would get errors when trying to declare queues.

### How It Works

The proxy is a Python script (`proxy.py`) that runs as a sidecar container in each RabbitMQ pod:

1. **Listens** on the standard AMQP port (5672 or 5671 with TLS) on all interfaces
2. **Forwards** connections to RabbitMQ on a backend port (55672) on localhost
3. **Rewrites AMQP frames**: Intercepts `queue.declare` and `exchange.declare` frames and forces `durable=True`, allowing non-durable clients to work with durable quorum queues

When proxy is enabled:
- RabbitMQ binds its AMQP listener to **localhost only** (backend port 55672)
- The proxy binds to **all interfaces** on the standard port
- Management and Prometheus ports remain unchanged
- RabbitMQ is configured with `quorum_queue.property_equivalence.relaxed_checks_on_redeclaration = true` to tolerate queue redeclarations with different properties

### Network Architecture (Proxy Mode)

```
Client -> [proxy :5672/5671] -> [RabbitMQ :55672 localhost]
                                 [RabbitMQ :15672 management] <- direct
                                 [RabbitMQ :15692 prometheus] <- direct
```

### Removing the Proxy

The proxy is removed through a two-step annotation-based process:

1. An external actor (admin or automation) sets the annotation `rabbitmq.openstack.org/clients-reconfigured: "true"` on the `RabbitMq` CR after confirming all dataplane clients have been reconfigured to use durable queues.

2. The controller detects the annotation and:
   - Sets `Status.ProxyRequired = "False"`
   - Removes the annotation
   - On the next reconcile, the proxy sidecar is removed from the StatefulSet
   - RabbitMQ resumes listening on all interfaces directly

## Migration from rabbitmq-cluster-operator

The infra-operator also handles migration from the old `rabbitmq-cluster-operator` (rabbitmq.com CRDs). This is separate from the version upgrade but often happens together:

### Resource Adoption

The `adoptResource()` function removes foreign controller owner references from resources (StatefulSet, Services, Secrets, ConfigMaps) so the new `RabbitMq` CR can take ownership.

### VolumeClaimTemplate Fix

Old StatefulSets may have `ownerReferences` from the old `rabbitmq.com/v1beta1` RabbitmqCluster in their `volumeClaimTemplates`. Since VCTs are immutable, the controller:
1. Cleans stale owner references from existing PVCs
2. Orphan-deletes the StatefulSet (keeping pods running)
3. Recreates the StatefulSet with clean VCTs on the next reconcile

### Old CR Cleanup

After resources are reparented, `cleanupOldRabbitmqClusterCR()`:
1. Strips old owner references from PVCs (to prevent cascade deletion)
2. Removes finalizers from the old `RabbitmqCluster` CR
3. Deletes the old CR

### openstack-operator Side

The `reconcileRabbitMQ()` function in openstack-operator also handles migration:
- `removeRabbitmqClusterControllerReference()`: Removes the old controller reference from the `rabbitmq.com` `RabbitmqCluster` CR
- `removeConfigMapControllerReference()`: Removes the old controller reference from the config-data ConfigMap

## Version-Specific Configuration Differences

| Setting | RabbitMQ 3.x | RabbitMQ 4.x |
|---------|-------------|-------------|
| Queue locator | `queue_master_locator = min-masters` | `queue_leader_locator = balanced` |
| TLS versions (non-FIPS) | `['tlsv1.2']` | `['tlsv1.2','tlsv1.3']` |
| Inter-node TLS verify | `verify_none` | `verify_none` (OTP 26 static config lacks wildcard match support) |
| Peer discovery | K8s API-based | Seed-node approach (k8s config silently ignored) |
| Mirrored queues | Supported via `ha-all` policy | Not supported (removed) |
| Relaxed quorum checks | N/A | Enabled during migration (proxy active) |

## Webhook Protections

### Defaulting Webhook
- New clusters default to `QueueType=Quorum`
- Existing clusters without `QueueType` default to `Mirrored`
- `Mirrored` is forced to `Quorum` when `TargetVersion` is 4.x+
- Preserves user-specified `QueueType`

### Validation Webhook
- Rejects `QueueType=Mirrored` with `TargetVersion` 4.x+
- Rejects version downgrades (e.g., 4.2 -> 3.9)
- Rejects scale-down (reducing replicas)
- Validates `QueueType` is one of `Mirrored` or `Quorum`

## Upgrade State Machine

```
                    TargetVersion changed
                    or QueueType Mirrored->Quorum
                            |
                            v
    UpgradePhase: None ──> DeletingResources
                            |
                            | Delete STS, pods, ha-all policy
                            | Set ProxyRequired=True (if migrating to Quorum)
                            | Update Status.QueueType
                            v
                       WaitingForCluster
                            |
                            | Recreate STS with wipe-data init container
                            | + proxy sidecar (if ProxyRequired)
                            | Wait for all replicas ready
                            v
                    UpgradePhase: None
                    CurrentVersion = TargetVersion
                    WipeReason = ""
                            |
                            | (proxy still active if ProxyRequired=True)
                            |
                            v
                    Annotation: clients-reconfigured=true
                            |
                            | Clear ProxyRequired
                            | Remove proxy sidecar
                            v
                         Complete
```

## Summary

The upgrade is a destructive operation by design: RabbitMQ data is wiped clean because in-place major version upgrades are unreliable and mirrored-to-quorum queue migration requires starting fresh. The AMQP proxy sidecar provides backward compatibility during the transition window, allowing existing OpenStack clients to continue operating with non-durable queue declarations against the new quorum-queue-only RabbitMQ 4.2 cluster. Once all clients are reconfigured to use durable queues natively, the proxy is removed and the migration is complete.
