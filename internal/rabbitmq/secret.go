package rabbitmq

import (
	"fmt"

	rabbitmqv1 "github.com/openstack-k8s-operators/infra-operator/apis/rabbitmq/v1beta1"
	"github.com/openstack-k8s-operators/lib-common/modules/common/util"
	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
)

const (
	erlangCookieLength = 20
)

// GenerateErlangCookie generates a secret containing the Erlang cookie
func GenerateErlangCookie(r *rabbitmqv1.RabbitMq) (*corev1.Secret, error) {
	cookie, err := util.GeneratePassword(erlangCookieLength)
	if err != nil {
		return nil, fmt.Errorf("failed to generate erlang cookie: %w", err)
	}

	secret := &corev1.Secret{
		ObjectMeta: metav1.ObjectMeta{
			Name:      fmt.Sprintf("%s-erlang-cookie", r.Name),
			Namespace: r.Namespace,
		},
		Type: corev1.SecretTypeOpaque,
		Data: map[string][]byte{
			".erlang.cookie": []byte(cookie),
		},
	}

	return secret, nil
}

// GenerateDefaultUser generates a secret containing default user credentials.
// The secret includes username, password, default_user.conf (for RabbitMQ config),
// host (service DNS name), and port (AMQP or AMQPS based on TLS).
func GenerateDefaultUser(r *rabbitmqv1.RabbitMq) (*corev1.Secret, error) {
	username := fmt.Sprintf("default_user_%s", r.Name)
	password, err := util.GeneratePassword(24)
	if err != nil {
		return nil, fmt.Errorf("failed to generate default user password: %w", err)
	}

	// Generate default_user.conf content
	defaultUserConf := fmt.Sprintf(`default_user = %s
default_pass = %s
default_user_tags.administrator = true
`, username, password)

	// Determine host and port
	host := fmt.Sprintf("%s.%s.svc", r.Name, r.Namespace)
	port := "5672"
	if r.Spec.TLS.SecretName != "" {
		port = "5671"
	}

	secret := &corev1.Secret{
		ObjectMeta: metav1.ObjectMeta{
			Name:      fmt.Sprintf("%s-default-user", r.Name),
			Namespace: r.Namespace,
		},
		Type: corev1.SecretTypeOpaque,
		Data: map[string][]byte{
			"username":          []byte(username),
			"password":          []byte(password),
			"default_user.conf": []byte(defaultUserConf),
			"host":              []byte(host),
			"port":              []byte(port),
		},
	}

	return secret, nil
}
