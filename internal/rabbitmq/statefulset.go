package rabbitmq

import (
	"fmt"

	rabbitmqv1 "github.com/openstack-k8s-operators/infra-operator/apis/rabbitmq/v1beta1"
	topologyv1 "github.com/openstack-k8s-operators/infra-operator/apis/topology/v1beta1"
	labels "github.com/openstack-k8s-operators/lib-common/modules/common/labels"
	appsv1 "k8s.io/api/apps/v1"
	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/api/resource"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/util/intstr"
	"k8s.io/utils/ptr"
)

// ProxyConfig holds configuration for the AMQP proxy sidecar
type ProxyConfig struct {
	Enabled     bool
	IPv6Enabled bool
	// BuildContainer is a function that builds the proxy sidecar container.
	// Injected by the controller to avoid circular dependencies.
	BuildContainer func(instance *rabbitmqv1.RabbitMq, ipv6 bool) corev1.Container
}

// StatefulSet returns a StatefulSet resource for the RabbitMQ CR
// matching the old rabbitmq-cluster-operator's resource layout.
// If targetVersion is non-empty and needsDataWipe is true, a wipe-data init container
// is prepended to clear persistent data before the main setup container runs.
// If proxy is enabled, a proxy sidecar container is added and RabbitMQ's readiness
// probe is adjusted to check the backend port on localhost.
func StatefulSet(
	r *rabbitmqv1.RabbitMq,
	configHash string,
	topology *topologyv1.Topology,
	envVars []corev1.EnvVar,
	targetVersion string,
	needsDataWipe bool,
	proxy ProxyConfig,
) *appsv1.StatefulSet {
	matchls := map[string]string{
		labels.K8sAppName: r.Name,
	}
	ls := map[string]string{
		labels.K8sAppComponent: "rabbitmq",
		labels.K8sAppName:      r.Name,
		labels.K8sAppPartOf:    "rabbitmq",
	}

	// Default replica count
	replicas := ptr.To(int32(1))
	if r.Spec.Replicas != nil {
		replicas = r.Spec.Replicas
	}

	// Build container environment variables
	containerEnv := buildContainerEnv(r, envVars)

	// Build the readiness probe (no liveness probe, matching old operator)
	readinessProbe := buildReadinessProbe(r)

	// Build the main RabbitMQ container
	rabbitmqContainer := corev1.Container{
		Name:  "rabbitmq",
		Image: r.Spec.ContainerImage,
		Args: []string{
			"/usr/lib/rabbitmq/bin/rabbitmq-server",
		},
		Env:            containerEnv,
		Ports:          buildContainerPorts(r),
		VolumeMounts:   getVolumeMounts(r, proxy.IPv6Enabled),
		ReadinessProbe: readinessProbe,
		Lifecycle:      buildLifecycle(),
	}

	// When proxy is enabled, RabbitMQ listens on localhost only (backend port).
	// The proxy handles client connections on the standard ports.
	// Update the readiness probe to check backend port via exec (since RabbitMQ
	// listens on loopback, TCPSocket probes from kubelet won't reach it).
	if proxy.Enabled {
		loopbackAddr := "127.0.0.1"
		if proxy.IPv6Enabled {
			loopbackAddr = "::1"
		}
		rabbitmqContainer.ReadinessProbe = &corev1.Probe{
			ProbeHandler: corev1.ProbeHandler{
				Exec: &corev1.ExecAction{
					Command: []string{
						"/bin/sh",
						"-c",
						fmt.Sprintf("timeout 1 bash -c '</dev/tcp/%s/%d'", loopbackAddr, BackendPort),
					},
				},
			},
			InitialDelaySeconds: 10,
			TimeoutSeconds:      5,
			PeriodSeconds:       10,
			SuccessThreshold:    1,
			FailureThreshold:    3,
		}
	}

	// Set resources if specified
	if r.Spec.Resources != nil {
		rabbitmqContainer.Resources = *r.Spec.Resources
	}

	// Build init containers
	var initContainers []corev1.Container

	// Add data-wipe init container for storage upgrades.
	// Runs BEFORE setup-container and clears /var/lib/rabbitmq.
	// Version-specific marker files prevent duplicate wipes across pod restarts.
	if needsDataWipe && targetVersion != "" && ValidVersionPattern(targetVersion) {
		initContainers = append(initContainers, buildWipeDataInitContainer(r, targetVersion))
	}

	initContainers = append(initContainers, buildInitContainer(r))

	// Build containers list
	containers := []corev1.Container{rabbitmqContainer}

	// Build volumes
	volumes := getVolumes(r)

	// Add proxy sidecar if enabled
	if proxy.Enabled && proxy.BuildContainer != nil {
		containers = append(containers, proxy.BuildContainer(r, proxy.IPv6Enabled))

		// Add proxy script volume
		volumes = append(volumes, corev1.Volume{
			Name: "proxy-script",
			VolumeSource: corev1.VolumeSource{
				ConfigMap: &corev1.ConfigMapVolumeSource{
					LocalObjectReference: corev1.LocalObjectReference{
						Name: r.Name + "-proxy-script",
					},
					DefaultMode: ptr.To[int32](0555),
				},
			},
		})
	}

	sts := &appsv1.StatefulSet{
		ObjectMeta: metav1.ObjectMeta{
			Name:      fmt.Sprintf("%s-server", r.Name),
			Namespace: r.Namespace,
			Labels:    ls,
		},
		Spec: appsv1.StatefulSetSpec{
			ServiceName:         fmt.Sprintf("%s-nodes", r.Name),
			Replicas:            replicas,
			PodManagementPolicy: appsv1.ParallelPodManagement,
			Selector: &metav1.LabelSelector{
				MatchLabels: matchls,
			},
			PersistentVolumeClaimRetentionPolicy: &appsv1.StatefulSetPersistentVolumeClaimRetentionPolicy{
				WhenDeleted: appsv1.RetainPersistentVolumeClaimRetentionPolicyType,
				WhenScaled:  appsv1.RetainPersistentVolumeClaimRetentionPolicyType,
			},
			Template: corev1.PodTemplateSpec{
				ObjectMeta: metav1.ObjectMeta{
					Labels: ls,
					Annotations: map[string]string{
						"config-hash": configHash,
					},
				},
				Spec: corev1.PodSpec{
					ServiceAccountName:            r.RbacResourceName(),
					AutomountServiceAccountToken:  ptr.To(true),
					TerminationGracePeriodSeconds: ptr.To(int64(604800)),
					InitContainers:                initContainers,
					Containers:                    containers,
					Volumes:                       volumes,
					SecurityContext: &corev1.PodSecurityContext{
						FSGroup: ptr.To(int64(0)),
					},
				},
			},
			VolumeClaimTemplates: getVolumeClaimTemplates(r),
		},
	}

	// Apply node selector if specified
	if r.Spec.NodeSelector != nil {
		sts.Spec.Template.Spec.NodeSelector = *r.Spec.NodeSelector
	}

	// Apply topology or default affinity (matching old operator's preferredDuringScheduling)
	if topology != nil {
		topology.ApplyTo(&sts.Spec.Template)
	} else {
		sts.Spec.Template.Spec.Affinity = &corev1.Affinity{
			PodAntiAffinity: &corev1.PodAntiAffinity{
				PreferredDuringSchedulingIgnoredDuringExecution: []corev1.WeightedPodAffinityTerm{
					{
						Weight: 100,
						PodAffinityTerm: corev1.PodAffinityTerm{
							LabelSelector: &metav1.LabelSelector{
								MatchExpressions: []metav1.LabelSelectorRequirement{
									{
										Key:      labels.K8sAppName,
										Operator: metav1.LabelSelectorOpIn,
										Values:   []string{r.Name},
									},
								},
							},
							TopologyKey: "kubernetes.io/hostname",
						},
					},
				},
			},
		}
		sts.Spec.Template.Spec.TopologySpreadConstraints = []corev1.TopologySpreadConstraint{
			{
				MaxSkew:           1,
				TopologyKey:       "topology.kubernetes.io/zone",
				WhenUnsatisfiable: corev1.ScheduleAnyway,
				LabelSelector: &metav1.LabelSelector{
					MatchLabels: matchls,
				},
			},
		}
	}

	return sts
}

