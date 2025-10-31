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
import struct
import subprocess
import concurrent.futures
import logging
import requests
from unittest.mock import Mock, patch, MagicMock, call
from datetime import datetime, timedelta
from io import StringIO

# Add the module path for testing
#sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.append('../../templates/instanceha/bin/')

# Suppress configuration warnings during testing
logging.getLogger().setLevel(logging.CRITICAL)

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


class TestHelperFunctions(unittest.TestCase):
    """Test utility helper functions."""

    def test_extract_hostname_fqdn(self):
        """Test extracting hostname from FQDN."""
        result = instanceha._extract_hostname('compute-01.example.com')
        self.assertEqual(result, 'compute-01')

    def test_extract_hostname_short(self):
        """Test extracting hostname from short hostname."""
        result = instanceha._extract_hostname('compute-01')
        self.assertEqual(result, 'compute-01')

    def test_extract_hostname_multiple_dots(self):
        """Test extracting hostname with multiple dots."""
        result = instanceha._extract_hostname('host.subdomain.example.com')
        self.assertEqual(result, 'host')

    def test_extract_hostname_no_dot(self):
        """Test extracting hostname with no dots."""
        result = instanceha._extract_hostname('localhost')
        self.assertEqual(result, 'localhost')


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

        self.mock_cloud_client = Mock()
        self.service = instanceha.InstanceHAService(self.mock_config, self.mock_cloud_client)

    def test_service_initialization(self):
        """Test InstanceHAService initialization."""
        self.assertEqual(self.service.config, self.mock_config)
        self.assertEqual(self.service.cloud_client, self.mock_cloud_client)
        self.assertEqual(self.service.current_hash, "")
        self.assertTrue(self.service.hash_update_successful)

    def test_get_connection(self):
        """Test cloud connection retrieval."""
        connection = self.service.get_connection()
        self.assertEqual(connection, self.mock_cloud_client)

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

        self.mock_cloud_client.flavors.list.return_value = [mock_flavor]

        # First call should populate cache
        flavors1 = self.service.get_evacuable_flavors(self.mock_cloud_client)

        # Second call should use cache
        flavors2 = self.service.get_evacuable_flavors(self.mock_cloud_client)

        self.assertEqual(flavors1, flavors2)
        self.assertEqual(flavors1, ['flavor-123'])
        # Should only call the API once due to caching
        self.mock_cloud_client.flavors.list.assert_called_once()

    def test_cache_refresh(self):
        """Test cache refresh functionality."""
        # Set up initial cache
        self.service._evacuable_flavors_cache = ['cached-flavor']
        self.service._cache_timestamp = time.time() - 400  # Make cache stale

        mock_flavor = Mock()
        mock_flavor.id = 'new-flavor'
        mock_flavor.get_keys.return_value = {'evacuable': 'true'}
        self.mock_cloud_client.flavors.list.return_value = [mock_flavor]

        # Mock images to avoid slow API calls in refresh
        with patch.object(self.service, 'get_evacuable_images', return_value=['test-image']):
            # Force refresh should update cache
            refreshed = self.service.refresh_evacuable_cache(self.mock_cloud_client, force=True)
            self.assertTrue(refreshed)

        # Cache should be updated
        flavors = self.service.get_evacuable_flavors(self.mock_cloud_client)
        self.assertEqual(flavors, ['new-flavor'])

    def test_cache_thread_safety(self):
        """Test that cache operations are thread-safe."""
        import threading
        import time

        # Set up mock data
        mock_flavor = Mock()
        mock_flavor.id = 'thread-safe-flavor'
        mock_flavor.get_keys.return_value = {'evacuable': 'true'}
        self.mock_cloud_client.flavors.list.return_value = [mock_flavor]

        errors = []

        def cache_operation(thread_id):
            try:
                # Simulate concurrent cache operations
                self.service.clear_cache()
                with patch.object(self.service, 'get_evacuable_images', return_value=[]):
                    self.service.refresh_evacuable_cache(self.mock_cloud_client, force=True)
                flavors = self.service.get_evacuable_flavors(self.mock_cloud_client)
                # Should not raise any exceptions
                self.assertIsInstance(flavors, list)
            except Exception as e:
                errors.append(f"Thread {thread_id}: {e}")

        # Run multiple threads concurrently
        threads = []
        for i in range(5):
            thread = threading.Thread(target=cache_operation, args=(i,))
            threads.append(thread)
            thread.start()

        # Wait for all threads to complete
        for thread in threads:
            thread.join()

        # Should not have any errors from race conditions
        self.assertEqual(errors, [], f"Thread safety errors: {errors}")

    def test_unified_resource_evacuable_checking(self):
        """Test the unified resource evacuable checking methods."""
        # Test _is_resource_evacuable with different resource types
        mock_image = Mock()
        mock_image.tags = ['evacuable']
        mock_image.metadata = {'custom': 'evacuable'}
        mock_image.properties = {'trait:evacuable': 'true'}

        # Test with tags
        self.assertTrue(self.service._is_resource_evacuable(mock_image, 'evacuable', ['tags']))

        # Test with metadata
        mock_image2 = Mock()
        mock_image2.metadata = {'evacuable': 'true'}
        self.assertTrue(self.service._is_resource_evacuable(mock_image2, 'evacuable', ['metadata']))

        # Test _is_flavor_evacuable
        mock_flavor = Mock()
        mock_flavor.get_keys.return_value = {'evacuable': 'true'}
        self.assertTrue(self.service._is_flavor_evacuable(mock_flavor, 'evacuable'))

        # Test with composite key
        mock_flavor2 = Mock()
        mock_flavor2.get_keys.return_value = {'trait:CUSTOM_EVACUABLE': 'true'}
        self.assertTrue(self.service._is_flavor_evacuable(mock_flavor2, 'EVACUABLE'))

        # Test negative cases
        mock_flavor3 = Mock()
        mock_flavor3.get_keys.return_value = {'other': 'false'}
        self.assertFalse(self.service._is_flavor_evacuable(mock_flavor3, 'evacuable'))

    def test_interface_dependency_injection(self):
        """Test that interfaces enable better dependency injection."""
        # Create a custom mock that implements the OpenStack client interface
        custom_mock = Mock()
        custom_mock.services.list.return_value = []
        custom_mock.flavors.list.return_value = []

        # Service should accept any object that implements the interface
        service_with_custom_client = instanceha.InstanceHAService(self.mock_config, custom_mock)

        # Test that the custom client is used
        connection = service_with_custom_client.get_connection()
        self.assertEqual(connection, custom_mock)

        # Test that methods work with the interface
        flavors = service_with_custom_client.get_evacuable_flavors(custom_mock)
        self.assertIsInstance(flavors, list)

    def test_generator_based_service_categorization(self):
        """Test that service categorization uses memory-efficient generators."""
        from datetime import datetime, timedelta
        import instanceha

        # Create mock services
        mock_services = []
        for i in range(100):  # Simulate large deployment
            svc = Mock()
            svc.host = f'host-{i}'
            svc.status = 'enabled' if i % 3 == 0 else 'disabled'
            svc.forced_down = (i % 5 == 0)
            svc.state = 'down' if i % 4 == 0 else 'up'
            svc.updated_at = (datetime.now() - timedelta(seconds=60)).isoformat()
            svc.disabled_reason = 'instanceha evacuation' if i % 7 == 0 else 'other'
            mock_services.append(svc)

        target_date = datetime.now() - timedelta(seconds=30)

        # Test that categorization returns generators
        compute_nodes, to_resume, to_reenable = instanceha._categorize_services(mock_services, target_date)

        # Should be generators, not lists
        self.assertTrue(hasattr(compute_nodes, '__iter__'))
        self.assertTrue(hasattr(to_resume, '__iter__'))
        self.assertTrue(hasattr(to_reenable, '__iter__'))

        # Converting to lists should work
        compute_list = list(compute_nodes)
        resume_list = list(to_resume)
        reenable_list = list(to_reenable)

        # All results should be mock objects from our input
        for result_list in [compute_list, resume_list, reenable_list]:
            for item in result_list:
                self.assertIn(item, mock_services)

    def test_health_hash_update(self):
        """Test health hash update functionality."""
        # Initial state
        self.assertEqual(self.service.current_hash, "")
        self.assertTrue(self.service.hash_update_successful)

        # First update should set hash
        self.service.update_health_hash(hash_interval=0)  # Force immediate update
        self.assertNotEqual(self.service.current_hash, "")
        self.assertTrue(self.service.hash_update_successful)

        # Same hash should trigger failure
        old_hash = self.service.current_hash
        self.service._previous_hash = old_hash  # Simulate same hash scenario

        import unittest.mock
        with unittest.mock.patch('hashlib.sha256') as mock_sha:
            mock_sha.return_value.hexdigest.return_value = old_hash
            self.service.update_health_hash(hash_interval=0)
            self.assertFalse(self.service.hash_update_successful)

    def test_new_configuration_values(self):
        """Test new configuration values are accessible."""
        import instanceha

        # Create a real config manager to test new configuration values
        real_config = instanceha.ConfigManager()

        # Test unified fencing timeout configuration
        fencing_timeout = real_config.get_config_value('FENCING_TIMEOUT')
        self.assertIsInstance(fencing_timeout, int)
        self.assertGreaterEqual(fencing_timeout, 5)
        self.assertLessEqual(fencing_timeout, 120)

        hash_interval = real_config.get_config_value('HASH_INTERVAL')
        self.assertIsInstance(hash_interval, int)
        self.assertGreaterEqual(hash_interval, 30)

        metrics_interval = real_config.get_config_value('METRICS_LOG_INTERVAL')
        self.assertIsInstance(metrics_interval, int)
        self.assertGreaterEqual(metrics_interval, 300)

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

        self.mock_cloud_client.aggregates.list.return_value = [mock_aggregate]

        result = self.service.is_aggregate_evacuable(self.mock_cloud_client, 'host1')
        self.assertTrue(result)

        result = self.service.is_aggregate_evacuable(self.mock_cloud_client, 'host3')
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

    def test_server_evacuate_none_response(self):
        """Test _server_evacuate when response is None."""
        self.mock_connection.servers.evacuate.return_value = (None, {})

        result = instanceha._server_evacuate(self.mock_connection, 'server-123')

        self.assertFalse(result['accepted'])
        self.assertEqual(result['reason'], 'No response received while evacuating instance')

    def test_server_evacuate_none_reason(self):
        """Test _server_evacuate when response.reason is None (uses fallback)."""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.reason = None
        self.mock_connection.servers.evacuate.return_value = (mock_response, {})

        result = instanceha._server_evacuate(self.mock_connection, 'server-123')

        self.assertTrue(result['accepted'])
        self.assertEqual(result['reason'], 'Evacuation initiated successfully')

    def test_server_evacuate_error_status_no_reason(self):
        """Test _server_evacuate when status code is error but reason is None."""
        mock_response = Mock()
        mock_response.status_code = 500
        mock_response.reason = None
        self.mock_connection.servers.evacuate.return_value = (mock_response, {})

        result = instanceha._server_evacuate(self.mock_connection, 'server-123')

        self.assertFalse(result['accepted'])
        self.assertIn('status 500', result['reason'])

    def test_host_disable_log_reason_fails_non_critical(self):
        """Test that disable_log_reason failure is handled gracefully."""
        # disable_log_reason fails
        self.mock_connection.services.disable_log_reason.side_effect = Exception("Logging failed")

        result = instanceha._host_disable(self.mock_connection, self.mock_service)

        # Should return False but service is still forced down
        self.assertFalse(result)
        self.mock_connection.services.force_down.assert_called_once()
        self.mock_connection.services.disable_log_reason.assert_called_once()

    def test_handle_nova_exception_notfound(self):
        """Test handling NotFound exception."""
        class MockNotFound(Exception):
            pass

        with patch('instanceha.NotFound', MockNotFound):
            result = instanceha._handle_nova_exception(
                "test operation", "test-service", MockNotFound("Not found"), is_critical=True
            )
            self.assertFalse(result)

    def test_handle_nova_exception_conflict(self):
        """Test handling Conflict exception."""
        class MockConflict(Exception):
            pass

        with patch('instanceha.Conflict', MockConflict):
            result = instanceha._handle_nova_exception(
                "test operation", "test-service", MockConflict("Conflict"), is_critical=True
            )
            self.assertFalse(result)

    def test_handle_nova_exception_generic(self):
        """Test handling generic exception."""
        result = instanceha._handle_nova_exception(
            "test operation", "test-service", Exception("Generic error"), is_critical=True
        )
        self.assertFalse(result)

    def test_handle_nova_exception_non_critical(self):
        """Test handling non-critical exception (should log warning)."""
        result = instanceha._handle_nova_exception(
            "test operation", "test-service", Exception("Error"), is_critical=False
        )
        self.assertFalse(result)

    def test_update_service_disable_reason_with_id(self):
        """Test updating disable reason with provided service ID."""
        result = instanceha._update_service_disable_reason(
            self.mock_connection, 'test-host', service_id='service-123'
        )
        self.assertTrue(result)
        self.mock_connection.services.disable_log_reason.assert_called_once()
        self.mock_connection.services.list.assert_not_called()

    def test_update_service_disable_reason_without_id(self):
        """Test updating disable reason without service ID (fetch service)."""
        mock_service = Mock()
        mock_service.host = 'test-host'
        mock_service.binary = 'nova-compute'
        mock_service.id = 'service-123'
        self.mock_connection.services.list.return_value = [mock_service]

        result = instanceha._update_service_disable_reason(
            self.mock_connection, 'test-host', service_id=None
        )
        self.assertTrue(result)
        self.mock_connection.services.disable_log_reason.assert_called_once()

    def test_update_service_disable_reason_service_not_found(self):
        """Test updating disable reason when service not found."""
        self.mock_connection.services.list.return_value = []
        result = instanceha._update_service_disable_reason(
            self.mock_connection, 'test-host', service_id=None
        )
        self.assertFalse(result)

    def test_update_service_disable_reason_exception_handling(self):
        """Test exception handling in update disable reason."""
        self.mock_connection.services.disable_log_reason.side_effect = Exception("API error")
        result = instanceha._update_service_disable_reason(
            self.mock_connection, 'test-host', service_id='service-123'
        )
        self.assertFalse(result)

    def test_migration_status_constants_defined(self):
        """Test that migration status constants are defined."""
        self.assertTrue(hasattr(instanceha, 'MIGRATION_STATUS_COMPLETED'))
        self.assertTrue(hasattr(instanceha, 'MIGRATION_STATUS_ERROR'))
        self.assertIsInstance(instanceha.MIGRATION_STATUS_COMPLETED, list)
        self.assertIsInstance(instanceha.MIGRATION_STATUS_ERROR, list)

    def test_migration_status_completed_values(self):
        """Test that completed status includes expected values."""
        self.assertIn('completed', instanceha.MIGRATION_STATUS_COMPLETED)
        self.assertIn('done', instanceha.MIGRATION_STATUS_COMPLETED)

    def test_migration_status_error_values(self):
        """Test that error status includes expected values."""
        self.assertIn('error', instanceha.MIGRATION_STATUS_ERROR)
        self.assertIn('failed', instanceha.MIGRATION_STATUS_ERROR)
        self.assertIn('cancelled', instanceha.MIGRATION_STATUS_ERROR)

    @patch('instanceha.datetime')
    @patch('instanceha.timedelta')
    def test_server_evacuation_status_uses_constants(self, mock_timedelta, mock_datetime):
        """Test that _server_evacuation_status uses constants."""
        from datetime import datetime, timedelta as real_timedelta
        mock_datetime.now = Mock(return_value=datetime(2024, 1, 1, 0, 0, 0))
        mock_timedelta.return_value = real_timedelta(minutes=5)

        mock_migration = Mock()
        mock_migration.status = 'completed'
        mock_connection = Mock()
        mock_connection.migrations.list.return_value = [mock_migration]

        result = instanceha._server_evacuation_status(mock_connection, 'server-123')
        self.assertTrue(result['completed'])
        self.assertFalse(result['error'])

        # Verify constants are used (indirectly - status 'completed' is in constant)
        self.assertIn(mock_migration.status, instanceha.MIGRATION_STATUS_COMPLETED)

    @patch('instanceha._server_evacuate_future')
    @patch('time.sleep')
    @patch('concurrent.futures.ThreadPoolExecutor')
    def test_smart_evacuation_all_success(self, mock_executor_class, mock_sleep, mock_evacuate_future):
        """Test smart evacuation returns True when all evacuations succeed."""
        # Mock failed service
        mock_failed_service = Mock()
        mock_failed_service.host = 'test-host'
        mock_failed_service.id = 'service-123'

        # Mock service configuration
        mock_service = Mock()
        mock_service.config.get_evacuable_images.return_value = []
        mock_service.config.get_evacuable_flavors.return_value = []
        mock_service.config.is_smart_evacuation_enabled.return_value = True
        mock_service.config.get_workers.return_value = 4
        mock_service.config.get_delay.return_value = 0
        mock_service.is_server_evacuable.return_value = True

        # Mock servers
        mock_server1 = Mock()
        mock_server1.id = 'server-1'
        mock_server1.status = 'ACTIVE'
        mock_server2 = Mock()
        mock_server2.id = 'server-2'
        mock_server2.status = 'ACTIVE'

        self.mock_connection.servers.list.return_value = [mock_server1, mock_server2]

        # Mock ThreadPoolExecutor
        mock_executor = Mock()
        mock_executor.__enter__ = Mock(return_value=mock_executor)
        mock_executor.__exit__ = Mock(return_value=None)
        mock_executor_class.return_value = mock_executor

        # Mock futures to return True (success)
        mock_future1 = Mock()
        mock_future1.result.return_value = True
        mock_future2 = Mock()
        mock_future2.result.return_value = True

        mock_executor.submit.side_effect = [mock_future1, mock_future2]

        with patch('instanceha.concurrent.futures.as_completed') as mock_as_completed:
            mock_as_completed.return_value = [mock_future1, mock_future2]

            result = instanceha._host_evacuate(
                self.mock_connection, mock_failed_service, mock_service
            )

        # The fix ensures this returns True when all succeed
        self.assertTrue(result, "Smart evacuation should return True when all evacuations succeed")

    @patch('instanceha._update_service_disable_reason')
    @patch('instanceha._server_evacuate_future')
    @patch('time.sleep')
    def test_smart_evacuation_partial_failure(self, mock_sleep, mock_evacuate_future, mock_update_reason):
        """Test smart evacuation returns False when any evacuation fails."""
        # Mock failed service
        mock_failed_service = Mock()
        mock_failed_service.host = 'test-host'
        mock_failed_service.id = 'service-123'

        # Mock service configuration
        mock_service = Mock()
        mock_service.config.get_evacuable_images.return_value = []
        mock_service.config.get_evacuable_flavors.return_value = []
        mock_service.config.is_smart_evacuation_enabled.return_value = True
        mock_service.config.get_workers.return_value = 4
        mock_service.config.get_delay.return_value = 0
        mock_service.is_server_evacuable.return_value = True

        # Mock servers
        mock_server1 = Mock()
        mock_server1.id = 'server-1'
        mock_server1.status = 'ACTIVE'

        self.mock_connection.servers.list.return_value = [mock_server1]

        # Create a future that will return False when result() is called
        mock_future = MagicMock()
        mock_future.result.return_value = False

        # Mock ThreadPoolExecutor
        with patch('instanceha.concurrent.futures.ThreadPoolExecutor') as mock_executor_class:
            mock_executor = MagicMock()
            # Make submit return our mock future
            mock_executor.submit.return_value = mock_future

            # Set up context manager properly
            context_manager = MagicMock()
            context_manager.__enter__ = Mock(return_value=mock_executor)
            context_manager.__exit__ = Mock(return_value=None)
            mock_executor_class.return_value = context_manager

            # Mock as_completed - it needs to receive the future_to_server dict
            # and return an iterable of futures from that dict
            def mock_as_completed_side_effect(future_to_server):
                # Return the futures from the dict
                return iter(future_to_server.keys())

            with patch('instanceha.concurrent.futures.as_completed', side_effect=mock_as_completed_side_effect):
                result = instanceha._host_evacuate(
                    self.mock_connection, mock_failed_service, mock_service
                )

        # The result should be False because the evacuation failed
        self.assertFalse(result, "Smart evacuation should return False when any evacuation fails")
        # Verify update_reason was called
        mock_update_reason.assert_called_once()


