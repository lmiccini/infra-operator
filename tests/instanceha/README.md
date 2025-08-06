# InstanceHA Test Suite

## Running Tests

To run the complete test suite (unit tests, functional tests, and kdump functionality):

```bash
cd tests/instanceha
./run_tests.sh
```

This comprehensive test runner includes:
- **Unit Tests** (48 tests): Core functionality, configuration, metrics, kdump unit tests, and integration tests
- **Functional Tests** (50 tests): End-to-end scenarios, evacuation logic, kdump functional tests, and edge cases

### Test Coverage

- **Configuration Management**: Loading, validation, type conversion
- **Evacuation Logic**: Image/flavor tagging, aggregates, thresholds, reserved hosts  
- **Kdump Functionality**: UDP message processing, timeout handling, thread safety, background listener
- **Performance**: Caching, large-scale scenarios, memory management
- **Error Handling**: Network errors, permission issues, malformed data

Total: **98 tests** with comprehensive coverage of all InstanceHA functionality.

### Development

- All tests include warning suppression for missing config files in test environments
- Tests are organized into logical test classes for maintainability
- Both unit and functional tests include extensive kdump coverage
- Mock objects simulate OpenStack services for isolated testing