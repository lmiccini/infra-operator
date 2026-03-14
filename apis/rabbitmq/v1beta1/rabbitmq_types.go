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
	"bytes"
	"encoding/json"
	"fmt"

	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/api/resource"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/util/validation/field"
	"sigs.k8s.io/controller-runtime/pkg/webhook/admission"

	topologyv1 "github.com/openstack-k8s-operators/infra-operator/apis/topology/v1beta1"
	condition "github.com/openstack-k8s-operators/lib-common/modules/common/condition"
	"github.com/openstack-k8s-operators/lib-common/modules/common/service"
	"github.com/openstack-k8s-operators/lib-common/modules/common/util"
)

const (
	// Container image fall-back defaults

	// RabbitMqContainerImage is the fall-back container image for RabbitMQ
	RabbitMqContainerImage = "quay.io/podified-antelope-centos9/openstack-rabbitmq:current-podified"

	// CrMaxLengthCorrection - DNS1123LabelMaxLength (63) - CrMaxLengthCorrection used in validation to
	// omit issue with statefulset pod label "controller-revision-hash": "<statefulset_name>-<hash>"
	// Int32 is a 10 character + hyphen = 11
	CrMaxLengthCorrection   = 11
	errInvalidOverride      = "invalid spec override (%s)"
	warnOverrideStatefulSet = "%s: is deprecated and will be removed in a future API version"

	// Queue types
	// QueueTypeMirrored - mirrored queue type
	QueueTypeMirrored = "Mirrored"
	// QueueTypeQuorum - quorum queue type
	QueueTypeQuorum = "Quorum"
	// QueueTypeNone - no special queue type
	QueueTypeNone = "None"

	// AnnotationClientsReconfigured - set to "true" when dataplane clients have been
	// reconfigured for quorum queues, allowing the proxy sidecar to be removed
	AnnotationClientsReconfigured = "rabbitmq.openstack.org/clients-reconfigured"
)

// UpgradePhase tracks the current phase of a version upgrade or queue migration.
type UpgradePhase string

const (
	// UpgradePhaseNone - no upgrade in progress
	UpgradePhaseNone UpgradePhase = ""
	// UpgradePhaseDeletingResources - deleting ha-all policy and StatefulSet
	UpgradePhaseDeletingResources UpgradePhase = "DeletingResources"
	// UpgradePhaseWaitingForCluster - waiting for cluster to become ready with new version
	UpgradePhaseWaitingForCluster UpgradePhase = "WaitingForCluster"
)

// WipeReason describes why a storage wipe was initiated.
type WipeReason string

const (
	// WipeReasonNone - no wipe in progress
	WipeReasonNone WipeReason = ""
	// WipeReasonVersionUpgrade - storage wipe due to major/minor version change
	WipeReasonVersionUpgrade WipeReason = "VersionUpgrade"
	// WipeReasonQueueTypeMigration - storage wipe due to queue type migration
	WipeReasonQueueTypeMigration WipeReason = "QueueTypeMigration"
)

// PodOverride defines per-pod service configurations
type PodOverride struct {
	// +kubebuilder:validation:Optional
	// +listType=atomic
	// Services - list of per-pod service overrides
	Services []service.OverrideSpec `json:"services,omitempty"`
}

// RabbitMqSpec defines the desired state of RabbitMq
type RabbitMqSpec struct {
	RabbitMqSpecCore `json:",inline"`
	// +kubebuilder:validation:Required
	// Name of the rabbitmq container image to run (will be set to environmental default if empty)
	ContainerImage string `json:"containerImage"`
}

// RabbitMQStorageSpec defines storage configuration for RabbitMQ
type RabbitMQStorageSpec struct {
	// +kubebuilder:validation:Optional
	// StorageClassName - Storage class name for the persistent volume claim
	StorageClassName *string `json:"storageClassName,omitempty"`
	// +kubebuilder:validation:Optional
	// +kubebuilder:default:="10Gi"
	// Storage - Size of the persistent volume claim
	Storage *resource.Quantity `json:"storage,omitempty"`
}

// RabbitMQConfigSpec defines RabbitMQ configuration options
type RabbitMQConfigSpec struct {
	// +kubebuilder:validation:Optional
	// +listType=atomic
	// AdditionalPlugins - Additional RabbitMQ plugins to enable
	AdditionalPlugins []string `json:"additionalPlugins,omitempty"`
	// +kubebuilder:validation:Optional
	// AdditionalConfig - Additional RabbitMQ configuration
	AdditionalConfig string `json:"additionalConfig,omitempty"`
	// +kubebuilder:validation:Optional
	// AdvancedConfig - Erlang advanced configuration
	AdvancedConfig string `json:"advancedConfig,omitempty"`
}

