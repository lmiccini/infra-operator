#!/usr/bin/env python3

"""
Functional tests for InstanceHA service.

This script provides integration-level testing of the InstanceHA service
with mock OpenStack services to validate end-to-end functionality.
"""

import os
import sys
import time
import tempfile
import yaml
import threading
import socket
import struct
import unittest
from unittest.mock import Mock, patch, MagicMock
from datetime import datetime, timedelta

# Add the module path for testing
#sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.append('../../templates/instanceha/bin/')

import instanceha


class MockNovaService:
    """Mock Nova service for functional testing."""

    def __init__(self):
        self.services = MockServiceManager()
        self.servers = MockServerManager()
        self.flavors = MockFlavorManager()
        self.aggregates = MockAggregateManager()
        self.migrations = MockMigrationManager()
        self.images = MockImageManager()
        self.versions = Mock()
        self.versions.get_current.return_value = Mock()

    def __str__(self):
        return "MockNovaService"


class MockServiceManager:
    """Mock Nova service manager."""

    def __init__(self):
        self.services_data = []

    def list(self, binary=None):
        """List services with optional filtering."""
        services = []
        for svc_data in self.services_data:
            if binary is None or svc_data.get('binary') == binary:
                service = Mock()
                for key, value in svc_data.items():
                    setattr(service, key, value)
                services.append(service)
        return services

    def force_down(self, service_id, state):
        """Force service down/up."""
        for svc in self.services_data:
            if svc['id'] == service_id:
                svc['forced_down'] = state
                return True
        return False

    def disable_log_reason(self, service_id, reason):
        """Disable service with reason."""
        for svc in self.services_data:
            if svc['id'] == service_id:
                svc['status'] = 'disabled'
                svc['disabled_reason'] = reason
                return True
        return False

    def enable(self, service_id):
        """Enable service."""
        for svc in self.services_data:
            if svc['id'] == service_id:
                svc['status'] = 'enabled'
                svc['disabled_reason'] = None
                return True
        return False

    def add_service(self, **kwargs):
        """Add a service for testing."""
        service_data = {
            'id': f"service-{len(self.services_data)}",
            'host': kwargs.get('host', f"compute-{len(self.services_data)}"),
            'binary': kwargs.get('binary', 'nova-compute'),
            'status': kwargs.get('status', 'enabled'),
            'state': kwargs.get('state', 'up'),
            'updated_at': kwargs.get('updated_at', datetime.now().isoformat()),
            'zone': kwargs.get('zone', 'nova'),
            'forced_down': kwargs.get('forced_down', False),
            'disabled_reason': kwargs.get('disabled_reason', None)
        }
        self.services_data.append(service_data)
        return service_data['id']


class MockServerManager:
    """Mock Nova server manager."""

    def __init__(self):
        self.servers_data = {}

    def list(self, search_opts=None):
        """List servers with optional filtering."""
        servers = []
        for server_data in self.servers_data.values():
            # Apply filters
            if search_opts:
                if 'host' in search_opts and server_data.get('host') != search_opts['host']:
                    continue
                if 'all_tenants' in search_opts and not search_opts['all_tenants']:
                    continue

            server = Mock()
            for key, value in server_data.items():
                setattr(server, key, value)
            servers.append(server)
        return servers

    def evacuate(self, server):
        """Evacuate a server."""
        if server in self.servers_data:
            response = Mock()
            response.status_code = 200
            response.reason = 'OK'
            return response, {}
        else:
            response = Mock()
            response.status_code = 404
            response.reason = 'Not Found'
            return response, {}

    def add_server(self, **kwargs):
        """Add a server for testing."""
        server_id = kwargs.get('id', f"server-{len(self.servers_data)}")
        server_data = {
            'id': server_id,
            'name': kwargs.get('name', f"instance-{len(self.servers_data)}"),
            'status': kwargs.get('status', 'ACTIVE'),
            'host': kwargs.get('host', 'compute-0'),
            'flavor': kwargs.get('flavor', {'id': 'flavor-1', 'extra_specs': {}}),
            'image': kwargs.get('image', {'id': 'image-1'}),
        }
        self.servers_data[server_id] = server_data
        return server_id


class MockFlavorManager:
    """Mock Nova flavor manager."""

    def __init__(self):
        self.flavors_data = {}

    def list(self, is_public=None):
        """List flavors."""
        flavors = []
        for flavor_data in self.flavors_data.values():
            flavor = Mock()
            for key, value in flavor_data.items():
                setattr(flavor, key, value)
            flavor.get_keys = Mock(return_value=flavor_data.get('extra_specs', {}))
            flavors.append(flavor)
        return flavors

    def add_flavor(self, **kwargs):
        """Add a flavor for testing."""
        flavor_id = kwargs.get('id', f"flavor-{len(self.flavors_data)}")
        flavor_data = {
            'id': flavor_id,
            'name': kwargs.get('name', f"flavor-{len(self.flavors_data)}"),
            'extra_specs': kwargs.get('extra_specs', {})
        }
        self.flavors_data[flavor_id] = flavor_data
        return flavor_id


class MockAggregateManager:
    """Mock Nova aggregate manager."""

    def __init__(self):
        self.aggregates_data = {}

    def list(self):
        """List aggregates."""
        aggregates = []
        for agg_data in self.aggregates_data.values():
            aggregate = Mock()
            for key, value in agg_data.items():
                setattr(aggregate, key, value)
            aggregates.append(aggregate)
        return aggregates

    def add_aggregate(self, **kwargs):
        """Add an aggregate for testing."""
        agg_id = kwargs.get('id', f"agg-{len(self.aggregates_data)}")
        agg_data = {
            'id': agg_id,
            'name': kwargs.get('name', f"aggregate-{len(self.aggregates_data)}"),
            'hosts': kwargs.get('hosts', []),
            'metadata': kwargs.get('metadata', {})
        }
        self.aggregates_data[agg_id] = agg_data
        return agg_id


