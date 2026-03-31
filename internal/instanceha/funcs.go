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

// Package instanceha provides utilities for creating Kubernetes resources for InstanceHA
package instanceha

import (
	"os"

	instancehav1 "github.com/openstack-k8s-operators/infra-operator/apis/instanceha/v1beta1"
	topologyv1 "github.com/openstack-k8s-operators/infra-operator/apis/topology/v1beta1"
	env "github.com/openstack-k8s-operators/lib-common/modules/common/env"

	appsv1 "k8s.io/api/apps/v1"
	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/util/intstr"
	"k8s.io/utils/ptr"
)

// Deployment creates a Kubernetes Deployment for the InstanceHa resource
func Deployment(
	instance *instancehav1.InstanceHa,
	labels map[string]string,
	annotations map[string]string,
	openstackcloud string,
	configHash string,
	containerImage string,
	topology *topologyv1.Topology,
) *appsv1.Deployment {
	replicas := int32(1)

	envVars := map[string]env.Setter{}
	envVars["OS_CLOUD"] = env.SetValue(openstackcloud)
	envVars["CONFIG_HASH"] = env.SetValue(configHash)
	envVars["INSTANCEHA_DISABLED"] = env.SetValue(string(instance.Spec.Disabled))

	// AI configuration: pass through from operator environment.
	// These will be sourced from the CRD spec once AIConfig is added.
	for _, aiVar := range []string{"AI_ENABLED", "AI_ENDPOINT", "AI_MODEL", "AI_API_KEY", "AI_MODEL_PATH", "AI_N_CTX", "AI_N_THREADS"} {
		if val := os.Getenv(aiVar); val != "" {
			envVars[aiVar] = env.SetValue(val)
		}
	}

	// create Volume and VolumeMounts
	volumes := instancehaPodVolumes(instance)
	volumeMounts := instancehaPodVolumeMounts()

	livenessProbe := &corev1.Probe{
		// TODO might need tuning
		TimeoutSeconds:      30,
		PeriodSeconds:       30,
		InitialDelaySeconds: 10,
	}

	livenessProbe.HTTPGet = &corev1.HTTPGetAction{
		Path: "/",
		Port: intstr.IntOrString{Type: intstr.Int, IntVal: 8080},
	}

	// add CA cert if defined
	if instance.Spec.CaBundleSecretName != "" {
		volumes = append(volumes, instance.Spec.CreateVolume())
		volumeMounts = append(volumeMounts, instance.Spec.CreateVolumeMounts(nil)...)
	}

	dep := &appsv1.Deployment{
		ObjectMeta: metav1.ObjectMeta{
			Name:      instance.Name,
			Namespace: instance.Namespace,
		},
		Spec: appsv1.DeploymentSpec{
			Replicas: &replicas,
			Strategy: appsv1.DeploymentStrategy{
				Type: "Recreate",
			},
			Selector: &metav1.LabelSelector{
				MatchLabels: labels,
			},
			Template: corev1.PodTemplateSpec{
				ObjectMeta: metav1.ObjectMeta{
					Labels:      labels,
					Annotations: annotations,
				},
				Spec: corev1.PodSpec{
					ServiceAccountName:            instance.RbacResourceName(),
					Volumes:                       volumes,
					TerminationGracePeriodSeconds: ptr.To[int64](0),
					Containers: []corev1.Container{{
						Name:    "instanceha",
						Image:   containerImage,
						Command: []string{"/usr/bin/python3", "-u", "/var/lib/instanceha/scripts/instanceha.py"},
						SecurityContext: &corev1.SecurityContext{
							RunAsUser:                ptr.To[int64](42401),
							RunAsGroup:               ptr.To[int64](42401),
							RunAsNonRoot:             ptr.To(true),
							AllowPrivilegeEscalation: ptr.To(false),
							Capabilities: &corev1.Capabilities{
								Drop: []corev1.Capability{
									"ALL",
								},
							},
						},
						Env: env.MergeEnvs([]corev1.EnvVar{}, envVars),
						Ports: []corev1.ContainerPort{
							{
								ContainerPort: instance.Spec.InstanceHaKdumpPort,
								Protocol:      "UDP",
								Name:          "instanceha",
							},
							{
								ContainerPort: 8081,
								Protocol:      "TCP",
								Name:          "mcp",
							},
						},
						VolumeMounts:  volumeMounts,
						LivenessProbe: livenessProbe,
					}},
				},
			},
		},
	}

	if instance.Spec.NodeSelector != nil {
		dep.Spec.Template.Spec.NodeSelector = *instance.Spec.NodeSelector
	}

	if topology != nil {
		// Get the Topology .Spec
		ts := topology.Spec
		// Process TopologySpreadConstraints if defined in the referenced Topology
		if ts.TopologySpreadConstraints != nil {
			dep.Spec.Template.Spec.TopologySpreadConstraints = *topology.Spec.TopologySpreadConstraints
		}
		// Process Affinity if defined in the referenced Topology
		if ts.Affinity != nil {
			dep.Spec.Template.Spec.Affinity = ts.Affinity
		}
	}

	return dep
}

