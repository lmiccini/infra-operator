# InstanceHA Architecture Documentation

## Overview

InstanceHA is a Python service that monitors OpenStack compute nodes and automatically evacuates instances from failed hosts. It integrates with the Nova API to detect down or stale compute services, verify host states, and coordinate the evacuation process including fencing, disabling hosts, and managing reserved hosts.

## Architecture

The service follows a modular architecture with the following key components:

```
┌─────────────────────────────────────────────────────────┐
│                    Main Loop                            │
│  (Polls Nova API every POLL seconds)                    │
└─────────────────┬───────────────────────────────────────┘
                  │
                  ├──► Categorize Services
                  │    (Stale, Resume, Re-enable)
                  │
                  ├──► Process Stale Services
                  │    ├──► Filter by Kdump (optional)
                  │    ├──► Filter by Aggregates (optional)
                  │    └──► Evacuation Workflow
                  │
                  └──► Process Re-enabling
```

## Main Components

### 1. ConfigManager

The `ConfigManager` class handles all configuration loading, validation, and access:

```python
class ConfigManager:
    def __init__(self, config_path: Optional[str] = None):
        self.config_path = config_path or os.getenv('INSTANCEHA_CONFIG_PATH',
                                                     '/var/lib/instanceha/config.yaml')
        self.clouds_path = os.getenv('CLOUDS_CONFIG_PATH',
                                     '/home/cloud-admin/.config/openstack/clouds.yaml')
        self.secure_path = os.getenv('SECURE_CONFIG_PATH',
                                      '/home/cloud-admin/.config/openstack/secure.yaml')
        self.fencing_path = os.getenv('FENCING_CONFIG_PATH', '/secrets/fencing.yaml')
```

It loads configuration from multiple YAML files:
- **Main config**: `/var/lib/instanceha/config.yaml` - Service settings
- **Clouds config**: `clouds.yaml` - OpenStack connection details
- **Secure config**: `secure.yaml` - Credentials (passwords, tokens)
- **Fencing config**: `/secrets/fencing.yaml` - BareMetal Host fencing configuration

### Configuration Options

InstanceHA supports extensive configuration through the main configuration file. All configuration values are validated with type checking and range constraints. Below is a comprehensive list of all available configuration options:

#### Core Service Configuration

##### `DELTA` (Integer, Default: 30, Range: 10-300 seconds)
Time threshold in seconds for detecting stale compute services. A service is considered stale (and eligible for evacuation) if its `updated_at` timestamp is older than `DELTA` seconds from the current time. Setting this too low may cause false positives during normal API latency, while setting it too high delays response to actual failures.

##### `POLL` (Integer, Default: 45, Range: 15-600 seconds)
Polling interval in seconds for querying the Nova API to check compute service status. The main loop sleeps for this duration between each polling cycle. Lower values provide faster detection but increase API load, while higher values reduce load but increase detection time.

**Note**: If `CHECK_KDUMP` is enabled with `KDUMP_TIMEOUT=30` and `POLL=30`, this may result in unexpected failures. Consider increasing `POLL` to 45 seconds or greater when using kdump detection.

##### `THRESHOLD` (Integer, Default: 50, Range: 0-100)
Maximum percentage of compute nodes that can be evacuated simultaneously. If the percentage of failed hosts exceeds this threshold, evacuation is aborted to prevent overwhelming the cluster.

**Important**: The threshold check uses a "greater than" comparison, so:
- Setting to **100** allows evacuating all hosts (since no percentage can exceed 100%)
- Setting to **0** blocks all evacuations (any percentage > 0 exceeds the threshold)
- Threshold checking cannot be disabled - it's always enforced, but can be set to 100 to effectively allow all hosts

##### `WORKERS` (Integer, Default: 4, Range: 1-50)
Number of worker threads for parallel operations such as:
- Concurrent evacuation status polling (when `SMART_EVACUATION` is enabled)
- Parallel kdump checks
- Concurrent host processing

Increasing this value can speed up operations but also increases resource usage and API load.

##### `DELAY` (Integer, Default: 0, Range: 0-300 seconds)
Delay in seconds to wait before starting evacuation after determining which instances to evacuate from a failed host. This delay occurs after server enumeration and filtering but before evacuation requests are submitted. This can be useful to allow for transient network issues or brief service interruptions to self-resolve before initiating evacuation.