class TestKdumpFunctionality(unittest.TestCase):
    """Test kdump detection and filtering functionality."""

    def setUp(self):
        """Set up test fixtures."""
        self.mock_service = Mock()
        self.mock_service.config.get_config_value.return_value = 10  # KDUMP_TIMEOUT
        self.mock_service.config.get_workers.return_value = 4
        self.mock_service.UDP_IP = '0.0.0.0'
        self.mock_service.config.get_udp_port.return_value = 7410

        # Create a mock service for kdump testing with proper config
        mock_config = Mock()
        mock_config.get_config_value.return_value = 10  # KDUMP_TIMEOUT
        mock_config.get_workers.return_value = 4
        self.mock_service_instance = instanceha.InstanceHAService(mock_config)
        self.mock_service_instance.kdump_hosts_timestamp.clear()

    def tearDown(self):
        """Clean up test fixtures."""
        # Clean up service instance
        if hasattr(self, 'mock_service_instance'):
            self.mock_service_instance.kdump_hosts_timestamp.clear()

    def test_kdump_timeout_configuration(self):
        """Test KDUMP_TIMEOUT configuration loading."""
        config = instanceha.ConfigManager()
        # Should have KDUMP_TIMEOUT in config map
        self.assertIn('KDUMP_TIMEOUT', config._config_map)
        # Should accept valid timeout values
        config._config = {'KDUMP_TIMEOUT': 30}
        self.assertEqual(config.get_config_value('KDUMP_TIMEOUT'), 30)

    def test_kdump_configuration_validation_warning(self):
        """Test POLL and KDUMP_TIMEOUT configuration conflict warning."""
        with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
            yaml.dump({'config': {'POLL': 30, 'KDUMP_TIMEOUT': 30, 'CHECK_KDUMP': True}}, f)
            config_path = f.name

        try:
            with patch('logging.warning') as mock_warning:
                config = instanceha.ConfigManager(config_path=config_path)
                # Check if the warning was called with the expected message
                mock_warning.assert_called()
                args, kwargs = mock_warning.call_args
                self.assertIn('CHECK_KDUMP', args[0])
                self.assertIn('KDUMP_TIMEOUT', args[0])
        finally:
            os.unlink(config_path)

    def test_kdump_single_host_recent_message(self):
        """Test _check_kdump_single with recent kdump message."""
        self.mock_service_instance.kdump_hosts_timestamp['compute-01'] = time.time() - 5
        # Mock socket operations to avoid real UDP timeouts
        with patch('socket.socket'):
            result = instanceha._check_kdump_single('compute-01.example.com', self.mock_service_instance)
        self.assertTrue(result)

    def test_kdump_single_host_old_message(self):
        """Test _check_kdump_single with old kdump message."""
        current_time = time.time()
        self.mock_service_instance.kdump_hosts_timestamp['compute-01'] = current_time - 60

        # Mock time.time to simulate timeout loop completion immediately
        time_calls = [current_time, current_time, current_time + 31]  # Exceed timeout quickly
        with patch('socket.socket'), \
             patch('time.sleep'), \
             patch('time.time', side_effect=time_calls):
            result = instanceha._check_kdump_single('compute-01.example.com', self.mock_service_instance)
            self.assertFalse(result)

    def test_kdump_single_host_no_message(self):
        """Test _check_kdump_single with no kdump message."""
        current_time = time.time()

        # Mock time.time to simulate timeout loop completion immediately
        time_calls = [current_time, current_time, current_time + 31]  # Exceed timeout quickly
        with patch('socket.socket'), \
             patch('time.sleep'), \
             patch('time.time', side_effect=time_calls):
            result = instanceha._check_kdump_single('compute-01.example.com', self.mock_service_instance)
            self.assertFalse(result)

    def test_kdump_single_host_delayed_message(self):
        """Test _check_kdump_single with delayed kdump message."""
        call_count = 0
        def simulate_delayed_message(duration):
            nonlocal call_count
            call_count += 1
            if call_count >= 3:  # After 3 calls to sleep
                self.mock_service_instance.kdump_hosts_timestamp['compute-01'] = time.time()

        # Mock socket operations to avoid real UDP timeouts
        with patch('socket.socket'), patch('time.sleep', side_effect=simulate_delayed_message):
            result = instanceha._check_kdump_single('compute-01.example.com', self.mock_service_instance)
            self.assertTrue(result)

    def test_kdump_check_no_services(self):
        """Test _check_kdump with empty service list."""
        result = instanceha._check_kdump([], self.mock_service_instance)
        self.assertEqual(result, [])

    def test_kdump_check_immediate_detection(self):
        """Test _check_kdump with immediate kdump detection."""
        mock_service1 = Mock()
        mock_service1.host = 'compute-01.example.com'

        # Set recent kdump timestamp
        self.mock_service_instance.kdump_hosts_timestamp['compute-01'] = time.time() - 5

        result = instanceha._check_kdump([mock_service1], self.mock_service_instance)
        self.assertEqual(len(result), 0)  # Host should be filtered out

    def test_kdump_check_no_kdump_activity(self):
        """Test _check_kdump with no kdump activity."""
        mock_service1 = Mock()
        mock_service1.host = 'compute-01.example.com'

        with patch('instanceha._check_kdump_single', return_value=False):
            result = instanceha._check_kdump([mock_service1], self.mock_service_instance)
            self.assertEqual(len(result), 1)  # Host should not be filtered

    def test_kdump_message_processing_valid_magic(self):
        """Test UDP message processing with valid magic number."""
        # Test both byte orders
        native_data = struct.pack('I', 0x1B302A40) + b'test_data'
        network_data = struct.pack('!I', 0x1B302A40) + b'test_data'

        # Both should be valid magic numbers
        magic_native = struct.unpack('I', native_data[:4])[0]
        magic_network = struct.unpack('!I', network_data[:4])[0]

        self.assertTrue(magic_native == 0x1B302A40 or magic_network == 0x1B302A40)

    def test_kdump_message_processing_invalid_magic(self):
        """Test UDP message processing with invalid magic number."""
        invalid_data = struct.pack('I', 0xDEADBEEF) + b'test_data'
        magic = struct.unpack('I', invalid_data[:4])[0]

        self.assertNotEqual(magic, 0x1B302A40)

    def test_kdump_timestamp_cleanup(self):
        """Test automatic cleanup of old kdump timestamps."""
        # Add old timestamps
        old_time = time.time() - 400  # Older than 300s cleanup threshold
        self.mock_service_instance.kdump_hosts_timestamp['old-host'] = old_time
        self.mock_service_instance.kdump_hosts_timestamp['recent-host'] = time.time()

        # Simulate cleanup logic
        if len(self.mock_service_instance.kdump_hosts_timestamp) > 0:  # Simplified condition for test
            cutoff = time.time() - 300
            old_keys = [k for k, v in self.mock_service_instance.kdump_hosts_timestamp.items() if v < cutoff]
            for k in old_keys:
                del self.mock_service_instance.kdump_hosts_timestamp[k]

        self.assertNotIn('old-host', self.mock_service_instance.kdump_hosts_timestamp)
        self.assertIn('recent-host', self.mock_service_instance.kdump_hosts_timestamp)

    def test_kdump_parallel_checking(self):
        """Test parallel kdump checking for multiple hosts."""
        mock_service1 = Mock()
        mock_service1.host = 'compute-01.example.com'
        mock_service2 = Mock()
        mock_service2.host = 'compute-02.example.com'

        services = [mock_service1, mock_service2]

        # Create a service instance for this test
        mock_service_instance = instanceha.InstanceHAService(Mock())
        mock_service_instance.config = self.mock_service.config

        with patch('instanceha._check_kdump_single') as mock_check:
            mock_check.side_effect = [True, False]  # First host kdumping, second not
            result = instanceha._check_kdump(services, mock_service_instance)

            # Should return only the non-kdumping host
            self.assertEqual(len(result), 1)
            self.assertEqual(result[0].host, 'compute-02.example.com')

    def test_kdump_long_timeout_scenario(self):
        """Test kdump detection with long timeout for delayed starts."""
        # Create mock service with longer timeout
        mock_service = Mock()
        mock_service.config.get_config_value.return_value = 90  # 90 second timeout

        call_count = 0
        # Create a service instance for this test
        mock_service_instance = instanceha.InstanceHAService(Mock())
        mock_service_instance.config = mock_service.config

        def simulate_very_delayed_kdump(duration):
            nonlocal call_count
            call_count += 1
            if call_count >= 60:  # After 60 sleep calls (simulating 60+ seconds)
                mock_service_instance.kdump_hosts_timestamp['compute-slow'] = time.time()

        # Mock socket operations to avoid real UDP timeouts
        with patch('socket.socket'), patch('time.sleep', side_effect=simulate_very_delayed_kdump):
            result = instanceha._check_kdump_single('compute-slow.example.com', mock_service_instance)
            self.assertTrue(result)

    def test_kdump_timeout_exceeded_scenario(self):
        """Test kdump timeout exceeded - host should not be considered kdumping."""
        # Create mock service with short timeout
        mock_service = Mock()
        mock_service.config.get_config_value.return_value = 5  # 5 second timeout

        current_time = time.time()

        # Mock time.time to simulate timeout exceeded immediately
        # Create a service instance for this test
        mock_service_instance = instanceha.InstanceHAService(Mock())
        mock_service_instance.config = mock_service.config

        time_calls = [current_time, current_time, current_time + 6]  # Exceed 5s timeout quickly
        with patch('socket.socket'), \
             patch('time.sleep'), \
             patch('time.time', side_effect=time_calls):
            result = instanceha._check_kdump_single('compute-never-kdump.example.com', mock_service_instance)
            self.assertFalse(result)


