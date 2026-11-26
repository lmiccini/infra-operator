/*
Copyright 2026.

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
*/

package rabbitmq

import (
	"context"
	"errors"
	"fmt"

	rabbitmqv1 "github.com/openstack-k8s-operators/infra-operator/apis/rabbitmq/v1beta1"
	remediationv1 "github.com/openstack-k8s-operators/infra-operator/apis/remediation/v1beta1"
	"github.com/openstack-k8s-operators/infra-operator/internal/rabbitmq/admin"
	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/types"
	"sigs.k8s.io/controller-runtime/pkg/client"
)

var errOldQueueReplicaPresent = errors.New("old broker still has queue replicas")

// hasStartupGate reports whether this pod was built with the startup-gate init
// container. Pods in clusters that do not take part in remediation are not,
// and start their broker without waiting for anything.
func hasStartupGate(pod *corev1.Pod) bool {
	for _, init := range pod.Spec.InitContainers {
		if init.Name == "startup-gate" {
			return true
		}
	}
	return false
}

// startupBlocked reports whether this pod's broker is still held behind the
// gate and has therefore not yet contacted the cluster.
//
// It returns false on any sign that the broker may have started: an approval
// annotation matching the pod, a gate container that has exited successfully,
// or a rabbitmq container that is running or has previously run. Callers use
// this to decide whether a pod can still be treated as inert, so each of those
// states has to disqualify it.
func startupBlocked(pod *corev1.Pod) bool {
	if !hasStartupGate(pod) || pod.UID == "" ||
		pod.Annotations[rabbitmqv1.AnnotationStartupApproved] == string(pod.UID) {
		return false
	}
	for _, status := range pod.Status.InitContainerStatuses {
		if status.Name == "startup-gate" && status.State.Terminated != nil && status.State.Terminated.ExitCode == 0 {
			return false
		}
	}
	for _, status := range pod.Status.ContainerStatuses {
		if status.Name == rabbitmqContainerName &&
			(status.State.Running != nil || status.State.Terminated != nil || status.LastTerminationState.Terminated != nil) {
			return false
		}
	}
	return true
}

// ensurePodStartup decides whether this pod may start its broker, and if so
// writes the approval annotation.
//
// It reads from the API server rather than the cache because the pod-create
// event can arrive before the cache reflects the consent and status writes
// that explain why the pod exists. Approving from that older view would allow
// a replacement to start before its old member was removed.
//
// An ordinary pod is approved once its claim checks out. A pod recorded as a
// replacement is approved only if the durable status reports the old member
// removed, names this exact claim UID as the replacement it cleared, and the
// pod carries the matching rejoin authorization. Any missing or inconsistent
// value returns allowed=false, leaving the pod to wait for a later attempt.
func (r *Reconciler) ensurePodStartup(ctx context.Context, instance *rabbitmqv1.RabbitMq, pod *corev1.Pod) (allowed, changed bool, err error) {
	if !hasStartupGate(pod) {
		return true, false, nil
	}
	live := &corev1.Pod{}
	if err := r.safetyReader().Get(ctx, client.ObjectKeyFromObject(pod), live); err != nil {
		return false, false, err
	}
	if live.UID == "" || live.UID != pod.UID || live.DeletionTimestamp != nil {
		return false, false, fmt.Errorf("pod %s changed before startup approval", pod.Name)
	}
	if live.Annotations[rabbitmqv1.AnnotationStartupApproved] == string(live.UID) {
		*pod = *live
		return true, false, nil
	}
	pvcName := "persistence-" + live.Name
	if _, ok := rabbitMQClaimOrdinal(instance.Name, pvcName); !ok || !podUsesPVC(live, pvcName) {
		return false, false, fmt.Errorf("pod %s does not use its canonical RabbitMQ claim", pod.Name)
	}
	current := &rabbitmqv1.RabbitMq{}
	if err := r.safetyReader().Get(ctx, client.ObjectKeyFromObject(instance), current); err != nil {
		return false, false, err
	}
	if current.DeletionTimestamp != nil || current.UID != instance.UID {
		return false, false, nil
	}
	claim := &corev1.PersistentVolumeClaim{}
	if err := r.safetyReader().Get(ctx, client.ObjectKey{Namespace: live.Namespace, Name: pvcName}, claim); err != nil {
		return false, false, err
	}
	if claim.UID == "" || claim.DeletionTimestamp != nil {
		return false, false, nil
	}
	// The cached instance already knew this was a replacement: refuse to start
	// it unless the durable record still agrees on who authorized the deletion.
	if expected, pending := instance.Status.PVCRemediation[pvcName]; pending &&
		expected.Consented() && string(claim.UID) != expected.PVCUID {
		record, exists := current.Status.PVCRemediation[pvcName]
		if !exists || !record.Consented() ||
			record.RequestID != expected.RequestID || record.PVCUID != expected.PVCUID {
			return false, false, nil
		}
	}
	// A replacement may only start once the old member has been removed from
	// the surviving cluster, and only for the exact claim that removal targeted.
	if recovery, exists := current.Status.PVCRemediation[pvcName]; exists &&
		recovery.Consented() && string(claim.UID) != recovery.PVCUID {
		// A recreated pod on the already verified replacement PVC needs only
		// ordinary UID-scoped approval. Before Recovered, the exact removal
		// request and rejoin authorization are still required.
		verifiedReplacement := recovery.Phase.AtLeast(rabbitmqv1.PVCRemediationRecovered) &&
			recovery.ReplacementPVCUID == string(claim.UID)
		if !verifiedReplacement &&
			(recovery.RequestID == "" || recovery.PVCUID == "" ||
				!recovery.Phase.AtLeast(rabbitmqv1.PVCRemediationMemberRemoved) ||
				recovery.ReplacementPVCUID != string(claim.UID) ||
				live.Annotations[rabbitmqv1.AnnotationRejoinCluster] != recovery.RequestID) {
			return false, false, nil
		}
	}
	base := live.DeepCopy()
	if live.Annotations == nil {
		live.Annotations = map[string]string{}
	}
	live.Annotations[rabbitmqv1.AnnotationStartupApproved] = string(live.UID)
	patchErr := r.Patch(ctx, live, client.MergeFromWithOptions(base, client.MergeFromWithOptimisticLock{}))
	if apierrors.IsConflict(patchErr) {
		// The pod moved under us; report a change so the caller comes back with
		// a fresh read instead of failing the reconcile.
		return false, true, nil
	}
	if patchErr != nil {
		return false, false, fmt.Errorf("approving startup of %s: %w", live.Name, patchErr)
	}
	*pod = *live
	return true, true, nil
}

