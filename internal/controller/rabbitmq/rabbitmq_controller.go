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
	"strings"
	"time"

	apiextensionsv1 "k8s.io/apiextensions-apiserver/pkg/apis/apiextensions/v1"
	k8s_errors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	uns "k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"
	"k8s.io/apimachinery/pkg/fields"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/runtime/schema"
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

	"github.com/go-logr/logr"
	rabbitmqv1beta1 "github.com/openstack-k8s-operators/infra-operator/apis/rabbitmq/v1beta1"
	topologyv1 "github.com/openstack-k8s-operators/infra-operator/apis/topology/v1beta1"
	"github.com/openstack-k8s-operators/infra-operator/internal/rabbitmq"
	condition "github.com/openstack-k8s-operators/lib-common/modules/common/condition"
	"github.com/openstack-k8s-operators/lib-common/modules/common/helper"
	"github.com/openstack-k8s-operators/lib-common/modules/common/labels"
	"github.com/openstack-k8s-operators/lib-common/modules/common/ocp"
	"github.com/openstack-k8s-operators/lib-common/modules/common/pdb"
	common_rbac "github.com/openstack-k8s-operators/lib-common/modules/common/rbac"
	"github.com/openstack-k8s-operators/lib-common/modules/common/service"
	"github.com/openstack-k8s-operators/lib-common/modules/common/tls"
	"github.com/openstack-k8s-operators/lib-common/modules/common/util"
	appsv1 "k8s.io/api/apps/v1"
	corev1 "k8s.io/api/core/v1"
	rbacv1 "k8s.io/api/rbac/v1"
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

