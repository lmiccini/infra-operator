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
	condition "github.com/openstack-k8s-operators/lib-common/modules/common/condition"
)

// Watcher Condition Types used by API objects.
const (
	// WatcherReadyCondition Status=True condition which indicates if Watcher is configured and operational
	WatcherReadyCondition condition.Type = "WatcherReady"
)

// Watcher Reasons used by API objects.
const ()

// Common Messages used by API objects.
const (
	// WatcherReadyInitMessage
	WatcherReadyInitMessage = "Watcher not started, waiting on keystone API"

	// WatcherKeystoneWaitingMessage
	WatcherKeystoneWaitingMessage = "Watcher keystone API not yet ready"

	// WatcherConfigMapWaitingMessage
	WatcherConfigMapWaitingMessage = "Watcher waiting for configmap"

	// WatcherSecretWaitingMessage
	WatcherSecretWaitingMessage = "Watcher waiting for secret"

	// WatcherInputReady
	WatcherInputReady = "Watcher input ready"

	// WatcherReadyMessage
	WatcherReadyMessage = "Watcher created"

	// WatcherReadyErrorMessage
	WatcherReadyErrorMessage = "Watcher error occured %s"
)