// buildContainerEnv builds the environment variables for the RabbitMQ container
// matching the old rabbitmq-cluster-operator layout
func buildContainerEnv(r *rabbitmqv1.RabbitMq, additionalEnv []corev1.EnvVar) []corev1.EnvVar {
	env := []corev1.EnvVar{
		{
			Name: "MY_POD_NAME",
			ValueFrom: &corev1.EnvVarSource{
				FieldRef: &corev1.ObjectFieldSelector{
					APIVersion: "v1",
					FieldPath:  "metadata.name",
				},
			},
		},
		{
			Name: "MY_POD_NAMESPACE",
			ValueFrom: &corev1.EnvVarSource{
				FieldRef: &corev1.ObjectFieldSelector{
					APIVersion: "v1",
					FieldPath:  "metadata.namespace",
				},
			},
		},
		{
			Name:  "K8S_SERVICE_NAME",
			Value: fmt.Sprintf("%s-nodes", r.Name),
		},
	}

	// Append additional environment variables (ERL_ARGS, TLS config from cluster.go)
	env = append(env, additionalEnv...)

	env = append(env,
		corev1.EnvVar{
			Name:  "RABBITMQ_UPGRADE_LOG",
			Value: "/var/lib/rabbitmq/rabbitmq_upgrade.log",
		},
		corev1.EnvVar{
			Name:  "HOME",
			Value: "/var/lib/rabbitmq",
		},
		corev1.EnvVar{
			Name:  "PATH",
			Value: "/usr/lib/rabbitmq/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
		},
		corev1.EnvVar{
			Name:  "RABBITMQ_ENABLED_PLUGINS_FILE",
			Value: "/operator/enabled_plugins",
		},
		corev1.EnvVar{
			Name:  "RABBITMQ_USE_LONGNAME",
			Value: "true",
		},
		corev1.EnvVar{
			Name:  "RABBITMQ_NODENAME",
			Value: "rabbit@$(MY_POD_NAME).$(K8S_SERVICE_NAME).$(MY_POD_NAMESPACE)",
		},
		corev1.EnvVar{
			Name:  "K8S_HOSTNAME_SUFFIX",
			Value: ".$(K8S_SERVICE_NAME).$(MY_POD_NAMESPACE)",
		},
	)

	return env
}

