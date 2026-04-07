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
	. "github.com/onsi/ginkgo/v2" //revive:disable:dot-imports
	. "github.com/onsi/gomega"    //revive:disable:dot-imports
	rabbitmqv1beta1 "github.com/openstack-k8s-operators/infra-operator/apis/rabbitmq/v1beta1"
	"github.com/openstack-k8s-operators/lib-common/modules/common/service"
	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/api/resource"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/utils/ptr"
)

var _ = Describe("RabbitMq webhook", func() {
	Context("Default method", func() {
		It("should default QueueType to Quorum for a new cluster", func() {
			rabbitmq := &rabbitmqv1beta1.RabbitMq{
				ObjectMeta: metav1.ObjectMeta{
					Name:      "test-rabbitmq-new",
					Namespace: "default",
				},
				Spec: rabbitmqv1beta1.RabbitMqSpec{},
			}

			rabbitmq.Default(k8sClient)

			Expect(rabbitmq.Spec.QueueType).NotTo(BeNil())
			Expect(*rabbitmq.Spec.QueueType).To(Equal(rabbitmqv1beta1.QueueTypeQuorum))
		})

		It("should preserve explicitly set QueueType=Quorum", func() {
			rabbitmq := &rabbitmqv1beta1.RabbitMq{
				ObjectMeta: metav1.ObjectMeta{
					Name:      "test-rabbitmq-quorum",
					Namespace: "default",
				},
				Spec: rabbitmqv1beta1.RabbitMqSpec{
					RabbitMqSpecCore: rabbitmqv1beta1.RabbitMqSpecCore{
						QueueType: ptr.To(rabbitmqv1beta1.QueueTypeQuorum),
					},
				},
			}

			rabbitmq.Default(k8sClient)

			Expect(rabbitmq.Spec.QueueType).NotTo(BeNil())
			Expect(*rabbitmq.Spec.QueueType).To(Equal(rabbitmqv1beta1.QueueTypeQuorum))
		})

		It("should preserve explicitly set QueueType=Mirrored", func() {
			rabbitmq := &rabbitmqv1beta1.RabbitMq{
				ObjectMeta: metav1.ObjectMeta{
					Name:      "test-rabbitmq-mirrored",
					Namespace: "default",
				},
				Spec: rabbitmqv1beta1.RabbitMqSpec{
					RabbitMqSpecCore: rabbitmqv1beta1.RabbitMqSpecCore{
						QueueType: ptr.To(rabbitmqv1beta1.QueueTypeMirrored),
					},
				},
			}

			rabbitmq.Default(k8sClient)

			Expect(rabbitmq.Spec.QueueType).NotTo(BeNil())
			Expect(*rabbitmq.Spec.QueueType).To(Equal(rabbitmqv1beta1.QueueTypeMirrored))
		})

		It("should force QueueType from Mirrored to Quorum with 4.x target", func() {
			rabbitmq := &rabbitmqv1beta1.RabbitMq{
				ObjectMeta: metav1.ObjectMeta{
					Name:      "test-rabbitmq-4x",
					Namespace: "default",
				},
				Spec: rabbitmqv1beta1.RabbitMqSpec{
					RabbitMqSpecCore: rabbitmqv1beta1.RabbitMqSpecCore{
						QueueType:     ptr.To(rabbitmqv1beta1.QueueTypeMirrored),
						TargetVersion: ptr.To("4.2"),
					},
				},
			}

			rabbitmq.Default(k8sClient)

			// Default() forces Mirrored → Quorum because mirrored queues are not supported in 4.x
			Expect(rabbitmq.Spec.QueueType).NotTo(BeNil())
			Expect(*rabbitmq.Spec.QueueType).To(Equal(rabbitmqv1beta1.QueueTypeQuorum))
		})

		It("should reject Mirrored queues with RabbitMQ 4.x via validation (safety net)", func() {
			rabbitmq := &rabbitmqv1beta1.RabbitMq{
				ObjectMeta: metav1.ObjectMeta{
					Name:      "test-rabbitmq-4x-invalid",
					Namespace: "default",
				},
				Spec: rabbitmqv1beta1.RabbitMqSpec{
					RabbitMqSpecCore: rabbitmqv1beta1.RabbitMqSpecCore{
						QueueType:     ptr.To(rabbitmqv1beta1.QueueTypeMirrored),
						TargetVersion: ptr.To("4.2"),
					},
				},
			}

			_, err := rabbitmq.ValidateCreate()
			Expect(err).To(HaveOccurred())
			Expect(err.Error()).To(ContainSubstring("Mirrored queues are not supported"))
		})

		It("should set ContainerImage default when not set", func() {
			rabbitmqv1beta1.SetupRabbitMqDefaults(rabbitmqv1beta1.RabbitMqDefaults{
				ContainerImageURL: "test-image:latest",
			})

			rabbitmq := &rabbitmqv1beta1.RabbitMq{
				ObjectMeta: metav1.ObjectMeta{
					Name:      "test-rabbitmq-image",
					Namespace: "default",
				},
				Spec: rabbitmqv1beta1.RabbitMqSpec{},
			}

			rabbitmq.Default(k8sClient)

			Expect(rabbitmq.Spec.ContainerImage).To(Equal("test-image:latest"))
			Expect(rabbitmq.Spec.QueueType).NotTo(BeNil())
			Expect(*rabbitmq.Spec.QueueType).To(Equal(rabbitmqv1beta1.QueueTypeQuorum))
		})

		It("should not override existing ContainerImage", func() {
			rabbitmqv1beta1.SetupRabbitMqDefaults(rabbitmqv1beta1.RabbitMqDefaults{
				ContainerImageURL: "test-image:latest",
			})

			rabbitmq := &rabbitmqv1beta1.RabbitMq{
				ObjectMeta: metav1.ObjectMeta{
					Name:      "test-rabbitmq-custom-image",
					Namespace: "default",
				},
				Spec: rabbitmqv1beta1.RabbitMqSpec{
					ContainerImage: "custom-image:v1",
				},
			}

			rabbitmq.Default(k8sClient)

			Expect(rabbitmq.Spec.ContainerImage).To(Equal("custom-image:v1"))
		})

		It("should migrate persistence.storage to storage.storage", func() {
			storageSize := resource.MustParse("50Gi")
			rabbitmq := &rabbitmqv1beta1.RabbitMq{
				ObjectMeta: metav1.ObjectMeta{
					Name:      "test-rabbitmq-persist",
					Namespace: "default",
				},
				Spec: rabbitmqv1beta1.RabbitMqSpec{
					RabbitMqSpecCore: rabbitmqv1beta1.RabbitMqSpecCore{
						Persistence: rabbitmqv1beta1.DeprecatedPersistenceSpec{
							Storage: &storageSize,
						},
					},
				},
			}

			rabbitmq.Default(k8sClient)

			Expect(rabbitmq.Spec.Persistence.Storage).NotTo(BeNil())
			Expect(rabbitmq.Spec.Persistence.Storage.String()).To(Equal("50Gi"))
		})

		It("should preserve persistence.storageClassName", func() {
			rabbitmq := &rabbitmqv1beta1.RabbitMq{
				ObjectMeta: metav1.ObjectMeta{
					Name:      "test-rabbitmq-sc",
					Namespace: "default",
				},
				Spec: rabbitmqv1beta1.RabbitMqSpec{
					RabbitMqSpecCore: rabbitmqv1beta1.RabbitMqSpecCore{
						Persistence: rabbitmqv1beta1.DeprecatedPersistenceSpec{
							StorageClassName: ptr.To("fast-ssd"),
						},
					},
				},
			}

			rabbitmq.Default(k8sClient)

			Expect(rabbitmq.Spec.Persistence.StorageClassName).NotTo(BeNil())
			Expect(*rabbitmq.Spec.Persistence.StorageClassName).To(Equal("fast-ssd"))
		})

		It("should migrate rabbitmq config to config fields", func() {
			rabbitmq := &rabbitmqv1beta1.RabbitMq{
				ObjectMeta: metav1.ObjectMeta{
					Name:      "test-rabbitmq-config",
					Namespace: "default",
				},
				Spec: rabbitmqv1beta1.RabbitMqSpec{
					RabbitMqSpecCore: rabbitmqv1beta1.RabbitMqSpecCore{
						Rabbitmq: rabbitmqv1beta1.DeprecatedRabbitmqConfigSpec{
							AdditionalConfig:  "log.console.level = debug",
							AdvancedConfig:    "[{rabbit,[]}].",
							AdditionalPlugins: []rabbitmqv1beta1.RabbitMQPlugin{"rabbitmq_shovel"},
							EnvConfig:         "NODENAME=rabbit@localhost",
							ErlangInetConfig:  "{inet6,true}.",
						},
					},
				},
			}

			rabbitmq.Default(k8sClient)

			Expect(rabbitmq.Spec.Rabbitmq.AdditionalConfig).To(Equal("log.console.level = debug"))
			Expect(rabbitmq.Spec.Rabbitmq.AdvancedConfig).To(Equal("[{rabbit,[]}]."))
			Expect(rabbitmq.Spec.Rabbitmq.AdditionalPlugins).To(Equal([]rabbitmqv1beta1.RabbitMQPlugin{"rabbitmq_shovel"}))
			Expect(rabbitmq.Spec.Rabbitmq.EnvConfig).To(Equal("NODENAME=rabbit@localhost"))
			Expect(rabbitmq.Spec.Rabbitmq.ErlangInetConfig).To(Equal("{inet6,true}."))
		})

		It("should preserve override.service fields", func() {
			rabbitmq := &rabbitmqv1beta1.RabbitMq{
				ObjectMeta: metav1.ObjectMeta{
					Name:      "test-rabbitmq-override",
					Namespace: "default",
				},
				Spec: rabbitmqv1beta1.RabbitMqSpec{
					RabbitMqSpecCore: rabbitmqv1beta1.RabbitMqSpecCore{
						Override: rabbitmqv1beta1.RabbitMQOverrideSpec{
							Service: &rabbitmqv1beta1.RabbitMQServiceOverride{
								EmbeddedLabelsAnnotations: &service.EmbeddedLabelsAnnotations{
									Annotations: map[string]string{"test-key": "test-value"},
								},
								Spec: &corev1.ServiceSpec{
									Type: corev1.ServiceTypeLoadBalancer,
								},
							},
						},
					},
				},
			}

			rabbitmq.Default(k8sClient)

			Expect(rabbitmq.Spec.Override.Service).NotTo(BeNil())
			Expect(rabbitmq.Spec.Override.Service.Spec.Type).To(Equal(corev1.ServiceTypeLoadBalancer))
			Expect(rabbitmq.Spec.Override.Service.Annotations).To(HaveKeyWithValue("test-key", "test-value"))
		})
	})

	Context("Validation method", func() {
		It("should allow creating new cluster with Quorum", func() {
			newRabbitMq := &rabbitmqv1beta1.RabbitMq{
				ObjectMeta: metav1.ObjectMeta{
					Name:      "test-rabbitmq",
					Namespace: "default",
				},
				Spec: rabbitmqv1beta1.RabbitMqSpec{
					RabbitMqSpecCore: rabbitmqv1beta1.RabbitMqSpecCore{
						QueueType: ptr.To(rabbitmqv1beta1.QueueTypeQuorum),
					},
				},
			}

			warnings, err := newRabbitMq.ValidateCreate()
			Expect(warnings).To(BeEmpty())
			Expect(err).ToNot(HaveOccurred())
		})

		It("should allow creating new cluster with Mirrored", func() {
			newRabbitMq := &rabbitmqv1beta1.RabbitMq{
				ObjectMeta: metav1.ObjectMeta{
					Name:      "test-rabbitmq-mirrored",
					Namespace: "default",
				},
				Spec: rabbitmqv1beta1.RabbitMqSpec{
					RabbitMqSpecCore: rabbitmqv1beta1.RabbitMqSpecCore{
						QueueType: ptr.To(rabbitmqv1beta1.QueueTypeMirrored),
					},
				},
			}

			warnings, err := newRabbitMq.ValidateCreate()
			Expect(warnings).To(BeEmpty())
			Expect(err).ToNot(HaveOccurred())
		})

		It("should reject creating new cluster with invalid QueueType", func() {
			newRabbitMq := &rabbitmqv1beta1.RabbitMq{
				ObjectMeta: metav1.ObjectMeta{
					Name:      "test-rabbitmq-invalid",
					Namespace: "default",
				},
				Spec: rabbitmqv1beta1.RabbitMqSpec{
					RabbitMqSpecCore: rabbitmqv1beta1.RabbitMqSpecCore{
						QueueType: ptr.To(rabbitmqv1beta1.QueueType("Invalid")),
					},
				},
			}

			_, err := newRabbitMq.ValidateCreate()
			Expect(err).To(HaveOccurred())
		})

		It("should allow updating cluster with Quorum", func() {
			existingRabbitMq := &rabbitmqv1beta1.RabbitMq{
				ObjectMeta: metav1.ObjectMeta{
					Name:      "test-rabbitmq",
					Namespace: "default",
				},
				Spec: rabbitmqv1beta1.RabbitMqSpec{
					RabbitMqSpecCore: rabbitmqv1beta1.RabbitMqSpecCore{
						QueueType: ptr.To(rabbitmqv1beta1.QueueTypeQuorum),
					},
				},
			}

			updatedRabbitMq := existingRabbitMq.DeepCopy()
			updatedRabbitMq.Spec.QueueType = ptr.To(rabbitmqv1beta1.QueueTypeQuorum)

			warnings, err := updatedRabbitMq.ValidateUpdate(existingRabbitMq)
			Expect(warnings).To(BeEmpty())
			Expect(err).ToNot(HaveOccurred())
		})

		It("should allow updating cluster from Mirrored to Quorum", func() {
			existingRabbitMq := &rabbitmqv1beta1.RabbitMq{
				ObjectMeta: metav1.ObjectMeta{
					Name:      "test-rabbitmq",
					Namespace: "default",
				},
				Spec: rabbitmqv1beta1.RabbitMqSpec{
					RabbitMqSpecCore: rabbitmqv1beta1.RabbitMqSpecCore{
						QueueType: ptr.To(rabbitmqv1beta1.QueueTypeMirrored),
					},
				},
			}

			updatedRabbitMq := existingRabbitMq.DeepCopy()
			updatedRabbitMq.Spec.QueueType = ptr.To(rabbitmqv1beta1.QueueTypeQuorum)

			warnings, err := updatedRabbitMq.ValidateUpdate(existingRabbitMq)
			Expect(warnings).To(BeEmpty())
			Expect(err).ToNot(HaveOccurred())
		})

		It("should reject version downgrade from 4.2 to 3.9", func() {
			existingRabbitMq := &rabbitmqv1beta1.RabbitMq{
				ObjectMeta: metav1.ObjectMeta{
					Name:      "test-rabbitmq-downgrade",
					Namespace: "default",
				},
				Spec: rabbitmqv1beta1.RabbitMqSpec{
					RabbitMqSpecCore: rabbitmqv1beta1.RabbitMqSpecCore{
						QueueType: ptr.To(rabbitmqv1beta1.QueueTypeQuorum),
					},
				},
				Status: rabbitmqv1beta1.RabbitMqStatus{
					CurrentVersion: "4.2",
				},
			}

			updatedRabbitMq := existingRabbitMq.DeepCopy()
			updatedRabbitMq.Spec.TargetVersion = ptr.To("3.9")

			_, err := updatedRabbitMq.ValidateUpdate(existingRabbitMq)
			Expect(err).To(HaveOccurred())
			Expect(err.Error()).To(ContainSubstring("version downgrades are not supported"))
		})

		It("should reject version downgrade within same major version", func() {
			existingRabbitMq := &rabbitmqv1beta1.RabbitMq{
				ObjectMeta: metav1.ObjectMeta{
					Name:      "test-rabbitmq-minor-downgrade",
					Namespace: "default",
				},
				Spec: rabbitmqv1beta1.RabbitMqSpec{
					RabbitMqSpecCore: rabbitmqv1beta1.RabbitMqSpecCore{
						QueueType: ptr.To(rabbitmqv1beta1.QueueTypeQuorum),
					},
				},
				Status: rabbitmqv1beta1.RabbitMqStatus{
					CurrentVersion: "4.2",
				},
			}

			updatedRabbitMq := existingRabbitMq.DeepCopy()
			updatedRabbitMq.Spec.TargetVersion = ptr.To("4.1")

			_, err := updatedRabbitMq.ValidateUpdate(existingRabbitMq)
			Expect(err).To(HaveOccurred())
			Expect(err.Error()).To(ContainSubstring("version downgrades are not supported"))
		})

		It("should allow version upgrade from 3.9 to 4.2", func() {
			existingRabbitMq := &rabbitmqv1beta1.RabbitMq{
				ObjectMeta: metav1.ObjectMeta{
					Name:      "test-rabbitmq-upgrade",
					Namespace: "default",
				},
				Spec: rabbitmqv1beta1.RabbitMqSpec{
					RabbitMqSpecCore: rabbitmqv1beta1.RabbitMqSpecCore{
						QueueType: ptr.To(rabbitmqv1beta1.QueueTypeQuorum),
					},
				},
				Status: rabbitmqv1beta1.RabbitMqStatus{
					CurrentVersion: "3.9",
				},
			}

			updatedRabbitMq := existingRabbitMq.DeepCopy()
			updatedRabbitMq.Spec.TargetVersion = ptr.To("4.2")

			warnings, err := updatedRabbitMq.ValidateUpdate(existingRabbitMq)
			Expect(warnings).To(BeEmpty())
			Expect(err).ToNot(HaveOccurred())
		})

		It("should allow setting TargetVersion when CurrentVersion is empty", func() {
			existingRabbitMq := &rabbitmqv1beta1.RabbitMq{
				ObjectMeta: metav1.ObjectMeta{
					Name:      "test-rabbitmq-new",
					Namespace: "default",
				},
				Spec: rabbitmqv1beta1.RabbitMqSpec{
					RabbitMqSpecCore: rabbitmqv1beta1.RabbitMqSpecCore{
						QueueType: ptr.To(rabbitmqv1beta1.QueueTypeQuorum),
					},
				},
			}

			updatedRabbitMq := existingRabbitMq.DeepCopy()
			updatedRabbitMq.Spec.TargetVersion = ptr.To("4.2")

			warnings, err := updatedRabbitMq.ValidateUpdate(existingRabbitMq)
			Expect(warnings).To(BeEmpty())
			Expect(err).ToNot(HaveOccurred())
		})

		It("should reject Mirrored queues with 4.x on update (safety net after defaulting)", func() {
			existingRabbitMq := &rabbitmqv1beta1.RabbitMq{
				ObjectMeta: metav1.ObjectMeta{
					Name:      "test-rabbitmq-mirrored-4x",
					Namespace: "default",
				},
				Spec: rabbitmqv1beta1.RabbitMqSpec{
					RabbitMqSpecCore: rabbitmqv1beta1.RabbitMqSpecCore{
						QueueType: ptr.To(rabbitmqv1beta1.QueueTypeMirrored),
					},
				},
				Status: rabbitmqv1beta1.RabbitMqStatus{
					CurrentVersion: "3.9",
				},
			}

			updatedRabbitMq := existingRabbitMq.DeepCopy()
			updatedRabbitMq.Spec.TargetVersion = ptr.To("4.2")
			// Leave QueueType as Mirrored — validation rejects, but defaulting
			// would have already forced it to Quorum in normal flow

			_, err := updatedRabbitMq.ValidateUpdate(existingRabbitMq)
			Expect(err).To(HaveOccurred())
			Expect(err.Error()).To(ContainSubstring("Mirrored queues are not supported"))
		})

		It("should allow 3.x to 4.x upgrade after defaulting forces Quorum", func() {
			existingRabbitMq := &rabbitmqv1beta1.RabbitMq{
				ObjectMeta: metav1.ObjectMeta{
					Name:      "test-rabbitmq-upgrade-flow",
					Namespace: "default",
				},
				Spec: rabbitmqv1beta1.RabbitMqSpec{
					RabbitMqSpecCore: rabbitmqv1beta1.RabbitMqSpecCore{
						QueueType: ptr.To(rabbitmqv1beta1.QueueTypeMirrored),
					},
				},
				Status: rabbitmqv1beta1.RabbitMqStatus{
					CurrentVersion: "3.9",
				},
			}

			// Simulate the full webhook flow: defaulting then validation
			updatedRabbitMq := existingRabbitMq.DeepCopy()
			updatedRabbitMq.Spec.TargetVersion = ptr.To("4.2")
			updatedRabbitMq.Spec.RabbitMqSpecCore.Default(false) // defaulting forces Mirrored → Quorum

			Expect(*updatedRabbitMq.Spec.QueueType).To(Equal(rabbitmqv1beta1.QueueTypeQuorum))

			warnings, err := updatedRabbitMq.ValidateUpdate(existingRabbitMq)
			Expect(warnings).To(BeEmpty())
			Expect(err).ToNot(HaveOccurred())
		})

		It("should allow same version when TargetVersion equals CurrentVersion", func() {
			existingRabbitMq := &rabbitmqv1beta1.RabbitMq{
				ObjectMeta: metav1.ObjectMeta{
					Name:      "test-rabbitmq-same",
					Namespace: "default",
				},
				Spec: rabbitmqv1beta1.RabbitMqSpec{
					RabbitMqSpecCore: rabbitmqv1beta1.RabbitMqSpecCore{
						QueueType:     ptr.To(rabbitmqv1beta1.QueueTypeQuorum),
						TargetVersion: ptr.To("4.2"),
					},
				},
				Status: rabbitmqv1beta1.RabbitMqStatus{
					CurrentVersion: "4.2",
				},
			}

			updatedRabbitMq := existingRabbitMq.DeepCopy()
			updatedRabbitMq.Spec.TargetVersion = ptr.To("4.2")

			warnings, err := updatedRabbitMq.ValidateUpdate(existingRabbitMq)
			Expect(warnings).To(BeEmpty())
			Expect(err).ToNot(HaveOccurred())
		})

		It("should reject scaling down replicas", func() {
			existingRabbitMq := &rabbitmqv1beta1.RabbitMq{
				ObjectMeta: metav1.ObjectMeta{
					Name:      "test-rabbitmq-scaledown",
					Namespace: "default",
				},
				Spec: rabbitmqv1beta1.RabbitMqSpec{
					RabbitMqSpecCore: rabbitmqv1beta1.RabbitMqSpecCore{
						QueueType: ptr.To(rabbitmqv1beta1.QueueTypeQuorum),
						Replicas:  ptr.To(int32(3)),
					},
				},
			}

			updatedRabbitMq := existingRabbitMq.DeepCopy()
			updatedRabbitMq.Spec.Replicas = ptr.To(int32(1))

			_, err := updatedRabbitMq.ValidateUpdate(existingRabbitMq)
			Expect(err).To(HaveOccurred())
			Expect(err.Error()).To(ContainSubstring("scaling down"))
		})

		It("should allow scaling up replicas", func() {
			existingRabbitMq := &rabbitmqv1beta1.RabbitMq{
				ObjectMeta: metav1.ObjectMeta{
					Name:      "test-rabbitmq-scaleup",
					Namespace: "default",
				},
				Spec: rabbitmqv1beta1.RabbitMqSpec{
					RabbitMqSpecCore: rabbitmqv1beta1.RabbitMqSpecCore{
						QueueType: ptr.To(rabbitmqv1beta1.QueueTypeQuorum),
						Replicas:  ptr.To(int32(1)),
					},
				},
			}

			updatedRabbitMq := existingRabbitMq.DeepCopy()
			updatedRabbitMq.Spec.Replicas = ptr.To(int32(3))

			warnings, err := updatedRabbitMq.ValidateUpdate(existingRabbitMq)
			Expect(warnings).To(BeEmpty())
			Expect(err).ToNot(HaveOccurred())
		})

		It("should allow keeping same replica count", func() {
			existingRabbitMq := &rabbitmqv1beta1.RabbitMq{
				ObjectMeta: metav1.ObjectMeta{
					Name:      "test-rabbitmq-same-replicas",
					Namespace: "default",
				},
				Spec: rabbitmqv1beta1.RabbitMqSpec{
					RabbitMqSpecCore: rabbitmqv1beta1.RabbitMqSpecCore{
						QueueType: ptr.To(rabbitmqv1beta1.QueueTypeQuorum),
						Replicas:  ptr.To(int32(3)),
					},
				},
			}

			updatedRabbitMq := existingRabbitMq.DeepCopy()

			warnings, err := updatedRabbitMq.ValidateUpdate(existingRabbitMq)
			Expect(warnings).To(BeEmpty())
			Expect(err).ToNot(HaveOccurred())
		})
	})
})