class MockMigrationManager:
    """Mock Nova migration manager."""

    def __init__(self):
        self.migrations_data = {}

    def list(self, **kwargs):
        """List migrations with filtering."""
        migrations = []
        for migration_data in self.migrations_data.values():
            # Apply filters
            if 'instance_uuid' in kwargs and migration_data.get('instance_uuid') != kwargs['instance_uuid']:
                continue
            if 'migration_type' in kwargs and migration_data.get('migration_type') != kwargs['migration_type']:
                continue
            if 'source_compute' in kwargs and migration_data.get('source_compute') != kwargs['source_compute']:
                continue

            migration = Mock()
            for key, value in migration_data.items():
                setattr(migration, key, value)
            migrations.append(migration)
        return migrations

    def add_migration(self, **kwargs):
        """Add a migration for testing."""
        migration_id = kwargs.get('id', f"migration-{len(self.migrations_data)}")
        migration_data = {
            'id': migration_id,
            'instance_uuid': kwargs.get('instance_uuid', 'server-1'),
            'source_compute': kwargs.get('source_compute', 'compute-0'),
            'dest_compute': kwargs.get('dest_compute', 'compute-1'),
            'migration_type': kwargs.get('migration_type', 'evacuation'),
            'status': kwargs.get('status', 'running'),
            'created_at': kwargs.get('created_at', datetime.now().isoformat())
        }
        self.migrations_data[migration_id] = migration_data
        return migration_id


class MockImageManager:
    """Mock image manager for testing."""

    def __init__(self):
        self.images = []

    def list(self):
        """List all images."""
        return self.images

    def get(self, image_id):
        """Get specific image by ID."""
        for image in self.images:
            if image.id == image_id:
                return image
        raise Exception(f"Image {image_id} not found")

    def add_image(self, **kwargs):
        """Add a new image."""
        image = Mock()
        image.id = kwargs.get('id', f'image-{len(self.images)}')
        image.tags = kwargs.get('tags', [])
        image.metadata = kwargs.get('metadata', {})
        image.properties = kwargs.get('properties', {})
        self.images.append(image)
        return image


class FunctionalTestEnvironment:
    """Test environment setup for functional tests."""

    def __init__(self):
        self.temp_dir = tempfile.mkdtemp()
        self.mock_nova = MockNovaService()
        self.config_manager = None
        self.service = None

        # Set up test configuration
        self._create_test_configs()
        self._setup_service()

    def _create_test_configs(self):
        """Create test configuration files."""
        # Main config
        config_data = {
            'config': {
                'EVACUABLE_TAG': 'evacuable',
                'DELTA': 30,
                'POLL': 15,
                'THRESHOLD': 50,
                'WORKERS': 2,
                'DELAY': 0,
                'LOGLEVEL': 'INFO',
                'SMART_EVACUATION': True,
                'RESERVED_HOSTS': True,
                'TAGGED_IMAGES': True,
                'TAGGED_FLAVORS': True,
                'TAGGED_AGGREGATES': True,
                'LEAVE_DISABLED': False,
                'FORCE_ENABLE': False,
                'CHECK_KDUMP': False,
                'DISABLED': False,
                'SSL_VERIFY': True
            }
        }

        config_path = os.path.join(self.temp_dir, 'config.yaml')
        with open(config_path, 'w') as f:
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

        clouds_path = os.path.join(self.temp_dir, 'clouds.yaml')
        with open(clouds_path, 'w') as f:
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

        secure_path = os.path.join(self.temp_dir, 'secure.yaml')
        with open(secure_path, 'w') as f:
            yaml.dump(secure_data, f)

        # Fencing config
        fencing_data = {
            'FencingConfig': {
                'compute-0': {
                    'agent': 'noop'
                },
                'compute-1': {
                    'agent': 'ipmi',
                    'ipaddr': '192.168.1.100',
                    'ipport': '623',
                    'login': 'admin',
                    'passwd': 'password'
                }
            }
        }

        fencing_path = os.path.join(self.temp_dir, 'fencing.yaml')
        with open(fencing_path, 'w') as f:
            yaml.dump(fencing_data, f)

        # Create ConfigManager
        self.config_manager = instanceha.ConfigManager(config_path)
        self.config_manager.clouds_path = clouds_path
        self.config_manager.secure_path = secure_path
        self.config_manager.fencing_path = fencing_path
        # Reinitialize with updated paths
        self.config_manager.config = self.config_manager._load_config()
        self.config_manager.clouds = self.config_manager._load_clouds_config()
        self.config_manager.secure = self.config_manager._load_secure_config()
        self.config_manager.fencing = self.config_manager._load_fencing_config()

    def _setup_service(self):
        """Set up the InstanceHA service."""
        assert self.config_manager is not None, "ConfigManager must be initialized"
        self.service = instanceha.InstanceHAService(self.config_manager, self.mock_nova)

    def add_compute_node(self, host, state='up', status='enabled', **kwargs):
        """Add a compute node to the test environment."""
        return self.mock_nova.services.add_service(
            host=host,
            state=state,
            status=status,
            binary='nova-compute',
            **kwargs
        )

    def add_server(self, host, evacuable=True, **kwargs):
        """Add a server to a compute host."""
        # Set up evacuable flavor/image if needed
        if evacuable:
            flavor = {'id': 'evacuable-flavor', 'extra_specs': {'evacuable': 'true'}}
            image = {'id': 'evacuable-image'}
        else:
            flavor = {'id': 'regular-flavor', 'extra_specs': {}}
            image = {'id': 'regular-image'}

        kwargs.setdefault('flavor', flavor)
        kwargs.setdefault('image', image)
        kwargs.setdefault('host', host)

        return self.mock_nova.servers.add_server(**kwargs)

    def add_evacuable_flavor(self, flavor_id=None, tag_value='true'):
        """Add an evacuable flavor."""
        return self.mock_nova.flavors.add_flavor(
            id=flavor_id,
            extra_specs={'evacuable': tag_value}
        )

    def add_evacuable_image(self, image_id=None, tag_value='evacuable'):
        """Add an evacuable image to the test environment."""
        if image_id is None:
            image_id = f'evacuable-image-{len(getattr(self.mock_nova, "images", []))}'

        # Mock image object with evacuable tag
        image = Mock()
        image.id = image_id
        image.tags = [tag_value]  # Glance v2 API uses tags as list
        image.metadata = {tag_value: 'true'}  # Fallback metadata
        image.properties = {tag_value: 'true'}  # Another fallback

        # Add to mock Nova's image list if it doesn't exist
        if not hasattr(self.mock_nova, 'images'):
            self.mock_nova.images = Mock()
            self.mock_nova.images.images = []
            self.mock_nova.images.list = lambda: self.mock_nova.images.images

        self.mock_nova.images.images.append(image)
        return image_id

    def add_evacuable_aggregate(self, hosts, tag_value='true'):
        """Add an evacuable aggregate."""
        return self.mock_nova.aggregates.add_aggregate(
            hosts=hosts,
            metadata={'evacuable': tag_value}
        )

    def simulate_host_failure(self, host):
        """Simulate a host failure by marking it as down."""
        for service in self.mock_nova.services.services_data:
            if service['host'] == host:
                service['state'] = 'down'
                service['updated_at'] = (datetime.now() - timedelta(minutes=5)).isoformat()
                break

    def cleanup(self):
        """Clean up test environment."""
        import shutil
        shutil.rmtree(self.temp_dir)