// buildContainerPorts builds the port list for the RabbitMQ container
func buildContainerPorts(r *rabbitmqv1.RabbitMq) []corev1.ContainerPort {
	ports := []corev1.ContainerPort{
		{
			Name:          "epmd",
			ContainerPort: EPMDPort,
			Protocol:      corev1.ProtocolTCP,
		},
	}

	tlsEnabled := r.Spec.TLS.SecretName != ""
	disableNonTLS := r.Spec.TLS.DisableNonTLSListeners

	// AMQP ports
	if !tlsEnabled || !disableNonTLS {
		ports = append(ports, corev1.ContainerPort{
			Name:          "amqp",
			ContainerPort: AMQPPort,
			Protocol:      corev1.ProtocolTCP,
		})
	}
	if tlsEnabled {
		ports = append(ports, corev1.ContainerPort{
			Name:          "amqps",
			ContainerPort: AMQPSPort,
			Protocol:      corev1.ProtocolTCP,
		})
	}

	// Management ports
	if !tlsEnabled || !disableNonTLS {
		ports = append(ports, corev1.ContainerPort{
			Name:          "management",
			ContainerPort: ManagementPort,
			Protocol:      corev1.ProtocolTCP,
		})
	}
	if tlsEnabled {
		ports = append(ports, corev1.ContainerPort{
			Name:          "management-tls",
			ContainerPort: ManagementTLSPort,
			Protocol:      corev1.ProtocolTCP,
		})
	}

	// Prometheus ports
	if !tlsEnabled || !disableNonTLS {
		ports = append(ports, corev1.ContainerPort{
			Name:          "prometheus",
			ContainerPort: PrometheusPort,
			Protocol:      corev1.ProtocolTCP,
		})
	}
	if tlsEnabled {
		ports = append(ports, corev1.ContainerPort{
			Name:          "prometheus-tls",
			ContainerPort: PrometheusTLSPort,
			Protocol:      corev1.ProtocolTCP,
		})
	}

	return ports
}

