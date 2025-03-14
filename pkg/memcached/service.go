package memcached

import (
	memcachedv1 "github.com/openstack-k8s-operators/infra-operator/apis/memcached/v1beta1"
	common "github.com/openstack-k8s-operators/lib-common/modules/common"
	labels "github.com/openstack-k8s-operators/lib-common/modules/common/labels"
	service "github.com/openstack-k8s-operators/lib-common/modules/common/service"
	corev1 "k8s.io/api/core/v1"
)

// HeadlessService exposes all memcached replicas for a memcached CR
func HeadlessService(m *memcachedv1.Memcached) *corev1.Service {
	labels := labels.GetLabels(m, "memcached", map[string]string{
		"app":                m.GetName(),
		common.OwnerSelector: "infra-operator",
		"cr":                 m.GetName(),
		common.AppSelector:   m.GetName(),
	})

	ports := []corev1.ServicePort{
		{Name: "memcached-tls", Protocol: "TCP", Port: MemcachedTLSPort},
		{Name: "memcached", Protocol: "TCP", Port: MemcachedPort},
	}

	if m.Spec.TLS.MTLS.SslVerifyMode == "Require" {
		ports = []corev1.ServicePort{{Name: "memcached-tls", Protocol: "TCP", Port: MemcachedTLSPort}}
	}

	details := &service.GenericServiceDetails{
		Name:      m.GetName(),
		Namespace: m.GetNamespace(),
		Labels:    labels,
		Selector: map[string]string{
			common.AppSelector: m.GetName(),
			"app":              m.GetName(),
		},
		Ports:     ports,
		ClusterIP: "None",
	}

	svc := service.GenericService(details)
	return svc
}