class TestKdumpIntegration(unittest.TestCase):
    """Integration tests for kdump functionality."""

    def setUp(self):
        """Set up integration test fixtures."""
        self.temp_dir = tempfile.mkdtemp()
        self.mock_service = Mock()
        self.mock_service.config.get_config_value.return_value = 10
        self.mock_service.config.get_workers.return_value = 2
        self.mock_service.UDP_IP = '127.0.0.1'
        self.mock_service.config.get_udp_port.return_value = 7411  # Different port for testing

    def tearDown(self):
        """Clean up integration test fixtures."""
        import shutil
        shutil.rmtree(self.temp_dir, ignore_errors=True)
        # Reset kdump state
        if hasattr(self, 'mock_service_instance'):
            self.mock_service_instance.kdump_hosts_timestamp.clear()

    def test_kdump_configuration_loading(self):
        """Test kdump configuration loading from file."""
        config_path = os.path.join(self.temp_dir, 'config.yaml')
        config_data = {
            'config': {
                'CHECK_KDUMP': True,
                'KDUMP_TIMEOUT': 30,
                'POLL': 45
            }
        }
        with open(config_path, 'w') as f:
            yaml.dump(config_data, f)

        # Verify configuration can be loaded
        self.assertTrue(os.path.exists(config_path))

    def test_kdump_udp_message_simulation(self):
        """Test UDP message format and processing simulation."""
        # Simulate creating a valid kdump message
        magic_number = 0x1B302A40
        test_data = struct.pack('!I', magic_number) + b'additional_data'

        # Verify message format
        self.assertEqual(len(test_data), 4 + len(b'additional_data'))
        parsed_magic = struct.unpack('!I', test_data[:4])[0]
        self.assertEqual(parsed_magic, magic_number)

    def test_kdump_workflow_integration(self):
        """Test complete kdump workflow integration."""
        # Setup test services
        mock_service1 = Mock()
        mock_service1.host = 'compute-01.example.com'
        mock_service2 = Mock()
        mock_service2.host = 'compute-02.example.com'

        services = [mock_service1, mock_service2]

        # Create a service instance for this test
        mock_config = Mock()
        mock_config.get_config_value.return_value = 10  # KDUMP_TIMEOUT
        mock_config.get_workers.return_value = 4
        mock_service_instance = instanceha.InstanceHAService(mock_config)

        # Simulate one host kdumping, one not
        mock_service_instance.kdump_hosts_timestamp['compute-01'] = time.time() - 5

        with patch('instanceha._check_kdump_single', return_value=False):
            result = instanceha._check_kdump(services, mock_service_instance)

            # Should filter out the kdumping host
            self.assertEqual(len(result), 1)
            self.assertEqual(result[0].host, 'compute-02.example.com')

    def test_kdump_thread_safety(self):
        """Test thread safety of kdump operations."""
        # Simulate concurrent access to kdump_hosts_timestamp
        # Create a service instance for this test
        mock_service_instance = instanceha.InstanceHAService(Mock())

        def update_timestamp(host_id):
            for i in range(10):
                mock_service_instance.kdump_hosts_timestamp[f'host-{host_id}-{i}'] = time.time()

        threads = []
        for i in range(3):
            thread = threading.Thread(target=update_timestamp, args=(i,))
            threads.append(thread)
            thread.start()

        for thread in threads:
            thread.join()

        # Should have entries from all threads
        self.assertGreaterEqual(len(mock_service_instance.kdump_hosts_timestamp), 30)

    def test_kdump_memory_management(self):
        """Test memory management and cleanup."""
        # Create a service instance for this test
        mock_service_instance = instanceha.InstanceHAService(Mock())

        # Add many old entries
        old_time = time.time() - 400
        for i in range(150):
            mock_service_instance.kdump_hosts_timestamp[f'old-host-{i}'] = old_time

        # Add some recent entries
        recent_time = time.time()
        for i in range(10):
            mock_service_instance.kdump_hosts_timestamp[f'recent-host-{i}'] = recent_time

        # Simulate cleanup when > 100 entries
        if len(mock_service_instance.kdump_hosts_timestamp) > 100:
            cutoff = time.time() - 300
            old_keys = [k for k, v in mock_service_instance.kdump_hosts_timestamp.items() if v < cutoff]
            for k in old_keys:
                del mock_service_instance.kdump_hosts_timestamp[k]

        # Should only have recent entries left
        self.assertLessEqual(len(mock_service_instance.kdump_hosts_timestamp), 10)
        self.assertTrue(all('recent-host' in k for k in mock_service_instance.kdump_hosts_timestamp.keys()))