##### `LOGLEVEL` (String, Default: "INFO")
Logging verbosity level. Valid values: `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL`. Setting to `DEBUG` provides detailed diagnostic information including API calls and internal state, while `ERROR` only logs error conditions.

#### Evacuation Behavior Configuration

##### `SMART_EVACUATION` (Boolean, Default: False)
Enables smart evacuation mode. When enabled, the service polls evacuation status for each server to ensure completion before proceeding. This provides better reliability and error detection but increases evacuation time. When disabled, uses a "fire-and-forget" approach where evacuation requests are submitted without waiting for completion.

##### `RESERVED_HOSTS` (Boolean, Default: False)
Enables reserved host management. When enabled, the service will automatically enable reserved hosts to replace failed hosts during evacuation. Reserved hosts are compute nodes that are disabled with the string "reserved" appearing anywhere in their `disabled_reason` field. The service matches reserved hosts to failed hosts based on aggregate membership (if `TAGGED_AGGREGATES` is enabled) or availability zone. If no matching reserved host is found, evacuation proceeds without replacement capacity - this is not considered a failure. Reserved hosts are typically pre-configured and ready to accept evacuated instances.

##### `LEAVE_DISABLED` (Boolean, Default: False)
Controls whether to leave hosts disabled after evacuation. When `False` (default), hosts are re-enabled after successful evacuation. When `True`, hosts remain disabled and must be manually re-enabled, useful for investigation or maintenance scenarios.

##### `FORCE_ENABLE` (Boolean, Default: False)
Forces host re-enabling even when migrations are still running. Normally, the service waits for all migrations to complete before re-enabling a host. Enabling this bypasses that check (use with caution).

##### `DISABLED` (Boolean, Default: False)
Global service disable flag. When set to `True`, the service will continue polling and monitoring but will not perform any evacuations. Useful for temporarily suspending evacuation operations during maintenance or testing.

#### Tag-Based Filtering Configuration

##### `EVACUABLE_TAG` (String, Default: "evacuable")
Tag name used to identify evacuable resources (flavors, images, aggregates). Resources tagged with this tag (set to "true") are eligible for evacuation. The tag can match exact keys or be part of composite keys (e.g., "evacuable" matches both "evacuable" and "trait:evacuable").

##### `TAGGED_IMAGES` (Boolean, Default: True)
Enables image-based evacuation filtering. When enabled, only instances using images tagged with `EVACUABLE_TAG` are evacuated. When disabled, image tags are not considered (all instances may be evacuated, subject to flavor/aggregate filtering).

##### `TAGGED_FLAVORS` (Boolean, Default: True)
Enables flavor-based evacuation filtering. When enabled, only instances using flavors with `EVACUABLE_TAG` in their extra specs are evacuated. When disabled, flavor tags are not considered.

##### `TAGGED_AGGREGATES` (Boolean, Default: True)
Enables aggregate-based evacuation filtering. When enabled, only hosts that are members of aggregates tagged with `EVACUABLE_TAG` in their metadata are considered for evacuation. When disabled, aggregate membership is not checked.

**Tag Filtering Logic**:
- If all tagging options are disabled, all instances are evacuated (backward compatibility).
- If any tagging option is enabled but no tagged resources exist, all instances are evacuated (fallback behavior).
- When multiple tagging options are enabled, instances matching **any** enabled criteria are evacuated (OR logic).

#### Kdump Detection Configuration

##### `CHECK_KDUMP` (Boolean, Default: False)
Enables kdump detection before evacuation. When enabled, the service listens for kdump UDP messages from hosts before evacuating. If a host is kdumping (has sent a kdump message within `KDUMP_TIMEOUT`), evacuation is delayed to allow crash dump completion.

##### `KDUMP_TIMEOUT` (Integer, Default: 30, Range: 5-300 seconds)
Timeout in seconds for waiting for kdump messages. If a host has sent a kdump message within this time window, evacuation is skipped. The service also waits up to this duration for delayed kdump starts before proceeding with evacuation.

**Important**: Ensure `POLL` is at least 45 seconds when using `CHECK_KDUMP` with `KDUMP_TIMEOUT=30` to avoid timing conflicts.

#### SSL/TLS Configuration

##### `SSL_VERIFY` (Boolean, Default: True)
Enables SSL certificate verification for HTTPS requests (Redfish API, etc.). Set to `False` to disable verification (not recommended for production). When disabled, a warning is logged.

