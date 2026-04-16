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

package functional_test

import (
	"fmt"

	. "github.com/onsi/ginkgo/v2" //revive:disable:dot-imports
	. "github.com/onsi/gomega"    //revive:disable:dot-imports

	//revive:disable-next-line:dot-imports
	redisv1 "github.com/openstack-k8s-operators/infra-operator/apis/redis/v1beta1"
	condition "github.com/openstack-k8s-operators/lib-common/modules/common/condition"
	. "github.com/openstack-k8s-operators/lib-common/modules/common/test/helpers"
	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/types"
)

var _ = Describe("Redis Controller", func() {
	var redisName types.NamespacedName

	When("a default Redis gets created", func() {
		BeforeEach(func() {
			redis := CreateRedisConfig(namespace, GetDefaultRedisSpec())
			redisName.Name = redis.GetName()
			redisName.Namespace = redis.GetNamespace()
			DeferCleanup(th.DeleteInstance, redis)
		})

		It("should have created a Redis", func() {
			Eventually(func(_ Gomega) {
				GetRedis(redisName)
			}, timeout, interval).Should(Succeed())
		})
	})

	When("Deployment rollout is progressing", func() {
		BeforeEach(func() {
			redis := CreateRedisConfig(namespace, GetDefaultRedisSpec())
			redisName.Name = redis.GetName()
			redisName.Namespace = redis.GetNamespace()
			DeferCleanup(th.DeleteInstance, redis)

			stsName := types.NamespacedName{
				Name:      redisName.Name + "-redis",
				Namespace: redisName.Namespace,
			}
			th.SimulateStatefulSetProgressing(stsName)
		})

		It("reaches Ready when deployment rollout finished", func() {
			stsName := types.NamespacedName{
				Name:      redisName.Name + "-redis",
				Namespace: redisName.Namespace,
			}

			th.ExpectCondition(
				redisName,
				ConditionGetterFunc(RedisConditionGetter),
				condition.DeploymentReadyCondition,
				corev1.ConditionFalse,
			)

			th.SimulateStatefulSetReplicaReady(stsName)

			th.ExpectCondition(
				redisName,
				ConditionGetterFunc(RedisConditionGetter),
				condition.DeploymentReadyCondition,
				corev1.ConditionTrue,
			)
		})
	})

	When("Redis reaches Ready state", func() {
		BeforeEach(func() {
			redis := CreateRedisConfig(namespace, GetDefaultRedisSpec())
			redisName.Name = redis.GetName()
			redisName.Namespace = redis.GetNamespace()
			DeferCleanup(th.DeleteInstance, redis)

			stsName := types.NamespacedName{
				Name:      redisName.Name + "-redis",
				Namespace: redisName.Namespace,
			}
			th.SimulateStatefulSetReplicaReady(stsName)
		})

		It("populates SentinelHosts in status", func() {
			Eventually(func(g Gomega) {
				instance := GetRedis(redisName)
				g.Expect(instance.Status.SentinelHosts).To(HaveLen(3))
				headlessServiceName := redisName.Name + "-redis"
				for i := 0; i < 3; i++ {
					expectedHost := fmt.Sprintf("%s-%d.%s.%s.svc.cluster.local:%d",
						headlessServiceName, i,
						headlessServiceName,
						redisName.Namespace,
						redisv1.SentinelPort,
					)
					g.Expect(instance.Status.SentinelHosts[i]).To(Equal(expectedHost))
				}
			}, timeout, interval).Should(Succeed())
		})

		It("returns correct sentinel URL from GetRedisSentinelURL", func() {
			Eventually(func(g Gomega) {
				instance := GetRedis(redisName)
				g.Expect(instance.Status.SentinelHosts).To(HaveLen(3))

				sentinelURL := instance.GetRedisSentinelURL()
				headlessServiceName := redisName.Name + "-redis"
				expectedPrimary := fmt.Sprintf("%s-0.%s.%s.svc.cluster.local:%d",
					headlessServiceName,
					headlessServiceName,
					redisName.Namespace,
					redisv1.SentinelPort,
				)
				g.Expect(sentinelURL).To(HavePrefix("redis://" + expectedPrimary))
				g.Expect(sentinelURL).To(ContainSubstring("sentinel=redis"))
				for i := 1; i < 3; i++ {
					fallback := fmt.Sprintf("%s-%d.%s.%s.svc.cluster.local:%d",
						headlessServiceName, i,
						headlessServiceName,
						redisName.Namespace,
						redisv1.SentinelPort,
					)
					g.Expect(sentinelURL).To(ContainSubstring("sentinel_fallback=" + fallback))
				}
				g.Expect(sentinelURL).NotTo(ContainSubstring("ssl=true"))
			}, timeout, interval).Should(Succeed())
		})

		It("returns correct master name from GetSentinelMasterName", func() {
			instance := GetRedis(redisName)
			Expect(instance.GetSentinelMasterName()).To(Equal("redis"))
		})

		It("returns correct client URL from GetRedisClientURL", func() {
			instance := GetRedis(redisName)
			clientURL := instance.GetRedisClientURL()
			Expect(clientURL).To(Equal(fmt.Sprintf("redis://%s:6379/", redisName.Name)))
			Expect(clientURL).NotTo(ContainSubstring("ssl=true"))
		})

		It("sets TLSSupport to False when TLS is not configured", func() {
			Eventually(func(g Gomega) {
				instance := GetRedis(redisName)
				g.Expect(instance.Status.TLSSupport).To(Equal("False"))
			}, timeout, interval).Should(Succeed())
		})
	})
})
