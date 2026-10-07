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
	"strings"
	"testing"

	corev1 "k8s.io/api/core/v1"
	"k8s.io/utils/ptr"
)

func containerByName(cs []corev1.Container, name string) *corev1.Container {
	for i := range cs {
		if cs[i].Name == name {
			return &cs[i]
		}
	}
	return nil
}

func hasEnv(c *corev1.Container, name string) bool {
	for _, e := range c.Env {
		if e.Name == name {
			return true
		}
	}
	return false
}

func TestNodeRejoinEnabled(t *testing.T) {
	cases := []struct {
		version  string
		replicas int32
		want     bool
	}{
		{"4.2", 3, true},
		{"4.1", 3, true},
		{"4.1.2", 5, true},
		{"4.0", 3, false},  // seed-node behavior starts at 4.1
		{"3.13", 3, false}, // old peer discovery, no seed problem
		{"4.2", 1, false},  // single node, nothing to rejoin
		{"bad", 3, false},  // unparseable
	}
	for _, tc := range cases {
		r := newTestRabbitMq("rabbitmq")
		r.Spec.Replicas = ptr.To(tc.replicas)
		if got := nodeRejoinEnabled(r, tc.version); got != tc.want {
			t.Errorf("nodeRejoinEnabled(%q, replicas=%d) = %v, want %v", tc.version, tc.replicas, got, tc.want)
		}
	}
}

func TestNodeRejoinScriptEmbedded(t *testing.T) {
	for _, tok := range []string{"await_online_nodes", "join_cluster", ".operator-fresh-node", "server-0"} {
		if !strings.Contains(nodeRejoinScript, tok) {
			t.Errorf("embedded node-rejoin.sh missing %q", tok)
		}
	}
}

func TestStatefulSet_RejoinWiredWhenEnabled(t *testing.T) {
	r := newTestRabbitMq("rabbitmq")
	r.Spec.Replicas = ptr.To(int32(3))
	sts := StatefulSet(r, "hash", nil, nil, "4.2", false, ProxyConfig{})

	main := containerByName(sts.Spec.Template.Spec.Containers, "rabbitmq")
	if main == nil {
		t.Fatal("rabbitmq container not found")
	}
	if main.Lifecycle == nil || main.Lifecycle.PostStart == nil || main.Lifecycle.PostStart.Exec == nil {
		t.Fatal("expected PostStart exec hook on rabbitmq container")
	}
	if got := strings.Join(main.Lifecycle.PostStart.Exec.Command, " "); !strings.Contains(got, "/operator/node-rejoin.sh") {
		t.Errorf("PostStart command = %q, want it to run /operator/node-rejoin.sh", got)
	}
	if !hasEnv(main, "RABBITMQ_REPLICAS") {
		t.Error("expected RABBITMQ_REPLICAS env on main container")
	}

	setup := containerByName(sts.Spec.Template.Spec.InitContainers, "setup-container")
	if setup == nil {
		t.Fatal("setup-container not found")
	}
	args := strings.Join(setup.Args, "\n")
	if !strings.Contains(args, "base64 -d > /operator/node-rejoin.sh") {
		t.Error("init container should stage the rejoin script into /operator")
	}
	if !strings.Contains(args, ".operator-fresh-node") {
		t.Error("init container should write the fresh-node marker for a blank server-0")
	}
	for _, e := range []string{"MY_POD_NAME", "MY_POD_NAMESPACE", "K8S_SERVICE_NAME"} {
		if !hasEnv(setup, e) {
			t.Errorf("init container missing env %q", e)
		}
	}
}

func TestStatefulSet_RejoinAbsentWhenDisabled(t *testing.T) {
	cases := []struct {
		name     string
		version  string
		replicas int32
	}{
		{"single-replica", "4.2", 1},
		{"pre-4.1", "4.0", 3},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			r := newTestRabbitMq("rabbitmq")
			r.Spec.Replicas = ptr.To(tc.replicas)
			sts := StatefulSet(r, "hash", nil, nil, tc.version, false, ProxyConfig{})

			main := containerByName(sts.Spec.Template.Spec.Containers, "rabbitmq")
			if main.Lifecycle != nil && main.Lifecycle.PostStart != nil {
				t.Error("did not expect a PostStart hook when rejoin is disabled")
			}
			if hasEnv(main, "RABBITMQ_REPLICAS") {
				t.Error("did not expect RABBITMQ_REPLICAS when rejoin is disabled")
			}
			setup := containerByName(sts.Spec.Template.Spec.InitContainers, "setup-container")
			if strings.Contains(strings.Join(setup.Args, "\n"), ".operator-fresh-node") {
				t.Error("did not expect the fresh-node marker when rejoin is disabled")
			}
		})
	}
}
