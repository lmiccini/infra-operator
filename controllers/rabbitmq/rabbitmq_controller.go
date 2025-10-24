/*
Copyright 2023.

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

// Package rabbitmq implements the RabbitMQ controller for managing RabbitMQ cluster instances
package rabbitmq

import (
	"bytes"
	"context"
	"fmt"
	"time"

	rabbitmqv2 "github.com/rabbitmq/cluster-operator/v2/api/v1beta1"
	appsv1 "k8s.io/api/apps/v1"
	k8s_errors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/fields"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/types"
	"k8s.io/client-go/kubernetes"
	"k8s.io/client-go/rest"
	"k8s.io/utils/ptr"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/builder"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/controller/controllerutil"
	"sigs.k8s.io/controller-runtime/pkg/handler"
	"sigs.k8s.io/controller-runtime/pkg/log"
	"sigs.k8s.io/controller-runtime/pkg/predicate"
	"sigs.k8s.io/controller-runtime/pkg/reconcile"

	condition "github.com/openstack-k8s-operators/lib-common/modules/common/condition"
	"github.com/openstack-k8s-operators/lib-common/modules/common/configmap"
	"github.com/openstack-k8s-operators/lib-common/modules/common/helper"
	"github.com/openstack-k8s-operators/lib-common/modules/common/labels"
	"github.com/openstack-k8s-operators/lib-common/modules/common/ocp"
	"github.com/openstack-k8s-operators/lib-common/modules/common/pdb"
	"github.com/openstack-k8s-operators/lib-common/modules/common/rsh"
	"github.com/openstack-k8s-operators/lib-common/modules/common/tls"
	"github.com/openstack-k8s-operators/lib-common/modules/common/util"

	"github.com/go-logr/logr"
	rabbitmqv1beta1 "github.com/openstack-k8s-operators/infra-operator/apis/rabbitmq/v1beta1"
	"github.com/openstack-k8s-operators/infra-operator/pkg/rabbitmq"
	"github.com/openstack-k8s-operators/infra-operator/pkg/rabbitmq/impl"

	topologyv1 "github.com/openstack-k8s-operators/infra-operator/apis/topology/v1beta1"
	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/util/intstr"
)

// GetLogger returns a logger object with a prefix of "controller.name" and additional controller context fields
func (r *Reconciler) GetLogger(ctx context.Context) logr.Logger {
	return log.FromContext(ctx).WithName("Controllers").WithName("RabbitMq")
}

// fields to index to reconcile on CR change
const (
	serviceSecretNameField = ".spec.tls.SecretName"
	caSecretNameField      = ".spec.tls.CASecretName"
	topologyField          = ".spec.topologyRef.Name"
)

var rmqAllWatchFields = []string{
	serviceSecretNameField,
	caSecretNameField,
	topologyField,
}

// Reconciler reconciles a RabbitMq object
type Reconciler struct {
	client.Client
	Kclient kubernetes.Interface
	config  *rest.Config
	Scheme  *runtime.Scheme
}

// +kubebuilder:rbac:groups=rabbitmq.openstack.org,resources=rabbitmqs,verbs=get;list;watch;create;update;patch;delete
// +kubebuilder:rbac:groups=rabbitmq.openstack.org,resources=rabbitmqs/status,verbs=get;update;patch
// +kubebuilder:rbac:groups=rabbitmq.openstack.org,resources=rabbitmqs/finalizers,verbs=update

// +kubebuilder:rbac:groups=rabbitmq.com,resources=rabbitmqclusters,verbs=get;list;watch;create;update;patch;delete

// Required to determine IPv6 and FIPS
// +kubebuilder:rbac:groups=config.openshift.io,resources=networks,verbs=get;list;watch;

// Required to exec into pods
// +kubebuilder:rbac:groups=core,resources=pods,verbs=get;list;watch;create;update;patch;delete;
// +kubebuilder:rbac:groups=core,resources=pods/exec,verbs=create

// Required to manage PersistentVolumeClaims for version upgrades
// +kubebuilder:rbac:groups=core,resources=persistentvolumeclaims,verbs=get;list;watch;delete

// Required to manage PodDisruptionBudgets for multi-replica deployments
// +kubebuilder:rbac:groups=policy,resources=poddisruptionbudgets,verbs=get;list;watch;create;update;patch;delete

// Reconcile - RabbitMq
func (r *Reconciler) Reconcile(ctx context.Context, req ctrl.Request) (result ctrl.Result, _err error) {
	Log := r.GetLogger(ctx)

	// Fetch the RabbitMq instance
	instance := &rabbitmqv1beta1.RabbitMq{}
	err := r.Get(ctx, req.NamespacedName, instance)
	if err != nil {
		if k8s_errors.IsNotFound(err) {
			// Request object not found, could have been deleted after reconcile request.
			// Owned objects are automatically garbage collected. For additional cleanup logic use finalizers.
			// Return and don't requeue
			return ctrl.Result{}, nil
		}
		// Error reading the object - requeue the request.
		return ctrl.Result{}, err
	}

	helper, err := helper.NewHelper(
		instance,
		r.Client,
		r.Kclient,
		r.Scheme,
		Log,
	)
	if err != nil {
		return ctrl.Result{}, err
	}

	// Add RabbitmqCurrentVersion label to instance if it doesn't exist or is empty
	if instance.Labels == nil {
		instance.Labels = make(map[string]string)
	}
	if currentVersion, exists := instance.Labels["rabbitmqcurrentversion"]; !exists || currentVersion == "" {
		instance.Labels["rabbitmqcurrentversion"] = "3.9"
		// Patch the instance to persist the default label
		if err := helper.PatchInstance(ctx, instance); err != nil {
			Log.Error(err, "Failed to set default rabbitmqcurrentversion label")
			return ctrl.Result{}, err
		}
		Log.Info("Set default rabbitmqcurrentversion label to 3.9")
		// Return to let the reconcile cycle continue with the updated labels
		return ctrl.Result{Requeue: true}, nil
	}

	// Check version compatibility and handle version upgrades
	var versionMismatch bool
	var targetVersion string
	Log.Info(fmt.Sprintf("Checking version compatibility. Labels: %v", instance.Labels))
	if currentVersion, hasCurrent := instance.Labels["rabbitmqcurrentversion"]; hasCurrent {
		if tv, hasTarget := instance.Labels["rabbitmqversion"]; hasTarget {
			targetVersion = tv
			Log.Info(fmt.Sprintf("Version check: current=%s, target=%s, upgradeInProgress=%s", currentVersion, targetVersion, instance.Status.VersionUpgradeInProgress))
			if currentVersion != targetVersion {
				// Check if upgrade is already in progress
				if instance.Status.VersionUpgradeInProgress != targetVersion {
					versionMismatch = true
					// Mark upgrade as in progress
					instance.Status.VersionUpgradeInProgress = targetVersion
					Log.Info(fmt.Sprintf("RabbitMQ version mismatch: current=%s, target=%s. Starting version upgrade process.", currentVersion, targetVersion))
					// Persist the upgrade status immediately
					if err := helper.PatchInstance(ctx, instance); err != nil {
						Log.Error(err, "Failed to set version upgrade in progress status")
						return ctrl.Result{}, err
					}
				} else {
					Log.Info("Version upgrade already in progress")
				}
			} else {
				// Versions match, clear any upgrade in progress status
				if instance.Status.VersionUpgradeInProgress != "" {
					Log.Info("Versions match, clearing upgrade in progress status")
					instance.Status.VersionUpgradeInProgress = ""
				}
			}
		} else {
			Log.Info("No rabbitmqversion label found")
		}
	} else {
		Log.Info("No rabbitmqcurrentversion label found")
	}

	// initialize status if Conditions is nil, but do not reset if it already
	// exists
	isNewInstance := instance.Status.Conditions == nil
	if isNewInstance {
		instance.Status.Conditions = condition.Conditions{}
	}

	// Save a copy of the condtions so that we can restore the LastTransitionTime
	// when a condition's state doesn't change.
	savedConditions := instance.Status.Conditions.DeepCopy()

	// Always patch the instance status when exiting this function so we can
	// persist any changes.
	defer func() {
		// Don't update the status, if reconciler Panics
		if r := recover(); r != nil {
			Log.Info(fmt.Sprintf("panic during reconcile %v\n", r))
			panic(r)
		}
		condition.RestoreLastTransitionTimes(
			&instance.Status.Conditions, savedConditions)
		if instance.Status.Conditions.IsUnknown(condition.ReadyCondition) {
			instance.Status.Conditions.Set(
				instance.Status.Conditions.Mirror(condition.ReadyCondition))
		}
		err := helper.PatchInstance(ctx, instance)
		if err != nil {
			_err = err
			return
		}
	}()

	//
	// initialize status
	//
	// initialize conditions used later as Status=Unknown
	cl := condition.CreateList(
		condition.UnknownCondition(condition.ReadyCondition, condition.InitReason, condition.ReadyInitMessage),
		// TLS cert secrets
		condition.UnknownCondition(condition.TLSInputReadyCondition, condition.InitReason, condition.InputReadyInitMessage),
		// configmap generation
		condition.UnknownCondition(condition.ServiceConfigReadyCondition, condition.InitReason, condition.ServiceConfigReadyInitMessage),
		// rabbitmq pods ready
		condition.UnknownCondition(condition.DeploymentReadyCondition, condition.InitReason, condition.DeploymentReadyInitMessage),
		// PDB ready
		condition.UnknownCondition(condition.PDBReadyCondition, condition.InitReason, condition.PDBReadyInitMessage),
	)

	instance.Status.Conditions.Init(&cl)
	instance.Status.ObservedGeneration = instance.Generation

	// Init Topology condition if there's a reference
	if instance.Spec.TopologyRef != nil {
		c := condition.UnknownCondition(condition.TopologyReadyCondition, condition.InitReason, condition.TopologyReadyInitMessage)
		cl.Set(c)
	}

	// Handle service delete
	if !instance.DeletionTimestamp.IsZero() {
		return r.reconcileDelete(ctx, instance, helper)
	}

	//
	// TLS input validation
	//
	// Validate service cert secret
	if instance.Spec.TLS.SecretName != "" {
		// Create a fake service to validate
		srv := tls.Service{
			SecretName: instance.Spec.TLS.SecretName,
		}
		if instance.Spec.TLS.CaSecretName == instance.Spec.TLS.SecretName {
			srv.CaMount = ptr.To("/dev/null")
		}
		_, err := srv.ValidateCertSecret(ctx, helper, instance.Namespace)
		if err != nil {
			if k8s_errors.IsNotFound(err) {
				instance.Status.Conditions.Set(condition.FalseCondition(
					condition.TLSInputReadyCondition,
					condition.RequestedReason,
					condition.SeverityInfo,
					condition.TLSInputReadyWaitingMessage, err.Error()))
				return ctrl.Result{}, nil
			}
			instance.Status.Conditions.Set(condition.FalseCondition(
				condition.TLSInputReadyCondition,
				condition.ErrorReason,
				condition.SeverityWarning,
				condition.TLSInputErrorMessage,
				err.Error()))
			return ctrl.Result{}, err
		}
	}
	// all cert input checks out so report InputReady
	instance.Status.Conditions.MarkTrue(condition.TLSInputReadyCondition, condition.InputReadyMessage)

	IPv6Enabled, err := ocp.FirstClusterNetworkIsIPv6(ctx, helper)
	if err != nil {
		instance.Status.Conditions.Set(condition.FalseCondition(
			condition.ServiceConfigReadyCondition,
			condition.ErrorReason,
			condition.SeverityWarning,
			condition.ServiceConfigReadyErrorMessage,
			err.Error()))
		return ctrl.Result{}, fmt.Errorf("error getting cluster IPv6 config: %w", err)
	}

	fipsEnabled, err := ocp.IsFipsCluster(ctx, helper)
	if err != nil {
		instance.Status.Conditions.Set(condition.FalseCondition(
			condition.ServiceConfigReadyCondition,
			condition.ErrorReason,
			condition.SeverityWarning,
			condition.ServiceConfigReadyErrorMessage,
			err.Error()))
		return ctrl.Result{}, fmt.Errorf("error getting cluster FIPS config: %w", err)
	}

	// NOTE(dciabrin) OSPRH-20331 reported RabbitMQ partitionning during
	// key update events, so until this can be resolved, revert to the
	// same configuration scheme as OSP17 (see OSPRH-13633)
	var tlsVersions string
	if fipsEnabled {
		tlsVersions = "['tlsv1.2','tlsv1.3']"
	} else {
		tlsVersions = "['tlsv1.2']"
	}
	// RabbitMq config maps
	cms := []util.Template{
		{
			Name:         fmt.Sprintf("%s-config-data", instance.Name),
			Namespace:    instance.Namespace,
			Type:         util.TemplateTypeConfig,
			InstanceType: "rabbitmq",
			Labels:       map[string]string{},
			CustomData: map[string]string{
				"inter_node_tls.config": fmt.Sprintf(`[
  {server, [
    {cacertfile,"/etc/rabbitmq-tls/ca.crt"},
    {certfile,"/etc/rabbitmq-tls/tls.crt"},
    {keyfile,"/etc/rabbitmq-tls/tls.key"},
    {secure_renegotiate, true},
    {fail_if_no_peer_cert, true},
    {verify, verify_peer},
    {versions, %s}
  ]},
  {client, [
    {cacertfile,"/etc/rabbitmq-tls/ca.crt"},
    {certfile,"/etc/rabbitmq-tls/tls.crt"},
    {keyfile,"/etc/rabbitmq-tls/tls.key"},
    {secure_renegotiate, true},
    {verify, verify_peer},
    {versions, %s}
  ]}
].
`, tlsVersions, tlsVersions),
			},
		},
	}

	err = configmap.EnsureConfigMaps(ctx, helper, instance, cms, nil)
	if err != nil {
		instance.Status.Conditions.Set(condition.FalseCondition(
			condition.ServiceConfigReadyCondition,
			condition.ErrorReason,
			condition.SeverityWarning,
			condition.ServiceConfigReadyErrorMessage,
			err.Error()))
		return ctrl.Result{}, fmt.Errorf("error calculating configmap hash: %w", err)
	}

	rabbitmqCluster := &rabbitmqv2.RabbitmqCluster{
		ObjectMeta: metav1.ObjectMeta{
			Name:      instance.Name,
			Namespace: instance.Namespace,
		},
	}

	err = instance.Spec.MarshalInto(&rabbitmqCluster.Spec)
	if err != nil {
		instance.Status.Conditions.Set(condition.FalseCondition(
			condition.ServiceConfigReadyCondition,
			condition.ErrorReason,
			condition.SeverityWarning,
			condition.ServiceConfigReadyErrorMessage,
			err.Error()))
		return ctrl.Result{}, fmt.Errorf("error creating RabbitmqCluster Spec: %w", err)
	}

	instance.Status.Conditions.MarkTrue(condition.ServiceConfigReadyCondition, condition.ServiceConfigReadyMessage)

	//
	// Handle Topology
	//
	topology, err := topologyv1.EnsureServiceTopology(
		ctx,
		helper,
		instance.Spec.TopologyRef,
		instance.Status.LastAppliedTopology,
		instance.Name,
		labels.GetLabelSelector(
			map[string]string{
				labels.K8sAppName:      instance.Name,
				labels.K8sAppComponent: "rabbitmq",
				labels.K8sAppPartOf:    "rabbitmq",
			},
		),
	)
	if err != nil {
		instance.Status.Conditions.Set(condition.FalseCondition(
			condition.TopologyReadyCondition,
			condition.ErrorReason,
			condition.SeverityWarning,
			condition.TopologyReadyErrorMessage,
			err.Error()))
		return ctrl.Result{}, fmt.Errorf("waiting for Topology requirements: %w", err)
	}

	// If TopologyRef is present and ensureHeatTopology returned a valid
	// topology object, set .Status.LastAppliedTopology to the referenced one
	// and mark the condition as true
	if instance.Spec.TopologyRef != nil {
		// update the Status with the last retrieved TopologyRef
		instance.Status.LastAppliedTopology = instance.Spec.TopologyRef
		// update the TopologyRef associated condition
		instance.Status.Conditions.MarkTrue(condition.TopologyReadyCondition, condition.TopologyReadyMessage)
	} else {
		// remove LastAppliedTopology from the .Status
		instance.Status.LastAppliedTopology = nil
	}

	// Scale to 1 replica during version upgrade to avoid mixing incompatible versions
	if versionMismatch {
		Log.Info("Starting version upgrade: scaling to 1 replica")
		rabbitmqCluster.Spec.Replicas = ptr.To(int32(1))
	}

	err = rabbitmq.ConfigureCluster(rabbitmqCluster, IPv6Enabled, fipsEnabled, topology, instance.Spec.NodeSelector, instance.Spec.Override)
	if err != nil {
		instance.Status.Conditions.Set(condition.FalseCondition(
			condition.ServiceConfigReadyCondition,
			condition.ErrorReason,
			condition.SeverityWarning,
			condition.ServiceConfigReadyErrorMessage,
			err.Error()))
		return ctrl.Result{}, fmt.Errorf("error configuring RabbitmqCluster: %w", err)
	}

	rabbitmqImplCluster := impl.NewRabbitMqCluster(rabbitmqCluster, 5)
	rmqres, rmqerr := rabbitmqImplCluster.CreateOrPatch(ctx, helper)
	if rmqerr != nil {
		return rmqres, rmqerr
	}

	// Pause RabbitMQ operator reconciliation and patch StatefulSet for version upgrade
	if versionMismatch {
		Log.Info("Pausing RabbitMQ operator reconciliation and patching StatefulSet for version upgrade")
		if err := r.pauseAndPatchForVersionUpgrade(ctx, instance); err != nil {
			Log.Error(err, "Failed to pause and patch for version upgrade")
			return ctrl.Result{}, err
		}
	}

	rabbitmqClusterInstance := rabbitmqImplCluster.GetRabbitMqCluster()

	clusterReady := false
	if rabbitmqClusterInstance.Status.ObservedGeneration == rabbitmqClusterInstance.Generation {
		for _, oldCond := range rabbitmqClusterInstance.Status.Conditions {
			// Forced to hardcode "ClusterAvailable" here because linter will not allow
			// us to import "github.com/rabbitmq/cluster-operator/internal/status"
			if string(oldCond.Type) == "ClusterAvailable" && oldCond.Status == corev1.ConditionTrue {
				clusterReady = true
			}
		}
	}

	// Handle version upgrade completion
	Log.Info(fmt.Sprintf("Version upgrade check: versionMismatch=%t", versionMismatch))
	if versionMismatch && clusterReady {
		// Version upgrade is complete, scale back up to original replica count
		Log.Info("Version upgrade completed successfully, scaling back up to original replica count")

		// Scale back up to original replica count
		if instance.Spec.Replicas != nil {
			Log.Info(fmt.Sprintf("Scaling RabbitMQ cluster back up to %d replicas", *instance.Spec.Replicas))
			rabbitmqCluster.Spec.Replicas = instance.Spec.Replicas
		}

		// Resume RabbitMQ operator reconciliation
		if err := r.resumeRabbitMQReconciliation(ctx, instance); err != nil {
			Log.Error(err, "Failed to resume RabbitMQ operator reconciliation")
			return ctrl.Result{}, err
		}

		// Update version labels and clear upgrade status
		instance.Labels["rabbitmqcurrentversion"] = targetVersion
		instance.Status.VersionUpgradeInProgress = ""
		if err := helper.PatchInstance(ctx, instance); err != nil {
			Log.Error(err, "Failed to update version label")
			return ctrl.Result{}, err
		}
		Log.Info(fmt.Sprintf("Version upgrade completed. Updated rabbitmqcurrentversion to %s", targetVersion))
	}

	if clusterReady {
		instance.Status.Conditions.MarkTrue(condition.DeploymentReadyCondition, condition.DeploymentReadyMessage)

		if instance.Spec.Replicas != nil && *instance.Spec.Replicas > 1 {
			// Apply PDB for multi-replica deployments
			labelMap := map[string]string{
				labels.K8sAppName:      instance.Name,
				labels.K8sAppComponent: "rabbitmq",
				labels.K8sAppPartOf:    "rabbitmq",
			}

			pdbSpec := pdb.MaxUnavailablePodDisruptionBudget(
				instance.Name,
				instance.Namespace,
				intstr.FromInt(1),
				labelMap,
			)
			pdbInstance := pdb.NewPDB(pdbSpec, 5*time.Second)

			_, err := pdbInstance.CreateOrPatch(ctx, helper)
			if err != nil {
				Log.Error(err, "Could not apply PDB")
				instance.Status.Conditions.Set(condition.FalseCondition(
					condition.PDBReadyCondition,
					condition.ErrorReason,
					condition.SeverityWarning,
					condition.PDBReadyErrorMessage, err.Error()))
				return ctrl.Result{}, err
			}
		}
		instance.Status.Conditions.MarkTrue(condition.PDBReadyCondition, condition.PDBReadyMessage)

		// Let's wait DeploymentReadyCondition=True to apply the policy
		if instance.Spec.QueueType == "Mirrored" && *instance.Spec.Replicas > 1 && instance.Status.QueueType != "Mirrored" {
			Log.Info("ha-all policy not present. Applying.")
			err := updateMirroredPolicy(ctx, helper, instance, r.config, true)
			if err != nil {
				Log.Error(err, "Could not apply ha-all policy")
				instance.Status.Conditions.Set(condition.FalseCondition(
					condition.DeploymentReadyCondition,
					condition.ErrorReason,
					condition.SeverityWarning,
					condition.DeploymentReadyErrorMessage, err.Error()))
				return ctrl.Result{}, err
			}
		} else if instance.Spec.QueueType != "Mirrored" && instance.Status.QueueType == "Mirrored" {
			Log.Info("Removing ha-all policy")
			err := updateMirroredPolicy(ctx, helper, instance, r.config, false)
			if err != nil {
				Log.Error(err, "Could not remove ha-all policy")
				instance.Status.Conditions.Set(condition.FalseCondition(
					condition.DeploymentReadyCondition,
					condition.ErrorReason,
					condition.SeverityWarning,
					condition.DeploymentReadyErrorMessage, err.Error()))
				return ctrl.Result{}, err
			}
		}

		// Update status for Quorum queue type
		if instance.Spec.QueueType == "Quorum" && instance.Status.QueueType != "Quorum" {
			Log.Info("Setting queue type status to quorum")
		} else if instance.Spec.QueueType != "Quorum" && instance.Status.QueueType == "Quorum" {
			Log.Info("Removing quorum queue type status")
		}

		instance.Status.QueueType = instance.Spec.QueueType
	}

	if instance.Status.Conditions.AllSubConditionIsTrue() {
		instance.Status.Conditions.MarkTrue(
			condition.ReadyCondition, condition.ReadyMessage)
	}
	return ctrl.Result{}, nil
}

func updateMirroredPolicy(ctx context.Context, helper *helper.Helper, instance *rabbitmqv1beta1.RabbitMq, config *rest.Config, apply bool) error {
	cli := helper.GetKClient()

	pod := types.NamespacedName{
		Name:      instance.Name + "-server-0",
		Namespace: instance.Namespace,
	}

	container := "rabbitmq"
	s := []string{"/bin/bash", "-c", "rabbitmqctl clear_policy ha-all"}

	if apply {
		s = []string{"/bin/bash", "-c", "rabbitmqctl set_policy ha-all \"\" '{\"ha-mode\":\"exactly\",\"ha-params\":2,\"ha-promote-on-shutdown\":\"always\"}'"}
	}

	err := rsh.ExecInPod(ctx, cli, config, pod, container, s,
		func(_ *bytes.Buffer, _ *bytes.Buffer) error {
			return nil
		})
	return err
}

func (r *Reconciler) reconcileDelete(ctx context.Context, instance *rabbitmqv1beta1.RabbitMq, helper *helper.Helper) (ctrl.Result, error) {
	Log := r.GetLogger(ctx)
	Log.Info("Reconciling Service delete")

	rabbitmqCluster := impl.NewRabbitMqCluster(
		&rabbitmqv2.RabbitmqCluster{
			ObjectMeta: metav1.ObjectMeta{
				Name:      instance.Name,
				Namespace: instance.Namespace,
			},
		},
		5,
	)
	err := rabbitmqCluster.Delete(ctx, helper)
	if err != nil {
		return ctrl.Result{}, err
	}

	// Remove finalizer on the Topology CR
	if ctrlResult, err := topologyv1.EnsureDeletedTopologyRef(
		ctx,
		helper,
		instance.Status.LastAppliedTopology,
		instance.Name,
	); err != nil {
		return ctrlResult, err
	}

	// Service is deleted so remove the finalizer.
	controllerutil.RemoveFinalizer(instance, helper.GetFinalizer())
	Log.Info("Reconciled Service delete successfully")

	return ctrl.Result{}, nil
}

// pauseAndPatchForVersionUpgrade pauses RabbitMQ operator reconciliation and patches StatefulSet
func (r *Reconciler) pauseAndPatchForVersionUpgrade(ctx context.Context, instance *rabbitmqv1beta1.RabbitMq) error {
	Log := r.GetLogger(ctx)

	// Retry mechanism for updating RabbitmqCluster (handles race conditions)
	var rabbitmqCluster *rabbitmqv2.RabbitmqCluster
	var err error

	for i := 0; i < 3; i++ {
		// Get the RabbitmqCluster created by our controller
		rabbitmqCluster = &rabbitmqv2.RabbitmqCluster{}
		err = r.Client.Get(ctx, types.NamespacedName{
			Name:      instance.Name,
			Namespace: instance.Namespace,
		}, rabbitmqCluster)
		if err != nil {
			if k8s_errors.IsNotFound(err) {
				Log.Info("RabbitmqCluster not found yet, will retry")
				return nil
			}
			return err
		}

		// Check if already paused
		if rabbitmqCluster.Labels != nil && rabbitmqCluster.Labels["rabbitmq.com/pauseReconciliation"] == "true" {
			Log.Info("RabbitMQ operator reconciliation already paused")
			break
		}

		// Pause RabbitMQ operator reconciliation
		if rabbitmqCluster.Labels == nil {
			rabbitmqCluster.Labels = make(map[string]string)
		}
		rabbitmqCluster.Labels["rabbitmq.com/pauseReconciliation"] = "true"

		err = r.Client.Update(ctx, rabbitmqCluster)
		if err != nil {
			if k8s_errors.IsConflict(err) {
				Log.Info(fmt.Sprintf("Conflict updating RabbitmqCluster, retrying (attempt %d/3)", i+1))
				time.Sleep(time.Duration(i+1) * time.Second)
				continue
			}
			return err
		}
		Log.Info("RabbitMQ operator reconciliation paused")
		break
	}

	if err != nil {
		return fmt.Errorf("failed to pause RabbitMQ operator reconciliation after 3 attempts: %w", err)
	}

	// Wait a moment for the operator to stop reconciling
	time.Sleep(2 * time.Second)

	// Get the StatefulSet created by the RabbitMQ operator
	statefulSet := &appsv1.StatefulSet{}
	err = r.Client.Get(ctx, types.NamespacedName{
		Name:      instance.Name + "-server",
		Namespace: instance.Namespace,
	}, statefulSet)
	if err != nil {
		if k8s_errors.IsNotFound(err) {
			Log.Info("StatefulSet not found yet, will retry")
			return nil
		}
		return err
	}

	Log.Info(fmt.Sprintf("Found StatefulSet with %d init containers", len(statefulSet.Spec.Template.Spec.InitContainers)))
	for i, initContainer := range statefulSet.Spec.Template.Spec.InitContainers {
		Log.Info(fmt.Sprintf("Init container %d: %s", i, initContainer.Name))
	}

	// Create a cleanup init container
	cleanupContainer := corev1.Container{
		Name:    "clean-mnesia",
		Image:   instance.Spec.ContainerImage,
		Command: []string{"/bin/bash"},
		Args:    []string{"-c", "rm -rf /var/lib/rabbitmq/mnesia/*"},
		VolumeMounts: []corev1.VolumeMount{
			{
				Name:      "persistence",
				MountPath: "/var/lib/rabbitmq",
			},
		},
	}

	// Add the cleanup container at the beginning of initContainers
	statefulSet.Spec.Template.Spec.InitContainers = append(
		[]corev1.Container{cleanupContainer},
		statefulSet.Spec.Template.Spec.InitContainers...,
	)

	// Patch the StatefulSet
	err = r.Client.Update(ctx, statefulSet)
	if err != nil {
		return err
	}

	Log.Info("StatefulSet patched successfully with mnesia cleanup init container")

	// Verify the StatefulSet was updated by checking it again
	time.Sleep(1 * time.Second)
	updatedStatefulSet := &appsv1.StatefulSet{}
	err = r.Client.Get(ctx, types.NamespacedName{
		Name:      instance.Name + "-server",
		Namespace: instance.Namespace,
	}, updatedStatefulSet)
	if err != nil {
		Log.Error(err, "Failed to verify StatefulSet update")
		return err
	}

	// Check if our init container is present
	cleanupContainerFound := false
	for _, initContainer := range updatedStatefulSet.Spec.Template.Spec.InitContainers {
		if initContainer.Name == "clean-mnesia" {
			cleanupContainerFound = true
			break
		}
	}

	if !cleanupContainerFound {
		return fmt.Errorf("clean-mnesia init container not found in StatefulSet after update")
	}

	Log.Info("Verified: clean-mnesia init container successfully added to StatefulSet")
	return nil
}

// resumeRabbitMQReconciliation resumes RabbitMQ operator reconciliation
func (r *Reconciler) resumeRabbitMQReconciliation(ctx context.Context, instance *rabbitmqv1beta1.RabbitMq) error {
	Log := r.GetLogger(ctx)

	// Retry mechanism for updating RabbitmqCluster (handles race conditions)
	for i := 0; i < 3; i++ {
		// Get the RabbitmqCluster
		rabbitmqCluster := &rabbitmqv2.RabbitmqCluster{}
		err := r.Client.Get(ctx, types.NamespacedName{
			Name:      instance.Name,
			Namespace: instance.Namespace,
		}, rabbitmqCluster)
		if err != nil {
			return err
		}

		// Check if already resumed
		if rabbitmqCluster.Labels == nil || rabbitmqCluster.Labels["rabbitmq.com/pauseReconciliation"] != "true" {
			Log.Info("RabbitMQ operator reconciliation already resumed")
			return nil
		}

		// Remove the pause reconciliation label
		delete(rabbitmqCluster.Labels, "rabbitmq.com/pauseReconciliation")

		err = r.Client.Update(ctx, rabbitmqCluster)
		if err != nil {
			if k8s_errors.IsConflict(err) {
				Log.Info(fmt.Sprintf("Conflict updating RabbitmqCluster, retrying (attempt %d/3)", i+1))
				time.Sleep(time.Duration(i+1) * time.Second)
				continue
			}
			return err
		}

		Log.Info("RabbitMQ operator reconciliation resumed")
		return nil
	}

	return fmt.Errorf("failed to resume RabbitMQ operator reconciliation after 3 attempts")
}

// SetupWithManager sets up the controller with the Manager.
func (r *Reconciler) SetupWithManager(mgr ctrl.Manager) error {
	r.config = mgr.GetConfig()

	// Various CR fields need to be indexed to filter watch events
	// for the secret changes we want to be notified of
	// index TLS secretName
	if err := mgr.GetFieldIndexer().IndexField(context.Background(), &rabbitmqv1beta1.RabbitMq{}, serviceSecretNameField, func(rawObj client.Object) []string {
		// Extract the secret name from the spec, if one is provided
		cr := rawObj.(*rabbitmqv1beta1.RabbitMq)
		tls := &cr.Spec.TLS
		if tls.SecretName != "" {
			return []string{tls.SecretName}
		}
		return nil
	}); err != nil {
		return err
	}

	// index TLS CA secretName
	if err := mgr.GetFieldIndexer().IndexField(context.Background(), &rabbitmqv1beta1.RabbitMq{}, caSecretNameField, func(rawObj client.Object) []string {
		// Extract the secret name from the spec, if one is provided
		cr := rawObj.(*rabbitmqv1beta1.RabbitMq)
		tls := &cr.Spec.TLS
		if tls.CaSecretName != "" {
			return []string{tls.CaSecretName}
		}
		return nil
	}); err != nil {
		return err
	}

	// index topologyField
	if err := mgr.GetFieldIndexer().IndexField(context.Background(), &rabbitmqv1beta1.RabbitMq{}, topologyField, func(rawObj client.Object) []string {
		// Extract the topology name from the spec, if one is provided
		cr := rawObj.(*rabbitmqv1beta1.RabbitMq)
		if cr.Spec.TopologyRef == nil {
			return nil
		}
		return []string{cr.Spec.TopologyRef.Name}
	}); err != nil {
		return err
	}

	return ctrl.NewControllerManagedBy(mgr).
		For(&rabbitmqv1beta1.RabbitMq{}).
		Owns(&rabbitmqv2.RabbitmqCluster{}).
		Watches(
			&corev1.Secret{},
			handler.EnqueueRequestsFromMapFunc(r.findObjectsForSrc),
			builder.WithPredicates(predicate.ResourceVersionChangedPredicate{}),
		).
		Watches(&topologyv1.Topology{},
			handler.EnqueueRequestsFromMapFunc(r.findObjectsForSrc),
			builder.WithPredicates(predicate.GenerationChangedPredicate{})).
		Complete(r)
}

// findObjectsForSrc - returns a reconcile request if the object is referenced by a Redis CR
func (r *Reconciler) findObjectsForSrc(ctx context.Context, src client.Object) []reconcile.Request {
	requests := []reconcile.Request{}

	Log := r.GetLogger(ctx)

	for _, field := range rmqAllWatchFields {
		crList := &rabbitmqv1beta1.RabbitMqList{}
		listOps := &client.ListOptions{
			FieldSelector: fields.OneTermEqualSelector(field, src.GetName()),
			Namespace:     src.GetNamespace(),
		}
		err := r.List(ctx, crList, listOps)
		if err != nil {
			Log.Error(err, fmt.Sprintf("listing %s for field: %s - %s", crList.GroupVersionKind().Kind, field, src.GetNamespace()))
			return requests
		}

		for _, item := range crList.Items {
			Log.Info(fmt.Sprintf("input source %s changed, reconcile: %s - %s", src.GetName(), item.GetName(), item.GetNamespace()))

			requests = append(requests,
				reconcile.Request{
					NamespacedName: types.NamespacedName{
						Name:      item.GetName(),
						Namespace: item.GetNamespace(),
					},
				},
			)
		}
	}

	return requests
}