class TestBasicEvacuation(unittest.TestCase):
    """Test basic evacuation functionality."""

    def setUp(self):
        """Set up test environment."""
        self.env = FunctionalTestEnvironment()

    def tearDown(self):
        """Clean up test environment."""
        self.env.cleanup()

    def test_single_host_evacuation(self):
        """Test evacuation of a single failed host."""
        # Set up test scenario with proper evacuable flavor
        compute_id = self.env.add_compute_node('compute-0')
        # Add evacuable flavor to the mock environment
        self.env.add_evacuable_flavor('evacuable-flavor')
        # Add server that uses the evacuable flavor
        server_id = self.env.add_server('compute-0', evacuable=True,
                                       flavor={'id': 'evacuable-flavor', 'extra_specs': {'evacuable': 'true'}})

        # Simulate host failure
        self.env.simulate_host_failure('compute-0')

        # Find the failed service
        services = self.env.mock_nova.services.list(binary='nova-compute')
        failed_service = [s for s in services if s.host == 'compute-0' and s.state == 'down'][0]

        # Test evacuation process - mock ALL the functions that interact with external systems
        with patch('instanceha._get_nova_connection', return_value=self.env.mock_nova) as mock_get_conn:
            with patch('instanceha._host_fence', return_value=True) as mock_fence:
                with patch('instanceha._host_disable', return_value=True) as mock_disable:
                    with patch('instanceha._manage_reserved_hosts', return_value=True) as mock_reserved:
                        with patch('instanceha._host_evacuate', return_value=True) as mock_evacuate:
                            with patch('instanceha._post_evacuation_recovery', return_value=True) as mock_recovery:
                                result = instanceha.process_service(failed_service, [], False, self.env.service)

        # Verify results
        self.assertTrue(result)
        mock_get_conn.assert_called_once()
        mock_fence.assert_called_once()
        mock_disable.assert_called_once()
        mock_reserved.assert_called_once()
        mock_evacuate.assert_called_once()
        mock_recovery.assert_called_once()

    def test_evacuation_with_non_evacuable_servers(self):
        """Test evacuation when servers are not marked as evacuable."""
        # Set up test scenario with both evacuable and non-evacuable flavors
        self.env.add_compute_node('compute-0')

        # Add evacuable flavor to the mock environment (so tagged resources exist)
        self.env.add_evacuable_flavor('evacuable-flavor')

        # Add server that uses a NON-evacuable flavor
        server_id = self.env.add_server('compute-0', evacuable=False,
                                       flavor={'id': 'regular-flavor', 'extra_specs': {}})

        # Simulate host failure
        self.env.simulate_host_failure('compute-0')

        # Test server evacuability
        servers = self.env.mock_nova.servers.list(search_opts={'host': 'compute-0'})
        server = servers[0]

        # Get evacuable flavors from the service (should find the evacuable-flavor)
        assert self.env.service is not None, "Service must be initialized"
        evac_flavors = self.env.service.get_evacuable_flavors()
        evac_images = self.env.service.get_evacuable_images()

        # Should not be evacuable because it doesn't use an evacuable flavor
        is_evacuable = self.env.service.is_server_evacuable(server, evac_flavors, evac_images)
        self.assertFalse(is_evacuable)

    def test_evacuation_with_reserved_hosts(self):
        """Test evacuation with reserved host management."""
        # Set up test scenario
        self.env.add_compute_node('compute-0')  # Failed host
        reserved_id = self.env.add_compute_node('compute-reserved', status='disabled', disabled_reason='reserved')
        self.env.add_server('compute-0', evacuable=True)

        # Simulate host failure
        self.env.simulate_host_failure('compute-0')

        # Test reserved host management
        services = self.env.mock_nova.services.list()
        failed_service = [s for s in services if s.host == 'compute-0'][0]
        reserved_hosts = [s for s in services if 'reserved' in str(s.disabled_reason)]

        with patch('instanceha._enable_matching_reserved_host', return_value=True) as mock_enable:
            result = instanceha._manage_reserved_hosts(
                self.env.mock_nova, failed_service, reserved_hosts, self.env.service
            )

        self.assertTrue(result)
        mock_enable.assert_called_once()


