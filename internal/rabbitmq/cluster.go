// Package rabbitmq provides utilities for configuring and managing RabbitMQ clusters
package rabbitmq

import (
	"fmt"
	"regexp"
	"strings"

	rabbitmqv1 "github.com/openstack-k8s-operators/infra-operator/apis/rabbitmq/v1beta1"
	corev1 "k8s.io/api/core/v1"
)

// validVersionPattern matches version strings like "3.9", "3.13.1", "4.2"
var validVersionPattern = regexp.MustCompile(`^\d+\.\d+(\.\d+)?$`)

// Version is an alias for the canonical version type in the apis package.
type Version = rabbitmqv1.Version

// ParseRabbitMQVersion delegates to the canonical implementation in the apis package.
var ParseRabbitMQVersion = rabbitmqv1.ParseRabbitMQVersion

// Is3xTo4xUpgrade delegates to the canonical implementation in the apis package.
var Is3xTo4xUpgrade = rabbitmqv1.Is3xTo4xUpgrade

// IsVersion4OrLater delegates to the canonical implementation in the apis package.
var IsVersion4OrLater = rabbitmqv1.IsVersion4OrLater

// TLSVersionsForRabbitMQ returns the TLS versions string based on the RabbitMQ version and FIPS mode.
// RabbitMQ 4.x+ enables TLS 1.2+1.3; 3.x with FIPS also enables both; 3.x without FIPS uses 1.2 only
// (workaround for OSPRH-20331 partitioning issue).
func TLSVersionsForRabbitMQ(rabbitmqVersion string, fipsEnabled bool) string {
	if IsVersion4OrLater(rabbitmqVersion) {
		return "['tlsv1.2','tlsv1.3']"
	}
	if fipsEnabled {
		return "['tlsv1.2','tlsv1.3']"
	}
	return "['tlsv1.2']"
}

// RequiresStorageWipe determines if a version change requires full storage wipe.
// Returns true for major/minor version changes, false for same version or patch changes.
func RequiresStorageWipe(fromStr, toStr string) (bool, error) {
	if fromStr == toStr {
		return false, nil
	}
	from, err := ParseRabbitMQVersion(fromStr)
	if err != nil {
		return false, fmt.Errorf("failed to parse current version %q: %w", fromStr, err)
	}
	to, err := ParseRabbitMQVersion(toStr)
	if err != nil {
		return false, fmt.Errorf("failed to parse target version %q: %w", toStr, err)
	}
	if from.Major == to.Major && from.Minor == to.Minor {
		return false, nil
	}
	return true, nil
}

// ValidVersionPattern checks if a version string is well-formed (e.g. "3.9", "4.2.1")
func ValidVersionPattern(s string) bool {
	return validVersionPattern.MatchString(s)
}

// BuildRabbitMQConfig builds the RabbitMQ configuration environment variables.
// Always sets RABBITMQ_SERVER_ADDITIONAL_ERL_ARGS and RABBITMQ_CTL_ERL_ARGS
// to match the original infra-operator behavior with the cluster-operator.
// The inetrc file ensures Erlang uses the correct IP family for DNS resolution,
// and -proto_dist sets the inter-node communication protocol.
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
		Value: strings.TrimSpace(fmt.Sprintf(
			"-kernel inetrc '/etc/rabbitmq/erl_inetrc' -proto_dist %s_%s %s %s",
			inetFamily,
			inetProtocol,
			tlsArgs,
			fipsArgs,
		)),
	}, corev1.EnvVar{
		Name:  "RABBITMQ_CTL_ERL_ARGS",
		Value: strings.TrimSpace(fmt.Sprintf("-proto_dist %s_%s %s", inetFamily, inetProtocol, tlsArgs)),
	})

	return envVars, nil
}
