/*
Copyright 2022.

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
	"time"

	"github.com/go-logr/logr"
	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/fields"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/builder"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/handler"
	"sigs.k8s.io/controller-runtime/pkg/log"
	"sigs.k8s.io/controller-runtime/pkg/predicate"
	"sigs.k8s.io/controller-runtime/pkg/reconcile"

	rabbitmqv1 "github.com/openstack-k8s-operators/infra-operator/apis/rabbitmq/v1beta1"
	condition "github.com/openstack-k8s-operators/lib-common/modules/common/condition"
	helper "github.com/openstack-k8s-operators/lib-common/modules/common/helper"
	oko_secret "github.com/openstack-k8s-operators/lib-common/modules/common/secret"
	rabbitmqclusterv2 "github.com/rabbitmq/cluster-operator/v2/api/v1beta1"
	k8s_errors "k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/types"
	"k8s.io/client-go/kubernetes"
	"sigs.k8s.io/controller-runtime/pkg/controller/controllerutil"
)


// GetClient -
func (r *TransportURLReconciler) GetClient() client.Client {
	return r.Client
}

// GetKClient -
func (r *TransportURLReconciler) GetKClient() kubernetes.Interface {
	return r.Kclient
}

// GetScheme -
func (r *TransportURLReconciler) GetScheme() *runtime.Scheme {
	return r.Scheme
}

// TransportURLReconciler reconciles a TransportURL object
type TransportURLReconciler struct {
	client.Client
	Kclient kubernetes.Interface
	Scheme  *runtime.Scheme
}

// GetLogger returns a logger object with a prefix of "controller.name" and additional controller context fields
func (r *TransportURLReconciler) GetLogger(ctx context.Context) logr.Logger {
	return log.FromContext(ctx).WithName("Controllers").WithName("TransportURL")
}

//+kubebuilder:rbac:groups=rabbitmq.openstack.org,resources=transporturls,verbs=get;list;watch;create;update;patch;delete
//+kubebuilder:rbac:groups=rabbitmq.openstack.org,resources=transporturls/status,verbs=get;update;patch
//+kubebuilder:rbac:groups=rabbitmq.openstack.org,resources=transporturls/finalizers,verbs=update
//+kubebuilder:rbac:groups=rabbitmq.openstack.org,resources=rabbitmqs,verbs=get;list;watch
//+kubebuilder:rbac:groups=rabbitmq.openstack.org,resources=rabbitmqs,verbs=get;list;watch
//+kubebuilder:rbac:groups=rabbitmq.com,resources=rabbitmqclusters,verbs=get;list;watch
//+kubebuilder:rbac:groups=core,resources=secrets,verbs=get;list;watch;create;update;patch;delete;

// Reconcile - https://pkg.go.dev/sigs.k8s.io/controller-runtime@v0.12.2/pkg/reconcile
func (r *TransportURLReconciler) Reconcile(ctx context.Context, req ctrl.Request) (result ctrl.Result, _err error) {
	Log := r.GetLogger(ctx)
	// Fetch the TransportURL instance
	instance := &rabbitmqv1.TransportURL{}
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
	cl := condition.CreateList(
		condition.UnknownCondition(condition.ReadyCondition, condition.InitReason, condition.ReadyInitMessage),
		condition.UnknownCondition(rabbitmqv1.TransportURLReadyCondition, condition.InitReason, rabbitmqv1.TransportURLReadyInitMessage),
	)

	instance.Status.Conditions.Init(&cl)
	instance.Status.ObservedGeneration = instance.Generation

	if isNewInstance {
		// Return to register overall status immediately to have an early feedback e.g. in the cli
		return ctrl.Result{}, nil
	}

	return r.reconcileNormal(ctx, instance, helper)
}

// generatePassword generates a random password
func generatePassword(length int) (string, error) {
	bytes := make([]byte, length)
	_, err := rand.Read(bytes)
	if err != nil {
		return "", err
	}
	return base64.URLEncoding.EncodeToString(bytes)[:length], nil
}

// getUsername returns the username to use, either from spec or generated from instance name
func getUsername(instance *rabbitmqv1.TransportURL) string {
	if instance.Spec.RabbitmqUsername != "" {
		return instance.Spec.RabbitmqUsername
	}
	// Use the same prefix pattern as currently done (nova-api-, nova-cell1-, etc.)
	return instance.Name
}

// getVhost returns the vhost to use, either from spec or default
func getVhost(instance *rabbitmqv1.TransportURL) string {
	if instance.Spec.RabbitmqVhost != "" {
		return instance.Spec.RabbitmqVhost
	}
	return "/" // default vhost
}

// createRabbitMQUser creates a RabbitMQ user using messaging-topology-operator
func (r *TransportURLReconciler) createRabbitMQUser(ctx context.Context, instance *rabbitmqv1.TransportURL, username, password string) error {
	user := &unstructured.Unstructured{
		Object: map[string]interface{}{
			"apiVersion": "rabbitmq.com/v1beta1",
			"kind":       "User",
			"metadata": map[string]interface{}{
				"name":      username,
				"namespace": instance.Namespace,
			},
			"spec": map[string]interface{}{
				"rabbitmqClusterReference": map[string]interface{}{
					"name": instance.Spec.RabbitmqClusterName,
				},
				"tags": []string{},
				"importCredentialsSecret": map[string]interface{}{
					"name": fmt.Sprintf("rabbitmq-user-%s", username),
				},
			},
		},
	}

	// Create the user credentials secret
	secret := &corev1.Secret{
		ObjectMeta: metav1.ObjectMeta{
			Name:      fmt.Sprintf("rabbitmq-user-%s", username),
			Namespace: instance.Namespace,
		},
		Data: map[string][]byte{
			"username": []byte(username),
			"password": []byte(password),
		},
	}

	// Create or update the secret
	_, err := controllerutil.CreateOrUpdate(ctx, r.Client, secret, func() error {
		secret.Data["username"] = []byte(username)
		secret.Data["password"] = []byte(password)
		return controllerutil.SetControllerReference(instance, secret, r.Scheme)
	})
	if err != nil {
		return fmt.Errorf("failed to create user secret: %w", err)
	}

	// Create or update the user
	_, err = controllerutil.CreateOrUpdate(ctx, r.Client, user, func() error {
		// Update the spec for unstructured object
		spec := user.Object["spec"].(map[string]interface{})
		spec["rabbitmqClusterReference"] = map[string]interface{}{
			"name": instance.Spec.RabbitmqClusterName,
		}
		spec["tags"] = []string{}
		spec["importCredentialsSecret"] = map[string]interface{}{
			"name": fmt.Sprintf("rabbitmq-user-%s", username),
		}
		return controllerutil.SetControllerReference(instance, user, r.Scheme)
	})
	if err != nil {
		return fmt.Errorf("failed to create RabbitMQ user: %w", err)
	}

	return nil
}

// createRabbitMQVhost creates a RabbitMQ vhost using messaging-topology-operator
func (r *TransportURLReconciler) createRabbitMQVhost(ctx context.Context, instance *rabbitmqv1.TransportURL, vhostName string) error {
	if vhostName == "/" {
		// Default vhost already exists, no need to create
		return nil
	}

	vhost := &unstructured.Unstructured{
		Object: map[string]interface{}{
			"apiVersion": "rabbitmq.com/v1beta1",
			"kind":       "Vhost",
			"metadata": map[string]interface{}{
				"name":      vhostName,
				"namespace": instance.Namespace,
			},
			"spec": map[string]interface{}{
				"name": vhostName,
				"rabbitmqClusterReference": map[string]interface{}{
					"name": instance.Spec.RabbitmqClusterName,
				},
			},
		},
	}

	_, err := controllerutil.CreateOrUpdate(ctx, r.Client, vhost, func() error {
		// Update the spec for unstructured object
		spec := vhost.Object["spec"].(map[string]interface{})
		spec["name"] = vhostName
		spec["rabbitmqClusterReference"] = map[string]interface{}{
			"name": instance.Spec.RabbitmqClusterName,
		}
		return controllerutil.SetControllerReference(instance, vhost, r.Scheme)
	})
	if err != nil {
		return fmt.Errorf("failed to create RabbitMQ vhost: %w", err)
	}

	return nil
}

// createRabbitMQPermission creates RabbitMQ permissions for a user on a vhost
func (r *TransportURLReconciler) createRabbitMQPermission(ctx context.Context, instance *rabbitmqv1.TransportURL, username, vhostName string) error {
	permissionName := fmt.Sprintf("%s-%s-permission", username, vhostName)
	permission := &unstructured.Unstructured{
		Object: map[string]interface{}{
			"apiVersion": "rabbitmq.com/v1beta1",
			"kind":       "Permission",
			"metadata": map[string]interface{}{
				"name":      permissionName,
				"namespace": instance.Namespace,
			},
			"spec": map[string]interface{}{
				"vhost": vhostName,
				"user":  username,
				"permissions": map[string]interface{}{
					"configure": ".*",
					"write":     ".*",
					"read":      ".*",
				},
				"rabbitmqClusterReference": map[string]interface{}{
					"name": instance.Spec.RabbitmqClusterName,
				},
			},
		},
	}

	_, err := controllerutil.CreateOrUpdate(ctx, r.Client, permission, func() error {
		// Update the spec for unstructured object
		spec := permission.Object["spec"].(map[string]interface{})
		spec["vhost"] = vhostName
		spec["user"] = username
		spec["permissions"] = map[string]interface{}{
			"configure": ".*",
			"write":     ".*",
			"read":      ".*",
		}
		spec["rabbitmqClusterReference"] = map[string]interface{}{
			"name": instance.Spec.RabbitmqClusterName,
		}
		return controllerutil.SetControllerReference(instance, permission, r.Scheme)
	})
	if err != nil {
		return fmt.Errorf("failed to create RabbitMQ permission: %w", err)
	}

	return nil
}

// cleanupOldUser removes the old RabbitMQ user and its associated resources
// WARNING: This is a manual operation triggered by annotation. Only use when
// you are certain no services are still using the old user credentials.
func (r *TransportURLReconciler) cleanupOldUser(ctx context.Context, instance *rabbitmqv1.TransportURL, oldUsername string) error {
	Log := r.GetLogger(ctx)

	if oldUsername == "" {
		return nil
	}

	Log.Info(fmt.Sprintf("Cleaning up old user: %s", oldUsername))

	// Delete the old user
	oldUser := &unstructured.Unstructured{
		Object: map[string]interface{}{
			"apiVersion": "rabbitmq.com/v1beta1",
			"kind":       "User",
			"metadata": map[string]interface{}{
				"name":      oldUsername,
				"namespace": instance.Namespace,
			},
		},
	}
	err := r.Client.Delete(ctx, oldUser)
	if err != nil && !k8s_errors.IsNotFound(err) {
		Log.Error(err, fmt.Sprintf("Failed to delete old user %s", oldUsername))
		return err
	}

	// Delete the old user secret
	oldSecretName := fmt.Sprintf("rabbitmq-user-%s", oldUsername)
	oldSecret := &corev1.Secret{
		ObjectMeta: metav1.ObjectMeta{
			Name:      oldSecretName,
			Namespace: instance.Namespace,
		},
	}
	err = r.Client.Delete(ctx, oldSecret)
	if err != nil && !k8s_errors.IsNotFound(err) {
		Log.Error(err, fmt.Sprintf("Failed to delete old user secret %s", oldSecretName))
		return err
	}

	// Delete old permissions for the old user
	oldPermissionName := fmt.Sprintf("%s-%s-permission", oldUsername, instance.Status.RabbitmqVhost)
	oldPermission := &unstructured.Unstructured{
		Object: map[string]interface{}{
			"apiVersion": "rabbitmq.com/v1beta1",
			"kind":       "Permission",
			"metadata": map[string]interface{}{
				"name":      oldPermissionName,
				"namespace": instance.Namespace,
			},
		},
	}
	err = r.Client.Delete(ctx, oldPermission)
	if err != nil && !k8s_errors.IsNotFound(err) {
		Log.Error(err, fmt.Sprintf("Failed to delete old permission %s", oldPermissionName))
		return err
	}

	Log.Info(fmt.Sprintf("Successfully cleaned up old user: %s", oldUsername))
	return nil
}

func (r *TransportURLReconciler) reconcileNormal(ctx context.Context, instance *rabbitmqv1.TransportURL, helper *helper.Helper) (ctrl.Result, error) {
	Log := r.GetLogger(ctx)
	Log.Info("Reconciling Service")

	// TODO (implement a watch on the rabbitmq cluster resources to update things if there are changes)
	rabbit, err := getRabbitmqCluster(ctx, helper, instance)
	if err != nil {
		return ctrl.Result{}, err
	}

	// Wait on RabbitmqCluster to be ready
	rabbitReady := false
	for _, condition := range rabbit.Status.Conditions {
		if condition.Reason == "AllPodsAreReady" && condition.Status == "True" {
			rabbitReady = true
			break
		}
	}
	if !rabbitReady {
		instance.Status.Conditions.Set(condition.FalseCondition(
			rabbitmqv1.TransportURLReadyCondition,
			condition.RequestedReason,
			condition.SeverityInfo,
			rabbitmqv1.TransportURLInProgressMessage))
		return ctrl.Result{RequeueAfter: time.Duration(10) * time.Second}, nil
	}

	// Get username and vhost for this transport URL
	dedicatedUsername := getUsername(instance)
	vhostName := getVhost(instance)

	// Check if we should use dedicated user or default RabbitMQ user
	useDedicatedUser := instance.Spec.RabbitmqUsername != "" || instance.Spec.RabbitmqVhost != ""

	var username, password string
	if useDedicatedUser {
		username = dedicatedUsername
		// Check if username has changed (for gradual migration)
		// When username changes, we create a new user but keep the old one active
		// to prevent breaking existing services. Old user cleanup is manual only.
		if instance.Status.RabbitmqUsername != "" && instance.Status.RabbitmqUsername != username {
			Log.Info(fmt.Sprintf("Username changed from %s to %s, creating new user while keeping old one", instance.Status.RabbitmqUsername, username))
			// Store the previous username for potential manual cleanup later
			instance.Status.PreviousRabbitmqUsername = instance.Status.RabbitmqUsername
		}

		// Check if we need to create a new user or if credentials have changed
		userSecretName := fmt.Sprintf("rabbitmq-user-%s", username)
		userSecret, _, err := oko_secret.GetSecret(ctx, helper, userSecretName, instance.Namespace)
		needNewPassword := false

		if err != nil {
			if k8s_errors.IsNotFound(err) {
				// User doesn't exist, we need to create it
				needNewPassword = true
			} else {
				instance.Status.Conditions.Set(condition.FalseCondition(
					rabbitmqv1.TransportURLReadyCondition,
					condition.ErrorReason,
					condition.SeverityWarning,
					rabbitmqv1.TransportURLReadyErrorMessage,
					err.Error()))
				return ctrl.Result{}, err
			}
		}

		// Generate password if needed
		var password string
		if needNewPassword {
			password, err = generatePassword(32)
			if err != nil {
				instance.Status.Conditions.Set(condition.FalseCondition(
					rabbitmqv1.TransportURLReadyCondition,
					condition.ErrorReason,
					condition.SeverityWarning,
					rabbitmqv1.TransportURLReadyErrorMessage,
					err.Error()))
				return ctrl.Result{}, err
			}
		} else {
			password = string(userSecret.Data["password"])
		}

		// Create RabbitMQ user
		err = r.createRabbitMQUser(ctx, instance, username, password)
		if err != nil {
			instance.Status.Conditions.Set(condition.FalseCondition(
				rabbitmqv1.TransportURLReadyCondition,
				condition.ErrorReason,
				condition.SeverityWarning,
				rabbitmqv1.TransportURLReadyErrorMessage,
				err.Error()))
			return ctrl.Result{}, err
		}

		// Create RabbitMQ vhost if needed
		err = r.createRabbitMQVhost(ctx, instance, vhostName)
		if err != nil {
			instance.Status.Conditions.Set(condition.FalseCondition(
				rabbitmqv1.TransportURLReadyCondition,
				condition.ErrorReason,
				condition.SeverityWarning,
				rabbitmqv1.TransportURLReadyErrorMessage,
				err.Error()))
			return ctrl.Result{}, err
		}

		// Create RabbitMQ permissions
		err = r.createRabbitMQPermission(ctx, instance, username, vhostName)
		if err != nil {
			instance.Status.Conditions.Set(condition.FalseCondition(
				rabbitmqv1.TransportURLReadyCondition,
				condition.ErrorReason,
				condition.SeverityWarning,
				rabbitmqv1.TransportURLReadyErrorMessage,
				err.Error()))
			return ctrl.Result{}, err
		}
	} else {
		// Use default RabbitMQ user behavior (backward compatibility)
		Log.Info("Using default RabbitMQ user behavior (no dedicated user specified)")
		username = ""   // Will be set from default user secret below
		vhostName = "/" // Default vhost
	}

	// Get connection details from the default RabbitMQ secret
	rabbitSecret, _, err := oko_secret.GetSecret(ctx, helper, rabbit.Status.DefaultUser.SecretReference.Name, instance.Namespace)
	if err != nil {
		if k8s_errors.IsNotFound(err) {
			// Since the RabbitMQ secret should have been automatically created by the RabbitMQ cluster,
			// we treat this as a warning because it means that the service will not be able to start.
			instance.Status.Conditions.Set(condition.FalseCondition(
				rabbitmqv1.TransportURLReadyCondition,
				condition.ErrorReason,
				condition.SeverityWarning,
				rabbitmqv1.TransportURLInProgressMessage))
			return ctrl.Result{RequeueAfter: time.Duration(10) * time.Second}, nil
		}
		instance.Status.Conditions.Set(condition.FalseCondition(
			rabbitmqv1.TransportURLReadyCondition,
			condition.ErrorReason,
			condition.SeverityWarning,
			rabbitmqv1.TransportURLReadyErrorMessage,
			err.Error()))
		return ctrl.Result{}, err
	}

	var host string
	if h, ok := rabbitSecret.Data["host"]; ok {
		host = string(h)
	} else {
		err := fmt.Errorf("host does not exist in rabbitmq secret %s", rabbitSecret.Name)
		instance.Status.Conditions.Set(condition.FalseCondition(
			rabbitmqv1.TransportURLReadyCondition,
			condition.ErrorReason,
			condition.SeverityWarning,
			rabbitmqv1.TransportURLReadyErrorMessage,
			err.Error()))
		return ctrl.Result{}, err
	}

	var port string
	if p, ok := rabbitSecret.Data["port"]; ok {
		port = string(p)
	} else {
		err := fmt.Errorf("port does not exist in rabbitmq secret %s", rabbitSecret.Name)
		instance.Status.Conditions.Set(condition.FalseCondition(
			rabbitmqv1.TransportURLReadyCondition,
			condition.ErrorReason,
			condition.SeverityWarning,
			rabbitmqv1.TransportURLReadyErrorMessage,
			err.Error()))
		return ctrl.Result{}, err
	}

	// Set username and password based on whether we're using dedicated user or default
	var finalUsername, finalPassword string
	if useDedicatedUser {
		finalUsername = username
		finalPassword = password
	} else {
		// Use default RabbitMQ user credentials
		if u, ok := rabbitSecret.Data["username"]; ok {
			finalUsername = string(u)
		} else {
			err := fmt.Errorf("username does not exist in rabbitmq secret %s", rabbitSecret.Name)
			instance.Status.Conditions.Set(condition.FalseCondition(
				rabbitmqv1.TransportURLReadyCondition,
				condition.ErrorReason,
				condition.SeverityWarning,
				rabbitmqv1.TransportURLReadyErrorMessage,
				err.Error()))
			return ctrl.Result{}, err
		}

		if p, ok := rabbitSecret.Data["password"]; ok {
			finalPassword = string(p)
		} else {
			err := fmt.Errorf("password does not exist in rabbitmq secret %s", rabbitSecret.Name)
			instance.Status.Conditions.Set(condition.FalseCondition(
				rabbitmqv1.TransportURLReadyCondition,
				condition.ErrorReason,
				condition.SeverityWarning,
				rabbitmqv1.TransportURLReadyErrorMessage,
				err.Error()))
			return ctrl.Result{}, err
		}
	}

	tlsEnabled := rabbit.Spec.TLS.SecretName != ""

	Log.Info(fmt.Sprintf("rabbitmq cluster %s has TLS enabled: %t", rabbit.Name, tlsEnabled))

	// Get RabbitMq CR for both secret generation and status update
	rabbitmqCR := &rabbitmqv1.RabbitMq{}
	err = r.Get(ctx, types.NamespacedName{Name: instance.Spec.RabbitmqClusterName, Namespace: instance.Namespace}, rabbitmqCR)

	// Determine quorum setting for secret generation
	quorum := false
	if err != nil {
		Log.Info(fmt.Sprintf("Could not fetch RabbitMQ CR: %v", err))
		// Default to false for quorum if we can't fetch the CR
	} else {
		Log.Info(fmt.Sprintf("Found RabbitMQ CR: %s", rabbitmqCR.Name))

		quorum = rabbitmqCR.Status.QueueType == "Quorum"
		Log.Info(fmt.Sprintf("Setting quorum to: %t based on status QueueType", quorum))

		// Update QueueType and add annotation to signal change
		if rabbitmqCR.Status.QueueType != instance.Status.QueueType {
			Log.Info(fmt.Sprintf("Updating transportURL Status.QueueType from %s to %s", instance.Status.QueueType, rabbitmqCR.Status.QueueType))
			instance.Status.QueueType = rabbitmqCR.Status.QueueType

			// Signal change to dependent controllers via annotation
			if instance.Annotations == nil {
				instance.Annotations = make(map[string]string)
			}
			instance.Annotations["rabbitmq.openstack.org/queuetype-hash"] = fmt.Sprintf("%s-%d", rabbitmqCR.Status.QueueType, time.Now().Unix())
		}
	}

	// Create a new secret with the transport URL for this CR
	secret := r.createTransportURLSecret(instance, finalUsername, finalPassword, host, port, vhostName, tlsEnabled, quorum)
	_, op, err := oko_secret.CreateOrPatchSecret(ctx, helper, instance, secret)
	if err != nil {
		instance.Status.Conditions.Set(condition.FalseCondition(
			rabbitmqv1.TransportURLReadyCondition,
			condition.ErrorReason,
			condition.SeverityWarning,
			rabbitmqv1.TransportURLReadyErrorMessage,
			err.Error()))
		return ctrl.Result{}, err
	}
	if op != controllerutil.OperationResultNone {
		instance.Status.Conditions.Set(condition.FalseCondition(
			rabbitmqv1.TransportURLReadyCondition,
			condition.RequestedReason,
			condition.SeverityInfo,
			rabbitmqv1.TransportURLReadyInitMessage))
		return ctrl.Result{RequeueAfter: time.Second * 5}, nil
	}

	// Update the CR status with actual values used
	instance.Status.SecretName = secret.Name
	instance.Status.RabbitmqUsername = finalUsername
	instance.Status.RabbitmqVhost = vhostName

	// Handle cleanup of old users ONLY if explicitly requested via annotation
	// This is a manual operation to prevent accidentally breaking services
	if instance.Annotations != nil && instance.Annotations["rabbitmq.openstack.org/cleanup-old-user"] == "true" {
		if instance.Status.PreviousRabbitmqUsername != "" {
			Log.Info(fmt.Sprintf("Manual cleanup requested for old user: %s", instance.Status.PreviousRabbitmqUsername))
			err = r.cleanupOldUser(ctx, instance, instance.Status.PreviousRabbitmqUsername)
			if err != nil {
				Log.Error(err, "Failed to cleanup old user, will retry on next reconciliation")
				// Don't fail the reconciliation, just log the error and retry later
			} else {
				Log.Info(fmt.Sprintf("Successfully cleaned up old user: %s", instance.Status.PreviousRabbitmqUsername))
				// Remove the cleanup annotation and clear previous username
				delete(instance.Annotations, "rabbitmq.openstack.org/cleanup-old-user")
				instance.Status.PreviousRabbitmqUsername = ""
			}
		} else {
			// No previous username to clean up, remove the annotation
			delete(instance.Annotations, "rabbitmq.openstack.org/cleanup-old-user")
		}
	}

	instance.Status.Conditions.MarkTrue(rabbitmqv1.TransportURLReadyCondition, rabbitmqv1.TransportURLReadyMessage)

	// We reached the end of the Reconcile, update the Ready condition based on
	// the sub conditions
	if instance.Status.Conditions.AllSubConditionIsTrue() {
		instance.Status.Conditions.MarkTrue(
			condition.ReadyCondition, condition.ReadyMessage)
	}
	Log.Info("Reconciled Service successfully")
	return ctrl.Result{}, nil
}

// Create k8s secret with transport URL
func (r *TransportURLReconciler) createTransportURLSecret(
	instance *rabbitmqv1.TransportURL,
	username string,
	password string,
	host string,
	port string,
	vhost string,
	tlsEnabled bool,
	quorum bool,
) *corev1.Secret {
	query := ""
	if tlsEnabled {
		query += "?ssl=1"
	} else {
		query += "?ssl=0"
	}

	// Create a new secret with the transport URL for this CR
	// Include vhost in the transport URL path
	data := map[string][]byte{
		"transport_url": fmt.Appendf(nil, "rabbit://%s:%s@%s:%s%s%s", username, password, host, port, vhost, query),
	}
	if quorum {
		data["quorumqueues"] = []byte("true")
	}

	return &corev1.Secret{
		ObjectMeta: metav1.ObjectMeta{
			Name:      "rabbitmq-transport-url-" + instance.Name,
			Namespace: instance.Namespace,
		},
		Data: data,
	}
}

// fields to index to reconcile when change
const (
	rabbitmqClusterNameField = ".spec.rabbitmqClusterName"
)

var allWatchFields = []string{
	rabbitmqClusterNameField,
}

// SetupWithManager sets up the controller with the Manager.
func (r *TransportURLReconciler) SetupWithManager(mgr ctrl.Manager) error {
	// index caSecretName
	if err := mgr.GetFieldIndexer().IndexField(context.Background(), &rabbitmqv1.TransportURL{}, rabbitmqClusterNameField, func(rawObj client.Object) []string {
		// Extract the secret name from the spec, if one is provided
		cr := rawObj.(*rabbitmqv1.TransportURL)
		if cr.Spec.RabbitmqClusterName == "" {
			return nil
		}
		return []string{cr.Spec.RabbitmqClusterName}
	}); err != nil {
		return err
	}

	return ctrl.NewControllerManagedBy(mgr).
		For(&rabbitmqv1.TransportURL{}).
		Owns(&corev1.Secret{}).
		Watches(
			&rabbitmqclusterv2.RabbitmqCluster{},
			handler.EnqueueRequestsFromMapFunc(r.findObjectsForSrc),
			builder.WithPredicates(predicate.ResourceVersionChangedPredicate{}),
		).
		Watches(
			&rabbitmqv1.RabbitMq{},
			handler.EnqueueRequestsFromMapFunc(r.findObjectsForSrc),
			builder.WithPredicates(predicate.ResourceVersionChangedPredicate{}),
		).
		Complete(r)
}

func (r *TransportURLReconciler) findObjectsForSrc(ctx context.Context, src client.Object) []reconcile.Request {
	requests := []reconcile.Request{}

	Log := r.GetLogger(ctx)

	for _, field := range allWatchFields {
		crList := &rabbitmqv1.TransportURLList{}
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

// GetRabbitmqCluster - get RabbitmqCluster object in namespace
func getRabbitmqCluster(
	ctx context.Context,
	h *helper.Helper,
	instance *rabbitmqv1.TransportURL,
) (*rabbitmqclusterv2.RabbitmqCluster, error) {
	rabbitMqCluster := &rabbitmqclusterv2.RabbitmqCluster{}

	err := h.GetClient().Get(ctx, types.NamespacedName{Name: instance.Spec.RabbitmqClusterName, Namespace: instance.Namespace}, rabbitMqCluster)

	return rabbitMqCluster, err
}