class TestConfigurationValidation(unittest.TestCase):
    """Test configuration validation functionality."""

    def setUp(self):
        """Set up test environment."""
        self.env = FunctionalTestEnvironment()

    def tearDown(self):
        """Clean up test environment."""
        self.env.cleanup()

    def test_configuration_loading(self):
        """Test configuration loading and validation."""
        assert self.env.config_manager is not None, "ConfigManager must be initialized"
        config = self.env.config_manager

        # Test basic configuration values
        self.assertEqual(config.get_evacuable_tag(), 'evacuable')
        self.assertEqual(config.get_delta(), 30)
        self.assertEqual(config.get_poll_interval(), 15)
        self.assertEqual(config.get_threshold(), 50)
        self.assertEqual(config.get_workers(), 2)

        # Test boolean configurations
        self.assertTrue(config.is_smart_evacuation_enabled())
        self.assertTrue(config.is_reserved_hosts_enabled())
        self.assertTrue(config.is_tagged_images_enabled())
        self.assertTrue(config.is_tagged_flavors_enabled())
        self.assertTrue(config.is_tagged_aggregates_enabled())
        self.assertFalse(config.is_leave_disabled_enabled())

    def test_configuration_bounds_checking(self):
        """Test configuration bounds and validation."""
        config = self.env.config_manager

        # Test integer bounds
        self.assertEqual(config.get_int('WORKERS', min_val=1, max_val=10), 2)
        self.assertEqual(config.get_int('INVALID_KEY', default=5, min_val=1, max_val=10), 5)

        # Test minimum enforcement
        self.assertEqual(config.get_int('POLL', min_val=60), 60)  # Should enforce minimum

    def test_invalid_configuration_keys(self):
        """Test handling of invalid configuration keys."""
        config = self.env.config_manager

        # Should raise ValueError for unknown keys
        with self.assertRaises(ValueError):
            config.get_config_value('NONEXISTENT_KEY')


class TestCachingAndPerformance(unittest.TestCase):
    """Test caching and performance optimization features."""

    def setUp(self):
        """Set up test environment."""
        self.env = FunctionalTestEnvironment()

    def tearDown(self):
        """Clean up test environment."""
        self.env.cleanup()

    def test_flavor_caching(self):
        """Test evacuable flavor caching mechanism."""
        # Add evacuable flavors
        self.env.add_evacuable_flavor('flavor-1')
        self.env.add_evacuable_flavor('flavor-2')

        # First call should populate cache
        flavors1 = self.env.service.get_evacuable_flavors()

        # Second call should use cache
        flavors2 = self.env.service.get_evacuable_flavors()

        # Should be identical
        self.assertEqual(flavors1, flavors2)
        self.assertEqual(len(flavors1), 2)

    def test_cache_refresh(self):
        """Test cache refresh functionality."""
        # Initial cache setup
        self.env.service._evacuable_flavors_cache = ['old-flavor']

        # Add new flavors
        self.env.add_evacuable_flavor('new-flavor-1')
        self.env.add_evacuable_flavor('new-flavor-2')

        # Force cache refresh
        refreshed = self.env.service.refresh_evacuable_cache(force=True)
        self.assertTrue(refreshed)

        # Cache should be updated
        new_flavors = self.env.service.get_evacuable_flavors()
        self.assertIn('new-flavor-1', new_flavors)
        self.assertIn('new-flavor-2', new_flavors)
        self.assertNotIn('old-flavor', new_flavors)

    def test_host_server_caching(self):
        """Test host-server mapping caching."""
        # Set up hosts and servers
        self.env.add_compute_node('compute-0')
        self.env.add_compute_node('compute-1')
        self.env.add_server('compute-0', id='server-1')
        self.env.add_server('compute-0', id='server-2')
        self.env.add_server('compute-1', id='server-3')

        # Test caching
        services = self.env.mock_nova.services.list(binary='nova-compute')
        cache = self.env.service.get_hosts_with_servers_cached(self.env.mock_nova, services)

        # Verify cache structure
        self.assertIn('compute-0', cache)
        self.assertIn('compute-1', cache)
        self.assertEqual(len(cache['compute-0']), 2)
        self.assertEqual(len(cache['compute-1']), 1)


