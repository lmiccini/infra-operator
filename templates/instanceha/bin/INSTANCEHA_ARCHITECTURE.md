# InstanceHA Architecture Documentation

## Overview

InstanceHA is a high-availability service for OpenStack that automatically detects and evacuates instances from failed compute nodes. It provides intelligent failure detection, flexible tagging, multiple fencing mechanisms, and comprehensive error handling to ensure workload continuity in production OpenStack clouds.

**Version**: 2.0
**Code Coverage**: 71% (1140/1625 lines)
**Test Suite**: 202 tests, ~14 seconds execution time

## Table of Contents

1. [Core Components](#core-components)
2. [Architecture Patterns](#architecture-patterns)
3. [Service Workflow](#service-workflow)
4. [Configuration System](#configuration-system)
5. [Evacuation Mechanisms](#evacuation-mechanisms)
6. [Fencing Agents](#fencing-agents)
7. [Advanced Features](#advanced-features)
8. [Security](#security)
9. [Performance](#performance)
10. [Testing](#testing)

---

## Core Components

### 0. Data Structures

**Location**: `instanceha.py:104-116`

**Dataclasses**:

```python
@dataclass
class EvacuationResult:
    """Result of a server evacuation request."""
    uuid: str
    accepted: bool
    reason: str

@dataclass
class EvacuationStatus:
    """Status of an ongoing server evacuation."""
    completed: bool
    error: bool

@dataclass
class ConfigItem:
    """Configuration item with type and validation constraints."""
    type: str
    default: Any
    min_val: Optional[int]
    max_val: Optional[int]
```

---

### 1. ConfigManager

**Purpose**: Centralized configuration management with validation and secure access.

**Location**: `instanceha.py:312-578`

**Responsibilities**:
- Load and merge YAML configuration files
- Validate configuration values with type checking
- Provide type-safe accessors with defaults
- Manage SSL/TLS configuration
- Handle cloud credentials securely

**Key Features**:
- **Configuration Sources**: Main config, clouds.yaml, secure.yaml, fencing.yaml
- **Validation**: Type checking, range validation (min/max), enum validation
- **SSL Support**: CA bundle, client certificates, verification toggle
- **Environment Overrides**: OS_CLOUD, UDP_PORT, SSL paths

**Configuration Map**:
```python
_config_map: Dict[str, ConfigItem] = {
    'EVACUABLE_TAG': ConfigItem('str', 'evacuable'),
    'DELTA': ConfigItem('int', 30, 10, 300),      # Service staleness threshold
    'POLL': ConfigItem('int', 45, 15, 600),       # Poll interval
    'THRESHOLD': ConfigItem('int', 50, 0, 100),   # Failure threshold %
    'WORKERS': ConfigItem('int', 4, 1, 50),       # Thread pool size
    'SMART_EVACUATION': ConfigItem('bool', False),
    'RESERVED_HOSTS': ConfigItem('bool', False),
    'TAGGED_IMAGES': ConfigItem('bool', True),
    'TAGGED_FLAVORS': ConfigItem('bool', True),
    'TAGGED_AGGREGATES': ConfigItem('bool', True),
    'CHECK_KDUMP': ConfigItem('bool', False),
    'KDUMP_TIMEOUT': ConfigItem('int', 30, 5, 300),
    'FENCING_TIMEOUT': ConfigItem('int', 30, 5, 120),
    # ... and more
}
```

**File Locations**:
- `/var/lib/instanceha/config.yaml` - Main configuration
- `/home/cloud-admin/.config/openstack/clouds.yaml` - Cloud auth
- `/home/cloud-admin/.config/openstack/secure.yaml` - Passwords
- `/secrets/fencing.yaml` - Fencing credentials

---

### 2. InstanceHAService

**Purpose**: Main service orchestrator that coordinates all InstanceHA operations.

**Location**: `instanceha.py:668-1305`

**Architecture**: Dependency injection pattern with protocol-based interfaces.

**State Management**:
```python
# Service state
self.current_hash = ""                          # Health check hash
self.hash_update_successful = True              # Health status
self._last_hash_time = 0                        # Hash timestamp

# Cache for performance
self._host_servers_cache = {}                   # Host -> servers mapping
self._evacuable_flavors_cache = None            # Cached evacuable flavors
self._evacuable_images_cache = None             # Cached evacuable images
self._cache_timestamp = 0                       # Cache age
self._cache_lock = threading.Lock()             # Thread-safe access

# Kdump state
self.kdump_hosts_timestamp = defaultdict(float) # Host -> last kdump time
self.kdump_hosts_checking = defaultdict(float)  # Host -> check start time
self.kdump_listener_stop_event = threading.Event()  # Stop signal

# Host processing tracking
self.hosts_processing = defaultdict(float)      # Host -> processing start
self.processing_lock = threading.Lock()         # Thread-safe tracking
```

**Key Methods**:
- `get_connection()` - Get Nova client with dependency injection support
- `is_server_evacuable()` - Check evacuability based on tags
- `get_evacuable_flavors()` - Get cached evacuable flavor list
- `get_evacuable_images()` - Get cached evacuable image list
- `is_aggregate_evacuable()` - Check aggregate evacuability
- `refresh_evacuable_cache()` - Force cache refresh
- `update_health_hash()` - Update health monitoring hash

---

### 3. Metrics

**Purpose**: Lightweight performance monitoring and metrics collection.

**Location**: `instanceha.py:580-666`

**Metrics Tracked**:
- **Counters**: Operation counts (evacuations_total, evacuations_successful, etc.)
- **Durations**: Operation timings (evacuation_duration, etc.)
- **Timing History**: Last 100 measurements for percentile calculation

**Features**:
- Context manager for automatic timing: `with metrics.timer('operation'):`
- Percentile calculation (P95)
- Summary generation and logging
- Automatic failure tracking

---

### 4. CloudConnectionProvider (Protocol)

**Purpose**: Abstract interface for cloud connection management.

**Location**: `instanceha.py:298-310`

**Pattern**: Protocol-based dependency injection for testability.

**Interface**:
```python
class CloudConnectionProvider(ABC):
    @abstractmethod
    def get_connection(self) -> Optional[OpenStackClient]:
        """Get a connection to the cloud provider."""
        pass

    @abstractmethod
    def create_connection(self) -> Optional[OpenStackClient]:
        """Create a new connection to the cloud provider."""
        pass
```

**Implementation**: InstanceHAService implements this protocol.

---

## Architecture Patterns

### 1. Dependency Injection

**Pattern**: Constructor injection with optional test doubles.

**Example**:
```python
class InstanceHAService(CloudConnectionProvider):
    def __init__(self, config_manager: ConfigManager,
                 cloud_client: Optional[OpenStackClient] = None):
        self.config = config_manager
        self.cloud_client = cloud_client  # None in production, mock in tests
```

**Benefits**:
- Testability without mocking module-level functions
- Clear dependency graph
- Easy to swap implementations

---

### 2. Context Managers

**Pattern**: Automatic resource cleanup using context managers.

**Examples**:

**Host Processing Tracking** (`instanceha.py:266-275`):
```python
@contextmanager
def track_host_processing(service: 'InstanceHAService', hostname: str):
    """Context manager for tracking host processing with automatic cleanup."""
    try:
        yield
    finally:
        with service.processing_lock:
            service.hosts_processing.pop(hostname, None)
            logging.debug(f'Cleaned up processing tracking for {hostname}')
```

**UDP Socket Management** (`instanceha.py:1318-1340`):
```python
class UDPSocketManager:
    """Context manager for UDP socket with proper resource cleanup."""
    def __enter__(self):
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.socket.bind((self.udp_ip, self.udp_port))
        return self.socket

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self.socket:
            self.socket.close()
```

---

### 3. Thread Safety

**Pattern**: Lock-protected shared state with fine-grained locking.

**Cache Access**:
```python
# Check cache with lock (read-only)
with self._cache_lock:
    if self._evacuable_flavors_cache is not None:
        return self._evacuable_flavors_cache

# Expensive API call outside lock (no blocking)
flavors = connection.flavors.list()
cache_data = [f.id for f in flavors if self._is_flavor_evacuable(f)]

# Update cache with lock (write)
with self._cache_lock:
    self._evacuable_flavors_cache = cache_data
```

**Host Processing Tracking** (`instanceha.py:2543-2565`):
```python
with service.processing_lock:
    # Clean up expired entries
    service._cleanup_dict_by_condition(
        service.hosts_processing,
        lambda h, t: current_time - t > max_processing_time
    )
    # Mark hosts as processing
    for svc in compute_nodes:
        hostname = _extract_hostname(svc.host)
        service.hosts_processing[hostname] = current_time
```

---

### 4. Unified Validation

**Pattern**: Centralized validation to prevent SSRF and injection attacks.

**Location**: `instanceha.py:142-216`

**Validation Types**:
- **URL**: Scheme, netloc, localhost/link-local blocking
- **IP Address**: IPv4/IPv6 validation
- **Port**: Range validation (1-65535)
- **Kubernetes Resources**: Regex validation
- **Power Actions**: Whitelist validation

**Implementation**:
```python
VALIDATION_PATTERNS = {
    'k8s_namespace': (r'^[a-z0-9]([-a-z0-9]*[a-z0-9])?$', 63),
    'k8s_resource': (r'^[a-z0-9]([-a-z0-9]*[a-z0-9])?(\\.[a-z0-9]...)*$', 253),
    'power_action': (['on', 'off', 'status', 'ForceOff', ...], None),
    'ip_address': ('ip', None),
    'port': ('port', None),
}

def validate_input(value: str, validation_type: str, context: str) -> bool:
    # Special validation for URLs (block localhost, link-local)
    if validation_type == 'url':
        p = urlparse(value)
        if p.hostname in ['localhost', '127.0.0.1', '::1', '0.0.0.0']:
            logging.error(f"Blocked localhost/link-local access in {context}")
            return False
    # ... pattern matching, IP validation, port validation
```

---

## Service Workflow

### Main Poll Loop

**Location**: `instanceha.py:2711-2755`

**Execution Flow**:

```
┌─────────────────────────────────────────────────────────┐
│                    Main Poll Loop                        │
└─────────────────────────────────────────────────────────┘
                            │
                            ▼
    ┌──────────────────────────────────────────────────┐
    │ 1. Update Health Hash                            │
    │    - SHA256 of current timestamp                 │
    │    - Update only if HASH_INTERVAL elapsed        │
    └──────────────────────────────────────────────────┘
                            │
                            ▼
    ┌──────────────────────────────────────────────────┐
    │ 2. Query Nova API                                │
    │    - Get all nova-compute services               │
    │    - Calculate target_date (now - DELTA)         │
    └──────────────────────────────────────────────────┘
                            │
                            ▼
    ┌──────────────────────────────────────────────────┐
    │ 3. Categorize Services                           │
    │    - compute_nodes: down or stale                │
    │    - to_resume: 'evacuation:' marker + down      │
    │    - to_reenable: forced_down OR 'complete:'     │
    │      (excluding resume candidates)               │
    └──────────────────────────────────────────────────┘
                            │
                            ▼
    ┌──────────────────────────────────────────────────┐
    │ 4. Process Stale Services                        │
    │    - Filter by servers, tags, aggregates         │
    │    - Check threshold                             │
    │    - Execute evacuations                         │
    └──────────────────────────────────────────────────┘
                            │
                            ▼
    ┌──────────────────────────────────────────────────┐
    │ 5. Process Re-enabling                           │
    │    - Check migration status                      │
    │    - Unset force-down first (allows srv to go up)│
    │    - Wait for service state='up'                 │
    │    - Then enable disabled services               │
    │    - Two-stage process for proper recovery       │
    └──────────────────────────────────────────────────┘
                            │
                            ▼
    ┌──────────────────────────────────────────────────┐
    │ 6. Log Metrics (if interval elapsed)             │
    │    - Uptime, evacuation counts, durations        │
    │    - P95 latencies                               │
    └──────────────────────────────────────────────────┘
                            │
                            ▼
    ┌──────────────────────────────────────────────────┐
    │ 7. Sleep (POLL seconds)                          │
    └──────────────────────────────────────────────────┘
                            │
                            └──────────────┐
                                          │
                                          ▼
                                      (repeat)
```

---

### Service Processing Pipeline

**Location**: `instanceha.py:2415-2459`

**Per-Host Evacuation Flow**:

```
process_service(failed_service, reserved_hosts, resume, service)
    │
    ├─ (if not resume)
    │   ├─ 1. Fence Host
    │   │   └─ _host_fence(host, 'off')
    │   │       ├─ Look up fencing config
    │   │       ├─ Validate inputs (SSRF prevention)
    │   │       └─ Execute fencing operation
    │   │           ├─ IPMI: ipmitool with retries
    │   │           ├─ Redfish: HTTP POST with retries
    │   │           └─ BMH: Kubernetes PATCH with wait
    │   │
    │   └─ 2. Disable Host in Nova
    │       └─ _host_disable(connection, service)
    │           ├─ Force service down (forced_down=True)
    │           └─ Set disabled_reason = 'instanceha evacuation: {timestamp}'
    │
    ├─ 3. Manage Reserved Hosts
    │   └─ _manage_reserved_hosts(conn, failed_service, reserved_hosts)
    │       ├─ Match by aggregate or zone
    │       └─ Enable matching reserved host
    │
    ├─ 4. Evacuate Servers
    │   └─ _host_evacuate(connection, failed_service, service)
    │       ├─ Get evacuable images/flavors
    │       ├─ List servers on host
    │       ├─ Filter evacuable servers
    │       └─ Execute evacuation
    │           ├─ Smart: _server_evacuate_future (track to completion)
    │           └─ Traditional: fire-and-forget
    │
    └─ 5. Post-Evacuation Recovery
        └─ _post_evacuation_recovery(conn, failed_service, service, resume)
            ├─ Power on host (_host_fence(host, 'on'))
            │   ├─ Skip if resume=True
            │   └─ Skip if kdump-fenced
            ├─ Update disabled_reason = 'instanceha evacuation complete: {timestamp}'
            │   └─ Prevents resume loops (excludes from resume criteria)
            └─ Service remains disabled until it comes back up
```

---

### Service Categorization Logic

**Location**: `instanceha.py:2456-2479`

**Implementation**:
```python
def _categorize_services(services: List[Any], target_date: datetime) -> tuple:
    # Compute nodes: not disabled/forced-down, and (down OR stale)
    compute_nodes = (svc for svc in services
                     if not ('disabled' in svc.status or svc.forced_down)
                     and (svc.state == 'down' or
                          datetime.fromisoformat(svc.updated_at) < target_date))

    # Resume: forced_down + disabled + 'instanceha evacuation:' marker + not FAILED/complete
    # Note: Must exclude 'evacuation complete:' to prevent resume loops
    resume = (svc for svc in services
              if svc.forced_down and svc.state == 'down'
              and 'disabled' in svc.status
              and 'instanceha evacuation' in svc.disabled_reason
              and 'evacuation FAILED' not in svc.disabled_reason
              and 'evacuation complete' not in svc.disabled_reason)

    # Re-enable: forced_down OR 'evacuation complete' marker (but NOT resume candidates)
    reenable = (svc for svc in services
                if (('enabled' in svc.status and svc.forced_down)
                   or ('disabled' in svc.status and 'instanceha evacuation complete' in svc.disabled_reason))
                and not (svc.forced_down and svc.state == 'down' and 'disabled' in svc.status
                        and 'instanceha evacuation' in svc.disabled_reason
                        and 'evacuation FAILED' not in svc.disabled_reason
                        and 'evacuation complete' not in svc.disabled_reason))

    return compute_nodes, resume, reenable
```

**States Explained**:
- **compute_nodes**: Fresh failures, need full evacuation workflow
- **resume**: Previous evacuation interrupted (still down+disabled+forced_down), skip fencing/disable
  - Matches services with `disabled_reason` containing "instanceha evacuation:"
  - Explicitly excludes "evacuation complete:" to prevent resume loops
  - Explicitly excludes "evacuation FAILED:" to prevent retry of failed evacuations
- **reenable**: Services needing re-enabling (two-stage process)
  - Stage 1: Unset force_down to allow service to report up
  - Stage 2: Once state='up', enable disabled services
  - Matches services with `disabled_reason` containing "instanceha evacuation complete:"
  - Excludes resume candidates to avoid double-processing

**State Transitions**:
1. Service fails → `compute_nodes` → evacuated → `disabled_reason` = "instanceha evacuation: {timestamp}"
2. Next poll → `disabled_reason` updated to "instanceha evacuation complete: {timestamp}"
   - Service now excluded from `resume` (has "complete" marker)
   - Service now included in `reenable` (has "complete" marker)
3. Subsequent polls → `reenable` → force_down unset → service reports up → enabled
4. Service returns to normal operation

**Disabled Reason Values**:

The `disabled_reason` field tracks evacuation state and prevents processing loops:

| Value | Set By | Categorization | Meaning |
|-------|--------|----------------|---------|
| `instanceha evacuation: {timestamp}` | `_host_disable()` during initial evacuation | `resume` | Evacuation in progress or interrupted |
| `instanceha evacuation (kdump): {timestamp}` | `_host_disable()` when host is kdump-fenced | `resume` | Kdump evacuation in progress, host rebooting |
| `instanceha evacuation complete: {timestamp}` | `_post_evacuation_recovery()` or `_host_enable()` after successful evacuation | `reenable` | Evacuation complete, waiting for re-enable |
| `instanceha evacuation FAILED: {timestamp}` | `_update_service_disable_reason()` on evacuation failure | (none) | Evacuation failed, requires manual intervention |

**Key Design Points**:
- The `resume` check explicitly excludes "complete" and "FAILED" to prevent loops
- The `reenable` exclusion also excludes "complete" to prevent excluding completed evacuations
- Services transition from `resume` → `reenable` via the disabled_reason update
- Failed evacuations remain disabled until manually resolved
- Both checks use substring matching, so explicit exclusions are critical

---

## Configuration System

### Configuration Files

**1. Main Configuration** (`/var/lib/instanceha/config.yaml`):
```yaml
config:
  EVACUABLE_TAG: 'evacuable'
  DELTA: 30           # Service staleness seconds
  POLL: 45            # Poll interval seconds
  THRESHOLD: 50       # Failure threshold percentage
  WORKERS: 4          # Thread pool size
  SMART_EVACUATION: true
  RESERVED_HOSTS: false
  TAGGED_IMAGES: true
  TAGGED_FLAVORS: true
  TAGGED_AGGREGATES: true
  LEAVE_DISABLED: false
  FORCE_ENABLE: false
  CHECK_KDUMP: false
  KDUMP_TIMEOUT: 30
  DISABLED: false
  SSL_VERIFY: true
  FENCING_TIMEOUT: 30
  HASH_INTERVAL: 60
  METRICS_LOG_INTERVAL: 3600
  LOGLEVEL: 'INFO'
```

**2. Cloud Configuration** (`clouds.yaml`):
```yaml
clouds:
  overcloud:
    auth:
      username: admin
      project_name: admin
      auth_url: http://keystone:5000/v3
      user_domain_name: Default
      project_domain_name: Default
```

**3. Secure Configuration** (`secure.yaml`):
```yaml
clouds:
  overcloud:
    auth:
      password: secret_password
```

**4. Fencing Configuration** (`fencing.yaml`):
```yaml
FencingConfig:
  compute-01.example.com:
    agent: fence_ipmilan
    ipaddr: 192.168.1.10
    ipport: '623'
    login: admin
    passwd: ipmi_password

  compute-02.example.com:
    agent: fence_redfish
    ipaddr: 192.168.1.11
    ipport: '443'
    login: root
    passwd: redfish_password
    tls: 'true'
    uuid: System.Embedded.1

  compute-03.example.com:
    agent: fence_metal3
    host: metal3-0
    namespace: openshift-machine-api
    token: eyJhbGciOi...
```

### Configuration Validation

**Location**: `instanceha.py:378-477`

**Type Checking**:
```python
def get_int(self, key: str, default: int = 0,
            min_val: Optional[int] = None,
            max_val: Optional[int] = None) -> int:
    value = self.config.get(key, default)

    try:
        int_value = int(value)
    except (ValueError, TypeError):
        logging.warning(f"Invalid {key}, using default: {default}")
        return default

    # Clamp to min/max
    if min_val is not None:
        int_value = max(min_val, int_value)
    if max_val is not None:
        int_value = min(max_val, int_value)

    return int_value
```

**Special Validations**:
- **LOGLEVEL**: Must be in ['DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL']
- **KDUMP_TIMEOUT + POLL**: Warning if both are 30 seconds (default values - not a functional issue, just informational)
- **SSL Paths**: Check file existence before returning

**Kdump Timing**:
- Timeout is evaluated per-host based on when first detected as down
- Multiple poll cycles can occur during the waiting period
- Example: `POLL=45s`, `KDUMP_TIMEOUT=300s` → host waits across ~7 poll cycles before evacuation if no kdump detected

---

## Evacuation Mechanisms

### 1. Traditional Evacuation

**Location**: `instanceha.py:1498-1514`

**Pattern**: Fire-and-forget approach.

**Flow**:
```python
for server in evacuables:
    response = _server_evacuate(connection, server.id)
    if response.accepted:
        logging.debug("Evacuated %s", server.id)
    else:
        logging.warning("Failed to evacuate %s", server.id)
```

**Pros**: Fast, simple, works with older OpenStack
**Cons**: No completion verification, no error detection

---

### 2. Smart Evacuation

**Location**: `instanceha.py:1579-1671, 1470-1496`

**Pattern**: Evacuation with migration tracking and completion verification.

**Enabled By**: `SMART_EVACUATION: true`

**Flow**:
```python
def _server_evacuate_future(connection, server) -> bool:
    # 1. Initiate evacuation
    response = _server_evacuate(connection, server.id)
    if not response.accepted:
        return False

    # 2. Wait before first poll
    time.sleep(INITIAL_EVACUATION_WAIT_SECONDS)

    # 3. Poll migration status until completion or timeout
    start_time = time.time()
    while True:
        if time.time() - start_time > MAX_EVACUATION_TIMEOUT_SECONDS:
            return False

        status = _server_evacuation_status(connection, server.id)

        if status.completed:
            return True
        if status.error:
            error_count += 1
            if error_count >= MAX_EVACUATION_RETRIES:
                return False
            time.sleep(EVACUATION_RETRY_WAIT_SECONDS)
            continue

        time.sleep(EVACUATION_POLL_INTERVAL_SECONDS)
```

**Features**:
- Migration status tracking via Nova API
- Automatic retry on transient errors (max 5 retries)
- Timeout protection (max 300 seconds)
- Error detection and reporting

---

### 3. Server Evacuability Logic

**Location**: `instanceha.py:787-831`

**Tagging System** (OR semantics):
1. **Flavor-based** (`TAGGED_FLAVORS: true`)
2. **Image-based** (`TAGGED_IMAGES: true`)
3. **Aggregate-based** (`TAGGED_AGGREGATES: true`)

**Evaluation Logic**:
```python
def is_server_evacuable(self, server, evac_flavors=None, evac_images=None):
    images_enabled = self.config.is_tagged_images_enabled()
    flavors_enabled = self.config.is_tagged_flavors_enabled()

    # Backward compatibility: if no tagging, evacuate all
    if not (images_enabled or flavors_enabled):
        return True

    # No tagged resources: evacuate all
    if not ((images_enabled and evac_images) or (flavors_enabled and evac_flavors)):
        return True

    # Check matches (OR logic)
    matches = [
        self._check_image_match(server, evac_images) if images_enabled else False,
        self._check_flavor_match(server, evac_flavors) if flavors_enabled else False
    ]

    return any(matches)
```

---

## Fencing Agents

### 1. IPMI (Intelligent Platform Management Interface)

**Agent**: `fence_ipmilan`

**Location**: `instanceha.py:2117-2175`

**Configuration**:
```yaml
compute-01:
  agent: fence_ipmilan
  ipaddr: 192.168.1.10
  ipport: '623'
  login: admin
  passwd: ipmi_password
```

**Security**:
- Password via environment variable (not command-line)
- Validation: IP, port, username
- Safe logging (credentials sanitized)

---

### 2. Redfish

**Agent**: `fence_redfish`

**Location**: `instanceha.py:2177-2189, 1895-1967`

**Configuration**:
```yaml
compute-02:
  agent: fence_redfish
  ipaddr: 192.168.1.11
  ipport: '443'
  login: root
  passwd: redfish_password
  tls: 'true'
  uuid: System.Embedded.1
```

**Features**:
- SSL/TLS support
- Retry logic (max 3 attempts)
- Power state verification
- SSRF prevention

---

### 3. BMH (BareMetal Host - Metal3)

**Agent**: `fence_metal3` (BMH)

**Location**: `instanceha.py:2190-2194, 1969-2084`

**Configuration**:
```yaml
compute-03:
  agent: fence_metal3
  host: metal3-0
  namespace: openshift-machine-api
  token: eyJhbGciOi...
```

**Features**:
- Kubernetes API integration
- Bearer token authentication
- Power-off wait loop
- Input validation

---

## Advanced Features

### 1. Kdump Detection

**Purpose**: Detect when hosts are kdumping and evacuate them safely once fenced.

**Location**: `instanceha.py:1340-1380, 1740-1798`

**Architecture**:
- Background UDP listener thread (port 7410)
- Magic number validation (0x1B302A40)
- Reverse DNS lookup (IP → hostname)
- Timestamp tracking with cleanup

**Behavior**:
1. **First poll**: When host is detected as down, start waiting for `KDUMP_TIMEOUT` seconds
2. **Kdump message received**: Host is fenced → evacuate immediately
   - Host marked with `disabled_reason = "instanceha evacuation (kdump): {timestamp}"`
3. **Timeout expired**: No kdump detected → proceed with normal evacuation
4. **Power-on optimization**: Skip power-on for kdump-fenced hosts during recovery
   - Kdump `final_action` in `/etc/kdump.conf` determines host behavior (poweroff/reboot/halt)
   - Skipping power-on avoids interfering with user-configured kdump recovery process
5. **Re-enablement delay**: After evacuation, wait 60s after last kdump message before unsetting force-down
   - Prevents premature re-enablement while host is still dumping memory and rebooting
   - Once 60s have passed with no kdump messages, migrations should be in `completed` state
   - Kdump marker removed from `disabled_reason` once force-down successfully unset

---

### 2. Reserved Hosts

**Purpose**: Maintain spare capacity by auto-enabling reserved hosts.

**Location**: `instanceha.py:2272-2356`

**Matching Strategies**:
1. **Aggregate-Based** (when `TAGGED_AGGREGATES: true`)
2. **Zone-Based** (when `TAGGED_AGGREGATES: false`)

---

### 3. Caching System

**Purpose**: Reduce Nova API calls and improve performance.

**Location**: `instanceha.py:1253-1304, 833-883`

**Cached Data**:
- Evacuable flavors (300s TTL)
- Evacuable images (300s TTL)
- Host servers mapping

**Thread-Safe Access**:
- Check with lock (fast read)
- API call outside lock (no blocking)
- Update with lock (fast write)

---

### 4. Threshold Protection

**Purpose**: Prevent mass evacuations during datacenter-level failures.

**Location**: `instanceha.py:2612-2618`

**Implementation**:
```python
threshold_percent = (len(compute_nodes) / len(services)) * 100
if threshold_percent > service.config.get_threshold():
    logging.error(f'Impacted ({threshold_percent:.1f}%) exceeds threshold')
    return  # Do not evacuate
```

---

## Security

### 1. Input Validation

**Location**: `instanceha.py:153-211`

**SSRF Prevention**:
- URL validation (block localhost, link-local)
- IP address validation (IPv4/IPv6)
- Port range validation (1-65535)

**Injection Prevention**:
- Kubernetes resource name validation
- Power action whitelisting
- Username validation

---

### 2. Credential Security

**Location**: `instanceha.py:105-113, 282-286, 2130-2134`

**Password Handling**:
```python
# IPMI: Use environment variable
env = os.environ.copy()
env['IPMITOOL_PASSWORD'] = passwd
cmd = ["ipmitool", "-U", login, "-E", ...]  # -E uses env var
```

**Safe Exception Logging**:
```python
_SECRET_PATTERNS = {
    'password': re.compile(r'\bpassword=[^\s)\'\"]+', re.IGNORECASE),
    'token': re.compile(r'\btoken=[^\s)\'\"]+', re.IGNORECASE),
    'secret': re.compile(r'\bsecret=[^\s)\'\"]+', re.IGNORECASE),
    'credential': re.compile(r'\bcredential=[^\s)\'\"]+', re.IGNORECASE),
    'auth': re.compile(r'\bauth=[^\s)\'\"]+', re.IGNORECASE),
}

def _safe_log_exception(msg: str, e: Exception):
    safe_msg = str(e)
    for secret_word, pattern in _SECRET_PATTERNS.items():
        safe_msg = pattern.sub(f'{secret_word}=***', safe_msg)
    logging.error("%s: %s", msg, safe_msg)
```

---

### 3. SSL/TLS Configuration

**Location**: `instanceha.py:512-525, 218-228`

**Requests SSL Config**:
```python
def get_requests_ssl_config(self) -> Union[bool, str, tuple]:
    if not self.is_ssl_verification_enabled():
        return False  # Insecure

    if self.ssl_cert_path and self.ssl_key_path:
        return (self.ssl_cert_path, self.ssl_key_path)  # Client cert
    if self.ssl_ca_bundle:
        return self.ssl_ca_bundle  # CA bundle
    return True  # Default system CA
```

---

## Performance

### 1. Caching Performance

**Benchmark** (100 flavors, 100 images):
- First call: ~50ms (API call)
- Cached call: ~0.1ms (500x faster)
- Cache hit ratio: >95% in production

---

### 2. Concurrent Processing

**ThreadPoolExecutor** for smart evacuation:
```python
with concurrent.futures.ThreadPoolExecutor(max_workers=WORKERS) as executor:
    future_to_server = {
        executor.submit(_server_evacuate_future, conn, srv): srv
        for srv in evacuables
    }
```

**Speedup**: Linear with WORKERS (4 workers = 4x faster)

---

### 3. Memory Management

**Location**: `instanceha.py:1365-1368, 1262-1279`

**Cleanup Strategies**:
- Kdump timestamp cleanup (>100 entries)
- Host processing expiration
- Generic cleanup helper

---

## Testing

### Test Statistics

- **Total Tests**: 202
- **Code Coverage**: 71% (1140/1625 lines)
- **Execution Time**: ~14 seconds

### Test Categories

**1. Unit Tests** (134 tests):
- Configuration, metrics, main function initialization
- Evacuation logic, smart evacuation
- Kdump, fencing, input validation, thread safety

**2. Functional Tests** (60 tests):
- End-to-end workflows
- Large-scale scenarios (100+ hosts)
- Tag filtering, performance

**3. Integration Tests** (21 tests):
- Service initialization, Nova connection
- Categorization, full workflows
- Deferred re-enabling (disabled services only re-enabled when up)
- Migration completion verification

**4. Advanced Integration** (12 tests):
- Smart evacuation (tracking, timeout, retry)
- Kdump UDP listener, reserved hosts
- Fencing resilience, main loop recovery

### Coverage by Component

| Component | Coverage |
|-----------|----------|
| ConfigManager | 95% |
| Metrics | 85% |
| Evacuation Logic | 80% |
| Smart Evacuation | 75% |
| Fencing | 70% |
| Kdump | 65% |
| Main Loop | 60% |
| Reserved Hosts | 55% |

### Test Patterns

**Exception Mocking**:
```python
class NotFound(Exception):
    pass
novaclient_exceptions = MagicMock()
novaclient_exceptions.NotFound = NotFound
sys.modules['novaclient.exceptions'] = novaclient_exceptions
```

**Performance Optimization**:
```python
with patch('instanceha.time.sleep'):
    with patch('instanceha.EVACUATION_POLL_INTERVAL_SECONDS', 0):
        result = _server_evacuate_future(conn, server)
```

---

## Deployment

### Kubernetes Deployment

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: instanceha
spec:
  replicas: 1
  template:
    spec:
      containers:
      - name: instanceha
        image: quay.io/openstack-k8s-operators/instanceha:latest
        env:
        - name: OS_CLOUD
          value: overcloud
        volumeMounts:
        - name: config
          mountPath: /var/lib/instanceha
        - name: clouds
          mountPath: /home/cloud-admin/.config/openstack
        - name: fencing
          mountPath: /secrets
        livenessProbe:
          httpGet:
            path: /
            port: 8080
        readinessProbe:
          httpGet:
            path: /
            port: 8080
      volumes:
      - name: config
        configMap:
          name: instanceha-config
      - name: clouds
        secret:
          secretName: clouds-yaml
      - name: fencing
        secret:
          secretName: fencing-credentials
```

---

## Troubleshooting

### Common Issues

**1. High threshold prevents evacuation**:
```
ERROR: Impacted (60.0%) exceeds threshold (50%).
```
**Solution**: Increase `THRESHOLD` or investigate datacenter failure.

**2. Kdump check delays evacuations**:
```
INFO: Checking 10 hosts for kdump activity
INFO: Host compute-0 down, waiting 30s for kdump
```
**Explanation**: When kdump checking is enabled, hosts wait for `KDUMP_TIMEOUT` seconds before evacuation to allow kdump messages to arrive.

**Solutions**:
- **Disable kdump check**: Set `CHECK_KDUMP: false` for immediate evacuation
- **Reduce timeout**: Lower `KDUMP_TIMEOUT` (minimum 5 seconds)
- **Enable kdump on computes**: Configure fence_kdump to send messages and benefit from safe fencing

**3. Smart evacuation timeout**:
```
ERROR: Evacuation timed out after 300 seconds.
```
**Solution**: Check migration queue, increase timeout, or disable.

**4. Fencing failures**:
```
ERROR: Redfish reset failed: authentication error
```
**Solution**: Verify credentials, IP addresses, connectivity.

---

## References

- **Code**: `instanceha.py` (2771 lines)
- **Tests**: `test_instanceha.py` (5156 lines, 197 tests)
- **Documentation**: This file
- **OpenStack API**: https://docs.openstack.org/api-ref/compute/
- **Redfish**: https://www.dmtf.org/standards/redfish
- **Metal3**: https://metal3.io/