##### `SSL_CA_BUNDLE` (String, Optional)
Path to a CA certificate bundle file for SSL verification. Used when custom CA certificates are required (e.g., self-signed certificates). If not provided or file doesn't exist, system default CA bundle is used.

##### `SSL_CERT_PATH` (String, Optional)
Path to SSL client certificate file for mutual TLS authentication. Must be provided together with `SSL_KEY_PATH`. Used for client certificate authentication with Redfish APIs.

##### `SSL_KEY_PATH` (String, Optional)
Path to SSL client private key file for mutual TLS authentication. Must be provided together with `SSL_CERT_PATH`. Used for client certificate authentication with Redfish APIs.

#### Fencing Configuration

The fencing configuration is loaded from `/secrets/fencing.yaml` (or the path specified by `FENCING_CONFIG_PATH`). The configuration file uses a `FencingConfig` key at the top level, with hostnames as keys mapping to their fencing agent configurations.

##### `FENCING_TIMEOUT` (Integer, Default: 30, Range: 5-120 seconds)
Timeout in seconds for fencing operations (power off/on via Redfish, IPMI, or BareMetal Host). Operations that exceed this timeout are considered failed. This timeout is used for:
- Redfish power state queries and reset operations
- BareMetal Host fencing via Kubernetes API
- Overall fencing operation timeout

##### Fencing Configuration File Format

The fencing configuration file supports three fencing agents: **IPMI**, **Redfish**, and **BareMetal Host (BMH)**. Each host can be configured with one of these agents:

```yaml
FencingConfig:
  # IPMI fencing example
  compute-node-01.example.com:
    agent: ipmi
    ipaddr: "192.168.1.100"
    ipport: "623"
    login: "admin"
    passwd: "ipmi-password"
    timeout: 30  # Optional, overrides FENCING_TIMEOUT config

  # Redfish fencing example
  compute-node-02.example.com:
    agent: redfish
    ipaddr: "192.168.1.101"
    login: "root"
    passwd: "redfish-password"
    ipport: "443"              # Optional, default: 443
    uuid: "System.Embedded.1"   # Optional, default: System.Embedded.1
    tls: "true"                # Optional, default: false (use https vs http)
    timeout: 45                # Optional, overrides FENCING_TIMEOUT config

  # BareMetal Host (BMH) fencing example
  compute-node-03.example.com:
    agent: bmh
    token: "eyJhbGciOiJSUzI1NiIsImtpZCI6InRlc3Q..."  # Kubernetes service account token
    host: "compute-node-03"    # BareMetalHost resource name
    namespace: "metal3"        # Kubernetes namespace where BMH exists

  # No-op fencing (for testing, NOT recommended for production)
  compute-node-04.example.com:
    agent: noop
```

**Fencing Agent Requirements**:

- **IPMI**: Requires `ipaddr`, `ipport`, `login`, `passwd`. Uses `ipmitool` for power control.
- **Redfish**: Requires `ipaddr`, `login`, `passwd`. Optional: `ipport` (default: 443), `uuid` (default: System.Embedded.1), `tls` (default: false for http, set to "true" for https).
- **BMH**: Requires `token` (Kubernetes service account token), `host` (BareMetalHost resource name), `namespace` (Kubernetes namespace). Uses Kubernetes API to annotate the BareMetalHost resource for power control.

**Important Notes**:
- Hostnames in the fencing configuration are matched by short hostname (hostname without domain). For example, `compute-node-01.example.com` matches both `compute-node-01` and `compute-node-01.example.com`.
- All passwords and tokens should be kept secure and never exposed in logs (the service uses safe logging to prevent credential leaks).
- The `noop` agent is for testing only and will log warnings - it does not actually fence hosts, which can lead to VM corruption in production environments.

#### Monitoring and Health Configuration

##### `HASH_INTERVAL` (Integer, Default: 60, Range: 30-300 seconds)
Interval in seconds for updating the health monitoring hash. The service generates a new SHA256 hash periodically to indicate it's alive and functioning. External monitoring can check this hash to verify service health.

##### `METRICS_LOG_INTERVAL` (Integer, Default: 3600, Range: 300-86400 seconds)
Interval in seconds for logging metrics summaries. The service logs operation counters, durations, and performance metrics at this interval. Set to a lower value for more frequent metrics or higher value to reduce log volume.

#### Environment Variables

In addition to the configuration file, the following environment variables can be used:

