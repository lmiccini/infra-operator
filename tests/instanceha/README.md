# InstanceHA Comprehensive Test Suite

This directory contains a comprehensive test suite for the InstanceHA service that validates functionality across multiple levels of integration.

## Test Structure

The test suite is organized into three main categories:

### 1. Unit Tests (`test_instanceha.py`)
- **122 tests** validating individual components
- Configuration management and validation
- Metrics collection and timing
- Service initialization and caching
- Evacuation logic and tag checking
- Smart evacuation with success, failure, and exception path testing
- Kdump detection and UDP message processing
- Redfish and IPMI fencing operations
- Thread safety and memory management
- Security and secret sanitization

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

## Test Coverage

The test suite provides coverage for:

### Core Functionality
- Service initialization and configuration loading
- Nova API connection establishment
- Service state categorization (stale, resume, re-enable)
- Host and server filtering logic
- Evacuation workflow execution
- Service re-enabling after evacuation

### Advanced Features
- Flavor-based tagging and filtering
- Image-based tagging and filtering
- Aggregate-based filtering
- Reserved host management
- Smart evacuation with threading and comprehensive error handling:
  - Successful evacuation scenarios
  - Partial failure scenarios with host marking
  - Exception handling during evacuation with proper host marking
- Kdump detection and filtering
- Threshold protection against mass failures

### Performance and Scaling
- Large-scale evacuations (100+ hosts)
- Caching mechanisms for flavors, images, and servers
- Concurrent evacuation processing
- Memory management and cleanup
- Performance benchmarks

### Error Handling
- Nova API failures and timeouts
- Configuration errors and validation
- Partial evacuation failures and recovery with proper host marking
- Exception handling in smart evacuation (ensures failed hosts are marked appropriately)
- Network errors and permission issues
- Malformed data handling

### Security and Reliability
- Input validation and sanitization
- Password security in command execution
- Thread safety for concurrent operations
- Resource cleanup and leak prevention
