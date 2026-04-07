/*
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
)

// DeprecatedEmbeddedLabelsAnnotations is an embedded subset of the fields included in
// k8s.io/apimachinery/pkg/apis/meta/v1.ObjectMeta. Only labels and annotations are included.
type DeprecatedEmbeddedLabelsAnnotations struct {
	// Map of string keys and values that can be used to organize and categorize (scope and select) objects.
	// +optional
	Labels map[string]string `json:"labels,omitempty"`
	// Annotations is an unstructured key value map stored with a resource.
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

// DeprecatedRabbitMQServiceSpec mirrors the old top-level service configuration.
// DEPRECATED: Use Override.Service instead.
type DeprecatedRabbitMQServiceSpec struct {
	// +kubebuilder:default:="ClusterIP"
	// +kubebuilder:validation:Enum=ClusterIP;LoadBalancer;NodePort
	// Type of Service to create for the cluster. Must be one of: ClusterIP, LoadBalancer, NodePort.
	// For more info see https://pkg.go.dev/k8s.io/api/core/v1#ServiceType
	// +optional
	Type corev1.ServiceType `json:"type,omitempty"`
	// Annotations to add to the Service.
	// +optional
	Annotations map[string]string `json:"annotations,omitempty"`
	// +optional
	Labels map[string]string `json:"labels,omitempty"`
	// +kubebuilder:validation:Enum=SingleStack;PreferDualStack;RequireDualStack
	// IPFamilyPolicy represents the dual-stack-ness requested or required by a Service
	// See also: https://pkg.go.dev/k8s.io/api/core/v1#IPFamilyPolicy
	// +optional
	IPFamilyPolicy *corev1.IPFamilyPolicy `json:"ipFamilyPolicy,omitempty"`
}

// DeprecatedPersistenceSpec mirrors the old rabbitmq-cluster-operator RabbitmqClusterPersistenceSpec type.
type DeprecatedPersistenceSpec struct {
	// The name of the StorageClass to claim a PersistentVolume from.
	StorageClassName *string `json:"storageClassName,omitempty"`
	// The requested size of the persistent volume attached to each Pod in the RabbitmqCluster.
	// The format of this field matches that defined by kubernetes/apimachinery.
	// See https://pkg.go.dev/k8s.io/apimachinery/pkg/api/resource#Quantity for more info on the format of this field.
	// +kubebuilder:default:="10Gi"
	Storage *resource.Quantity `json:"storage,omitempty"`
}

// DeprecatedRabbitmqConfigSpec mirrors the old rabbitmq-cluster-operator RabbitmqClusterConfigurationSpec type.
type DeprecatedRabbitmqConfigSpec struct {
	// List of plugins to enable in addition to essential plugins: rabbitmq_management,
	// rabbitmq_prometheus, and rabbitmq_peer_discovery_k8s.
	// +optional
	// +kubebuilder:validation:MaxItems=100
	AdditionalPlugins []RabbitMQPlugin `json:"additionalPlugins,omitempty"`
	// Modify to add to the rabbitmq.conf file in addition to default configurations set by the operator.
	// Modifying this property on an existing RabbitmqCluster will trigger a StatefulSet rolling restart
	// and will cause rabbitmq downtime.
	// For more information on this config, see https://www.rabbitmq.com/configure.html#config-file
	// +optional
	// +kubebuilder:validation:MaxLength=100000
	AdditionalConfig string `json:"additionalConfig,omitempty"`
	// Specify any rabbitmq advanced.config configurations to apply to the cluster.
	// For more information on advanced config, see https://www.rabbitmq.com/configure.html#advanced-config-file
	// +optional
	// +kubebuilder:validation:MaxLength=100000
	AdvancedConfig string `json:"advancedConfig,omitempty"`
	// Modify to add to the rabbitmq-env.conf file. Modifying this property on an existing
	// RabbitmqCluster will trigger a StatefulSet rolling restart and will cause rabbitmq downtime.
	// For more information on env config, see https://www.rabbitmq.com/man/rabbitmq-env.conf.5.html
	// +optional
	// +kubebuilder:validation:MaxLength=100000
	EnvConfig string `json:"envConfig,omitempty"`
	// Erlang Inet configuration to apply to the Erlang VM running rabbit.
	// See also: https://www.erlang.org/doc/apps/erts/inet_cfg.html
	// +optional
	// +kubebuilder:validation:MaxLength=2000
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
// VaultSpec will add Vault annotations (see https://www.vaultproject.io/docs/platform/k8s/injector/annotations)
// to RabbitMQ Pods. It requires a Vault Agent Sidecar Injector
// (https://www.vaultproject.io/docs/platform/k8s/injector) to be installed in the K8s cluster.
type DeprecatedVaultSpec struct {
	// Role in Vault.
	// If vault.defaultUserPath is set, this role must have capability to read the pre-created default user
	// credential in Vault.
	// If vault.tls is set, this role must have capability to create and update certificates in the Vault PKI
	// engine for the domains "<namespace>" and "<namespace>.svc".
	// +optional
	Role string `json:"role,omitempty"`
	// Vault annotations that override the Vault annotations set by the cluster-operator.
	// For a list of valid Vault annotations, see
	// https://www.vaultproject.io/docs/platform/k8s/injector/annotations
	// +optional
	Annotations map[string]string `json:"annotations,omitempty"`
	// Path in Vault to access a KV (Key-Value) secret with the fields username and password
	// for the default user. For example "secret/data/rabbitmq/config".
	// +optional
	DefaultUserPath string `json:"defaultUserPath,omitempty"`
	// Sidecar container that updates the default user's password in RabbitMQ when it changes in Vault.
	// Additionally, it updates /var/lib/rabbitmq/.rabbitmqadmin.conf (used by rabbitmqadmin CLI).
	// Set to empty string to disable the sidecar container.
	// +optional
	DefaultUserUpdaterImage string `json:"defaultUserUpdaterImage,omitempty"`
	// +optional
	TLS *DeprecatedVaultTLSSpec `json:"tls,omitempty"`
}

// DeprecatedVaultTLSSpec mirrors the old rabbitmq-cluster-operator VaultSpec TLS fields.
type DeprecatedVaultTLSSpec struct {
	// Path in Vault PKI engine. For example "pki/issue/hashicorp-com". Required.
	// +optional
	PkiIssuerPath string `json:"pkiIssuerPath,omitempty"`
	// Specifies an optional path to retrieve the root CA from vault.
	// Useful if certificates are issued by an intermediate CA.
	// +optional
	PkiRootPath string `json:"pkiRootPath,omitempty"`
	// Specifies the requested Subject Alternative Names (SANs), in a comma-delimited list.
	// These will be appended to the SANs added by the cluster-operator.
	// +optional
	AltNames string `json:"altNames,omitempty"`
	// Specifies the requested certificate Common Name (CN).
	// Defaults to <serviceName>.<namespace>.svc if not provided.
	// +optional
	CommonName string `json:"commonName,omitempty"`
	// Specifies the requested IP Subject Alternative Names, in a comma-delimited list.
	// +optional
	IpSans string `json:"ipSans,omitempty"`
}