##### `INSTANCEHA_CONFIG_PATH`
Overrides the default configuration file path (`/var/lib/instanceha/config.yaml`).

##### `CLOUDS_CONFIG_PATH`
Overrides the default clouds configuration file path (`/home/cloud-admin/.config/openstack/clouds.yaml`).

##### `SECURE_CONFIG_PATH`
Overrides the default secure configuration file path (`/home/cloud-admin/.config/openstack/secure.yaml`).

##### `FENCING_CONFIG_PATH`
Overrides the default fencing configuration file path (`/secrets/fencing.yaml`).

##### `OS_CLOUD`
OpenStack cloud name to use from clouds.yaml (default: "overcloud").

##### `UDP_PORT`
UDP port for kdump listener (default: 7410).

#### Configuration File Format

The configuration file should be in YAML format:

```yaml
config:
  EVACUABLE_TAG: "evacuable"
  DELTA: "30"
  POLL: "45"
  THRESHOLD: "50"
  WORKERS: "4"
  DELAY: "0"
  LOGLEVEL: "INFO"
  SMART_EVACUATION: "false"
  RESERVED_HOSTS: "false"
  TAGGED_IMAGES: "true"
  TAGGED_FLAVORS: "true"
  TAGGED_AGGREGATES: "true"
  LEAVE_DISABLED: "false"
  FORCE_ENABLE: "false"
  CHECK_KDUMP: "false"
  KDUMP_TIMEOUT: "30"
  DISABLED: "false"
  SSL_VERIFY: "true"
  FENCING_TIMEOUT: "30"
  HASH_INTERVAL: "60"
  METRICS_LOG_INTERVAL: "3600"
  SSL_CA_BUNDLE: "/path/to/ca-bundle.pem"  # Optional
  SSL_CERT_PATH: "/path/to/client.crt"      # Optional
  SSL_KEY_PATH: "/path/to/client.key"      # Optional
```

All configuration values are validated at startup. Invalid values (out of range, wrong type, invalid enum values) will cause the service to fail with a descriptive error message.

### 2. InstanceHAService

The main service class encapsulates all InstanceHA functionality:

```python
class InstanceHAService(CloudConnectionProvider):
    def __init__(self, config_manager: ConfigManager, cloud_client: Optional[OpenStackClient] = None):
        self.config = config_manager
        self.cloud_client = cloud_client

        # Service state
        self.current_hash = ""  # Health monitoring hash
        self._host_servers_cache = {}  # Cache for performance
        self._evacuable_flavors_cache = None
        self._evacuable_images_cache = None

        # Kdump state management
        self.kdump_hosts_timestamp = defaultdict(float)
        self.kdump_hosts_checking = defaultdict(float)

        # Host processing state management
        self.hosts_processing = defaultdict(float)
        self.processing_lock = threading.Lock()
```

Key responsibilities:
- Manages OpenStack/Nova connections
- Caches evacuable flavors and images
- Tracks host processing state to prevent concurrent processing
- Manages kdump detection state

### 3. Metrics

The `Metrics` class provides performance monitoring:

```python
class Metrics:
    def __init__(self):
        self.counters = {}  # Event counters
        self.durations = {}  # Operation durations
        self.timing_history = {}  # Timing history for percentiles
```

It tracks:
- Operation counts (successful/failed evacuations)
- Operation durations and percentiles
- System uptime

### 4. Main Loop

The `main()` function implements the core polling loop:

```python
def main():
    service, metrics = _initialize_service()
    conn = _establish_nova_connection(service)

    while True:
        service.update_health_hash()  # Update health monitoring

        try:
            with metrics.timer('main_loop'):
                # Query Nova for compute services
                services = conn.services.list(binary="nova-compute")
                if not services:
                    continue

                # Calculate threshold date for stale detection
                target_date = datetime.now() - timedelta(seconds=service.config.get_delta())

                # Categorize services
                compute_nodes, to_resume, to_reenable = _categorize_services(services, target_date)

            # Process stale services for evacuation
            _process_stale_services(conn, service, services, compute_nodes, to_resume)

            # Process services that can be re-enabled
            _process_reenabling(conn, service, to_reenable)

        except Exception as e:
            logging.warning("Failed to query compute status from Nova API: %s", e)
            metrics.increment('main_loop_errors')

        # Log metrics periodically
        if time.time() - metrics._last_summary > metrics_interval:
            metrics.log_summary()
            metrics._last_summary = time.time()

        time.sleep(service.config.get_poll_interval())
```

