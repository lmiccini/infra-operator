package rabbitmq

import (
	"context"
	_ "embed"
	"fmt"

	instancehav1beta1 "github.com/openstack-k8s-operators/infra-operator/apis/instanceha/v1beta1"
	rabbitmqv1beta1 "github.com/openstack-k8s-operators/infra-operator/apis/rabbitmq/v1beta1"
	"github.com/openstack-k8s-operators/lib-common/modules/common/helper"
	rabbitmqv2 "github.com/rabbitmq/cluster-operator/v2/api/v1beta1"
	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/api/resource"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/util/intstr"
	"k8s.io/utils/ptr"
	"sigs.k8s.io/controller-runtime/pkg/controller/controllerutil"
)

// proxyScript contains the embedded proxy.py content
//
//go:embed data/proxy.py
var proxyScript string

const (
	// Proxy container name
	proxyContainerName = "amqp-proxy"

	// RabbitMQ backend port (proxy will forward to this)
	// RabbitMQ will listen on this port on localhost without TLS
	// Using non-standard port to avoid conflicts with proxy frontend
	rabbitmqBackendPort = 5673

	// Proxy listen port with TLS (standard AMQP TLS port)
	proxyListenPortTLS = 5671

	// Proxy listen port without TLS (standard AMQP port)
	proxyListenPortPlain = 5672
)

// ensureProxyConfigMap creates or updates the ConfigMap containing the proxy script
func (r *Reconciler) ensureProxyConfigMap(
	ctx context.Context,
	instance *rabbitmqv1beta1.RabbitMq,
	helper *helper.Helper,
) error {
	Log := r.GetLogger(ctx)

	configMap := &corev1.ConfigMap{
		ObjectMeta: metav1.ObjectMeta{
			Name:      instance.Name + "-proxy-script",
			Namespace: instance.Namespace,
		},
	}

	_, err := controllerutil.CreateOrUpdate(ctx, r.Client, configMap, func() error {
		configMap.Data = map[string]string{
			"proxy.py": proxyScript,
		}

		// Set owner reference to RabbitMq CR (not RabbitMQCluster) so ConfigMap persists
		// during pod termination and is garbage collected when CR is deleted
		err := controllerutil.SetControllerReference(instance, configMap, helper.GetScheme())
		if err != nil {
			return err
		}

		return nil
	})

	if err != nil {
		Log.Error(err, "Failed to create/update proxy ConfigMap")
		return err
	}

	Log.Info("Proxy ConfigMap ensured", "configmap", configMap.Name)
	return nil
}

// addProxySidecar adds the AMQP proxy sidecar to the RabbitMQCluster spec
// and configures RabbitMQ to listen on the backend port.
// This allows non-durable clients to work with durable quorum queues.
//
// NOTE: ConfigureCluster() must be called before this function.
// It guarantees the Override.StatefulSet structure is fully initialized
// and rebuilds the spec from scratch each reconcile, so there's no need
// to check for existing containers/volumes or nil-check the structure.
func (r *Reconciler) addProxySidecar(
	ctx context.Context,
	instance *rabbitmqv1beta1.RabbitMq,
	cluster *rabbitmqv2.RabbitmqCluster,
	IPv6Enabled bool,
) {
	Log := r.GetLogger(ctx)

	podSpec := cluster.Spec.Override.StatefulSet.Spec.Template.Spec

	// Add proxy sidecar container
	podSpec.Containers = append(podSpec.Containers, r.buildProxySidecarContainer(instance, IPv6Enabled))

	// Update RabbitMQ container's readiness probe to check the backend port.
	// We use an exec probe instead of TCPSocket because RabbitMQ listens on loopback only,
	// and TCPSocket probes run from the kubelet (node context), not the pod network namespace.
	loopbackAddr := "127.0.0.1"
	if IPv6Enabled {
		loopbackAddr = "::1"
	}
	for i, container := range podSpec.Containers {
		if container.Name == "rabbitmq" {
			podSpec.Containers[i].ReadinessProbe = &corev1.Probe{
				ProbeHandler: corev1.ProbeHandler{
					Exec: &corev1.ExecAction{
						Command: []string{
							"/bin/sh",
							"-c",
							fmt.Sprintf("timeout 1 bash -c '</dev/tcp/%s/%d'", loopbackAddr, rabbitmqBackendPort),
						},
					},
				},
				InitialDelaySeconds: 10,
				TimeoutSeconds:      5,
				PeriodSeconds:       10,
				SuccessThreshold:    1,
				FailureThreshold:    3,
			}
			break
		}
	}

	// Add volume for proxy script
	podSpec.Volumes = append(podSpec.Volumes, corev1.Volume{
		Name: "proxy-script",
		VolumeSource: corev1.VolumeSource{
			ConfigMap: &corev1.ConfigMapVolumeSource{
				LocalObjectReference: corev1.LocalObjectReference{
					Name: instance.Name + "-proxy-script",
				},
				DefaultMode: ptr.To[int32](0555), // Executable
			},
		},
	})

	// Configure RabbitMQ to listen on localhost:backendPort only.
	// The proxy listens on the standard port and forwards to the backend.
	listenerConfig := fmt.Sprintf(`
# Proxy mode: RabbitMQ listens on localhost only, proxy handles client connections
listeners.tcp.1 = %s:%d
listeners.ssl.1 = %s:5674
listeners.ssl.default = %s:5674
`, loopbackAddr, rabbitmqBackendPort, loopbackAddr, loopbackAddr)
	cluster.Spec.Rabbitmq.AdditionalConfig = listenerConfig + cluster.Spec.Rabbitmq.AdditionalConfig

	Log.Info("Added proxy sidecar to RabbitMQ cluster",
		"backendPort", rabbitmqBackendPort)
}