// RabbitMQTLSSpec defines TLS configuration for RabbitMQ
type RabbitMQTLSSpec struct {
	// +kubebuilder:validation:Optional
	// SecretName - Name of the secret containing TLS certificates (tls.crt and tls.key)
	SecretName string `json:"secretName,omitempty"`
	// +kubebuilder:validation:Optional
	// CaSecretName - Name of the secret containing CA certificate (ca.crt)
	CaSecretName string `json:"caSecretName,omitempty"`
	// +kubebuilder:validation:Optional
	// DisableNonTLSListeners - Disable non-TLS listeners
	DisableNonTLSListeners bool `json:"disableNonTLSListeners,omitempty"`
}

// RabbitMQServiceSpec defines service configuration for RabbitMQ
type RabbitMQServiceSpec struct {
	// +kubebuilder:validation:Optional
	// Type - Service type (ClusterIP, LoadBalancer, etc.)
	Type corev1.ServiceType `json:"type,omitempty"`
	// +kubebuilder:validation:Optional
	// Annotations - Service annotations
	Annotations map[string]string `json:"annotations,omitempty"`
	// +kubebuilder:validation:Optional
	// IPFamilyPolicy - IP family policy for the service
	IPFamilyPolicy *corev1.IPFamilyPolicy `json:"ipFamilyPolicy,omitempty"`
}

// RabbitMqSpecCore - this version is used by the OpenStackControlplane CR (no container images)
type RabbitMqSpecCore struct {
	// +kubebuilder:validation:Optional
	// +kubebuilder:validation:Minimum:=0
	// +kubebuilder:default:=1
	// +operator-sdk:csv:customresourcedefinitions:type=spec
	// Replicas - Number of RabbitMQ nodes in the cluster
	Replicas *int32 `json:"replicas,omitempty"`

	// +kubebuilder:validation:Optional
	// +operator-sdk:csv:customresourcedefinitions:type=spec
	// Resources - Resource requirements for RabbitMQ containers
	Resources *corev1.ResourceRequirements `json:"resources,omitempty"`

	// +kubebuilder:validation:Optional
	// +operator-sdk:csv:customresourcedefinitions:type=spec
	// Storage - Persistent storage configuration
	Storage RabbitMQStorageSpec `json:"storage,omitempty"`

	// +kubebuilder:validation:Optional
	// +operator-sdk:csv:customresourcedefinitions:type=spec
	// Config - RabbitMQ configuration options
	Config RabbitMQConfigSpec `json:"config,omitempty"`

	// +kubebuilder:validation:Optional
	// +operator-sdk:csv:customresourcedefinitions:type=spec
	// TLS - TLS configuration
	TLS RabbitMQTLSSpec `json:"tls,omitempty"`

	// +kubebuilder:validation:Optional
	// +operator-sdk:csv:customresourcedefinitions:type=spec
	// Service - Service configuration
	Service RabbitMQServiceSpec `json:"service,omitempty"`

	// +kubebuilder:validation:Optional
	// +operator-sdk:csv:customresourcedefinitions:type=spec
	// Affinity - Pod affinity/anti-affinity rules
	Affinity *corev1.Affinity `json:"affinity,omitempty"`

	// +kubebuilder:validation:Optional
	// +operator-sdk:csv:customresourcedefinitions:type=spec
	// Tolerations - Pod tolerations
	Tolerations []corev1.Toleration `json:"tolerations,omitempty"`

	// +kubebuilder:validation:Optional
	// +operator-sdk:csv:customresourcedefinitions:type=spec
	// NodeSelector to target subset of worker nodes running this service
	NodeSelector *map[string]string `json:"nodeSelector,omitempty"`

	// +kubebuilder:validation:Optional
	// TopologyRef to apply the Topology defined by the associated CR referenced by name
	TopologyRef *topologyv1.TopoRef `json:"topologyRef,omitempty"`

	// +kubebuilder:validation:Optional
	// +operator-sdk:csv:customresourcedefinitions:type=spec
	// QueueType to eventually apply the ha-all policy or configure default queue type for the cluster.
	// Allowed values are: None, Mirrored, Quorum. Defaults to Quorum if not specified.
	QueueType *string `json:"queueType,omitempty"`

	// +kubebuilder:validation:Optional
	// +operator-sdk:csv:customresourcedefinitions:type=spec
	// PodOverride - Override configuration for per-pod services
	PodOverride *PodOverride `json:"podOverride,omitempty"`

	// +kubebuilder:validation:Optional
	// +kubebuilder:validation:Minimum:=0
	// +kubebuilder:default:=604800
	// +operator-sdk:csv:customresourcedefinitions:type=spec
	// TerminationGracePeriodSeconds - Timeout for graceful pod termination
	TerminationGracePeriodSeconds *int64 `json:"terminationGracePeriodSeconds,omitempty"`

	// +kubebuilder:validation:Optional
	// +operator-sdk:csv:customresourcedefinitions:type=spec
	// SkipPostDeploySteps - Skip post-deploy queue rebalancing
	SkipPostDeploySteps bool `json:"skipPostDeploySteps,omitempty"`

	// +kubebuilder:validation:Optional
	// +kubebuilder:validation:Pattern=`^\d+\.\d+(\.\d+)?$`
	// +operator-sdk:csv:customresourcedefinitions:type=spec
	// TargetVersion - the desired RabbitMQ version (e.g., "4.2", "3.13.1").
	// When set to a version different from Status.CurrentVersion, the controller
	// will initiate a storage wipe and version upgrade. The controller updates
	// Status.CurrentVersion once the upgrade completes.
	TargetVersion *string `json:"targetVersion,omitempty"`

	// DEPRECATED: For backward compatibility with old RabbitmqClusterSpecCore format.
	// Use explicit fields above instead. This will be removed in a future release.
	// +kubebuilder:validation:Optional
	// +kubebuilder:pruning:PreserveUnknownFields
	Override *DeprecatedOverride `json:"override,omitempty"`

	// DEPRECATED: For backward compatibility with old format.
	// Use Storage field instead.
	// +kubebuilder:validation:Optional
	Persistence DeprecatedPersistenceSpec `json:"persistence,omitempty"`

	// DEPRECATED: For backward compatibility with old format.
	// Use Config field instead.
	// +kubebuilder:validation:Optional
	Rabbitmq DeprecatedRabbitmqConfigSpec `json:"rabbitmq,omitempty"`
}