## Service Categorization

The service categorizes compute nodes into three groups:

### 1. Compute Nodes (Stale/Down)

Services that need evacuation:

```python
def _get_compute_nodes(services, target_date):
    """Generator for compute nodes needing evacuation."""
    for svc in services:
        # Skip services already disabled or forced down
        if 'disabled' in svc.status or svc.forced_down:
            continue

        # Evacuate if service is down
        if svc.state == 'down':
            yield svc
            continue

        # Evacuate if service is stale (not updated recently)
        if datetime.fromisoformat(svc.updated_at) < target_date:
            yield svc
```

A service is considered stale if its `updated_at` timestamp is older than `DELTA` seconds.

### 2. Resume Candidates

Services that had evacuation started but need to resume:

```python
def _get_resume_candidates(services, target_date):
    """Generator for services to resume evacuation."""
    for svc in services:
        if (svc.forced_down and svc.state == 'down' and 'disabled' in svc.status and
            'instanceha evacuation' in svc.disabled_reason and
            'evacuation FAILED' not in svc.disabled_reason):
            yield svc
```

### 3. Re-enable Candidates

Services that can be re-enabled after successful evacuation:

```python
def _get_reenable_candidates(services, target_date):
    """Generator for services that can be re-enabled."""
    for svc in services:
        if 'enabled' in svc.status and svc.forced_down:
            yield svc
```

## Evacuation Workflow

The evacuation process follows these steps for each failed host:

```python
def process_service(failed_service, reserved_hosts, resume, service):
    """Process a failed compute service through the complete recovery workflow."""
    host_name = failed_service.host

    try:
        conn = _get_nova_connection(service)

        if not resume:
            # Step 1: Fence the host (power off)
            _execute_step("Fencing", _host_fence, host_name, host_name, 'off', service)

            # Step 2: Disable the host in Nova
            _execute_step("Host disable", _host_disable, host_name, conn, failed_service)

        # Step 3: Manage reserved hosts
        _execute_step("Reserved host management", _manage_reserved_hosts,
                     host_name, conn, failed_service, reserved_hosts, service)

        # Step 4: Evacuate instances
        _execute_step("Evacuation", _host_evacuate, host_name,
                     conn, failed_service, service)

        # Step 5: Post-evacuation recovery
        _execute_step("Recovery", _post_evacuation_recovery, host_name,
                     conn, failed_service, service)

        return True
    finally:
        # Clean up processing tracking
        with service.processing_lock:
            if hostname in service.hosts_processing:
                del service.hosts_processing[hostname]
```

### Step 1: Fencing

Fencing powers off the failed host to prevent split-brain scenarios:

```python
def _host_fence(host, action, service):
    """Fence a host using configured method."""
    # Supports multiple fencing methods:
    # 1. BareMetal Host (BMH) via Kubernetes API
    # 2. Redfish API
    # 3. IPMI via ipmitool
```

The fencing method is determined by the fencing configuration:

- **BareMetal Host**: Uses Kubernetes API to annotate the BMH resource
- **Redfish**: Uses Redfish API to power off the system
- **IPMI**: Uses `ipmitool` to power off via IPMI

### Step 2: Host Disable

Disables the compute service in Nova and marks it as down:

```python
def _host_disable(connection, service):
    """Disable a compute service by forcing it down and logging the reason."""
    # Force the service down (required for evacuation)
    connection.services.force_down(service.id, True)

    # Log the reason for disabling
    disable_reason = f"instanceha evacuation: {datetime.now().isoformat()}"
    connection.services.disable_log_reason(service.id, disable_reason)
```

### Step 3: Reserved Host Management

If reserved hosts are enabled, the service will enable a reserved host to replace the failed one:

```python
def _manage_reserved_hosts(conn, failed_service, reserved_hosts, service):
    """Manage reserved hosts to replace failed hosts."""
    if not reserved_hosts:
        return True

    # Enable a matching reserved host
    _enable_matching_reserved_host(conn, failed_service, reserved_hosts, service)
```

Reserved hosts are compute nodes that are disabled with a `reserved` tag in their disable reason. They are enabled when a host fails.

### Step 4: Evacuation

Evacuates all instances from the failed host:

