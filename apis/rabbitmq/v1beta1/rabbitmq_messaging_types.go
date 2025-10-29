/*
Copyright 2022.

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

package v1beta1

import (
	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
)

// RabbitmqClusterReference represents a reference to a RabbitMQ cluster
type RabbitmqClusterReference struct {
	Name string `json:"name"`
}

// UserTag represents a tag for a RabbitMQ user
type UserTag struct {
	Name string `json:"name"`
}

// UserSpec defines the desired state of User
type UserSpec struct {
	RabbitmqClusterReference RabbitmqClusterReference     `json:"rabbitmqClusterReference"`
	Tags                     []UserTag                    `json:"tags"`
	ImportCredentialsSecret  *corev1.LocalObjectReference `json:"importCredentialsSecret,omitempty"`
}

//+kubebuilder:object:root=true
//+kubebuilder:subresource:status

// User is the Schema for the users API
type User struct {
	metav1.TypeMeta   `json:",inline"`
	metav1.ObjectMeta `json:"metadata,omitempty"`
	Spec              UserSpec `json:"spec"`
}

//+kubebuilder:object:root=true

// UserList contains a list of User
type UserList struct {
	metav1.TypeMeta `json:",inline"`
	metav1.ListMeta `json:"metadata,omitempty"`
	Items           []User `json:"items"`
}



// VhostSpec defines the desired state of Vhost
type VhostSpec struct {
	Name                     string                   `json:"name"`
	RabbitmqClusterReference RabbitmqClusterReference `json:"rabbitmqClusterReference"`
}

//+kubebuilder:object:root=true
//+kubebuilder:subresource:status

// Vhost is the Schema for the vhosts API
type Vhost struct {
	metav1.TypeMeta   `json:",inline"`
	metav1.ObjectMeta `json:"metadata,omitempty"`
	Spec              VhostSpec `json:"spec"`
}

//+kubebuilder:object:root=true

// VhostList contains a list of Vhost
type VhostList struct {
	metav1.TypeMeta `json:",inline"`
	metav1.ListMeta `json:"metadata,omitempty"`
	Items           []Vhost `json:"items"`
}



// VhostPermissions defines the permissions for a vhost
type VhostPermissions struct {
	Configure string `json:"configure"`
	Write     string `json:"write"`
	Read      string `json:"read"`
}

// PermissionSpec defines the desired state of Permission
type PermissionSpec struct {
	Vhost                    string                   `json:"vhost"`
	User                     string                   `json:"user"`
	Permissions              VhostPermissions         `json:"permissions"`
	RabbitmqClusterReference RabbitmqClusterReference `json:"rabbitmqClusterReference"`
}

//+kubebuilder:object:root=true
//+kubebuilder:subresource:status

// Permission is the Schema for the permissions API
type Permission struct {
	metav1.TypeMeta   `json:",inline"`
	metav1.ObjectMeta `json:"metadata,omitempty"`
	Spec              PermissionSpec `json:"spec"`
}

//+kubebuilder:object:root=true

// PermissionList contains a list of Permission
type PermissionList struct {
	metav1.TypeMeta `json:",inline"`
	metav1.ListMeta `json:"metadata,omitempty"`
	Items           []Permission `json:"items"`
}



func init() {
	SchemeBuilder.Register(&User{}, &UserList{})
	SchemeBuilder.Register(&Vhost{}, &VhostList{})
	SchemeBuilder.Register(&Permission{}, &PermissionList{})
}
