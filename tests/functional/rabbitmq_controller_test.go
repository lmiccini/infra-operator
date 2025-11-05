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
	"encoding/json"
	"fmt"
	"strings"

	. "github.com/onsi/ginkgo/v2" //revive:disable:dot-imports
	. "github.com/onsi/gomega"    //revive:disable:dot-imports
	rabbitmqv1 "github.com/openstack-k8s-operators/infra-operator/apis/rabbitmq/v1beta1"
	condition "github.com/openstack-k8s-operators/lib-common/modules/common/condition"

	//revive:disable-next-line:dot-imports

	//. "github.com/openstack-k8s-operators/lib-common/modules/common/test/helpers"
	appsv1 "k8s.io/api/apps/v1"
	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
)

const (
	rabbitmqDefaultName = "rabbitmq"
)

var _ = Describe("RabbitMQ Controller", func() {
	var rabbitmqName types.NamespacedName

	BeforeEach(func() {
		rabbitmqName = types.NamespacedName{
			Name:      rabbitmqDefaultName,
			Namespace: namespace,
		}
		clusterCm := types.NamespacedName{Name: "cluster-config-v1", Namespace: "kube-system"}
		th.CreateConfigMap(
			clusterCm,
			map[string]any{
				"install-config": "fips: false",
			},
		)
		DeferCleanup(th.DeleteConfigMap, clusterCm)
	})

	When("a default RabbitMQ gets created", func() {
		BeforeEach(func() {
			rabbitmq := CreateRabbitMQ(rabbitmqName, GetDefaultRabbitMQSpec())
			DeferCleanup(th.DeleteInstance, rabbitmq)
		})

		It("should have created a RabbitMQCluster", func() {
			SimulateRabbitMQClusterReady(rabbitmqName)
			Eventually(func(g Gomega) {
				cluster := GetRabbitMQCluster(rabbitmqName)
				g.Expect(*cluster.Spec.Replicas).To(Equal(int32(1)))
				g.Expect(cluster.Spec.TLS.SecretName).To(BeEmpty())
				g.Expect(cluster.Spec.TLS.CaSecretName).To(BeEmpty())
				g.Expect(cluster.Spec.TLS.DisableNonTLSListeners).To(BeFalse())

				container := cluster.Spec.Override.StatefulSet.Spec.Template.Spec.Containers[0]
				var rabbitmqServerAdditionalErlArgs string
				for _, env := range container.Env {
					if env.Name == "RABBITMQ_SERVER_ADDITIONAL_ERL_ARGS" {
						rabbitmqServerAdditionalErlArgs = env.Value
						break
					}
				}
				g.Expect(rabbitmqServerAdditionalErlArgs).To(ContainSubstring("-proto_dist inet_tcp"))
				g.Expect(cluster.Spec.Rabbitmq.AdditionalConfig).To(ContainSubstring("prometheus.tcp.ip = ::"))
			}, timeout, interval).Should(Succeed())

		})
	})

	When("RabbitMQ gets created with TLS enabled", func() {
		var certSecret *corev1.Secret
		BeforeEach(func() {
			certSecret = CreateCertSecret(rabbitmqName)
			DeferCleanup(th.DeleteSecret, types.NamespacedName{Name: certSecret.Name, Namespace: namespace})
			spec := GetDefaultRabbitMQSpec()
			spec["tls"] = map[string]any{
				"secretName": certSecret.Name,
			}
			rabbitmq := CreateRabbitMQ(rabbitmqName, spec)
			DeferCleanup(th.DeleteInstance, rabbitmq)
		})

		It("should have created a RabbitMQCluster with TLS enabled", func() {
			SimulateRabbitMQClusterReady(rabbitmqName)
			Eventually(func(g Gomega) {
				cluster := GetRabbitMQCluster(rabbitmqName)
				g.Expect(*cluster.Spec.Replicas).To(Equal(int32(1)))
				g.Expect(cluster.Spec.TLS.SecretName).To(Equal(certSecret.Name))
				g.Expect(cluster.Spec.TLS.CaSecretName).To(Equal(certSecret.Name))
				g.Expect(cluster.Spec.TLS.DisableNonTLSListeners).To(BeTrue())
				g.Expect(cluster.Spec.Rabbitmq.AdvancedConfig).To(ContainSubstring("ssl_options"))
				g.Expect(cluster.Spec.Rabbitmq.AdditionalConfig).To(ContainSubstring("prometheus.ssl.ip = ::"))

				container := cluster.Spec.Override.StatefulSet.Spec.Template.Spec.Containers[0]
				var rabbitmqServerAdditionalErlArgs string
				for _, env := range container.Env {
					if env.Name == "RABBITMQ_SERVER_ADDITIONAL_ERL_ARGS" {
						rabbitmqServerAdditionalErlArgs = env.Value
						break
					}
				}
				g.Expect(rabbitmqServerAdditionalErlArgs).NotTo(ContainSubstring("-crypto fips_mode true"))
				g.Expect(rabbitmqServerAdditionalErlArgs).To(ContainSubstring("-proto_dist inet_tls"))
				g.Expect(rabbitmqServerAdditionalErlArgs).To(ContainSubstring("-ssl_dist_optfile /etc/rabbitmq/inter-node-tls.config"))
			}, timeout, interval).Should(Succeed())
		})

		It("should configure TLS 1.2 only for non-FIPS mode", func() {
			SimulateRabbitMQClusterReady(rabbitmqName)
			Eventually(func(g Gomega) {
				// TLS settings for Erlang and RabbitMQ endpoints (AdvancedConfig)
				// are in an Erlang data structure, so just parse the expected strings
				// for application "rabbit", "rabbitmq_management", "client" and "ssl"
				cluster := GetRabbitMQCluster(rabbitmqName)
				advancedConfig := cluster.Spec.Rabbitmq.AdvancedConfig

				g.Expect(advancedConfig).To(ContainSubstring("{ssl, [{protocol_version, ['tlsv1.2']}"))
				g.Expect(advancedConfig).To(ContainSubstring("{rabbit, ["))
				g.Expect(advancedConfig).To(ContainSubstring("{rabbitmq_management, ["))
				g.Expect(advancedConfig).To(ContainSubstring("{client, ["))
				g.Expect(strings.Count(advancedConfig, "{versions, ['tlsv1.2']}")).To(Equal(3))
				// Ensure it doesn't use TLS1.3 (as FIPS still does for the time being)
				g.Expect(strings.Count(advancedConfig, "'tlsv1.3'")).To(Equal(0))

				// TLS settings for RabbitMQ cluster communication (inter-node-tls.config)
				// should have a config for server and client. Those are in an Erlang
				// data structure, so just parse the expected string
				configMapName := types.NamespacedName{
					Name:      fmt.Sprintf("%s-config-data", rabbitmqName.Name),
					Namespace: rabbitmqName.Namespace,
				}
				cm := th.GetConfigMap(configMapName)
				g.Expect(cm.Data).To(HaveKey("inter_node_tls.config"))
				interNodeConfig := cm.Data["inter_node_tls.config"]

				// Verify server and client configurations use TLS 1.2 only
				g.Expect(interNodeConfig).To(ContainSubstring("{server, ["))
				g.Expect(interNodeConfig).To(ContainSubstring("{client, ["))
				g.Expect(strings.Count(interNodeConfig, "{versions, ['tlsv1.2']}")).To(Equal(2))
				// Ensure it doesn't use TLS1.3 (as FIPS still does for the time being)
				g.Expect(strings.Count(interNodeConfig, "'tlsv1.3'")).To(Equal(0))
			}, timeout, interval).Should(Succeed())
		})
	})

	When("RabbitMQ gets created with FIPS enabled", func() {
		var certSecret *corev1.Secret
		BeforeEach(func() {
			clusterCm := types.NamespacedName{Name: "cluster-config-v1", Namespace: "kube-system"}
			cm := th.GetConfigMap(clusterCm)
			cm.Data["install-config"] = "fips: true"
			err := th.K8sClient.Update(ctx, cm)
			Expect(err).To(Succeed())
			certSecret = CreateCertSecret(rabbitmqName)
			DeferCleanup(th.DeleteSecret, types.NamespacedName{Name: certSecret.Name, Namespace: namespace})
			spec := GetDefaultRabbitMQSpec()
			spec["tls"] = map[string]any{
				"secretName": certSecret.Name,
			}
			rabbitmq := CreateRabbitMQ(rabbitmqName, spec)
			DeferCleanup(th.DeleteInstance, rabbitmq)
		})

		It("should have created a RabbitMQCluster with FIPS enabled", func() {
			SimulateRabbitMQClusterReady(rabbitmqName)
			Eventually(func(g Gomega) {
				cluster := GetRabbitMQCluster(rabbitmqName)
				g.Expect(*cluster.Spec.Replicas).To(Equal(int32(1)))
				g.Expect(cluster.Spec.TLS.SecretName).To(Equal(certSecret.Name))
				g.Expect(cluster.Spec.TLS.CaSecretName).To(Equal(certSecret.Name))
				g.Expect(cluster.Spec.TLS.DisableNonTLSListeners).To(BeTrue())
				g.Expect(cluster.Spec.Rabbitmq.AdvancedConfig).To(ContainSubstring("ssl_options"))
				g.Expect(cluster.Spec.Rabbitmq.AdditionalConfig).To(ContainSubstring("prometheus.ssl.ip = ::"))

				container := cluster.Spec.Override.StatefulSet.Spec.Template.Spec.Containers[0]
				var rabbitmqServerAdditionalErlArgs string
				for _, env := range container.Env {
					if env.Name == "RABBITMQ_SERVER_ADDITIONAL_ERL_ARGS" {
						rabbitmqServerAdditionalErlArgs = env.Value
						break
					}
				}
				g.Expect(rabbitmqServerAdditionalErlArgs).To(ContainSubstring("-crypto fips_mode true"))
				g.Expect(rabbitmqServerAdditionalErlArgs).To(ContainSubstring("-proto_dist inet_tls"))
				g.Expect(rabbitmqServerAdditionalErlArgs).To(ContainSubstring("-ssl_dist_optfile /etc/rabbitmq/inter-node-tls.config"))
			}, timeout, interval).Should(Succeed())
		})

		It("should configure TLS 1.2 and 1.3 for FIPS mode", func() {
			SimulateRabbitMQClusterReady(rabbitmqName)
			Eventually(func(g Gomega) {
				// TLS settings for Erlang and RabbitMQ endpoints (AdvancedConfig)
				// are in an Erlang data structure, so just parse the expected strings
				// for application "rabbit", "rabbitmq_management", "client" and "ssl"
				cluster := GetRabbitMQCluster(rabbitmqName)
				advancedConfig := cluster.Spec.Rabbitmq.AdvancedConfig

				g.Expect(advancedConfig).To(ContainSubstring("{ssl, [{protocol_version, ['tlsv1.2','tlsv1.3']}"))
				g.Expect(advancedConfig).To(ContainSubstring("{rabbit, ["))
				g.Expect(advancedConfig).To(ContainSubstring("{rabbitmq_management, ["))
				g.Expect(advancedConfig).To(ContainSubstring("{client, ["))
				g.Expect(strings.Count(advancedConfig, "{versions, ['tlsv1.2','tlsv1.3']}")).To(Equal(3))

				// TLS settings for RabbitMQ cluster communication (inter-node-tls.config)
				// should have a config for server and client. Those are in an Erlang
				// data structure, so just parse the expected string
				configMapName := types.NamespacedName{
					Name:      fmt.Sprintf("%s-config-data", rabbitmqName.Name),
					Namespace: rabbitmqName.Namespace,
				}
				cm := th.GetConfigMap(configMapName)
				g.Expect(cm.Data).To(HaveKey("inter_node_tls.config"))
				interNodeConfig := cm.Data["inter_node_tls.config"]

				// Verify server and client configurations use TLS 1.2 only
				g.Expect(interNodeConfig).To(ContainSubstring("{server, ["))
				g.Expect(interNodeConfig).To(ContainSubstring("{client, ["))
				g.Expect(strings.Count(interNodeConfig, "{versions, ['tlsv1.2','tlsv1.3']}")).To(Equal(2))
			}, timeout, interval).Should(Succeed())
		})
	})

	When("RabbitMQ TLS input validation", func() {
		It("should set TLSInputReadyCondition to true when valid TLS secret is provided", func() {
			certSecret := CreateCertSecret(rabbitmqName)
			DeferCleanup(th.DeleteSecret, types.NamespacedName{Name: certSecret.Name, Namespace: namespace})

			spec := GetDefaultRabbitMQSpec()
			spec["tls"] = map[string]any{
				"secretName": certSecret.Name,
			}
			rabbitmq := CreateRabbitMQ(rabbitmqName, spec)
			DeferCleanup(th.DeleteInstance, rabbitmq)

			Eventually(func(g Gomega) {
				instance := GetRabbitMQ(rabbitmqName)
				g.Expect(instance.Status.Conditions.Has(condition.TLSInputReadyCondition)).To(BeTrue())
				tlsCondition := instance.Status.Conditions.Get(condition.TLSInputReadyCondition)
				g.Expect(tlsCondition.Status).To(Equal(corev1.ConditionTrue))
				g.Expect(tlsCondition.Message).To(Equal(condition.InputReadyMessage))
			}, timeout, interval).Should(Succeed())
		})

		It("should set TLSInputReadyCondition to false when TLS secret is missing", func() {
			spec := GetDefaultRabbitMQSpec()
			spec["tls"] = map[string]any{
				"secretName": "non-existent-secret",
			}
			rabbitmq := CreateRabbitMQ(rabbitmqName, spec)
			DeferCleanup(th.DeleteInstance, rabbitmq)

			Eventually(func(g Gomega) {
				instance := GetRabbitMQ(rabbitmqName)
				g.Expect(instance.Status.Conditions.Has(condition.TLSInputReadyCondition)).To(BeTrue())
				tlsCondition := instance.Status.Conditions.Get(condition.TLSInputReadyCondition)
				g.Expect(tlsCondition.Status).To(Equal(corev1.ConditionFalse))
				g.Expect(string(tlsCondition.Reason)).To(Equal(string(condition.RequestedReason)))
			}, timeout, interval).Should(Succeed())
		})
	})

	When("RabbitMQ gets created with a complete statefulset override", func() {
		BeforeEach(func() {
			spec := GetDefaultRabbitMQSpec()
			spec["override"] = map[string]any{
				"statefulSet": map[string]any{
					"spec": map[string]any{
						"replicas": 3,
						"template": map[string]any{
							"spec": map[string]any{
								"containers": []any{
									map[string]any{
										"name": "foobar",
									},
								},
							},
						},
					},
				},
			}
			rabbitmq := CreateRabbitMQ(rabbitmqName, spec)
			DeferCleanup(th.DeleteInstance, rabbitmq)
		})

		It("should have created a RabbitMQCluster", func() {
			SimulateRabbitMQClusterReady(rabbitmqName)
			Eventually(func(g Gomega) {
				cluster := GetRabbitMQCluster(rabbitmqName)
				g.Expect(*cluster.Spec.Replicas).To(Equal(int32(1)))
				g.Expect(*cluster.Spec.Override.StatefulSet.Spec.Replicas).To(Equal(int32(3)))
				g.Expect(cluster.Spec.Override.StatefulSet.Spec.Template.Spec.Containers[0].Name).To(Equal("foobar"))
			}, timeout, interval).Should(Succeed())

		})
	})

	When("RabbitMQ gets created with a partial statefulset override", func() {
		BeforeEach(func() {
			spec := GetDefaultRabbitMQSpec()
			spec["override"] = map[string]any{
				"statefulSet": map[string]any{
					"spec": map[string]any{
						"replicas": 3,
					},
				},
			}
			rabbitmq := CreateRabbitMQ(rabbitmqName, spec)
			DeferCleanup(th.DeleteInstance, rabbitmq)
		})

		It("should have created a RabbitMQCluster", func() {
			SimulateRabbitMQClusterReady(rabbitmqName)
			Eventually(func(g Gomega) {
				cluster := GetRabbitMQCluster(rabbitmqName)
				g.Expect(*cluster.Spec.Replicas).To(Equal(int32(1)))
				g.Expect(*cluster.Spec.Override.StatefulSet.Spec.Replicas).To(Equal(int32(3)))
				g.Expect(cluster.Spec.Override.StatefulSet.Spec.Template.Spec.Containers[0].Name).To(Equal(rabbitmqDefaultName))
			}, timeout, interval).Should(Succeed())

		})
	})

	When("RabbitMQ gets updated with an invalid statefulset override", func() {
		BeforeEach(func() {
			spec := GetDefaultRabbitMQSpec()
			spec["override"] = map[string]any{
				"statefulSet": map[string]any{
					"spec": map[string]any{
						"replicas": 3,
					},
				},
			}
			rabbitmq := CreateRabbitMQ(rabbitmqName, spec)
			DeferCleanup(th.DeleteInstance, rabbitmq)
		})

		It("gets blocked by the webhook and fail", func() {
			instance := &rabbitmqv1.RabbitMq{}

			Eventually(func(g Gomega) {
				g.Expect(k8sClient.Get(ctx, rabbitmqName, instance)).Should(Succeed())
				data, _ := json.Marshal(map[string]any{
					"wrong": "type",
				})
				instance.Spec.Override.StatefulSet.Raw = data
				err := th.K8sClient.Update(ctx, instance)
				g.Expect(err.Error()).Should(ContainSubstring("invalid spec override"))
			}, timeout, interval).Should(Succeed())
		})
	})

	When("RabbitMQ gets created with a service override", func() {
		BeforeEach(func() {
			spec := GetDefaultRabbitMQSpec()
			spec["override"] = map[string]any{
				"service": map[string]any{
					"metadata": map[string]any{
						"annotations": map[string]any{
							"metallb.universe.tf/address-pool":    "internalapi",
							"metallb.universe.tf/loadBalancerIPs": "192.0.2.1",
						},
					},
					"spec": map[string]any{
						"type": "LoadBalancer",
					},
				},
			}
			rabbitmq := CreateRabbitMQ(rabbitmqName, spec)
			DeferCleanup(th.DeleteInstance, rabbitmq)
		})

		It("should have created a RabbitMQCluster with the correct service annotations", func() {
			SimulateRabbitMQClusterReady(rabbitmqName)
			Eventually(func(g Gomega) {
				cluster := GetRabbitMQCluster(rabbitmqName)
				g.Expect(cluster.Spec.Override.Service.Annotations["metallb.universe.tf/address-pool"]).To(Equal("internalapi"))
				g.Expect(cluster.Spec.Override.Service.Annotations["metallb.universe.tf/loadBalancerIPs"]).To(Equal("192.0.2.1"))
				g.Expect(cluster.Spec.Override.Service.Annotations["dnsmasq.network.openstack.org/hostname"]).To(Equal(fmt.Sprintf("%s.%s.svc", rabbitmqDefaultName, namespace)))
				g.Expect(cluster.Spec.Override.Service.Spec.Type).To(Equal(corev1.ServiceTypeLoadBalancer))
			}, timeout, interval).Should(Succeed())
		})
	})

	When("RabbitMQ version upgrade", func() {
		When("RabbitMQ is created without version labels", func() {
			BeforeEach(func() {
				rabbitmq := CreateRabbitMQ(rabbitmqName, GetDefaultRabbitMQSpec())
				DeferCleanup(th.DeleteInstance, rabbitmq)
			})

			It("should default rabbitmqcurrentversion to 3.9", func() {
				Eventually(func(g Gomega) {
					instance := GetRabbitMQ(rabbitmqName)
					g.Expect(instance.Labels).NotTo(BeNil())
					g.Expect(instance.Labels["rabbitmqcurrentversion"]).To(Equal("3.9"))
				}, timeout, interval).Should(Succeed())
			})
		})

		When("RabbitMQ version label changes from 3.9 to 3.13", func() {
			BeforeEach(func() {
				spec := GetDefaultRabbitMQSpec()
				spec["queueType"] = "Quorum" // Avoid policy application that requires real pods
				rabbitmq := CreateRabbitMQ(rabbitmqName, spec)
				DeferCleanup(th.DeleteInstance, rabbitmq)

				// Wait for default version to be set
				Eventually(func(g Gomega) {
					instance := GetRabbitMQ(rabbitmqName)
					g.Expect(instance.Labels["rabbitmqcurrentversion"]).To(Equal("3.9"))
				}, timeout, interval).Should(Succeed())
			})

			It("should detect version mismatch and start upgrade", func() {
				// Update rabbitmqversion label to trigger upgrade
				instance := GetRabbitMQ(rabbitmqName)
				if instance.Labels == nil {
					instance.Labels = make(map[string]string)
				}
				instance.Labels["rabbitmqversion"] = "3.13"
				Expect(k8sClient.Update(ctx, instance)).Should(Succeed())

				// Verify upgrade in progress status is set
				Eventually(func(g Gomega) {
					updatedInstance := GetRabbitMQ(rabbitmqName)
					g.Expect(updatedInstance.Status.VersionUpgradeInProgress).To(Equal("3.13"))
					g.Expect(updatedInstance.Labels["rabbitmqcurrentversion"]).To(Equal("3.9"))
					g.Expect(updatedInstance.Labels["rabbitmqversion"]).To(Equal("3.13"))
				}, timeout, interval).Should(Succeed())
			})
		})

		When("Version upgrade patches StatefulSet", func() {
			BeforeEach(func() {
				spec := GetDefaultRabbitMQSpec()
				spec["containerImage"] = "quay.io/rabbitmq/rabbitmq:3.13"
				spec["queueType"] = "Quorum" // Avoid policy application that requires real pods
				rabbitmq := CreateRabbitMQ(rabbitmqName, spec)
				DeferCleanup(th.DeleteInstance, rabbitmq)

				// Wait for cluster to be created
				SimulateRabbitMQClusterReady(rabbitmqName)
				Eventually(func(g Gomega) {
					instance := GetRabbitMQ(rabbitmqName)
					g.Expect(instance.Labels["rabbitmqcurrentversion"]).To(Equal("3.9"))
				}, timeout, interval).Should(Succeed())

				// Create a mock StatefulSet to simulate the one created by RabbitMQ operator
				statefulSet := &appsv1.StatefulSet{}
				statefulSet.Name = rabbitmqName.Name + "-server"
				statefulSet.Namespace = rabbitmqName.Namespace
				statefulSet.Spec.Selector = &metav1.LabelSelector{
					MatchLabels: map[string]string{
						"app.kubernetes.io/name": rabbitmqName.Name,
					},
				}
				statefulSet.Spec.Template.ObjectMeta.Labels = map[string]string{
					"app.kubernetes.io/name": rabbitmqName.Name,
				}
				statefulSet.Spec.Template.Spec.Containers = []corev1.Container{
					{
						Name:  "rabbitmq",
						Image: "quay.io/rabbitmq/rabbitmq:3.13",
					},
				}
				statefulSet.Spec.UpdateStrategy.Type = appsv1.RollingUpdateStatefulSetStrategyType
				Expect(k8sClient.Create(ctx, statefulSet)).Should(Succeed())
				DeferCleanup(k8sClient.Delete, statefulSet)
			})

			It("should patch StatefulSet with cleanup container and OnDelete strategy", func() {
				// Trigger version upgrade
				instance := GetRabbitMQ(rabbitmqName)
				if instance.Labels == nil {
					instance.Labels = make(map[string]string)
				}
				instance.Labels["rabbitmqversion"] = "3.13"
				Expect(k8sClient.Update(ctx, instance)).Should(Succeed())

				// Verify StatefulSet is patched
				Eventually(func(g Gomega) {
					statefulSet := &appsv1.StatefulSet{}
					g.Expect(k8sClient.Get(ctx, types.NamespacedName{
						Name:      rabbitmqName.Name + "-server",
						Namespace: rabbitmqName.Namespace,
					}, statefulSet)).Should(Succeed())

					// Check for clean-mnesia init container
					g.Expect(len(statefulSet.Spec.Template.Spec.InitContainers)).To(BeNumerically(">", 0))
					g.Expect(statefulSet.Spec.Template.Spec.InitContainers[0].Name).To(Equal("clean-mnesia"))
					g.Expect(statefulSet.Spec.Template.Spec.InitContainers[0].Image).To(Equal("quay.io/rabbitmq/rabbitmq:3.13"))
					g.Expect(statefulSet.Spec.Template.Spec.InitContainers[0].Command).To(Equal([]string{"sh", "-c", "rm -rf /var/lib/rabbitmq/mnesia/*"}))

					// Check update strategy
					g.Expect(statefulSet.Spec.UpdateStrategy.Type).To(Equal(appsv1.OnDeleteStatefulSetStrategyType))
				}, timeout, interval).Should(Succeed())
			})
		})

		When("Version upgrade completes and reconciliation is paused", func() {
			BeforeEach(func() {
				spec := GetDefaultRabbitMQSpec()
				spec["containerImage"] = "quay.io/rabbitmq/rabbitmq:3.13"
				spec["queueType"] = "Quorum" // Avoid policy application that requires real pods
				rabbitmq := CreateRabbitMQ(rabbitmqName, spec)
				DeferCleanup(th.DeleteInstance, rabbitmq)

				SimulateRabbitMQClusterReady(rabbitmqName)
				Eventually(func(g Gomega) {
					instance := GetRabbitMQ(rabbitmqName)
					g.Expect(instance.Labels["rabbitmqcurrentversion"]).To(Equal("3.9"))
				}, timeout, interval).Should(Succeed())

				// Create mock StatefulSet
				statefulSet := &appsv1.StatefulSet{}
				statefulSet.Name = rabbitmqName.Name + "-server"
				statefulSet.Namespace = rabbitmqName.Namespace
				statefulSet.Spec.Selector = &metav1.LabelSelector{
					MatchLabels: map[string]string{
						"app.kubernetes.io/name": rabbitmqName.Name,
					},
				}
				statefulSet.Spec.Template.ObjectMeta.Labels = map[string]string{
					"app.kubernetes.io/name": rabbitmqName.Name,
				}
				statefulSet.Spec.Template.Spec.Containers = []corev1.Container{
					{Name: "rabbitmq", Image: "quay.io/rabbitmq/rabbitmq:3.13"},
				}
				statefulSet.Spec.UpdateStrategy.Type = appsv1.RollingUpdateStatefulSetStrategyType
				Expect(k8sClient.Create(ctx, statefulSet)).Should(Succeed())
				DeferCleanup(k8sClient.Delete, statefulSet)

				// Trigger upgrade
				instance := GetRabbitMQ(rabbitmqName)
				instance.Labels["rabbitmqversion"] = "3.13"
				Expect(k8sClient.Update(ctx, instance)).Should(Succeed())

				// Wait for StatefulSet to be patched
				Eventually(func(g Gomega) {
					ss := &appsv1.StatefulSet{}
					g.Expect(k8sClient.Get(ctx, types.NamespacedName{
						Name:      rabbitmqName.Name + "-server",
						Namespace: rabbitmqName.Namespace,
					}, ss)).Should(Succeed())
					g.Expect(ss.Spec.UpdateStrategy.Type).To(Equal(appsv1.OnDeleteStatefulSetStrategyType))
				}, timeout, interval).Should(Succeed())
			})

			It("should update version labels when cluster is ready", func() {
				// Simulate cluster ready
				SimulateRabbitMQClusterReady(rabbitmqName)

				// Verify version labels are updated
				Eventually(func(g Gomega) {
					instance := GetRabbitMQ(rabbitmqName)
					g.Expect(instance.Labels["rabbitmqcurrentversion"]).To(Equal("3.13"))
					g.Expect(instance.Status.VersionUpgradeInProgress).To(BeEmpty())
				}, timeout, interval).Should(Succeed())
			})

			It("should verify reconciliation is paused during upgrade", func() {
				// Verify RabbitmqCluster has pause label
				Eventually(func(g Gomega) {
					cluster := GetRabbitMQCluster(rabbitmqName)
					g.Expect(cluster.Labels).NotTo(BeNil())
					g.Expect(cluster.Labels["rabbitmq.com/pauseReconciliation"]).To(Equal("true"))
				}, timeout, interval).Should(Succeed())
			})
		})

		When("RabbitMQ upgrades from 3.9 to 3.13 (end-to-end)", func() {
			BeforeEach(func() {
				spec := GetDefaultRabbitMQSpec()
				spec["containerImage"] = "quay.io/rabbitmq/rabbitmq:3.9"
				spec["queueType"] = "Quorum" // Avoid policy application that requires real pods
				rabbitmq := CreateRabbitMQ(rabbitmqName, spec)
				DeferCleanup(th.DeleteInstance, rabbitmq)

				SimulateRabbitMQClusterReady(rabbitmqName)
			})

			It("should complete full upgrade cycle with pause and resume", func() {
				// 1. Verify initial version is 3.9
				Eventually(func(g Gomega) {
					instance := GetRabbitMQ(rabbitmqName)
					g.Expect(instance.Labels["rabbitmqcurrentversion"]).To(Equal("3.9"))
					g.Expect(instance.Status.VersionUpgradeInProgress).To(BeEmpty())
				}, timeout, interval).Should(Succeed())

				// Create mock StatefulSet
				statefulSet := &appsv1.StatefulSet{}
				statefulSet.Name = rabbitmqName.Name + "-server"
				statefulSet.Namespace = rabbitmqName.Namespace
				statefulSet.Spec.Selector = &metav1.LabelSelector{
					MatchLabels: map[string]string{
						"app.kubernetes.io/name": rabbitmqName.Name,
					},
				}
				statefulSet.Spec.Template.ObjectMeta.Labels = map[string]string{
					"app.kubernetes.io/name": rabbitmqName.Name,
				}
				statefulSet.Spec.Template.Spec.Containers = []corev1.Container{
					{Name: "rabbitmq", Image: "quay.io/rabbitmq/rabbitmq:3.9"},
				}
				statefulSet.Spec.UpdateStrategy.Type = appsv1.RollingUpdateStatefulSetStrategyType
				Expect(k8sClient.Create(ctx, statefulSet)).Should(Succeed())
				DeferCleanup(k8sClient.Delete, statefulSet)

				// 2. Update to version 3.13
				instance := GetRabbitMQ(rabbitmqName)
				instance.Labels["rabbitmqversion"] = "3.13"
				instance.Spec.ContainerImage = "quay.io/rabbitmq/rabbitmq:3.13"
				Expect(k8sClient.Update(ctx, instance)).Should(Succeed())

				// 3. Verify upgrade in progress
				Eventually(func(g Gomega) {
					updatedInstance := GetRabbitMQ(rabbitmqName)
					g.Expect(updatedInstance.Status.VersionUpgradeInProgress).To(Equal("3.13"))
				}, timeout, interval).Should(Succeed())

				// 4. Verify RabbitmqCluster reconciliation is paused
				Eventually(func(g Gomega) {
					cluster := GetRabbitMQCluster(rabbitmqName)
					g.Expect(cluster.Labels).NotTo(BeNil())
					g.Expect(cluster.Labels["rabbitmq.com/pauseReconciliation"]).To(Equal("true"))
				}, timeout, interval).Should(Succeed())

				// 5. Verify StatefulSet is patched with cleanup container and OnDelete
				Eventually(func(g Gomega) {
					ss := &appsv1.StatefulSet{}
					g.Expect(k8sClient.Get(ctx, types.NamespacedName{
						Name:      rabbitmqName.Name + "-server",
						Namespace: rabbitmqName.Namespace,
					}, ss)).Should(Succeed())

					g.Expect(len(ss.Spec.Template.Spec.InitContainers)).To(BeNumerically(">", 0))
					g.Expect(ss.Spec.Template.Spec.InitContainers[0].Name).To(Equal("clean-mnesia"))
					g.Expect(ss.Spec.UpdateStrategy.Type).To(Equal(appsv1.OnDeleteStatefulSetStrategyType))
				}, timeout, interval).Should(Succeed())

				// 6. Simulate cluster ready (with pods restarted)
				SimulateRabbitMQClusterReady(rabbitmqName)

				// Create mock pods to simulate ready state
				for i := 0; i < 3; i++ {
					pod := &corev1.Pod{}
					pod.Name = fmt.Sprintf("%s-server-%d", rabbitmqName.Name, i)
					pod.Namespace = rabbitmqName.Namespace
					pod.Labels = map[string]string{
						"app.kubernetes.io/name": rabbitmqName.Name,
					}
					pod.Spec.Containers = []corev1.Container{
						{Name: "rabbitmq", Image: "quay.io/rabbitmq/rabbitmq:3.13"},
					}
					pod.Status.Phase = corev1.PodRunning
					pod.Status.ContainerStatuses = []corev1.ContainerStatus{
						{Name: "rabbitmq", Ready: true},
					}
					Expect(k8sClient.Create(ctx, pod)).Should(Succeed())
					DeferCleanup(k8sClient.Delete, pod)
				}

				// 7. Verify version labels are updated
				Eventually(func(g Gomega) {
					finalInstance := GetRabbitMQ(rabbitmqName)
					g.Expect(finalInstance.Labels["rabbitmqcurrentversion"]).To(Equal("3.13"))
					g.Expect(finalInstance.Status.VersionUpgradeInProgress).To(BeEmpty())
				}, timeout, interval).Should(Succeed())

				// 8. Verify reconciliation is eventually resumed
				// (In a real scenario, this happens after pods are ready with new version)
				Eventually(func(g Gomega) {
					cluster := GetRabbitMQCluster(rabbitmqName)
					// Reconciliation should be resumed (pause label removed)
					if cluster.Labels != nil {
						g.Expect(cluster.Labels["rabbitmq.com/pauseReconciliation"]).To(Or(BeEmpty(), Equal("false")))
					}
				}, timeout*3, interval).Should(Succeed())
			})
		})
	})
})