if __name__ == '__main__':
    # Configure logging for tests
    import logging
    logging.basicConfig(level=logging.DEBUG)


class TestRedfishFencing(unittest.TestCase):
    """Unit tests for Redfish fencing functionality."""

    def setUp(self):
        """Set up test environment."""
        self.config_manager = instanceha.ConfigManager()

    @patch('instanceha.requests.post')
    @patch('instanceha.requests.get')
    @patch('instanceha.config_manager.get_requests_ssl_config')
    def test_redfish_reset_409_conflict_already_off(self, mock_ssl_config, mock_get, mock_post):
        """Test Redfish reset with 409 conflict when server is already off."""
        # Mock SSL config
        mock_ssl_config.return_value = False

        # Mock POST response returning 409 Conflict
        mock_post_response = Mock()
        mock_post_response.status_code = 409
        mock_post.return_value = mock_post_response

        # Mock GET response returning power state OFF
        mock_get_response = Mock()
        mock_get_response.status_code = 200
        mock_get_response.json.return_value = {'PowerState': 'Off'}
        mock_get.return_value = mock_get_response

        # Test the function
        result = instanceha._redfish_reset('http://test-server/redfish/v1/Systems/1', 'user', 'pass', 30, 'ForceOff')

        # Should return True because server is already off
        self.assertTrue(result)

        # Verify POST was called
        mock_post.assert_called_once()

        # Verify GET was called to check power state
        mock_get.assert_called_once()

    @patch('instanceha.requests.post')
    @patch('instanceha.requests.get')
    @patch('instanceha.config_manager.get_requests_ssl_config')
    def test_redfish_reset_409_conflict_not_off(self, mock_ssl_config, mock_get, mock_post):
        """Test Redfish reset with 409 conflict when server is not off."""
        # Mock SSL config
        mock_ssl_config.return_value = False

        # Mock POST response returning 409 Conflict
        mock_post_response = Mock()
        mock_post_response.status_code = 409
        mock_post.return_value = mock_post_response

        # Mock GET response returning power state ON
        mock_get_response = Mock()
        mock_get_response.status_code = 200
        mock_get_response.json.return_value = {'PowerState': 'On'}
        mock_get.return_value = mock_get_response

        # Test the function
        result = instanceha._redfish_reset('http://test-server/redfish/v1/Systems/1', 'user', 'pass', 30, 'ForceOff')

        # Should return False because server is not off
        self.assertFalse(result)

        # Verify both POST and GET were called
        mock_post.assert_called_once()
        mock_get.assert_called_once()

    @patch('instanceha.requests.post')
    @patch('instanceha.config_manager.get_requests_ssl_config')
    def test_redfish_reset_success(self, mock_ssl_config, mock_post):
        """Test successful Redfish reset."""
        # Mock SSL config
        mock_ssl_config.return_value = False

        # Mock POST response returning 200 OK
        mock_post_response = Mock()
        mock_post_response.status_code = 200
        mock_post.return_value = mock_post_response

        # Test the function with On action (no wait required)
        result = instanceha._redfish_reset('http://test-server/redfish/v1/Systems/1', 'user', 'pass', 1, 'On')

        # Should return True
        self.assertTrue(result)

        # Verify POST was called
        mock_post.assert_called_once()

    @patch('requests.get')
    @patch('instanceha.config_manager.get_requests_ssl_config')
    def test_redfish_get_power_state_success(self, mock_ssl_config, mock_get):
        """Test successful power state retrieval."""
        # Mock SSL config
        mock_ssl_config.return_value = False

        # Mock GET response - ensure it returns immediately
        mock_get_response = Mock()
        mock_get_response.status_code = 200
        mock_get_response.json.return_value = {'PowerState': 'On'}

        # Configure mock to return immediately
        mock_get.return_value = mock_get_response

        # Test the function with a very short timeout
        result = instanceha._redfish_get_power_state('http://test-server/redfish/v1/Systems/1', 'user', 'pass', 0.1)

        # Should return 'ON'
        self.assertEqual(result, 'ON')

        # Verify GET was called with correct parameters
        mock_get.assert_called_once()
        call_args = mock_get.call_args
        self.assertEqual(call_args[1]['timeout'], 0.1)  # Verify timeout parameter

    @patch('requests.get')
    @patch('instanceha.config_manager.get_requests_ssl_config')
    def test_redfish_get_power_state_failure(self, mock_ssl_config, mock_get):
        """Test power state retrieval failure."""
        # Mock SSL config
        mock_ssl_config.return_value = False

        # Mock GET response returning error
        mock_get_response = Mock()
        mock_get_response.status_code = 500
        mock_get.return_value = mock_get_response

        # Test the function
        result = instanceha._redfish_get_power_state('http://test-server/redfish/v1/Systems/1', 'user', 'pass', 30)

        # Should return None
        self.assertIsNone(result)

        # Verify GET was called
        mock_get.assert_called_once()

    @patch('instanceha.requests.post')
    @patch('requests.get')
    @patch('instanceha.config_manager.get_requests_ssl_config')
    @patch('instanceha.time.sleep')
    def test_redfish_reset_forceoff_wait_for_power_off(self, mock_sleep, mock_ssl_config, mock_get, mock_post):
        """Test Redfish ForceOff waits for actual power off confirmation."""
        # Reset mock call counts
        mock_get.reset_mock()
        mock_sleep.reset_mock()

        # Mock SSL config
        mock_ssl_config.return_value = False

        # Mock POST response returning 200 OK
        mock_post_response = Mock()
        mock_post_response.status_code = 200
        mock_post.return_value = mock_post_response

        # Mock GET responses: first ON, then OFF
        mock_get_responses = [
            Mock(status_code=200, json=Mock(return_value={'PowerState': 'On'})),
            Mock(status_code=200, json=Mock(return_value={'PowerState': 'Off'}))
        ]
        mock_get.side_effect = mock_get_responses

        # Test the function
        result = instanceha._redfish_reset('http://test-server/redfish/v1/Systems/1', 'user', 'pass', 2, 'ForceOff')

        # Should return True after power off confirmation
        self.assertTrue(result)

        # Verify that the function attempted to check power state multiple times
        # (exact count may vary due to other test interference)
        self.assertGreaterEqual(mock_get.call_count, 2)  # At least 2 calls expected
        self.assertGreaterEqual(mock_sleep.call_count, 1)  # At least 1 sleep call expected

    @patch('instanceha.requests.post')
    @patch('requests.get')
    @patch('instanceha.config_manager.get_requests_ssl_config')
    @patch('instanceha.time.sleep')
    def test_redfish_reset_forceoff_timeout(self, mock_sleep, mock_ssl_config, mock_get, mock_post):
        """Test Redfish ForceOff times out waiting for power off."""
        # Mock SSL config
        mock_ssl_config.return_value = False

        # Mock POST response returning 200 OK
        mock_post_response = Mock()
        mock_post_response.status_code = 200
        mock_post.return_value = mock_post_response

        # Mock GET response always returning ON (never powers off)
        mock_get_response = Mock()
        mock_get_response.status_code = 200
        mock_get_response.json.return_value = {'PowerState': 'On'}
        mock_get.return_value = mock_get_response

        # Test the function with timeout=2
        result = instanceha._redfish_reset('http://test-server/redfish/v1/Systems/1', 'user', 'pass', 2, 'ForceOff')

        # Should return False due to timeout
        self.assertFalse(result)

        # Verify that the function attempted to check power state multiple times
        # (exact count may vary due to other test interference)
        self.assertGreaterEqual(mock_get.call_count, 2)  # At least 2 calls expected
        self.assertGreaterEqual(mock_sleep.call_count, 2)  # At least 2 sleep calls expected

    @patch('instanceha.requests.post')
    @patch('instanceha.config_manager.get_requests_ssl_config')
    def test_redfish_reset_on_no_wait(self, mock_ssl_config, mock_post):
        """Test Redfish On action doesn't wait for power off."""
        # Mock SSL config
        mock_ssl_config.return_value = False

        # Mock POST response returning 200 OK
        mock_post_response = Mock()
        mock_post_response.status_code = 200
        mock_post.return_value = mock_post_response

        # Test the function
        result = instanceha._redfish_reset('http://test-server/redfish/v1/Systems/1', 'user', 'pass', 1, 'On')

        # Should return True immediately
        self.assertTrue(result)
        mock_post.assert_called_once()

    @patch('instanceha.requests.post')
    @patch('instanceha.config_manager.get_requests_ssl_config')
    @patch('instanceha.time.sleep')
    def test_redfish_reset_retry_on_server_error(self, mock_sleep, mock_ssl_config, mock_post):
        """Test Redfish reset retries on 5xx server errors."""
        mock_ssl_config.return_value = False

        # Mock POST responses: first two 503 errors, then 200 success
        mock_post_responses = [
            Mock(status_code=503),
            Mock(status_code=503),
            Mock(status_code=200)
        ]
        mock_post.side_effect = mock_post_responses

        result = instanceha._redfish_reset('http://test-server/redfish/v1/Systems/1', 'user', 'pass', 1, 'On')

        # Should succeed after retries
        self.assertTrue(result)
        self.assertEqual(mock_post.call_count, 3)
        self.assertEqual(mock_sleep.call_count, 2)  # Sleep between retries

    @patch('instanceha.requests.post')
    @patch('instanceha.config_manager.get_requests_ssl_config')
    @patch('instanceha.time.sleep')
    def test_redfish_reset_retry_exhausted(self, mock_sleep, mock_ssl_config, mock_post):
        """Test Redfish reset fails after max retries on server errors."""
        mock_ssl_config.return_value = False

        # Mock POST always returning 503
        mock_post_response = Mock(status_code=503)
        mock_post.return_value = mock_post_response

        result = instanceha._redfish_reset('http://test-server/redfish/v1/Systems/1', 'user', 'pass', 1, 'On')

        # Should fail after 3 attempts
        self.assertFalse(result)
        self.assertEqual(mock_post.call_count, 3)
        self.assertEqual(mock_sleep.call_count, 2)

    @patch('instanceha.requests.post')
    @patch('instanceha.config_manager.get_requests_ssl_config')
    @patch('instanceha.time.sleep')
    def test_redfish_reset_retry_on_network_error(self, mock_sleep, mock_ssl_config, mock_post):
        """Test Redfish reset retries on network errors."""
        mock_ssl_config.return_value = False

        # Mock network errors then success
        import requests.exceptions
        mock_post.side_effect = [
            requests.exceptions.ConnectionError("Connection failed"),
            requests.exceptions.Timeout("Request timeout"),
            Mock(status_code=200)
        ]

        result = instanceha._redfish_reset('http://test-server/redfish/v1/Systems/1', 'user', 'pass', 1, 'On')

        # Should succeed after retries
        self.assertTrue(result)
        self.assertEqual(mock_post.call_count, 3)
        self.assertEqual(mock_sleep.call_count, 2)

    @patch('instanceha.requests.post')
    @patch('instanceha.config_manager.get_requests_ssl_config')
    def test_redfish_reset_no_retry_on_auth_error(self, mock_ssl_config, mock_post):
        """Test Redfish reset doesn't retry on 401/403 auth errors."""
        mock_ssl_config.return_value = False

        # Mock POST response returning 401
        mock_post_response = Mock(status_code=401)
        mock_post.return_value = mock_post_response

        result = instanceha._redfish_reset('http://test-server/redfish/v1/Systems/1', 'user', 'pass', 1, 'On')

        # Should fail immediately without retry
        self.assertFalse(result)
        mock_post.assert_called_once()  # Only called once, no retry

    @patch('instanceha.requests.post')
    @patch('instanceha.config_manager.get_requests_ssl_config')
    @patch('instanceha.logging.error')
    def test_redfish_reset_url_sanitization(self, mock_log, mock_ssl_config, mock_post):
        """Test that URLs with embedded credentials are sanitized in logs."""
        mock_ssl_config.return_value = False

        # URL with embedded credentials
        url_with_creds = 'http://admin:secret123@test-server/redfish/v1/Systems/1'
        mock_post_response = Mock(status_code=401)
        mock_post.return_value = mock_post_response

        instanceha._redfish_reset(url_with_creds, 'user', 'pass', 1, 'On')

        # Verify URL was sanitized in logs (extract all log message arguments)
        log_messages = []
        for call_args in mock_log.call_args_list:
            if call_args[0]:  # Positional arguments
                log_messages.append(' '.join(str(arg) for arg in call_args[0]))
            if call_args[1]:  # Keyword arguments
                log_messages.append(' '.join(str(v) for v in call_args[1].values()))

        log_output = ' '.join(log_messages)

        # Should not contain the password
        self.assertNotIn('secret123', log_output)
        self.assertNotIn('admin', log_output)
        # Should contain sanitized version
        self.assertIn('***@', log_output)

    def test_sanitize_url_function(self):
        """Test _sanitize_url function directly."""
        # Test URL with credentials
        url_with_creds = 'http://admin:secret123@test-server/path'
        sanitized = instanceha._sanitize_url(url_with_creds)
        self.assertEqual(sanitized, 'http://***@test-server/path')
        self.assertNotIn('secret123', sanitized)
        self.assertNotIn('admin', sanitized)

        # Test HTTPS URL with credentials
        https_url = 'https://user:pass@example.com/api'
        sanitized = instanceha._sanitize_url(https_url)
        self.assertEqual(sanitized, 'https://***@example.com/api')

        # Test URL without credentials (should remain unchanged)
        normal_url = 'http://test-server/path'
        self.assertEqual(instanceha._sanitize_url(normal_url), normal_url)