func (r *Reconciler) verifyOldQueueReplicasRemoved(ctx context.Context, peer *corev1.Pod, cluster *admin.Client, oldNode string) error {
	queues, err := cluster.Queues(ctx, peer, admin.ColumnsMembership)
	if err != nil {
		return err
	}
	for _, q := range queues {
		if q.Type != admin.QueueTypeQuorum {
			continue // Other queue kinds are checked by rmqQueuesSafeAfterDeletion.
		}
		members, _, err := admin.QuorumQueueMembers(q)
		if err != nil {
			return err
		}
		if members[oldNode] {
			return fmt.Errorf("%w: %s/%s", errOldQueueReplicaPresent, q.Vhost, q.Name)
		}
	}
	return nil
}

func (r *Reconciler) safeSurvivorNodes(
	ctx context.Context,
	instance *rabbitmqv1.RabbitMq,
	targetName string,
	pods []corev1.Pod,
	running map[string]bool,
) (map[string]bool, error) {
	claims, err := r.rmqDataClaims(ctx, instance, r.safetyReader())
	if err != nil {
		return nil, err
	}
	stuck := rmqStuckNodes(claims)
	claimsByName := make(map[string]*corev1.PersistentVolumeClaim, len(claims))
	for i := range claims {
		claimsByName[claims[i].Name] = &claims[i]
	}
	for name, entry := range instance.Status.PVCRemediation {
		if !entry.Consented() || entry.StuckNode == "" {
			continue
		}
		claim := claimsByName[name]
		if claim != nil && claim.UID == types.UID(entry.PVCUID) && claim.DeletionTimestamp == nil &&
			claim.Annotations[remediationv1.PVCDeletionCommitFinalizedAnnotation] == "" {
			// The original claim is still governed by its live annotation.
			continue
		}
		// Once the original claim is gone or past its commit boundary, retain
		// the old node exclusion until replacement recovery completes.
		stuck[entry.StuckNode] = true
	}
	safe := map[string]bool{}
	for i := range pods {
		pod := &pods[i]
		nodeName := rmqNodeName(instance, pod.Name)
		if pod.Name != targetName && pod.DeletionTimestamp == nil &&
			podReady(pod) && !stuck[pod.Spec.NodeName] && running[nodeName] {
			safe[nodeName] = true
		}
	}
	return safe, nil
}

