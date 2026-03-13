// Package rabbitmq provides utilities for configuring and managing RabbitMQ clusters
package rabbitmq

import (
	"fmt"

	rabbitmqv1 "github.com/openstack-k8s-operators/infra-operator/apis/rabbitmq/v1beta1"
	corev1 "k8s.io/api/core/v1"
)

// BuildRabbitMQConfig builds the RabbitMQ configuration environment variables
func BuildRabbitMQConfig(
	r *rabbitmqv1.RabbitMq,
	IPv6Enabled bool,
	fipsEnabled bool,
) ([]corev1.EnvVar, error) {
	var envVars []corev1.EnvVar

	inetFamily := "inet"
	inetProtocol := "tcp"
	tlsArgs := ""
	fipsArgs := ""
	if IPv6Enabled {
		inetFamily = "inet6"
	}

	if r.Spec.TLS.SecretName != "" {
		inetProtocol = "tls"
		tlsArgs = "-ssl_dist_optfile /etc/rabbitmq/inter-node-tls.config"
		if fipsEnabled {
			fipsArgs = "-crypto fips_mode true"
		}
	}

	envVars = append(envVars, corev1.EnvVar{
		Name: "RABBITMQ_SERVER_ADDITIONAL_ERL_ARGS",
		Value: fmt.Sprintf(
			"-kernel inetrc '/etc/rabbitmq/erl_inetrc' -proto_dist %s_%s %s %s",
			inetFamily,
			inetProtocol,
			tlsArgs,
			fipsArgs,
		),
	}, corev1.EnvVar{
		Name:  "RABBITMQ_CTL_ERL_ARGS",
		Value: fmt.Sprintf("-proto_dist %s_%s %s", inetFamily, inetProtocol, tlsArgs),
	})

	return envVars, nil
}
