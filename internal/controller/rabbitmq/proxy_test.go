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
	"testing"

	rabbitmqv1beta1 "github.com/openstack-k8s-operators/infra-operator/apis/rabbitmq/v1beta1"
	"k8s.io/utils/ptr"
)

func TestShouldEnableProxy(t *testing.T) {
	r := &Reconciler{}

	tests := []struct {
		name     string
		instance func() *rabbitmqv1beta1.RabbitMq
		want     bool
	}{
		{
			name: "should enable proxy when ProxyRequired status is true",
			instance: func() *rabbitmqv1beta1.RabbitMq {
				return &rabbitmqv1beta1.RabbitMq{
					Spec: rabbitmqv1beta1.RabbitMqSpec{
						RabbitMqSpecCore: rabbitmqv1beta1.RabbitMqSpecCore{
							QueueType: ptr.To("Quorum"),
						},
					},
					Status: rabbitmqv1beta1.RabbitMqStatus{
						CurrentVersion: "4.2.0",
						QueueType:      "Quorum",
						ProxyRequired:  true,
					},
				}
			},
			want: true,
		},
		{
			name: "should enable proxy during 3.x to 4.x upgrade with Quorum (ProxyRequired set by controller)",
			instance: func() *rabbitmqv1beta1.RabbitMq {
				return &rabbitmqv1beta1.RabbitMq{
					Spec: rabbitmqv1beta1.RabbitMqSpec{
						RabbitMqSpecCore: rabbitmqv1beta1.RabbitMqSpecCore{
							QueueType: ptr.To("Quorum"),
						},
					},
					Status: rabbitmqv1beta1.RabbitMqStatus{
						UpgradePhase:   rabbitmqv1beta1.UpgradePhaseWaitingForCluster,
						CurrentVersion: "3.9.0",
						QueueType:      "Mirrored",
						ProxyRequired:  true, // Set by controller during 3.x → 4.x upgrade
					},
				}
			},
			want: true,
		},
		{
			name: "should NOT enable proxy when ProxyRequired is false",
			instance: func() *rabbitmqv1beta1.RabbitMq {
				return &rabbitmqv1beta1.RabbitMq{
					Spec: rabbitmqv1beta1.RabbitMqSpec{
						RabbitMqSpecCore: rabbitmqv1beta1.RabbitMqSpecCore{
							QueueType: ptr.To("Quorum"),
						},
					},
					Status: rabbitmqv1beta1.RabbitMqStatus{
						CurrentVersion: "4.0.0",
						QueueType:      "Quorum",
						ProxyRequired:  false,
					},
				}
			},
			want: false,
		},
		// Note: clients-reconfigured annotation is handled early in the reconciler
		// (before shouldEnableProxy is called), which sets ProxyRequired=false.
		// So shouldEnableProxy only needs to check ProxyRequired (tested above).
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			instance := tt.instance()
			got := r.shouldEnableProxy(instance)
			if got != tt.want {
				t.Errorf("shouldEnableProxy() = %v, want %v", got, tt.want)
			}
		})
	}
}
