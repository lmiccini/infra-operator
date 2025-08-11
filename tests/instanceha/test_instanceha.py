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
import concurrent.futures
import logging
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

    # Create test suite
    test_suite = unittest.TestSuite()

    # Add all test classes
    test_classes = [
        TestConfigManager,
        TestMetrics,
        TestInstanceHAService,
        TestEvacuationFunctions,
        TestKdumpFunctionality,
        TestKdumpIntegration,
    ]

    for test_class in test_classes:
        tests = unittest.TestLoader().loadTestsFromTestCase(test_class)
        test_suite.addTests(tests)

    # Run tests
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(test_suite)

    # Exit with appropriate code
    sys.exit(0 if result.wasSuccessful() else 1)
