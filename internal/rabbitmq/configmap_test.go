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

package rabbitmq

import (
	"strings"
	"testing"

	rabbitmqv1 "github.com/openstack-k8s-operators/infra-operator/apis/rabbitmq/v1beta1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/utils/ptr"
)

func TestGenerateServerConfigMap_RelaxedChecks_DuringMigration(t *testing.T) {
	r := &rabbitmqv1.RabbitMq{
		ObjectMeta: metav1.ObjectMeta{Name: "test-mq", Namespace: "test-ns"},
		Spec: rabbitmqv1.RabbitMqSpec{
			RabbitMqSpecCore: rabbitmqv1.RabbitMqSpecCore{
				Replicas:  ptr.To(int32(3)),
				QueueType: ptr.To(rabbitmqv1.QueueTypeQuorum),
			},
		},
	}

	// With proxy enabled (migration in progress), relaxed checks should be set
	cm := GenerateServerConfigMap(r, false, false, "4.2", true)
	defaults := cm.Data["operatorDefaults.conf"]

	if !strings.Contains(defaults, "quorum_queue.property_equivalence.relaxed_checks_on_redeclaration = true") {
		t.Error("4.x with proxy enabled should enable relaxed checks")
	}

	// Without proxy (no migration), relaxed checks should NOT be set
	cmNoProxy := GenerateServerConfigMap(r, false, false, "4.2", false)
	defaultsNoProxy := cmNoProxy.Data["operatorDefaults.conf"]

	if strings.Contains(defaultsNoProxy, "relaxed_checks_on_redeclaration") {
		t.Error("4.x without proxy should not set relaxed checks")
	}
}

func TestGenerateServerConfigMap_RelaxedChecks_3x(t *testing.T) {
	r := &rabbitmqv1.RabbitMq{
		ObjectMeta: metav1.ObjectMeta{Name: "test-mq", Namespace: "test-ns"},
		Spec: rabbitmqv1.RabbitMqSpec{
			RabbitMqSpecCore: rabbitmqv1.RabbitMqSpecCore{
				Replicas:  ptr.To(int32(3)),
				QueueType: ptr.To(rabbitmqv1.QueueTypeQuorum),
			},
		},
	}

	// 3.x should NOT include relaxed checks even with proxy enabled
	cm := GenerateServerConfigMap(r, false, false, "3.9", true)
	defaults := cm.Data["operatorDefaults.conf"]

	if strings.Contains(defaults, "relaxed_checks_on_redeclaration") {
		t.Error("3.x should not set relaxed checks")
	}
}

func TestGenerateServerConfigMap_TLS_VersionAware(t *testing.T) {
	r := &rabbitmqv1.RabbitMq{
		ObjectMeta: metav1.ObjectMeta{Name: "test-mq", Namespace: "test-ns"},
		Spec: rabbitmqv1.RabbitMqSpec{
			RabbitMqSpecCore: rabbitmqv1.RabbitMqSpecCore{
				Replicas: ptr.To(int32(1)),
				TLS: rabbitmqv1.RabbitMQTLSSpec{
					SecretName: "tls-secret",
				},
			},
		},
	}

	// 3.x non-FIPS: TLS 1.2 only
	cm3x := GenerateServerConfigMap(r, false, false, "3.9", false)
	adv3x := cm3x.Data["advanced.config"]
	if !strings.Contains(adv3x, "['tlsv1.2']") {
		t.Error("3.x non-FIPS advanced.config should use TLS 1.2 only")
	}
	if strings.Contains(adv3x, "tlsv1.3") {
		t.Error("3.x non-FIPS advanced.config should not contain TLS 1.3")
	}

	// 4.x non-FIPS: TLS 1.2+1.3
	cm4x := GenerateServerConfigMap(r, false, false, "4.2", false)
	adv4x := cm4x.Data["advanced.config"]
	if !strings.Contains(adv4x, "['tlsv1.2','tlsv1.3']") {
		t.Error("4.x advanced.config should use TLS 1.2+1.3")
	}

	// 3.x FIPS: TLS 1.2+1.3
	cm3xFips := GenerateServerConfigMap(r, false, true, "3.9", false)
	adv3xFips := cm3xFips.Data["advanced.config"]
	if !strings.Contains(adv3xFips, "['tlsv1.2','tlsv1.3']") {
		t.Error("3.x FIPS advanced.config should use TLS 1.2+1.3")
	}
}

func TestGenerateConfigDataConfigMap_InterNodeTLS_VersionAware(t *testing.T) {
	r := &rabbitmqv1.RabbitMq{
		ObjectMeta: metav1.ObjectMeta{Name: "test-mq", Namespace: "test-ns"},
		Spec: rabbitmqv1.RabbitMqSpec{
			RabbitMqSpecCore: rabbitmqv1.RabbitMqSpecCore{
				TLS: rabbitmqv1.RabbitMQTLSSpec{
					SecretName: "tls-secret",
				},
			},
		},
	}

	// 3.x: TLS 1.2 only for inter-node
	cm3x := GenerateConfigDataConfigMap(r, false, "3.9")
	interNode3x := cm3x.Data["inter_node_tls.config"]
	if count := strings.Count(interNode3x, "['tlsv1.2']"); count != 2 {
		t.Errorf("3.x inter-node TLS should have 2 occurrences of TLS 1.2 only, got %d", count)
	}

	// 4.x: TLS 1.2+1.3 for inter-node
	cm4x := GenerateConfigDataConfigMap(r, false, "4.2")
	interNode4x := cm4x.Data["inter_node_tls.config"]
	if count := strings.Count(interNode4x, "['tlsv1.2','tlsv1.3']"); count != 2 {
		t.Errorf("4.x inter-node TLS should have 2 occurrences of TLS 1.2+1.3, got %d", count)
	}
}

func TestGenerateConfigDataConfigMap_NoTLS(t *testing.T) {
	r := &rabbitmqv1.RabbitMq{
		ObjectMeta: metav1.ObjectMeta{Name: "test-mq", Namespace: "test-ns"},
		Spec: rabbitmqv1.RabbitMqSpec{
			RabbitMqSpecCore: rabbitmqv1.RabbitMqSpecCore{},
		},
	}

	cm := GenerateConfigDataConfigMap(r, false, "4.2")
	if _, ok := cm.Data["inter_node_tls.config"]; ok {
		t.Error("config-data should not include inter_node_tls.config when TLS is not enabled")
	}
}

func TestGenerateServerConfigMap_NoTLS_AdvancedConfig(t *testing.T) {
	r := &rabbitmqv1.RabbitMq{
		ObjectMeta: metav1.ObjectMeta{Name: "test-mq", Namespace: "test-ns"},
		Spec: rabbitmqv1.RabbitMqSpec{
			RabbitMqSpecCore: rabbitmqv1.RabbitMqSpecCore{
				Replicas: ptr.To(int32(1)),
			},
		},
	}

	cm := GenerateServerConfigMap(r, false, false, "4.2", false)
	adv := cm.Data["advanced.config"]
	if adv != "[].\n" {
		t.Errorf("no-TLS advanced.config should be empty Erlang config, got %q", adv)
	}
}
