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
	"testing"
	"time"

	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"

	rabbitmqv1 "github.com/openstack-k8s-operators/infra-operator/apis/rabbitmq/v1beta1"
	"github.com/openstack-k8s-operators/lib-common/modules/common/util"
	dataplanev1 "github.com/openstack-k8s-operators/openstack-operator/api/dataplane/v1beta1"
)

func TestIsUserStillInUseByNodeSets(t *testing.T) {
	scheme := runtime.NewScheme()
	_ = rabbitmqv1.AddToScheme(scheme)
	_ = dataplanev1.AddToScheme(scheme)
	_ = corev1.AddToScheme(scheme)

	baseTime := time.Now()

	tests := []struct {
		name           string
		user           *rabbitmqv1.RabbitMQUser
		secret         *corev1.Secret
		nodesets       []*dataplanev1.OpenStackDataPlaneNodeSet
		secretName     string
		wantStillInUse bool
		wantInfoSubstr string
		wantErr        bool
	}{
		{
			name: "secret doesn't exist",
			user: &rabbitmqv1.RabbitMQUser{
				ObjectMeta: metav1.ObjectMeta{
					Name:              "test-user",
					Namespace:         "test",
					CreationTimestamp: metav1.Time{Time: baseTime},
				},
			},
			secretName:     "nonexistent",
			wantStillInUse: false,
			wantErr:        false,
		},
		{
			name: "no nodesets exist",
			user: &rabbitmqv1.RabbitMQUser{
				ObjectMeta: metav1.ObjectMeta{
					Name:              "test-user",
					Namespace:         "test",
					CreationTimestamp: metav1.Time{Time: baseTime},
				},
			},
			secret: &corev1.Secret{
				ObjectMeta: metav1.ObjectMeta{
					Name:      "test-secret",
					Namespace: "test",
				},
				Data: map[string][]byte{
					"username": []byte("testuser"),
					"password": []byte("testpass"),
				},
			},
			secretName:     "test-secret",
			wantStillInUse: false,
			wantErr:        false,
		},
		{
			name: "nodeset with partial update blocks deletion",
			user: &rabbitmqv1.RabbitMQUser{
				ObjectMeta: metav1.ObjectMeta{
					Name:              "test-user",
					Namespace:         "test",
					CreationTimestamp: metav1.Time{Time: baseTime},
				},
			},
			secret: &corev1.Secret{
				ObjectMeta: metav1.ObjectMeta{
					Name:      "test-secret",
					Namespace: "test",
				},
				Data: map[string][]byte{
					"username": []byte("testuser"),
					"password": []byte("testpass"),
				},
			},
			nodesets: []*dataplanev1.OpenStackDataPlaneNodeSet{
				{
					ObjectMeta: metav1.ObjectMeta{
						Name:              "test-nodeset",
						Namespace:         "test",
						CreationTimestamp: metav1.Time{Time: baseTime.Add(1 * time.Second)},
					},
					Spec: dataplanev1.OpenStackDataPlaneNodeSetSpec{
						Nodes: map[string]dataplanev1.NodeSection{
							"compute-0": {},
							"compute-1": {},
							"compute-2": {},
						},
					},
					Status: dataplanev1.OpenStackDataPlaneNodeSetStatus{
						ServiceCredentialStatus: map[string]dataplanev1.ServiceCredentialInfo{
							"nova": {
								SecretName:      "test-secret",
								SecretHash:      "", // Will be set below to match actual hash
								UpdatedNodes:    []string{"compute-0", "compute-1"},
								TotalNodes:      3,
								AllNodesUpdated: false,
							},
						},
					},
				},
			},
			secretName:     "test-secret",
			wantStillInUse: true,
			wantInfoSubstr: "2/3 nodes have this credential",
			wantErr:        false,
		},
		{
			name: "nodeset with all nodes updated allows deletion",
			user: &rabbitmqv1.RabbitMQUser{
				ObjectMeta: metav1.ObjectMeta{
					Name:              "test-user",
					Namespace:         "test",
					CreationTimestamp: metav1.Time{Time: baseTime},
				},
			},
			secret: &corev1.Secret{
				ObjectMeta: metav1.ObjectMeta{
					Name:      "test-secret",
					Namespace: "test",
				},
				Data: map[string][]byte{
					"username": []byte("testuser"),
					"password": []byte("testpass"),
				},
			},
			nodesets: []*dataplanev1.OpenStackDataPlaneNodeSet{
				{
					ObjectMeta: metav1.ObjectMeta{
						Name:              "test-nodeset",
						Namespace:         "test",
						CreationTimestamp: metav1.Time{Time: baseTime.Add(1 * time.Second)},
					},
					Spec: dataplanev1.OpenStackDataPlaneNodeSetSpec{
						Nodes: map[string]dataplanev1.NodeSection{
							"compute-0": {},
							"compute-1": {},
							"compute-2": {},
						},
					},
					Status: dataplanev1.OpenStackDataPlaneNodeSetStatus{
						ServiceCredentialStatus: map[string]dataplanev1.ServiceCredentialInfo{
							"nova": {
								SecretName:      "test-secret",
								SecretHash:      "test-hash",
								UpdatedNodes:    []string{"compute-0", "compute-1", "compute-2"},
								TotalNodes:      3,
								AllNodesUpdated: true,
							},
						},
					},
				},
			},
			secretName:     "test-secret",
			wantStillInUse: false,
			wantErr:        false,
		},
		{
			name: "nodeset created before user with partial update blocks deletion",
			user: &rabbitmqv1.RabbitMQUser{
				ObjectMeta: metav1.ObjectMeta{
					Name:              "test-user",
					Namespace:         "test",
					CreationTimestamp: metav1.Time{Time: baseTime},
				},
			},
			secret: &corev1.Secret{
				ObjectMeta: metav1.ObjectMeta{
					Name:      "test-secret",
					Namespace: "test",
				},
				Data: map[string][]byte{
					"username": []byte("testuser"),
					"password": []byte("testpass"),
				},
			},
			nodesets: []*dataplanev1.OpenStackDataPlaneNodeSet{
				{
					ObjectMeta: metav1.ObjectMeta{
						Name:              "older-nodeset",
						Namespace:         "test",
						CreationTimestamp: metav1.Time{Time: baseTime.Add(-1 * time.Hour)},
					},
					Spec: dataplanev1.OpenStackDataPlaneNodeSetSpec{
						Nodes: map[string]dataplanev1.NodeSection{
							"compute-0": {},
						},
					},
					Status: dataplanev1.OpenStackDataPlaneNodeSetStatus{
						ServiceCredentialStatus: map[string]dataplanev1.ServiceCredentialInfo{
							"nova": {
								SecretName:      "test-secret",
								SecretHash:      "", // Will be set to match actual hash
								UpdatedNodes:    []string{},
								TotalNodes:      1,
								AllNodesUpdated: false,
							},
						},
					},
				},
			},
			secretName:     "test-secret",
			wantStillInUse: true, // Even older nodesets block deletion if using credentials
			wantInfoSubstr: "0/1 nodes have this credential",
			wantErr:        false,
		},
		{
			name: "nodeset with different hash allows deletion",
			user: &rabbitmqv1.RabbitMQUser{
				ObjectMeta: metav1.ObjectMeta{
					Name:              "test-user",
					Namespace:         "test",
					CreationTimestamp: metav1.Time{Time: baseTime},
				},
			},
			secret: &corev1.Secret{
				ObjectMeta: metav1.ObjectMeta{
					Name:      "test-secret",
					Namespace: "test",
				},
				Data: map[string][]byte{
					"username": []byte("testuser"),
					"password": []byte("testpass"),
				},
			},
			nodesets: []*dataplanev1.OpenStackDataPlaneNodeSet{
				{
					ObjectMeta: metav1.ObjectMeta{
						Name:              "test-nodeset",
						Namespace:         "test",
						CreationTimestamp: metav1.Time{Time: baseTime.Add(1 * time.Second)},
					},
					Spec: dataplanev1.OpenStackDataPlaneNodeSetSpec{
						Nodes: map[string]dataplanev1.NodeSection{
							"compute-0": {},
						},
					},
					Status: dataplanev1.OpenStackDataPlaneNodeSetStatus{
						ServiceCredentialStatus: map[string]dataplanev1.ServiceCredentialInfo{
							"nova": {
								SecretName:      "test-secret",
								SecretHash:      "different-hash-new-credentials",
								UpdatedNodes:    []string{"compute-0"},
								TotalNodes:      1,
								AllNodesUpdated: true,
							},
						},
					},
				},
			},
			secretName:     "test-secret",
			wantStillInUse: false, // Different hash = different credentials
			wantErr:        false,
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			// If we have a secret, compute its hash and update nodesets that reference it
			var actualHash string
			if tt.secret != nil {
				// Use util.ObjectHash to match production code
				hash, err := util.ObjectHash(tt.secret.Data)
				if err != nil {
					t.Fatalf("Failed to compute hash: %v", err)
				}
				actualHash = hash

				// Update nodeset status with actual hash
				for _, ns := range tt.nodesets {
					for svcName, credInfo := range ns.Status.ServiceCredentialStatus {
						if credInfo.SecretName == tt.secret.Name && credInfo.SecretHash == "" {
							credInfo.SecretHash = actualHash
							ns.Status.ServiceCredentialStatus[svcName] = credInfo
						}
					}
				}
			}

			// Build list of runtime objects
			objs := []runtime.Object{tt.user}
			if tt.secret != nil {
				objs = append(objs, tt.secret)
			}
			for _, ns := range tt.nodesets {
				objs = append(objs, ns)
			}

			// Create fake client
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
				tt.secretName,
			)

			if (err != nil) != tt.wantErr {
				t.Errorf("isUserStillInUseByNodeSets() error = %v, wantErr %v", err, tt.wantErr)
				return
			}

			if stillInUse != tt.wantStillInUse {
				t.Errorf("isUserStillInUseByNodeSets() stillInUse = %v, want %v", stillInUse, tt.wantStillInUse)
			}

			if tt.wantInfoSubstr != "" {
				if info == "" {
					t.Errorf("isUserStillInUseByNodeSets() info is empty, want substring %q", tt.wantInfoSubstr)
				} else if !contains(info, tt.wantInfoSubstr) {
					t.Errorf("isUserStillInUseByNodeSets() info = %q, want substring %q", info, tt.wantInfoSubstr)
				}
			}
		})
	}
}

func contains(s, substr string) bool {
	return len(s) >= len(substr) && (s == substr || len(substr) == 0 ||
		(len(s) > 0 && len(substr) > 0 && findSubstring(s, substr)))
}

func findSubstring(s, substr string) bool {
	for i := 0; i <= len(s)-len(substr); i++ {
		if s[i:i+len(substr)] == substr {
			return true
		}
	}
	return false
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
