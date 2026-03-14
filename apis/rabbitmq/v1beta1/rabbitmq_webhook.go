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

package v1beta1

import (
	"context"
	"time"

	common_webhook "github.com/openstack-k8s-operators/lib-common/modules/common/webhook"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/runtime/schema"
	"k8s.io/apimachinery/pkg/types"
	"k8s.io/apimachinery/pkg/util/validation/field"
	"sigs.k8s.io/controller-runtime/pkg/client"
	logf "sigs.k8s.io/controller-runtime/pkg/log"
	"sigs.k8s.io/controller-runtime/pkg/webhook"
	"sigs.k8s.io/controller-runtime/pkg/webhook/admission"
)

// RabbitMqDefaults -
type RabbitMqDefaults struct {
	ContainerImageURL string
}

var rabbitMqDefaults RabbitMqDefaults

// log is for logging in this package.
var rabbitmqlog = logf.Log.WithName("rabbitmq-resource")

// SetupRabbitMqDefaults - initialize RabbitMq spec defaults for use with either internal or external webhooks
func SetupRabbitMqDefaults(defaults RabbitMqDefaults) {
	rabbitMqDefaults = defaults
	rabbitmqlog.Info("RabbitMq defaults initialized", "defaults", defaults)
}

// Default sets default values for the RabbitMq using the provided Kubernetes client
// to check if the cluster already exists
//
// NOTE: This function has a potential race condition (TOCTOU - Time Of Check Time Of Use).
// Between reading the existing resources and applying defaults, the state could change.
// We try to mitigate the risk by relying on the controller reconciliation loop and
// by checking multiple sources (CR spec, status, cluster).
// Complete prevention would require distributed locking, which is not practical for webhooks
func (r *RabbitMq) Default(k8sClient client.Client) {
	rabbitmqlog.Info("default", "name", r.Name, "namespace", r.Namespace)

	if r.Name == "" || r.Namespace == "" {
		r.Spec.Default(true)
		return
	}

	// Determine if this is a new or existing CR to choose the right QueueType default
	isNew := true

	if k8sClient != nil {
		ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()

		// If user explicitly set QueueType, preserve it
		if r.Spec.QueueType != nil && *r.Spec.QueueType != "" {
			rabbitmqlog.Info("preserving user-specified QueueType", "name", r.Name, "queueType", *r.Spec.QueueType)
			isNew = false // skip defaulting
		} else {
			// Look up existing CR to determine if this is adoption or new creation
			existingRabbitMq := &RabbitMq{}
			err := k8sClient.Get(ctx, types.NamespacedName{
				Name: r.Name, Namespace: r.Namespace,
			}, existingRabbitMq)

			if err != nil && !apierrors.IsNotFound(err) {
				rabbitmqlog.Error(err, "failed to get existing RabbitMq CR, defaulting QueueType to Mirrored for safety",
					"name", r.Name, "namespace", r.Namespace)
				queueType := QueueTypeMirrored
				r.Spec.QueueType = &queueType
				isNew = false
			} else if err == nil {
				// Existing CR found - preserve its QueueType
				isNew = false
				if existingRabbitMq.Spec.QueueType != nil && *existingRabbitMq.Spec.QueueType != "" {
					queueType := *existingRabbitMq.Spec.QueueType
					r.Spec.QueueType = &queueType
					rabbitmqlog.Info("preserving QueueType from existing CR", "name", r.Name, "queueType", queueType)
				} else if existingRabbitMq.Status.QueueType != "" {
					statusQueueType := existingRabbitMq.Status.QueueType
					r.Spec.QueueType = &statusQueueType
					rabbitmqlog.Info("preserving QueueType from existing CR status", "name", r.Name, "queueType", statusQueueType)
				} else {
					// Existing deployment without QueueType: assume Mirrored
					queueType := QueueTypeMirrored
					r.Spec.QueueType = &queueType
					rabbitmqlog.Info("existing CR without QueueType, defaulting to Mirrored", "name", r.Name)
				}
			}
			// err is NotFound → isNew stays true, will default to Quorum
		}
	}

	r.Spec.Default(isNew)

	// Enforce Quorum when targeting RabbitMQ 4.x+ (mirrored queues not supported)
	if r.Spec.TargetVersion != nil && *r.Spec.TargetVersion != "" && IsVersion4OrLater(*r.Spec.TargetVersion) {
		if r.Spec.QueueType == nil || *r.Spec.QueueType != QueueTypeQuorum {
			rabbitmqlog.Info("enforcing Quorum queues for RabbitMQ 4.x target",
				"name", r.Name, "targetVersion", *r.Spec.TargetVersion)
			queueType := QueueTypeQuorum
			r.Spec.QueueType = &queueType
		}
	}
}