```python
def _host_evacuate(connection, failed_service, service):
    """Evacuate all instances from a failed host."""
    host = failed_service.host

    # Get evacuable images and flavors (cached)
    images = service.get_evacuable_images(connection)
    flavors = service.get_evacuable_flavors(connection)

    # Get all servers on the failed host
    servers = connection.servers.list(search_opts={'host': host, 'all_tenants': 1})
    servers = [s for s in servers if s.status in {'ACTIVE', 'ERROR', 'STOPPED'}]

    # Filter by evacuable tags if enabled
    if flavors or images:
        evacuables = [s for s in servers
                     if service.is_server_evacuable(s, flavors, images)]
    else:
        evacuables = servers

    # Use smart evacuation if enabled
    if service.config.is_smart_evacuation_enabled():
        # Use ThreadPoolExecutor to poll evacuation status
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
            future_to_server = {executor.submit(_server_evacuate_future, connection, s): s
                               for s in evacuables}

            for future in concurrent.futures.as_completed(future_to_server):
                success = future.result()
                if not success:
                    return False
        return True
    else:
        # Traditional fire-and-forget approach
        for server in evacuables:
            response = _server_evacuate(connection, server.id)
            if not response["accepted"]:
                return False
        return True
```

**Smart Evacuation**: Polls evacuation status to ensure completion before proceeding.

**Traditional Evacuation**: Submits evacuation requests without waiting for completion.

### Step 5: Post-Evacuation Recovery

After evacuation completes, the service may re-enable the host:

```python
def _post_evacuation_recovery(conn, failed_service, service):
    """Handle post-evacuation recovery steps."""
    # Check if LEAVE_DISABLED is set
    if service.config.is_leave_disabled_enabled():
        return True

    # Re-enable the host
    return _host_enable(conn, failed_service, reenable=True)
```

## Tag-Based Filtering

InstanceHA supports filtering evacuations based on tags applied to flavors, images, and aggregates.

### Evacuable Tag Check

The service checks if resources have the evacuable tag:

```python
def is_server_evacuable(self, server, evac_flavors=None, evac_images=None):
    """Check if a server is evacuable based on flavor and image tags."""
    # If neither tagging type enabled, evacuate all (backward compatibility)
    if not images_enabled and not flavors_enabled:
        return True

    # Check image tags
    if images_enabled:
        server_image_id = self._get_server_image_id(server)
        if evac_images and server_image_id in evac_images:
            image_matches = True

    # Check flavor extra specs
    if flavors_enabled:
        flavor_extra_specs = server.flavor.get('extra_specs', {})
        evacuable_tag = self.config.get_evacuable_tag()

        matching_key = next((k for k in flavor_extra_specs
                            if k == evacuable_tag or evacuable_tag in k), None)
        if matching_key and str(flavor_extra_specs[matching_key]).lower() == 'true':
            flavor_matches = True

    # Evacuate if matches enabled criteria (OR logic when both enabled)
    should_evacuate = (image_matches or flavor_matches) if (both enabled) else \
                     (image_matches if images_enabled else flavor_matches)
```

### Caching

To improve performance, evacuable flavors and images are cached:

```python
def get_evacuable_flavors(self, connection: Optional[OpenStackClient] = None):
    """Get list of evacuable flavor IDs with caching."""
    # Check cache with lock
    with self._cache_lock:
        if self._evacuable_flavors_cache is not None:
            return self._evacuable_flavors_cache

    # Perform expensive API call outside lock
    flavors = connection.flavors.list(is_public=None)
    evacuable_tag = self.config.get_evacuable_tag()

    cache_data = []
    for flavor in flavors:
        if self._is_flavor_evacuable(flavor, evacuable_tag):
            cache_data.append(flavor.id)

    # Update cache with lock
    with self._cache_lock:
        self._evacuable_flavors_cache = cache_data

    return self._evacuable_flavors_cache
```

The cache is refreshed when compute nodes go down to ensure fresh data.

## Kdump Detection

If `CHECK_KDUMP` is enabled, the service listens for kdump messages via UDP before evacuating:

```python
def _kdump_udp_listener(service):
    """Background UDP listener for kdump messages."""
    with UDPSocketManager(udp_ip, udp_port) as sock:
        while not service.kdump_listener_stop_event.is_set():
            data, _, _, address = sock.recvmsg(65535, 1024, 0)

            if len(data) >= 8:
                # Check for kdump magic number (0x1B302A40)
                magic_native = struct.unpack('I', data[:4])[0]
                magic_network = struct.unpack('!I', data[:4])[0]

                if magic_native == 0x1B302A40 or magic_network == 0x1B302A40:
                    hostname = _extract_hostname(socket.gethostbyaddr(address[0])[0])
                    service.kdump_hosts_timestamp[hostname] = time.time()
                    logging.info('Kdump message received from host: %s' % hostname)
```