// RabbitmqClusterSecretReference contains reference to a secret
type RabbitmqClusterSecretReference struct {
	// Name of the secret
	Name string `json:"name,omitempty"`
	// Namespace of the secret
	Namespace string `json:"namespace,omitempty"`
	// Keys in the secret
	Keys map[string]string `json:"keys,omitempty"`
}

// RabbitmqClusterServiceReference contains reference to a service
type RabbitmqClusterServiceReference struct {
	// Name of the service
	Name string `json:"name,omitempty"`
	// Namespace of the service
	Namespace string `json:"namespace,omitempty"`
}

// RabbitmqClusterDefaultUser contains default user information
type RabbitmqClusterDefaultUser struct {
	// SecretReference - reference to the secret containing credentials
	SecretReference *RabbitmqClusterSecretReference `json:"secretReference,omitempty"`
	// ServiceReference - reference to the service
	ServiceReference *RabbitmqClusterServiceReference `json:"serviceReference,omitempty"`
}

// RabbitMqStatus defines the observed state of RabbitMq
type RabbitMqStatus struct {
	// Conditions
	Conditions condition.Conditions `json:"conditions,omitempty" optional:"true"`

	// ObservedGeneration - the most recent generation observed for this
	// service. If the observed generation is less than the spec generation,
	// then the controller has not processed the latest changes injected by
	// the opentack-operator in the top-level CR (e.g. the ContainerImage)
	ObservedGeneration int64 `json:"observedGeneration,omitempty"`

	// LastAppliedTopology - the last applied Topology
	LastAppliedTopology *topologyv1.TopoRef `json:"lastAppliedTopology,omitempty"`

	// QueueType - store whether default ha-all policy is present or not
	QueueType string `json:"queueType,omitempty"`

	// ReadyCount tracks ready replicas
	ReadyCount int32 `json:"readyCount,omitempty"`

	// DefaultUser - Identifying information on default user secret
	DefaultUser *RabbitmqClusterDefaultUser `json:"defaultUser,omitempty"`

	// ServiceHostnames - list of per-pod service hostnames for RabbitMQ cluster.
	// When populated, transport URLs use these hostnames instead of pod names.
	// +listType=atomic
	ServiceHostnames []string `json:"serviceHostnames,omitempty"`

	// OldCRCleaned - whether the old rabbitmq.com RabbitmqCluster CR has been
	// cleaned up during migration from rabbitmq-cluster-operator.
	OldCRCleaned bool `json:"oldCRCleaned,omitempty"`

	// CurrentVersion - the currently deployed RabbitMQ version (e.g., "3.9", "4.2")
	// This is controller-managed and reflects the actual running version.
	// Set Spec.TargetVersion to request a version change.
	CurrentVersion string `json:"currentVersion,omitempty"`

	// UpgradePhase - tracks the current phase of a version upgrade or migration.
	// This allows resuming upgrades that failed midway.
	UpgradePhase UpgradePhase `json:"upgradePhase,omitempty"`

	// WipeReason - tracks why the current storage wipe was initiated.
	// Persisted so that resumed upgrades use the correct handling path.
	WipeReason WipeReason `json:"wipeReason,omitempty"`

	// +kubebuilder:validation:Optional
	// ProxyRequired - tracks whether the AMQP proxy sidecar is required for this cluster.
	// Set to true when upgrading from RabbitMQ 3.x to 4.x with Quorum queues.
	// The proxy allows non-durable clients to work with quorum queues during the upgrade window.
	// Only cleared when the AnnotationClientsReconfigured annotation is set to "true".
	ProxyRequired bool `json:"proxyRequired"`
}