// Default - set defaults for this RabbitMq spec
func (spec *RabbitMqSpec) Default(isNew bool) {
	if spec.ContainerImage == "" {
		spec.ContainerImage = rabbitMqDefaults.ContainerImageURL
	}
	spec.RabbitMqSpecCore.Default(isNew)
}

// Default - set defaults for this RabbitMqSpecCore and migrate from old format
func (spec *RabbitMqSpecCore) Default(isNew bool) {
	// Migrate from old embedded format (persistence, rabbitmq, override) to new explicit fields
	// This handles backward compatibility for existing CRs using the old format

	// Migrate from old persistence format
	if spec.Storage.StorageClassName == nil && spec.Persistence.StorageClassName != nil {
		spec.Storage.StorageClassName = spec.Persistence.StorageClassName
	}
	if spec.Storage.Storage == nil && spec.Persistence.Storage != nil {
		spec.Storage.Storage = spec.Persistence.Storage
	}

	// Migrate from old rabbitmq config format
	if spec.Config.AdditionalConfig == "" && spec.Rabbitmq.AdditionalConfig != "" {
		spec.Config.AdditionalConfig = spec.Rabbitmq.AdditionalConfig
	}
	if spec.Config.AdvancedConfig == "" && spec.Rabbitmq.AdvancedConfig != "" {
		spec.Config.AdvancedConfig = spec.Rabbitmq.AdvancedConfig
	}

	// Migrate from old override.service format to new service field
	if spec.Override != nil && spec.Override.Service != nil {
		// Migrate service type
		if spec.Service.Type == "" && spec.Override.Service.Spec != nil && spec.Override.Service.Spec.Type != "" {
			spec.Service.Type = spec.Override.Service.Spec.Type
		}
		// Migrate service annotations
		if len(spec.Service.Annotations) == 0 && spec.Override.Service.Annotations != nil {
			spec.Service.Annotations = make(map[string]string)
			for k, v := range spec.Override.Service.Annotations {
				spec.Service.Annotations[k] = v
			}
		}
	}

	// TLS defaulting - set CaSecretName to SecretName if not explicitly set
	if spec.TLS.SecretName != "" && spec.TLS.CaSecretName == "" {
		spec.TLS.CaSecretName = spec.TLS.SecretName
	}

	// TLS defaulting - set DisableNonTLSListeners to true when TLS is enabled
	if spec.TLS.SecretName != "" {
		spec.TLS.DisableNonTLSListeners = true
	}

	// QueueType defaulting: Quorum for new clusters, preserved for existing
	if isNew && (spec.QueueType == nil || *spec.QueueType == "") {
		queueType := QueueTypeQuorum
		spec.QueueType = &queueType
	}
}

var _ webhook.Validator = &RabbitMq{}

