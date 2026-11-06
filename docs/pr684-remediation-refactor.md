# PR #684 Refactor — RabbitMQ PVC remediation / quorum protection

Working design + progress doc so this effort can be paused and resumed. Not
upstream architecture documentation — decide separately whether to commit it.

- **Upstream PR:** https://github.com/openstack-k8s-operators/infra-operator/pull/684
  ("Protect RabbitMQ quorum during local PVC remediation")
- **Branch under refactor:** `podremediator-rabbitmq` (checked out from PR #684 head)
- **Jira:** OSPRH-36533
- **Builds on:** PR #677 (PodRemediator foundation, merged)

## How to resume

1. `git checkout podremediator-rabbitmq`
2. Re-read this doc's "Refactor plan" and tick off completed items.
3. After any Go change: `make generate manifests fmt vet`.
4. Tests: unit tests in `test/unit/rabbitmq/`, functional via `make test`.
5. Local lib-common fork is wired via `go.work` (dev only); the PR's `go.mod`
   pins the official lib-common. Do not rely on fork-only symbols.

## What the PR does (one paragraph)

When a worker hosting a RabbitMQ broker goes unhealthy, PodRemediator wants to
replace the node-local PVC. Before that is safe, the RabbitMQ controller must
confirm the broker cluster and every quorum/stream/durable-classic queue can
survive losing that replica. The controller grants request-scoped
`safe-to-delete` consent (annotation handshake), then — for 4.1+ multi-replica
clusters — repairs the blank replacement: forget the stale member, `join_cluster`,
grow quorum-queue membership, and verify the replacement is online in every
quorum queue before returning it to client traffic.

## Combined review findings (staff-level)

Severity-ranked. Source: automated `/code-review high` pass + manual analysis.

1. **[design] Readiness-gate blast radius.** The PR adds a template-wide
   readiness gate (`rabbitmq.openstack.org/rejoin-ready`) to every pod of
   4.1+ multi-replica clusters. The condition is absent on pod creation, so a
   pod is `NotReady` until the operator sets it. If infra-operator is down when
   any broker pod restarts (drain/OOM/rollout), that pod is dropped from the
   client Service though RabbitMQ is healthy → operator downtime becomes a
   data-plane outage. **DECISION: fix via option 1 (see Decisions).**
2. **[liveness] Remediation slot trap.** The single remediation slot is released
   only when `ReadyCount >= Replicas && runningNodes >= Replicas`. If an
   unrelated broker is intentionally down (maintenance) or a second node fails,
   the slot stays occupied forever and no future PVC on any node can get
   consent. **FIX: release the slot per-PVC when that replacement has rejoined
   (its pod Ready + node running in mgmt API), not on full cluster count.**
3. **[security] `InsecureSkipVerify` on a destructive gate.** `rmqManagementGet`
   skips TLS verification when `creds.caPEM` is empty, and that probe authorizes
   PVC deletion. Reachability analysis below. **FIX: fail closed on misconfig,
   log visibly on the legitimate no-CA fallback, fix the misleading comment.**
4. **[duplication] `HasRemediationConsent` reimplemented inline.** The consent
   check in `CheckForStuckPVCRequiringRemediation` open-codes exactly
   `apis/remediation/v1beta1/annotations.go:HasRemediationConsent` (already used
   by `deletion_commit.go`, `pvc_scan.go`). **FIX: call the shared helper.**
5. **[duplication] Paginated queue scanner duplicated.** `rmqQueuesSafeAfterDeletion`
   and `verifyQuorumQueueReplicasOnline` share ~60 lines of pagination envelope
   validation + per-queue parsing, differing only in error strings and the
   per-queue decision. **FIX: one `scanQueues(...)` iterator + per-queue closure.**
6. **[perf] Reconcile storm.** The Pod watch uses
   `ResourceVersionChangedPredicate`, firing a full reconcile on every pod status
   heartbeat. **FIX: predicate keyed on readiness/condition changes.**
7. **[perf] New HTTP client/transport per call.** `rmqManagementGet` builds a
   fresh `http.Client`+`http.Transport` each call → new TCP+TLS handshake per
   page (up to 100). **FIX: build once per scan, pass the client in.**
8. **[quality] Open-coded quorum + overlapping pod-ready helpers.** `n/2+1`
   repeated 5+ places; `podIsReady`/`podRejoinReady`/`podRabbitMQContainerReady`/
   `rmqReadyPods` overlap. **FIX: `quorumOf()` + consolidate readiness helpers.**
9. **[minor] Loose watch predicates.** `IsRabbitmqPVC`/`IsRabbitmqPod` match any
   object carrying `app.kubernetes.io/name`. Tighten to the real selector.

### Not bugs (verified, excluded)

Label propagation to PVCs; headless service `PublishNotReadyAddresses:true`
(lets probes reach not-ready replacements); version-resolution symmetry between
readiness gate and `rejoinEnabled`; empty-cluster pagination formula; `join_cluster`
self-preparing on 4.1+. No new helper duplicates a **lib-common** primitive
(lib-common has no RabbitMQ mgmt client and no pod-readiness predicate; the exec
path correctly reuses `rsh.ExecInPod`). The duplication is in-repo only.

## Point 3 reachability analysis (resolved)

- The literal state "`spec.tls.secretName` set, `caSecretName` empty" is **not**
  reachable via normal admission: the mutating webhook
  (`apis/rabbitmq/v1beta1/rabbitmq_webhook.go:127-128`) defaults
  `CaSecretName = SecretName`. Only reachable if webhooks are bypassed.
- But the `InsecureSkipVerify` branch keys on `creds.caPEM` being empty (the
  referenced secret lacks a `ca.crt` key), **not** on `CaSecretName == ""`. That
  IS reachable and common: a plain `kubectl create secret tls` secret (or the
  webhook-defaulted `CaSecretName == SecretName` case the controller already
  models with a `/dev/null` CA mount at `rabbitmq_controller.go:374-376`) has no
  `ca.crt`. So the branch fires in real TLS-without-separate-CA deployments. The
  PR's comment describing the trigger is wrong; the risk is real.

## Decisions

- **#1 → Option 1 (label-based, fail-open). Principle: do NOT couple RabbitMQ
  availability to operator liveness.** Rationale captured below.
- Keep the *verified rejoin repair* (forget/join/grow/verify) for the
  PodRemediator-consented path — it is valuable and independent of the gate.

### Why option 1 (the analysis that drove the decision)

- A readiness gate must be template-wide (no per-pod gates; `readinessGates` is
  immutable post-creation). So its availability coupling is inherent and hits
  ALL scenarios (unrelated restarts, rollouts, manual ops), while the protection
  it buys only applies to the automated remediation path.
- **Manual `kubectl delete pod`+`pvc` is NOT protected by the PR.** Rejoin
  replacement-detection is built solely from `Status.PVCRemediation` entries,
  which only exist for PVCs annotated by PodRemediator. A manual delete falls
  through to the "ordinary pods pass the gate" branch and is marked
  `rejoin-ready=true` WITHOUT cluster verification. So:
  - On `main` today: manual delete → blank pod serves when ready (fully
    fail-open; on 4.1+ server-0 can form a standalone/split-brain cluster).
    Unmitigated today.
  - With the PR: manual delete → gated briefly, then marked ready unverified
    (still fail-open for data), AND if the operator is down the pod is stuck
    NotReady (pure availability coupling, no benefit).
- Therefore option 1's only "cost" (a blank replacement briefly serving) is the
  STATUS QUO on `main`, not a new regression — whereas the gate's availability
  coupling IS a new regression. Option 1 is the consistent choice given the
  stated principle. The brief exposure window is minimized by stripping the
  `serving` label on the pod-ADD watch event.

## Refactor plan (tick as completed)

### A. Safe cleanups (no behavior change)  — DONE (build + unit tests green)
- [x] A1. `scanQueues(...)` iterator owning pagination+validation+dedup; both
      callers supply a closure. Added `quorumQueueMembers()` helper too.
- [x] A2. Use `remediationv1.HasRemediationConsent(pvc.Annotations)` for consent.
- [x] A3. `quorumOf(n int32) int`; used in queue-member majority +
      `canRetainQuorumAfterLosingOneReplica`.
- [x] A4. One `podReady(pod)` predicate; `rmqReadyPods` + rejoin reuse it
      (renamed `podIsReady`→`podReady`).
- [x] A5. `rmqManagementClient(...)` builds `*http.Client`/transport once per
      scan; `rmqManagementGet` now takes the client.

### B. Point 3 hardening (TLS) — DONE
- [x] B1. Fixed misleading comment (empty caPEM, not empty CaSecretName).
- [x] B2. Separate CA configured (`CaSecretName != SecretName`) but `ca.crt`
      missing → fail closed via `creds.caConfiguredSeparately`.
- [x] B3. Legitimate no-CA fallback logs at Info level in `rmqManagementClient`.

### C. #1 — label-based serving exclusion (replaces readiness gate) — BLOCKED on a mechanism decision

**Hazard discovered during implementation (was not visible when option 1 was chosen).**
A Kubernetes Service `spec.selector` is equality-only and *inclusive*: a pod is in
the Service iff it matches ALL selector labels. So the only native way to hold a
pod out via a label is to ADD `serving=true` to the client-Service selector and
give serving pods that exact label. That creates two new ways to cause an outage:

1. **Upgrade migration:** the moment the operator applies the new client-Service
   selector (`serving=true`), every already-running pod that lacks the label is
   dropped from the Service — a full outage until the StatefulSet template roll
   re-creates pods with the label (minutes for a 3-node cluster).
2. **Transition divergence (if the label is applied conditionally, i.e. only for
   4.1+ multi-replica like the original gate):** during a scale 1→3 or a 4.0→4.1
   upgrade, the selector and the pods' labels can briefly disagree → zero pods
   selected → outage.

Neither hazard exists with the readiness-gate mechanism (a missing gate just makes
a pod Ready sooner; it never removes a pod from a Service selector).

**Candidate mechanisms (need a decision):**

- **C-opt-A — Always-on label + pre-label before selector.** Add `serving=true`
  to the pod template UNCONDITIONALLY (all versions/replica counts) so selector and
  labels never diverge; and, before applying the client Service, have the operator
  label any existing unlabeled pod `serving=true` so the upgrade migration causes no
  outage. Cost: a one-time fleet-wide StatefulSet roll (template changed for 3.x and
  single-replica too), and the exclusion is still fail-open (brief blank-replacement
  exposure, = today's `main`).
- **C-opt-B — Keep the readiness gate, make it fail-open via fast default
  (the earlier "hybrid").** No Service-selector change, so neither hazard applies.
  Operator sets `rejoin-ready=true` for every non-replacement pod as early as
  possible (pod-ADD watch). Residual: still couples to operator liveness during the
  (now small) window a restarted pod waits for the operator; full-operator-down +
  restart still drops a pod. This is what the user explicitly did NOT want.
- **C-opt-C — Negative label on the headless path only / EndpointSlice surgery.**
  Operator-managed EndpointSlice exclusion. More moving parts; rejected as
  over-engineered for now.

**DECISION: C-opt-A chosen and IMPLEMENTED** (build + unit + RabbitMq functional
tests green). The only new cost is a one-time fleet roll and the brief exposure
that already exists on `main`.

C-opt-A edits (DONE):
- [x] C1. Const `RabbitMQServingLabel = "rabbitmq.openstack.org/serving"` +
      `RabbitMQServingTrue`/`RabbitMQServingFalse` in apis; removed
      `RabbitMQRejoinReadyCondition`.
- [x] C2. StatefulSet pod template: `serving=true` on all pods (unconditional, via
      `podLabels`). Removed `ReadinessGates`/`rejoinReadinessGates` + the
      `sts...ReadinessGates =` update sync line.
- [x] C3. ClientService selector requires `serving=true`; HeadlessService unchanged.
      `ensurePodsServingLabeled` backfills existing pods BEFORE the client Service is
      applied (migration-safe upgrade).
- [x] C4. Removed `setRejoinReady`/`podRejoinReady`; added `setPodServing(pod,bool)`
      (patches metadata label) + `podServing(pod)bool`. Dropped `pods/status` RBAC;
      regenerated role.yaml.
- [x] C5. Rejoin controller holds a replacement via `serving=false`, restores to
      `true` after verify; non-pending pods default to `serving=true`. The existing
      Pod watch drives prompt reaction. (Watch predicate still ResourceVersion-based;
      see E1.)
- [x] C6. Verified rejoin repair (forget/join/grow/verify) unchanged.

### D. #2 — slot-trap fix — DONE (build + existing unit tests green)
- [x] D1. Release each pending-recovery entry when THAT replacement pod is Ready
      and its node is `running` in the mgmt API (per-PVC `stillRecovering` loop),
      not `ReadyCount>=Replicas`. Removes unrelated-broker coupling while keeping
      one-at-a-time. Removed now-unused `rmqRunningNodeCount`.
      TODO(F): add a unit test for the unrelated-broker-down release path.

### E. Lower priority — DONE
- [x] E1. Pod watch now uses `rmqPodUpdatePredicate` (finding #6): update events
      enqueue a reconcile only on PodReady transition, serving-label change, rejoin
      annotation change, phase change, or deletion-timestamp (dis)appearance.
      Create/Delete/Generic still pass. Replaces `ResourceVersionChangedPredicate`.
- [x] E2. Tightened predicates (#9): `IsRabbitmqPod` requires
      `app.kubernetes.io/component=rabbitmq` (+ name label + `-server-`);
      `IsRabbitmqPVC` requires the name label + `persistence-…-server-<ordinal>`
      shape (valid ordinal via `rmqPVCOrdinal`). Note PVCs only inherit the STS
      selector label, so component/part-of are unavailable there. New unit tests in
      `test/unit/rabbitmq/predicate_test.go`.

### F. Verify
- [x] `make generate manifests fmt vet` clean.
- [x] Unit tests migrated to the serving model (`rejoin_test.go`,
      `remediation_quorum_test.go`); `go test ./test/unit/...` green.
- [x] RabbitMq functional suite green (envtest, abs `KUBEBUILDER_ASSETS`).
- [x] RBAC regenerated; `pods/status` removed from role.yaml.
- [x] Added unit tests for the two NEW behaviors:
      - D: `TestPVCRemediationSlotReleasedPerReplacement` — slot released when an
        unrelated broker is down but the replacement rejoined; retained (error)
        when the replacement is still rejoining. (First case fails under old code.)
      - C: `TestRejoinHoldsFreshReplacementOutOfService` — a fresh replacement
        (serving=true) is actively flipped to serving=false. Restore-after-verify
        is covered by the existing completion test (asserts serving=true at end).
- [x] Full functional suite green: `ginkgo --procs 5 --timeout 25m
      ./test/functional/` → 280/280 passed (2m17s). NOTE: run with an ABSOLUTE
      `KUBEBUILDER_ASSETS=$PWD/bin/k8s/1.33.0-linux-amd64`; a serial `go test`
      hits the default 10-min timeout (use ginkgo parallel or `-timeout 25m`).

## Residual risks / open questions

- Option 1 exposure window: blank replacement may briefly join client Service
  before the operator strips `serving`. Minimized via pod-ADD watch; accepted as
  equal to current `main` behavior. Revisit if a stronger guarantee is required.
- Manual pod+PVC deletion remains unverified by design (out of PodRemediator
  scope). Consider a follow-up to detect blank-seed rejoin generically.
- `pods/status` RBAC (`verbs=get;update;patch`) added by the PR may become
  unnecessary after C4 (we patch metadata labels, not status). Re-check during F.

## Progress log

- 2026-11-06: Doc created. Review complete, decisions locked (option 1 + hybrid
  discarded). Starting refactor at phase A.
- 2026-11-06: Phases A, B, D implemented; build + unit tests green.
- 2026-11-06: C mechanism hazard found (Service-selector migration/transition
  outage). Decision: C-opt-A (always-on label + pre-label migration).
- 2026-11-06: C implemented (serving label replaces readiness gate). All unit
  tests migrated + green; `go vet ./...` clean; RabbitMq functional suite green;
  RBAC regenerated (pods/status dropped).
- 2026-11-06: Phase F complete. Added D test
  (TestPVCRemediationSlotReleasedPerReplacement) and C test
  (TestRejoinHoldsFreshReplacementOutOfService). Full functional suite 280/280
  passed.
- 2026-11-06: E1 + E2 done (findings #6, #9). Pod watch uses
  `rmqPodUpdatePredicate`; `IsRabbitmqPod`/`IsRabbitmqPVC` tightened; new
  `predicate_test.go`. `go vet ./...` clean, unit green, functional 280/280 green.
  ALL planned work (A–F + E) complete; refactor ready for review/PR.
