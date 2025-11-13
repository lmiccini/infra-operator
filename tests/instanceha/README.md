# InstanceHA Comprehensive Test Suite

This directory contains a comprehensive test suite for the InstanceHA service that validates functionality across multiple levels of integration.

## Test Statistics

- **Total Tests**: 197
- **Code Coverage**: 70% (1132/1625 lines)
- **Execution Time**: ~14 seconds
- **Status**: All tests passing ✅

## Test Structure

The test suite is organized into four main categories:

### 1. Unit Tests (`test_instanceha.py`)
- **131 tests** validating individual components and isolated functionality
- Configuration management and validation
- Metrics collection and timing
- Service initialization and caching
- Evacuation logic and tag checking
- Smart evacuation with success, failure, and exception path testing
- Kdump detection and UDP message processing
- Redfish, IPMI, and BMH fencing operations
- Thread safety and memory management
- Security and secret sanitization
- Input validation and SSRF prevention

### 2. Functional Tests (`functional_test.py`)
- **60 tests** validating end-to-end scenarios
- Basic evacuation workflows
- Large-scale evacuation scenarios (100+ hosts)
- Host state classification and filtering
- Tagging logic combinations (images, flavors, aggregates)
- Performance testing with large server lists
- Kdump integration workflows
- Error handling and edge cases

### 3. Integration Tests (`integration_test.py`)
- **19 tests** validating cross-component interactions
- Complete service initialization workflows
- Nova connection establishment
- Service categorization and filtering pipelines
- Full evacuation workflows with all components
- Re-enabling workflows with migration checks
- Performance and scaling under load
- Error handling and recovery scenarios

### 4. Advanced Integration Tests (`TestAdvancedIntegration`)
- **12 tests** targeting critical code paths for coverage improvement
- **Priority 1: Smart Evacuation (3 tests)**
  - Migration tracking to completion
  - Timeout handling
  - Retry logic on errors
- **Priority 2: Advanced Features (4 tests)**
  - Kdump UDP listener operation
  - Reserved hosts matching by aggregate
  - Reserved hosts exhaustion handling
  - Health check server operation
- **Priority 3: Fencing Resilience (3 tests)**
  - Redfish retry logic
  - BMH power-off wait loop
  - IPMI timeout handling
- **Priority 4: Main Loop (2 tests)**
  - Error recovery and continued operation
  - Metrics logging at intervals

## Running Tests

### Run All Tests
```bash
./run_tests.sh
```

### Run Individual Test Suites
```bash
# Unit tests only
python3 test_instanceha.py

# Functional tests only
python3 functional_test.py

# Integration tests only
python3 integration_test.py
```

### Run Coverage Analysis
```bash
python3 -m coverage run test_instanceha.py
python3 -m coverage report
python3 -m coverage html  # Generate HTML report
```

## Test Coverage

### Coverage by Category
- **Configuration Management**: 95%
- **Metrics and Performance**: 85%
- **Evacuation Logic**: 80%
- **Smart Evacuation**: 75%
- **Fencing Operations**: 70%
- **Kdump Detection**: 65%
- **Main Loop**: 60%
- **Reserved Hosts**: 55%

### Core Functionality
- Service initialization and configuration loading
- Nova API connection establishment
- Service state categorization (stale, resume, re-enable)
- Host and server filtering logic
- Evacuation workflow execution
- Service re-enabling after evacuation
- Migration status tracking
- Cache lifecycle management

### Advanced Features
- **Tagging and Filtering**:
  - Flavor-based tagging and filtering
  - Image-based tagging and filtering
  - Aggregate-based filtering
  - Combined tag logic (OR semantics)
- **Reserved Host Management**:
  - Aggregate-based matching
  - Zone-based matching
  - Exhaustion handling
- **Smart Evacuation**:
  - Successful evacuation scenarios
  - Migration tracking with status polling
  - Timeout handling
  - Retry logic on transient errors
  - Partial failure scenarios with host marking
  - Exception handling during evacuation with proper host marking
