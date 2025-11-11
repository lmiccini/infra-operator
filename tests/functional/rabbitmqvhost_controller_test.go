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

package functional_test

import (
	. "github.com/onsi/ginkgo/v2"
	. "github.com/onsi/gomega"
	rabbitmqv1 "github.com/openstack-k8s-operators/infra-operator/apis/rabbitmq/v1beta1"
	"k8s.io/apimachinery/pkg/types"
)

var _ = Describe("RabbitMQVhost controller", func() {
	var rabbitmqClusterName types.NamespacedName
	var vhostName types.NamespacedName

	BeforeEach(func() {
		rabbitmqClusterName = types.NamespacedName{Name: "rabbitmq", Namespace: namespace}
		vhostName = types.NamespacedName{Name: "test-vhost", Namespace: namespace}

		CreateRabbitMQCluster(rabbitmqClusterName, GetDefaultRabbitMQClusterSpec(false))
		DeferCleanup(DeleteRabbitMQCluster, rabbitmqClusterName)
		SimulateRabbitMQClusterReady(rabbitmqClusterName)
	})

	When("a RabbitMQVhost is created", func() {
		BeforeEach(func() {
			spec := map[string]any{
				"rabbitmqClusterName": rabbitmqClusterName.Name,
				"name":                "test",
			}
			vhost := CreateRabbitMQVhost(vhostName, spec)
			DeferCleanup(th.DeleteInstance, vhost)
		})

		It("should have spec fields set", func() {
			vhost := GetRabbitMQVhost(vhostName)
			Expect(vhost.Spec.RabbitmqClusterName).To(Equal(rabbitmqClusterName.Name))
			Expect(vhost.Spec.Name).To(Equal("test"))
		})

		It("should have initialized conditions", func() {
			Eventually(func(g Gomega) {
				vhost := GetRabbitMQVhost(vhostName)
				g.Expect(vhost.Status.Conditions).NotTo(BeNil())
				g.Expect(vhost.Status.Conditions.Has(rabbitmqv1.VhostReadyCondition)).To(BeTrue())
			}, timeout, interval).Should(Succeed())
		})
	})

	When("a RabbitMQVhost with default name is created", func() {
		BeforeEach(func() {
			spec := map[string]any{
				"rabbitmqClusterName": rabbitmqClusterName.Name,
			}
			vhost := CreateRabbitMQVhost(vhostName, spec)
			DeferCleanup(th.DeleteInstance, vhost)
		})

		It("should have default vhost name '/'", func() {
			vhost := GetRabbitMQVhost(vhostName)
			Expect(vhost.Spec.Name).To(Equal("/"))
		})
	})

	When("a RabbitMQVhost references non-existent cluster", func() {
		var vhostBadCluster types.NamespacedName

		BeforeEach(func() {
			vhostBadCluster = types.NamespacedName{Name: "bad-cluster-vhost", Namespace: namespace}
			spec := map[string]any{
				"rabbitmqClusterName": "non-existent",
				"name":                "test",
			}
			vhost := CreateRabbitMQVhost(vhostBadCluster, spec)
			DeferCleanup(th.DeleteInstance, vhost)
		})

		It("should have spec with non-existent cluster reference", func() {
			vhost := GetRabbitMQVhost(vhostBadCluster)
			Expect(vhost.Spec.RabbitmqClusterName).To(Equal("non-existent"))
		})
	})
})
