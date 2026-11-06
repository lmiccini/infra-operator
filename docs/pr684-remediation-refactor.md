# RabbitMQ PVC remediation — refactor design notes

Design rationale and outcomes for the refactor applied on top of PR #684
("Protect RabbitMQ quorum during local PVC remediation"). It records the review
findings, the decisions and *why* they were made, and the resulting behaviour, so
reviewers and future maintainers can follow the reasoning without re-deriving it.

- **Upstream PR:** https://github.com/openstack-k8s-operators/infra-operator/pull/684
- **Jira:** OSPRH-36533
- **Builds on:** PR #677 (PodRemediator foundation, merged)

## What the feature does

When a worker hosting a RabbitMQ broker goes unhealthy, PodRemediator wants to
replace the node-local PVC. Before that is safe, the RabbitMQ controller confirms
the broker cluster and every quorum / stream / durable-classic queue can survive
losing that replica, then grants request-scoped `safe-to-delete` consent (an
annotation handshake). For 4.1+ multi-replica clusters it repairs the blank
replacement: forget the stale member, `join_cluster`, grow quorum-queue
membership, and verify the replacement is online in every quorum queue before
returning it to client traffic.

## Findings addressed

Source: a `/code-review high` pass plus manual analysis.

1. **[design] Availability coupled to the operator.** The original PR held a
   replacement out of client traffic with a template-wide readiness gate
   (`rabbitmq.openstack.org/rejoin-ready`). Because the gate condition is absent
   on pod creation, any pod that (re)starts while the operator is down is dropped
   from the client Service even though RabbitMQ is healthy — operator downtime
   became a data-plane outage. **Fixed** by replacing the gate with a `serving`
   label (see "Key decision").
2. **[liveness] Remediation-slot trap.** The single slot was released only when
   the whole cluster was back at the full configured replica count, so an
   unrelated broker being down (maintenance or a second failure) pinned the slot
   forever and blocked all future remediations. **Fixed**: the slot is released
   per-PVC once *that* replacement rejoins (its pod Ready and its node running in
   the live view).
3. **[security] `InsecureSkipVerify` on a destructive gate.** The management
   probe that authorizes PVC deletion skipped TLS verification whenever no CA
   bytes were available. **Fixed**: fail closed when a *separate* CA secret is
   configured but has no `ca.crt`; log visibly on the legitimate no-CA fallback;
   corrected the misleading comment.
4. **[duplication] `HasRemediationConsent` reimplemented inline.** **Fixed**: the
   consent check now calls `remediationv1.HasRemediationConsent`, as the sibling
   remediation controllers already do.
5. **[duplication] Paginated queue scanner duplicated** (~60 lines across the
   consent and rejoin scans). **Fixed**: one `scanQueues(...)` iterator +
   per-queue closure, plus a shared `quorumQueueMembers()` helper.
6. **[perf] Reconcile storm.** The Pod watch used
   `ResourceVersionChangedPredicate`, reconciling on every status heartbeat.
   **Fixed**: `rmqPodUpdatePredicate` enqueues only on readiness, serving-label,
   rejoin-annotation, phase, or deletion-timestamp changes.
7. **[perf] New HTTP client/transport per management call.** **Fixed**:
   `rmqManagementClient` builds one client/transport per scan.
8. **[quality] Open-coded quorum + overlapping pod-ready helpers.** **Fixed**:
   `quorumOf()` and a single `podReady()` predicate.
9. **[minor] Loose watch predicates.** `IsRabbitmqPod`/`IsRabbitmqPVC` matched any
   object carrying `app.kubernetes.io/name`. **Fixed**: pods must carry
   `app.kubernetes.io/component=rabbitmq`; PVCs must match the
   `persistence-…-server-<ordinal>` shape (PVCs only inherit the StatefulSet
   selector label, so component/part-of are unavailable there).

### Verified not-bugs

Label propagation to PVCs; headless service `PublishNotReadyAddresses:true`
(lets probes reach not-ready replacements); empty-cluster pagination formula;
`join_cluster` self-preparing on 4.1+. No new helper duplicates a **lib-common**
primitive — lib-common has no RabbitMQ management client and no pod-readiness
predicate; the exec path correctly reuses `rsh.ExecInPod`. The duplication was
in-repo only.

## Key decision: a `serving` label instead of the readiness gate

**Principle: do not couple RabbitMQ availability to operator liveness.**

A readiness gate must be template-wide (there are no per-pod gates, and
`readinessGates` is immutable after pod creation), so its availability coupling
is inherent and hits *all* scenarios — unrelated restarts, rollouts, manual
operations — while the protection it buys applies only to the automated
remediation path. Notably, a manual `kubectl delete pod`+`pvc` is **not**
protected by the feature at all: replacement detection is built solely from
`Status.PVCRemediation` entries (only created for PodRemediator-annotated PVCs),
so a manually recreated pod is returned to service without cluster verification —
exactly as on `main` today.

The chosen mechanism (a `serving` label on the client Service selector) therefore
makes the exclusion **fail-open**: pods are created `serving=true`, so a pod that
restarts while the operator is down keeps serving; the controller only flips a
pending replacement to `serving=false` until it has rejoined, then restores it.