- **Kdump Detection**:
  - UDP listener thread operation
  - Message parsing and validation (magic number 0x1B302A40)
  - Waiting mechanism for KDUMP_TIMEOUT before evacuation
  - Immediate evacuation when kdump messages received (kdump-fenced)
  - Timeout-based evacuation when no kdump detected
  - Power-on skip optimization for kdump-fenced hosts
  - Cleanup of old entries and tracking state
- **Threshold Protection**:
  - Mass failure prevention
  - Percentage-based limits

### Performance and Scaling
- Large-scale evacuations (100+ hosts)
- Caching mechanisms for flavors, images, and servers
- Cache refresh and invalidation
- Concurrent evacuation processing with ThreadPoolExecutor
- Memory management and cleanup
- Performance benchmarks (categorization < 1s for 100 services)
- Optimized test execution (time.sleep() mocking)

### Error Handling
- Nova API failures and timeouts
- Configuration errors and validation
- Partial evacuation failures and recovery with proper host marking
- Exception handling in smart evacuation (ensures failed hosts are marked appropriately)
- Network errors and permission issues
- Malformed data handling
- Missing resource handling
- Main loop error recovery and continuation

### Security and Reliability
- **Input Validation**:
  - SSRF prevention (URL, IP, port validation)
  - Injection attack prevention
  - Kubernetes resource name validation
  - Power action whitelisting
- **Password Security**:
  - Environment variable usage for IPMI passwords
  - Credential sanitization in logs
  - Safe exception logging
- **Thread Safety**:
  - Lock-protected cache operations
  - Concurrent evacuation handling
  - Processing state tracking
- **Resource Management**:
  - Cleanup and leak prevention
  - Context managers for automatic cleanup
  - Background thread lifecycle management

## Test Implementation Patterns

### Exception Class Mocking
Tests create real Exception subclasses instead of MagicMock objects to avoid "catching classes that do not inherit from BaseException" errors:

```python
class NotFound(Exception):
    pass
class Conflict(Exception):
    pass
class Forbidden(Exception):
    pass
class Unauthorized(Exception):
    pass

novaclient_exceptions = MagicMock()
novaclient_exceptions.NotFound = NotFound
novaclient_exceptions.Conflict = Conflict
novaclient_exceptions.Forbidden = Forbidden
novaclient_exceptions.Unauthorized = Unauthorized
sys.modules['novaclient.exceptions'] = novaclient_exceptions
```

### Glance API Mocking
Mocking `glance.list()` to return an empty list instead of Mock object to avoid "TypeError: 'Mock' object is not iterable":

```python
mock_nova.glance = Mock()
mock_nova.glance.list = Mock(return_value=[])  # Return list, not Mock
```

### Performance Optimization
Tests mock `time.sleep()` and timing constants to achieve fast execution:

```python
with patch('instanceha.time.sleep'):  # Mock all sleep calls
    with patch('instanceha.INITIAL_EVACUATION_WAIT_SECONDS', 0):
        with patch('instanceha.EVACUATION_POLL_INTERVAL_SECONDS', 0):
            result = instanceha._server_evacuate_future(conn, server)
```

### Stateful Nova Client Mock
Tests use stateful mock that simulates Nova API behavior:

```python
def _create_mock_nova_stateful():
    state = {
        'services': [],
        'servers': defaultdict(list),
        'migrations': []
    }

    def evacuate_server(server=None, *args, **kwargs):
        migration = Mock(status='completed', instance_uuid=server)
        state['migrations'].append(migration)
        return (Mock(status_code=200, reason='OK'), {})

    mock_nova.servers.evacuate.side_effect = evacuate_server
    return mock_nova, state
```

## Coverage Gaps and Future Work

### High Priority (not yet covered)
- Main poll loop integration (41 lines)
- BMH fencing complete workflow (35 lines)
- Redfish URL building edge cases (15 lines)

### Medium Priority
- Error recovery in background threads
- Cache expiration edge cases
- Complex aggregate matching scenarios

### Low Priority
- Rare exception paths
- Deprecated code paths
- Debug logging branches

## Contributing

When adding new tests:
1. Follow existing patterns for mocking and fixtures
2. Use real Exception subclasses for exception testing
3. Mock `time.sleep()` for fast execution
4. Document test purpose in docstring
5. Verify coverage improvement with `coverage report`
6. Ensure all tests pass before committing
