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

var _ = Describe("RabbitMQUser controller", func() {
	var rabbitmqClusterName types.NamespacedName
	var vhostName types.NamespacedName
	var userName types.NamespacedName

	BeforeEach(func() {
		rabbitmqClusterName = types.NamespacedName{Name: "rabbitmq", Namespace: namespace}
		vhostName = types.NamespacedName{Name: "test-vhost", Namespace: namespace}
		userName = types.NamespacedName{Name: "test-user", Namespace: namespace}

		CreateRabbitMQCluster(rabbitmqClusterName, GetDefaultRabbitMQClusterSpec(false))
		DeferCleanup(DeleteRabbitMQCluster, rabbitmqClusterName)
		SimulateRabbitMQClusterReady(rabbitmqClusterName)

		vhost := CreateRabbitMQVhost(vhostName, map[string]any{
			"rabbitmqClusterName": rabbitmqClusterName.Name,
			"name":                "test",
		})
		DeferCleanup(th.DeleteInstance, vhost)
	})

	When("a RabbitMQUser is created", func() {
		BeforeEach(func() {
			spec := map[string]any{
				"rabbitmqClusterName": rabbitmqClusterName.Name,
				"vhostRef":            vhostName.Name,
			}
			user := CreateRabbitMQUser(userName, spec)
			DeferCleanup(th.DeleteInstance, user)
		})

		It("should have spec fields set", func() {
			user := GetRabbitMQUser(userName)
			Expect(user.Spec.RabbitmqClusterName).To(Equal(rabbitmqClusterName.Name))
			Expect(user.Spec.VhostRef).To(Equal(vhostName.Name))
		})

		It("should have initialized conditions", func() {
			Eventually(func(g Gomega) {
				user := GetRabbitMQUser(userName)
				g.Expect(user.Status.Conditions).NotTo(BeNil())
				g.Expect(user.Status.Conditions.Has(rabbitmqv1.UserReadyCondition)).To(BeTrue())
			}, timeout, interval).Should(Succeed())
		})
	})

	When("a RabbitMQUser with custom username is created", func() {
		BeforeEach(func() {
			spec := map[string]any{
				"rabbitmqClusterName": rabbitmqClusterName.Name,
				"vhostRef":            vhostName.Name,
				"username":            "custom-user",
			}
			user := CreateRabbitMQUser(userName, spec)
			DeferCleanup(th.DeleteInstance, user)
		})

		It("should have custom username in spec", func() {
			user := GetRabbitMQUser(userName)
			Expect(user.Spec.Username).To(Equal("custom-user"))
		})
	})

	When("a RabbitMQUser with custom permissions is created", func() {
		BeforeEach(func() {
			spec := map[string]any{
				"rabbitmqClusterName": rabbitmqClusterName.Name,
				"vhostRef":            vhostName.Name,
				"permissions": map[string]any{
					"configure": "^queue.*",
					"write":     "^queue.*",
					"read":      ".*",
				},
			}
			user := CreateRabbitMQUser(userName, spec)
			DeferCleanup(th.DeleteInstance, user)
		})

		It("should have custom permissions in spec", func() {
			user := GetRabbitMQUser(userName)
			Expect(user.Spec.Permissions.Configure).To(Equal("^queue.*"))
			Expect(user.Spec.Permissions.Write).To(Equal("^queue.*"))
			Expect(user.Spec.Permissions.Read).To(Equal(".*"))
		})
	})

	When("a RabbitMQUser references non-existent vhost", func() {
		var userWithBadVhost types.NamespacedName

		BeforeEach(func() {
			userWithBadVhost = types.NamespacedName{Name: "bad-vhost-user", Namespace: namespace}
			spec := map[string]any{
				"rabbitmqClusterName": rabbitmqClusterName.Name,
				"vhostRef":            "non-existent",
			}
			user := CreateRabbitMQUser(userWithBadVhost, spec)
			DeferCleanup(th.DeleteInstance, user)
		})

		It("should have spec with non-existent vhost reference", func() {
			user := GetRabbitMQUser(userWithBadVhost)
			Expect(user.Spec.VhostRef).To(Equal("non-existent"))
		})
	})
})
