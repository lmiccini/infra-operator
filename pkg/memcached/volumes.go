package memcached

import (
	"fmt"

	memcachedv1 "github.com/openstack-k8s-operators/infra-operator/apis/memcached/v1beta1"
	"github.com/openstack-k8s-operators/lib-common/modules/common/tls"
	corev1 "k8s.io/api/core/v1"
)

const (
	MemcachedCertPrefix = "memcached"
)

func getVolumes(m *memcachedv1.Memcached) []corev1.Volume {
	vols := []corev1.Volume{
		{
			Name: "kolla-config",
			VolumeSource: corev1.VolumeSource{
				ConfigMap: &corev1.ConfigMapVolumeSource{
					LocalObjectReference: corev1.LocalObjectReference{
						Name: fmt.Sprintf("%s-config-data", m.Name),
					},
					Items: []corev1.KeyToPath{
						{
							Key:  "config.json",
							Path: "config.json",
						},
					},
				},
			},
		},
		{
			Name: "config-data",
			VolumeSource: corev1.VolumeSource{
				ConfigMap: &corev1.ConfigMapVolumeSource{
					LocalObjectReference: corev1.LocalObjectReference{
						Name: fmt.Sprintf("%s-config-data", m.Name),
					},
					Items: []corev1.KeyToPath{
						{
							Key:  "memcached",
							Path: "etc/sysconfig/memcached",
						},
					},
				},
			},
		},
	}

	if m.Spec.TLS.Enabled() {
		svc := tls.Service{
			SecretName: *m.Spec.TLS.GenericService.SecretName,
			CertMount:  nil,
			KeyMount:   nil,
			CaMount:    nil,
		}
		vols = append(vols, svc.CreateVolume(MemcachedCertPrefix))
		if m.Spec.TLS.Ca.CaBundleSecretName != "" {
			vols = append(vols, m.Spec.TLS.Ca.CreateVolume())
		}
	}

	if m.Spec.Auth {
		v := []corev1.Volume{
			{
				Name: "sasl-db",
				VolumeSource: corev1.VolumeSource{
					Secret: &corev1.SecretVolumeSource{
						SecretName: m.Spec.AuthSecret,
					},
				},
			},
			{
				Name: "sasl-config",
				VolumeSource: corev1.VolumeSource{
					 ConfigMap: &corev1.ConfigMapVolumeSource{
						 LocalObjectReference: corev1.LocalObjectReference{
							 Name: fmt.Sprintf("%s-sasl-config", m.Name),
						 },
						 Items: []corev1.KeyToPath{
							 {
								  Key:  "memcached.conf",
								  Path: "etc/memcached/memcached.conf",
							 },
						 },
					 },
				},
			},

		}
		vols = append(vols, v...)
	}

	return vols
}

func getVolumeMounts(m *memcachedv1.Memcached) []corev1.VolumeMount {
	vm := []corev1.VolumeMount{{
		MountPath: "/var/lib/kolla/config_files/src",
		ReadOnly:  true,
		Name:      "config-data",
	}, {
		MountPath: "/var/lib/kolla/config_files",
		ReadOnly:  true,
		Name:      "kolla-config",
	}}

	if  m.Spec.Auth {
		vm2 := []corev1.VolumeMount{{
			MountPath: "/etc/memcached/memcached-sasl-db",
			ReadOnly:  true,
			Name:      "sasl-db",
		}, {
			MountPath: "/etc/memcached/memcached.conf",
			ReadOnly:  true,
			Name:      "sasl-conf",
		}}
		vm = append(vm, vm2...)
	}

	if m.Spec.TLS.Enabled() {
		svc := tls.Service{
			SecretName: *m.Spec.TLS.GenericService.SecretName,
			CertMount:  nil,
			KeyMount:   nil,
			CaMount:    nil,
		}
		vm = append(vm, svc.CreateVolumeMounts(MemcachedCertPrefix)...)
		if m.Spec.TLS.Ca.CaBundleSecretName != "" {
			vm = append(vm, m.Spec.TLS.Ca.CreateVolumeMounts(nil)...)
		}
	}

	return vm
}
