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
			Expect(*rabbitmq.Spec.QueueType).To(Equal("Quorum"))
		})

		It("should preserve explicitly set QueueType=Quorum", func() {
			rabbitmq := &rabbitmqv1beta1.RabbitMq{
				ObjectMeta: metav1.ObjectMeta{
					Name:      "test-rabbitmq-quorum",
					Namespace: "default",
				},
				Spec: rabbitmqv1beta1.RabbitMqSpec{
					RabbitMqSpecCore: rabbitmqv1beta1.RabbitMqSpecCore{
						QueueType: ptr.To("Quorum"),
					},
				},
			}

			rabbitmq.Default(k8sClient)

			Expect(rabbitmq.Spec.QueueType).NotTo(BeNil())
			Expect(*rabbitmq.Spec.QueueType).To(Equal("Quorum"))
		})

		It("should preserve explicitly set QueueType=Mirrored", func() {
			rabbitmq := &rabbitmqv1beta1.RabbitMq{
				ObjectMeta: metav1.ObjectMeta{
					Name:      "test-rabbitmq-mirrored",
					Namespace: "default",
				},
				Spec: rabbitmqv1beta1.RabbitMqSpec{
					RabbitMqSpecCore: rabbitmqv1beta1.RabbitMqSpecCore{
						QueueType: ptr.To("Mirrored"),
					},
				},
			}

			rabbitmq.Default(k8sClient)

			Expect(rabbitmq.Spec.QueueType).NotTo(BeNil())
			Expect(*rabbitmq.Spec.QueueType).To(Equal("Mirrored"))
		})

		It("should enforce Quorum when TargetVersion is 4.x", func() {
			rabbitmq := &rabbitmqv1beta1.RabbitMq{
				ObjectMeta: metav1.ObjectMeta{
					Name:      "test-rabbitmq-4x",
					Namespace: "default",
				},
				Spec: rabbitmqv1beta1.RabbitMqSpec{
					RabbitMqSpecCore: rabbitmqv1beta1.RabbitMqSpecCore{
						QueueType:     ptr.To("Mirrored"),
						TargetVersion: ptr.To("4.2"),
					},
				},
			}

			rabbitmq.Default(k8sClient)

			Expect(rabbitmq.Spec.QueueType).NotTo(BeNil())
			Expect(*rabbitmq.Spec.QueueType).To(Equal("Quorum"))
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
			Expect(*rabbitmq.Spec.QueueType).To(Equal("Quorum"))
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
						QueueType: ptr.To("Quorum"),
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
						QueueType: ptr.To("Mirrored"),
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
						QueueType: ptr.To("Invalid"),
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
						QueueType: ptr.To("Quorum"),
					},
				},
			}

			updatedRabbitMq := existingRabbitMq.DeepCopy()
			updatedRabbitMq.Spec.QueueType = ptr.To("Quorum")

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
						QueueType: ptr.To("Mirrored"),
					},
				},
			}

			updatedRabbitMq := existingRabbitMq.DeepCopy()
			updatedRabbitMq.Spec.QueueType = ptr.To("Quorum")

			warnings, err := updatedRabbitMq.ValidateUpdate(existingRabbitMq)
			Expect(warnings).To(BeEmpty())
			Expect(err).ToNot(HaveOccurred())
		})
	})
})
