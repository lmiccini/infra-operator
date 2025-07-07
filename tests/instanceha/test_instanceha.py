#!/usr/bin/env python3

import os
import sys
import unittest
import tempfile
import yaml
import json
import socket
import threading
import time
from unittest.mock import Mock, patch, MagicMock, call
from datetime import datetime, timedelta
from io import StringIO

# Add the module path for testing
#sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.append('../../templates/instanceha/bin/')

# Import the module under test
import instanceha


class TestConfigManager(unittest.TestCase):
    """Test the ConfigManager class functionality."""

    def setUp(self):
        """Set up test fixtures."""
        self.temp_dir = tempfile.mkdtemp()
        self.config_path = os.path.join(self.temp_dir, 'config.yaml')
        self.clouds_path = os.path.join(self.temp_dir, 'clouds.yaml')
        self.secure_path = os.path.join(self.temp_dir, 'secure.yaml')
        self.fencing_path = os.path.join(self.temp_dir, 'fencing.yaml')

        # Create sample configuration files
        self._create_test_configs()

    def tearDown(self):
        """Clean up test fixtures."""
        import shutil
        shutil.rmtree(self.temp_dir)

    def _create_test_configs(self):
        """Create test configuration files."""
        # Main config
        config_data = {
            'config': {
                'EVACUABLE_TAG': 'test_tag',
                'DELTA': 60,
                'POLL': 30,
                'THRESHOLD': 75,
                'WORKERS': 8,
                'LOGLEVEL': 'DEBUG',
                'SMART_EVACUATION': True,
                'RESERVED_HOSTS': True,
                'SSL_VERIFY': False
            }
        }
        with open(self.config_path, 'w') as f:
            yaml.dump(config_data, f)

        # Clouds config
        clouds_data = {
            'clouds': {
                'testcloud': {
                    'auth': {
                        'username': 'test_user',
                        'project_name': 'test_project',
                        'auth_url': 'http://keystone:5000/v3',
                        'user_domain_name': 'Default',
                        'project_domain_name': 'Default'
                    }
                }
            }
        }
        with open(self.clouds_path, 'w') as f:
            yaml.dump(clouds_data, f)

        # Secure config
        secure_data = {
            'clouds': {
                'testcloud': {
                    'auth': {
                        'password': 'test_password'
                    }
                }
            }
        }
        with open(self.secure_path, 'w') as f:
            yaml.dump(secure_data, f)

        # Fencing config
        fencing_data = {
            'FencingConfig': {
                'compute-01': {
                    'agent': 'ipmi',
                    'ipaddr': '192.168.1.100',
                    'ipport': '623',
                    'login': 'admin',
                    'passwd': 'password'
                },
                'compute-02': {
                    'agent': 'redfish',
                    'ipaddr': '192.168.1.101',
                    'login': 'admin',
                    'passwd': 'password'
                }
            }
        }
        with open(self.fencing_path, 'w') as f:
            yaml.dump(fencing_data, f)

    def test_config_manager_initialization(self):
        """Test ConfigManager initialization."""
        config_manager = instanceha.ConfigManager(self.config_path)
        config_manager.clouds_path = self.clouds_path
        config_manager.secure_path = self.secure_path
        config_manager.fencing_path = self.fencing_path
        config_manager.__init__(self.config_path)

        self.assertEqual(config_manager.get_str('EVACUABLE_TAG'), 'test_tag')
        self.assertEqual(config_manager.get_int('DELTA'), 60)
        self.assertEqual(config_manager.get_bool('SMART_EVACUATION'), True)

    def test_config_validation(self):
        """Test configuration validation."""
        config_manager = instanceha.ConfigManager(self.config_path)
        config_manager.clouds_path = self.clouds_path
        config_manager.secure_path = self.secure_path
        config_manager.fencing_path = self.fencing_path
        config_manager.__init__(self.config_path)

        # Test valid configuration
        self.assertEqual(config_manager.get_config_value('DELTA'), 60)
        self.assertEqual(config_manager.get_config_value('LOGLEVEL'), 'DEBUG')

        # Test invalid configuration key
        with self.assertRaises(ValueError):
            config_manager.get_config_value('INVALID_KEY')

    def test_config_type_conversion(self):
        """Test configuration type conversion and validation."""
        config_manager = instanceha.ConfigManager(self.config_path)
        config_manager.clouds_path = self.clouds_path
        config_manager.secure_path = self.secure_path
        config_manager.fencing_path = self.fencing_path
        config_manager.__init__(self.config_path)

        # Test integer bounds
        self.assertEqual(config_manager.get_int('WORKERS', min_val=1, max_val=50), 8)
        self.assertEqual(config_manager.get_int('INVALID_INT', default=5, min_val=1, max_val=10), 5)

        # Test boolean conversion
        self.assertTrue(config_manager.get_bool('SMART_EVACUATION'))
        self.assertFalse(config_manager.get_bool('SSL_VERIFY'))

    def test_missing_config_files(self):
        """Test behavior with missing configuration files."""
        # Test with non-existent config path
        with patch('os.path.exists', return_value=False):
            config_manager = instanceha.ConfigManager('/nonexistent/path')
            self.assertEqual(config_manager.get_str('EVACUABLE_TAG', 'default'), 'default')

    def test_dynamic_property_access(self):
        """Test dynamic property accessors."""
        config_manager = instanceha.ConfigManager(self.config_path)
        config_manager.clouds_path = self.clouds_path
        config_manager.secure_path = self.secure_path
        config_manager.fencing_path = self.fencing_path
        config_manager.__init__(self.config_path)

        # Test dynamic property access
        self.assertEqual(config_manager.get_evacuable_tag(), 'test_tag')
        self.assertEqual(config_manager.get_delta(), 60)
        self.assertTrue(config_manager.is_smart_evacuation_enabled())


