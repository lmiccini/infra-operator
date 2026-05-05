/*

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
*/

package v1beta1

import (
	"crypto/sha256"
	"fmt"

	condition "github.com/openstack-k8s-operators/lib-common/modules/common/condition"
)

// RabbitMQ Condition Types used by API objects.
const (
	// RabbitMQProxyActiveCondition indicates that the AMQP proxy sidecar is running.
	// Status=True means the proxy is active and must be cleared by setting the
	// clients-reconfigured annotation. Status=False means no proxy is running.
	RabbitMQProxyActiveCondition condition.Type = "RabbitMQProxyActive"
)

// TransportURL Condition Types used by API objects.
const (
	// TransportURLReadyCondition Status=True condition which indicates if TransportURL is configured and operational
	TransportURLReadyCondition condition.Type = "TransportURLReady"

	// TransportURLFinalizer - legacy finalizer for backward compatibility during migration.
	// New code should use TransportURLFinalizerFor() instead.
	TransportURLFinalizer = "transporturl.rabbitmq.openstack.org/finalizer"

	// TransportURLFinalizerPrefix - prefix for per-TransportURL finalizers on shared vhost/user CRs.
	// Use TransportURLFinalizerFor() to build the full finalizer name safely.
	TransportURLFinalizerPrefix = "turl.openstack.org/t-"

	// maxFinalizerNameSegment is the Kubernetes limit for the name segment after "/"
	maxFinalizerNameSegment = 63

	// MaxTransportURLDirectName is the maximum TransportURL name length that
	// can be embedded directly (without hashing) into a per-consumer finalizer.
	// Names longer than this are truncated+hashed, which breaks the watch-based
	// reverse mapping and requires a periodic requeue fallback.
	MaxTransportURLDirectName = maxFinalizerNameSegment - len("t-") // 61

	// RabbitMQUserCleanupBlockedFinalizer - safety finalizer that blocks automatic deletion of RabbitMQUsers.
	// When a shared user CR is orphaned (no active consumers), the user controller will only
	// auto-delete it after an admin removes this finalizer, confirming no external services
	// depend on the RabbitMQ user.
	RabbitMQUserCleanupBlockedFinalizer = "rabbitmq.openstack.org/cleanup-blocked"

	// RabbitMQUserOrphanedLabel marks a shared RabbitMQUser CR as having no active consumers.
	// The TransportURL controller sets this label instead of deleting the CR directly,
	// keeping the CR reclaimable by new consumers. The user controller auto-deletes
	// the CR only when this label is present AND the cleanup-blocked finalizer is removed.
	RabbitMQUserOrphanedLabel = "rabbitmq.openstack.org/orphaned"
)

// TransportURLFinalizerFor returns the per-consumer finalizer for a TransportURL.
// If the name fits within Kubernetes' 63-char name segment limit, it is used directly
// (preserving human readability and reverse mapping). For longer names, the suffix
// is truncated and a short hash is appended.
func TransportURLFinalizerFor(transportURLName string) string {
	prefix := "t-"
	maxNameLen := maxFinalizerNameSegment - len(prefix)
	if len(transportURLName) <= maxNameLen {
		return TransportURLFinalizerPrefix + transportURLName
	}
	hash := sha256.Sum256([]byte(transportURLName))
	hashHex := fmt.Sprintf("%x", hash[:4])
	truncLen := maxNameLen - len(hashHex)
	return TransportURLFinalizerPrefix + transportURLName[:truncLen] + hashHex
}

// TransportURL Reasons used by API objects.
const ()

// Common Messages used by API objects.
const (
	//
	// RabbitMQProxyActive condition messages
	//

	// RabbitMQProxyActiveMessage is the message when the proxy is active
	RabbitMQProxyActiveMessage = "AMQP proxy sidecar is active for queue migration. " +
		"To remove it, set annotation '%s: \"true\"' on the RabbitMq CR after all clients have been reconfigured for quorum queues"

	// RabbitMQProxyInactiveMessage is the message when the proxy is not active
	RabbitMQProxyInactiveMessage = "AMQP proxy sidecar is not active"

	//
	// TransportURLReady condition messages
	//

	// TransportURLReadyErrorMessage
	TransportURLReadyErrorMessage = "TransportURL error occured %s"

	// TransportURLReadyInitMessage
	TransportURLReadyInitMessage = "TransportURL not configured"

	// TransportURLReadyMessage
	TransportURLReadyMessage = "TransportURL completed"

	// TransportURLInProgressMessage
	TransportURLInProgressMessage = "TransportURL in progress"
)
