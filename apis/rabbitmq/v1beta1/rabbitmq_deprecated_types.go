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

package v1beta1

// DEPRECATED TYPES
// These types are local mirrors of the old rabbitmq-cluster-operator types,
// kept only for backward compatibility with existing CRs during migration.
// They will be removed in a future release once all CRs have been migrated
// to use the new explicit fields in RabbitMqSpecCore.

import (
	appsv1 "k8s.io/api/apps/v1"
	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/api/resource"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
)

// DeprecatedOverride mirrors the old rabbitmq-cluster-operator OverrideTrimmed type.
// +kubebuilder:pruning:PreserveUnknownFields
type DeprecatedOverride struct {
	// Override configuration for the RabbitMQ StatefulSet.
	StatefulSet *runtime.RawExtension `json:"statefulSet,omitempty"`
	// Override configuration for the Service created to serve traffic to the cluster.
	Service *DeprecatedServiceOverride `json:"service,omitempty"`
}

// DeprecatedServiceOverride mirrors the old rabbitmq-cluster-operator Service type.
type DeprecatedServiceOverride struct {
	// +optional
	*DeprecatedEmbeddedLabelsAnnotations `json:"metadata,omitempty"`
	// Spec defines the behavior of a Service.
	// +optional
	Spec *corev1.ServiceSpec `json:"spec,omitempty"`
}

// DeprecatedEmbeddedLabelsAnnotations mirrors the old rabbitmq-cluster-operator EmbeddedLabelsAnnotations type.
type DeprecatedEmbeddedLabelsAnnotations struct {
	// +optional
	Labels map[string]string `json:"labels,omitempty"`
	// +optional
	Annotations map[string]string `json:"annotations,omitempty"`
}

// DeprecatedStatefulSetOverride mirrors the old rabbitmq-cluster-operator StatefulSet type.
// Used for webhook validation of the override.statefulSet JSON field.
type DeprecatedStatefulSetOverride struct {
	// +optional
	*DeprecatedEmbeddedLabelsAnnotations `json:"metadata,omitempty"`
	// +optional
	Spec *DeprecatedStatefulSetSpec `json:"spec,omitempty"`
}

// DeprecatedStatefulSetSpec mirrors a subset of the old rabbitmq-cluster-operator StatefulSetSpec type.
type DeprecatedStatefulSetSpec struct {
	// +optional
	Replicas *int32 `json:"replicas,omitempty"`
	// +optional
	Selector *metav1.LabelSelector `json:"selector,omitempty"`
	// +optional
	Template *DeprecatedPodTemplateSpec `json:"template,omitempty"`
	// +optional
	VolumeClaimTemplates []corev1.PersistentVolumeClaim `json:"volumeClaimTemplates,omitempty"`
	// +optional
	ServiceName string `json:"serviceName,omitempty"`
	// +optional
	PodManagementPolicy appsv1.PodManagementPolicyType `json:"podManagementPolicy,omitempty"`
	// +optional
	UpdateStrategy *appsv1.StatefulSetUpdateStrategy `json:"updateStrategy,omitempty"`
	// +optional
	MinReadySeconds int32 `json:"minReadySeconds,omitempty"`
	// +optional
	PersistentVolumeClaimRetentionPolicy *appsv1.StatefulSetPersistentVolumeClaimRetentionPolicy `json:"persistentVolumeClaimRetentionPolicy,omitempty"`
}

// DeprecatedPodTemplateSpec mirrors the old rabbitmq-cluster-operator PodTemplateSpec type.
type DeprecatedPodTemplateSpec struct {
	// +optional
	*DeprecatedEmbeddedObjectMeta `json:"metadata,omitempty"`
	// +optional
	Spec *corev1.PodSpec `json:"spec,omitempty"`
}

// DeprecatedEmbeddedObjectMeta mirrors the old rabbitmq-cluster-operator EmbeddedObjectMeta type.
type DeprecatedEmbeddedObjectMeta struct {
	// +optional
	Name string `json:"name,omitempty"`
	// +optional
	Namespace string `json:"namespace,omitempty"`
	// +optional
	Labels map[string]string `json:"labels,omitempty"`
	// +optional
	Annotations map[string]string `json:"annotations,omitempty"`
}

// DeprecatedPersistenceSpec mirrors the old rabbitmq-cluster-operator RabbitmqClusterPersistenceSpec type.
type DeprecatedPersistenceSpec struct {
	// The name of the StorageClass to claim a PersistentVolume from.
	StorageClassName *string `json:"storageClassName,omitempty"`
	// The requested size of the persistent volume.
	// +kubebuilder:default:="10Gi"
	Storage *resource.Quantity `json:"storage,omitempty"`
}

// DeprecatedRabbitmqConfigSpec mirrors the old rabbitmq-cluster-operator RabbitmqClusterConfigurationSpec type.
type DeprecatedRabbitmqConfigSpec struct {
	// +optional
	AdditionalPlugins []string `json:"additionalPlugins,omitempty"`
	// +optional
	AdditionalConfig string `json:"additionalConfig,omitempty"`
	// +optional
	AdvancedConfig string `json:"advancedConfig,omitempty"`
	// +optional
	EnvConfig string `json:"envConfig,omitempty"`
	// +optional
	ErlangInetConfig string `json:"erlangInetConfig,omitempty"`
}

// DeprecatedSecretBackendSpec mirrors the old rabbitmq-cluster-operator SecretBackend type.
type DeprecatedSecretBackendSpec struct {
	// +optional
	ExternalSecret *corev1.LocalObjectReference `json:"externalSecret,omitempty"`
	// +optional
	Vault *DeprecatedVaultSpec `json:"vault,omitempty"`
}

// DeprecatedVaultSpec mirrors the old rabbitmq-cluster-operator VaultSpec type.
type DeprecatedVaultSpec struct {
	// +optional
	Role string `json:"role,omitempty"`
	// +optional
	Annotations map[string]string `json:"annotations,omitempty"`
	// +optional
	DefaultUserPath string `json:"defaultUserPath,omitempty"`
	// +optional
	DefaultUserUpdaterImage string `json:"defaultUserUpdaterImage,omitempty"`
	// +optional
	TLS *DeprecatedVaultTLSSpec `json:"tls,omitempty"`
}

// DeprecatedVaultTLSSpec mirrors the old rabbitmq-cluster-operator VaultSpec TLS fields.
type DeprecatedVaultTLSSpec struct {
	// +optional
	PkiIssuerPath string `json:"pkiIssuerPath,omitempty"`
	// +optional
	PkiRootPath string `json:"pkiRootPath,omitempty"`
	// +optional
	AltNames string `json:"altNames,omitempty"`
	// +optional
	CommonName string `json:"commonName,omitempty"`
	// +optional
	IpSans string `json:"ipSans,omitempty"`
}