class TestAggregateEvacuation(unittest.TestCase):
    """Test aggregate-based evacuation logic."""

    def setUp(self):
        """Set up test environment."""
        self.env = FunctionalTestEnvironment()

    def tearDown(self):
        """Clean up test environment."""
        self.env.cleanup()

    def test_aggregate_evacuability_checking(self):
        """Test aggregate evacuability checking."""
        # Set up evacuable aggregate
        self.env.add_evacuable_aggregate(['compute-0', 'compute-1'])

        # Test evacuability
        is_evacuable_0 = self.env.service.is_aggregate_evacuable(self.env.mock_nova, 'compute-0')
        is_evacuable_1 = self.env.service.is_aggregate_evacuable(self.env.mock_nova, 'compute-1')
        is_evacuable_2 = self.env.service.is_aggregate_evacuable(self.env.mock_nova, 'compute-2')

        self.assertTrue(is_evacuable_0)
        self.assertTrue(is_evacuable_1)
        self.assertFalse(is_evacuable_2)

    def test_non_evacuable_aggregate(self):
        """Test non-evacuable aggregate behavior."""
        # Set up non-evacuable aggregate
        self.env.add_evacuable_aggregate(['compute-0'], tag_value='false')

        # Test evacuability
        is_evacuable = self.env.service.is_aggregate_evacuable(self.env.mock_nova, 'compute-0')
        self.assertFalse(is_evacuable)


class TestMetricsAndMonitoring(unittest.TestCase):
    """Test metrics collection and monitoring functionality."""

    def setUp(self):
        """Set up test environment."""
        self.metrics = instanceha.Metrics()

    def test_metrics_collection(self):
        """Test basic metrics collection."""
        # Test counter increments
        self.metrics.increment('test_counter')
        self.metrics.increment('test_counter', 5)
        self.assertEqual(self.metrics.counters['test_counter'], 6)

        # Test duration recording
        self.metrics.record_duration('test_operation', 1.5)
        self.metrics.record_duration('test_operation', 2.5)
        self.assertEqual(self.metrics.durations['test_operation'], 4.0)

    def test_timing_context(self):
        """Test timing context manager."""
        with self.metrics.timer('test_timer'):
            time.sleep(0.01)  # Small delay for timing

        # Verify metrics were recorded
        self.assertIn('test_timer_total', self.metrics.counters)
        self.assertIn('test_timer_successful', self.metrics.counters)
        self.assertIn('test_timer_duration', self.metrics.durations)
        self.assertEqual(self.metrics.counters['test_timer_total'], 1)
        self.assertEqual(self.metrics.counters['test_timer_successful'], 1)
        self.assertGreater(self.metrics.durations['test_timer_duration'], 0)

    def test_metrics_summary(self):
        """Test metrics summary generation."""
        # Add some test data
        self.metrics.increment('evacuations_total', 10)
        self.metrics.increment('evacuations_successful', 8)
        self.metrics.record_duration('evacuation_duration', 120.5)

        summary = self.metrics.get_summary()

        # Verify summary structure
        self.assertIn('uptime_seconds', summary)
        self.assertIn('counters', summary)
        self.assertIn('total_durations', summary)
        self.assertEqual(summary['counters']['evacuations_total'], 10)
        self.assertEqual(summary['counters']['evacuations_successful'], 8)
        self.assertEqual(summary['total_durations']['evacuation_duration'], 120.5)


