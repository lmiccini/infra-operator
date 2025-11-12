/*
Copyright 2024.

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
	"fmt"
	"time"

	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/controller/controllerutil"
	"sigs.k8s.io/controller-runtime/pkg/log"

	rabbitmqv1 "github.com/openstack-k8s-operators/infra-operator/apis/rabbitmq/v1beta1"
	rabbitmqapi "github.com/openstack-k8s-operators/infra-operator/pkg/rabbitmq/api"
	condition "github.com/openstack-k8s-operators/lib-common/modules/common/condition"
	helper "github.com/openstack-k8s-operators/lib-common/modules/common/helper"
	oko_secret "github.com/openstack-k8s-operators/lib-common/modules/common/secret"
	rabbitmqclusterv2 "github.com/rabbitmq/cluster-operator/v2/api/v1beta1"
	k8s_errors "k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/types"
	"k8s.io/client-go/kubernetes"
)

const vhostFinalizer = "rabbitmqvhost.openstack.org/finalizer"

// RabbitMQVhostReconciler reconciles a RabbitMQVhost object
type RabbitMQVhostReconciler struct {
	client.Client
	Kclient kubernetes.Interface
	Scheme  *runtime.Scheme
}

//+kubebuilder:rbac:groups=rabbitmq.openstack.org,resources=rabbitmqvhosts,verbs=get;list;watch;create;update;patch;delete
//+kubebuilder:rbac:groups=rabbitmq.openstack.org,resources=rabbitmqvhosts/status,verbs=get;update;patch
//+kubebuilder:rbac:groups=rabbitmq.openstack.org,resources=rabbitmqvhosts/finalizers,verbs=update
//+kubebuilder:rbac:groups=rabbitmq.com,resources=rabbitmqclusters,verbs=get;list;watch
//+kubebuilder:rbac:groups=core,resources=secrets,verbs=get;list;watch

func (r *RabbitMQVhostReconciler) Reconcile(ctx context.Context, req ctrl.Request) (ctrl.Result, error) {
	Log := log.FromContext(ctx)

	instance := &rabbitmqv1.RabbitMQVhost{}
	err := r.Get(ctx, req.NamespacedName, instance)
	if err != nil {
		if k8s_errors.IsNotFound(err) {
			return ctrl.Result{}, nil
		}
		return ctrl.Result{}, err
	}

	h, _ := helper.NewHelper(instance, r.Client, r.Kclient, r.Scheme, Log)

	// Initialize status
	if instance.Status.Conditions == nil {
		instance.Status.Conditions = condition.Conditions{}
		cl := condition.CreateList(
			condition.UnknownCondition(condition.ReadyCondition, condition.InitReason, condition.ReadyInitMessage),
			condition.UnknownCondition(rabbitmqv1.VhostReadyCondition, condition.InitReason, rabbitmqv1.VhostReadyInitMessage),
		)
		instance.Status.Conditions.Init(&cl)
		instance.Status.ObservedGeneration = instance.Generation
	}

	defer func() {
		if instance.Status.Conditions.IsUnknown(condition.ReadyCondition) {
			instance.Status.Conditions.Set(instance.Status.Conditions.Mirror(condition.ReadyCondition))
		}
		h.PatchInstance(ctx, instance)
	}()

	// Handle deletion
	if !instance.DeletionTimestamp.IsZero() {
		return r.reconcileDelete(ctx, instance, h)
	}

	// Add finalizer
	if !controllerutil.ContainsFinalizer(instance, vhostFinalizer) {
		controllerutil.AddFinalizer(instance, vhostFinalizer)
		return ctrl.Result{Requeue: true}, nil
	}

	return r.reconcileNormal(ctx, instance, h)
}

func (r *RabbitMQVhostReconciler) reconcileNormal(ctx context.Context, instance *rabbitmqv1.RabbitMQVhost, h *helper.Helper) (ctrl.Result, error) {
	// Get RabbitMQ cluster
	rabbit := &rabbitmqclusterv2.RabbitmqCluster{}
	err := r.Get(ctx, types.NamespacedName{Name: instance.Spec.RabbitmqClusterName, Namespace: instance.Namespace}, rabbit)
	if err != nil {
		instance.Status.Conditions.Set(condition.FalseCondition(rabbitmqv1.VhostReadyCondition, condition.ErrorReason, condition.SeverityWarning, rabbitmqv1.VhostReadyErrorMessage, err.Error()))
		return ctrl.Result{}, err
	}

	// Get admin credentials
	rabbitSecret, _, err := oko_secret.GetSecret(ctx, h, rabbit.Status.DefaultUser.SecretReference.Name, instance.Namespace)
	if err != nil {
		instance.Status.Conditions.Set(condition.FalseCondition(rabbitmqv1.VhostReadyCondition, condition.ErrorReason, condition.SeverityWarning, rabbitmqv1.VhostReadyErrorMessage, err.Error()))
		return ctrl.Result{}, err
	}

	// Create API client
	tlsEnabled := rabbit.Spec.TLS.SecretName != ""
	protocol := "http"
	managementPort := "15672"
	if tlsEnabled {
		protocol = "https"
		managementPort = "15671"
	}
	baseURL := fmt.Sprintf("%s://%s:%s", protocol, string(rabbitSecret.Data["host"]), managementPort)
	apiClient := rabbitmqapi.NewClient(baseURL, string(rabbitSecret.Data["username"]), string(rabbitSecret.Data["password"]), tlsEnabled)

	// Create vhost
	vhostName := instance.Spec.Name
	if vhostName == "" {
		vhostName = "/"
	}

	if vhostName != "/" {
		err = apiClient.CreateOrUpdateVhost(vhostName)
		if err != nil {
			instance.Status.Conditions.Set(condition.FalseCondition(rabbitmqv1.VhostReadyCondition, condition.ErrorReason, condition.SeverityWarning, rabbitmqv1.VhostReadyErrorMessage, err.Error()))
			return ctrl.Result{}, err
		}
	}

	instance.Status.Conditions.MarkTrue(rabbitmqv1.VhostReadyCondition, rabbitmqv1.VhostReadyMessage)
	instance.Status.Conditions.MarkTrue(condition.ReadyCondition, condition.ReadyMessage)

	return ctrl.Result{}, nil
}

func (r *RabbitMQVhostReconciler) reconcileDelete(ctx context.Context, instance *rabbitmqv1.RabbitMQVhost, h *helper.Helper) (ctrl.Result, error) {
	// Block deletion if owned by TransportURL and has protection finalizer
	if controllerutil.ContainsFinalizer(instance, rabbitmqv1.VhostFinalizer) {
		for _, owner := range instance.OwnerReferences {
			if owner.Controller != nil && *owner.Controller && owner.Kind == "TransportURL" {
				// Check if TransportURL still references this vhost
				transportURL := &rabbitmqv1.TransportURL{}
				if err := r.Get(ctx, types.NamespacedName{Name: owner.Name, Namespace: instance.Namespace}, transportURL); err == nil {
					vhostName := transportURL.Spec.Vhost
					if vhostName == "" {
						vhostName = "/"
					}
					expectedVhostRef := ""
					if vhostName != "/" {
						expectedVhostRef = fmt.Sprintf("%s-%s-vhost", transportURL.Name, vhostName)
					}
					if expectedVhostRef == instance.Name {
						// Still in use - block deletion
						return ctrl.Result{RequeueAfter: time.Duration(10) * time.Second}, nil
					}
				}
			}
		}
		controllerutil.RemoveFinalizer(instance, rabbitmqv1.VhostFinalizer)
	}

	// Get RabbitMQ cluster
	rabbit := &rabbitmqclusterv2.RabbitmqCluster{}
	err := r.Get(ctx, types.NamespacedName{Name: instance.Spec.RabbitmqClusterName, Namespace: instance.Namespace}, rabbit)
	if err != nil && !k8s_errors.IsNotFound(err) {
		return ctrl.Result{}, err
	}

	if err == nil {
		// Get admin credentials
		rabbitSecret, _, err := oko_secret.GetSecret(ctx, h, rabbit.Status.DefaultUser.SecretReference.Name, instance.Namespace)
		if err == nil {
			// Create API client
			tlsEnabled := rabbit.Spec.TLS.SecretName != ""
			protocol := "http"
			managementPort := "15672"
			if tlsEnabled {
				protocol = "https"
				managementPort = "15671"
			}
			baseURL := fmt.Sprintf("%s://%s:%s", protocol, string(rabbitSecret.Data["host"]), managementPort)
			apiClient := rabbitmqapi.NewClient(baseURL, string(rabbitSecret.Data["username"]), string(rabbitSecret.Data["password"]), tlsEnabled)

			// Delete vhost (skip default)
			vhostName := instance.Spec.Name
			if vhostName == "" {
				vhostName = "/"
			}
			if vhostName != "/" {
				apiClient.DeleteVhost(vhostName)
			}
		}
	}

	controllerutil.RemoveFinalizer(instance, vhostFinalizer)
	return ctrl.Result{}, nil
}

func (r *RabbitMQVhostReconciler) SetupWithManager(mgr ctrl.Manager) error {
	return ctrl.NewControllerManagedBy(mgr).
		For(&rabbitmqv1.RabbitMQVhost{}).
		Complete(r)
}