// removeOldMember runs only while the replacement cannot start RabbitMQ. The
// original PVC deletion already required the generic controller's SNR fencing
// barrier. If the old broker is nevertheless online, keep startup blocked.
func (r *Reconciler) removeOldMember(ctx context.Context, instance *rabbitmqv1.RabbitMq,
	target, anchor *corev1.Pod, pods []corev1.Pod, pvcName string, membership admin.Membership) error {
	live := &corev1.Pod{}
	if err := r.safetyReader().Get(ctx, client.ObjectKeyFromObject(target), live); err != nil {
		return err
	}
	if live.UID != target.UID || live.DeletionTimestamp != nil || !startupBlocked(live) {
		return fmt.Errorf("replacement %s is not held behind its startup gate", target.Name)
	}
	claim := &corev1.PersistentVolumeClaim{}
	if err := r.safetyReader().Get(ctx, client.ObjectKey{Namespace: target.Namespace, Name: pvcName}, claim); err != nil {
		return err
	}
	recovery := instance.Status.PVCRemediation[pvcName]
	current := &rabbitmqv1.RabbitMq{}
	if err := r.safetyReader().Get(ctx, client.ObjectKeyFromObject(instance), current); err != nil {
		return err
	}
	if current.UID != instance.UID || current.DeletionTimestamp != nil {
		return fmt.Errorf("RabbitMQ resource changed before member removal")
	}
	record, exists := current.Status.PVCRemediation[pvcName]
	if !exists || !record.Consented() ||
		record.RequestID != recovery.RequestID || record.PVCUID != recovery.PVCUID {
		return fmt.Errorf("PVC recovery authorization changed before member removal")
	}
	if claim.DeletionTimestamp != nil || claim.UID == "" || recovery.PVCUID == "" ||
		string(claim.UID) == recovery.PVCUID || recovery.RequestID == "" {
		return fmt.Errorf("replacement claim changed before old-member removal")
	}
	oldNode := rmqNodeName(instance, target.Name)
	if membership.Running[oldNode] {
		return fmt.Errorf("old RabbitMQ member is still running; keeping replacement blocked")
	}
	// Seed the stuck set from the live claims, not only from status: status is
	// refreshed by CheckForStuckPVCRequiringRemediation, which runs after this
	// in the same reconcile, so a claim annotated since the last status write
	// has no entry yet. Commit-time validation reads the annotation directly,
	// and the two must not disagree about who counts as a survivor.
	safe, err := r.safeSurvivorNodes(ctx, instance, target.Name, pods, membership.Running)
	if err != nil {
		return err
	}
	quorum := quorumOf(*instance.Spec.Replicas)
	if len(safe) < quorum {
		return fmt.Errorf("not enough healthy survivors for member removal")
	}
	cluster, err := r.rmqAdmin(ctx, instance)
	if err != nil {
		return err
	}
	if err := r.rmqQueuesSafeAfterDeletion(ctx, instance, anchor, cluster, oldNode, safe); err != nil {
		return err
	}
	members, err := cluster.ConsistentMembers(ctx, anchor)
	if err != nil {
		return err
	}
	queueErr := r.verifyOldQueueReplicasRemoved(ctx, anchor, cluster, oldNode)
	if queueErr != nil && !errors.Is(queueErr, errOldQueueReplicaPresent) {
		return queueErr
	}
	beforePods, err := r.rmqListPods(ctx, instance, r.safetyReader())
	if err != nil {
		return err
	}
	beforeMembership, err := cluster.Membership(ctx, anchor)
	if err != nil {
		return err
	}
	safeBefore, err := r.safeSurvivorNodes(ctx, instance, target.Name, beforePods, beforeMembership.Running)
	if err != nil {
		return err
	}
	if beforeMembership.Running[oldNode] || len(safeBefore) < quorum {
		return fmt.Errorf("survivor membership changed before old-member removal")
	}
	if err := r.rmqQueuesSafeAfterDeletion(ctx, instance, anchor, cluster, oldNode, safeBefore); err != nil {
		return err
	}
	if members[oldNode] || errors.Is(queueErr, errOldQueueReplicaPresent) {
		if err := cluster.ForgetNode(ctx, anchor, oldNode); err != nil {
			return fmt.Errorf("removing old member: %w", err)
		}
	}
	members, err = cluster.ConsistentMembers(ctx, anchor)
	if err != nil {
		return err
	}
	if members[oldNode] || len(members) < quorum {
		return fmt.Errorf("old member removal is not confirmed with survivor quorum")
	}
	after, err := cluster.Membership(ctx, anchor)
	if err != nil {
		return err
	}
	// Recount survivors by the pre-removal rule rather than trusting the raw
	// running list: a broker that is running but not PodReady, or that sits on
	// a node PodRemediator has marked stuck, is not an established survivor.
	afterPods, err := r.rmqListPods(ctx, instance, r.safetyReader())
	if err != nil {
		return err
	}
	safeAfter, err := r.safeSurvivorNodes(ctx, instance, target.Name, afterPods, after.Running)
	if err != nil {
		return err
	}
	voters := 0
	for node := range safeAfter {
		if members[node] {
			voters++
		}
	}
	if after.Running[oldNode] || voters < quorum {
		return fmt.Errorf("survivor membership changed after removal")
	}
	if err := r.rmqQueuesSafeAfterDeletion(ctx, instance, anchor, cluster, oldNode, safeAfter); err != nil {
		return err
	}
	if err := r.verifyOldQueueReplicasRemoved(ctx, anchor, cluster, oldNode); err != nil {
		return err
	}
	recovery.ReplacementPVCUID = string(claim.UID)
	recovery.Phase = rabbitmqv1.PVCRemediationMemberRemoved
	instance.Status.PVCRemediation[pvcName] = recovery
	// Return before approving startup. The outer reconcile must first persist
	// this progress, and ensurePodStartup subsequently reads it from the API.
	return nil
}