class TestEvacuationLogicCombinations(unittest.TestCase):
    """Comprehensive tests for all evacuation logic combinations."""

    def setUp(self):
        """Set up test environment."""
        self.env = FunctionalTestEnvironment()

    def tearDown(self):
        """Clean up test environment."""
        self.env.cleanup()

    def test_all_tagging_disabled(self):
        """Test evacuation when all tagging features are disabled."""
        # Configure to disable all tagging
        self.env.config_manager.config.update({
            'TAGGED_IMAGES': False,
            'TAGGED_FLAVORS': False,
            'TAGGED_AGGREGATES': False
        })

        # Set up hosts and servers
        self.env.add_compute_node('compute-0')
        self.env.add_server('compute-0', id='server-1', evacuable=False)  # Non-evacuable server
        self.env.add_server('compute-0', id='server-2', evacuable=True)   # Evacuable server

        # Get servers and test evacuability
        servers = self.env.mock_nova.servers.list(search_opts={'host': 'compute-0'})

        for server in servers:
            # All servers should be evacuable when tagging is disabled
            is_evacuable = self.env.service.is_server_evacuable(server)
            self.assertTrue(is_evacuable, f"Server {server.id} should be evacuable when tagging is disabled")

    def test_image_tagging_only_with_evacuable_images(self):
        """Test evacuation with only image tagging enabled and evacuable images exist."""
        # Configure image tagging only
        self.env.config_manager.config.update({
            'TAGGED_IMAGES': True,
            'TAGGED_FLAVORS': False,
            'TAGGED_AGGREGATES': False
        })

        # Add evacuable image
        self.env.add_evacuable_image('evacuable-image-1')

        # Set up servers
        self.env.add_compute_node('compute-0')
        evacuable_server = self.env.add_server('compute-0', id='server-evacuable',
                                             image={'id': 'evacuable-image-1'})
        non_evacuable_server = self.env.add_server('compute-0', id='server-non-evacuable',
                                                 image={'id': 'regular-image'})

        # Test evacuability
        servers = self.env.mock_nova.servers.list(search_opts={'host': 'compute-0'})
        evacuable_srv = [s for s in servers if s.id == 'server-evacuable'][0]
        non_evacuable_srv = [s for s in servers if s.id == 'server-non-evacuable'][0]

        self.assertTrue(self.env.service.is_server_evacuable(evacuable_srv),
                       "Server with evacuable image should be evacuable")
        self.assertFalse(self.env.service.is_server_evacuable(non_evacuable_srv),
                        "Server with non-evacuable image should not be evacuable")

    def test_image_tagging_only_no_evacuable_images(self):
        """Test evacuation with image tagging enabled but no evacuable images (backward compatibility)."""
        # Configure image tagging only
        self.env.config_manager.config.update({
            'TAGGED_IMAGES': True,
            'TAGGED_FLAVORS': False,
            'TAGGED_AGGREGATES': False
        })

        # Don't add any evacuable images

        # Set up servers
        self.env.add_compute_node('compute-0')
        self.env.add_server('compute-0', id='server-1', image={'id': 'regular-image'})

        # Test evacuability - should evacuate all (backward compatibility)
        servers = self.env.mock_nova.servers.list(search_opts={'host': 'compute-0'})
        server = servers[0]

        self.assertTrue(self.env.service.is_server_evacuable(server),
                       "Server should be evacuable when no tagged images exist (backward compatibility)")

    def test_flavor_tagging_only_with_evacuable_flavors(self):
        """Test evacuation with only flavor tagging enabled and evacuable flavors exist."""
        # Configure flavor tagging only
        self.env.config_manager.config.update({
            'TAGGED_IMAGES': False,
            'TAGGED_FLAVORS': True,
            'TAGGED_AGGREGATES': False
        })

        # Add evacuable flavor
        self.env.add_evacuable_flavor('evacuable-flavor-1')

        # Set up servers
        self.env.add_compute_node('compute-0')
        evacuable_server = self.env.add_server('compute-0', id='server-evacuable',
                                             flavor={'id': 'evacuable-flavor-1', 'extra_specs': {'evacuable': 'true'}})
        non_evacuable_server = self.env.add_server('compute-0', id='server-non-evacuable',
                                                 flavor={'id': 'regular-flavor', 'extra_specs': {}})

        # Test evacuability
        servers = self.env.mock_nova.servers.list(search_opts={'host': 'compute-0'})
        evacuable_srv = [s for s in servers if s.id == 'server-evacuable'][0]
        non_evacuable_srv = [s for s in servers if s.id == 'server-non-evacuable'][0]

        self.assertTrue(self.env.service.is_server_evacuable(evacuable_srv),
                       "Server with evacuable flavor should be evacuable")
        self.assertFalse(self.env.service.is_server_evacuable(non_evacuable_srv),
                        "Server with non-evacuable flavor should not be evacuable")

    def test_flavor_tagging_only_no_evacuable_flavors(self):
        """Test evacuation with flavor tagging enabled but no evacuable flavors (backward compatibility)."""
        # Configure flavor tagging only
        self.env.config_manager.config.update({
            'TAGGED_IMAGES': False,
            'TAGGED_FLAVORS': True,
            'TAGGED_AGGREGATES': False
        })

        # Don't add any evacuable flavors

        # Set up servers
        self.env.add_compute_node('compute-0')
        self.env.add_server('compute-0', id='server-1', flavor={'id': 'regular-flavor', 'extra_specs': {}})

        # Test evacuability - should evacuate all (backward compatibility)
        servers = self.env.mock_nova.servers.list(search_opts={'host': 'compute-0'})
        server = servers[0]

        self.assertTrue(self.env.service.is_server_evacuable(server),
                       "Server should be evacuable when no tagged flavors exist (backward compatibility)")

    def test_both_image_and_flavor_tagging_with_resources(self):
        """Test evacuation with both image and flavor tagging enabled (OR logic)."""
        # Configure both image and flavor tagging
        self.env.config_manager.config.update({
            'TAGGED_IMAGES': True,
            'TAGGED_FLAVORS': True,
            'TAGGED_AGGREGATES': False
        })

        # Add evacuable resources
        self.env.add_evacuable_image('evacuable-image-1')
        self.env.add_evacuable_flavor('evacuable-flavor-1')

        # Set up servers with different combinations
        self.env.add_compute_node('compute-0')

        # Server with evacuable image but non-evacuable flavor
        server_image_only = self.env.add_server('compute-0', id='server-image-only',
                                              image={'id': 'evacuable-image-1'},
                                              flavor={'id': 'regular-flavor', 'extra_specs': {}})

        # Server with evacuable flavor but non-evacuable image
        server_flavor_only = self.env.add_server('compute-0', id='server-flavor-only',
                                                image={'id': 'regular-image'},
                                                flavor={'id': 'evacuable-flavor-1', 'extra_specs': {'evacuable': 'true'}})

        # Server with both evacuable image and flavor
        server_both = self.env.add_server('compute-0', id='server-both',
                                        image={'id': 'evacuable-image-1'},
                                        flavor={'id': 'evacuable-flavor-1', 'extra_specs': {'evacuable': 'true'}})

        # Server with neither evacuable image nor flavor
        server_neither = self.env.add_server('compute-0', id='server-neither',
                                           image={'id': 'regular-image'},
                                           flavor={'id': 'regular-flavor', 'extra_specs': {}})

        # Test evacuability (OR logic - any match should make it evacuable)
        servers = self.env.mock_nova.servers.list(search_opts={'host': 'compute-0'})
        server_map = {s.id: s for s in servers}

        self.assertTrue(self.env.service.is_server_evacuable(server_map['server-image-only']),
                       "Server with evacuable image should be evacuable (OR logic)")
        self.assertTrue(self.env.service.is_server_evacuable(server_map['server-flavor-only']),
                       "Server with evacuable flavor should be evacuable (OR logic)")
        self.assertTrue(self.env.service.is_server_evacuable(server_map['server-both']),
                       "Server with both evacuable image and flavor should be evacuable")
        self.assertFalse(self.env.service.is_server_evacuable(server_map['server-neither']),
                        "Server with neither evacuable image nor flavor should not be evacuable")

    def test_both_image_and_flavor_tagging_no_resources(self):
        """Test evacuation with both tagging enabled but no evacuable resources (backward compatibility)."""
        # Configure both image and flavor tagging
        self.env.config_manager.config.update({
            'TAGGED_IMAGES': True,
            'TAGGED_FLAVORS': True,
            'TAGGED_AGGREGATES': False
        })

        # Don't add any evacuable resources

        # Set up servers
        self.env.add_compute_node('compute-0')
        self.env.add_server('compute-0', id='server-1',
                          image={'id': 'regular-image'},
                          flavor={'id': 'regular-flavor', 'extra_specs': {}})

        # Test evacuability - should evacuate all (backward compatibility)
        servers = self.env.mock_nova.servers.list(search_opts={'host': 'compute-0'})
        server = servers[0]

        self.assertTrue(self.env.service.is_server_evacuable(server),
                       "Server should be evacuable when no tagged resources exist (backward compatibility)")

    def test_aggregate_filtering_evacuable_host(self):
        """Test evacuation filtering based on evacuable aggregates."""
        # Configure aggregate tagging
        self.env.config_manager.config.update({
            'TAGGED_IMAGES': False,
            'TAGGED_FLAVORS': False,
            'TAGGED_AGGREGATES': True
        })

        # Set up hosts and aggregates
        self.env.add_compute_node('compute-evacuable')
        self.env.add_compute_node('compute-non-evacuable')

        # Add evacuable aggregate with one host
        self.env.add_evacuable_aggregate(['compute-evacuable'])

        # Add non-evacuable aggregate with other host
        self.env.mock_nova.aggregates.add_aggregate(
            name='non-evacuable-agg',
            hosts=['compute-non-evacuable'],
            metadata={}  # No evacuable tag
        )

        # Test aggregate evacuability
        self.assertTrue(self.env.service.is_aggregate_evacuable(self.env.mock_nova, 'compute-evacuable'),
                       "Host in evacuable aggregate should be evacuable")
        self.assertFalse(self.env.service.is_aggregate_evacuable(self.env.mock_nova, 'compute-non-evacuable'),
                        "Host in non-evacuable aggregate should not be evacuable")

    def test_composite_evacuable_tag_matching(self):
        """Test matching composite tags like 'trait:CUSTOM_EVACUABLE'."""
        # Configure flavor tagging
        self.env.config_manager.config.update({
            'TAGGED_IMAGES': False,
            'TAGGED_FLAVORS': True,
            'TAGGED_AGGREGATES': False
        })

        # Add flavor with composite tag
        self.env.mock_nova.flavors.add_flavor(
            id='composite-flavor',
            extra_specs={'trait:CUSTOM_evacuable': 'true'}  # Composite key containing evacuable tag
        )

        # Set up server
        self.env.add_compute_node('compute-0')
        self.env.add_server('compute-0', id='server-composite',
                          flavor={'id': 'composite-flavor', 'extra_specs': {'trait:CUSTOM_evacuable': 'true'}})

        # Test evacuability
        servers = self.env.mock_nova.servers.list(search_opts={'host': 'compute-0'})
        server = servers[0]

        self.assertTrue(self.env.service.is_server_evacuable(server),
                       "Server with composite evacuable tag should be evacuable")

    def test_case_insensitive_tag_values(self):
        """Test that evacuable tag values are case-insensitive."""
        # Configure flavor tagging
        self.env.config_manager.config.update({
            'TAGGED_IMAGES': False,
            'TAGGED_FLAVORS': True,
            'TAGGED_AGGREGATES': False
        })

        # Set up servers with different case values
        self.env.add_compute_node('compute-0')

        test_cases = [
            ('TRUE', True),
            ('True', True),
            ('true', True),
            ('FALSE', False),
            ('False', False),
            ('false', False),
            ('yes', False),  # Only 'true' values should work
            ('1', False),    # Only 'true' values should work
        ]

        for i, (value, expected) in enumerate(test_cases):
            flavor_id = f'test-flavor-{i}'
            server_id = f'server-{i}'

            # Add flavor with test value
            self.env.mock_nova.flavors.add_flavor(
                id=flavor_id,
                extra_specs={'evacuable': value}
            )

            # Add server using this flavor
            self.env.add_server('compute-0', id=server_id,
                              flavor={'id': flavor_id, 'extra_specs': {'evacuable': value}})

        # Test all servers
        servers = self.env.mock_nova.servers.list(search_opts={'host': 'compute-0'})

        for i, (value, expected) in enumerate(test_cases):
            server = [s for s in servers if s.id == f'server-{i}'][0]
            result = self.env.service.is_server_evacuable(server)

            self.assertEqual(result, expected,
                           f"Server with evacuable='{value}' should {'be' if expected else 'not be'} evacuable")

    def test_missing_server_attributes(self):
        """Test handling of servers with missing image or flavor attributes."""
        # Configure both image and flavor tagging
        self.env.config_manager.config.update({
            'TAGGED_IMAGES': True,
            'TAGGED_FLAVORS': True,
            'TAGGED_AGGREGATES': False
        })

        # Add evacuable resources
        self.env.add_evacuable_image('evacuable-image-1')
        self.env.add_evacuable_flavor('evacuable-flavor-1')

        # Set up compute node
        self.env.add_compute_node('compute-0')

        # Create server with missing image
        server_no_image = self.env.mock_nova.servers.add_server(
            id='server-no-image',
            host='compute-0',
            image=None,  # Missing image
            flavor={'id': 'evacuable-flavor-1', 'extra_specs': {'evacuable': 'true'}},
            status='ACTIVE'
        )

        # Create server with missing flavor
        server_no_flavor = self.env.mock_nova.servers.add_server(
            id='server-no-flavor',
            host='compute-0',
            image={'id': 'evacuable-image-1'},
            flavor=None,  # Missing flavor
            status='ACTIVE'
        )

        # Test evacuability - should handle gracefully
        servers = self.env.mock_nova.servers.list(search_opts={'host': 'compute-0'})
        server_map = {s.id: s for s in servers}

        # Server with missing image but evacuable flavor should still be evacuable
        self.assertTrue(self.env.service.is_server_evacuable(server_map['server-no-image']),
                       "Server with missing image but evacuable flavor should be evacuable")

        # Server with missing flavor but evacuable image should still be evacuable
        self.assertTrue(self.env.service.is_server_evacuable(server_map['server-no-flavor']),
                       "Server with missing flavor but evacuable image should be evacuable")

    def test_empty_extra_specs_handling(self):
        """Test handling of flavors with empty or missing extra_specs."""
        # Configure flavor tagging
        self.env.config_manager.config.update({
            'TAGGED_IMAGES': False,
            'TAGGED_FLAVORS': True,
            'TAGGED_AGGREGATES': False
        })

        # Add evacuable flavor for comparison
        self.env.add_evacuable_flavor('evacuable-flavor')

        # Set up compute node
        self.env.add_compute_node('compute-0')

        # Server with empty extra_specs
        server_empty_specs = self.env.add_server('compute-0', id='server-empty-specs',
                                               flavor={'id': 'empty-flavor', 'extra_specs': {}})

        # Server with missing extra_specs (None)
        server_no_specs = self.env.mock_nova.servers.add_server(
            id='server-no-specs',
            host='compute-0',
            image={'id': 'regular-image'},
            flavor={'id': 'no-specs-flavor'},  # Missing extra_specs key
            status='ACTIVE'
        )

        # Test evacuability
        servers = self.env.mock_nova.servers.list(search_opts={'host': 'compute-0'})
        server_map = {s.id: s for s in servers}

        # Both should be non-evacuable since they don't have evacuable tags
        self.assertFalse(self.env.service.is_server_evacuable(server_map['server-empty-specs']),
                        "Server with empty extra_specs should not be evacuable")
        self.assertFalse(self.env.service.is_server_evacuable(server_map['server-no-specs']),
                        "Server with missing extra_specs should not be evacuable")

    def test_performance_with_large_server_list(self):
        """Test evacuation logic performance with large number of servers."""
        # Configure both image and flavor tagging
        self.env.config_manager.config.update({
            'TAGGED_IMAGES': True,
            'TAGGED_FLAVORS': True,
            'TAGGED_AGGREGATES': False
        })

        # Add evacuable resources
        self.env.add_evacuable_image('evacuable-image')
        self.env.add_evacuable_flavor('evacuable-flavor')

        # Set up compute node
        self.env.add_compute_node('compute-0')

        # Add many servers (mix of evacuable and non-evacuable)
        server_count = 100
        for i in range(server_count):
            if i % 2 == 0:  # Even servers are evacuable
                self.env.add_server('compute-0', id=f'server-{i}',
                                  image={'id': 'evacuable-image'},
                                  flavor={'id': 'evacuable-flavor', 'extra_specs': {'evacuable': 'true'}})
            else:  # Odd servers are not evacuable
                self.env.add_server('compute-0', id=f'server-{i}',
                                  image={'id': 'regular-image'},
                                  flavor={'id': 'regular-flavor', 'extra_specs': {}})

        # Test filtering performance
        import time
        start_time = time.time()

        services = [Mock(host='compute-0')]
        host_servers_cache = self.env.service.get_hosts_with_servers_cached(self.env.mock_nova, services)
        filtered_services = self.env.service.filter_hosts_with_evacuable_servers(
            services, host_servers_cache,
            self.env.service.get_evacuable_flavors(),
            self.env.service.get_evacuable_images()
        )

        end_time = time.time()

        # Should complete reasonably quickly (less than 1 second for 100 servers)
        self.assertLess(end_time - start_time, 1.0,
                       "Evacuation filtering should complete quickly even with many servers")

        # Should find the host since it has evacuable servers
        self.assertEqual(len(filtered_services), 1,
                        "Should find compute node with evacuable servers")


def run_functional_tests():
    """Run all functional tests."""
    print("=" * 60)
    print("Running InstanceHA Functional Tests")
    print("=" * 60)

    # Create test suite
    test_suite = unittest.TestSuite()

    # Add test classes
    test_classes = [
        TestBasicEvacuation,
        TestConfigurationValidation,
        TestCachingAndPerformance,
        TestAggregateEvacuation,
        TestMetricsAndMonitoring,
        TestEvacuationLogicCombinations
    ]

    for test_class in test_classes:
        tests = unittest.TestLoader().loadTestsFromTestCase(test_class)
        test_suite.addTests(tests)

    # Run tests
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(test_suite)

    # Print summary
    print("\n" + "=" * 60)
    if result.wasSuccessful():
        print("✅ All functional tests passed!")
    else:
        print(f"❌ {len(result.failures)} test(s) failed, {len(result.errors)} error(s)")
        for test, traceback in result.failures + result.errors:
            print(f"FAILED: {test}")
            print(f"  {traceback.split('AssertionError:')[-1].strip()}")
    print("=" * 60)

    return result.wasSuccessful()


if __name__ == '__main__':
    import logging
    logging.basicConfig(level=logging.INFO)

    success = run_functional_tests()
    sys.exit(0 if success else 1)