// RabbitMQ version upgrade constants
const (
	// DefaultRabbitMQVersion is the default RabbitMQ version when Spec.TargetVersion is not set
	DefaultRabbitMQVersion = "4.2"
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

// Required to cleanup old rabbitmq-cluster-operator RabbitmqCluster CRs during migration
// +kubebuilder:rbac:groups=rabbitmq.com,resources=rabbitmqclusters,verbs=get;list;watch;update;patch;delete

// Required for direct StatefulSet management
// +kubebuilder:rbac:groups=apps,resources=statefulsets,verbs=get;list;watch;create;update;patch;delete

// Required for RBAC resources (ServiceAccount, Role, RoleBinding)
// +kubebuilder:rbac:groups=core,resources=serviceaccounts,verbs=get;list;watch;create;update;patch;delete
// +kubebuilder:rbac:groups=rbac.authorization.k8s.io,resources=roles,verbs=get;list;watch;create;update;patch;delete
// +kubebuilder:rbac:groups=rbac.authorization.k8s.io,resources=rolebindings,verbs=get;list;watch;create;update;patch;delete

// Required for Secrets and ConfigMaps
// +kubebuilder:rbac:groups=core,resources=persistentvolumeclaims,verbs=get;list;watch;update;patch;delete
// +kubebuilder:rbac:groups=core,resources=secrets,verbs=get;list;watch;create;update;patch
// +kubebuilder:rbac:groups=core,resources=configmaps,verbs=get;list;watch;create;update;patch

// Required to grant endpoints/events permissions to RabbitMQ pods via Role
// +kubebuilder:rbac:groups=core,resources=endpoints,verbs=get;list;watch
// +kubebuilder:rbac:groups=core,resources=events,verbs=create

// Required to determine IPv6 and FIPS
// +kubebuilder:rbac:groups=config.openshift.io,resources=networks,verbs=get;list;watch;

// Required to exec into pods
// +kubebuilder:rbac:groups=core,resources=pods,verbs=get;list;watch;create;update;patch;delete;
// +kubebuilder:rbac:groups=core,resources=pods/exec,verbs=create

// Required to manage PodDisruptionBudgets for multi-replica deployments
// +kubebuilder:rbac:groups=policy,resources=poddisruptionbudgets,verbs=get;list;watch;create;update;patch;delete

// Required to create per-pod LoadBalancer services
// +kubebuilder:rbac:groups=core,resources=services,verbs=get;list;watch;create;update;patch;delete

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

	// Initialize RabbitMQ version in Status if not set.
	// For existing StatefulSets, infer version from the running cluster;
	// for new deployments, use TargetVersion or the default.
	if instance.Status.CurrentVersion == "" {
		initialVersion := DefaultRabbitMQVersion
		existingSts := &appsv1.StatefulSet{}
		stsKey := types.NamespacedName{Name: fmt.Sprintf("%s-server", instance.Name), Namespace: instance.Namespace}
		err := r.Get(ctx, stsKey, existingSts)
		if err == nil && !existingSts.CreationTimestamp.IsZero() {
			initialVersion = "3.9"
			Log.Info("Existing StatefulSet found - initializing CurrentVersion for upgrade tracking",
				"statefulset", stsKey, "initialVersion", initialVersion)
		} else if err == nil || k8s_errors.IsNotFound(err) {
			if instance.Spec.TargetVersion != nil && *instance.Spec.TargetVersion != "" {
				initialVersion = *instance.Spec.TargetVersion
			}
		} else {
			Log.Error(err, "Failed to check for existing StatefulSet during version initialization")
		}
		instance.Status.CurrentVersion = initialVersion
		Log.Info("Initialized RabbitMQ current version in status", "version", initialVersion)
		if err := helper.PatchInstance(ctx, instance); err != nil {
			return ctrl.Result{}, err
		}
		return ctrl.Result{Requeue: true}, nil
	}

	// Check if storage wipe is needed for version upgrades or queue type migration
	var requiresWipe bool
	switch instance.Status.UpgradePhase {
	case rabbitmqv1beta1.UpgradePhaseDeletingResources, rabbitmqv1beta1.UpgradePhaseWaitingForCluster:
		requiresWipe = true
	case rabbitmqv1beta1.UpgradePhaseNone:
		if instance.Spec.TargetVersion != nil && *instance.Spec.TargetVersion != "" {
			needsWipe, wipeErr := rabbitmq.RequiresStorageWipe(instance.Status.CurrentVersion, *instance.Spec.TargetVersion)
			if wipeErr != nil {
				Log.Error(wipeErr, "Failed to determine upgrade compatibility")
				return ctrl.Result{}, fmt.Errorf("failed to check upgrade compatibility: %w", wipeErr)
			}
			if needsWipe {
				requiresWipe = true
				instance.Status.WipeReason = rabbitmqv1beta1.WipeReasonVersionUpgrade
				Log.Info("RabbitMQ upgrade requires storage wipe",
					"currentVersion", instance.Status.CurrentVersion,
					"targetVersion", *instance.Spec.TargetVersion)
			}
		}
		// Check for Mirrored → Quorum queue type migration (without version change)
		if !requiresWipe && instance.Spec.QueueType != nil && *instance.Spec.QueueType == rabbitmqv1beta1.QueueTypeQuorum {
			if instance.Status.QueueType == rabbitmqv1beta1.QueueTypeMirrored {
				requiresWipe = true
				instance.Status.WipeReason = rabbitmqv1beta1.WipeReasonQueueTypeMigration
				Log.Info("Queue type change from Mirrored to Quorum requires storage wipe")
			}
		}
	}

	// initialize status if Conditions is nil, but do not reset if it already
	// exists
	isNewInstance := instance.Status.Conditions == nil
	if isNewInstance {
		instance.Status.Conditions = condition.Conditions{}
	}

	// Handle clients-reconfigured annotation BEFORE the deferred PatchInstance.
	if instance.DeletionTimestamp.IsZero() && instance.Annotations != nil {
		if configured, ok := instance.Annotations[rabbitmqv1beta1.AnnotationClientsReconfigured]; ok && configured == "true" {
			instance.Status.ProxyRequired = false
			if err := r.Client.Status().Update(ctx, instance); err != nil {
				return ctrl.Result{}, err
			}
			delete(instance.Annotations, rabbitmqv1beta1.AnnotationClientsReconfigured)
			if err := r.Client.Update(ctx, instance); err != nil {
				return ctrl.Result{}, err
			}
			Log.Info("Clients reconfigured - cleared ProxyRequired and removed annotation")
			return ctrl.Result{Requeue: true}, nil
		}
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
		// per-pod services ready
		condition.UnknownCondition(condition.CreateServiceReadyCondition, condition.InitReason, condition.CreateServiceReadyInitMessage),
	)

	// Only init conditions if they haven't been initialized yet
	// Otherwise Init() would reset all conditions to Unknown on every reconciliation
	if len(instance.Status.Conditions) == 0 {
		instance.Status.Conditions.Init(&cl)
	}
	instance.Status.ObservedGeneration = instance.Generation

	// Init Topology condition if there's a reference
	if instance.Spec.TopologyRef != nil {
		c := condition.UnknownCondition(condition.TopologyReadyCondition, condition.InitReason, condition.TopologyReadyInitMessage)
		instance.Status.Conditions.Set(c)
	}

	// If we're not deleting this and the service object doesn't have our finalizer, add it.
	if instance.DeletionTimestamp.IsZero() {
		if controllerutil.AddFinalizer(instance, helper.GetFinalizer()) {
			// Finalizer was added, will be persisted by defer PatchInstance
			return ctrl.Result{}, nil
		}
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

	// Calculate hash for config tracking
	// We'll use a simple hash of the config parameters
	configMapHash, err := util.ObjectHash(map[string]interface{}{
		"ipv6Enabled":      IPv6Enabled,
		"fipsEnabled":      fipsEnabled,
		"tlsSecret":        instance.Spec.TLS.SecretName,
		"additionalConfig": instance.Spec.Config.AdditionalConfig,
		"advancedConfig":   instance.Spec.Config.AdvancedConfig,
		"replicas":         instance.Spec.Replicas,
	})
	if err != nil {
		instance.Status.Conditions.Set(condition.FalseCondition(
			condition.ServiceConfigReadyCondition,
			condition.ErrorReason,
			condition.SeverityWarning,
			condition.ServiceConfigReadyErrorMessage,
			err.Error()))
		return ctrl.Result{}, fmt.Errorf("error calculating config hash: %w", err)
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

	//
	// Handle storage wipe phase 1: set initial phase.
	// Phase flow: None → DeletingResources → WaitingForCluster → None
	//
	if requiresWipe && instance.Status.UpgradePhase == rabbitmqv1beta1.UpgradePhaseNone {
		instance.Status.UpgradePhase = rabbitmqv1beta1.UpgradePhaseDeletingResources
		Log.Info("Starting storage wipe", "reason", instance.Status.WipeReason)
		if err := helper.PatchInstance(ctx, instance); err != nil {
			return ctrl.Result{}, err
		}
		return ctrl.Result{Requeue: true}, nil
	}

	// Determine the config version: use TargetVersion during upgrade,
	// otherwise use CurrentVersion.
	configVersion := ""
	if instance.Spec.TargetVersion != nil && *instance.Spec.TargetVersion != "" {
		configVersion = *instance.Spec.TargetVersion
	}
	if configVersion == "" {
		configVersion = instance.Status.CurrentVersion
		if configVersion == "" {
			configVersion = DefaultRabbitMQVersion
		}
	}

	// Determine if we need to add data-wipe init container
	needsDataWipe := instance.Status.UpgradePhase == rabbitmqv1beta1.UpgradePhaseDeletingResources ||
		instance.Status.UpgradePhase == rabbitmqv1beta1.UpgradePhaseWaitingForCluster

	// Preserve wipe-data init container if it already exists (avoid unnecessary pod restarts)
	if !needsDataWipe {
		existingSts := &appsv1.StatefulSet{}
		stsKey := types.NamespacedName{Name: fmt.Sprintf("%s-server", instance.Name), Namespace: instance.Namespace}
		if err := r.Get(ctx, stsKey, existingSts); err == nil {
			for _, c := range existingSts.Spec.Template.Spec.InitContainers {
				if c.Name == "wipe-data" {
					needsDataWipe = true
					break
				}
			}
		}
	}

	// Build RabbitMQ configuration environment variables
	envVars, err := rabbitmq.BuildRabbitMQConfig(instance, IPv6Enabled, fipsEnabled)
	if err != nil {
		instance.Status.Conditions.Set(condition.FalseCondition(
			condition.ServiceConfigReadyCondition,
			condition.ErrorReason,
			condition.SeverityWarning,
			condition.ServiceConfigReadyErrorMessage,
			err.Error()))
		return ctrl.Result{}, fmt.Errorf("error building RabbitMQ config: %w", err)
	}

	// Delete RBAC resources owned by old rabbitmq-cluster-operator so
	// ReconcileRbac can recreate them with the correct ownership.
	rbacName := instance.RbacResourceName()
	for _, obj := range []client.Object{
		&rbacv1.RoleBinding{ObjectMeta: metav1.ObjectMeta{Name: rbacName, Namespace: instance.Namespace}},
		&rbacv1.Role{ObjectMeta: metav1.ObjectMeta{Name: rbacName, Namespace: instance.Namespace}},
		&corev1.ServiceAccount{ObjectMeta: metav1.ObjectMeta{Name: rbacName, Namespace: instance.Namespace}},
	} {
		if err := r.Client.Get(ctx, types.NamespacedName{Name: obj.GetName(), Namespace: obj.GetNamespace()}, obj); err == nil {
			for _, ref := range obj.GetOwnerReferences() {
				if ref.Controller != nil && *ref.Controller && ref.UID != instance.UID {
					Log.Info("Deleting RBAC resource owned by old controller", "name", obj.GetName())
					if err := r.Client.Delete(ctx, obj); err != nil && !k8s_errors.IsNotFound(err) {
						return ctrl.Result{}, fmt.Errorf("failed to delete old RBAC resource %s: %w", obj.GetName(), err)
					}
					break
				}
			}
		}
	}

	// Reconcile RBAC for RabbitMQ pods
	rbacRules := []rbacv1.PolicyRule{
		{
			APIGroups:     []string{"security.openshift.io"},
			ResourceNames: []string{"anyuid"},
			Resources:     []string{"securitycontextconstraints"},
			Verbs:         []string{"use"},
		},
		{
			APIGroups: []string{""},
			Resources: []string{"endpoints"},
			Verbs:     []string{"get", "list", "watch"},
		},
		{
			APIGroups: []string{""},
			Resources: []string{"events"},
			Verbs:     []string{"create"},
		},
	}
	rbacResult, err := common_rbac.ReconcileRbac(ctx, helper, instance, rbacRules)
	if err != nil {
		return rbacResult, err
	} else if (rbacResult != ctrl.Result{}) {
		return rbacResult, nil
	}

	// Ensure Erlang cookie secret exists
	err = r.ensureErlangCookie(ctx, helper, instance)
	if err != nil {
		return ctrl.Result{}, err
	}

	// Ensure default user secret exists (optional, for initial setup)
	err = r.ensureDefaultUser(ctx, helper, instance)
	if err != nil {
		return ctrl.Result{}, err
	}

	// Generate plugins ConfigMap
	pluginsCm := rabbitmq.GeneratePluginsConfigMap(instance)
	pcmop, err := controllerutil.CreateOrPatch(ctx, r.Client, pluginsCm, func() error {
		pluginsCm.Data = rabbitmq.GeneratePluginsConfigMap(instance).Data
		adoptResource(pluginsCm, instance.UID)
		return controllerutil.SetControllerReference(instance, pluginsCm, r.Scheme)
	})
	if err != nil {
		return ctrl.Result{}, err
	}
	if pcmop != controllerutil.OperationResultNone {
		Log.Info(fmt.Sprintf("ConfigMap %s - %s", pluginsCm.Name, pcmop))
	}

	// Generate server configuration ConfigMap
	serverCm := rabbitmq.GenerateServerConfigMap(instance, IPv6Enabled, fipsEnabled, configVersion)
	scmop, err := controllerutil.CreateOrPatch(ctx, r.Client, serverCm, func() error {
		serverCm.Data = rabbitmq.GenerateServerConfigMap(instance, IPv6Enabled, fipsEnabled, configVersion).Data
		adoptResource(serverCm, instance.UID)
		return controllerutil.SetControllerReference(instance, serverCm, r.Scheme)
	})
	if err != nil {
		return ctrl.Result{}, err
	}
	if scmop != controllerutil.OperationResultNone {
		Log.Info(fmt.Sprintf("ConfigMap %s - %s", serverCm.Name, scmop))
	}

	// Generate config-data ConfigMap (for inter-node TLS config)
	configDataCm := rabbitmq.GenerateConfigDataConfigMap(instance, fipsEnabled, configVersion)
	cdcmop, err := controllerutil.CreateOrPatch(ctx, r.Client, configDataCm, func() error {
		configDataCm.Data = rabbitmq.GenerateConfigDataConfigMap(instance, fipsEnabled, configVersion).Data
		adoptResource(configDataCm, instance.UID)
		return controllerutil.SetControllerReference(instance, configDataCm, r.Scheme)
	})
	if err != nil {
		return ctrl.Result{}, err
	}
	if cdcmop != controllerutil.OperationResultNone {
		Log.Info(fmt.Sprintf("ConfigMap %s - %s", configDataCm.Name, cdcmop))
	}

	// Create headless service for StatefulSet
	headlessSvc := rabbitmq.HeadlessService(instance)

	hsop, err := controllerutil.CreateOrPatch(ctx, r.Client, headlessSvc, func() error {
		desired := rabbitmq.HeadlessService(instance)
		headlessSvc.Spec.Ports = desired.Spec.Ports
		headlessSvc.Spec.Selector = desired.Spec.Selector
		headlessSvc.Labels = desired.Labels
		adoptResource(headlessSvc, instance.UID)
		return controllerutil.SetControllerReference(instance, headlessSvc, r.Scheme)
	})
	if err != nil {
		return ctrl.Result{}, err
	}
	if hsop != controllerutil.OperationResultNone {
		Log.Info(fmt.Sprintf("Headless Service %s - %s", headlessSvc.Name, hsop))
	}

	// Create client service
	clientSvc := rabbitmq.ClientService(instance)

	csop, err := controllerutil.CreateOrPatch(ctx, r.Client, clientSvc, func() error {
		desired := rabbitmq.ClientService(instance)
		clientSvc.Spec.Ports = desired.Spec.Ports
		clientSvc.Spec.Selector = desired.Spec.Selector
		clientSvc.Spec.Type = desired.Spec.Type
		// Merge annotations: set our desired ones without removing externally-added
		// annotations (e.g. from MetalLB) to avoid reconcile loops
		if clientSvc.Annotations == nil {
			clientSvc.Annotations = map[string]string{}
		}
		for k, v := range desired.Annotations {
			clientSvc.Annotations[k] = v
		}
		clientSvc.Labels = desired.Labels
		adoptResource(clientSvc, instance.UID)
		return controllerutil.SetControllerReference(instance, clientSvc, r.Scheme)
	})
	if err != nil {
		return ctrl.Result{}, err
	}
	if csop != controllerutil.OperationResultNone {
		Log.Info(fmt.Sprintf("Client Service %s - %s", clientSvc.Name, csop))
	}

	// Create/Update StatefulSet
	// Use a minimal object for CreateOrPatch to avoid pre-populated fields
	// interfering with the JSON merge patch computation.
	sts := &appsv1.StatefulSet{
		ObjectMeta: metav1.ObjectMeta{
			Name:      fmt.Sprintf("%s-server", instance.Name),
			Namespace: instance.Namespace,
		},
	}

	stsop, err := controllerutil.CreateOrPatch(ctx, r.Client, sts, func() error {
		// Adopt from old owner if needed (migration from rabbitmq-cluster-operator)
		adoptResource(sts, instance.UID)
		err := controllerutil.SetControllerReference(instance, sts, r.Scheme)
		if err != nil {
			return err
		}

		desired := rabbitmq.StatefulSet(instance, configMapHash, topology, envVars, configVersion, needsDataWipe)
		isCreate := sts.CreationTimestamp.IsZero()

		if isCreate {
			// On create, set everything including immutable fields
			sts.Spec = desired.Spec
			sts.Labels = desired.Labels
		} else {
			// On update, preserve immutable fields (Selector, VolumeClaimTemplates, ServiceName)
			existingSelectorLabels := map[string]string{}
			if sts.Spec.Selector != nil {
				for k, v := range sts.Spec.Selector.MatchLabels {
					existingSelectorLabels[k] = v
				}
			}

			sts.Labels = desired.Labels
			sts.Spec.Replicas = desired.Spec.Replicas
			sts.Spec.Template.Labels = desired.Spec.Template.Labels
			sts.Spec.Template.Annotations = desired.Spec.Template.Annotations
			sts.Spec.Template.Spec.ServiceAccountName = desired.Spec.Template.Spec.ServiceAccountName
			sts.Spec.Template.Spec.Volumes = desired.Spec.Template.Spec.Volumes
			sts.Spec.Template.Spec.SecurityContext = desired.Spec.Template.Spec.SecurityContext
			sts.Spec.Template.Spec.Affinity = desired.Spec.Template.Spec.Affinity
			sts.Spec.Template.Spec.NodeSelector = desired.Spec.Template.Spec.NodeSelector
			sts.Spec.Template.Spec.Tolerations = desired.Spec.Template.Spec.Tolerations
			sts.Spec.Template.Spec.TopologySpreadConstraints = desired.Spec.Template.Spec.TopologySpreadConstraints
			sts.Spec.Template.Spec.AutomountServiceAccountToken = desired.Spec.Template.Spec.AutomountServiceAccountToken
			if desired.Spec.Template.Spec.TerminationGracePeriodSeconds != nil {
				sts.Spec.Template.Spec.TerminationGracePeriodSeconds = desired.Spec.Template.Spec.TerminationGracePeriodSeconds
			}

			// Update containers individually to preserve server-defaulted fields
			if len(sts.Spec.Template.Spec.Containers) == len(desired.Spec.Template.Spec.Containers) {
				mergeContainers(sts.Spec.Template.Spec.Containers, desired.Spec.Template.Spec.Containers)
			} else {
				sts.Spec.Template.Spec.Containers = desired.Spec.Template.Spec.Containers
			}
			sts.Spec.Template.Spec.InitContainers = mergeOrReplaceInitContainers(
				sts.Spec.Template.Spec.InitContainers, desired.Spec.Template.Spec.InitContainers)

			// Merge preserved selector labels into the new template labels
			for k, v := range existingSelectorLabels {
				sts.Spec.Template.Labels[k] = v
			}
			// Selector, ServiceName, VolumeClaimTemplates, PodManagementPolicy
			// are NOT updated — they are immutable and preserved from the server state.
		}

		return nil
	})
	if err != nil {
		return ctrl.Result{}, err
	}
	if stsop != controllerutil.OperationResultNone {
		Log.Info(fmt.Sprintf("StatefulSet %s - %s", sts.Name, stsop))
	}

	//
	// Handle storage wipe phase 2: delete StatefulSet.
	// The StatefulSet spec has been updated above (via CreateOrPatch) with the
	// wipe init container. Now we delete the StatefulSet so it is recreated
	// with the new spec including the wipe init container.
	//
	if requiresWipe && instance.Status.UpgradePhase == rabbitmqv1beta1.UpgradePhaseDeletingResources {
		Log.Info("Deleting StatefulSet for storage wipe", "reason", instance.Status.WipeReason)

		// Delete ha-all policy if migrating from Mirrored queues
		if instance.Status.QueueType == rabbitmqv1beta1.QueueTypeMirrored {
			if err := deleteMirroredPolicy(ctx, helper, instance); err != nil {
				Log.Error(err, "Failed to delete ha-all policy during storage wipe")
				return ctrl.Result{}, err
			}
		}

		// Delete the StatefulSet so it is recreated with the wipe init container
		stsToDelete := &appsv1.StatefulSet{}
		stsDeleteName := types.NamespacedName{Name: fmt.Sprintf("%s-server", instance.Name), Namespace: instance.Namespace}
		if err := r.Get(ctx, stsDeleteName, stsToDelete); err != nil {
			if !k8s_errors.IsNotFound(err) {
				return ctrl.Result{}, err
			}
			Log.Info("StatefulSet already deleted", "name", stsDeleteName.Name)
		} else {
			if err := r.Delete(ctx, stsToDelete); err != nil && !k8s_errors.IsNotFound(err) {
				return ctrl.Result{}, err
			}
			Log.Info("Deleted StatefulSet for storage wipe", "name", stsDeleteName.Name)
		}

		// Update queue type status for queue migrations
		if instance.Status.WipeReason == rabbitmqv1beta1.WipeReasonQueueTypeMigration && instance.Spec.QueueType != nil {
			instance.Status.QueueType = *instance.Spec.QueueType
			Log.Info("Updated Status.QueueType after queue migration", "queueType", instance.Status.QueueType)
		}

		instance.Status.UpgradePhase = rabbitmqv1beta1.UpgradePhaseWaitingForCluster
		if err := helper.PatchInstance(ctx, instance); err != nil {
			return ctrl.Result{}, err
		}
		return ctrl.Result{Requeue: true}, nil
	}

	// Check StatefulSet readiness
	clusterReady := false
	if sts.Status.ObservedGeneration == sts.Generation &&
		sts.Status.ReadyReplicas == *sts.Spec.Replicas &&
		sts.Status.ReadyReplicas > 0 {
		clusterReady = true
	}

	if clusterReady {
		instance.Status.Conditions.MarkTrue(condition.DeploymentReadyCondition, condition.DeploymentReadyMessage)
		instance.Status.ReadyCount = *sts.Spec.Replicas

		// If we just completed a storage wipe, update CurrentVersion and clear UpgradePhase.
		// The default-user secret survives because we only deleted the StatefulSet.
		if instance.Status.UpgradePhase == rabbitmqv1beta1.UpgradePhaseWaitingForCluster {
			// Set ProxyRequired for any Mirrored → Quorum migration (version upgrade or queue migration).
			// Must be checked BEFORE clearing WipeReason and updating CurrentVersion.
			if !instance.Status.ProxyRequired && instance.Spec.QueueType != nil && *instance.Spec.QueueType == rabbitmqv1beta1.QueueTypeQuorum {
				// Proxy needed for: version upgrade from 3.x→4.x, or explicit queue type migration
				isVersionUpgradeWithMigration := instance.Spec.TargetVersion != nil && *instance.Spec.TargetVersion != "" &&
					rabbitmq.Is3xTo4xUpgrade(instance.Status.CurrentVersion, *instance.Spec.TargetVersion)
				isQueueTypeMigration := instance.Status.WipeReason == rabbitmqv1beta1.WipeReasonQueueTypeMigration

				if isVersionUpgradeWithMigration || isQueueTypeMigration {
					instance.Status.ProxyRequired = true
					Log.Info("Enabling proxy for Mirrored to Quorum migration",
						"wipeReason", instance.Status.WipeReason)
				}
			}

			instance.Status.UpgradePhase = rabbitmqv1beta1.UpgradePhaseNone
			instance.Status.WipeReason = rabbitmqv1beta1.WipeReasonNone

			if instance.Spec.TargetVersion != nil && *instance.Spec.TargetVersion != "" {
				instance.Status.CurrentVersion = *instance.Spec.TargetVersion
				Log.Info("Version upgrade complete", "version", *instance.Spec.TargetVersion)
			} else {
				Log.Info("Queue migration complete - cluster recreated with new queue type")
			}
		}

		labelMap := map[string]string{
			labels.K8sAppName:      instance.Name,
			labels.K8sAppComponent: "rabbitmq",
			labels.K8sAppPartOf:    "rabbitmq",
		}

		if instance.Spec.Replicas != nil && *instance.Spec.Replicas > 1 {
			// Apply PDB for multi-replica deployments
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

		// Create per-pod services when podOverride is configured
		if instance.Spec.Replicas != nil && *instance.Spec.Replicas > 0 &&
			instance.Spec.PodOverride != nil && len(instance.Spec.PodOverride.Services) > 0 {
			ctrlResult, err := r.reconcilePerPodServices(ctx, instance, helper, labelMap)
			if err != nil {
				instance.Status.Conditions.Set(condition.FalseCondition(
					condition.CreateServiceReadyCondition,
					condition.ErrorReason,
					condition.SeverityWarning,
					condition.CreateServiceReadyErrorMessage, err.Error()))
				return ctrlResult, err
			} else if (ctrlResult != ctrl.Result{}) {
				instance.Status.Conditions.Set(condition.FalseCondition(
					condition.CreateServiceReadyCondition,
					condition.RequestedReason,
					condition.SeverityInfo,
					condition.CreateServiceReadyRunningMessage))
				return ctrlResult, nil
			}
		} else if len(instance.Status.ServiceHostnames) > 0 {
			// PodOverride was removed, clean up per-pod services
			if err := r.deletePerPodServices(ctx, instance); err != nil {
				instance.Status.Conditions.Set(condition.FalseCondition(
					condition.CreateServiceReadyCondition,
					condition.ErrorReason,
					condition.SeverityWarning,
					condition.CreateServiceReadyErrorMessage, err.Error()))
				return ctrl.Result{}, err
			}
			instance.Status.ServiceHostnames = nil
		}
		instance.Status.Conditions.MarkTrue(condition.CreateServiceReadyCondition, condition.CreateServiceReadyMessage)

		// Sync Status.QueueType with Spec.QueueType
		if instance.Spec.QueueType != nil {
			if *instance.Spec.QueueType == rabbitmqv1beta1.QueueTypeMirrored && *instance.Spec.Replicas > 1 && instance.Status.QueueType != rabbitmqv1beta1.QueueTypeMirrored {
				Log.Info("Applying ha-all policy for Mirrored queues")
				if err := ensureMirroredPolicy(ctx, helper, instance); err != nil {
					Log.Error(err, "Could not apply ha-all policy")
					instance.Status.Conditions.Set(condition.FalseCondition(
						condition.DeploymentReadyCondition,
						condition.ErrorReason,
						condition.SeverityWarning,
						condition.DeploymentReadyErrorMessage, err.Error()))
					return ctrl.Result{}, err
				}
				instance.Status.QueueType = rabbitmqv1beta1.QueueTypeMirrored
			} else if *instance.Spec.QueueType != rabbitmqv1beta1.QueueTypeMirrored && instance.Status.QueueType == rabbitmqv1beta1.QueueTypeMirrored {
				Log.Info("QueueType changed from Mirrored, removing ha-all policy")
				if err := deleteMirroredPolicy(ctx, helper, instance); err != nil {
					Log.Error(err, "Could not remove ha-all policy")
					instance.Status.Conditions.Set(condition.FalseCondition(
						condition.DeploymentReadyCondition,
						condition.ErrorReason,
						condition.SeverityWarning,
						condition.DeploymentReadyErrorMessage, err.Error()))
					return ctrl.Result{}, err
				}
			}

			if *instance.Spec.QueueType == rabbitmqv1beta1.QueueTypeQuorum && instance.Status.QueueType != rabbitmqv1beta1.QueueTypeQuorum {
				Log.Info("Setting queue type status to Quorum")
				instance.Status.QueueType = rabbitmqv1beta1.QueueTypeQuorum
			}
		}
	}

	// After all resources are reparented, clean up the old RabbitmqCluster CR (once)
	if !instance.Status.OldCRCleaned {
		if err := r.cleanupOldRabbitmqClusterCR(ctx, instance); err != nil {
			return ctrl.Result{}, fmt.Errorf("failed to cleanup old RabbitmqCluster CR: %w", err)
		}
		instance.Status.OldCRCleaned = true
	}

	if instance.Status.Conditions.AllSubConditionIsTrue() {
		instance.Status.Conditions.MarkTrue(
			condition.ReadyCondition, condition.ReadyMessage)
	}
	return ctrl.Result{}, nil
}

func (r *Reconciler) reconcilePerPodServices(ctx context.Context, instance *rabbitmqv1beta1.RabbitMq, helper *helper.Helper, labelMap map[string]string) (ctrl.Result, error) {
	Log := r.GetLogger(ctx)

	if instance.Spec.PodOverride == nil || len(instance.Spec.PodOverride.Services) == 0 {
		Log.Info("PodOverride not configured, skipping per-pod service creation")
		instance.Status.ServiceHostnames = nil
		return ctrl.Result{}, nil
	}

	replicas := int(*instance.Spec.Replicas)

	if len(instance.Spec.PodOverride.Services) != replicas {
		return ctrl.Result{}, fmt.Errorf("number of services in podOverride (%d) must match number of replicas (%d)", len(instance.Spec.PodOverride.Services), replicas)
	}

	Log.Info("Creating per-pod services using podOverride configuration")

	var serviceHostnames []string
	var requeueNeeded bool
	for i := 0; i < replicas; i++ {
		podName := fmt.Sprintf("%s-server-%d", instance.Name, i)
		svcName := podName

		svc, err := service.NewService(
			service.GenericService(&service.GenericServiceDetails{
				Name:      svcName,
				Namespace: instance.Namespace,
				Labels:    labelMap,
				Selector: map[string]string{
					appsv1.StatefulSetPodNameLabel: podName,
				},
				Ports: []corev1.ServicePort{
					{Name: "amqp", Port: 5672, TargetPort: intstr.FromInt(5672)},
					{Name: "amqps", Port: 5671, TargetPort: intstr.FromInt(5671)},
				},
			}),
			5,
			&instance.Spec.PodOverride.Services[i],
		)
		if err != nil {
			instance.Status.Conditions.Set(condition.FalseCondition(
				condition.CreateServiceReadyCondition,
				condition.ErrorReason,
				condition.SeverityWarning,
				condition.CreateServiceReadyErrorMessage, err.Error()))
			return ctrl.Result{}, err
		}

		if svc.GetServiceType() == corev1.ServiceTypeLoadBalancer {
			svc.AddAnnotation(map[string]string{
				service.AnnotationHostnameKey: svc.GetServiceHostname(),
			})
		}

		ctrlResult, err := svc.CreateOrPatch(ctx, helper)
		if err != nil {
			// Check if this is a LoadBalancer IP pending error - if so, continue with other services
			if k8s_errors.IsServiceUnavailable(err) || strings.Contains(err.Error(), "LoadBalancer IP still pending") {
				requeueNeeded = true
			} else {
				// Real error, return immediately
				instance.Status.Conditions.Set(condition.FalseCondition(
					condition.CreateServiceReadyCondition,
					condition.ErrorReason,
					condition.SeverityWarning,
					condition.CreateServiceReadyErrorMessage, err.Error()))
				return ctrlResult, err
			}
		} else if (ctrlResult != ctrl.Result{}) {
			requeueNeeded = true
		}

		serviceHostnames = append(serviceHostnames, svc.GetServiceHostname())
		instance.Status.ServiceHostnames = serviceHostnames
	}

	if requeueNeeded {
		instance.Status.Conditions.Set(condition.FalseCondition(
			condition.CreateServiceReadyCondition,
			condition.RequestedReason,
			condition.SeverityInfo,
			condition.CreateServiceReadyRunningMessage))
		return ctrl.Result{RequeueAfter: time.Second * 5}, nil
	}

	return ctrl.Result{}, nil
}

func (r *Reconciler) deletePerPodServices(ctx context.Context, instance *rabbitmqv1beta1.RabbitMq) error {
	// Only delete per-pod services (named <instance>-server-N), not the
	// headless or client services which are also owned by this instance.
	serviceList := &corev1.ServiceList{}
	listOpts := []client.ListOption{
		client.InNamespace(instance.Namespace),
	}

	if err := r.List(ctx, serviceList, listOpts...); err != nil {
		return err
	}

	prefix := instance.Name + "-server-"
	for _, svc := range serviceList.Items {
		if !strings.HasPrefix(svc.Name, prefix) {
			continue
		}
		for _, ownerRef := range svc.GetOwnerReferences() {
			if ownerRef.UID == instance.UID {
				if err := r.Delete(ctx, &svc); err != nil && !k8s_errors.IsNotFound(err) {
					return err
				}
				break
			}
		}
	}
	return nil
}

func ensureMirroredPolicy(ctx context.Context, helper *helper.Helper, instance *rabbitmqv1beta1.RabbitMq) error {
	policyName := types.NamespacedName{
		Name:      instance.Name + "-ha-all",
		Namespace: instance.Namespace,
	}

	policy := &rabbitmqv1beta1.RabbitMQPolicy{}
	err := helper.GetClient().Get(ctx, policyName, policy)
	if err == nil {
		return nil // already exists
	}
	if !k8s_errors.IsNotFound(err) {
		return err
	}

	definition := map[string]interface{}{
		"ha-mode":                "exactly",
		"ha-params":              2,
		"ha-promote-on-shutdown": "always",
	}
	definitionJSON, marshalErr := json.Marshal(definition)
	if marshalErr != nil {
		return marshalErr
	}

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

func deleteMirroredPolicy(ctx context.Context, helper *helper.Helper, instance *rabbitmqv1beta1.RabbitMq) error {
	policyName := types.NamespacedName{
		Name:      instance.Name + "-ha-all",
		Namespace: instance.Namespace,
	}

	policy := &rabbitmqv1beta1.RabbitMQPolicy{}
	err := helper.GetClient().Get(ctx, policyName, policy)
	if k8s_errors.IsNotFound(err) {
		return nil
	}
	if err != nil {
		return err
	}
	return helper.GetClient().Delete(ctx, policy)
}

func (r *Reconciler) reconcileDelete(ctx context.Context, instance *rabbitmqv1beta1.RabbitMq, helper *helper.Helper) (ctrl.Result, error) {
	Log := r.GetLogger(ctx)
	Log.Info("Reconciling Service delete")

	// Delete per-pod services if they exist
	if err := r.deletePerPodServices(ctx, instance); err != nil {
		return ctrl.Result{}, err
	}

	// Resources (StatefulSet, Services, Secrets, ConfigMaps) will be automatically
	// garbage collected by Kubernetes due to owner references

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

// ensureErlangCookie ensures the Erlang cookie secret exists
func (r *Reconciler) ensureErlangCookie(
	ctx context.Context,
	_ *helper.Helper,
	instance *rabbitmqv1beta1.RabbitMq,
) error {
	Log := r.GetLogger(ctx)
	secretName := fmt.Sprintf("%s-erlang-cookie", instance.Name)
	secret := &corev1.Secret{
		ObjectMeta: metav1.ObjectMeta{
			Name:      secretName,
			Namespace: instance.Namespace,
		},
	}

	op, err := controllerutil.CreateOrPatch(ctx, r.Client, secret, func() error {
		adoptResource(secret, instance.UID)
		if err := controllerutil.SetControllerReference(instance, secret, r.Scheme); err != nil {
			return err
		}

		// Only generate the cookie on first creation
		if secret.Data == nil || len(secret.Data) == 0 {
			generated, genErr := rabbitmq.GenerateErlangCookie(instance)
			if genErr != nil {
				return fmt.Errorf("failed to generate erlang cookie: %w", genErr)
			}
			secret.Type = corev1.SecretTypeOpaque
			secret.Data = generated.Data
		}

		return nil
	})
	if err != nil {
		return fmt.Errorf("failed to ensure erlang cookie secret: %w", err)
	}
	if op != controllerutil.OperationResultNone {
		Log.Info(fmt.Sprintf("Erlang cookie secret %s - %s", secretName, op))
	}

	return nil
}

// ensureDefaultUser ensures the default user secret exists and keeps host/port up to date.
// Credentials (username, password) are only generated on first creation.
// Host and port are updated on every reconcile to reflect TLS config changes.
func (r *Reconciler) ensureDefaultUser(
	ctx context.Context,
	_ *helper.Helper,
	instance *rabbitmqv1beta1.RabbitMq,
) error {
	Log := r.GetLogger(ctx)
	secretName := fmt.Sprintf("%s-default-user", instance.Name)
	secret := &corev1.Secret{
		ObjectMeta: metav1.ObjectMeta{
			Name:      secretName,
			Namespace: instance.Namespace,
		},
	}

	// Determine host and port based on current config
	host := fmt.Sprintf("%s.%s.svc", instance.Name, instance.Namespace)
	port := fmt.Sprintf("%d", 5672)
	if instance.Spec.TLS.SecretName != "" {
		port = fmt.Sprintf("%d", 5671)
	}

	op, err := controllerutil.CreateOrPatch(ctx, r.Client, secret, func() error {
		adoptResource(secret, instance.UID)
		if err := controllerutil.SetControllerReference(instance, secret, r.Scheme); err != nil {
			return err
		}

		// Only generate credentials on first creation
		if secret.Data == nil || len(secret.Data) == 0 {
			generated, genErr := rabbitmq.GenerateDefaultUser(instance)
			if genErr != nil {
				return fmt.Errorf("failed to generate default user: %w", genErr)
			}
			secret.Type = corev1.SecretTypeOpaque
			secret.Data = generated.Data
		}

		// Ensure default_user.conf exists (may be missing when migrating from old operator)
		if _, ok := secret.Data["default_user.conf"]; !ok {
			username := string(secret.Data["username"])
			password := string(secret.Data["password"])
			defaultUserConf := fmt.Sprintf("default_user = %s\ndefault_pass = %s\ndefault_user_tags.administrator = true\n", username, password)
			secret.Data["default_user.conf"] = []byte(defaultUserConf)
		}

		// Always update host and port (may change with TLS config)
		secret.Data["host"] = []byte(host)
		secret.Data["port"] = []byte(port)

		return nil
	})
	if err != nil {
		return fmt.Errorf("failed to ensure default user secret: %w", err)
	}
	if op != controllerutil.OperationResultNone {
		Log.Info(fmt.Sprintf("Default user secret %s - %s", secretName, op))
	}

	// Update status with default user information
	instance.Status.DefaultUser = &rabbitmqv1beta1.RabbitmqClusterDefaultUser{
		SecretReference: &rabbitmqv1beta1.RabbitmqClusterSecretReference{
			Name:      secretName,
			Namespace: instance.Namespace,
			Keys: map[string]string{
				"username": "username",
				"password": "password",
			},
		},
		ServiceReference: &rabbitmqv1beta1.RabbitmqClusterServiceReference{
			Name:      instance.Name,
			Namespace: instance.Namespace,
		},
	}

	return nil
}

// mergeContainers updates existing containers in-place with desired values,
// preserving server-defaulted fields to avoid reconcile loops.
func mergeContainers(existing []corev1.Container, desired []corev1.Container) {
	for i, d := range desired {
		if i >= len(existing) {
			break
		}
		existing[i].Name = d.Name
		existing[i].Image = d.Image
		existing[i].Command = d.Command
		existing[i].Args = d.Args
		existing[i].Env = d.Env
		existing[i].Ports = d.Ports
		existing[i].VolumeMounts = d.VolumeMounts
		existing[i].Resources = d.Resources
		existing[i].LivenessProbe = d.LivenessProbe
		existing[i].ReadinessProbe = d.ReadinessProbe
		existing[i].Lifecycle = d.Lifecycle
		existing[i].SecurityContext = d.SecurityContext
	}
}

// mergeOrReplaceInitContainers merges desired init containers into existing ones,
// preserving server-defaulted fields. If the count changes, replaces the list.
func mergeOrReplaceInitContainers(existing, desired []corev1.Container) []corev1.Container {
	if len(existing) != len(desired) {
		return desired
	}
	for i, d := range desired {
		existing[i].Name = d.Name
		existing[i].Image = d.Image
		existing[i].Command = d.Command
		existing[i].Args = d.Args
		existing[i].Env = d.Env
		existing[i].VolumeMounts = d.VolumeMounts
		existing[i].Resources = d.Resources
		existing[i].SecurityContext = d.SecurityContext
	}
	return existing
}

// adoptResource removes any foreign controller owner references from the object
// so that SetControllerReference can set the new owner without conflict.
// This is needed during migration from the old rabbitmq-cluster-operator, where
// resources (StatefulSet, Service, Secret) may still be owned by the old RabbitmqCluster CR.
func adoptResource(obj metav1.Object, newOwnerUID types.UID) {
	var cleaned []metav1.OwnerReference
	for _, ref := range obj.GetOwnerReferences() {
		if ref.Controller != nil && *ref.Controller && ref.UID != newOwnerUID {
			continue
		}
		cleaned = append(cleaned, ref)
	}
	obj.SetOwnerReferences(cleaned)
}

// cleanupOldRabbitmqClusterCR deletes the old rabbitmq.com/v1beta1 RabbitmqCluster CR
// that is no longer needed after resources have been reparented to the new RabbitMq CR.
func (r *Reconciler) cleanupOldRabbitmqClusterCR(ctx context.Context, instance *rabbitmqv1beta1.RabbitMq) error {
	Log := r.GetLogger(ctx)

	// Try to find a RabbitmqCluster CR with the same name in the same namespace
	oldCR := &uns.Unstructured{}
	oldCR.SetGroupVersionKind(schema.GroupVersionKind{
		Group:   "rabbitmq.com",
		Version: "v1beta1",
		Kind:    "RabbitmqCluster",
	})
	err := r.Client.Get(ctx, types.NamespacedName{Name: instance.Name, Namespace: instance.Namespace}, oldCR)
	if err != nil {
		if k8s_errors.IsNotFound(err) || k8s_errors.IsForbidden(err) ||
			strings.Contains(err.Error(), "no matches for kind") {
			// Not found, CRD doesn't exist, or no permission — nothing to clean up
			return nil
		}
		return err
	}

	// Strip old owner references from PVCs before deleting the old CR,
	// otherwise Kubernetes GC will cascade-delete the PVCs.
	oldUID := oldCR.GetUID()
	pvcList := &corev1.PersistentVolumeClaimList{}
	if err := r.Client.List(ctx, pvcList, client.InNamespace(instance.Namespace)); err != nil {
		return fmt.Errorf("failed to list PVCs: %w", err)
	}
	for i := range pvcList.Items {
		pvc := &pvcList.Items[i]
		needsUpdate := false
		var cleaned []metav1.OwnerReference
		for _, ref := range pvc.OwnerReferences {
			if ref.UID == oldUID {
				needsUpdate = true
				continue
			}
			cleaned = append(cleaned, ref)
		}
		if needsUpdate {
			pvc.OwnerReferences = cleaned
			if err := r.Client.Update(ctx, pvc); err != nil {
				if !k8s_errors.IsNotFound(err) {
					Log.Error(err, "Failed to strip old owner from PVC", "pvc", pvc.Name)
				}
			} else {
				Log.Info("Stripped old RabbitmqCluster owner from PVC", "pvc", pvc.Name)
			}
		}
	}

	// Remove finalizers (the old operator that would handle them is gone)
	if len(oldCR.GetFinalizers()) > 0 {
		oldCR.SetFinalizers(nil)
		if err := r.Client.Update(ctx, oldCR); err != nil {
			return fmt.Errorf("failed to remove finalizers from old RabbitmqCluster %s: %w", instance.Name, err)
		}
	}

	if err := r.Client.Delete(ctx, oldCR); err != nil {
		if !k8s_errors.IsNotFound(err) {
			return fmt.Errorf("failed to delete old RabbitmqCluster %s: %w", instance.Name, err)
		}
	} else {
		Log.Info("Deleted old RabbitmqCluster CR after reparenting", "name", instance.Name)
	}

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
		Owns(&appsv1.StatefulSet{}).
		Owns(&corev1.Service{}).
		Owns(&corev1.ServiceAccount{}).
		Owns(&rbacv1.Role{}).
		Owns(&rbacv1.RoleBinding{}).
		Owns(&corev1.Secret{}).
		Owns(&corev1.ConfigMap{}).
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

// findObjectsForSrc - returns a reconcile request if the object is referenced by a RabbitMq CR
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
