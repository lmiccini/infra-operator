/*
Copyright 2025.

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
	"crypto/rand"
	"encoding/base64"
	"fmt"
	"strings"
	"time"

	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/controller/controllerutil"
	"sigs.k8s.io/controller-runtime/pkg/handler"
	"sigs.k8s.io/controller-runtime/pkg/log"
	"sigs.k8s.io/controller-runtime/pkg/reconcile"

	rabbitmqv1 "github.com/openstack-k8s-operators/infra-operator/apis/rabbitmq/v1beta1"
	rabbitmqapi "github.com/openstack-k8s-operators/infra-operator/pkg/rabbitmq/api"
	condition "github.com/openstack-k8s-operators/lib-common/modules/common/condition"
	helper "github.com/openstack-k8s-operators/lib-common/modules/common/helper"
	"github.com/openstack-k8s-operators/lib-common/modules/common/object"
	oko_secret "github.com/openstack-k8s-operators/lib-common/modules/common/secret"
	"github.com/openstack-k8s-operators/lib-common/modules/common/util"
	dataplanev1 "github.com/openstack-k8s-operators/openstack-operator/api/dataplane/v1beta1"
	rabbitmqclusterv2 "github.com/rabbitmq/cluster-operator/v2/api/v1beta1"
	k8s_errors "k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/types"
	"k8s.io/client-go/kubernetes"
)

// userFinalizer is the controller-level finalizer for RabbitMQUser resources.
// Note: rabbitmqv1.UserFinalizer exists in the API types but is reserved for
// TransportURL-owned users. This separate constant is used by the controller
// for its own lifecycle management.
const userFinalizer = "rabbitmquser.openstack.org/finalizer"

// credentialSecretNameField is the field index for the credential secret
const credentialSecretNameField = ".spec.secret"

// ConnectionCheckInterval is how often to recheck deployment and nodeset status during deletion
const ConnectionCheckInterval = 30 * time.Second

// DeletionGracePeriod is the default time to wait before allowing deletion despite check failures
// This prevents indefinite blocking on transient API errors or RBAC issues
const DeletionGracePeriod = 1 * time.Hour

// Annotation keys for deletion safety
const (
	// FirstDeletionCheckFailureAnnotation records when deletion checks first started failing
	FirstDeletionCheckFailureAnnotation = "rabbitmq.openstack.org/first-deletion-check-failure"
	// DeletionGracePeriodAnnotation allows overriding the default grace period (duration format like "2h", "30m")
	DeletionGracePeriodAnnotation = "rabbitmq.openstack.org/deletion-grace-period"
)

// RabbitMQ user secret prefix
const rabbitmqUserSecretPrefix = "rabbitmq-user-"

// generatePassword generates a random password
func generatePassword(length int) (string, error) {
	bytes := make([]byte, length)
	_, err := rand.Read(bytes)
	if err != nil {
		return "", err
	}
	return base64.URLEncoding.EncodeToString(bytes)[:length], nil
}

// RabbitMQUserReconciler reconciles a RabbitMQUser object
//
//nolint:revive
type RabbitMQUserReconciler struct {
	client.Client
	Kclient kubernetes.Interface
	Scheme  *runtime.Scheme
}

//+kubebuilder:rbac:groups=rabbitmq.openstack.org,resources=rabbitmqusers,verbs=get;list;watch;create;update;patch;delete
//+kubebuilder:rbac:groups=rabbitmq.openstack.org,resources=rabbitmqusers/status,verbs=get;update;patch
//+kubebuilder:rbac:groups=rabbitmq.openstack.org,resources=rabbitmqusers/finalizers,verbs=update
//+kubebuilder:rbac:groups=rabbitmq.openstack.org,resources=rabbitmqvhosts,verbs=get;list;watch;update;patch
//+kubebuilder:rbac:groups=rabbitmq.openstack.org,resources=rabbitmqvhosts/finalizers,verbs=update
//+kubebuilder:rbac:groups=rabbitmq.com,resources=rabbitmqclusters,verbs=get;list;watch
//+kubebuilder:rbac:groups=core,resources=secrets,verbs=get;list;watch;create;update;patch;delete
//+kubebuilder:rbac:groups=dataplane.openstack.org,resources=openstackdataplanenodesets,verbs=get;list;watch

// Reconcile reconciles a RabbitMQUser object
func (r *RabbitMQUserReconciler) Reconcile(ctx context.Context, req ctrl.Request) (ctrl.Result, error) {
	Log := log.FromContext(ctx)

	instance := &rabbitmqv1.RabbitMQUser{}
	err := r.Get(ctx, req.NamespacedName, instance)
	if err != nil {
		if k8s_errors.IsNotFound(err) {
			return ctrl.Result{}, nil
		}
		return ctrl.Result{}, err
	}

	h, _ := helper.NewHelper(instance, r.Client, r.Kclient, r.Scheme, Log)

	// Save a copy of the conditions so that we can restore the LastTransitionTime
	// when a condition's state doesn't change
	savedConditions := instance.Status.Conditions.DeepCopy()

	// Initialize status conditions
	cl := condition.CreateList(
		condition.UnknownCondition(condition.ReadyCondition, condition.InitReason, condition.ReadyInitMessage),
		condition.UnknownCondition(rabbitmqv1.RabbitMQUserReadyCondition, condition.InitReason, rabbitmqv1.RabbitMQUserReadyInitMessage),
	)
	instance.Status.Conditions.Init(&cl)
	instance.Status.ObservedGeneration = instance.Generation

	defer func() {
		// Restore condition timestamps if they haven't changed
		condition.RestoreLastTransitionTimes(&instance.Status.Conditions, savedConditions)

		if instance.Status.Conditions.IsUnknown(condition.ReadyCondition) {
			instance.Status.Conditions.Set(instance.Status.Conditions.Mirror(condition.ReadyCondition))
		}
		if err := h.PatchInstance(ctx, instance); err != nil {
			Log.Error(err, "Failed to patch instance")
		}
	}()

	// Handle deletion
	if !instance.DeletionTimestamp.IsZero() {
		return r.reconcileDelete(ctx, instance, h)
	}

	// Two-phase finalizer addition:
	// 1. Add user finalizer first - prevents user deletion before vhost finalizer is added
	// 2. Add vhost finalizer second - ensures user is protected during vhost finalizer addition
	//
	// If deletion occurs between phases, reconcileDelete handles it with best-effort cleanup
	if controllerutil.AddFinalizer(instance, userFinalizer) {
		Log.Info("Added user finalizer, will reconcile again to add vhost finalizer")
		return ctrl.Result{}, nil
	}

	// Add vhost finalizer after user finalizer exists
	if instance.Spec.VhostRef != "" {
		username := instance.Spec.Username

		// Validate that username is set before creating finalizer
		// The webhook should have set this, but we validate defensively
		if username == "" {
			err := fmt.Errorf("spec.Username is empty, cannot create vhost finalizer")
			instance.Status.Conditions.Set(condition.FalseCondition(
				rabbitmqv1.RabbitMQUserReadyCondition,
				condition.ErrorReason,
				condition.SeverityWarning,
				rabbitmqv1.RabbitMQUserReadyErrorMessage,
				err.Error()))
			return ctrl.Result{}, err
		}

		vhostFinalizer := rabbitmqv1.UserVhostFinalizerPrefix + username

		vhost := &rabbitmqv1.RabbitMQVhost{}
		if err := r.Get(ctx, types.NamespacedName{Name: instance.Spec.VhostRef, Namespace: instance.Namespace}, vhost); err != nil {
			instance.Status.Conditions.Set(condition.FalseCondition(rabbitmqv1.RabbitMQUserReadyCondition, condition.ErrorReason, condition.SeverityWarning, rabbitmqv1.RabbitMQUserReadyErrorMessage, err.Error()))
			return ctrl.Result{}, err
		}

		// Add per-user finalizer to vhost to prevent deletion while this user exists
		if controllerutil.AddFinalizer(vhost, vhostFinalizer) {
			if err := r.Update(ctx, vhost); err != nil {
				return ctrl.Result{}, fmt.Errorf("failed to add finalizer %s to vhost %s: %w", vhostFinalizer, instance.Spec.VhostRef, err)
			}
			Log.Info("Added finalizer to vhost", "vhost", instance.Spec.VhostRef, "finalizer", vhostFinalizer)
		}
	}

	return r.reconcileNormal(ctx, instance, h)
}

func (r *RabbitMQUserReconciler) reconcileNormal(ctx context.Context, instance *rabbitmqv1.RabbitMQUser, h *helper.Helper) (ctrl.Result, error) {
	Log := log.FromContext(ctx)

	var password string
	var secretName string

	// Username is now always stored in spec.Username by the webhook
	username := instance.Spec.Username

	// Handle VhostRef changes - remove finalizer from old vhost if changed
	// We track the previous vhost CR name in status.VhostRef to detect changes
	userFinalizer := rabbitmqv1.UserVhostFinalizerPrefix + username
	if instance.Status.VhostRef != "" && instance.Status.VhostRef != instance.Spec.VhostRef {
		// VhostRef changed - remove finalizer from old vhost
		oldVhost := &rabbitmqv1.RabbitMQVhost{}
		if err := r.Get(ctx, types.NamespacedName{Name: instance.Status.VhostRef, Namespace: instance.Namespace}, oldVhost); err == nil {
			if controllerutil.RemoveFinalizer(oldVhost, userFinalizer) {
				if err := r.Update(ctx, oldVhost); err != nil {
					// Requeue to retry - this is important for VhostRef changes
					return ctrl.Result{RequeueAfter: time.Duration(2) * time.Second}, fmt.Errorf("failed to remove finalizer %s from old vhost %s: %w", userFinalizer, instance.Status.VhostRef, err)
				}
			}
		} else if !k8s_errors.IsNotFound(err) {
			// If we get an error other than NotFound, return it to requeue
			return ctrl.Result{}, fmt.Errorf("failed to get old vhost %s for finalizer removal: %w", instance.Status.VhostRef, err)
		}
	}

	// Get vhost - default to "/" if VhostRef is empty
	vhostName := "/"
	var vhost *rabbitmqv1.RabbitMQVhost
	if instance.Spec.VhostRef != "" {
		vhost = &rabbitmqv1.RabbitMQVhost{}
		if err := r.Get(ctx, types.NamespacedName{Name: instance.Spec.VhostRef, Namespace: instance.Namespace}, vhost); err != nil {
			instance.Status.Conditions.Set(condition.FalseCondition(rabbitmqv1.RabbitMQUserReadyCondition, condition.ErrorReason, condition.SeverityWarning, rabbitmqv1.RabbitMQUserReadyErrorMessage, err.Error()))
			return ctrl.Result{}, err
		}

		vhostName = vhost.Spec.Name

		// Add per-user finalizer to vhost to prevent deletion while this user exists
		// Design note: Using per-user finalizers (rabbitmquser.rabbitmq.openstack.org/user-<name>)
		// instead of a shared finalizer avoids the need for reference counting.
		if controllerutil.AddFinalizer(vhost, userFinalizer) {
			if err := r.Update(ctx, vhost); err != nil {
				// Requeue to retry - this ensures the finalizer is eventually added
				return ctrl.Result{RequeueAfter: time.Duration(2) * time.Second}, fmt.Errorf("failed to add finalizer %s to vhost %s: %w", userFinalizer, instance.Spec.VhostRef, err)
			}
		}
	}

	// Get RabbitMQ cluster
	rabbit := &rabbitmqclusterv2.RabbitmqCluster{}
	err := r.Get(ctx, types.NamespacedName{Name: instance.Spec.RabbitmqClusterName, Namespace: instance.Namespace}, rabbit)
	if err != nil {
		instance.Status.Conditions.Set(condition.FalseCondition(rabbitmqv1.RabbitMQUserReadyCondition, condition.ErrorReason, condition.SeverityWarning, rabbitmqv1.RabbitMQUserReadyErrorMessage, err.Error()))
		return ctrl.Result{}, err
	}

	// Determine credentials source
	var op controllerutil.OperationResult
	if instance.Spec.Secret != nil && *instance.Spec.Secret != "" {
		// Use user-provided secret
		secretName = *instance.Spec.Secret
		userSecret, _, err := oko_secret.GetSecret(ctx, h, secretName, instance.Namespace)
		if err != nil {
			instance.Status.Conditions.Set(condition.FalseCondition(
				rabbitmqv1.RabbitMQUserReadyCondition,
				condition.ErrorReason,
				condition.SeverityWarning,
				rabbitmqv1.RabbitMQUserReadyErrorMessage,
				fmt.Sprintf("failed to get credential secret %s: %v", secretName, err)))
			return ctrl.Result{}, err
		}

		// Default credential selectors if not set (defensive coding)
		if instance.Spec.CredentialSelectors == nil {
			instance.Spec.CredentialSelectors = &rabbitmqv1.CredentialSelectors{
				Username: "username",
				Password: "password",
			}
		}

		// Extract password using credential selector
		passwordBytes, ok := userSecret.Data[instance.Spec.CredentialSelectors.Password]
		if !ok {
			err := fmt.Errorf("password key %q not found in secret %s",
				instance.Spec.CredentialSelectors.Password, secretName)
			instance.Status.Conditions.Set(condition.FalseCondition(
				rabbitmqv1.RabbitMQUserReadyCondition,
				condition.ErrorReason,
				condition.SeverityWarning,
				rabbitmqv1.RabbitMQUserReadyErrorMessage,
				err.Error()))
			return ctrl.Result{}, err
		}
		password = string(passwordBytes)

		// Always update the RabbitMQ user when using user-provided secrets to ensure
		// password is synced if it changed in the secret.
		// CreateOrUpdateUser is idempotent so this is safe to call on every reconcile.
		// We set this to Created if this is the first time (status empty), otherwise Updated.
		if instance.Status.Username == "" {
			op = controllerutil.OperationResultCreated
		} else {
			// Treat as updated to ensure password changes are propagated
			op = controllerutil.OperationResultUpdated
		}
	} else {
		// Existing auto-generation logic for backward compatibility
		secretName = fmt.Sprintf("rabbitmq-user-%s", instance.Name)
		userSecret, _, err := oko_secret.GetSecret(ctx, h, secretName, instance.Namespace)

		if err != nil {
			if k8s_errors.IsNotFound(err) {
				password, err = generatePassword(32)
				if err != nil {
					instance.Status.Conditions.Set(condition.FalseCondition(rabbitmqv1.RabbitMQUserReadyCondition, condition.ErrorReason, condition.SeverityWarning, rabbitmqv1.RabbitMQUserReadyErrorMessage, err.Error()))
					return ctrl.Result{}, err
				}
			} else {
				instance.Status.Conditions.Set(condition.FalseCondition(rabbitmqv1.RabbitMQUserReadyCondition, condition.ErrorReason, condition.SeverityWarning, rabbitmqv1.RabbitMQUserReadyErrorMessage, err.Error()))
				return ctrl.Result{}, err
			}
		} else {
			password = string(userSecret.Data["password"])
		}

		// Create or update user secret
		secret := &corev1.Secret{
			ObjectMeta: metav1.ObjectMeta{
				Name:      secretName,
				Namespace: instance.Namespace,
			},
			Data: map[string][]byte{},
		}

		op, err = controllerutil.CreateOrUpdate(ctx, r.Client, secret, func() error {
			secret.Data["username"] = []byte(username)
			secret.Data["password"] = []byte(password)
			return controllerutil.SetControllerReference(instance, secret, r.Scheme)
		})
		if err != nil {
			instance.Status.Conditions.Set(condition.FalseCondition(rabbitmqv1.RabbitMQUserReadyCondition, condition.ErrorReason, condition.SeverityWarning, rabbitmqv1.RabbitMQUserReadyErrorMessage, err.Error()))
			return ctrl.Result{}, err
		}
	}

	// Update status with credentials info immediately, before attempting RabbitMQ operations
	// This ensures status is recorded even if RabbitMQ operations fail
	instance.Status.SecretName = secretName
	instance.Status.Username = username
	instance.Status.VhostRef = instance.Spec.VhostRef // Track the vhost CR name for finalizer management
	// Note: status.Vhost will be updated after successful permission operations

	// Create/update user in RabbitMQ if:
	// 1. Secret was just created (new user)
	// 2. Secret was updated (for user-provided secrets, password might have changed)
	// 3. Vhost changed (need to update permissions)
	// Note: We check if status.Vhost differs from spec, including when status is empty
	vhostChanged := instance.Status.Vhost != vhostName
	oldPermissionsDeleted := true
	if op == controllerutil.OperationResultCreated || op == controllerutil.OperationResultUpdated || vhostChanged {
		// Get admin credentials
		rabbitSecret, _, err := oko_secret.GetSecret(ctx, h, rabbit.Status.DefaultUser.SecretReference.Name, instance.Namespace)
		if err != nil {
			instance.Status.Conditions.Set(condition.FalseCondition(rabbitmqv1.RabbitMQUserReadyCondition, condition.ErrorReason, condition.SeverityWarning, rabbitmqv1.RabbitMQUserReadyErrorMessage, err.Error()))
			return ctrl.Result{}, err
		}

		// Create API client
		baseURL := getManagementURL(rabbit, rabbitSecret)
		tlsEnabled := rabbit.Spec.TLS.SecretName != ""
		caCert, err := getTLSCACert(ctx, h, rabbit, instance.Namespace)
		if err != nil {
			instance.Status.Conditions.Set(condition.FalseCondition(rabbitmqv1.RabbitMQUserReadyCondition, condition.ErrorReason, condition.SeverityWarning, rabbitmqv1.RabbitMQUserReadyErrorMessage, err.Error()))
			return ctrl.Result{}, err
		}
		apiClient := rabbitmqapi.NewClient(baseURL, string(rabbitSecret.Data["username"]), string(rabbitSecret.Data["password"]), tlsEnabled, caCert)

		// If vhost changed and there was a previous vhost, delete permissions from old vhost first
		if vhostChanged && instance.Status.Vhost != "" {
			Log.Info("Vhost changed, deleting permissions from old vhost", "old_vhost", instance.Status.Vhost, "new_vhost", vhostName, "username", username)
			if err := apiClient.DeletePermissions(instance.Status.Vhost, username); err != nil {
				// Track that old permissions weren't deleted - we'll retry on next reconciliation
				// We continue to set new permissions so the user works in the new vhost,
				// but we won't update status.Vhost until old permissions are cleaned up
				oldPermissionsDeleted = false
				Log.Error(err, "Failed to delete permissions from old vhost, will retry", "old_vhost", instance.Status.Vhost, "username", username)
			}
		}

		// Create user
		tags := instance.Spec.Tags
		if tags == nil {
			tags = []string{}
		}
		err = apiClient.CreateOrUpdateUser(username, password, tags)
		if err != nil {
			instance.Status.Conditions.Set(condition.FalseCondition(rabbitmqv1.RabbitMQUserReadyCondition, condition.ErrorReason, condition.SeverityWarning, rabbitmqv1.RabbitMQUserReadyErrorMessage, err.Error()))
			return ctrl.Result{}, err
		}

		// Set permissions
		// Note: instance.Spec.Permissions is never nil because the field doesn't use omitempty.
		// Individual permission fields (Configure/Write/Read) are guaranteed to have values
		// either from user input or from kubebuilder defaults (".*" for full permissions).
		err = apiClient.SetPermissions(vhostName, username,
			instance.Spec.Permissions.Configure,
			instance.Spec.Permissions.Write,
			instance.Spec.Permissions.Read)
		if err != nil {
			instance.Status.Conditions.Set(condition.FalseCondition(rabbitmqv1.RabbitMQUserReadyCondition, condition.ErrorReason, condition.SeverityWarning, rabbitmqv1.RabbitMQUserReadyErrorMessage, err.Error()))
			return ctrl.Result{}, err
		}
	}

	// Only update status.Vhost if old permissions were successfully deleted
	// This ensures we keep track of the old vhost and retry cleanup on next reconciliation
	if oldPermissionsDeleted {
		instance.Status.Vhost = vhostName
	}
	instance.Status.Conditions.MarkTrue(rabbitmqv1.RabbitMQUserReadyCondition, rabbitmqv1.RabbitMQUserReadyMessage)
	instance.Status.Conditions.MarkTrue(condition.ReadyCondition, condition.ReadyMessage)

	return ctrl.Result{}, nil
}

// isUserStillInUseByNodeSets checks if any nodeset still has nodes that haven't been
// updated with new credentials yet. This prevents premature deletion when nodes are
// offline or when partial deployments (AnsibleLimit) haven't covered all nodes.
func (r *RabbitMQUserReconciler) isUserStillInUseByNodeSets(
	ctx context.Context,
	instance *rabbitmqv1.RabbitMQUser,
	secretName string,
) (stillInUse bool, nodesetInfo string, err error) {
	log := log.FromContext(ctx)

	// 1. Get THIS user's secret
	secret := &corev1.Secret{}
	err = r.Get(ctx, types.NamespacedName{
		Name:      secretName,
		Namespace: instance.Namespace,
	}, secret)
	if err != nil {
		if k8s_errors.IsNotFound(err) {
			// Secret doesn't exist - can't be in use
			log.Info("Secret not found, not in use", "secret", secretName)
			return false, "", nil
		}
		// Conservative: if we can't get the secret, block deletion
		return true, "", fmt.Errorf("failed to get secret: %w", err)
	}

	// 2. Compute THIS user's secret hash
	if secret.Data == nil {
		// Secret exists but has no data - can't be in use
		log.Info("Secret has no data, not in use", "secret", secretName)
		return false, "", nil
	}

	thisUserSecretHash, err := util.ObjectHash(secret.Data)
	if err != nil {
		// Conservative: if we can't compute hash, block deletion
		return true, "", fmt.Errorf("failed to compute secret hash: %w", err)
	}

	log.Info("Checking nodesets for credential usage",
		"secret", secretName,
		"thisUserHash", thisUserSecretHash)

	// 3. List OpenStackDataPlaneNodeSets in the same namespace
	// NOTE: This lists all nodesets in the namespace. For large-scale deployments,
	// consider adding field indexing or label selectors to filter by credential usage.
	nodesets := &dataplanev1.OpenStackDataPlaneNodeSetList{}
	if err := r.List(ctx, nodesets, client.InNamespace(instance.Namespace)); err != nil {
		// Conservative: if we can't list nodesets, block deletion
		return true, "", fmt.Errorf("failed to list OpenStackDataPlaneNodeSets: %w", err)
	}

	if len(nodesets.Items) == 0 {
		log.Info("No nodesets found in namespace - credentials not in use",
			"namespace", instance.Namespace,
			"secret", secretName)
		return false, "", nil
	}

	log.V(1).Info("Checking credential status across nodesets",
		"nodesetCount", len(nodesets.Items),
		"namespace", instance.Namespace)

	// Track nodesets with empty status
	// This is important for the bootstrapping case where tracking hasn't run yet
	var nodesetsWithEmptyStatus []string

	// 4. Check each nodeset's credential status
	for i := range nodesets.Items {
		nodeset := &nodesets.Items[i]

		// Check for context cancellation
		if err := ctx.Err(); err != nil {
			// Context cancelled (e.g., pod shutdown)
			// Return error so caller can apply grace period logic
			return true, "", fmt.Errorf("context cancelled during nodeset check: %w", err)
		}

		// Check if nodeset has no credential status
		// This can happen when:
		// 1. New tracking code deployed, old deployments not yet reconciled
		// 2. Deployments ran before tracking was implemented
		if nodeset.Status.ServiceCredentialStatus == nil {
			log.Info("Nodeset has no credential status - cannot verify safety",
				"nodeset", nodeset.Name,
				"namespace", nodeset.Namespace)
			nodesetsWithEmptyStatus = append(nodesetsWithEmptyStatus, nodeset.Name)
			continue
		}

		// Check each service in the nodeset
		for serviceName, credInfo := range nodeset.Status.ServiceCredentialStatus {
			// Check if this service uses this secret name (current version)
			if credInfo.SecretName == secretName {
				// Check if it's using THIS user's hash
				if credInfo.SecretHash == thisUserSecretHash {
					// NodeSet is tracking this credential - means dataplane nodes have been
					// deployed with this credential and are using it
					// BLOCK deletion regardless of allNodesUpdated status
					info := fmt.Sprintf("nodeset %s/%s, service %s: %d/%d nodes have this credential (allNodesUpdated: %v)",
						nodeset.Namespace, nodeset.Name, serviceName,
						len(credInfo.UpdatedNodes), credInfo.TotalNodes, credInfo.AllNodesUpdated)

					log.Info("User credentials deployed to nodeset (current version), blocking deletion",
						"nodeset", nodeset.Name,
						"namespace", nodeset.Namespace,
						"service", serviceName,
						"updatedNodes", len(credInfo.UpdatedNodes),
						"totalNodes", credInfo.TotalNodes,
						"allNodesUpdated", credInfo.AllNodesUpdated,
						"secretHash", thisUserSecretHash)

					return true, info, nil
				}
			}

			// BUG FIX: Also check if this service uses this secret as PREVIOUS version
			// During credential rotation, nodes may still have old credentials.
			// The PreviousSecretHash field is only set when a new credential is deployed but
			// not all nodes have it yet. It's cleared when AllNodesUpdated becomes true for
			// the current version. So if PreviousSecretHash exists, by definition some nodes
			// still have the old credential and we must block deletion.
			if credInfo.PreviousSecretName == secretName {
				if credInfo.PreviousSecretHash == thisUserSecretHash {
					// Previous credential still tracked - means rotation in progress
					// BLOCK deletion until all nodes migrated to new version
					info := fmt.Sprintf("nodeset %s/%s, service %s: previous credential still tracked during rotation (current: %d/%d nodes updated)",
						nodeset.Namespace, nodeset.Name, serviceName,
						len(credInfo.UpdatedNodes), credInfo.TotalNodes)

					log.Info("User credentials in previous version during rotation, blocking deletion",
						"nodeset", nodeset.Name,
						"namespace", nodeset.Namespace,
						"service", serviceName,
						"currentSecret", credInfo.SecretName,
						"previousSecret", credInfo.PreviousSecretName,
						"updatedNodes", len(credInfo.UpdatedNodes),
						"totalNodes", credInfo.TotalNodes,
						"allNodesUpdated", credInfo.AllNodesUpdated,
						"previousSecretHash", thisUserSecretHash)

					return true, info, nil
				}
			}
		}
	}

	// Check if any nodesets had empty status
	// If so, we cannot safely determine if credentials are in use
	// Return error to trigger grace period and allow time for status to be populated
	if len(nodesetsWithEmptyStatus) > 0 {
		return true, "", fmt.Errorf("cannot verify credential safety: %d nodeset(s) have empty serviceCredentialStatus - "+
			"credential tracking may not be initialized yet. "+
			"Will retry after grace period allows operators to reconcile and populate status",
			len(nodesetsWithEmptyStatus))
	}

	// No nodesets are using this user's credentials on nodes that haven't been updated
	log.Info("All deletion safety checks passed - credentials not in use by any nodesets",
		"secret", secretName,
		"thisUserHash", thisUserSecretHash,
		"nodesetsChecked", len(nodesets.Items))
	return false, "", nil
}

func (r *RabbitMQUserReconciler) reconcileDelete(ctx context.Context, instance *rabbitmqv1.RabbitMQUser, h *helper.Helper) (ctrl.Result, error) {
	Log := log.FromContext(ctx)

	// If TransportURL finalizer exists, wait for TransportURL to remove it
	// The TransportURL controller manages this finalizer and removes it when switching users
	if controllerutil.ContainsFinalizer(instance, rabbitmqv1.TransportURLFinalizer) {
		instance.Status.Conditions.MarkFalse(
			rabbitmqv1.RabbitMQUserReadyCondition,
			condition.DeletingReason,
			condition.SeverityInfo,
			"Waiting for TransportURL to release user (finalizer: %s)",
			rabbitmqv1.TransportURLFinalizer,
		)
		return ctrl.Result{RequeueAfter: time.Duration(2) * time.Second}, nil
	}

	// Check if cleanup-blocked finalizer exists and remove it if safety checks pass
	// This finalizer was used as a temporary measure to prevent automatic cleanup
	// Now we check nodeset status to determine if it's safe to remove
	if controllerutil.ContainsFinalizer(instance, rabbitmqv1.RabbitMQUserCleanupBlockedFinalizer) {
		// Get secret name for nodeset check
		secretName := instance.Status.SecretName
		if secretName == "" {
			if instance.Spec.Secret != nil && *instance.Spec.Secret != "" {
				secretName = *instance.Spec.Secret
			} else {
				// Auto-generated secret name
				secretName = fmt.Sprintf("%s%s", rabbitmqUserSecretPrefix, instance.Name)
			}
		}

		if secretName != "" {
			// Check if any nodeset still has nodes that haven't been updated
			// This prevents deletion when nodes are offline or partial deployments haven't covered all nodes
			stillInUse, nodesetInfo, err := r.isUserStillInUseByNodeSets(ctx, instance, secretName)
			if err != nil {
				// Check if we've exceeded the grace period for check failures
				// This prevents indefinite blocking on transient API errors or RBAC issues
				now := time.Now()
				gracePeriod := DeletionGracePeriod

				// Check for custom grace period annotation
				if customPeriod, ok := instance.Annotations[DeletionGracePeriodAnnotation]; ok {
					if parsed, parseErr := time.ParseDuration(customPeriod); parseErr == nil {
						gracePeriod = parsed
						Log.Info("Using custom deletion grace period from annotation",
							"gracePeriod", gracePeriod,
							"annotation", DeletionGracePeriodAnnotation)
					} else {
						Log.Error(parseErr, "Failed to parse custom grace period annotation, using default",
							"annotation", customPeriod,
							"default", DeletionGracePeriod)
					}
				}

				// Check if we already have a failure timestamp
				firstFailureStr, hasFailureAnnotation := instance.Annotations[FirstDeletionCheckFailureAnnotation]
				var shouldAllowDeletion bool

				if hasFailureAnnotation {
					// Parse the existing timestamp
					if firstFailure, parseErr := time.Parse(time.RFC3339, firstFailureStr); parseErr == nil {
						elapsed := now.Sub(firstFailure)
						if elapsed > gracePeriod {
							// Grace period exceeded - allow deletion despite check failure
							shouldAllowDeletion = true
							Log.Info("Deletion check failures exceeded grace period, allowing deletion",
								"secret", secretName,
								"firstFailure", firstFailure,
								"elapsed", elapsed,
								"gracePeriod", gracePeriod,
								"checkError", err)
						} else {
							Log.Info("Deletion check failed but within grace period, continuing to block",
								"secret", secretName,
								"firstFailure", firstFailure,
								"elapsed", elapsed,
								"gracePeriod", gracePeriod,
								"remaining", gracePeriod-elapsed)
						}
					} else {
						// Invalid timestamp - reset it
						Log.Error(parseErr, "Failed to parse first failure timestamp, resetting",
							"annotation", firstFailureStr)
						shouldAllowDeletion = false
						// Will set new annotation below
						hasFailureAnnotation = false
					}
				}

				if !shouldAllowDeletion {
					// Record first failure time if not already set
					if !hasFailureAnnotation {
						if instance.Annotations == nil {
							instance.Annotations = make(map[string]string)
						}
						instance.Annotations[FirstDeletionCheckFailureAnnotation] = now.Format(time.RFC3339)

						if updateErr := r.Update(ctx, instance); updateErr != nil {
							Log.Error(updateErr, "Failed to update first deletion check failure annotation")
							// Continue anyway - we'll retry next reconcile
						} else {
							Log.Info("Recorded first deletion check failure timestamp",
								"secret", secretName,
								"timestamp", now.Format(time.RFC3339),
								"gracePeriod", gracePeriod)
						}
					}

					// Block deletion and wait
					Log.Error(err, "Failed to check nodeset status, delaying cleanup-blocked finalizer removal", "secret", secretName)

					instance.Status.Conditions.MarkFalse(
						rabbitmqv1.RabbitMQUserReadyCondition,
						condition.DeletingReason,
						condition.SeverityWarning,
						"Unable to verify nodeset status: %v",
						err,
					)

					return ctrl.Result{RequeueAfter: ConnectionCheckInterval}, nil
				}

				// Grace period exceeded - clear the annotation and allow deletion to proceed
				if hasFailureAnnotation {
					delete(instance.Annotations, FirstDeletionCheckFailureAnnotation)
					if updateErr := r.Update(ctx, instance); updateErr != nil {
						Log.Error(updateErr, "Failed to clear first deletion check failure annotation")
						// Continue anyway - deletion safety is more important
					}
				}

				Log.Info("Allowing deletion despite check failure - grace period exceeded",
					"secret", secretName,
					"gracePeriod", gracePeriod)
				// Fall through to allow deletion
			}

			if stillInUse {
				Log.Info("Credentials still in use by nodesets, keeping cleanup-blocked finalizer",
					"secret", secretName,
					"nodesetInfo", nodesetInfo)

				instance.Status.Conditions.MarkFalse(
					rabbitmqv1.RabbitMQUserReadyCondition,
					condition.DeletingReason,
					condition.SeverityInfo,
					"Credentials still in use: %s",
					nodesetInfo,
				)

				return ctrl.Result{RequeueAfter: ConnectionCheckInterval}, nil
			}

			Log.Info("All nodesets have been updated, removing cleanup-blocked finalizer", "secret", secretName)
		}

		// Clear any deletion check failure annotation since checks are now succeeding
		if _, hasFailureAnnotation := instance.Annotations[FirstDeletionCheckFailureAnnotation]; hasFailureAnnotation {
			delete(instance.Annotations, FirstDeletionCheckFailureAnnotation)
			Log.Info("Clearing deletion check failure annotation - checks now succeeding")
		}

		// All safety checks passed - remove the cleanup-blocked finalizer
		controllerutil.RemoveFinalizer(instance, rabbitmqv1.RabbitMQUserCleanupBlockedFinalizer)
		if err := r.Update(ctx, instance); err != nil {
			return ctrl.Result{}, fmt.Errorf("failed to remove cleanup-blocked finalizer: %w", err)
		}

		Log.Info("Removed cleanup-blocked finalizer, requeuing for deletion", "user", instance.Name)
		return ctrl.Result{Requeue: true}, nil
	}

	// Check for other external finalizers (not managed by this controller)
	// Wait for all external finalizers to be removed before proceeding with cleanup
	// This ensures other controllers (e.g., dataplane) finish using this user before deletion
	externalFinalizers := []string{}
	for _, finalizer := range instance.GetFinalizers() {
		if !rabbitmqv1.IsInternalFinalizer(finalizer) {
			externalFinalizers = append(externalFinalizers, finalizer)
		}
	}

	if len(externalFinalizers) > 0 {
		Log.Info("Waiting for external finalizers to be removed before deleting user",
			"user", instance.Name,
			"finalizers", strings.Join(externalFinalizers, ", "))

		instance.Status.Conditions.MarkFalse(
			rabbitmqv1.RabbitMQUserReadyCondition,
			condition.DeletingReason,
			condition.SeverityInfo,
			"Waiting for external finalizers to be removed: %s",
			strings.Join(externalFinalizers, ", "),
		)
		return ctrl.Result{RequeueAfter: time.Duration(2) * time.Second}, nil
	}

	// All safety checks passed - ready to delete user
	Log.Info("All safety checks passed, proceeding with user deletion",
		"user", instance.Name,
		"username", instance.Spec.Username)

	// Mark as ready for deletion
	instance.Status.Conditions.MarkTrue(
		rabbitmqv1.RabbitMQUserReadyCondition,
		"RabbitMQ user ready for deletion",
	)

	// Remove per-user finalizer from vhost if it exists
	// Use VhostRef from status (current) or spec (fallback) to find the vhost
	vhostRef := instance.Status.VhostRef
	if vhostRef == "" {
		vhostRef = instance.Spec.VhostRef
	}

	// Remove per-user finalizer from vhost if it exists
	// We retry on transient errors, but skip when vhost is already being deleted
	if vhostRef != "" {
		userFinalizer := rabbitmqv1.UserVhostFinalizerPrefix + instance.Spec.Username
		vhost := &rabbitmqv1.RabbitMQVhost{}
		err := r.Get(ctx, types.NamespacedName{Name: vhostRef, Namespace: instance.Namespace}, vhost)

		// Check for context cancellation (e.g., pod shutdown)
		if ctx.Err() != nil {
			return ctrl.Result{}, ctx.Err()
		}

		if err == nil {
			// Vhost exists - try to remove our finalizer (even if vhost is being deleted)
			if controllerutil.RemoveFinalizer(vhost, userFinalizer) {
				if err := r.Update(ctx, vhost); err != nil {
					if k8s_errors.IsNotFound(err) {
						// Vhost was deleted between Get and Update - that's fine
						Log.Info("Vhost was deleted before finalizer could be removed", "vhost", vhostRef)
					} else {
						// Failed to update vhost - retry with exponential backoff
						return ctrl.Result{}, fmt.Errorf("failed to remove finalizer %s from vhost %s: %w", userFinalizer, vhostRef, err)
					}
				} else {
					Log.Info("Successfully removed finalizer from vhost", "vhost", vhostRef, "finalizer", userFinalizer)
				}
			}
		} else if k8s_errors.IsNotFound(err) {
			// Vhost doesn't exist - nothing to clean up
			Log.Info("Vhost not found during user deletion", "vhost", vhostRef)
		} else {
			// Failed to get vhost (other error) - retry
			return ctrl.Result{}, fmt.Errorf("failed to get vhost %s for finalizer removal: %w", vhostRef, err)
		}
	}

	// Get username for deletion
	username := instance.Status.Username
	if username == "" {
		username = instance.Spec.Username
	}

	// Get vhost name - priority order:
	// 1. From status.Vhost (the actual vhost name, stored during normal reconciliation)
	// 2. From the vhost CR if it still exists
	// 3. Default to "/" if VhostRef is empty
	vhostName := "/"
	if instance.Status.Vhost != "" {
		// Use the vhost name from status - this is the most reliable source during deletion
		vhostName = instance.Status.Vhost
	} else if instance.Spec.VhostRef != "" {
		// Try to get vhost CR to determine the vhost name
		vhost := &rabbitmqv1.RabbitMQVhost{}
		vhostErr := r.Get(ctx, types.NamespacedName{Name: instance.Spec.VhostRef, Namespace: instance.Namespace}, vhost)
		if vhostErr != nil && !k8s_errors.IsNotFound(vhostErr) {
			// Log non-NotFound errors but continue with deletion
			Log.Error(vhostErr, "Failed to get vhost", "vhost", instance.Spec.VhostRef)
		} else if vhostErr == nil && vhost.Spec.Name != "" {
			vhostName = vhost.Spec.Name
		}
	}

	// Get RabbitMQ cluster for deletion
	rabbit := &rabbitmqclusterv2.RabbitmqCluster{}
	err := r.Get(ctx, types.NamespacedName{Name: instance.Spec.RabbitmqClusterName, Namespace: instance.Namespace}, rabbit)

	// If cluster is being deleted or not found, skip cleanup and just remove finalizer
	if err != nil && !k8s_errors.IsNotFound(err) {
		// Error getting cluster - return error to retry
		return ctrl.Result{}, err
	}

	if k8s_errors.IsNotFound(err) || !rabbit.DeletionTimestamp.IsZero() {
		// Cluster doesn't exist or is being deleted - skip RabbitMQ cleanup
		Log.Info("RabbitMQ cluster not found or being deleted, skipping RabbitMQ cleanup",
			"cluster", instance.Spec.RabbitmqClusterName,
			"notFound", k8s_errors.IsNotFound(err),
			"beingDeleted", !rabbit.DeletionTimestamp.IsZero(),
			"vhostRef", vhostRef)

		instance.Status.Conditions.MarkTrue(
			rabbitmqv1.RabbitMQUserReadyCondition,
			"RabbitMQ cluster deleted, skipping RabbitMQ cleanup",
		)

		// Vhost finalizer removal was already attempted above (best effort)
		// We don't block user deletion even if it failed - the vhost controller
		// will clean up any orphaned finalizers after 10 minutes.

		controllerutil.RemoveFinalizer(instance, userFinalizer)
		return ctrl.Result{}, nil
	}

	// Cluster exists and is not being deleted - perform cleanup
	// Get admin credentials
	rabbitSecret, _, err := oko_secret.GetSecret(ctx, h, rabbit.Status.DefaultUser.SecretReference.Name, instance.Namespace)
	if err != nil {
		// If cluster exists and is healthy, secret should be available
		// Return error to retry
		return ctrl.Result{}, err
	}

	// Create API client
	baseURL := getManagementURL(rabbit, rabbitSecret)
	tlsEnabled := rabbit.Spec.TLS.SecretName != ""
	caCert, err := getTLSCACert(ctx, h, rabbit, instance.Namespace)
	if err != nil {
		return ctrl.Result{}, err
	}
	apiClient := rabbitmqapi.NewClient(baseURL, string(rabbitSecret.Data["username"]), string(rabbitSecret.Data["password"]), tlsEnabled, caCert)

	// Delete permissions and user from RabbitMQ
	// The Delete methods already treat 404 as success
	if err := apiClient.DeletePermissions(vhostName, username); err != nil {
		// Return error to trigger retry - see rabbitmqpolicy_controller.go for detailed rationale
		return ctrl.Result{}, fmt.Errorf("failed to delete permissions for user %s from vhost %s in RabbitMQ: %w", username, vhostName, err)
	}

	if err := apiClient.DeleteUser(username); err != nil {
		// Return error to trigger retry - see rabbitmqpolicy_controller.go for detailed rationale
		return ctrl.Result{}, fmt.Errorf("failed to delete user %s from RabbitMQ: %w", username, err)
	}

	// Only delete auto-generated secret (when spec.secret is not set)
	// User-provided secrets are NOT deleted
	if instance.Spec.Secret == nil || *instance.Spec.Secret == "" {
		secretName := fmt.Sprintf("rabbitmq-user-%s", instance.Name)
		secret := &corev1.Secret{}

		// Get the secret first to check ownership
		if err := r.Get(ctx, types.NamespacedName{Name: secretName, Namespace: instance.Namespace}, secret); err == nil {
			// Check if we are the owner before deleting
			if object.CheckOwnerRefExist(instance.GetUID(), secret.GetOwnerReferences()) {
				if err := r.Delete(ctx, secret); err != nil && !k8s_errors.IsNotFound(err) {
					log.FromContext(ctx).Error(err, "Failed to delete user secret", "secret", secretName)
				}
			} else {
				log.FromContext(ctx).Info("Skipping secret deletion - not owned by this RabbitMQUser", "secret", secretName)
			}
		} else if !k8s_errors.IsNotFound(err) {
			log.FromContext(ctx).Error(err, "Failed to get secret for ownership check", "secret", secretName)
		}
	}

	controllerutil.RemoveFinalizer(instance, userFinalizer)
	return ctrl.Result{}, nil
}

// vhostToUserMapFunc maps vhost changes to user reconciliation requests
// This allows the user controller to react when a vhost changes, eliminating
// the need to poll when waiting for old vhost permissions to be deleted
func (r *RabbitMQUserReconciler) vhostToUserMapFunc(ctx context.Context, obj client.Object) []reconcile.Request {
	vhost := obj.(*rabbitmqv1.RabbitMQVhost)
	userList := &rabbitmqv1.RabbitMQUserList{}
	if err := r.List(ctx, userList, client.InNamespace(vhost.Namespace)); err != nil {
		log.FromContext(ctx).Error(err, "Failed to list users for vhost watch", "vhost", vhost.Name)
		return []reconcile.Request{}
	}

	requests := []reconcile.Request{}
	for _, user := range userList.Items {
		// Reconcile users that reference this vhost (either current or old)
		if user.Spec.VhostRef == vhost.Name || user.Status.Vhost == vhost.Name {
			requests = append(requests, reconcile.Request{
				NamespacedName: types.NamespacedName{
					Name:      user.Name,
					Namespace: user.Namespace,
				},
			})
		}
	}
	return requests
}

// findRabbitMQUsersForSecret finds all RabbitMQUsers that reference the given secret
// This function uses the field index for efficient lookups
func (r *RabbitMQUserReconciler) findRabbitMQUsersForSecret(ctx context.Context, secret client.Object) []reconcile.Request {
	// Use the field index to find RabbitMQUsers that reference this secret
	userList := &rabbitmqv1.RabbitMQUserList{}
	if err := r.List(ctx, userList,
		client.InNamespace(secret.GetNamespace()),
		client.MatchingFields{credentialSecretNameField: secret.GetName()}); err != nil {
		return []reconcile.Request{}
	}

	requests := []reconcile.Request{}
	for _, user := range userList.Items {
		requests = append(requests, reconcile.Request{
			NamespacedName: types.NamespacedName{
				Name:      user.Name,
				Namespace: user.Namespace,
			},
		})
	}
	return requests
}

// SetupWithManager sets up the controller with the Manager.
func (r *RabbitMQUserReconciler) SetupWithManager(mgr ctrl.Manager) error {
	// Register field index for efficient secret watching
	if err := mgr.GetFieldIndexer().IndexField(context.Background(),
		&rabbitmqv1.RabbitMQUser{}, credentialSecretNameField,
		func(rawObj client.Object) []string {
			user := rawObj.(*rabbitmqv1.RabbitMQUser)
			if user.Spec.Secret == nil || *user.Spec.Secret == "" {
				return nil
			}
			return []string{*user.Spec.Secret}
		}); err != nil {
		return err
	}

	return ctrl.NewControllerManagedBy(mgr).
		For(&rabbitmqv1.RabbitMQUser{}).
		Owns(&corev1.Secret{}).
		Watches(&rabbitmqv1.RabbitMQVhost{},
			handler.EnqueueRequestsFromMapFunc(r.vhostToUserMapFunc)).
		Watches(
			&corev1.Secret{},
			handler.EnqueueRequestsFromMapFunc(r.findRabbitMQUsersForSecret),
		).
		Complete(r)
}