// ValidateCreate implements webhook.Validator so a webhook will be registered for the type
func (r *RabbitMq) ValidateCreate() (admission.Warnings, error) {
	rabbitmqlog.Info("validate create", "name", r.Name)
	var allErrs field.ErrorList
	var allWarn []string
	basePath := field.NewPath("spec")

	// When a TopologyRef CR is referenced, fail if a different Namespace is
	// referenced because is not supported
	allErrs = append(allErrs, r.Spec.ValidateTopology(basePath, r.Namespace)...)

	warn, errs := r.Spec.ValidateOverride(basePath, r.Namespace)
	allWarn = append(allWarn, warn...)
	allErrs = append(allErrs, errs...)

	allErrs = append(allErrs, common_webhook.ValidateDNS1123Label(
		field.NewPath("metadata").Child("name"),
		[]string{r.Name},
		CrMaxLengthCorrection,
	)...) // omit issue with  statefulset pod label "controller-revision-hash": "<statefulset_name>-<hash>"

	allErrs = append(allErrs, r.Spec.ValidateQueueType(basePath)...)

	if len(allErrs) != 0 {
		return allWarn, apierrors.NewInvalid(
			schema.GroupKind{Group: "rabbitmq.openstack.org", Kind: "RabbitMq"},
			r.Name, allErrs,
		)
	}

	return allWarn, nil

}

// ValidateUpdate implements webhook.Validator so a webhook will be registered for the type
func (r *RabbitMq) ValidateUpdate(_ runtime.Object) (admission.Warnings, error) {
	rabbitmqlog.Info("validate update", "name", r.Name)

	var allErrs field.ErrorList
	var allWarn []string
	basePath := field.NewPath("spec")

	// When a TopologyRef CR is referenced, fail if a different Namespace is
	// referenced because is not supported
	allErrs = append(allErrs, r.Spec.ValidateTopology(basePath, r.Namespace)...)

	warn, errs := r.Spec.ValidateOverride(basePath, r.Namespace)
	allWarn = append(allWarn, warn...)
	allErrs = append(allErrs, errs...)

	allErrs = append(allErrs, r.Spec.ValidateQueueType(basePath)...)

	if len(allErrs) != 0 {
		return allWarn, apierrors.NewInvalid(
			schema.GroupKind{Group: "rabbitmq.openstack.org", Kind: "RabbitMq"},
			r.Name, allErrs,
		)
	}

	return allWarn, nil
}

// ValidateCreate performs validation when creating a new RabbitMqSpecCore.
func (spec *RabbitMqSpecCore) ValidateCreate(basePath *field.Path, namespace string) (admission.Warnings, field.ErrorList) {
	var allErrs field.ErrorList
	var allWarn []string

	allErrs = append(allErrs, spec.ValidateTopology(basePath, namespace)...)
	warn, errs := spec.ValidateOverride(basePath, namespace)
	allWarn = append(allWarn, warn...)
	allErrs = append(allErrs, errs...)
	allErrs = append(allErrs, spec.ValidateQueueType(basePath)...)

	return allWarn, allErrs
}

// ValidateUpdate performs validation when updating an existing RabbitMqSpecCore.
func (spec *RabbitMqSpecCore) ValidateUpdate(_ RabbitMqSpecCore, basePath *field.Path, namespace string) (admission.Warnings, field.ErrorList) {
	var allErrs field.ErrorList
	var allWarn []string

	allErrs = append(allErrs, spec.ValidateTopology(basePath, namespace)...)
	warn, errs := spec.ValidateOverride(basePath, namespace)
	allWarn = append(allWarn, warn...)
	allErrs = append(allErrs, errs...)
	allErrs = append(allErrs, spec.ValidateQueueType(basePath)...)

	return allWarn, allErrs
}

// ValidateQueueType validates that QueueType is one of the allowed values
func (spec *RabbitMqSpecCore) ValidateQueueType(basePath *field.Path) field.ErrorList {
	var allErrs field.ErrorList
	if spec.QueueType != nil {
		if *spec.QueueType != QueueTypeMirrored && *spec.QueueType != QueueTypeQuorum {
			allErrs = append(allErrs, field.NotSupported(
				basePath.Child("queueType"),
				*spec.QueueType,
				[]string{QueueTypeMirrored, QueueTypeQuorum},
			))
		}
	}
	return allErrs
}

// ValidateDelete implements webhook.Validator so a webhook will be registered for the type
func (r *RabbitMq) ValidateDelete() (admission.Warnings, error) {
	rabbitmqlog.Info("validate delete", "name", r.Name)

	// TODO(user): fill in your validation logic upon object deletion.
	return nil, nil
}
