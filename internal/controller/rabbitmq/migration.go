/*
Copyright 2025.

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

package rabbitmq

import (
	"bytes"
	"context"
	"crypto/tls"
	"crypto/x509"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"time"

	"github.com/go-logr/logr"
	rabbitmqv1beta1 "github.com/openstack-k8s-operators/infra-operator/apis/rabbitmq/v1beta1"
	helper "github.com/openstack-k8s-operators/lib-common/modules/common/helper"
	"github.com/openstack-k8s-operators/lib-common/modules/common/rsh"
	oko_secret "github.com/openstack-k8s-operators/lib-common/modules/common/secret"
	rabbitmqclusterv2 "github.com/rabbitmq/cluster-operator/v2/api/v1beta1"
	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/types"
	"k8s.io/client-go/rest"
	"sigs.k8s.io/controller-runtime/pkg/client"
)

// RabbitMQDefinitions represents the structure of RabbitMQ definitions export
type RabbitMQDefinitions struct {
	RabbitVersion    string               `json:"rabbit_version,omitempty"`
	Users            []interface{}        `json:"users,omitempty"`
	Vhosts           []VhostDefinition    `json:"vhosts,omitempty"`
	Permissions      []interface{}        `json:"permissions,omitempty"`
	TopicPermissions []interface{}        `json:"topic_permissions,omitempty"`
	Parameters       []interface{}        `json:"parameters,omitempty"`
	GlobalParameters []interface{}        `json:"global_parameters,omitempty"`
	Policies         []interface{}        `json:"policies,omitempty"`
	Queues           []QueueDefinition    `json:"queues,omitempty"`
	Exchanges        []ExchangeDefinition `json:"exchanges,omitempty"`
	Bindings         []BindingDefinition  `json:"bindings,omitempty"`
}

// VhostDefinition represents a virtual host definition
type VhostDefinition struct {
	Name string `json:"name"`
}

// QueueDefinition represents a queue definition
type QueueDefinition struct {
	Name       string                 `json:"name"`
	Vhost      string                 `json:"vhost"`
	Durable    bool                   `json:"durable"`
	AutoDelete bool                   `json:"auto_delete"`
	Arguments  map[string]interface{} `json:"arguments"`
}

// ExchangeDefinition represents an exchange definition
type ExchangeDefinition struct {
	Name       string                 `json:"name"`
	Vhost      string                 `json:"vhost"`
	Type       string                 `json:"type"`
	Durable    bool                   `json:"durable"`
	AutoDelete bool                   `json:"auto_delete"`
	Internal   bool                   `json:"internal"`
	Arguments  map[string]interface{} `json:"arguments"`
}

// BindingDefinition represents a binding definition
type BindingDefinition struct {
	Source          string                 `json:"source"`
	Vhost           string                 `json:"vhost"`
	Destination     string                 `json:"destination"`
	DestinationType string                 `json:"destination_type"`
	RoutingKey      string                 `json:"routing_key"`
	Arguments       map[string]interface{} `json:"arguments"`
}

// ShovelDefinition represents a shovel parameter
type ShovelDefinition struct {
	Value     ShovelValue `json:"value"`
	Vhost     string      `json:"vhost"`
	Component string      `json:"component"`
	Name      string      `json:"name"`
}

// ShovelValue represents shovel configuration
type ShovelValue struct {
	SrcProtocol    string `json:"src-protocol"`
	SrcURI         string `json:"src-uri"`
	SrcQueue       string `json:"src-queue"`
	DestProtocol   string `json:"dest-protocol"`
	DestURI        string `json:"dest-uri"`
	DestQueue      string `json:"dest-queue"`
	AckMode        string `json:"ack-mode"`
	DeleteAfter    string `json:"delete-after,omitempty"`
	SrcDeleteAfter string `json:"src-delete-after,omitempty"`
}

// QueueInfo represents queue status information
type QueueInfo struct {
	Name                   string `json:"name"`
	Vhost                  string `json:"vhost"`
	Messages               int64  `json:"messages"`
	MessagesReady          int64  `json:"messages_ready"`
	MessagesUnacknowledged int64  `json:"messages_unacknowledged"`
	State                  string `json:"state"`
}

// MigrationStatus tracks the progress of queue migration
type MigrationStatus struct {
	TotalQueues       int
	MigratedQueues    int
	RemainingMessages int64
	ShovelsActive     int
	Errors            []string
}

// RabbitMQAPIClient handles communication with RabbitMQ Management API
type RabbitMQAPIClient struct {
	baseURL    string
	username   string
	password   string
	httpClient *http.Client
	log        logr.Logger
}

// NewRabbitMQAPIClient creates a new RabbitMQ Management API client
func NewRabbitMQAPIClient(ctx context.Context, h *helper.Helper, instance *rabbitmqv1beta1.RabbitMq, log logr.Logger) (*RabbitMQAPIClient, error) {
	// Get RabbitMQCluster
	cluster := &rabbitmqclusterv2.RabbitmqCluster{}
	err := h.GetClient().Get(ctx, types.NamespacedName{
		Name:      instance.Name,
		Namespace: instance.Namespace,
	}, cluster)
	if err != nil {
		return nil, fmt.Errorf("failed to get RabbitMQCluster: %w", err)
	}

	// Get credentials secret
	secretName := fmt.Sprintf("%s-default-user", instance.Name)
	secret, _, err := oko_secret.GetSecret(ctx, h, secretName, instance.Namespace)
	if err != nil {
		return nil, fmt.Errorf("failed to get credentials secret: %w", err)
	}

	username := string(secret.Data["username"])
	password := string(secret.Data["password"])
	baseURL := getManagementURL(cluster, secret)

	// Setup HTTP client with TLS if needed
	httpClient := &http.Client{
		Timeout: 30 * time.Second,
	}

	if cluster.Spec.TLS.SecretName != "" {
		caCert, err := getTLSCACert(ctx, h, cluster, instance.Namespace)
		if err != nil {
			return nil, fmt.Errorf("failed to get CA cert: %w", err)
		}

		caCertPool := x509.NewCertPool()
		caCertPool.AppendCertsFromPEM(caCert)

		httpClient.Transport = &http.Transport{
			TLSClientConfig: &tls.Config{
				RootCAs: caCertPool,
			},
		}
	}

	return &RabbitMQAPIClient{
		baseURL:    baseURL,
		username:   username,
		password:   password,
		httpClient: httpClient,
		log:        log,
	}, nil
}

// doRequest performs an HTTP request to the RabbitMQ Management API
func (c *RabbitMQAPIClient) doRequest(method, path string, body interface{}) ([]byte, error) {
	var reqBody io.Reader
	if body != nil {
		jsonData, err := json.Marshal(body)
		if err != nil {
			return nil, fmt.Errorf("failed to marshal request body: %w", err)
		}
		reqBody = bytes.NewBuffer(jsonData)
	}

	url := c.baseURL + path
	req, err := http.NewRequest(method, url, reqBody)
	if err != nil {
		return nil, fmt.Errorf("failed to create request: %w", err)
	}

	req.SetBasicAuth(c.username, c.password)
	req.Header.Set("Content-Type", "application/json")

	resp, err := c.httpClient.Do(req)
	if err != nil {
		return nil, fmt.Errorf("failed to execute request: %w", err)
	}
	defer resp.Body.Close()

	respBody, err := io.ReadAll(resp.Body)
	if err != nil {
		return nil, fmt.Errorf("failed to read response body: %w", err)
	}

	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		return nil, fmt.Errorf("API request failed with status %d: %s", resp.StatusCode, string(respBody))
	}

	return respBody, nil
}

// ExportDefinitions exports definitions from a specific vhost
func (c *RabbitMQAPIClient) ExportDefinitions(vhost string) (*RabbitMQDefinitions, error) {
	// URL encode vhost name ("/" becomes "%2F")
	encodedVhost := vhost
	if vhost == "/" {
		encodedVhost = "%2F"
	}

	path := fmt.Sprintf("/api/definitions/%s", encodedVhost)
	c.log.Info("Exporting definitions from vhost", "vhost", vhost, "path", path)

	respBody, err := c.doRequest("GET", path, nil)
	if err != nil {
		return nil, fmt.Errorf("failed to export definitions: %w", err)
	}

	var defs RabbitMQDefinitions
	if err := json.Unmarshal(respBody, &defs); err != nil {
		return nil, fmt.Errorf("failed to parse definitions: %w", err)
	}

	c.log.Info("Exported definitions",
		"queues", len(defs.Queues),
		"exchanges", len(defs.Exchanges),
		"bindings", len(defs.Bindings))

	return &defs, nil
}

// ImportDefinitions imports definitions to a specific vhost
func (c *RabbitMQAPIClient) ImportDefinitions(vhost string, defs *RabbitMQDefinitions) error {
	// RabbitMQ Management API only supports POST /api/definitions (without vhost in path)
	// The vhost is specified in the definitions themselves
	path := "/api/definitions"
	c.log.Info("Importing definitions", "targetVhost", vhost, "path", path,
		"queues", len(defs.Queues),
		"exchanges", len(defs.Exchanges),
		"bindings", len(defs.Bindings))

	_, err := c.doRequest("POST", path, defs)
	if err != nil {
		return fmt.Errorf("failed to import definitions: %w", err)
	}

	c.log.Info("Successfully imported definitions to vhost", "vhost", vhost)
	return nil
}

// GetQueueInfo retrieves information about a specific queue
func (c *RabbitMQAPIClient) GetQueueInfo(vhost, queueName string) (*QueueInfo, error) {
	encodedVhost := vhost
	if vhost == "/" {
		encodedVhost = "%2F"
	}

	path := fmt.Sprintf("/api/queues/%s/%s", encodedVhost, queueName)
	respBody, err := c.doRequest("GET", path, nil)
	if err != nil {
		return nil, fmt.Errorf("failed to get queue info: %w", err)
	}

	var info QueueInfo
	if err := json.Unmarshal(respBody, &info); err != nil {
		return nil, fmt.Errorf("failed to parse queue info: %w", err)
	}

	return &info, nil
}

// CreateShovel creates a dynamic shovel to move messages between queues
func (c *RabbitMQAPIClient) CreateShovel(vhost, shovelName, srcVhost, srcQueue, destVhost, destQueue, destURI string) error {
	encodedVhost := vhost
	if vhost == "/" {
		encodedVhost = "%2F"
	}

	path := fmt.Sprintf("/api/parameters/shovel/%s/%s", encodedVhost, shovelName)

	shovel := map[string]interface{}{
		"value": map[string]interface{}{
			"src-protocol":  "amqp091",
			"src-uri":       "amqp://", // Use local connection
			"src-queue":     srcQueue,
			"dest-protocol": "amqp091",
			"dest-uri":      destURI,
			"dest-queue":    destQueue,
			"ack-mode":      "on-confirm",
			"delete-after":  "never", // We'll delete manually after verification
		},
	}

	c.log.Info("Creating shovel",
		"name", shovelName,
		"srcQueue", srcQueue,
		"destQueue", destQueue)

	_, err := c.doRequest("PUT", path, shovel)
	if err != nil {
		return fmt.Errorf("failed to create shovel: %w", err)
	}

	return nil
}

// DeleteShovel deletes a dynamic shovel
func (c *RabbitMQAPIClient) DeleteShovel(vhost, shovelName string) error {
	encodedVhost := vhost
	if vhost == "/" {
		encodedVhost = "%2F"
	}

	path := fmt.Sprintf("/api/parameters/shovel/%s/%s", encodedVhost, shovelName)

	c.log.Info("Deleting shovel", "name", shovelName)

	_, err := c.doRequest("DELETE", path, nil)
	if err != nil {
		return fmt.Errorf("failed to delete shovel: %w", err)
	}

	return nil
}

// ListQueues lists all queues in a vhost
func (c *RabbitMQAPIClient) ListQueues(vhost string) ([]QueueInfo, error) {
	encodedVhost := vhost
	if vhost == "/" {
		encodedVhost = "%2F"
	}

	path := fmt.Sprintf("/api/queues/%s", encodedVhost)
	respBody, err := c.doRequest("GET", path, nil)
	if err != nil {
		return nil, fmt.Errorf("failed to list queues: %w", err)
	}

	var queues []QueueInfo
	if err := json.Unmarshal(respBody, &queues); err != nil {
		return nil, fmt.Errorf("failed to parse queue list: %w", err)
	}

	return queues, nil
}

// TransformDefinitionsForQuorum transforms definitions to use quorum queues
func TransformDefinitionsForQuorum(defs *RabbitMQDefinitions, targetVhost string) *RabbitMQDefinitions {
	transformed := &RabbitMQDefinitions{
		Vhosts:    []VhostDefinition{{Name: targetVhost}},
		Queues:    make([]QueueDefinition, 0, len(defs.Queues)),
		Exchanges: make([]ExchangeDefinition, 0, len(defs.Exchanges)),
		Bindings:  make([]BindingDefinition, 0, len(defs.Bindings)),
	}

	// Transform queues to quorum type
	for _, q := range defs.Queues {
		// Skip internal queues
		if q.Name == "" || q.Name[0] == 'a' && q.Name[1] == 'm' && q.Name[2] == 'q' {
			continue
		}

		newQueue := QueueDefinition{
			Name:       q.Name,
			Vhost:      targetVhost,
			Durable:    true,  // Quorum queues must be durable
			AutoDelete: false, // Quorum queues cannot be auto-delete
			Arguments:  make(map[string]interface{}),
		}

		// Set queue type to quorum
		newQueue.Arguments["x-queue-type"] = "quorum"

		// Copy over message TTL if it exists
		if ttl, ok := q.Arguments["x-message-ttl"]; ok {
			newQueue.Arguments["x-message-ttl"] = ttl
		}

		// Copy over max length if it exists
		if maxLen, ok := q.Arguments["x-max-length"]; ok {
			newQueue.Arguments["x-max-length"] = maxLen
		}

		transformed.Queues = append(transformed.Queues, newQueue)
	}

	// Transform exchanges (change vhost)
	for _, e := range defs.Exchanges {
		// Skip default exchanges
		if e.Name == "" || e.Name == "amq.direct" || e.Name == "amq.fanout" ||
			e.Name == "amq.headers" || e.Name == "amq.match" || e.Name == "amq.topic" {
			continue
		}

		newExchange := ExchangeDefinition{
			Name:       e.Name,
			Vhost:      targetVhost,
			Type:       e.Type,
			Durable:    e.Durable,
			AutoDelete: e.AutoDelete,
			Internal:   e.Internal,
			Arguments:  e.Arguments,
		}

		transformed.Exchanges = append(transformed.Exchanges, newExchange)
	}

	// Transform bindings (change vhost)
	for _, b := range defs.Bindings {
		// Skip default exchange bindings
		if b.Source == "" {
			continue
		}

		newBinding := BindingDefinition{
			Source:          b.Source,
			Vhost:           targetVhost,
			Destination:     b.Destination,
			DestinationType: b.DestinationType,
			RoutingKey:      b.RoutingKey,
			Arguments:       b.Arguments,
		}

		transformed.Bindings = append(transformed.Bindings, newBinding)
	}

	return transformed
}

// MigrationOrchestrator handles the complete migration workflow
type MigrationOrchestrator struct {
	client      *RabbitMQAPIClient
	instance    *rabbitmqv1beta1.RabbitMq
	helper      *helper.Helper
	restConfig  *rest.Config
	log         logr.Logger
	sourceVhost string
	targetVhost string
}

// NewMigrationOrchestrator creates a new migration orchestrator
func NewMigrationOrchestrator(ctx context.Context, h *helper.Helper, restConfig *rest.Config, instance *rabbitmqv1beta1.RabbitMq, log logr.Logger) (*MigrationOrchestrator, error) {
	client, err := NewRabbitMQAPIClient(ctx, h, instance, log)
	if err != nil {
		return nil, fmt.Errorf("failed to create RabbitMQ API client: %w", err)
	}

	return &MigrationOrchestrator{
		client:      client,
		instance:    instance,
		helper:      h,
		restConfig:  restConfig,
		log:         log,
		sourceVhost: "/",
		targetVhost: "quorum", // Vhost name is "quorum" not "/quorum"
	}, nil
}

// EnableShovelPlugin enables the rabbitmq_shovel plugin on all RabbitMQ pods
func (m *MigrationOrchestrator) EnableShovelPlugin(ctx context.Context) error {
	m.log.Info("Enabling rabbitmq_shovel plugin on all RabbitMQ pods")

	// Get all RabbitMQ pods
	podList := &corev1.PodList{}
	if err := m.helper.GetClient().List(ctx, podList, client.InNamespace(m.instance.Namespace), client.MatchingLabels{
		"app.kubernetes.io/name": m.instance.Name,
	}); err != nil {
		return fmt.Errorf("failed to list RabbitMQ pods: %w", err)
	}

	if len(podList.Items) == 0 {
		return fmt.Errorf("no RabbitMQ pods found")
	}

	// Enable shovel plugin on each pod
	for _, pod := range podList.Items {
		if pod.Status.Phase != corev1.PodRunning {
			m.log.Info("Skipping pod that is not running", "pod", pod.Name, "phase", pod.Status.Phase)
			continue
		}

		m.log.Info("Enabling shovel plugin on pod", "pod", pod.Name)

		// Execute the rabbitmq-plugins enable command
		cmd := []string{"rabbitmq-plugins", "enable", "rabbitmq_shovel"}
		err := rsh.ExecInPod(
			ctx,
			m.helper.GetKClient(),
			m.restConfig,
			types.NamespacedName{Name: pod.Name, Namespace: pod.Namespace},
			"rabbitmq",
			cmd,
			func(stdout *bytes.Buffer, stderr *bytes.Buffer) error {
				m.log.Info("Plugin enable output",
					"pod", pod.Name,
					"stdout", stdout.String(),
					"stderr", stderr.String())
				return nil
			},
		)
		if err != nil {
			return fmt.Errorf("failed to enable shovel plugin on pod %s: %w", pod.Name, err)
		}

		m.log.Info("Successfully enabled shovel plugin on pod", "pod", pod.Name)
	}

	m.log.Info("Shovel plugin enabled on all RabbitMQ pods", "totalPods", len(podList.Items))
	return nil
}

// StartMigration initiates the migration process
func (m *MigrationOrchestrator) StartMigration(ctx context.Context) error {
	m.log.Info("Starting queue migration from mirrored to quorum",
		"sourceVhost", m.sourceVhost,
		"targetVhost", m.targetVhost)

	// Step 1: Export definitions from source vhost
	m.log.Info("Step 1: Exporting definitions from source vhost")
	sourceDefs, err := m.client.ExportDefinitions(m.sourceVhost)
	if err != nil {
		return fmt.Errorf("failed to export definitions: %w", err)
	}

	// Step 2: Transform definitions for quorum queues
	m.log.Info("Step 2: Transforming definitions for quorum queues",
		"queues", len(sourceDefs.Queues),
		"exchanges", len(sourceDefs.Exchanges))
	targetDefs := TransformDefinitionsForQuorum(sourceDefs, m.targetVhost)

	// Step 3: Import definitions to target vhost
	m.log.Info("Step 3: Importing definitions to target vhost",
		"queues", len(targetDefs.Queues),
		"exchanges", len(targetDefs.Exchanges))
	if err := m.client.ImportDefinitions(m.targetVhost, targetDefs); err != nil {
		return fmt.Errorf("failed to import definitions: %w", err)
	}

	// Step 4: Enable shovel plugin on all RabbitMQ pods
	m.log.Info("Step 4: Enabling shovel plugin on all RabbitMQ pods")
	if err := m.EnableShovelPlugin(ctx); err != nil {
		return fmt.Errorf("failed to enable shovel plugin: %w", err)
	}

	// Step 5: Create shovels for each queue
	m.log.Info("Step 5: Creating shovels to migrate messages")
	if err := m.createShovels(sourceDefs.Queues); err != nil {
		return fmt.Errorf("failed to create shovels: %w", err)
	}

	m.log.Info("Migration started successfully",
		"totalQueues", len(sourceDefs.Queues))

	return nil
}

// createShovels creates a shovel for each queue that has messages
func (m *MigrationOrchestrator) createShovels(queues []QueueDefinition) error {
	// Build destination URI for quorum vhost
	destURI := fmt.Sprintf("amqp:///%s", m.targetVhost)

	for _, queue := range queues {
		// Skip internal queues
		if queue.Name == "" || (len(queue.Name) >= 3 && queue.Name[:3] == "amq") {
			continue
		}

		// Check if queue has messages before creating shovel
		queueInfo, err := m.client.GetQueueInfo(m.sourceVhost, queue.Name)
		if err != nil {
			m.log.Error(err, "Failed to get queue info, skipping", "queue", queue.Name)
			continue
		}

		if queueInfo.Messages == 0 {
			m.log.Info("Queue is empty, skipping shovel creation", "queue", queue.Name)
			continue
		}

		// Create shovel
		shovelName := fmt.Sprintf("migrate-%s", queue.Name)
		err = m.client.CreateShovel(
			m.sourceVhost,
			shovelName,
			m.sourceVhost,
			queue.Name,
			m.targetVhost,
			queue.Name,
			destURI,
		)
		if err != nil {
			return fmt.Errorf("failed to create shovel for queue %s: %w", queue.Name, err)
		}

		m.log.Info("Created shovel for queue",
			"queue", queue.Name,
			"messages", queueInfo.Messages,
			"shovel", shovelName)
	}

	return nil
}

// GetMigrationStatus checks the current status of the migration
func (m *MigrationOrchestrator) GetMigrationStatus(ctx context.Context) (*MigrationStatus, error) {
	status := &MigrationStatus{
		Errors: make([]string, 0),
	}

	// Get queues from source vhost
	sourceQueues, err := m.client.ListQueues(m.sourceVhost)
	if err != nil {
		return nil, fmt.Errorf("failed to list source queues: %w", err)
	}

	// Count total non-internal queues
	for _, q := range sourceQueues {
		if q.Name != "" && !(len(q.Name) >= 3 && q.Name[:3] == "amq") {
			status.TotalQueues++
			status.RemainingMessages += q.Messages
		}
	}

	// Get queues from target vhost to count migrated queues
	targetQueues, err := m.client.ListQueues(m.targetVhost)
	if err != nil {
		return nil, fmt.Errorf("failed to list target queues: %w", err)
	}

	// Count queues that exist on target
	targetQueueNames := make(map[string]bool)
	for _, q := range targetQueues {
		targetQueueNames[q.Name] = true
	}

	for _, q := range sourceQueues {
		if q.Name != "" && !(len(q.Name) >= 3 && q.Name[:3] == "amq") {
			if targetQueueNames[q.Name] {
				// Check if source queue is now empty (migration complete for this queue)
				if q.Messages == 0 {
					status.MigratedQueues++
				} else {
					status.ShovelsActive++
				}
			}
		}
	}

	m.log.Info("Migration status",
		"totalQueues", status.TotalQueues,
		"migratedQueues", status.MigratedQueues,
		"remainingMessages", status.RemainingMessages,
		"shovelsActive", status.ShovelsActive)

	return status, nil
}

// IsMigrationComplete checks if the migration is fully complete
func (m *MigrationOrchestrator) IsMigrationComplete(ctx context.Context) (bool, error) {
	status, err := m.GetMigrationStatus(ctx)
	if err != nil {
		return false, err
	}

	// Migration is complete when all source queues are empty
	return status.RemainingMessages == 0, nil
}

// CleanupMigration removes shovels and performs post-migration cleanup
func (m *MigrationOrchestrator) CleanupMigration(ctx context.Context) error {
	m.log.Info("Cleaning up migration resources")

	// Get queues from source vhost to find shovels
	sourceQueues, err := m.client.ListQueues(m.sourceVhost)
	if err != nil {
		return fmt.Errorf("failed to list source queues: %w", err)
	}

	// Delete all migration shovels
	for _, q := range sourceQueues {
		if q.Name == "" || (len(q.Name) >= 3 && q.Name[:3] == "amq") {
			continue
		}

		shovelName := fmt.Sprintf("migrate-%s", q.Name)
		if err := m.client.DeleteShovel(m.sourceVhost, shovelName); err != nil {
			// Log but don't fail - shovel might not exist
			m.log.Info("Failed to delete shovel (may not exist)", "shovel", shovelName, "error", err.Error())
		} else {
			m.log.Info("Deleted shovel", "shovel", shovelName)
		}
	}

	m.log.Info("Migration cleanup complete")
	return nil
}

// VerifyTargetVhost ensures the target vhost exists and is ready
func (m *MigrationOrchestrator) VerifyTargetVhost(ctx context.Context) error {
	// Check if quorum vhost exists in RabbitMQ
	vhostName := types.NamespacedName{
		Name:      m.instance.Name + "-quorum-vhost",
		Namespace: m.instance.Namespace,
	}

	vhost := &rabbitmqv1beta1.RabbitMQVhost{}
	err := m.helper.GetClient().Get(ctx, vhostName, vhost)
	if err != nil {
		return fmt.Errorf("quorum vhost CR not found: %w", err)
	}

	// Check if vhost is ready
	if !vhost.Status.Conditions.IsTrue(rabbitmqv1beta1.RabbitMQVhostReadyCondition) {
		return fmt.Errorf("quorum vhost is not ready yet")
	}

	m.log.Info("Target vhost verified and ready", "vhost", m.targetVhost)
	return nil
}