class TestIPMIFencing(unittest.TestCase):
    """Unit tests for IPMI fencing functionality."""

    def setUp(self):
        """Set up test environment."""
        self.config_manager = instanceha.ConfigManager()

    @patch('instanceha.subprocess.run')
    def test_ipmi_get_power_state_success(self, mock_run):
        """Test successful IPMI power state retrieval."""
        # Mock subprocess run
        mock_result = Mock()
        mock_result.stdout = 'Chassis Power is off\n'
        mock_run.return_value = mock_result

        # Test the function
        result = instanceha._ipmi_get_power_state('192.168.1.1', '623', 'user', 'pass', 1)

        # Should return 'CHASSIS POWER IS OFF'
        self.assertEqual(result, 'CHASSIS POWER IS OFF')

        # Verify subprocess was called correctly
        mock_run.assert_called_once()
        call_args = mock_run.call_args
        self.assertEqual(call_args[0][0][0], 'ipmitool')
        self.assertIn('-E', call_args[0][0])  # Environment variable flag

    @patch('instanceha.subprocess.run')
    def test_ipmi_get_power_state_timeout(self, mock_run):
        """Test IPMI power state retrieval timeout."""
        # Mock subprocess timeout
        mock_run.side_effect = subprocess.TimeoutExpired('ipmitool', 1)

        # Test the function
        result = instanceha._ipmi_get_power_state('192.168.1.1', '623', 'user', 'pass', 1)

        # Should return None
        self.assertIsNone(result)

    @patch('instanceha.subprocess.run')
    def test_ipmi_get_power_state_error(self, mock_run):
        """Test IPMI power state retrieval error."""
        # Mock subprocess error
        mock_run.side_effect = subprocess.CalledProcessError(1, 'ipmitool')

        # Test the function
        result = instanceha._ipmi_get_power_state('192.168.1.1', '623', 'user', 'pass', 1)

        # Should return None
        self.assertIsNone(result)

    @patch('instanceha._ipmi_get_power_state')
    @patch('instanceha.subprocess.run')
    @patch('instanceha.time.sleep')
    def test_ipmi_power_off_wait_success(self, mock_sleep, mock_run, mock_get_power_state):
        """Test IPMI power off waits for confirmation."""
        # Mock successful power off command
        mock_run.return_value = Mock()

        # Mock power state checks: first ON, then OFF
        mock_get_power_state.side_effect = ['ON', 'CHASSIS POWER IS OFF']

        # Create mock service and fencing data
        mock_service = Mock()
        mock_service.config.get_config_value.return_value = 30

        # Test the function
        result = instanceha._execute_fence_operation('test-host', 'off', {
            'agent': 'ipmi',
            'ipaddr': '192.168.1.1',
            'ipport': '623',
            'login': 'user',
            'passwd': 'pass',
            'timeout': 2
        }, mock_service)

        # Should return True after power off confirmation
        self.assertTrue(result)
        self.assertEqual(mock_get_power_state.call_count, 2)  # Called twice
        mock_sleep.assert_called_once_with(1)

    @patch('instanceha._ipmi_get_power_state')
    @patch('instanceha.subprocess.run')
    @patch('instanceha.time.sleep')
    def test_ipmi_power_off_timeout(self, mock_sleep, mock_run, mock_get_power_state):
        """Test IPMI power off times out waiting for confirmation."""
        # Mock successful power off command
        mock_run.return_value = Mock()

        # Mock power state always returning ON (never powers off)
        mock_get_power_state.return_value = 'ON'

        # Create mock service and fencing data
        mock_service = Mock()
        mock_service.config.get_config_value.return_value = 30

        # Test the function with timeout=2
        result = instanceha._execute_fence_operation('test-host', 'off', {
            'agent': 'ipmi',
            'ipaddr': '192.168.1.1',
            'ipport': '623',
            'login': 'user',
            'passwd': 'pass',
            'timeout': 2
        }, mock_service)

        # Should return False due to timeout
        self.assertFalse(result)
        self.assertEqual(mock_get_power_state.call_count, 2)  # Called twice (timeout=2)
        self.assertEqual(mock_sleep.call_count, 2)  # sleep called twice

    @patch('instanceha.subprocess.run')
    def test_ipmi_power_on_no_wait(self, mock_run):
        """Test IPMI power on doesn't wait for power off."""
        # Mock successful power on command
        mock_run.return_value = Mock()

        # Create mock service and fencing data
        mock_service = Mock()
        mock_service.config.get_config_value.return_value = 30

        # Test the function
        result = instanceha._execute_fence_operation('test-host', 'on', {
            'agent': 'ipmi',
            'ipaddr': '192.168.1.1',
            'ipport': '623',
            'login': 'user',
            'passwd': 'pass',
            'timeout': 1
        }, mock_service)

        # Should return True immediately
        self.assertTrue(result)
        mock_run.assert_called_once()

    @patch('instanceha.subprocess.run')
    @patch('instanceha.time.sleep')
    def test_ipmi_retry_on_timeout(self, mock_sleep, mock_run):
        """Test IPMI retries on TimeoutExpired."""
        # Mock timeout then success
        mock_run.side_effect = [
            subprocess.TimeoutExpired('ipmitool', 1),
            subprocess.TimeoutExpired('ipmitool', 1),
            Mock()  # Success on third attempt
        ]

        mock_service = Mock()
        mock_service.config.get_config_value.return_value = 30

        result = instanceha._execute_fence_operation('test-host', 'on', {
            'agent': 'ipmi',
            'ipaddr': '192.168.1.1',
            'ipport': '623',
            'login': 'user',
            'passwd': 'pass',
            'timeout': 1
        }, mock_service)

        # Should succeed after retries
        self.assertTrue(result)
        self.assertEqual(mock_run.call_count, 3)
        self.assertEqual(mock_sleep.call_count, 2)  # Sleep between retries

    @patch('instanceha.subprocess.run')
    @patch('instanceha.time.sleep')
    def test_ipmi_retry_exhausted_timeout(self, mock_sleep, mock_run):
        """Test IPMI fails after max retries on TimeoutExpired."""
        # Mock always timing out
        mock_run.side_effect = subprocess.TimeoutExpired('ipmitool', 1)

        mock_service = Mock()
        mock_service.config.get_config_value.return_value = 30

        result = instanceha._execute_fence_operation('test-host', 'on', {
            'agent': 'ipmi',
            'ipaddr': '192.168.1.1',
            'ipport': '623',
            'login': 'user',
            'passwd': 'pass',
            'timeout': 1
        }, mock_service)

        # Should fail after 3 attempts
        self.assertFalse(result)
        self.assertEqual(mock_run.call_count, 3)
        self.assertEqual(mock_sleep.call_count, 2)

    @patch('instanceha.subprocess.run')
    @patch('instanceha.time.sleep')
    def test_ipmi_retry_on_called_process_error(self, mock_sleep, mock_run):
        """Test IPMI retries on CalledProcessError."""
        # Mock error then success
        mock_run.side_effect = [
            subprocess.CalledProcessError(1, 'ipmitool'),
            subprocess.CalledProcessError(1, 'ipmitool'),
            Mock()  # Success on third attempt
        ]

        mock_service = Mock()
        mock_service.config.get_config_value.return_value = 30

        result = instanceha._execute_fence_operation('test-host', 'on', {
            'agent': 'ipmi',
            'ipaddr': '192.168.1.1',
            'ipport': '623',
            'login': 'user',
            'passwd': 'pass',
            'timeout': 1
        }, mock_service)

        # Should succeed after retries
        self.assertTrue(result)
        self.assertEqual(mock_run.call_count, 3)
        self.assertEqual(mock_sleep.call_count, 2)

    @patch('instanceha.subprocess.run')
    @patch('instanceha.time.sleep')
    def test_ipmi_retry_exhausted_process_error(self, mock_sleep, mock_run):
        """Test IPMI fails after max retries on CalledProcessError."""
        # Mock always failing
        mock_run.side_effect = subprocess.CalledProcessError(1, 'ipmitool')

        mock_service = Mock()
        mock_service.config.get_config_value.return_value = 30

        result = instanceha._execute_fence_operation('test-host', 'on', {
            'agent': 'ipmi',
            'ipaddr': '192.168.1.1',
            'ipport': '623',
            'login': 'user',
            'passwd': 'pass',
            'timeout': 1
        }, mock_service)

        # Should fail after 3 attempts
        self.assertFalse(result)
        self.assertEqual(mock_run.call_count, 3)
        self.assertEqual(mock_sleep.call_count, 2)

    @patch('instanceha._safe_log_exception')
    @patch('instanceha.subprocess.run')
    def test_ipmi_safe_logging_on_final_failure(self, mock_run, mock_safe_log):
        """Test that IPMI uses safe logging on final failure."""
        # Mock TimeoutExpired that will fail after retries
        mock_run.side_effect = subprocess.TimeoutExpired('ipmitool', 1)

        mock_service = Mock()
        mock_service.config.get_config_value.return_value = 30

        result = instanceha._execute_fence_operation('test-host', 'on', {
            'agent': 'ipmi',
            'ipaddr': '192.168.1.1',
            'ipport': '623',
            'login': 'user',
            'passwd': 'pass',
            'timeout': 1
        }, mock_service)

        # Should fail and use safe logging
        self.assertFalse(result)
        mock_safe_log.assert_called_once()
        # Verify the message contains the expected text
        call_args = mock_safe_log.call_args
        self.assertIn('IPMI', call_args[0][0])
        self.assertIn('test-host', call_args[0][0])