Before evacuating a host, the service checks if it's kdumping:

```python
def _check_kdump(stale_services: List[Any], service: InstanceHAService):
    """Check for kdump messages and filter hosts individually."""
    kdumping_hosts = []

    for svc in stale_services:
        hostname = _extract_hostname(svc.host)
        last_seen = service.kdump_hosts_timestamp.get(hostname, 0)

        # Check if host sent kdump message within timeout period
        if last_seen > 0 and (time.time() - last_seen) <= kdump_timeout:
            kdumping_hosts.append(svc.host)
            logging.info('Host %s is kdumping, skipping evacuation' % svc.host)

    # Wait for delayed kdump starts (parallel check)
    uncertain_hosts = [s for s in stale_services if s.host not in kdumping_hosts]

    if uncertain_hosts:
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {executor.submit(_check_kdump_single, s.host, service): s
                       for s in uncertain_hosts}

            for future in concurrent.futures.as_completed(futures, timeout=kdump_timeout + 5):
                if future.result():
                    kdumping_hosts.append(futures[future].host)

    # Remove kdumping hosts from evacuation
    to_evacuate = [s for s in stale_services if s.host not in kdumping_hosts]
    return to_evacuate, kdumping_hosts
```

If a host is kdumping (has sent a kdump message within `KDUMP_TIMEOUT` seconds), evacuation is skipped to allow the crash dump to complete.

## Processing State Management

To prevent concurrent processing of the same host, the service tracks processing state:

```python
def _process_stale_services(conn, service, services, compute_nodes, to_resume):
    """Process stale compute services for evacuation."""
    current_time = time.time()

    with service.processing_lock:
        # Clean up expired processing entries
        expired_processing = [h for h, t in service.hosts_processing.items()
                             if current_time - t > max_processing_time + 30]
        for hostname in expired_processing:
            del service.hosts_processing[hostname]

        # Filter out hosts currently being processed
        compute_nodes = [svc for svc in compute_nodes
                        if _extract_hostname(svc.host) not in service.hosts_processing]

        # Mark hosts as being processed
        for svc in compute_nodes + to_resume:
            hostname = _extract_hostname(svc.host)
            service.hosts_processing[hostname] = current_time
```

This prevents race conditions when multiple poll cycles detect the same failed host simultaneously.

## Filtering and Thresholds

Before evacuating, the service applies several filters:

1. **Hosts with Servers**: Only evacuate hosts that have instances:

```python
def filter_hosts_with_servers(self, compute_nodes, host_servers_cache):
    """Filter compute nodes to only include those with servers."""
    return [svc for svc in compute_nodes
           if host_servers_cache.get(svc.host, [])]
```

2. **Evacuable Servers**: Filter by tags if enabled:

```python
def filter_hosts_with_evacuable_servers(self, compute_nodes, host_servers_cache, flavors, images):
    """Filter compute nodes to only include those with evacuable servers."""
    filtered = []
    for svc in compute_nodes:
        servers = host_servers_cache.get(svc.host, [])
        evacuable_servers = [s for s in servers
                            if self.is_server_evacuable(s, flavors, images)]
        if evacuable_servers:
            filtered.append(svc)
    return filtered
```

3. **Aggregate Filtering**: Filter by aggregate membership if enabled:

```python
def _filter_by_aggregates(conn, service, compute_nodes, services):
    """Filter compute nodes by aggregate evacuability."""
    aggregates = conn.aggregates.list()
    evacuable_tag = service.config.get_evacuable_tag()
    evacuable_hosts = set()

    for agg in aggregates:
        if service._is_resource_evacuable(agg, evacuable_tag, ['metadata']):
            evacuable_hosts.update(agg.hosts)

    return [svc for svc in compute_nodes if svc.host in evacuable_hosts]
```

4. **Evacuation Threshold**: Check if percentage of failed hosts exceeds threshold:

```python
if services and compute_nodes:
    threshold_percent = (len(compute_nodes) / len(services)) * 100
    if threshold_percent > service.config.get_threshold():
        logging.error('Number of impacted computes (%.1f%%) exceeds threshold (%d%%). Not evacuating.',
                     threshold_percent, service.config.get_threshold())
        return  # Abort evacuation
```

