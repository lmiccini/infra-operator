package rabbitmq

import (
	"fmt"

	rabbitmqv1 "github.com/openstack-k8s-operators/infra-operator/apis/rabbitmq/v1beta1"
	labels "github.com/openstack-k8s-operators/lib-common/modules/common/labels"
	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/util/intstr"
)

const (
	// Port definitions
	AMQPPort          = 5672
	AMQPSPort         = 5671
	EPMDPort          = 4369
	ManagementPort    = 15672
	ManagementTLSPort = 15671
	PrometheusPort    = 15692
	PrometheusTLSPort = 15691
	ClusterRPCPort    = 25672
	MQTTPort          = 1883
	MQTTTLSPort       = 8883
	STOMPPort         = 61613
	STOMPTLSPort      = 61614
	StreamPort        = 5552
	StreamTLSPort     = 5551
)

// HeadlessService creates the headless service for StatefulSet pod DNS
// matching the old rabbitmq-cluster-operator layout
func HeadlessService(r *rabbitmqv1.RabbitMq) *corev1.Service {
	ls := map[string]string{
		labels.K8sAppComponent: "rabbitmq",
		labels.K8sAppName:      r.Name,
		labels.K8sAppPartOf:    "rabbitmq",
	}
	selector := map[string]string{
		labels.K8sAppName: r.Name,
	}

	ports := []corev1.ServicePort{
		{
			Name:       "epmd",
			Protocol:   corev1.ProtocolTCP,
			Port:       EPMDPort,
			TargetPort: intstr.FromInt32(EPMDPort),
		},
		{
			Name:       "cluster-rpc",
			Protocol:   corev1.ProtocolTCP,
			Port:       ClusterRPCPort,
			TargetPort: intstr.FromInt32(ClusterRPCPort),
		},
	}

	svc := &corev1.Service{
		ObjectMeta: metav1.ObjectMeta{
			Name:      fmt.Sprintf("%s-nodes", r.Name),
			Namespace: r.Namespace,
			Labels:    ls,
		},
		Spec: corev1.ServiceSpec{
			Type:                     corev1.ServiceTypeClusterIP,
			ClusterIP:                "None",
			PublishNotReadyAddresses: true,
			Selector:                 selector,
			Ports:                    ports,
		},
	}

	// Set IPFamilyPolicy if specified
	if r.Spec.Service.IPFamilyPolicy != nil {
		svc.Spec.IPFamilyPolicy = r.Spec.Service.IPFamilyPolicy
	}

	return svc
}

// ClientService creates the client-facing service for RabbitMQ
// matching the old rabbitmq-cluster-operator layout
func ClientService(r *rabbitmqv1.RabbitMq) *corev1.Service {
	ls := map[string]string{
		labels.K8sAppComponent: "rabbitmq",
		labels.K8sAppName:      r.Name,
		labels.K8sAppPartOf:    "rabbitmq",
	}
	selector := map[string]string{
		labels.K8sAppName: r.Name,
	}

	ports := buildClientServicePorts(r)

	serviceType := corev1.ServiceTypeClusterIP
	if r.Spec.Service.Type != "" {
		serviceType = r.Spec.Service.Type
	}

	// Prepare annotations
	annotations := make(map[string]string)
	for k, v := range r.Spec.Service.Annotations {
		annotations[k] = v
	}
	if serviceType == corev1.ServiceTypeLoadBalancer {
		if _, exists := annotations["dnsmasq.network.openstack.org/hostname"]; !exists {
			annotations["dnsmasq.network.openstack.org/hostname"] = fmt.Sprintf("%s.%s.svc", r.Name, r.Namespace)
		}
	}

	svc := &corev1.Service{
		ObjectMeta: metav1.ObjectMeta{
			Name:        r.Name,
			Namespace:   r.Namespace,
			Labels:      ls,
			Annotations: annotations,
		},
		Spec: corev1.ServiceSpec{
			Type:     serviceType,
			Selector: selector,
			Ports:    ports,
		},
	}

	// Set IPFamilyPolicy if specified
	if r.Spec.Service.IPFamilyPolicy != nil {
		svc.Spec.IPFamilyPolicy = r.Spec.Service.IPFamilyPolicy
	}

	return svc
}

// buildClientServicePorts builds the list of ports for the client service
func buildClientServicePorts(r *rabbitmqv1.RabbitMq) []corev1.ServicePort {
	ports := []corev1.ServicePort{}

	tlsEnabled := r.Spec.TLS.SecretName != ""
	disableNonTLS := r.Spec.TLS.DisableNonTLSListeners

	// AMQP port
	if !tlsEnabled || !disableNonTLS {
		ports = append(ports, corev1.ServicePort{
			Name:       "amqp",
			Protocol:   corev1.ProtocolTCP,
			Port:       AMQPPort,
			TargetPort: intstr.FromInt32(AMQPPort),
		})
	}

	// AMQPS port (if TLS enabled)
	if tlsEnabled {
		ports = append(ports, corev1.ServicePort{
			Name:        "amqps",
			AppProtocol: strPtr("amqps"),
			Protocol:    corev1.ProtocolTCP,
			Port:        AMQPSPort,
			TargetPort:  intstr.FromInt32(AMQPSPort),
		})
	}

	// Management port
	if !tlsEnabled || !disableNonTLS {
		ports = append(ports, corev1.ServicePort{
			Name:       "management",
			Protocol:   corev1.ProtocolTCP,
			Port:       ManagementPort,
			TargetPort: intstr.FromInt32(ManagementPort),
		})
	}

	// Management TLS port (if TLS enabled)
	if tlsEnabled {
		ports = append(ports, corev1.ServicePort{
			Name:        "management-tls",
			AppProtocol: strPtr("https"),
			Protocol:    corev1.ProtocolTCP,
			Port:        ManagementTLSPort,
			TargetPort:  intstr.FromInt32(ManagementTLSPort),
		})
	}

	// Prometheus port
	if !tlsEnabled || !disableNonTLS {
		ports = append(ports, corev1.ServicePort{
			Name:       "prometheus",
			Protocol:   corev1.ProtocolTCP,
			Port:       PrometheusPort,
			TargetPort: intstr.FromInt32(PrometheusPort),
		})
	}

	// Prometheus TLS port (if TLS enabled)
	if tlsEnabled {
		ports = append(ports, corev1.ServicePort{
			Name:        "prometheus-tls",
			AppProtocol: strPtr("prometheus.io/metric-tls"),
			Protocol:    corev1.ProtocolTCP,
			Port:        PrometheusTLSPort,
			TargetPort:  intstr.FromInt32(PrometheusTLSPort),
		})
	}

	return ports
}

func strPtr(s string) *string {
	return &s
}