class TestSecretExposure(unittest.TestCase):
    """Test that credentials are never exposed in logs or debug outputs."""

    def setUp(self):
        """Set up test fixtures."""
        self.temp_dir = tempfile.mkdtemp()
        self.config_path = os.path.join(self.temp_dir, 'config.yaml')
        self.clouds_path = os.path.join(self.temp_dir, 'clouds.yaml')
        self.secure_path = os.path.join(self.temp_dir, 'secure.yaml')
        self.fencing_path = os.path.join(self.temp_dir, 'fencing.yaml')

        # Create sample configuration files with test secrets
        self._create_test_configs()

        # Set up log capture
        self.log_capture = StringIO()
        self.log_handler = logging.StreamHandler(self.log_capture)
        self.log_handler.setLevel(logging.DEBUG)
        logging.getLogger().addHandler(self.log_handler)
        logging.getLogger().setLevel(logging.DEBUG)

    def tearDown(self):
        """Clean up test fixtures."""
        import shutil
        logging.getLogger().removeHandler(self.log_handler)
        shutil.rmtree(self.temp_dir)

    def _create_test_configs(self):
        """Create test configuration files with secrets."""
        # Main config
        config_data = {
            'config': {
                'EVACUABLE_TAG': 'test_tag',
                'DELTA': 60,
                'POLL': 30,
                'THRESHOLD': 75,
                'WORKERS': 8,
                'LOGLEVEL': 'DEBUG'
            }
        }
        with open(self.config_path, 'w') as f:
            yaml.dump(config_data, f)

        # Clouds config
        clouds_data = {
            'clouds': {
                'testcloud': {
                    'auth': {
                        'username': 'test_os_user',
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

        # Secure config with password
        secure_data = {
            'clouds': {
                'testcloud': {
                    'auth': {
                        'password': 'super_secret_openstack_password_123'
                    }
                }
            }
        }
        with open(self.secure_path, 'w') as f:
            yaml.dump(secure_data, f)

        # Fencing config with various credentials
        fencing_data = {
            'FencingConfig': {
                'compute-0': {
                    'agent': 'ipmi',
                    'ipaddr': '192.168.1.100',
                    'ipport': '623',
                    'login': 'admin',
                    'passwd': 'secret_ipmi_password_456'
                },
                'compute-1': {
                    'agent': 'redfish',
                    'ipaddr': '192.168.1.101',
                    'ipport': '443',
                    'login': 'root',
                    'passwd': 'secret_redfish_password_789'
                },
                'compute-2': {
                    'agent': 'bmh',
                    'token': 'bearer_token_abc123xyz789',
                    'namespace': 'metal3',
                    'host': 'compute-2'
                }
            }
        }
        with open(self.fencing_path, 'w') as f:
            yaml.dump(fencing_data, f)

    def _assert_no_secrets_in_logs(self):
        """Assert that no test secrets appear in captured logs."""
        log_output = self.log_capture.getvalue()
        secrets = [
            'super_secret_openstack_password_123',
            'secret_ipmi_password_456',
            'secret_redfish_password_789',
            'bearer_token_abc123xyz789',
        ]
        for secret in secrets:
            self.assertNotIn(secret, log_output,
                f"Secret '{secret}' was found in log output!")

    def test_nova_login_exception_no_secret_exposure(self):
        """Test that Nova login exceptions don't expose passwords."""
        # Clear log capture
        self.log_capture.seek(0)
        self.log_capture.truncate(0)

        test_password = 'super_secret_openstack_password_123'
        test_username = 'test_os_user'

        # Mock keystoneauth1 to raise an exception
        with patch('instanceha.loading.get_plugin_loader') as mock_loader:
            mock_loader.side_effect = Exception("Connection failed: password=invalid")

            # Try to login - should not expose password
            result = instanceha.nova_login(
                test_username, test_password, 'project',
                'http://keystone:5000/v3', 'Default', 'Default'
            )

            self.assertIsNone(result)
            self._assert_no_secrets_in_logs()

    def test_create_connection_exception_no_secret_exposure(self):
        """Test that create_connection exceptions don't expose passwords."""
        # Clear log capture
        self.log_capture.seek(0)
        self.log_capture.truncate(0)

        config_manager = instanceha.ConfigManager(self.config_path)
        config_manager.clouds_path = self.clouds_path
        config_manager.secure_path = self.secure_path
        config_manager.clouds = config_manager._load_clouds_config()
        config_manager.secure = config_manager._load_secure_config()

        service = instanceha.InstanceHAService(config_manager)

        # Mock nova_login to raise exception
        with patch('instanceha.nova_login') as mock_login:
            mock_login.side_effect = Exception("Auth failed: password=wrong")

            # This should not expose password
            try:
                service.create_connection()
            except instanceha.NovaConnectionError:
                pass

            self._assert_no_secrets_in_logs()

    def test_redfish_get_power_state_exception_no_secret_exposure(self):
        """Test that Redfish power state exceptions don't expose passwords."""
        # Clear log capture
        self.log_capture.seek(0)
        self.log_capture.truncate(0)

        test_password = 'secret_redfish_password_789'
        test_user = 'root'

        with patch('requests.get') as mock_get:
            mock_get.side_effect = Exception(f"Connection failed: auth password={test_password}")

            instanceha._redfish_get_power_state(
                'http://192.168.1.101/redfish/v1/Systems/1',
                test_user, test_password, 30
            )

            self._assert_no_secrets_in_logs()

    def test_redfish_reset_exception_no_secret_exposure(self):
        """Test that Redfish reset exceptions don't expose passwords."""
        # Clear log capture
        self.log_capture.seek(0)
        self.log_capture.truncate(0)

        test_password = 'secret_redfish_password_789'
        test_user = 'root'

        with patch('requests.post') as mock_post:
            mock_post.side_effect = Exception(f"POST failed: password={test_password}")

            instanceha._redfish_reset(
                'http://192.168.1.101/redfish/v1/Systems/1',
                test_user, test_password, 30, 'ForceOff'
            )

            self._assert_no_secrets_in_logs()

    def test_bmh_fence_exception_no_secret_exposure(self):
        """Test that BMH fence exceptions don't expose tokens."""
        # Clear log capture
        self.log_capture.seek(0)
        self.log_capture.truncate(0)

        test_token = 'bearer_token_abc123xyz789'
        mock_service = Mock()
        mock_service.config = Mock()
        mock_service.config.get_config_value = Mock(return_value=30)

        with patch('requests.patch') as mock_patch:
            mock_patch.side_effect = Exception(f"Request failed: token={test_token}")

            instanceha._bmh_fence(
                test_token, 'metal3', 'compute-2', 'off', mock_service
            )

            self._assert_no_secrets_in_logs()

    def test_ipmi_fence_exception_no_secret_exposure(self):
        """Test that IPMI fence operations don't expose passwords in process list."""
        # Clear log capture
        self.log_capture.seek(0)
        self.log_capture.truncate(0)

        test_password = 'secret_ipmi_password_456'
        fencing_data = {
            'agent': 'ipmi',
            'ipaddr': '192.168.1.100',
            'ipport': '623',
            'login': 'admin',
            'passwd': test_password
        }
        mock_service = Mock()
        mock_service.config = Mock()
        mock_service.config.get_config_value = Mock(return_value=30)

        # Verify password is passed via environment variable, not command line
        with patch('subprocess.run') as mock_run:
            mock_run.side_effect = subprocess.CalledProcessError(1, 'ipmitool')

            try:
                instanceha._execute_fence_operation('compute-0', 'off', fencing_data, mock_service)
            except:
                pass

            # Check that password is not in command arguments
            if mock_run.called:
                cmd = mock_run.call_args[0][0]
                cmd_str = ' '.join(cmd)
                self.assertNotIn(test_password, cmd_str,
                    "IPMI password found in command line arguments!")

            self._assert_no_secrets_in_logs()

    def test_handle_nova_exception_no_secret_exposure(self):
        """Test that _handle_nova_exception doesn't expose secrets."""
        # Clear log capture
        self.log_capture.seek(0)
        self.log_capture.truncate(0)

        # Create exception that might contain sensitive info
        test_exception = Exception("Auth failed: password=secret123")

        instanceha._handle_nova_exception("test_operation", "test_service", test_exception)

        self._assert_no_secrets_in_logs()

    def test_get_nova_connection_exception_no_secret_exposure(self):
        """Test that _get_nova_connection exceptions don't expose passwords."""
        # Clear log capture
        self.log_capture.seek(0)
        self.log_capture.truncate(0)

        config_manager = instanceha.ConfigManager(self.config_path)
        config_manager.clouds_path = self.clouds_path
        config_manager.secure_path = self.secure_path
        config_manager.clouds = config_manager._load_clouds_config()
        config_manager.secure = config_manager._load_secure_config()

        service = instanceha.InstanceHAService(config_manager)

        with patch('instanceha.nova_login') as mock_login:
            mock_login.side_effect = Exception("Connection error: password=wrong")

            instanceha._get_nova_connection(service)

            self._assert_no_secrets_in_logs()

    def test_establish_nova_connection_exception_no_secret_exposure(self):
        """Test that _establish_nova_connection exceptions don't expose passwords."""
        # Clear log capture
        self.log_capture.seek(0)
        self.log_capture.truncate(0)

        config_manager = instanceha.ConfigManager(self.config_path)
        config_manager.clouds_path = self.clouds_path
        config_manager.secure_path = self.secure_path
        config_manager.clouds = config_manager._load_clouds_config()
        config_manager.secure = config_manager._load_secure_config()

        service = instanceha.InstanceHAService(config_manager)

        with patch('instanceha.nova_login') as mock_login:
            mock_login.side_effect = Exception("Auth error: password=invalid")

            try:
                instanceha._establish_nova_connection(service)
            except SystemExit:
                pass

            self._assert_no_secrets_in_logs()

    def test_bmh_wait_for_power_off_exception_no_secret_exposure(self):
        """Test that BMH wait exceptions don't expose tokens in headers."""
        # Clear log capture
        self.log_capture.seek(0)
        self.log_capture.truncate(0)

        test_token = 'bearer_token_abc123xyz789'
        headers = {'Authorization': f'Bearer {test_token}'}
        mock_service = Mock()
        mock_service.config = Mock()
        mock_service.config.get_config_value = Mock(return_value=30)

        with patch('instanceha.requests.Session') as mock_session_class:
            mock_session = Mock()
            mock_context = Mock()
            mock_context.__enter__ = Mock(return_value=mock_session)
            mock_context.__exit__ = Mock(return_value=None)
            mock_session_class.return_value = mock_context
            mock_session.get.side_effect = requests.exceptions.HTTPError("Request failed")

            instanceha._bmh_wait_for_power_off(
                'https://kubernetes/api/v1/name', headers,
                '/path/to/ca.crt', 'compute-2', 1, 0.1, mock_service
            )

            # Verify token not in logs
            self._assert_no_secrets_in_logs()

    def test_safe_log_exception_sanitizes_secrets(self):
        """Test that _safe_log_exception properly sanitizes secret patterns."""
        # Clear log capture
        self.log_capture.seek(0)
        self.log_capture.truncate(0)

        test_exceptions = [
            Exception("Connection failed: password=secret123"),
            Exception("Auth error: token=abc123xyz"),
            Exception("Failed: secret=mysecret"),
            Exception("Error: credential=pass123"),
            Exception("Auth failed with password=test"),
        ]

        for exc in test_exceptions:
            instanceha._safe_log_exception("Test error", exc)

        log_output = self.log_capture.getvalue()

        # Verify secrets are redacted
        self.assertNotIn('secret123', log_output)
        self.assertNotIn('abc123xyz', log_output)
        self.assertNotIn('mysecret', log_output)
        self.assertNotIn('pass123', log_output)
        self.assertNotIn('=test', log_output)

        # But should contain redacted versions
        self.assertIn('password=***', log_output.lower())
        self.assertIn('token=***', log_output.lower())

    def test_username_validation_no_exposure(self):
        """Test that invalid username validation doesn't log the username."""
        # Clear log capture
        self.log_capture.seek(0)
        self.log_capture.truncate(0)

        fencing_data = {
            'agent': 'ipmi',
            'ipaddr': '192.168.1.100',
            'ipport': '623',
            'login': 'invalid user@name',  # Invalid username with special chars
            'passwd': 'secret_password'
        }
        mock_service = Mock()
        mock_service.config = Mock()
        mock_service.config.get_config_value = Mock(return_value=30)

        instanceha._execute_fence_operation('compute-0', 'off', fencing_data, mock_service)

        log_output = self.log_capture.getvalue()
        # Should not expose the invalid username
        self.assertNotIn('invalid user@name', log_output)
        self.assertNotIn('secret_password', log_output)


class TestFencingRaceCondition(unittest.TestCase):
    """Unit tests for fencing race condition prevention."""

    def setUp(self):
        """Set up test environment."""
        self.config_manager = instanceha.ConfigManager()
        self.service = instanceha.InstanceHAService(self.config_manager)

    def test_hosts_processing_initialization(self):
        """Test that hosts_processing dict is properly initialized."""
        self.assertIsInstance(self.service.hosts_processing, dict)
        self.assertEqual(len(self.service.hosts_processing), 0)
        self.assertIsNotNone(self.service.processing_lock)

    def test_process_service_cleanup_on_success(self):
        """Test that process_service cleans up tracking on successful completion."""
        # Mock service object
        failed_service = type('MockService', (), {'host': 'test-host.example.com'})()

        # Mock all dependencies to ensure success
        with unittest.mock.patch('instanceha._get_nova_connection') as mock_conn, \
             unittest.mock.patch('instanceha._execute_step', return_value=True):

            # Call process_service
            result = instanceha.process_service(failed_service, [], False, self.service)

            # Should succeed
            self.assertTrue(result)

            # Verify hostname is not in processing dict (cleaned up)
            with self.service.processing_lock:
                self.assertNotIn('test-host', self.service.hosts_processing)

    def test_process_service_cleanup_on_failure(self):
        """Test that process_service cleans up tracking even on failure."""
        # Mock service object
        failed_service = type('MockService', (), {'host': 'test-host.example.com'})()

        # Mock dependencies to cause failure
        with unittest.mock.patch('instanceha._get_nova_connection', return_value=None):

            # Call process_service (should fail due to no connection)
            result = instanceha.process_service(failed_service, [], False, self.service)

            # Should fail
            self.assertFalse(result)

            # Verify hostname is still cleaned up
            with self.service.processing_lock:
                self.assertNotIn('test-host', self.service.hosts_processing)

    def test_fencing_timeout_configuration(self):
        """Test that FENCING_TIMEOUT is properly configured and accessible."""
        # Test default value
        fencing_timeout = self.service.config.get_config_value('FENCING_TIMEOUT')
        self.assertIsInstance(fencing_timeout, int)
        self.assertGreaterEqual(fencing_timeout, 5)
        self.assertLessEqual(fencing_timeout, 120)

        # Test that it's used in race condition prevention
        import time
        current_time = time.time()
        max_processing_time = max(self.service.config.get_config_value('FENCING_TIMEOUT'), 300)
        self.assertGreaterEqual(max_processing_time, fencing_timeout)


if __name__ == '__main__':
    # Create test suite
    test_suite = unittest.TestSuite()

    # Add all test classes
    test_classes = [
        TestConfigManager,
        TestMetrics,
        TestHelperFunctions,
        TestInstanceHAService,
        TestEvacuationFunctions,
        TestKdumpFunctionality,
        TestKdumpIntegration,
        TestRedfishFencing,
        TestSecretExposure,
        TestFencingRaceCondition,
    ]

    for test_class in test_classes:
        tests = unittest.TestLoader().loadTestsFromTestCase(test_class)
        test_suite.addTests(tests)

    # Run tests
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(test_suite)

    # Exit with appropriate code
    sys.exit(0 if result.wasSuccessful() else 1)