class TestMetrics(unittest.TestCase):
    """Test the Metrics class functionality."""

    def setUp(self):
        """Set up test fixtures."""
        self.metrics = instanceha.Metrics()

    def test_metrics_initialization(self):
        """Test Metrics initialization."""
        self.assertEqual(self.metrics.counters, {})
        self.assertEqual(self.metrics.durations, {})
        self.assertIsInstance(self.metrics.start_time, float)

    def test_counter_operations(self):
        """Test counter increment operations."""
        self.metrics.increment('test_counter')
        self.assertEqual(self.metrics.counters['test_counter'], 1)

        self.metrics.increment('test_counter', 5)
        self.assertEqual(self.metrics.counters['test_counter'], 6)

    def test_duration_recording(self):
        """Test duration recording."""
        self.metrics.record_duration('test_operation', 1.5)
        self.assertEqual(self.metrics.durations['test_operation'], 1.5)

        self.metrics.record_duration('test_operation', 2.5)
        self.assertEqual(self.metrics.durations['test_operation'], 4.0)

    def test_timing_context(self):
        """Test TimingContext manager."""
        with self.metrics.timer('test_timer'):
            time.sleep(0.1)

        # Check that metrics were recorded
        self.assertIn('test_timer_total', self.metrics.counters)
        self.assertIn('test_timer_successful', self.metrics.counters)
        self.assertIn('test_timer_duration', self.metrics.durations)
        self.assertEqual(self.metrics.counters['test_timer_total'], 1)
        self.assertEqual(self.metrics.counters['test_timer_successful'], 1)

    def test_timing_context_with_exception(self):
        """Test TimingContext with exception handling."""
        try:
            with self.metrics.timer('test_timer_error'):
                raise ValueError("Test error")
        except ValueError:
            pass

        # Check that failure was recorded
        self.assertIn('test_timer_error_total', self.metrics.counters)
        self.assertIn('test_timer_error_failed', self.metrics.counters)
        self.assertEqual(self.metrics.counters['test_timer_error_failed'], 1)

    def test_metrics_summary(self):
        """Test metrics summary generation."""
        self.metrics.increment('test_metric', 10)
        self.metrics.record_duration('test_duration', 2.0)

        summary = self.metrics.get_summary()

        self.assertIn('uptime_seconds', summary)
        self.assertIn('counters', summary)
        self.assertIn('total_durations', summary)
        self.assertEqual(summary['counters']['test_metric'], 10)
        self.assertEqual(summary['total_durations']['test_duration'], 2.0)


