package rabbitmq

import (
	"testing"

	rabbitmqv1beta1 "github.com/openstack-k8s-operators/infra-operator/apis/rabbitmq/v1beta1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/utils/ptr"
)

func TestShouldEnableProxy(t *testing.T) {
	r := &Reconciler{}

	tests := []struct {
		name     string
		instance func() *rabbitmqv1beta1.RabbitMq
		want     bool
	}{
		{
			name: "should enable proxy when ProxyRequired status is true",
			instance: func() *rabbitmqv1beta1.RabbitMq {
				return &rabbitmqv1beta1.RabbitMq{
					Spec: rabbitmqv1beta1.RabbitMqSpec{
						RabbitMqSpecCore: rabbitmqv1beta1.RabbitMqSpecCore{
							QueueType: ptr.To(rabbitmqv1beta1.QueueTypeQuorum),
						},
					},
					Status: rabbitmqv1beta1.RabbitMqStatus{
						ProxyRequired: true,
					},
				}
			},
			want: true,
		},
		{
			name: "should NOT enable proxy when ProxyRequired is false",
			instance: func() *rabbitmqv1beta1.RabbitMq {
				return &rabbitmqv1beta1.RabbitMq{
					Spec: rabbitmqv1beta1.RabbitMqSpec{
						RabbitMqSpecCore: rabbitmqv1beta1.RabbitMqSpecCore{
							QueueType: ptr.To(rabbitmqv1beta1.QueueTypeQuorum),
						},
					},
					Status: rabbitmqv1beta1.RabbitMqStatus{
						ProxyRequired: false,
					},
				}
			},
			want: false,
		},
		{
			name: "should NOT enable proxy when clients-reconfigured annotation is set",
			instance: func() *rabbitmqv1beta1.RabbitMq {
				return &rabbitmqv1beta1.RabbitMq{
					ObjectMeta: metav1.ObjectMeta{
						Annotations: map[string]string{
							rabbitmqv1beta1.AnnotationClientsReconfigured: "true",
						},
					},
					Spec: rabbitmqv1beta1.RabbitMqSpec{
						RabbitMqSpecCore: rabbitmqv1beta1.RabbitMqSpecCore{
							QueueType: ptr.To(rabbitmqv1beta1.QueueTypeQuorum),
						},
					},
					Status: rabbitmqv1beta1.RabbitMqStatus{
						ProxyRequired: true,
					},
				}
			},
			want: false,
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			instance := tt.instance()
			got := r.shouldEnableProxy(instance)
			if got != tt.want {
				t.Errorf("shouldEnableProxy() = %v, want %v", got, tt.want)
			}
		})
	}
}

func TestBuildProxySidecarContainer(t *testing.T) {
	tests := []struct {
		name        string
		instance    *rabbitmqv1beta1.RabbitMq
		ipv6        bool
		wantPort    int32
		wantListen  string
		wantBackend string
		wantTLS     bool
	}{
		{
			name: "plain mode IPv4",
			instance: &rabbitmqv1beta1.RabbitMq{
				Spec: rabbitmqv1beta1.RabbitMqSpec{
					ContainerImage: "test-image:latest",
				},
			},
			ipv6:        false,
			wantPort:    5672,
			wantListen:  "0.0.0.0:5672",
			wantBackend: "localhost:5673",
			wantTLS:     false,
		},
		{
			name: "TLS mode IPv4",
			instance: &rabbitmqv1beta1.RabbitMq{
				Spec: rabbitmqv1beta1.RabbitMqSpec{
					ContainerImage: "test-image:latest",
					RabbitMqSpecCore: rabbitmqv1beta1.RabbitMqSpecCore{
						TLS: rabbitmqv1beta1.RabbitMQTLSSpec{
							SecretName: "tls-secret",
						},
					},
				},
			},
			ipv6:        false,
			wantPort:    5671,
			wantListen:  "0.0.0.0:5671",
			wantBackend: "localhost:5673",
			wantTLS:     true,
		},
		{
			name: "plain mode IPv6",
			instance: &rabbitmqv1beta1.RabbitMq{
				Spec: rabbitmqv1beta1.RabbitMqSpec{
					ContainerImage: "test-image:latest",
				},
			},
			ipv6:        true,
			wantPort:    5672,
			wantListen:  "[::]:5672",
			wantBackend: "[::1]:5673",
			wantTLS:     false,
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			container := BuildProxySidecarContainer(tt.instance, tt.ipv6)

			if container.Name != ProxyContainerName {
				t.Errorf("container name = %q, want %q", container.Name, ProxyContainerName)
			}

			if container.Image != tt.instance.Spec.ContainerImage {
				t.Errorf("container image = %q, want %q", container.Image, tt.instance.Spec.ContainerImage)
			}

			if len(container.Ports) != 1 || container.Ports[0].ContainerPort != tt.wantPort {
				t.Errorf("container port = %v, want %d", container.Ports, tt.wantPort)
			}

			// Check args contain expected listen and backend addresses
			foundListen := false
			foundBackend := false
			for i, arg := range container.Args {
				if arg == "--listen" && i+1 < len(container.Args) && container.Args[i+1] == tt.wantListen {
					foundListen = true
				}
				if arg == "--backend" && i+1 < len(container.Args) && container.Args[i+1] == tt.wantBackend {
					foundBackend = true
				}
			}
			if !foundListen {
				t.Errorf("expected --listen %s in args %v", tt.wantListen, container.Args)
			}
			if !foundBackend {
				t.Errorf("expected --backend %s in args %v", tt.wantBackend, container.Args)
			}

			// Check TLS args
			hasTLSCert := false
			for _, arg := range container.Args {
				if arg == "--tls-cert" {
					hasTLSCert = true
				}
			}
			if hasTLSCert != tt.wantTLS {
				t.Errorf("TLS cert arg present = %v, want %v", hasTLSCert, tt.wantTLS)
			}

			// Check TLS volume mount
			if tt.wantTLS {
				foundTLSMount := false
				for _, mount := range container.VolumeMounts {
					if mount.Name == "rabbitmq-tls" {
						foundTLSMount = true
					}
				}
				if !foundTLSMount {
					t.Error("expected rabbitmq-tls volume mount when TLS enabled")
				}
			}
		})
	}
}