// buildProxySidecarContainer builds the proxy sidecar container spec
func (r *Reconciler) buildProxySidecarContainer(instance *rabbitmqv1beta1.RabbitMq, IPv6Enabled bool) corev1.Container {
	// Determine proxy listen port based on TLS configuration
	listenPort := proxyListenPortPlain
	if instance.Spec.TLS.SecretName != "" {
		listenPort = proxyListenPortTLS
	}

	// Build proxy command args
	// Use appropriate addresses for IPv4 vs IPv6
	listenAddr := fmt.Sprintf("0.0.0.0:%d", listenPort)
	backendAddr := fmt.Sprintf("localhost:%d", rabbitmqBackendPort)
	if IPv6Enabled {
		listenAddr = fmt.Sprintf("[::]:%d", listenPort)
		backendAddr = fmt.Sprintf("[::1]:%d", rabbitmqBackendPort)
	}
	args := []string{
		"--backend", backendAddr,
		"--listen", listenAddr,
		"--log-level", "INFO",
		"--stats-interval", "300", // Print stats every 5 minutes
	}

	// Add TLS args if TLS is enabled
	if instance.Spec.TLS.SecretName != "" {
		args = append(args,
			"--tls-cert", "/etc/rabbitmq-tls/tls.crt",
			"--tls-key", "/etc/rabbitmq-tls/tls.key",
		)
		// Note: We don't pass --tls-ca because:
		// 1. The proxy doesn't require client certificates (mutual TLS)
		// 2. OpenStack clients connect with server-side TLS only
		// 3. Adding --tls-ca would enable CERT_REQUIRED mode, breaking connections
	}

	// Note: We don't use --backend-tls because the proxy connects to
	// RabbitMQ via localhost (same pod) on a non-TLS port

	container := corev1.Container{
		Name:  proxyContainerName,
		Image: instancehav1beta1.InstanceHaContainerImage,
		Command: []string{
			"python3",
			"/scripts/proxy.py",
		},
		Args: args,
		Ports: []corev1.ContainerPort{
			{
				ContainerPort: int32(listenPort),
				Name:          "amqp",
				Protocol:      corev1.ProtocolTCP,
			},
		},
		Env: []corev1.EnvVar{
			{
				Name:  "PYTHONUNBUFFERED",
				Value: "1",
			},
		},
		VolumeMounts: []corev1.VolumeMount{
			{
				Name:      "proxy-script",
				MountPath: "/scripts",
				ReadOnly:  true,
			},
		},
		Resources: corev1.ResourceRequirements{
			Requests: corev1.ResourceList{
				corev1.ResourceMemory: resource.MustParse("512Mi"),
				corev1.ResourceCPU:    resource.MustParse("500m"),
			},
			Limits: corev1.ResourceList{
				corev1.ResourceMemory: resource.MustParse("2Gi"),
				corev1.ResourceCPU:    resource.MustParse("2000m"),
			},
		},
		LivenessProbe: &corev1.Probe{
			ProbeHandler: corev1.ProbeHandler{
				TCPSocket: &corev1.TCPSocketAction{
					Port: intstr.FromInt32(int32(listenPort)),
				},
			},
			InitialDelaySeconds: 10,
			PeriodSeconds:       30,
			TimeoutSeconds:      3,
			FailureThreshold:    3,
		},
		ReadinessProbe: &corev1.Probe{
			ProbeHandler: corev1.ProbeHandler{
				TCPSocket: &corev1.TCPSocketAction{
					Port: intstr.FromInt32(int32(listenPort)),
				},
			},
			InitialDelaySeconds: 5,
			PeriodSeconds:       10,
			TimeoutSeconds:      3,
			FailureThreshold:    3,
		},
		SecurityContext: &corev1.SecurityContext{
			RunAsNonRoot:             ptr.To(true),
			AllowPrivilegeEscalation: ptr.To(false),
			Capabilities: &corev1.Capabilities{
				Drop: []corev1.Capability{"ALL"},
			},
		},
	}

	// Mount TLS certificates if TLS is enabled
	// Only mount the cert/key, not the CA (we don't need it for server-side TLS)
	if instance.Spec.TLS.SecretName != "" {
		container.VolumeMounts = append(container.VolumeMounts, corev1.VolumeMount{
			Name:      "rabbitmq-tls",
			MountPath: "/etc/rabbitmq-tls",
			ReadOnly:  true,
		})
	}

	return container
}

// shouldEnableProxy determines if the proxy sidecar should be enabled.
//
// The proxy is enabled when Status.ProxyRequired is true, which is set by
// the main reconciler during 3.x → 4.x upgrades with Quorum migration.
// It persists after the upgrade completes and is only cleared when
// AnnotationClientsReconfigured is set to "true".
func (r *Reconciler) shouldEnableProxy(instance *rabbitmqv1beta1.RabbitMq) bool {
	// Check if clients have been reconfigured - if so, no proxy needed
	if instance.Annotations != nil {
		if configured, ok := instance.Annotations[rabbitmqv1beta1.AnnotationClientsReconfigured]; ok && configured == "true" {
			return false
		}
	}

	// ProxyRequired is set by the main reconciler during 3.x → 4.x upgrades
	// and persists across reconciliations until clients-reconfigured is set.
	return instance.Status.ProxyRequired
}