class TestInstanceHAService(unittest.TestCase):
    """Test the InstanceHAService class functionality."""

    def setUp(self):
        """Set up test fixtures."""
        self.mock_config = Mock()
        self.mock_config.get_evacuable_tag.return_value = 'evacuable'
        self.mock_config.is_tagged_images_enabled.return_value = True
        self.mock_config.is_tagged_flavors_enabled.return_value = True
        self.mock_config.is_tagged_aggregates_enabled.return_value = False
        self.mock_config.is_smart_evacuation_enabled.return_value = True
        self.mock_config.get_workers.return_value = 4
        self.mock_config.get_delay.return_value = 0

        self.mock_nova_client = Mock()
        self.service = instanceha.InstanceHAService(self.mock_config, self.mock_nova_client)

    def test_service_initialization(self):
        """Test InstanceHAService initialization."""
        self.assertEqual(self.service.config, self.mock_config)
        self.assertEqual(self.service.nova_client, self.mock_nova_client)
        self.assertEqual(self.service.current_hash, "")
        self.assertTrue(self.service.hash_update_successful)

    def test_get_nova_connection(self):
        """Test Nova connection retrieval."""
        connection = self.service.get_nova_connection()
        self.assertEqual(connection, self.mock_nova_client)

    def test_is_server_evacuable_with_no_tagging(self):
        """Test server evacuability when no tagging is enabled."""
        self.mock_config.is_tagged_images_enabled.return_value = False
        self.mock_config.is_tagged_flavors_enabled.return_value = False

        mock_server = Mock()
        mock_server.id = 'test-server-1'

        result = self.service.is_server_evacuable(mock_server)
        self.assertTrue(result)

    def test_is_server_evacuable_with_flavor_tagging(self):
        """Test server evacuability with flavor tagging."""
        self.mock_config.is_tagged_images_enabled.return_value = False
        self.mock_config.is_tagged_flavors_enabled.return_value = True

        mock_server = Mock()
        mock_server.id = 'test-server-1'
        mock_server.flavor = {'extra_specs': {'evacuable': 'true'}}

        result = self.service.is_server_evacuable(mock_server, [], [])
        self.assertTrue(result)

    def test_is_server_evacuable_with_image_tagging(self):
        """Test server evacuability with image tagging."""
        self.mock_config.is_tagged_images_enabled.return_value = True
        self.mock_config.is_tagged_flavors_enabled.return_value = False

        mock_server = Mock()
        mock_server.id = 'test-server-1'
        mock_server.image = {'id': 'image-123'}

        # Mock the image checking method
        with patch.object(self.service, 'is_server_image_evacuable', return_value=True):
            result = self.service.is_server_evacuable(mock_server, [], [])
            self.assertTrue(result)

    def test_get_evacuable_flavors_caching(self):
        """Test evacuable flavors caching mechanism."""
        mock_flavor = Mock()
        mock_flavor.id = 'flavor-123'
        mock_flavor.get_keys.return_value = {'evacuable': 'true'}

        self.mock_nova_client.flavors.list.return_value = [mock_flavor]

        # First call should populate cache
        flavors1 = self.service.get_evacuable_flavors(self.mock_nova_client)

        # Second call should use cache
        flavors2 = self.service.get_evacuable_flavors(self.mock_nova_client)

        self.assertEqual(flavors1, flavors2)
        self.assertEqual(flavors1, ['flavor-123'])
        # Should only call the API once due to caching
        self.mock_nova_client.flavors.list.assert_called_once()

    def test_cache_refresh(self):
        """Test cache refresh functionality."""
        # Set up initial cache
        self.service._evacuable_flavors_cache = ['cached-flavor']
        self.service._cache_timestamp = time.time() - 400  # Make cache stale

        mock_flavor = Mock()
        mock_flavor.id = 'new-flavor'
        mock_flavor.get_keys.return_value = {'evacuable': 'true'}
        self.mock_nova_client.flavors.list.return_value = [mock_flavor]

        # Force refresh should update cache
        refreshed = self.service.refresh_evacuable_cache(self.mock_nova_client, force=True)
        self.assertTrue(refreshed)

        # Cache should be updated
        flavors = self.service.get_evacuable_flavors(self.mock_nova_client)
        self.assertEqual(flavors, ['new-flavor'])

    def test_filter_hosts_with_servers(self):
        """Test filtering hosts that have servers."""
        mock_service1 = Mock()
        mock_service1.host = 'host1'
        mock_service2 = Mock()
        mock_service2.host = 'host2'

        services = [mock_service1, mock_service2]
        cache = {'host1': ['server1'], 'host2': []}

        filtered = self.service.filter_hosts_with_servers(services, cache)
        self.assertEqual(len(filtered), 1)
        self.assertEqual(filtered[0].host, 'host1')

    def test_aggregate_evacuability(self):
        """Test aggregate evacuability checking."""
        mock_aggregate = Mock()
        mock_aggregate.hosts = ['host1', 'host2']
        mock_aggregate.metadata = {'evacuable': 'true'}

        self.mock_nova_client.aggregates.list.return_value = [mock_aggregate]

        result = self.service.is_aggregate_evacuable(self.mock_nova_client, 'host1')
        self.assertTrue(result)

        result = self.service.is_aggregate_evacuable(self.mock_nova_client, 'host3')
        self.assertFalse(result)