// buildReadinessProbe builds the readiness probe for RabbitMQ
func buildReadinessProbe(r *rabbitmqv1.RabbitMq) *corev1.Probe {
	portName := "amqp"
	if r.Spec.TLS.SecretName != "" {
		portName = "amqps"
	}

	return &corev1.Probe{
		ProbeHandler: corev1.ProbeHandler{
			TCPSocket: &corev1.TCPSocketAction{
				Port: intstr.FromString(portName),
			},
		},
		InitialDelaySeconds: 10,
		PeriodSeconds:       10,
		TimeoutSeconds:      5,
		FailureThreshold:    3,
	}
}

// buildLifecycle builds the lifecycle hooks for graceful shutdown
// matching the old rabbitmq-cluster-operator's preStop hook
func buildLifecycle() *corev1.Lifecycle {
	return &corev1.Lifecycle{
		PreStop: &corev1.LifecycleHandler{
			Exec: &corev1.ExecAction{
				Command: []string{
					"/bin/bash",
					"-c",
					`if [ ! -z "$(cat /etc/pod-info/skipPreStopChecks)" ]; then exit 0; fi; rabbitmq-upgrade await_online_quorum_plus_one -t 604800 && rabbitmq-upgrade await_online_synchronized_mirror -t 604800 || true && rabbitmq-upgrade drain -t 604800`,
				},
			},
		},
	}
}

// buildInitContainer builds the init container for RabbitMQ setup
// matching the old rabbitmq-cluster-operator's init container
func buildInitContainer(r *rabbitmqv1.RabbitMq) corev1.Container {
	return corev1.Container{
		Name:    "setup-container",
		Image:   r.Spec.ContainerImage,
		Command: []string{"sh", "-c"},
		Args: []string{
			`cp /tmp/erlang-cookie-secret/.erlang.cookie /var/lib/rabbitmq/.erlang.cookie && chmod 600 /var/lib/rabbitmq/.erlang.cookie ; cp /tmp/rabbitmq-plugins/enabled_plugins /operator/enabled_plugins ; echo '[default]' > /var/lib/rabbitmq/.rabbitmqadmin.conf && sed -e 's/default_user/username/' -e 's/default_pass/password/' /tmp/default_user.conf >> /var/lib/rabbitmq/.rabbitmqadmin.conf && chmod 600 /var/lib/rabbitmq/.rabbitmqadmin.conf ; sleep 30`,
		},
		Resources: corev1.ResourceRequirements{
			Limits: corev1.ResourceList{
				corev1.ResourceCPU:    resource.MustParse("20m"),
				corev1.ResourceMemory: resource.MustParse("64Mi"),
			},
			Requests: corev1.ResourceList{
				corev1.ResourceCPU:    resource.MustParse("20m"),
				corev1.ResourceMemory: resource.MustParse("64Mi"),
			},
		},
		VolumeMounts: getInitContainerVolumeMounts(r),
	}
}

// buildWipeDataInitContainer builds the init container that wipes persistent data
// during version upgrades. Uses a version-specific marker file to prevent
// re-wiping on pod restarts.
func buildWipeDataInitContainer(r *rabbitmqv1.RabbitMq, targetVersion string) corev1.Container {
	wipeScript := fmt.Sprintf(`set -ex
WIPE_DIR="/var/lib/rabbitmq"
MARKER="${WIPE_DIR}/.operator-wipe-%s"

if [ -f "$MARKER" ]; then
  echo "Data already wiped for version %s, skipping..."
  exit 0
fi

echo "Wiping RabbitMQ data in $WIPE_DIR for upgrade to version %s..."
rm -rf "${WIPE_DIR:?}"/*
rm -rf "${WIPE_DIR:?}"/.[!.]*
touch "$MARKER"
echo "Data wipe complete for version %s (marker: $MARKER)"
ls -la "$WIPE_DIR"
`, targetVersion, targetVersion, targetVersion, targetVersion)

	return corev1.Container{
		Name:       "wipe-data",
		Image:      r.Spec.ContainerImage,
		WorkingDir: "/var/lib/rabbitmq",
		Command:    []string{"/bin/sh"},
		Args:       []string{"-c", wipeScript},
		VolumeMounts: []corev1.VolumeMount{
			{
				Name:      "persistence",
				MountPath: "/var/lib/rabbitmq",
			},
		},
	}
}
