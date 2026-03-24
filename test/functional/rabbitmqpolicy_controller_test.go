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
	. "github.com/onsi/ginkgo/v2" //nolint:revive
	. "github.com/onsi/gomega"    //nolint:revive
	rabbitmqv1 "github.com/openstack-k8s-operators/infra-operator/apis/rabbitmq/v1beta1"
	condition "github.com/openstack-k8s-operators/lib-common/modules/common/condition"
	"k8s.io/apimachinery/pkg/types"
)

var _ = Describe("RabbitMQPolicy controller", func() {
	var rabbitmqClusterName types.NamespacedName
	var policyName types.NamespacedName

	BeforeEach(func() {
		rabbitmqClusterName = types.NamespacedName{Name: "rabbitmq", Namespace: namespace}
		policyName = types.NamespacedName{Name: "test-policy", Namespace: namespace}

		CreateRabbitMQCluster(rabbitmqClusterName, GetDefaultRabbitMQClusterSpec(false))
		SimulateRabbitMQClusterReady(rabbitmqClusterName)
		DeferCleanup(DeleteRabbitMQCluster, rabbitmqClusterName)
	})

	// Mark cluster for deletion before cleanup phase to trigger skip-cleanup logic
	AfterEach(func() {
		cluster := &rabbitmqv1.RabbitMq{}
		err := th.K8sClient.Get(th.Ctx, rabbitmqClusterName, cluster)
		if err == nil && cluster.DeletionTimestamp.IsZero() {
			// Cluster exists and not being deleted - mark for deletion
			_ = th.K8sClient.Delete(th.Ctx, cluster)
		}
	})

	When("a RabbitMQPolicy is created", func() {
		BeforeEach(func() {
			spec := map[string]any{
				"rabbitmqClusterName": rabbitmqClusterName.Name,
				"pattern":             ".*",
				"definition": map[string]interface{}{
					"max-length": 10000,
				},
			}
			policy := CreateRabbitMQPolicy(policyName, spec)
			DeferCleanup(th.DeleteInstance, policy)
		})

		It("should have spec fields set", func() {
			policy := GetRabbitMQPolicy(policyName)
			Expect(policy.Spec.RabbitmqClusterName).To(Equal(rabbitmqClusterName.Name))
			Expect(policy.Spec.Pattern).To(Equal(".*"))
			Expect(string(policy.Spec.Definition.Raw)).To(ContainSubstring("max-length"))
			Expect(string(policy.Spec.Definition.Raw)).To(ContainSubstring("10000"))
		})
	})

	When("a RabbitMQPolicy with custom settings is created", func() {
		BeforeEach(func() {
			spec := map[string]any{
				"rabbitmqClusterName": rabbitmqClusterName.Name,
				"pattern":             "^queue.*",
				"definition": map[string]interface{}{
					"max-length": 1000,
					"expires":    3600000,
				},
				"priority": 10,
				"applyTo":  "queues",
			}
			policy := CreateRabbitMQPolicy(policyName, spec)
			DeferCleanup(th.DeleteInstance, policy)
		})

		It("should have custom settings in spec", func() {
			policy := GetRabbitMQPolicy(policyName)
			Expect(policy.Spec.Pattern).To(Equal("^queue.*"))
			Expect(policy.Spec.Priority).To(Equal(10))
			Expect(policy.Spec.ApplyTo).To(Equal("queues"))
			Expect(string(policy.Spec.Definition.Raw)).To(ContainSubstring("max-length"))
			Expect(string(policy.Spec.Definition.Raw)).To(ContainSubstring("1000"))
		})
	})

	When("a RabbitMQPolicy references non-existent cluster", func() {
		var policyBadCluster types.NamespacedName

		BeforeEach(func() {
			policyBadCluster = types.NamespacedName{Name: "bad-cluster-policy", Namespace: namespace}
			spec := map[string]any{
				"rabbitmqClusterName": "non-existent",
				"pattern":             ".*",
				"definition": map[string]interface{}{
					"max-length": 10000,
				},
			}
			policy := CreateRabbitMQPolicy(policyBadCluster, spec)
			DeferCleanup(th.DeleteInstance, policy)
		})

		It("should have spec with non-existent cluster reference", func() {
			policy := GetRabbitMQPolicy(policyBadCluster)
			Expect(policy.Spec.RabbitmqClusterName).To(Equal("non-existent"))
		})
	})

	When("a RabbitMQPolicy is created with mock RabbitMQ API", func() {
		var mockClusterName types.NamespacedName
		var mockVhostName types.NamespacedName
		var mockPolicyName types.NamespacedName

		BeforeEach(func() {
			mockClusterName = types.NamespacedName{Name: "rabbitmq-policy-mock", Namespace: namespace}
			mockVhostName = types.NamespacedName{Name: "vhost-policy-mock", Namespace: namespace}
			mockPolicyName = types.NamespacedName{Name: "policy-mock-test", Namespace: namespace}

			// Set up mock RabbitMQ Management API so controller can make API calls
			SetupMockRabbitMQAPI()
			DeferCleanup(StopMockRabbitMQAPI)

			// Create cluster and mark it ready
			CreateRabbitMQCluster(mockClusterName, GetDefaultRabbitMQClusterSpec(false))
			SimulateRabbitMQClusterReady(mockClusterName)
			DeferCleanup(DeleteRabbitMQCluster, mockClusterName)

			// Create vhost and mark it ready
			vhost := CreateRabbitMQVhost(mockVhostName, map[string]any{
				"rabbitmqClusterName": mockClusterName.Name,
				"name":                "test-vhost",
			})
			DeferCleanup(th.DeleteInstance, vhost)
			SimulateRabbitMQVhostReady(mockVhostName)

			// Create policy
			policy := CreateRabbitMQPolicy(mockPolicyName, map[string]any{
				"rabbitmqClusterName": mockClusterName.Name,
				"vhostRef":            mockVhostName.Name,
				"pattern":             ".*",
				"definition": map[string]interface{}{
					"max-length": 10000,
				},
			})
			DeferCleanup(th.DeleteInstance, policy)
		})

		It("should create policy via RabbitMQ Management API and become ready", func() {
			// Policy should become ready after successfully calling the mock API
			Eventually(func(g Gomega) {
				p := GetRabbitMQPolicy(mockPolicyName)
				g.Expect(p.Status.Conditions.IsTrue(rabbitmqv1.RabbitMQPolicyReadyCondition)).To(BeTrue())
				g.Expect(p.Status.Conditions.IsTrue(condition.ReadyCondition)).To(BeTrue())
			}, timeout, interval).Should(Succeed())
		})

		It("should reconcile policy on every reconciliation loop", func() {
			// Wait for initial ready state
			Eventually(func(g Gomega) {
				p := GetRabbitMQPolicy(mockPolicyName)
				g.Expect(p.Status.Conditions.IsTrue(rabbitmqv1.RabbitMQPolicyReadyCondition)).To(BeTrue())
			}, timeout, interval).Should(Succeed())

			// Update a label to trigger reconciliation
			Eventually(func(g Gomega) {
				p := GetRabbitMQPolicy(mockPolicyName)
				if p.Labels == nil {
					p.Labels = make(map[string]string)
				}
				p.Labels["test-reconcile"] = "trigger"
				g.Expect(th.K8sClient.Update(th.Ctx, p)).To(Succeed())
			}, timeout, interval).Should(Succeed())

			// Policy should remain ready - controller called API again
			Consistently(func(g Gomega) {
				p := GetRabbitMQPolicy(mockPolicyName)
				g.Expect(p.Status.Conditions.IsTrue(rabbitmqv1.RabbitMQPolicyReadyCondition)).To(BeTrue())
				g.Expect(p.Status.Conditions.IsTrue(condition.ReadyCondition)).To(BeTrue())
			}, "3s", interval).Should(Succeed())
		})
	})

	When("a RabbitMQPolicy definition is updated after creation", func() {
		var mockClusterName types.NamespacedName
		var updatePolicyName types.NamespacedName

		BeforeEach(func() {
			mockClusterName = types.NamespacedName{Name: "rabbitmq-policy-update", Namespace: namespace}
			updatePolicyName = types.NamespacedName{Name: "policy-update-test", Namespace: namespace}

			SetupMockRabbitMQAPI()
			DeferCleanup(StopMockRabbitMQAPI)

			CreateRabbitMQCluster(mockClusterName, GetDefaultRabbitMQClusterSpec(false))
			SimulateRabbitMQClusterReady(mockClusterName)
			DeferCleanup(DeleteRabbitMQCluster, mockClusterName)

			policy := CreateRabbitMQPolicy(updatePolicyName, map[string]any{
				"rabbitmqClusterName": mockClusterName.Name,
				"pattern":             ".*",
				"definition": map[string]interface{}{
					"max-length": 10000,
				},
			})
			DeferCleanup(th.DeleteInstance, policy)
		})

		It("should reconcile with updated definition and remain ready", func() {
			// Wait for initial ready state
			Eventually(func(g Gomega) {
				p := GetRabbitMQPolicy(updatePolicyName)
				g.Expect(p.Status.Conditions.IsTrue(condition.ReadyCondition)).To(BeTrue())
			}, timeout, interval).Should(Succeed())

			// Update the policy definition and pattern
			Eventually(func(g Gomega) {
				p := GetRabbitMQPolicy(updatePolicyName)
				p.Spec.Pattern = "^updated-.*"
				p.Spec.Priority = 5
				p.Spec.Definition.Raw = []byte(`{"max-length":5000,"expires":60000}`)
				g.Expect(th.K8sClient.Update(th.Ctx, p)).To(Succeed())
			}, timeout, interval).Should(Succeed())

			// Policy should remain ready after the update
			Eventually(func(g Gomega) {
				p := GetRabbitMQPolicy(updatePolicyName)
				g.Expect(p.Status.Conditions.IsTrue(condition.ReadyCondition)).To(BeTrue())
				g.Expect(p.Spec.Pattern).To(Equal("^updated-.*"))
				g.Expect(p.Spec.Priority).To(Equal(5))
				g.Expect(string(p.Spec.Definition.Raw)).To(ContainSubstring("5000"))
			}, timeout, interval).Should(Succeed())
		})
	})

	When("a RabbitMQPolicy targets a non-default vhost via vhostRef", func() {
		var mockClusterName types.NamespacedName
		var vhostName types.NamespacedName
		var vhostPolicyName types.NamespacedName

		BeforeEach(func() {
			mockClusterName = types.NamespacedName{Name: "rabbitmq-policy-vhost", Namespace: namespace}
			vhostName = types.NamespacedName{Name: "custom-vhost-for-policy", Namespace: namespace}
			vhostPolicyName = types.NamespacedName{Name: "policy-custom-vhost", Namespace: namespace}

			SetupMockRabbitMQAPI()
			DeferCleanup(StopMockRabbitMQAPI)

			CreateRabbitMQCluster(mockClusterName, GetDefaultRabbitMQClusterSpec(false))
			SimulateRabbitMQClusterReady(mockClusterName)
			DeferCleanup(DeleteRabbitMQCluster, mockClusterName)

			// Create vhost with a custom name
			vhost := CreateRabbitMQVhost(vhostName, map[string]any{
				"rabbitmqClusterName": mockClusterName.Name,
				"name":                "my-custom-vhost",
			})
			DeferCleanup(th.DeleteInstance, vhost)
			SimulateRabbitMQVhostReady(vhostName)

			// Create policy targeting this vhost
			policy := CreateRabbitMQPolicy(vhostPolicyName, map[string]any{
				"rabbitmqClusterName": mockClusterName.Name,
				"vhostRef":            vhostName.Name,
				"pattern":             "^queue-.*",
				"applyTo":             "queues",
				"definition": map[string]interface{}{
					"max-length": 500,
				},
			})
			DeferCleanup(th.DeleteInstance, policy)
		})

		It("should become ready using the vhost's actual name", func() {
			Eventually(func(g Gomega) {
				p := GetRabbitMQPolicy(vhostPolicyName)
				g.Expect(p.Status.Conditions.IsTrue(rabbitmqv1.RabbitMQPolicyReadyCondition)).To(BeTrue())
				g.Expect(p.Status.Conditions.IsTrue(condition.ReadyCondition)).To(BeTrue())
			}, timeout, interval).Should(Succeed())

			// Verify spec fields are correct
			p := GetRabbitMQPolicy(vhostPolicyName)
			Expect(p.Spec.VhostRef).To(Equal(vhostName.Name))
			Expect(p.Spec.ApplyTo).To(Equal("queues"))
			Expect(p.Spec.Pattern).To(Equal("^queue-.*"))
		})
	})

	When("a RabbitMQPolicy is created before cluster is ready", func() {
		var pendingClusterName types.NamespacedName
		var pendingPolicyName types.NamespacedName

		BeforeEach(func() {
			pendingClusterName = types.NamespacedName{Name: "rabbitmq-policy-pending", Namespace: namespace}
			pendingPolicyName = types.NamespacedName{Name: "policy-pending-cluster", Namespace: namespace}

			// Create cluster but do NOT mark it ready
			CreateRabbitMQCluster(pendingClusterName, GetDefaultRabbitMQClusterSpec(false))

			policy := CreateRabbitMQPolicy(pendingPolicyName, map[string]any{
				"rabbitmqClusterName": pendingClusterName.Name,
				"pattern":             ".*",
				"definition": map[string]any{
					"max-length": 10000,
				},
			})
			// During cleanup: delete cluster first (runs last in LIFO) so that
			// policy finalizer removal sees cluster gone and skips API cleanup
			DeferCleanup(DeleteRabbitMQCluster, pendingClusterName)
			DeferCleanup(func() {
				// Mark the cluster for deletion before deleting the policy
				// so reconcileDelete sees cluster being deleted and skips cleanup
				cluster := &rabbitmqv1.RabbitMq{}
				err := th.K8sClient.Get(th.Ctx, pendingClusterName, cluster)
				if err == nil && cluster.DeletionTimestamp.IsZero() {
					_ = th.K8sClient.Delete(th.Ctx, cluster)
				}
				th.DeleteInstance(policy)
			})
		})

		It("should wait for cluster readiness then become ready", func() {
			// Policy should not be ready yet - cluster is not ready
			Eventually(func(g Gomega) {
				p := GetRabbitMQPolicy(pendingPolicyName)
				g.Expect(p.Status.Conditions.IsTrue(condition.ReadyCondition)).To(BeFalse())
			}, timeout, interval).Should(Succeed())

			// Now set up mock API and mark cluster ready
			SetupMockRabbitMQAPI()
			DeferCleanup(StopMockRabbitMQAPI)
			SimulateRabbitMQClusterReady(pendingClusterName)

			// Policy should become ready
			Eventually(func(g Gomega) {
				p := GetRabbitMQPolicy(pendingPolicyName)
				g.Expect(p.Status.Conditions.IsTrue(rabbitmqv1.RabbitMQPolicyReadyCondition)).To(BeTrue())
				g.Expect(p.Status.Conditions.IsTrue(condition.ReadyCondition)).To(BeTrue())
			}, timeout, interval).Should(Succeed())
		})
	})

	When("a RabbitMQPolicy is deleted while cluster is being deleted", func() {
		var policyWithDeletingCluster types.NamespacedName
		var deletingClusterName types.NamespacedName

		BeforeEach(func() {
			deletingClusterName = types.NamespacedName{Name: "deleting-rabbitmq", Namespace: namespace}
			policyWithDeletingCluster = types.NamespacedName{Name: "policy-deleting-cluster", Namespace: namespace}

			// Create a separate cluster for this test
			CreateRabbitMQCluster(deletingClusterName, GetDefaultRabbitMQClusterSpec(false))
			SimulateRabbitMQClusterReady(deletingClusterName)

			// Create policy
			spec := map[string]any{
				"rabbitmqClusterName": deletingClusterName.Name,
				"pattern":             ".*",
				"definition": map[string]interface{}{
					"max-length": 10000,
				},
			}
			policy := CreateRabbitMQPolicy(policyWithDeletingCluster, spec)
			DeferCleanup(th.DeleteInstance, policy)

			// Wait for policy to have finalizer
			Eventually(func(g Gomega) {
				p := GetRabbitMQPolicy(policyWithDeletingCluster)
				g.Expect(p.Finalizers).NotTo(BeEmpty())
			}, timeout, interval).Should(Succeed())
		})

		It("should allow deletion without cleanup when cluster is being deleted", func() {
			// Delete policy first
			policy := GetRabbitMQPolicy(policyWithDeletingCluster)
			Expect(th.K8sClient.Delete(th.Ctx, policy)).To(Succeed())

			// Now mark cluster for deletion
			DeleteRabbitMQCluster(deletingClusterName)

			// Policy should be deleted without attempting cleanup
			Eventually(func(g Gomega) {
				p := &rabbitmqv1.RabbitMQPolicy{}
				err := th.K8sClient.Get(th.Ctx, policyWithDeletingCluster, p)
				g.Expect(err).To(HaveOccurred())
			}, timeout, interval).Should(Succeed())
		})
	})
})
