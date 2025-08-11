# InstanceHA Comprehensive Test Suite

This directory contains a comprehensive test suite for the InstanceHA service that validates functionality across multiple levels of integration.

## Test Structure

The test suite is organized into three main categories:

### 1. Unit Tests (`test_instanceha.py`)
- **48 tests** validating individual components
- Configuration management and validation
- Metrics collection and timing
- Service initialization and caching
- Evacuation logic and tag checking
- Kdump detection and UDP message processing
- Thread safety and memory management

### 2. Functional Tests (`functional_test.py`)
- **56 tests** validating end-to-end scenarios
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

The comprehensive test suite provides coverage for:

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
- Smart evacuation with threading
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
- Partial evacuation failures and recovery
- Network errors and permission issues
- Malformed data handling

### Security and Reliability
- Input validation and sanitization
- Password security in command execution
- Thread safety for concurrent operations
- Resource cleanup and leak prevention

## Test Results

Recent test run results:

```
========================================
            Test Summary
========================================
[PASS] Unit Tests: PASSED (48/48 tests)
[PASS] Functional Tests: PASSED (56/56 tests)
[PASS] Integration Tests: PASSED (19/19 tests)

Total: 3/3 test suites passed
[PASS] All tests completed successfully!
```

## Test Environment

The tests use comprehensive mocking to simulate:
- OpenStack Nova API responses
- Service states and server configurations
- Network conditions and timeouts
- Configuration file loading
- Threading and concurrency scenarios

This approach ensures tests are:
- Fast and reliable (no external dependencies)
- Deterministic and repeatable
- Isolated from system configuration
- Safe to run in CI/CD environments

## Code Quality Validation

The test suite validates the recent code quality improvements:

### Security Fixes
- Password protection in IPMI commands (environment variables)
- Input validation for IP addresses, ports, and usernames
- Protection against command injection attacks

### Architecture Improvements
- Function complexity reduction (main() refactored into 6 focused functions)
- Improved testability through dependency injection
- Better separation of concerns
- Enhanced error handling patterns

### Performance Optimizations
- Caching mechanisms for repeated API calls
- Efficient batch processing of services
- Memory management for large deployments
- Concurrent processing capabilities

The comprehensive test suite ensures that all improvements maintain backward compatibility while enhancing the service's reliability, security, and maintainability.