//+kubebuilder:object:root=true
//+kubebuilder:subresource:status
//+kubebuilder:resource:path=rabbitmqs,categories=all;rabbitmq
//+kubebuilder:printcolumn:name="Status",type="string",JSONPath=".status.conditions[0].status",description="Status"
//+kubebuilder:printcolumn:name="Message",type="string",JSONPath=".status.conditions[0].message",description="Message"

// RabbitMq is the Schema for the rabbitmqs API
type RabbitMq struct {
	metav1.TypeMeta   `json:",inline"`
	metav1.ObjectMeta `json:"metadata,omitempty"`

	Spec   RabbitMqSpec   `json:"spec,omitempty"`
	Status RabbitMqStatus `json:"status,omitempty"`
}

//+kubebuilder:object:root=true

// RabbitMqList contains a list of RabbitMq
type RabbitMqList struct {
	metav1.TypeMeta `json:",inline"`
	metav1.ListMeta `json:"metadata,omitempty"`
	Items           []RabbitMq `json:"items"`
}

func init() {
	SchemeBuilder.Register(&RabbitMq{}, &RabbitMqList{})
}

// IsReady - returns true if service is ready to serve requests
func (instance RabbitMq) IsReady() bool {
	return instance.Status.Conditions.IsTrue(condition.ReadyCondition)
}

// RbacConditionsSet - set the conditions for the rbac object
func (instance RabbitMq) RbacConditionsSet(c *condition.Condition) {
	instance.Status.Conditions.Set(c)
}

// RbacNamespace - return the namespace
func (instance RabbitMq) RbacNamespace() string {
	return instance.Namespace
}

// RbacResourceName - return the name to be used for rbac objects (serviceaccount, role, rolebinding)
func (instance RabbitMq) RbacResourceName() string {
	return instance.Name + "-server"
}

// SetupDefaults - initializes any CRD field defaults based on environment variables (the defaulting mechanism itself is implemented via webhooks)
func SetupDefaults() {
	// Acquire environmental defaults and initialize Redis defaults with them
	rabbitMqDefaults := RabbitMqDefaults{
		ContainerImageURL: util.GetEnvVar("RELATED_IMAGE_RABBITMQ_IMAGE_URL_DEFAULT", RabbitMqContainerImage),
	}

	SetupRabbitMqDefaults(rabbitMqDefaults)
}

// ValidateTopology -
func (instance *RabbitMqSpecCore) ValidateTopology(
	basePath *field.Path,
	namespace string,
) field.ErrorList {
	var allErrs field.ErrorList
	allErrs = append(allErrs, topologyv1.ValidateTopologyRef(
		instance.TopologyRef,
		*basePath.Child("topologyRef"), namespace)...)
	return allErrs
}

// ValidateOverride validates the override section of RabbitMqSpecCore.
func (instance *RabbitMqSpecCore) ValidateOverride(
	basePath *field.Path,
	_ string,
) (admission.Warnings, field.ErrorList) {
	var allErrs field.ErrorList
	var allWarn admission.Warnings
	if instance.Override != nil && instance.Override.StatefulSet != nil {
		var overrideObj DeprecatedStatefulSetOverride
		dec := json.NewDecoder(bytes.NewReader(instance.Override.StatefulSet.Raw))
		dec.DisallowUnknownFields()
		err := dec.Decode(&overrideObj)
		if err != nil {
			return nil, append(allErrs, field.Invalid(basePath.Child("override"), "<json>", fmt.Sprintf(errInvalidOverride, err)))
		}
		allWarn = append(allWarn, fmt.Sprintf(warnOverrideStatefulSet, basePath.Child("override").Child("statefulset").String()))
	}
	return allWarn, nil
}