func instancehaPodVolumeMounts() []corev1.VolumeMount {
	return []corev1.VolumeMount{
		{
			Name:      "openstack-config",
			MountPath: "/home/cloud-admin/.config/openstack/clouds.yaml",
			SubPath:   "clouds.yaml",
		},
		{
			Name:      "openstack-config-secret",
			MountPath: "/home/cloud-admin/.config/openstack/secure.yaml",
			SubPath:   "secure.yaml",
		},
		{
			Name:      "fencing-secret",
			MountPath: "/secrets/fencing.yaml",
			SubPath:   "fencing.yaml",
		},
		{
			Name:      "instanceha-script",
			MountPath: "/var/lib/instanceha/scripts",
			ReadOnly:  true,
		},
		{
			Name:      "instanceha-config",
			MountPath: "/var/lib/instanceha/config.yaml",
			SubPath:   "config.yaml",
			ReadOnly:  true,
		},
		{
			Name:      "instanceha-run",
			MountPath: "/var/run/instanceha",
		},
		{
			Name:      "instanceha-log",
			MountPath: "/var/log/instanceha",
		},
	}
}

func instancehaPodVolumes(
	instance *instancehav1.InstanceHa,
) []corev1.Volume {
	var config0644AccessMode int32 = 0o644
	return []corev1.Volume{
		{
			Name: "openstack-config",
			VolumeSource: corev1.VolumeSource{
				ConfigMap: &corev1.ConfigMapVolumeSource{
					LocalObjectReference: corev1.LocalObjectReference{
						Name: instance.Spec.OpenStackConfigMap,
					},
				},
			},
		},
		{
			Name: "openstack-config-secret",
			VolumeSource: corev1.VolumeSource{
				Secret: &corev1.SecretVolumeSource{
					SecretName: instance.Spec.OpenStackConfigSecret,
				},
			},
		},
		{
			Name: "fencing-secret",
			VolumeSource: corev1.VolumeSource{
				Secret: &corev1.SecretVolumeSource{
					SecretName: instance.Spec.FencingSecret,
				},
			},
		},
		{
			Name: "instanceha-script",
			VolumeSource: corev1.VolumeSource{
				ConfigMap: &corev1.ConfigMapVolumeSource{
					DefaultMode: &config0644AccessMode,
					LocalObjectReference: corev1.LocalObjectReference{
						Name: instance.Name + "-sh",
					},
					Items: []corev1.KeyToPath{
						// Entry point script
						{Key: "instanceha.py", Path: "instanceha.py"},
						// InstanceHA Python package
						{Key: "instanceha_init_py", Path: "instanceha/__init__.py"},
						{Key: "instanceha_models_py", Path: "instanceha/models.py"},
						{Key: "instanceha_validation_py", Path: "instanceha/validation.py"},
						{Key: "instanceha_config_py", Path: "instanceha/config.py"},
						{Key: "instanceha_nova_py", Path: "instanceha/nova.py"},
						{Key: "instanceha_service_py", Path: "instanceha/service.py"},
						{Key: "instanceha_monitoring_py", Path: "instanceha/monitoring.py"},
						{Key: "instanceha_fencing_py", Path: "instanceha/fencing.py"},
						{Key: "instanceha_evacuation_py", Path: "instanceha/evacuation.py"},
						{Key: "instanceha_reserved_hosts_py", Path: "instanceha/reserved_hosts.py"},
						{Key: "instanceha_main_py", Path: "instanceha/main.py"},
						// AI layer
						{Key: "instanceha_ai_init_py", Path: "instanceha/ai/__init__.py"},
						{Key: "instanceha_ai_tools_py", Path: "instanceha/ai/tools.py"},
						{Key: "instanceha_ai_safety_py", Path: "instanceha/ai/safety.py"},
						{Key: "instanceha_ai_read_tools_py", Path: "instanceha/ai/read_tools.py"},
						{Key: "instanceha_ai_write_tools_py", Path: "instanceha/ai/write_tools.py"},
						{Key: "instanceha_ai_diagnostic_tools_py", Path: "instanceha/ai/diagnostic_tools.py"},
						// AI chat interface
						{Key: "instanceha_ai_command_parser_py", Path: "instanceha/ai/command_parser.py"},
						{Key: "instanceha_ai_chat_server_py", Path: "instanceha/ai/chat_server.py"},
						{Key: "instanceha_ai_chat_client_py", Path: "instanceha/ai/chat_client.py"},
						// AI LLM integration
						{Key: "instanceha_ai_engine_py", Path: "instanceha/ai/engine.py"},
						{Key: "instanceha_ai_agent_py", Path: "instanceha/ai/agent.py"},
						{Key: "instanceha_ai_prompts_py", Path: "instanceha/ai/prompts.py"},
						{Key: "instanceha_ai_context_py", Path: "instanceha/ai/context.py"},
						// AI intelligent monitoring
						{Key: "instanceha_ai_event_bus_py", Path: "instanceha/ai/event_bus.py"},
						{Key: "instanceha_ai_observer_py", Path: "instanceha/ai/observer.py"},
						// AI MCP server
						{Key: "instanceha_ai_mcp_server_py", Path: "instanceha/ai/mcp_server.py"},
					},
				},
			},
		},
		{
			Name: "instanceha-run",
			VolumeSource: corev1.VolumeSource{
				EmptyDir: &corev1.EmptyDirVolumeSource{},
			},
		},
		{
			Name: "instanceha-log",
			VolumeSource: corev1.VolumeSource{
				EmptyDir: &corev1.EmptyDirVolumeSource{},
			},
		},
		{
			Name: "instanceha-config",
			VolumeSource: corev1.VolumeSource{
				ConfigMap: &corev1.ConfigMapVolumeSource{
					LocalObjectReference: corev1.LocalObjectReference{
						Name: instance.Spec.InstanceHaConfigMap,
					},
				},
			},
		},
	}
}
