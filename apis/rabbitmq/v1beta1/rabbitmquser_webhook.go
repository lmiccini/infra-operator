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

package v1beta1

import (
	"context"
	"fmt"

	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/runtime/schema"
	"k8s.io/apimachinery/pkg/util/validation/field"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	logf "sigs.k8s.io/controller-runtime/pkg/log"
	"sigs.k8s.io/controller-runtime/pkg/webhook"
	"sigs.k8s.io/controller-runtime/pkg/webhook/admission"
)

var rabbitmquserlog = logf.Log.WithName("rabbitmquser-resource")

var webhookClient client.Client

// SetupWebhookWithManager sets up the webhook with the Manager
func (r *RabbitMQUser) SetupWebhookWithManager(mgr ctrl.Manager) error {
	webhookClient = mgr.GetClient()
	return ctrl.NewWebhookManagedBy(mgr).
		For(r).
		Complete()
}

//+kubebuilder:webhook:path=/validate-rabbitmq-openstack-org-v1beta1-rabbitmquser,mutating=false,failurePolicy=fail,sideEffects=None,groups=rabbitmq.openstack.org,resources=rabbitmqusers,verbs=create;update,versions=v1beta1,name=vrabbitmquser.kb.io,admissionReviewVersions=v1

var _ webhook.Validator = &RabbitMQUser{}

// ValidateCreate implements webhook.Validator
func (r *RabbitMQUser) ValidateCreate() (admission.Warnings, error) {
	rabbitmquserlog.Info("validate create", "name", r.Name)
	return nil, r.validateUniqueUsername()
}

// ValidateUpdate implements webhook.Validator
func (r *RabbitMQUser) ValidateUpdate(old runtime.Object) (admission.Warnings, error) {
	rabbitmquserlog.Info("validate update", "name", r.Name)
	return nil, r.validateUniqueUsername()
}

// ValidateDelete implements webhook.Validator
func (r *RabbitMQUser) ValidateDelete() (admission.Warnings, error) {
	return nil, nil
}

// validateUniqueUsername checks that no other RabbitMQUser exists with the same username, vhost, and cluster
func (r *RabbitMQUser) validateUniqueUsername() error {
	// Determine the username that will be used
	username := r.Spec.Username
	if username == "" {
		username = r.Name
	}

	// List all RabbitMQUsers in the same namespace
	userList := &RabbitMQUserList{}
	if err := webhookClient.List(context.TODO(), userList, client.InNamespace(r.Namespace)); err != nil {
		return apierrors.NewInternalError(fmt.Errorf("failed to list RabbitMQUsers: %w", err))
	}

	// Check for conflicts
	for _, user := range userList.Items {
		// Skip self
		if user.Name == r.Name {
			continue
		}

		// Check if same RabbitMQ cluster
		if user.Spec.RabbitmqClusterName != r.Spec.RabbitmqClusterName {
			continue
		}

		// Check if same vhost
		if user.Spec.VhostRef != r.Spec.VhostRef {
			continue
		}

		// Determine the other user's username
		otherUsername := user.Spec.Username
		if otherUsername == "" {
			otherUsername = user.Name
		}

		// If usernames match, reject
		if username == otherUsername {
			return apierrors.NewInvalid(
				schema.GroupKind{Group: "rabbitmq.openstack.org", Kind: "RabbitMQUser"},
				r.Name,
				field.ErrorList{
					field.Duplicate(
						field.NewPath("spec", "username"),
						fmt.Sprintf("username %q already exists in vhost %q on cluster %q (existing RabbitMQUser: %s)",
							username, r.Spec.VhostRef, r.Spec.RabbitmqClusterName, user.Name),
					),
				},
			)
		}
	}

	return nil
}