### Why not keep the gate

A readiness gate is fail-closed for data but fail-closed-to-outage for
availability. The brief fail-open exposure of the label approach (a blank
replacement could appear in the client Service in the window before the operator
holds it) is the *status quo on `main`*, not a new regression — whereas the
gate's availability coupling *is* a new regression. Given the stated principle,
the label is the consistent choice. See "Accepted limitations" for the window.

### Service-selector hazards and how they are handled

A Service `spec.selector` is equality-only and inclusive, so holding a pod out
requires adding `serving=true` to the client Service selector. That introduces
two outage risks the gate never had, both mitigated:

- **Upgrade migration:** applying the new selector would drop already-running
  pods that lack the label. Mitigated by making the label **unconditional** on
  the pod template and by `ensurePodsServingLabeled`, which backfills existing
  pods with `serving=true` *before* the client Service is applied.
- **Scale/upgrade transition divergence:** avoided because the label is
  unconditional (all versions / replica counts), so selector and labels never
  disagree.

Cost accepted: a one-time, fleet-wide StatefulSet rolling restart on operator
upgrade (the pod template changes for all clusters, including 3.x/single-replica).

## Point 3 (TLS) reachability analysis

- The literal state "`spec.tls.secretName` set, `caSecretName` empty" is **not**
  reachable through normal admission: the mutating webhook
  (`apis/rabbitmq/v1beta1/rabbitmq_webhook.go`) defaults
  `CaSecretName = SecretName`.
- But the fallback keys on `caPEM` being empty (the referenced secret has no
  `ca.crt`), which *is* reachable and common: a plain `kubectl create secret tls`
  secret, or the webhook-defaulted `CaSecretName == SecretName` case the
  controller already models with a `/dev/null` CA mount. So the branch fired in
  real TLS-without-separate-CA deployments. The hardening (finding #3) fails
  closed only on a genuine misconfiguration (an explicitly separate CA secret
  with no `ca.crt`) and logs the legitimate no-CA fallback.

## What changed (by area)

- **apis/rabbitmq/v1beta1:** added `RabbitMQServingLabel` +
  `RabbitMQServingTrue`/`False`; removed `RabbitMQRejoinReadyCondition`.
- **internal/rabbitmq/statefulset.go:** pod template carries `serving=true`
  unconditionally; readiness gate and `rejoinReadinessGates` removed.
- **internal/rabbitmq/service.go:** client Service selector requires
  `serving=true`; headless Service unchanged (replacement stays reachable for
  clustering and the operator probe).
- **internal/controller/rabbitmq/rabbitmq_controller.go:** per-pod PodOverride
  Services also require `serving=true` (they feed transport URLs, so a held
  replacement must be excluded there too); `ensurePodsServingLabeled` migration
  backfill; `scanQueues`/`quorumQueueMembers`/`quorumOf`/`rmqManagementClient`;
  per-PVC slot release; `rmqPodUpdatePredicate`; tightened `IsRabbitmqPod/PVC`;
  `pods/status` RBAC removed (we patch labels, not status).
- **internal/controller/rabbitmq/rejoin.go:** `setPodServing`/`podServing`
  replace the gate helpers; verified rejoin repair unchanged.

## Accepted limitations

- **Fresh-replacement exposure window.** A replacement is born `serving=true`
  and the controller flips it to `serving=false` only after observing it. With a
  healthy operator the pod-ADD watch fires during pod startup, so the label is
  set before the pod is Ready and the window is effectively closed; the residual
  risk is only "operator down *and* a replacement becomes Ready", which equals
  `main`'s current behaviour. This is the deliberate fail-open trade for not
  coupling availability to the operator. Closing it entirely would require
  reintroducing operator-coupling (default `serving=false`), which is the
  availability bug this refactor removes.
- **Manual pod+PVC deletion** remains unverified by design (outside PodRemediator
  scope). A generic blank-seed rejoin detection could be a future follow-up.

## Testing

- `go vet ./...` clean; `go test ./test/unit/...` green, including:
  - `TestPVCRemediationSlotReleasedPerReplacement` (per-PVC slot release;
    unrelated-broker-down case fails under the old full-count logic),
  - `TestRejoinHoldsFreshReplacementOutOfService` (fresh replacement actively
    flipped `serving=true`→`false`; restore-after-verify is covered by the
    existing completion test),
  - `TestIsRabbitmqPod` / `TestIsRabbitmqPVC` (tightened predicates).
- Full functional suite green: 280/280. Run it with an **absolute**
  `KUBEBUILDER_ASSETS=$PWD/bin/k8s/<ver>` via ginkgo in parallel; a serial
  `go test ./test/functional/` hits the default 10-minute timeout.

## PR description call-outs

- The upgrade triggers a **one-time, fleet-wide StatefulSet rolling restart**
  (pod template changed for all RabbitMQ clusters).
- The fail-open exposure window above is an explicit, documented trade equal to
  current `main` behaviour — the price of decoupling RabbitMQ availability from
  operator liveness.
