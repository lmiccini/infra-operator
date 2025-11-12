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
	"context"
	"encoding/json"
	"fmt"
	"time"

	rabbitmqv2 "github.com/rabbitmq/cluster-operator/v2/api/v1beta1"
	apiextensionsv1 "k8s.io/apiextensions-apiserver/pkg/apis/apiextensions/v1"
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

// Required to manage PVCs for version upgrades
// +kubebuilder:rbac:groups=core,resources=persistentvolumeclaims,verbs=get;list;watch;delete

// Required to manage secrets for user credentials
// +kubebuilder:rbac:groups=core,resources=secrets,verbs=get;list;watch

// Required to manage RabbitMQUser resources
// +kubebuilder:rbac:groups=rabbitmq.openstack.org,resources=rabbitmqusers,verbs=get;list;watch;create;update;patch;delete

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

	// Check for version upgrade
	var versionMismatch bool
	var targetVersion string
	if instance.Labels != nil {
		if currentVersion, hasCurrent := instance.Labels["rabbitmqcurrentversion"]; hasCurrent {
			if tv, hasTarget := instance.Labels["rabbitmqversion"]; hasTarget {
				targetVersion = tv
				if currentVersion != targetVersion && instance.Status.VersionUpgradeInProgress == "" {
					Log.Info(fmt.Sprintf("RabbitMQ version upgrade detected: current=%s, target=%s", currentVersion, targetVersion))
					instance.Status.VersionUpgradeInProgress = targetVersion
					versionMismatch = true
				} else if instance.Status.VersionUpgradeInProgress != "" {
					versionMismatch = true
				}
			}
		}
	}

	// If version upgrade is in progress, orchestrate the upgrade
	if versionMismatch {
		return r.handleVersionUpgrade(ctx, instance, helper, targetVersion)
	}

	rabbitmqImplCluster := impl.NewRabbitMqCluster(rabbitmqCluster, 5)
	rmqres, rmqerr := rabbitmqImplCluster.CreateOrPatch(ctx, helper)
	if rmqerr != nil {
		return rmqres, rmqerr
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
	policyName := types.NamespacedName{
		Name:      instance.Name + "-ha-all",
		Namespace: instance.Namespace,
	}

	policy := &rabbitmqv1beta1.RabbitMQPolicy{}
	err := helper.GetClient().Get(ctx, policyName, policy)

	if apply {
		// Create or update the policy CR
		definition := map[string]interface{}{
			"ha-mode":                "exactly",
			"ha-params":              2,
			"ha-promote-on-shutdown": "always",
		}
		definitionJSON, marshalErr := json.Marshal(definition)
		if marshalErr != nil {
			return marshalErr
		}

		if err != nil && k8s_errors.IsNotFound(err) {
			// Create new policy
			policy = &rabbitmqv1beta1.RabbitMQPolicy{
				ObjectMeta: metav1.ObjectMeta{
					Name:      policyName.Name,
					Namespace: policyName.Namespace,
				},
				Spec: rabbitmqv1beta1.RabbitMQPolicySpec{
					RabbitmqClusterName: instance.Name,
					Name:                "ha-all",
					Pattern:             "",
					Definition:          apiextensionsv1.JSON{Raw: definitionJSON},
					Priority:            0,
					ApplyTo:             "all",
				},
			}
			if err := controllerutil.SetControllerReference(instance, policy, helper.GetScheme()); err != nil {
				return err
			}
			return helper.GetClient().Create(ctx, policy)
		}
		return err
	}

	// Delete the policy CR if it exists
	if err == nil {
		return helper.GetClient().Delete(ctx, policy)
	}
	if k8s_errors.IsNotFound(err) {
		return nil
	}
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

// handleVersionUpgrade orchestrates the RabbitMQ version upgrade process
func (r *Reconciler) handleVersionUpgrade(ctx context.Context, instance *rabbitmqv1beta1.RabbitMq, helper *helper.Helper, targetVersion string) (ctrl.Result, error) {
	Log := r.GetLogger(ctx)

	// Step 1: Pause cluster-operator reconciliation
	if err := r.pauseClusterOperator(ctx, instance); err != nil {
		Log.Error(err, "Failed to pause cluster-operator")
		return ctrl.Result{}, err
	}

	// Step 2: Collect default user credentials
	username, password, err := r.collectDefaultUserCredentials(ctx, instance)
	if err != nil {
		Log.Error(err, "Failed to collect default user credentials")
		return ctrl.Result{RequeueAfter: 5 * time.Second}, nil
	}

	// Step 3: Delete RabbitMQ resource, pods, and PVCs
	if err := r.deleteRabbitMQResources(ctx, instance); err != nil {
		Log.Error(err, "Failed to delete RabbitMQ resources")
		return ctrl.Result{}, err
	}

	// Wait for resources to be fully deleted
	if r.rabbitmqResourcesExist(ctx, instance) {
		Log.Info("Waiting for RabbitMQ resources to be deleted")
		return ctrl.Result{RequeueAfter: 5 * time.Second}, nil
	}

	// Step 4: Resume reconciliation
	if err := r.resumeClusterOperator(ctx, instance); err != nil {
		Log.Error(err, "Failed to resume cluster-operator")
		return ctrl.Result{}, err
	}

	// Wait for cluster to be ready
	ready, err := r.isClusterReady(ctx, instance)
	if err != nil {
		return ctrl.Result{}, err
	}
	if !ready {
		Log.Info("Waiting for RabbitMQ cluster to be ready after upgrade")
		return ctrl.Result{RequeueAfter: 10 * time.Second}, nil
	}

	// Step 5: Create RabbitMQUser with preserved credentials
	if err := r.createUpgradeUser(ctx, instance, username, password); err != nil {
		Log.Error(err, "Failed to create upgrade user")
		return ctrl.Result{}, err
	}

	// Upgrade complete - update version label and clear status
	instance.Labels["rabbitmqcurrentversion"] = targetVersion
	instance.Status.VersionUpgradeInProgress = ""
	Log.Info(fmt.Sprintf("RabbitMQ version upgrade completed: %s", targetVersion))

	return ctrl.Result{}, nil
}

// pauseClusterOperator pauses the RabbitMQ cluster-operator reconciliation
func (r *Reconciler) pauseClusterOperator(ctx context.Context, instance *rabbitmqv1beta1.RabbitMq) error {
	Log := r.GetLogger(ctx)

	rabbitmqCluster := &rabbitmqv2.RabbitmqCluster{}
	err := r.Client.Get(ctx, types.NamespacedName{Name: instance.Name, Namespace: instance.Namespace}, rabbitmqCluster)
	if err != nil {
		if k8s_errors.IsNotFound(err) {
			return nil
		}
		return err
	}

	if rabbitmqCluster.Labels == nil {
		rabbitmqCluster.Labels = make(map[string]string)
	}

	if rabbitmqCluster.Labels["rabbitmq.com/pauseReconciliation"] == "true" {
		return nil
	}

	rabbitmqCluster.Labels["rabbitmq.com/pauseReconciliation"] = "true"
	if err := r.Client.Update(ctx, rabbitmqCluster); err != nil {
		return err
	}

	Log.Info("Paused cluster-operator reconciliation")
	return nil
}

// resumeClusterOperator resumes the RabbitMQ cluster-operator reconciliation
func (r *Reconciler) resumeClusterOperator(ctx context.Context, instance *rabbitmqv1beta1.RabbitMq) error {
	Log := r.GetLogger(ctx)

	rabbitmqCluster := &rabbitmqv2.RabbitmqCluster{}
	err := r.Client.Get(ctx, types.NamespacedName{Name: instance.Name, Namespace: instance.Namespace}, rabbitmqCluster)
	if err != nil {
		if k8s_errors.IsNotFound(err) {
			return nil
		}
		return err
	}

	if rabbitmqCluster.Labels != nil && rabbitmqCluster.Labels["rabbitmq.com/pauseReconciliation"] == "true" {
		delete(rabbitmqCluster.Labels, "rabbitmq.com/pauseReconciliation")
		if err := r.Client.Update(ctx, rabbitmqCluster); err != nil {
			return err
		}
		Log.Info("Resumed cluster-operator reconciliation")
	}

	return nil
}

// collectDefaultUserCredentials retrieves the default user credentials from the secret
func (r *Reconciler) collectDefaultUserCredentials(ctx context.Context, instance *rabbitmqv1beta1.RabbitMq) (string, string, error) {
	secretName := instance.Name + "-default-user"
	secret := &corev1.Secret{}

	err := r.Client.Get(ctx, types.NamespacedName{Name: secretName, Namespace: instance.Namespace}, secret)
	if err != nil {
		return "", "", err
	}

	username := string(secret.Data["username"])
	password := string(secret.Data["password"])

	if username == "" || password == "" {
		return "", "", fmt.Errorf("username or password not found in secret %s", secretName)
	}

	return username, password, nil
}

// deleteRabbitMQResources deletes the RabbitMQ cluster, pods, and PVCs
func (r *Reconciler) deleteRabbitMQResources(ctx context.Context, instance *rabbitmqv1beta1.RabbitMq) error {
	Log := r.GetLogger(ctx)

	// Delete RabbitMQ cluster
	rabbitmqCluster := &rabbitmqv2.RabbitmqCluster{}
	err := r.Client.Get(ctx, types.NamespacedName{Name: instance.Name, Namespace: instance.Namespace}, rabbitmqCluster)
	if err == nil {
		if err := r.Client.Delete(ctx, rabbitmqCluster); err != nil && !k8s_errors.IsNotFound(err) {
			return err
		}
		Log.Info("Deleted RabbitMQ cluster")
	}

	// Delete pods
	podList := &corev1.PodList{}
	listOpts := []client.ListOption{
		client.InNamespace(instance.Namespace),
		client.MatchingLabels{"app.kubernetes.io/name": instance.Name},
	}
	if err := r.Client.List(ctx, podList, listOpts...); err == nil {
		for _, pod := range podList.Items {
			if err := r.Client.Delete(ctx, &pod); err != nil && !k8s_errors.IsNotFound(err) {
				return err
			}
		}
		Log.Info(fmt.Sprintf("Deleted %d pods", len(podList.Items)))
	}

	// Delete PVCs
	pvcList := &corev1.PersistentVolumeClaimList{}
	if err := r.Client.List(ctx, pvcList, listOpts...); err == nil {
		for _, pvc := range pvcList.Items {
			if err := r.Client.Delete(ctx, &pvc); err != nil && !k8s_errors.IsNotFound(err) {
				return err
			}
		}
		Log.Info(fmt.Sprintf("Deleted %d PVCs", len(pvcList.Items)))
	}

	return nil
}

// rabbitmqResourcesExist checks if RabbitMQ resources still exist
func (r *Reconciler) rabbitmqResourcesExist(ctx context.Context, instance *rabbitmqv1beta1.RabbitMq) bool {
	rabbitmqCluster := &rabbitmqv2.RabbitmqCluster{}
	if err := r.Client.Get(ctx, types.NamespacedName{Name: instance.Name, Namespace: instance.Namespace}, rabbitmqCluster); err == nil {
		return true
	}

	podList := &corev1.PodList{}
	listOpts := []client.ListOption{
		client.InNamespace(instance.Namespace),
		client.MatchingLabels{"app.kubernetes.io/name": instance.Name},
	}
	if err := r.Client.List(ctx, podList, listOpts...); err == nil && len(podList.Items) > 0 {
		return true
	}

	return false
}

// isClusterReady checks if the RabbitMQ cluster is ready
func (r *Reconciler) isClusterReady(ctx context.Context, instance *rabbitmqv1beta1.RabbitMq) (bool, error) {
	rabbitmqCluster := &rabbitmqv2.RabbitmqCluster{}
	err := r.Client.Get(ctx, types.NamespacedName{Name: instance.Name, Namespace: instance.Namespace}, rabbitmqCluster)
	if err != nil {
		if k8s_errors.IsNotFound(err) {
			return false, nil
		}
		return false, err
	}

	if rabbitmqCluster.Status.ObservedGeneration != rabbitmqCluster.Generation {
		return false, nil
	}

	for _, cond := range rabbitmqCluster.Status.Conditions {
		if string(cond.Type) == "ClusterAvailable" && cond.Status == corev1.ConditionTrue {
			return true, nil
		}
	}

	return false, nil
}

// createUpgradeUser creates a RabbitMQUser with the preserved credentials
func (r *Reconciler) createUpgradeUser(ctx context.Context, instance *rabbitmqv1beta1.RabbitMq, username, password string) error {
	Log := r.GetLogger(ctx)

	userName := instance.Name + "-upgrade"
	user := &rabbitmqv1beta1.RabbitMQUser{
		ObjectMeta: metav1.ObjectMeta{
			Name:      userName,
			Namespace: instance.Namespace,
		},
		Spec: rabbitmqv1beta1.RabbitMQUserSpec{
			RabbitmqClusterName: instance.Name,
			Username:            username,
			Tags:                []string{"administrator"},
		},
	}

	// Check if user already exists
	existingUser := &rabbitmqv1beta1.RabbitMQUser{}
	err := r.Client.Get(ctx, types.NamespacedName{Name: userName, Namespace: instance.Namespace}, existingUser)
	if err == nil {
		Log.Info(fmt.Sprintf("RabbitMQUser %s already exists", userName))
		return nil
	}

	if !k8s_errors.IsNotFound(err) {
		return err
	}

	if err := controllerutil.SetControllerReference(instance, user, r.Scheme); err != nil {
		return err
	}

	if err := r.Client.Create(ctx, user); err != nil {
		return err
	}

	// Update the secret with the preserved password
	time.Sleep(2 * time.Second)
	secretName := fmt.Sprintf("rabbitmq-user-%s", userName)
	secret := &corev1.Secret{}
	if err := r.Client.Get(ctx, types.NamespacedName{Name: secretName, Namespace: instance.Namespace}, secret); err == nil {
		secret.Data["password"] = []byte(password)
		if err := r.Client.Update(ctx, secret); err != nil {
			return err
		}
	}

	Log.Info(fmt.Sprintf("Created RabbitMQUser %s with preserved credentials", userName))
	return nil
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