This prevents evacuating too many hosts simultaneously, which could overload the cluster.

## Re-enabling Workflow

After evacuation completes, the service may re-enable hosts:

```python
def _process_reenabling(conn, service, to_reenable):
    """Process services that can be re-enabled."""
    for svc in to_reenable:
        # Check if all migrations are complete
        migrations = conn.server_migrations.list(host=svc.host, status='running')
        if migrations:
            logging.info('Skipping re-enable of %s: %d migrations still running',
                        svc.host, len(migrations))
            continue

        # Unset force-down and enable the service
        connection.services.force_down(svc.id, False)
        connection.services.enable(svc.id)
        logging.info('Re-enabled host %s after evacuation', svc.host)
```

The service checks for running migrations before re-enabling to ensure evacuation is complete.

## Error Handling

The service includes comprehensive error handling:

1. **Nova API Exceptions**: Handled via `_handle_nova_exception()`:

```python
def _handle_nova_exception(operation: str, service_info: str, e: Exception, is_critical: bool = True) -> bool:
    """Handle Nova API exceptions with appropriate logging."""
    if isinstance(e, (Unauthorized, Forbidden)):
        logging.error("%s: Authentication failed for %s", operation, service_info)
        return False
    elif isinstance(e, NotFound):
        logging.warning("%s: Resource not found for %s", operation, service_info)
        return not is_critical
    elif isinstance(e, Conflict):
        logging.warning("%s: Conflict for %s", operation, service_info)
        return not is_critical
    else:
        _safe_log_exception(f"{operation} failed for {service_info}", e)
        return False
```

2. **Safe Logging**: Prevents credential leaks in logs:

```python
def _safe_log_exception(msg: str, e: Exception, include_traceback: bool = False) -> None:
    """Log exception without exposing secrets in messages or tracebacks."""
    safe_msg = str(e)
    for secret_word in ['password', 'token', 'secret', 'credential', 'auth']:
        safe_msg = re.sub(rf'\b{secret_word}=[^\s)\'"]+',
                         f'{secret_word}=***', safe_msg, flags=re.IGNORECASE)
    logging.error("%s: %s", msg, safe_msg)
```

3. **Step Execution**: Wraps each step with error handling:

```python
def _execute_step(step_name, step_func, host_name, *args, **kwargs):
    """Execute a processing step with error handling."""
    try:
        logging.info("Starting %s for %s", step_name, host_name)
        result = step_func(*args, **kwargs)
        if result:
            logging.info("%s completed successfully for %s", step_name, host_name)
        else:
            logging.error("%s failed for %s", step_name, host_name)
        return result
    except Exception as e:
        logging.error("%s raised exception for %s: %s", step_name, host_name, e)
        return False
```

## Thread Safety

The service uses locks to ensure thread-safe operations:

1. **Cache Lock**: Protects cached evacuable resources:

```python
with self._cache_lock:
    if self._evacuable_flavors_cache is not None:
        return self._evacuable_flavors_cache
```

2. **Processing Lock**: Protects host processing state:

```python
with service.processing_lock:
    if hostname not in service.hosts_processing:
        service.hosts_processing[hostname] = current_time
```

## Health Monitoring

The service maintains a health hash that updates periodically:

```python
def update_health_hash(self, hash_interval: Optional[int] = None) -> None:
    """Update health monitoring hash for service status tracking."""
    current_timestamp = time.time()

    if current_timestamp - self._last_hash_time > hash_interval:
        new_hash = hashlib.sha256(str(current_timestamp).encode()).hexdigest()
        if new_hash == self._previous_hash:
            logging.error("Hash has not changed. Something went wrong.")
            self.hash_update_successful = False
        else:
            self.current_hash = new_hash
            self.hash_update_successful = True
            self._previous_hash = self.current_hash
            self._last_hash_time = current_timestamp
```

This hash can be monitored externally to verify the service is running.

## Summary

InstanceHA is a service that:

1. **Monitors** compute nodes via Nova API polling
2. **Detects** failed or stale compute services
3. **Verifies** host state (kdump detection, fencing)
4. **Filters** by tags and thresholds
5. **Evacuates** instances from failed hosts
6. **Manages** reserved hosts and post-evacuation recovery
7. **Re-enables** hosts after successful evacuation
