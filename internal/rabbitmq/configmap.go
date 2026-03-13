package rabbitmq

import (
	"fmt"
	"strings"

	rabbitmqv1 "github.com/openstack-k8s-operators/infra-operator/apis/rabbitmq/v1beta1"
	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
)

// GeneratePluginsConfigMap generates the plugins ConfigMap (rabbitmq-plugins-conf)
func GeneratePluginsConfigMap(r *rabbitmqv1.RabbitMq) *corev1.ConfigMap {
	return &corev1.ConfigMap{
		ObjectMeta: metav1.ObjectMeta{
			Name:      fmt.Sprintf("%s-plugins-conf", r.Name),
			Namespace: r.Namespace,
		},
		Data: map[string]string{
			"enabled_plugins": "[rabbitmq_peer_discovery_k8s,rabbitmq_prometheus,rabbitmq_management].",
		},
	}
}

// GenerateServerConfigMap generates the server configuration ConfigMap (rabbitmq-server-conf)
func GenerateServerConfigMap(
	r *rabbitmqv1.RabbitMq,
	IPv6Enabled bool,
	fipsEnabled bool,
) *corev1.ConfigMap {
	operatorDefaults := buildOperatorDefaults(r, IPv6Enabled)
	userConfig := r.Spec.Config.AdditionalConfig
	advancedConfig := buildAdvancedConfig(r, IPv6Enabled, fipsEnabled)

	erlInetrc := ""
	if IPv6Enabled {
		erlInetrc = "{inet6,true}.\n"
	}

	return &corev1.ConfigMap{
		ObjectMeta: metav1.ObjectMeta{
			Name:      fmt.Sprintf("%s-server-conf", r.Name),
			Namespace: r.Namespace,
		},
		Data: map[string]string{
			"operatorDefaults.conf":         operatorDefaults,
			"userDefinedConfiguration.conf": userConfig,
			"advanced.config":               advancedConfig,
			"erl_inetrc":                    erlInetrc,
		},
	}
}

// GenerateConfigDataConfigMap generates the config-data ConfigMap for inter-node TLS
// This is only needed when TLS is enabled
func GenerateConfigDataConfigMap(
	r *rabbitmqv1.RabbitMq,
	fipsEnabled bool,
) *corev1.ConfigMap {
	cm := &corev1.ConfigMap{
		ObjectMeta: metav1.ObjectMeta{
			Name:      fmt.Sprintf("%s-config-data", r.Name),
			Namespace: r.Namespace,
		},
		Data: map[string]string{},
	}

	if r.Spec.TLS.SecretName != "" {
		cm.Data["inter_node_tls.config"] = buildInterNodeTLSConfig(fipsEnabled)
	}

	return cm
}

func buildOperatorDefaults(r *rabbitmqv1.RabbitMq, IPv6Enabled bool) string {
	var config []string

	config = append(config, "queue_master_locator                       = min-masters")
	config = append(config, "disk_free_limit.absolute                   = 2GB")
	config = append(config, "cluster_partition_handling                 = pause_minority")
	config = append(config, "cluster_formation.peer_discovery_backend   = rabbit_peer_discovery_k8s")
	config = append(config, "cluster_formation.k8s.host                 = kubernetes.default")
	config = append(config, "cluster_formation.k8s.address_type         = hostname")
	config = append(config, fmt.Sprintf("cluster_formation.target_cluster_size_hint = %d", getReplicaCount(r)))
	config = append(config, fmt.Sprintf("cluster_name                               = %s", r.Name))
	config = append(config, "auth_mechanisms.1                          = PLAIN")
	config = append(config, "auth_mechanisms.2                          = AMQPLAIN")
	config = append(config, "log.console                                = true")
	config = append(config, "log.console.level                          = info")

	// TLS listener configuration
	if r.Spec.TLS.SecretName != "" {
		config = append(config, "listeners.ssl.default                      = 5671")
		config = append(config, "management.ssl.port                        = 15671")
		config = append(config, "prometheus.ssl.port                        = 15691")
		if r.Spec.TLS.DisableNonTLSListeners {
			config = append(config, "listeners.tcp                              = none")
		}
	}

	return strings.Join(config, "\n") + "\n"
}

func getReplicaCount(r *rabbitmqv1.RabbitMq) int32 {
	if r.Spec.Replicas != nil {
		return *r.Spec.Replicas
	}
	return 1
}

func buildAdvancedConfig(r *rabbitmqv1.RabbitMq, IPv6Enabled bool, fipsEnabled bool) string {
	// If user provided advanced config, use it
	if r.Spec.Config.AdvancedConfig != "" {
		return r.Spec.Config.AdvancedConfig
	}

	// If TLS is not enabled, return valid empty Erlang config
	// (an empty string causes RabbitMQ to fail parsing the file)
	if r.Spec.TLS.SecretName == "" {
		return "[].\n"
	}

	tlsVersions := "['tlsv1.2']"
	if fipsEnabled {
		tlsVersions = "['tlsv1.2','tlsv1.3']"
	}

	return fmt.Sprintf(`[
{ssl, [{protocol_version, %s}]},
{rabbit, [
{ssl_options, [
  {cacertfile,"/etc/rabbitmq-tls/ca.crt"},
  {certfile,"/etc/rabbitmq-tls/tls.crt"},
  {keyfile,"/etc/rabbitmq-tls/tls.key"},
  {depth,1},
  {secure_renegotiate,true},
  {reuse_sessions,true},
  {honor_cipher_order,false},
  {honor_ecc_order,false},
  {verify,verify_none},
  {fail_if_no_peer_cert,false},
  {versions, %s}
]}
]},
{rabbitmq_management, [
{ssl_config, [
  {ip,"::"},
  {cacertfile,"/etc/rabbitmq-tls/ca.crt"},
  {certfile,"/etc/rabbitmq-tls/tls.crt"},
  {keyfile,"/etc/rabbitmq-tls/tls.key"},
  {depth,1},
  {secure_renegotiate,true},
  {reuse_sessions,true},
  {honor_cipher_order,false},
  {honor_ecc_order,false},
  {verify,verify_none},
  {fail_if_no_peer_cert,false},
  {versions, %s}
]}
]},
{client, [
{cacertfile, "/etc/rabbitmq-tls/ca.crt"},
{verify,verify_peer},
{secure_renegotiate,true},
{versions, %s}
]}
].
`, tlsVersions, tlsVersions, tlsVersions, tlsVersions)
}

func buildInterNodeTLSConfig(fipsEnabled bool) string {
	tlsVersions := "['tlsv1.2']"
	if fipsEnabled {
		tlsVersions = "['tlsv1.2','tlsv1.3']"
	}

	return fmt.Sprintf(`[
  {server, [
    {cacertfile, "/etc/rabbitmq-tls/ca.crt"},
    {certfile, "/etc/rabbitmq-tls/tls.crt"},
    {keyfile, "/etc/rabbitmq-tls/tls.key"},
    {secure_renegotiate, true},
    {verify, verify_peer},
    {fail_if_no_peer_cert, true},
    {versions, %s}
  ]},
  {client, [
    {cacertfile, "/etc/rabbitmq-tls/ca.crt"},
    {certfile, "/etc/rabbitmq-tls/tls.crt"},
    {keyfile, "/etc/rabbitmq-tls/tls.key"},
    {secure_renegotiate, true},
    {verify, verify_peer},
    {fail_if_no_peer_cert, true},
    {versions, %s}
  ]}
].
`, tlsVersions, tlsVersions)
}
