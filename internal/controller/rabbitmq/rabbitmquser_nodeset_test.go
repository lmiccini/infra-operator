/*

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
	"context"
	"strings"
	"testing"
	"time"

	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"

	rabbitmqv1 "github.com/openstack-k8s-operators/infra-operator/apis/rabbitmq/v1beta1"
	oko_secret "github.com/openstack-k8s-operators/lib-common/modules/common/secret"
	dataplanev1 "github.com/openstack-k8s-operators/openstack-operator/api/dataplane/v1beta1"
)

func TestIsUserStillInUseByNodeSets(t *testing.T) {
	scheme := runtime.NewScheme()
	_ = rabbitmqv1.AddToScheme(scheme)
	_ = dataplanev1.AddToScheme(scheme)
	_ = corev1.AddToScheme(scheme)

	baseTime := time.Now()

	// Create cluster secrets and pre-compute their hashes
	currentSecret := &corev1.Secret{
		ObjectMeta: metav1.ObjectMeta{
			Name:      "nova-cell1-compute-config",
			Namespace: "test",
		},
		Data: map[string][]byte{"transport_url": []byte("rabbit://nova:current-password@rabbitmq:5672/")},
	}
	currentHash, _ := oko_secret.Hash(currentSecret)

	tests := []struct {
		name           string
		user           *rabbitmqv1.RabbitMQUser
		nodesets       []*dataplanev1.OpenStackDataPlaneNodeSet
		secrets        []*corev1.Secret
		wantStillInUse bool
		wantInfoSubstr string
		wantErr        bool
	}{
		{
			name: "no nodesets exist",
			user: &rabbitmqv1.RabbitMQUser{
				ObjectMeta: metav1.ObjectMeta{
					Name:              "test-user",
					Namespace:         "test",
					CreationTimestamp: metav1.Time{Time: baseTime},
				},
			},
			wantStillInUse: false,
			wantErr:        false,
		},
		{
			name: "nodeset with stale secrets blocks deletion",
			user: &rabbitmqv1.RabbitMQUser{
				ObjectMeta: metav1.ObjectMeta{
					Name:              "test-user",
					Namespace:         "test",
					CreationTimestamp: metav1.Time{Time: baseTime},
				},
			},
			nodesets: []*dataplanev1.OpenStackDataPlaneNodeSet{
				{
					ObjectMeta: metav1.ObjectMeta{
						Name:      "test-nodeset",
						Namespace: "test",
					},
					Spec: dataplanev1.OpenStackDataPlaneNodeSetSpec{
						Nodes: map[string]dataplanev1.NodeSection{
							"compute-0": {},
							"compute-1": {},
						},
					},
					Status: dataplanev1.OpenStackDataPlaneNodeSetStatus{
						SecretHashes: map[string]string{
							"nova-cell1-compute-config": "old-stale-hash",
						},
					},
				},
			},
			secrets:        []*corev1.Secret{currentSecret},
			wantStillInUse: true,
			wantInfoSubstr: "has changed since last full deployment",
			wantErr:        false,
		},
		{
			name: "nodeset with current secrets allows deletion",
			user: &rabbitmqv1.RabbitMQUser{
				ObjectMeta: metav1.ObjectMeta{
					Name:              "test-user",
					Namespace:         "test",
					CreationTimestamp: metav1.Time{Time: baseTime},
				},
			},
			nodesets: []*dataplanev1.OpenStackDataPlaneNodeSet{
				{
					ObjectMeta: metav1.ObjectMeta{
						Name:      "test-nodeset",
						Namespace: "test",
					},
					Spec: dataplanev1.OpenStackDataPlaneNodeSetSpec{
						Nodes: map[string]dataplanev1.NodeSection{
							"compute-0": {},
						},
					},
					Status: dataplanev1.OpenStackDataPlaneNodeSetStatus{
						SecretHashes: map[string]string{
							"nova-cell1-compute-config": currentHash,
						},
					},
				},
			},
			secrets:        []*corev1.Secret{currentSecret},
			wantStillInUse: false,
			wantErr:        false,
		},
		{
			name: "nodeset with empty SecretHashes allows deletion",
			user: &rabbitmqv1.RabbitMQUser{
				ObjectMeta: metav1.ObjectMeta{
					Name:              "test-user",
					Namespace:         "test",
					CreationTimestamp: metav1.Time{Time: baseTime},
				},
			},
			nodesets: []*dataplanev1.OpenStackDataPlaneNodeSet{
				{
					ObjectMeta: metav1.ObjectMeta{
						Name:      "never-deployed-nodeset",
						Namespace: "test",
					},
					Spec: dataplanev1.OpenStackDataPlaneNodeSetSpec{
						Nodes: map[string]dataplanev1.NodeSection{
							"compute-0": {},
						},
					},
					Status: dataplanev1.OpenStackDataPlaneNodeSetStatus{
						SecretHashes: map[string]string{},
					},
				},
			},
			wantStillInUse: false,
			wantErr:        false,
		},
		{
			name: "deployed secret deleted blocks deletion",
			user: &rabbitmqv1.RabbitMQUser{
				ObjectMeta: metav1.ObjectMeta{
					Name:              "test-user",
					Namespace:         "test",
					CreationTimestamp: metav1.Time{Time: baseTime},
				},
			},
			nodesets: []*dataplanev1.OpenStackDataPlaneNodeSet{
				{
					ObjectMeta: metav1.ObjectMeta{
						Name:      "test-nodeset",
						Namespace: "test",
					},
					Spec: dataplanev1.OpenStackDataPlaneNodeSetSpec{
						Nodes: map[string]dataplanev1.NodeSection{
							"compute-0": {},
						},
					},
					Status: dataplanev1.OpenStackDataPlaneNodeSetStatus{
						SecretHashes: map[string]string{
							"deleted-secret": "some-hash",
						},
					},
				},
			},
			secrets:        []*corev1.Secret{},
			wantStillInUse: true,
			wantInfoSubstr: "no longer exists",
			wantErr:        false,
		},
		{
			name: "multiple nodesets - one stale blocks deletion",
			user: &rabbitmqv1.RabbitMQUser{
				ObjectMeta: metav1.ObjectMeta{
					Name:              "test-user",
					Namespace:         "test",
					CreationTimestamp: metav1.Time{Time: baseTime},
				},
			},
			nodesets: []*dataplanev1.OpenStackDataPlaneNodeSet{
				{
					ObjectMeta: metav1.ObjectMeta{
						Name:      "up-to-date-nodeset",
						Namespace: "test",
					},
					Spec: dataplanev1.OpenStackDataPlaneNodeSetSpec{
						Nodes: map[string]dataplanev1.NodeSection{
							"compute-0": {},
						},
					},
					Status: dataplanev1.OpenStackDataPlaneNodeSetStatus{
						SecretHashes: map[string]string{
							"nova-cell1-compute-config": currentHash,
						},
					},
				},
				{
					ObjectMeta: metav1.ObjectMeta{
						Name:      "stale-nodeset",
						Namespace: "test",
					},
					Spec: dataplanev1.OpenStackDataPlaneNodeSetSpec{
						Nodes: map[string]dataplanev1.NodeSection{
							"compute-1": {},
							"compute-2": {},
						},
					},
					Status: dataplanev1.OpenStackDataPlaneNodeSetStatus{
						SecretHashes: map[string]string{
							"nova-cell1-compute-config": "old-hash-from-previous-deployment",
						},
					},
				},
			},
			secrets:        []*corev1.Secret{currentSecret},
			wantStillInUse: true,
			wantInfoSubstr: "has changed since last full deployment",
			wantErr:        false,
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			objs := []runtime.Object{tt.user}
			for _, ns := range tt.nodesets {
				objs = append(objs, ns)
			}
			for _, s := range tt.secrets {
				objs = append(objs, s)
			}

			client := fake.NewClientBuilder().
				WithScheme(scheme).
				WithRuntimeObjects(objs...).
				Build()

			reconciler := &RabbitMQUserReconciler{
				Client: client,
			}

			stillInUse, info, err := reconciler.isUserStillInUseByNodeSets(
				context.Background(),
				tt.user,
			)

			if (err != nil) != tt.wantErr {
				t.Errorf("isUserStillInUseByNodeSets() error = %v, wantErr %v", err, tt.wantErr)
				return
			}

			if stillInUse != tt.wantStillInUse {
				t.Errorf("isUserStillInUseByNodeSets() stillInUse = %v, want %v (info: %s)", stillInUse, tt.wantStillInUse, info)
			}

			if tt.wantInfoSubstr != "" {
				if info == "" {
					t.Errorf("isUserStillInUseByNodeSets() info is empty, want substring %q", tt.wantInfoSubstr)
				} else if !strings.Contains(info, tt.wantInfoSubstr) {
					t.Errorf("isUserStillInUseByNodeSets() info = %q, want substring %q", info, tt.wantInfoSubstr)
				}
			}
		})
	}
}

func TestIsInternalFinalizer(t *testing.T) {
	tests := []struct {
		name       string
		finalizer  string
		wantResult bool
	}{
		{
			name:       "UserFinalizer is internal",
			finalizer:  rabbitmqv1.UserFinalizer,
			wantResult: true,
		},
		{
			name:       "TransportURLFinalizer is internal",
			finalizer:  rabbitmqv1.TransportURLFinalizer,
			wantResult: true,
		},
		{
			name:       "RabbitMQUserCleanupBlockedFinalizer is internal",
			finalizer:  rabbitmqv1.RabbitMQUserCleanupBlockedFinalizer,
			wantResult: true,
		},
		{
			name:       "random finalizer is external",
			finalizer:  "some.other.controller/finalizer",
			wantResult: false,
		},
		{
			name:       "dataplane finalizer is external",
			finalizer:  "dataplane.openstack.org/finalizer",
			wantResult: false,
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			result := rabbitmqv1.IsInternalFinalizer(tt.finalizer)
			if result != tt.wantResult {
				t.Errorf("IsInternalFinalizer(%q) = %v, want %v", tt.finalizer, result, tt.wantResult)
			}
		})
	}
}