class TestEvacuationFunctions(unittest.TestCase):
    """Test evacuation-related functions."""

    def setUp(self):
        """Set up test fixtures."""
        self.mock_connection = Mock()
        self.mock_service = Mock()
        self.mock_service.host = 'test-host'
        self.mock_service.id = 'service-123'

    def test_server_evacuate_success(self):
        """Test successful server evacuation."""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.reason = 'OK'

        self.mock_connection.servers.evacuate.return_value = (mock_response, {})

        result = instanceha._server_evacuate(self.mock_connection, 'server-123')

        self.assertTrue(result['accepted'])
        self.assertEqual(result['uuid'], 'server-123')
        self.assertEqual(result['reason'], 'OK')

    def test_server_evacuate_failure(self):
        """Test failed server evacuation."""
        mock_response = Mock()
        mock_response.status_code = 500
        mock_response.reason = 'Internal Server Error'

        self.mock_connection.servers.evacuate.return_value = (mock_response, {})

        result = instanceha._server_evacuate(self.mock_connection, 'server-123')

        self.assertFalse(result['accepted'])
        self.assertEqual(result['uuid'], 'server-123')
        self.assertEqual(result['reason'], 'Internal Server Error')

    def test_server_evacuate_exception(self):
        """Test server evacuation with exception."""
        # Mock the exception class
        class MockNotFound(Exception):
            pass

        # Patch the module to use our mock exception
        with patch('instanceha.NotFound', MockNotFound):
            self.mock_connection.servers.evacuate.side_effect = MockNotFound('Server not found')

            result = instanceha._server_evacuate(self.mock_connection, 'server-123')

            self.assertFalse(result['accepted'])
            self.assertIn('not found', result['reason'])

    def test_host_disable_success(self):
        """Test successful host disable operation."""
        result = instanceha._host_disable(self.mock_connection, self.mock_service)
        self.assertTrue(result)

        # Verify both operations were called
        self.mock_connection.services.force_down.assert_called_once_with('service-123', True)
        self.mock_connection.services.disable_log_reason.assert_called_once()

    def test_host_disable_force_down_failure(self):
        """Test host disable when force_down fails."""
        # Mock the exception class
        class MockNotFound(Exception):
            pass

        with patch('instanceha.NotFound', MockNotFound):
            self.mock_connection.services.force_down.side_effect = MockNotFound('Service not found')

            result = instanceha._host_disable(self.mock_connection, self.mock_service)
            self.assertFalse(result)

            # disable_log_reason should not be called if force_down fails
            self.mock_connection.services.disable_log_reason.assert_not_called()

    @patch('time.sleep')
    def test_server_evacuation_status(self, mock_sleep):
        """Test server evacuation status checking."""
        mock_migration = Mock()
        mock_migration.status = 'completed'
        self.mock_connection.migrations.list.return_value = [mock_migration]

        result = instanceha._server_evacuation_status(self.mock_connection, 'server-123')

        self.assertTrue(result['completed'])
        self.assertFalse(result['error'])

    def test_check_evacuable_tag_dict(self):
        """Test evacuable tag checking with dictionary data."""
        service = instanceha.InstanceHAService(Mock(), Mock())

        # Test exact match
        data = {'evacuable': 'true'}
        result = service._check_evacuable_tag(data, 'evacuable')
        self.assertTrue(result)

        # Test composite key match
        data = {'trait:CUSTOM_EVACUABLE': 'true'}
        result = service._check_evacuable_tag(data, 'EVACUABLE')
        self.assertTrue(result)

        # Test no match
        data = {'evacuable': 'false'}
        result = service._check_evacuable_tag(data, 'evacuable')
        self.assertFalse(result)

    def test_check_evacuable_tag_list(self):
        """Test evacuable tag checking with list data."""
        service = instanceha.InstanceHAService(Mock(), Mock())

        # Test exact match in list
        data = ['evacuable', 'other_tag']
        result = service._check_evacuable_tag(data, 'evacuable')
        self.assertTrue(result)

        # Test substring match in list
        data = ['trait:CUSTOM_EVACUABLE']
        result = service._check_evacuable_tag(data, 'EVACUABLE')
        self.assertTrue(result)


if __name__ == '__main__':
    # Configure logging for tests
    import logging
    logging.basicConfig(level=logging.DEBUG)

    # Create test suite
    test_suite = unittest.TestSuite()

    # Add all test classes
    test_classes = [
        TestConfigManager,
        TestMetrics,
        TestInstanceHAService,
        TestEvacuationFunctions,
    ]

    for test_class in test_classes:
        tests = unittest.TestLoader().loadTestsFromTestCase(test_class)
        test_suite.addTests(tests)

    # Run tests
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(test_suite)

    # Exit with appropriate code
    sys.exit(0 if result.wasSuccessful() else 1)
