/*
Copyright 2023 Red Hat
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

package helpers

import (
	"fmt"

	t "github.com/onsi/gomega"
	redisv1 "github.com/openstack-k8s-operators/infra-operator/apis/redis/v1beta1"
	"github.com/openstack-k8s-operators/lib-common/modules/common/condition"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
	"k8s.io/utils/ptr"
)

// CreateRedis creates a new Redis instance with the specified namespace in the Kubernetes cluster.
func (tc *TestHelper) CreateRedis(namespace string, redisName string, spec redisv1.RedisSpec) types.NamespacedName {
	name := types.NamespacedName{
		Name:      redisName,
		Namespace: namespace,
	}

	r := &redisv1.Redis{
		TypeMeta: metav1.TypeMeta{
			APIVersion: "redis.openstack.org/v1beta1",
			Kind:       "Redis",
		},
		ObjectMeta: metav1.ObjectMeta{
			Name:      redisName,
			Namespace: namespace,
		},
		Spec: spec,
	}

	t.Expect(tc.K8sClient.Create(tc.Ctx, r)).Should(t.Succeed())

	return name
}

// GetRedis waits for and retrieves a Redis instance from the Kubernetes cluster
func (tc *TestHelper) GetRedis(name types.NamespacedName) *redisv1.Redis {
	r := &redisv1.Redis{}
	t.Eventually(func(g t.Gomega) {
		g.Expect(tc.K8sClient.Get(tc.Ctx, name, r)).Should(t.Succeed())
	}, tc.Timeout, tc.Interval).Should(t.Succeed())
	return r
}

// SimulateRedisReady simulates a ready state for a Redis instance in a Kubernetes cluster.
func (tc *TestHelper) SimulateRedisReady(name types.NamespacedName) {
	t.Eventually(func(g t.Gomega) {
		r := tc.GetRedis(name)
		r.Status.ObservedGeneration = r.Generation
		r.Status.Conditions.MarkTrue(condition.ReadyCondition, condition.ReadyMessage)

		replicas := int32(1)
		if r.Spec.Replicas != nil {
			replicas = *r.Spec.Replicas
		}

		headlessServiceName := r.Name + "-redis"
		sentinelHosts := make([]string, 0, replicas)
		for i := int32(0); i < replicas; i++ {
			sentinelHosts = append(sentinelHosts,
				fmt.Sprintf("%s-%d.%s.%s.svc:%d",
					headlessServiceName, i,
					headlessServiceName,
					r.Namespace,
					redisv1.SentinelPort,
				))
		}
		r.Status.SentinelHosts = sentinelHosts

		// This can return conflict so we have the t.Eventually block to retry
		g.Expect(tc.K8sClient.Status().Update(tc.Ctx, r)).To(t.Succeed())

	}, tc.Timeout, tc.Interval).Should(t.Succeed())

	tc.Logger.Info("Simulated redis ready", "on", name)
}

// GetDefaultRedisSpec returns redisv1.RedisSpec for test-helpers
func (tc *TestHelper) GetDefaultRedisSpec() redisv1.RedisSpec {
	return redisv1.RedisSpec{
		RedisSpecCore: redisv1.RedisSpecCore{
			Replicas: ptr.To(int32(3)),
		},
	}
}
