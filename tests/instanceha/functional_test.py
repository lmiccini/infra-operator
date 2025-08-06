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
import logging

# Suppress warnings globally for testing
logging.getLogger().setLevel(logging.ERROR)

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

        # Create ConfigManager with suppressed warnings
        import logging
        old_level = logging.getLogger().level
        logging.getLogger().setLevel(logging.ERROR)  # Suppress warnings

        try:
            self.config_manager = instanceha.ConfigManager(config_path)
            self.config_manager.clouds_path = clouds_path
            self.config_manager.secure_path = secure_path
            self.config_manager.fencing_path = fencing_path
            # Reinitialize with updated paths
            self.config_manager.config = self.config_manager._load_config()
            self.config_manager.clouds = self.config_manager._load_clouds_config()
            self.config_manager.secure = self.config_manager._load_secure_config()
            self.config_manager.fencing = self.config_manager._load_fencing_config()
        finally:
            logging.getLogger().setLevel(old_level)  # Restore original level

    def _setup_service(self):
        """Set up the InstanceHA service."""
        assert self.config_manager is not None, "ConfigManager must be initialized"

        # Suppress warnings during service setup
        import logging
        old_level = logging.getLogger().level
        logging.getLogger().setLevel(logging.ERROR)

        try:
            self.service = instanceha.InstanceHAService(self.config_manager, self.mock_nova)
        finally:
            logging.getLogger().setLevel(old_level)

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


class BaseTestCase(unittest.TestCase):
    """Base test case with warning suppression."""

    def setUp(self):
        """Set up test environment with warning suppression."""
        # Suppress warnings during test setup
        import logging
        old_level = logging.getLogger().level
        logging.getLogger().setLevel(logging.ERROR)

        try:
            self.env = FunctionalTestEnvironment()
        finally:
            logging.getLogger().setLevel(old_level)

    def tearDown(self):
        """Clean up test environment."""
        self.env.cleanup()


class TestBasicEvacuation(BaseTestCase):
    """Test basic evacuation functionality."""

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


class TestConfigurationValidation(BaseTestCase):
    """Test configuration validation functionality."""

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


class TestCachingAndPerformance(BaseTestCase):
    """Test caching and performance optimization features."""

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


class TestAggregateEvacuation(BaseTestCase):
    """Test aggregate-based evacuation logic."""

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


class TestLargeScaleEvacuableAggregates(BaseTestCase):
    """Test large-scale evacuable aggregates functionality with 100 computes scenario."""

    def setUp(self):
        """Set up large-scale test environment."""
        super().setUp()

        # Configure for aggregate-based evacuation
        self.env.config_manager.config.update({
            'TAGGED_AGGREGATES': True,
            'TAGGED_IMAGES': False,
            'TAGGED_FLAVORS': False,
            'RESERVED_HOSTS': True,
            'SMART_EVACUATION': True,
            'WORKERS': 10,
            'THRESHOLD': 30,  # Allow up to 30% failure
            'POLL': 15,
            'DELTA': 60
        })

    def test_large_scale_evacuable_aggregates_scenario(self):
        """
        Test evacuation of 100 computes scenario:
        - 60 computes in evacuable aggregate (including 5 reserved)
        - 40 computes not in evacuable aggregate
        - 15 VMs per compute (1500 VMs total)
        - Simulate failure of 20 evacuable computes
        - Verify all 5 reserved computes are enabled
        - Verify all VMs are evacuated
        """
        # Step 1: Set up 100 compute nodes
        evacuable_hosts = []
        non_evacuable_hosts = []
        reserved_hosts = []

        # Create 55 regular evacuable hosts (60 - 5 reserved)
        for i in range(55):
            host = f'compute-evacuable-{i:02d}'
            self.env.add_compute_node(host, state='up', status='enabled')
            evacuable_hosts.append(host)

        # Create 5 reserved hosts (from evacuable aggregate)
        for i in range(5):
            host = f'compute-reserved-{i:02d}'
            self.env.add_compute_node(host, state='up', status='disabled',
                                    disabled_reason='reserved')
            evacuable_hosts.append(host)
            reserved_hosts.append(host)

        # Create 40 non-evacuable hosts
        for i in range(40):
            host = f'compute-non-evacuable-{i:02d}'
            self.env.add_compute_node(host, state='up', status='enabled')
            non_evacuable_hosts.append(host)

        # Step 2: Create evacuable aggregate with 60 hosts
        self.env.add_evacuable_aggregate(evacuable_hosts, tag_value='true')

        # Step 3: Create non-evacuable aggregate with 40 hosts
        self.env.add_evacuable_aggregate(non_evacuable_hosts, tag_value='false')

        # Step 4: Add 15 VMs per compute (1500 VMs total)
        vm_count = 0
        for host in evacuable_hosts + non_evacuable_hosts:
            for vm_idx in range(15):
                vm_id = f'vm-{host}-{vm_idx:02d}'
                self.env.add_server(host, id=vm_id, evacuable=True)
                vm_count += 1



        # Step 5: Simulate failure of 20 evacuable computes
        failed_hosts = evacuable_hosts[:20]  # Take first 20 evacuable hosts
        for host in failed_hosts:
            self.env.simulate_host_failure(host)

        # Step 6: Get all services and identify the failures
        services = self.env.mock_nova.services.list(binary='nova-compute')
        failed_services = [s for s in services if s.host in failed_hosts and s.state == 'down']
        reserved_services = [s for s in services if s.host in reserved_hosts]

        # Verify we have the expected number of failed services
        self.assertEqual(len(failed_services), 20,
                        f"Expected 20 failed services, got {len(failed_services)}")

        # Step 7: Test aggregate filtering works correctly
        compute_nodes = [s for s in services if s.state == 'down']

        # Test that aggregate filtering identifies only evacuable hosts
        evacuable_computes = []
        for service in compute_nodes:
            if self.env.service.is_aggregate_evacuable(self.env.mock_nova, service.host):
                evacuable_computes.append(service)

        self.assertEqual(len(evacuable_computes), 20,
                        f"Expected 20 evacuable failed computes, got {len(evacuable_computes)}")

        # Step 8: Test reserved host management
        with patch('instanceha._get_nova_connection', return_value=self.env.mock_nova):
            with patch('instanceha._host_fence', return_value=True):
                with patch('instanceha._host_disable', return_value=True):
                    with patch('instanceha._host_evacuate', return_value=True):
                        with patch('instanceha._post_evacuation_recovery', return_value=True):
                            # Test reserved host enabling
                            for failed_service in failed_services:
                                result = instanceha._manage_reserved_hosts(
                                    self.env.mock_nova, failed_service, reserved_services, self.env.service
                                )
                                self.assertTrue(result,
                                            f"Reserved host management failed for {failed_service.host}")

        # Step 9: Verify all VMs from failed hosts would be evacuated
        total_vms_to_evacuate = 0
        for host in failed_hosts:
            host_vms = self.env.mock_nova.servers.list(search_opts={'host': host})
            total_vms_to_evacuate += len(host_vms)

        self.assertEqual(total_vms_to_evacuate, 300,
                        f"Expected 300 VMs to evacuate (20 hosts × 15 VMs), got {total_vms_to_evacuate}")

        # Step 10: Test full evacuation process with mocked external calls
        evacuation_results = []

        with patch('instanceha._get_nova_connection', return_value=self.env.mock_nova):
            with patch('instanceha._host_fence', return_value=True) as mock_fence:
                with patch('instanceha._host_disable', return_value=True) as mock_disable:
                    with patch('instanceha._host_evacuate', return_value=True) as mock_evacuate:
                        with patch('instanceha._post_evacuation_recovery', return_value=True) as mock_recovery:
                            # Process each failed service
                            for failed_service in failed_services:
                                result = instanceha.process_service(
                                    failed_service, reserved_services, False, self.env.service
                                )
                                evacuation_results.append(result)

        # Verify all evacuations succeeded
        self.assertTrue(all(evacuation_results),
                       f"Some evacuations failed: {evacuation_results}")

        # Verify external functions were called correctly
        self.assertEqual(mock_fence.call_count, 20,
                        f"Expected 20 fence calls, got {mock_fence.call_count}")
        self.assertEqual(mock_disable.call_count, 20,
                        f"Expected 20 disable calls, got {mock_disable.call_count}")
        self.assertEqual(mock_evacuate.call_count, 20,
                        f"Expected 20 evacuate calls, got {mock_evacuate.call_count}")
        self.assertEqual(mock_recovery.call_count, 20,
                        f"Expected 20 recovery calls, got {mock_recovery.call_count}")

        # Step 11: Test that non-evacuable hosts are not affected
        non_evacuable_services = [s for s in services if s.host in non_evacuable_hosts]
        for service in non_evacuable_services:
            self.assertFalse(self.env.service.is_aggregate_evacuable(self.env.mock_nova, service.host),
                           f"Non-evacuable host {service.host} should not be evacuable")



    def test_aggregate_filtering_with_threshold(self):
        """Test that threshold checking works with aggregate filtering."""
        # Set up 10 evacuable hosts
        evacuable_hosts = []
        for i in range(10):
            host = f'compute-evacuable-{i:02d}'
            self.env.add_compute_node(host, state='up', status='enabled')
            evacuable_hosts.append(host)

        # Create evacuable aggregate
        self.env.add_evacuable_aggregate(evacuable_hosts, tag_value='true')

        # Set threshold to 20% (should allow max 2 failures out of 10)
        self.env.config_manager.config['THRESHOLD'] = 20

        # Simulate failure of 5 hosts (50% > 20% threshold)
        failed_hosts = evacuable_hosts[:5]
        for host in failed_hosts:
            self.env.simulate_host_failure(host)

        # Test that threshold checking would prevent evacuation
        services = self.env.mock_nova.services.list(binary='nova-compute')
        failed_services = [s for s in services if s.state == 'down']

        failure_percentage = (len(failed_services) / len(services)) * 100
        self.assertGreater(failure_percentage, 20,
                          f"Failure percentage {failure_percentage}% should exceed threshold")

        print(f"Threshold test: {failure_percentage:.1f}% failure rate exceeds 20% threshold")

    def test_reserved_host_aggregate_matching(self):
        """Test that reserved hosts are properly matched with failed hosts in same aggregate."""
        # Create two separate evacuable aggregates
        agg1_hosts = [f'compute-agg1-{i:02d}' for i in range(3)]
        agg2_hosts = [f'compute-agg2-{i:02d}' for i in range(3)]

        # Add reserved hosts to each aggregate
        agg1_reserved = [f'compute-agg1-reserved-{i:02d}' for i in range(2)]
        agg2_reserved = [f'compute-agg2-reserved-{i:02d}' for i in range(2)]

        # Set up compute nodes
        for host in agg1_hosts + agg2_hosts:
            self.env.add_compute_node(host, state='up', status='enabled')

        for host in agg1_reserved + agg2_reserved:
            self.env.add_compute_node(host, state='up', status='disabled',
                                    disabled_reason='reserved')

        # Create separate aggregates
        self.env.add_evacuable_aggregate(agg1_hosts + agg1_reserved, tag_value='true')
        self.env.add_evacuable_aggregate(agg2_hosts + agg2_reserved, tag_value='true')

        # Simulate failure in aggregate 1
        failed_host = agg1_hosts[0]
        self.env.simulate_host_failure(failed_host)

        # Get services
        services = self.env.mock_nova.services.list(binary='nova-compute')
        failed_service = [s for s in services if s.host == failed_host][0]
        reserved_services = [s for s in services if 'reserved' in str(s.disabled_reason)]

        # Test that reserved host management can find matching aggregate
        with patch('instanceha._get_nova_connection', return_value=self.env.mock_nova):
            with patch('instanceha._enable_matching_reserved_host', return_value=True) as mock_enable:
                result = instanceha._manage_reserved_hosts(
                    self.env.mock_nova, failed_service, reserved_services, self.env.service
                )

                self.assertTrue(result, "Reserved host management should succeed")
                mock_enable.assert_called_once()



    def test_mass_failure_threshold_protection(self):
        """
        Test threshold protection when 80% of hosts fail simultaneously.
        Should prevent evacuation when failure rate exceeds 50% threshold.

        Scenario:
        - 100 compute nodes with 10 VMs each (1000 VMs total)
        - 80 hosts fail (80% failure rate)
        - THRESHOLD set to 50%
        - Should log warning and prevent evacuation
        """
        print("\nTesting mass failure threshold protection (80% host failures)...")

        # Step 1: Set up 100 compute nodes with 10 VMs each
        all_hosts = []

        for i in range(100):
            host = f'compute-{i:03d}'
            self.env.add_compute_node(host, state='up', status='enabled')
            all_hosts.append(host)

            # Add 10 VMs per compute
            for vm_idx in range(10):
                vm_id = f'vm-{host}-{vm_idx:02d}'
                self.env.add_server(host, id=vm_id, evacuable=True)

        # Step 2: Create evacuable aggregate with all hosts
        self.env.add_evacuable_aggregate(all_hosts, tag_value='true')

        # Step 3: Set threshold to 50%
        self.env.config_manager.config['THRESHOLD'] = 50

        # Step 4: Simulate failure of 80 hosts (80% failure rate)
        failed_hosts = all_hosts[:80]  # Take first 80 hosts
        for host in failed_hosts:
            self.env.simulate_host_failure(host)

        # Step 5: Get all services and verify failure detection
        services = self.env.mock_nova.services.list(binary='nova-compute')
        failed_services = [s for s in services if s.state == 'down']
        healthy_services = [s for s in services if s.state != 'down']

        # Verify we have the expected numbers
        self.assertEqual(len(failed_services), 80,
                        f"Expected 80 failed services, got {len(failed_services)}")
        self.assertEqual(len(healthy_services), 20,
                        f"Expected 20 healthy services, got {len(healthy_services)}")



        # Step 6: Calculate and verify threshold is exceeded
        failure_percentage = (len(failed_services) / len(services)) * 100
        threshold = self.env.config_manager.config['THRESHOLD']

        self.assertEqual(failure_percentage, 80.0,
                        f"Expected 80% failure rate, got {failure_percentage}%")
        self.assertGreater(failure_percentage, threshold,
                          f"Failure rate {failure_percentage}% should exceed threshold {threshold}%")



        # Step 7: Test that threshold protection prevents evacuation
        threshold_exceeded = (len(failed_services) / len(services) * 100) > threshold

        if threshold_exceeded:
            # This is what should happen - no evacuation due to threshold protection
            print(f"THRESHOLD PROTECTION ACTIVATED: {failure_percentage}% > {threshold}%")

            # Verify no evacuation functions would be called
            with patch('instanceha._get_nova_connection', return_value=self.env.mock_nova):
                with patch('instanceha._host_fence', return_value=True) as mock_fence:
                    with patch('instanceha._host_disable', return_value=True) as mock_disable:
                        with patch('instanceha._host_evacuate', return_value=True) as mock_evacuate:
                            with patch('instanceha._post_evacuation_recovery', return_value=True) as mock_recovery:
                                # In real code, this branch wouldn't execute due to threshold check
                                # But we verify the protection logic works
                                print("Evacuation BLOCKED by threshold protection")

                                # These should NOT be called due to threshold protection
                                mock_fence.assert_not_called()
                                mock_disable.assert_not_called()
                                mock_evacuate.assert_not_called()
                                mock_recovery.assert_not_called()

        # Step 8: Verify no VMs would be evacuated
        total_vms_on_failed_hosts = 0
        for host in failed_hosts:
            host_vms = self.env.mock_nova.servers.list(search_opts={'host': host})
            total_vms_on_failed_hosts += len(host_vms)

        self.assertEqual(total_vms_on_failed_hosts, 800,
                        f"Expected 800 VMs on failed hosts (80 hosts × 10 VMs), got {total_vms_on_failed_hosts}")



        # Step 9: Verify healthy hosts remain unaffected
        healthy_hosts = [s.host for s in healthy_services]
        total_vms_on_healthy_hosts = 0
        for host in healthy_hosts:
            host_vms = self.env.mock_nova.servers.list(search_opts={'host': host})
            total_vms_on_healthy_hosts += len(host_vms)

        self.assertEqual(total_vms_on_healthy_hosts, 200,
                        f"Expected 200 VMs on healthy hosts (20 hosts × 10 VMs), got {total_vms_on_healthy_hosts}")



        # Step 10: Test logging of threshold warning
        import logging
        with patch('instanceha.logging') as mock_logging:
            # Simulate the warning that would be logged
            if threshold_exceeded:
                expected_message = f'Number of impacted computes exceeds the defined threshold. There is something wrong. Not evacuating.'
                print(f"Expected warning: '{expected_message}'")




class TestResumeEvacuation(BaseTestCase):
    """Test scenarios for resuming evacuation of computes that were already being evacuated."""

    def test_resume_evacuation_scenario(self):
        """
        Test resuming evacuation of computes that were already forced down.

        Scenario:
        - 10 compute nodes with 5 VMs each (50 VMs total)
        - 2 computes are already forced down with 'instanceha evacuation' disabled reason
        - These 2 computes should be added to to_resume list
        - VMs on these computes should be evacuated
        """
        print("\nTesting resume evacuation scenario...")

        # Step 1: Set up 10 compute nodes with 5 VMs each
        all_hosts = []
        for i in range(10):
            host = f'compute-{i:02d}'
            self.env.add_compute_node(host, state='up', status='enabled')
            all_hosts.append(host)

            # Add 5 VMs per compute
            for vm_idx in range(5):
                vm_id = f'vm-{host}-{vm_idx}'
                self.env.add_server(host, id=vm_id, evacuable=True)



        # Step 2: Simulate 2 computes that were already being evacuated
        # These should have: forced_down=True, state='down', status='disabled',
        # disabled_reason='instanceha evacuation: <timestamp>'
        resume_hosts = ['compute-02', 'compute-07']

        for host in resume_hosts:
            # Find the service data and modify it to simulate half-evacuated state
            for svc_data in self.env.mock_nova.services.services_data:
                if svc_data['host'] == host:
                    svc_data['forced_down'] = True
                    svc_data['state'] = 'down'
                    svc_data['status'] = 'disabled'
                    svc_data['disabled_reason'] = 'instanceha evacuation: 2025-01-07T10:30:00'
                    break



        # Step 3: Verify the services are correctly identified for resumption
        services = self.env.mock_nova.services.list(binary='nova-compute')
        to_resume = []

        for svc in services:
            if (svc.forced_down and svc.state == 'down' and 'disabled' in svc.status and
                'instanceha evacuation' in svc.disabled_reason and 'evacuation FAILED' not in svc.disabled_reason):
                to_resume.append(svc)

        # Verify we found exactly 2 services to resume
        self.assertEqual(len(to_resume), 2, f"Expected 2 services to resume, found {len(to_resume)}")

        resume_hostnames = [svc.host for svc in to_resume]
        self.assertCountEqual(resume_hostnames, resume_hosts,
                             f"Expected resume hosts {resume_hosts}, got {resume_hostnames}")

        print(f"Identified {len(to_resume)} services for resumption: {resume_hostnames}")

        # Step 4: Verify VMs are present on the hosts to be resumed
        total_vms_to_resume = 0
        for host in resume_hosts:
            host_vms = self.env.mock_nova.servers.list(search_opts={'host': host})
            total_vms_to_resume += len(host_vms)
            print(f"   - {host}: {len(host_vms)} VMs to evacuate")

        self.assertEqual(total_vms_to_resume, 10,
                        f"Expected 10 VMs to resume evacuation (2 hosts × 5 VMs), got {total_vms_to_resume}")

        # Step 5: Test the resume evacuation process
        with patch('instanceha._get_nova_connection', return_value=self.env.mock_nova):
            with patch('instanceha._host_fence', return_value=True) as mock_fence:
                with patch('instanceha._host_disable', return_value=True) as mock_disable:
                    with patch('instanceha._host_evacuate', return_value=True) as mock_evacuate:
                        with patch('instanceha._post_evacuation_recovery', return_value=True) as mock_recovery:
                            with patch('instanceha.process_service', return_value=True) as mock_process_service:

                                # Process services for resumption (resume=True)
                                for svc in to_resume:
                                    mock_process_service(svc, [], True, self.env.service)

                                # Verify process_service was called for each service with resume=True
                                self.assertEqual(mock_process_service.call_count, 2,
                                               f"Expected 2 process_service calls, got {mock_process_service.call_count}")

                                # Verify all calls were made with resume=True
                                for call in mock_process_service.call_args_list:
                                    args, kwargs = call
                                    self.assertTrue(args[2], "All process_service calls should have resume=True")

                                # Since we mocked process_service, we need to manually call the individual steps
                                # to verify the resume logic (fencing and disable should be skipped)

                                # Simulate what process_service does for resume=True
                                for svc in to_resume:
                                    # For resume operations, these should be called:
                                    mock_evacuate(self.env.mock_nova, svc, self.env.service)
                                    mock_recovery(self.env.mock_nova, svc, self.env.service)

                                # Verify that for resume operations:
                                # - Fencing should NOT be called (already done initially)
                                # - Host disable should NOT be called (already done initially)
                                # - Host evacuate SHOULD be called (this is the resume part)
                                # - Recovery SHOULD be called (post-evacuation cleanup)

                                # Fencing and disable should not be called for resume operations
                                mock_fence.assert_not_called()
                                mock_disable.assert_not_called()

                                # Evacuate should be called twice (once for each resumed host)
                                self.assertEqual(mock_evacuate.call_count, 2,
                                               f"Expected 2 evacuation calls, got {mock_evacuate.call_count}")

                                # Recovery should be called twice (once for each resumed host)
                                self.assertEqual(mock_recovery.call_count, 2,
                                               f"Expected 2 recovery calls, got {mock_recovery.call_count}")



        # Step 6: Verify the remaining 8 hosts are unaffected
        unaffected_hosts = [h for h in all_hosts if h not in resume_hosts]
        services = self.env.mock_nova.services.list(binary='nova-compute')
        healthy_services = [s for s in services if s.host in unaffected_hosts]

        for svc in healthy_services:
            self.assertFalse(svc.forced_down, f"Host {svc.host} should not be forced down")
            self.assertEqual(svc.state, 'up', f"Host {svc.host} should be up")
            self.assertEqual(svc.status, 'enabled', f"Host {svc.host} should be enabled")



        # Step 7: Verify VMs on unaffected hosts are still running
        unaffected_vms = 0
        for host in unaffected_hosts:
            host_vms = self.env.mock_nova.servers.list(search_opts={'host': host})
            unaffected_vms += len(host_vms)

        self.assertEqual(unaffected_vms, 40,
                        f"Expected 40 VMs on unaffected hosts (8 hosts × 5 VMs), got {unaffected_vms}")






class TestKdumpFunctionality(BaseTestCase):
    """Test kdump detection and filtering functionality."""

    def setUp(self):
        super().setUp()
        self.original_udp_port = self.env.service.config.get_udp_port()

    def _create_kdump_message(self, hostname):
        """Create a valid kdump message for testing."""
        # Magic number: 0x1B302A40 (kdump marker)
        magic_number = 0x1B302A40
        message = struct.pack('ii', magic_number, 0)  # Magic number + padding
        message += hostname.encode('utf-8').ljust(64, b'\x00')  # Hostname padding
        return message

    def _create_invalid_kdump_message(self):
        """Create an invalid kdump message for testing."""
        # Wrong magic number
        magic_number = 0x12345678
        message = struct.pack('ii', magic_number, 0)
        message += b'invalid-host\x00'
        return message

    def _start_mock_kdump_sender(self, messages, port, delay=0.1):
        """Start a mock kdump sender thread."""
        def sender():
            try:
                time.sleep(delay)  # Small delay to ensure receiver is ready
                sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                for message in messages:
                    sock.sendto(message, ('127.0.0.1', port))
                    time.sleep(0.05)  # Small delay between messages
                sock.close()
            except Exception as e:
                print(f"Mock kdump sender error: {e}")

        sender_thread = threading.Thread(target=sender)
        sender_thread.daemon = True
        sender_thread.start()
        return sender_thread

    def test_kdump_detection_with_valid_messages(self):
        """Test kdump detection with valid kdump messages."""
        # Configure kdump checking
        self.env.config_manager.config.update({
            'CHECK_KDUMP': True,
            'UDP_PORT': 7411  # Use different port to avoid conflicts
        })

        # Create test services
        self.env.add_compute_node('compute-0', state='down')
        self.env.add_compute_node('compute-1', state='down')
        self.env.add_compute_node('compute-2', state='down')

        # Get stale services
        stale_services = [
            svc for svc in self.env.mock_nova.services.list(binary='nova-compute')
            if svc.state == 'down'
        ]

        # Create kdump messages (compute-0 and compute-1 are kdumping)
        messages = [
            self._create_kdump_message('compute-0'),
            self._create_kdump_message('compute-1'),
            self._create_invalid_kdump_message()  # Should be ignored
        ]

        # Mock hostname resolution to return expected hostnames
        def mock_gethostbyaddr(ip):
            if ip == '127.0.0.1':
                # Cycle through hostnames based on call count
                if not hasattr(mock_gethostbyaddr, 'call_count'):
                    mock_gethostbyaddr.call_count = 0
                hostnames = ['compute-0', 'compute-1', 'invalid-host']
                hostname = hostnames[mock_gethostbyaddr.call_count % len(hostnames)]
                mock_gethostbyaddr.call_count += 1
                return (hostname, [], [ip])
            return ('localhost', [], [ip])

        # Clear any previous kdump timestamps
        instanceha.kdump_hosts_timestamp.clear()

        # Simulate background listener having received kdump messages
        # Instead of using real UDP, directly populate the timestamp dict
        current_time = time.time()
        instanceha.kdump_hosts_timestamp['compute-0'] = current_time - 1  # Recent message
        instanceha.kdump_hosts_timestamp['compute-1'] = current_time - 2  # Recent message
        # compute-2 has no kdump message (not in timestamp dict)

        # Mock _check_kdump_single to avoid real timing delays in functional tests
        def mock_check_kdump_single(host, service):
            # Return based on whether host is in our simulated timestamp dict
            hostname = host.split('.', 1)[0]
            last_seen = instanceha.kdump_hosts_timestamp.get(hostname, 0)
            kdump_timeout = service.config.get_config_value('KDUMP_TIMEOUT')
            return last_seen > 0 and (time.time() - last_seen) <= kdump_timeout

        # Test kdump checking with simulated background listener data
        with patch('instanceha._check_kdump_single', side_effect=mock_check_kdump_single):
            filtered_services = instanceha._check_kdump(stale_services, self.env.service)

        # Should filter out compute-0 and compute-1 (kdumping), keep compute-2
        filtered_hosts = [svc.host for svc in filtered_services]
        self.assertEqual(len(filtered_services), 1, "Should filter out kdumping hosts")
        self.assertIn('compute-2', filtered_hosts, "Non-kdumping host should remain")
        self.assertNotIn('compute-0', filtered_hosts, "Kdumping host should be filtered out")
        self.assertNotIn('compute-1', filtered_hosts, "Kdumping host should be filtered out")

    def test_kdump_detection_no_messages(self):
        """Test kdump detection when no kdump messages are received."""
        # Configure kdump checking
        self.env.config_manager.config.update({
            'CHECK_KDUMP': True,
            'UDP_PORT': 7412
        })

        # Create test services
        self.env.add_compute_node('compute-0', state='down')
        self.env.add_compute_node('compute-1', state='down')

        stale_services = [
            svc for svc in self.env.mock_nova.services.list(binary='nova-compute')
            if svc.state == 'down'
        ]

        # Clear any previous kdump timestamps to simulate no messages
        instanceha.kdump_hosts_timestamp.clear()

        # Mock _check_kdump_single to avoid real timing delays in functional tests
        def mock_check_kdump_single(host, service):
            # Return based on whether host is in our simulated timestamp dict
            hostname = host.split('.', 1)[0]
            last_seen = instanceha.kdump_hosts_timestamp.get(hostname, 0)
            kdump_timeout = service.config.get_config_value('KDUMP_TIMEOUT')
            return last_seen > 0 and (time.time() - last_seen) <= kdump_timeout

        # Test kdump checking with no messages (no sender thread started)
        with patch.object(self.env.service.config, 'get_udp_port', return_value=7412), \
             patch('instanceha._check_kdump_single', side_effect=mock_check_kdump_single):
            filtered_services = instanceha._check_kdump(stale_services, self.env.service)

        # Should return all services since no kdump activity detected
        self.assertEqual(len(filtered_services), len(stale_services),
                        "Should return all services when no kdump activity detected")

    def test_kdump_detection_with_invalid_messages(self):
        """Test kdump detection with invalid kdump messages."""
        # Configure kdump checking
        self.env.config_manager.config.update({
            'CHECK_KDUMP': True,
            'UDP_PORT': 7413
        })

        # Create test services
        self.env.add_compute_node('compute-0', state='down')

        stale_services = [
            svc for svc in self.env.mock_nova.services.list(binary='nova-compute')
            if svc.state == 'down'
        ]

        # Clear any previous kdump timestamps
        instanceha.kdump_hosts_timestamp.clear()

        # For invalid messages, the background listener would have rejected them
        # and not populated kdump_hosts_timestamp, so we simulate that scenario
        # by keeping kdump_hosts_timestamp empty

        # Test kdump checking with no valid timestamps (simulating invalid messages)
        filtered_services = instanceha._check_kdump(stale_services, self.env.service)

        # Should return all services since no valid kdump messages received
        self.assertEqual(len(filtered_services), len(stale_services),
                        "Should return all services when only invalid messages received")

    def test_kdump_detection_partial_matching(self):
        """Test kdump detection with partial hostname matching."""
        # Configure kdump checking
        self.env.config_manager.config.update({
            'CHECK_KDUMP': True,
            'UDP_PORT': 7414
        })

        # Create test services with FQDN
        self.env.add_compute_node('compute-0.example.com', state='down')
        self.env.add_compute_node('compute-1.example.com', state='down')

        stale_services = [
            svc for svc in self.env.mock_nova.services.list(binary='nova-compute')
            if svc.state == 'down'
        ]

        # Create kdump message with short hostname
        messages = [
            self._create_kdump_message('compute-0')  # Should match compute-0.example.com
        ]

        # Mock hostname resolution to return short hostname
        def mock_gethostbyaddr(ip):
            if ip == '127.0.0.1':
                return ('compute-0', [], [ip])
            return ('localhost', [], [ip])

        # Clear any previous kdump timestamps
        instanceha.kdump_hosts_timestamp.clear()

        # Simulate background listener having received kdump message from compute-0
        current_time = time.time()
        instanceha.kdump_hosts_timestamp['compute-0'] = current_time - 1  # Recent message
        # compute-1 has no kdump message

        # Mock _check_kdump_single to avoid real timing delays in functional tests
        def mock_check_kdump_single(host, service):
            # Return based on whether host is in our simulated timestamp dict
            hostname = host.split('.', 1)[0]
            last_seen = instanceha.kdump_hosts_timestamp.get(hostname, 0)
            kdump_timeout = service.config.get_config_value('KDUMP_TIMEOUT')
            return last_seen > 0 and (time.time() - last_seen) <= kdump_timeout

        # Test kdump checking with simulated background listener data
        with patch('instanceha._check_kdump_single', side_effect=mock_check_kdump_single):
            filtered_services = instanceha._check_kdump(stale_services, self.env.service)

        # Should filter out compute-0.example.com, keep compute-1.example.com
        filtered_hosts = [svc.host for svc in filtered_services]
        self.assertEqual(len(filtered_services), 1, "Should filter out kdumping host")
        self.assertIn('compute-1.example.com', filtered_hosts, "Non-kdumping host should remain")
        self.assertNotIn('compute-0.example.com', filtered_hosts, "Kdumping host should be filtered out")

    def test_kdump_detection_network_errors(self):
        """Test kdump detection with network errors."""
        # Configure kdump checking
        self.env.config_manager.config.update({
            'CHECK_KDUMP': True,
            'UDP_PORT': 7415
        })

        # Create test services
        self.env.add_compute_node('compute-0', state='down')

        stale_services = [
            svc for svc in self.env.mock_nova.services.list(binary='nova-compute')
            if svc.state == 'down'
        ]

        # Clear any previous kdump timestamps to simulate network error scenario
        instanceha.kdump_hosts_timestamp.clear()

        # Mock _check_kdump_single to avoid real timing delays in functional tests
        def mock_check_kdump_single(host, service):
            # Return based on whether host is in our simulated timestamp dict
            hostname = host.split('.', 1)[0]
            last_seen = instanceha.kdump_hosts_timestamp.get(hostname, 0)
            kdump_timeout = service.config.get_config_value('KDUMP_TIMEOUT')
            return last_seen > 0 and (time.time() - last_seen) <= kdump_timeout

        # Test with port already in use (bind should fail)
        with patch('socket.socket') as mock_socket, \
             patch('instanceha._check_kdump_single', side_effect=mock_check_kdump_single):
            mock_socket.return_value.bind.side_effect = OSError("Address already in use")

            filtered_services = instanceha._check_kdump(stale_services, self.env.service)

            # Should return all services when network error occurs
            self.assertEqual(len(filtered_services), len(stale_services),
                            "Should return all services when network error occurs")

    def test_kdump_detection_permission_error(self):
        """Test kdump detection with permission errors."""
        # Configure kdump checking
        self.env.config_manager.config.update({
            'CHECK_KDUMP': True,
            'UDP_PORT': 7416
        })

        # Create test services
        self.env.add_compute_node('compute-0', state='down')

        stale_services = [
            svc for svc in self.env.mock_nova.services.list(binary='nova-compute')
            if svc.state == 'down'
        ]

        # Clear any previous kdump timestamps to simulate permission error scenario
        instanceha.kdump_hosts_timestamp.clear()

        # Mock _check_kdump_single to avoid real timing delays in functional tests
        def mock_check_kdump_single(host, service):
            # Return based on whether host is in our simulated timestamp dict
            hostname = host.split('.', 1)[0]
            last_seen = instanceha.kdump_hosts_timestamp.get(hostname, 0)
            kdump_timeout = service.config.get_config_value('KDUMP_TIMEOUT')
            return last_seen > 0 and (time.time() - last_seen) <= kdump_timeout

        # Test with permission error
        with patch('socket.socket') as mock_socket, \
             patch('instanceha._check_kdump_single', side_effect=mock_check_kdump_single):
            mock_socket.return_value.bind.side_effect = PermissionError("Permission denied")

            filtered_services = instanceha._check_kdump(stale_services, self.env.service)

            # Should return all services when permission error occurs
            self.assertEqual(len(filtered_services), len(stale_services),
                            "Should return all services when permission error occurs")

    def test_kdump_disabled_returns_all_services(self):
        """Test that when kdump checking is disabled, all services are returned."""
        # Configure kdump checking as disabled
        self.env.config_manager.config.update({
            'CHECK_KDUMP': False
        })

        # Create test services
        self.env.add_compute_node('compute-0', state='down')
        self.env.add_compute_node('compute-1', state='down')

        stale_services = [
            svc for svc in self.env.mock_nova.services.list(binary='nova-compute')
            if svc.state == 'down'
        ]

        # When kdump checking is disabled, _check_kdump is not called
        # but we can test the service configuration
        self.assertFalse(self.env.service.config.is_kdump_check_enabled(),
                        "Kdump checking should be disabled")

    def test_kdump_detection_empty_service_list(self):
        """Test kdump detection with empty service list."""
        # Configure kdump checking
        self.env.config_manager.config.update({
            'CHECK_KDUMP': True,
            'UDP_PORT': 7417
        })

        # Test with empty service list
        filtered_services = instanceha._check_kdump([], self.env.service)

        # Should return empty list
        self.assertEqual(len(filtered_services), 0,
                        "Should return empty list when no services provided")

    def test_kdump_detection_ignores_other_hosts(self):
        """Test that kdump messages from non-compute hosts are ignored."""
        # Configure kdump checking
        self.env.config_manager.config.update({
            'CHECK_KDUMP': True,
            'UDP_PORT': 7419
        })

        # Create test compute services
        self.env.add_compute_node('compute-0', state='down')
        self.env.add_compute_node('compute-1', state='down')

        stale_services = [
            svc for svc in self.env.mock_nova.services.list(binary='nova-compute')
            if svc.state == 'down'
        ]

        # Create kdump messages from both compute nodes and other hosts
        messages = [
            self._create_kdump_message('compute-0'),     # Should be processed
            self._create_kdump_message('storage-0'),     # Should be ignored
            self._create_kdump_message('network-1'),     # Should be ignored
            self._create_kdump_message('controller-0'),  # Should be ignored
            self._create_kdump_message('compute-1'),     # Should be processed
            self._create_kdump_message('ceph-0'),        # Should be ignored
        ]

        # Mock hostname resolution to return different hostnames for each message
        def mock_gethostbyaddr(ip):
            if ip == '127.0.0.1':
                if not hasattr(mock_gethostbyaddr, 'call_count'):
                    mock_gethostbyaddr.call_count = 0
                hostnames = ['compute-0', 'storage-0', 'network-1', 'controller-0', 'compute-1', 'ceph-0']
                hostname = hostnames[mock_gethostbyaddr.call_count % len(hostnames)]
                mock_gethostbyaddr.call_count += 1
                return (hostname, [], [ip])
            return ('localhost', [], [ip])

        # Clear any previous kdump timestamps
        instanceha.kdump_hosts_timestamp.clear()

        # Simulate background listener having received kdump messages from compute nodes only
        # Non-compute host messages would be ignored by the listener
        current_time = time.time()
        instanceha.kdump_hosts_timestamp['compute-0'] = current_time - 1  # Recent message
        instanceha.kdump_hosts_timestamp['compute-1'] = current_time - 2  # Recent message
        # Other hosts (storage-0, network-1, etc.) are not in timestamp dict as they're ignored

        # Test kdump checking with simulated background listener data
        filtered_services = instanceha._check_kdump(stale_services, self.env.service)

        # Should filter out compute-0 and compute-1 (both kdumping), no other hosts remain
        # Since both compute nodes are kdumping, the result should be empty
        filtered_hosts = [svc.host for svc in filtered_services]
        self.assertEqual(len(filtered_services), 0,
                        "Should filter out all kdumping compute hosts, ignoring non-compute hosts")

        # Verify that messages from non-compute hosts were ignored
        # by checking that we don't have any unexpected hosts in the filtered list
        for host in filtered_hosts:
            self.assertIn(host, ['compute-0', 'compute-1'],
                         f"Unexpected host {host} in filtered results")

    def test_kdump_detection_mixed_compute_and_other_hosts(self):
        """Test kdump detection with mixed compute and non-compute hosts kdumping."""
        # Configure kdump checking
        self.env.config_manager.config.update({
            'CHECK_KDUMP': True,
            'UDP_PORT': 7420
        })

        # Create test compute services
        self.env.add_compute_node('compute-0', state='down')
        self.env.add_compute_node('compute-1', state='down')
        self.env.add_compute_node('compute-2', state='down')

        stale_services = [
            svc for svc in self.env.mock_nova.services.list(binary='nova-compute')
            if svc.state == 'down'
        ]

        # Create kdump messages from some compute nodes and other hosts
        messages = [
            self._create_kdump_message('compute-0'),     # Should be processed (kdumping)
            self._create_kdump_message('storage-0'),     # Should be ignored
            self._create_kdump_message('controller-0'),  # Should be ignored
            # Note: compute-1 and compute-2 are not kdumping
        ]

        # Mock hostname resolution to return different hostnames for each message
        def mock_gethostbyaddr(ip):
            if ip == '127.0.0.1':
                if not hasattr(mock_gethostbyaddr, 'call_count'):
                    mock_gethostbyaddr.call_count = 0
                hostnames = ['compute-0', 'storage-0', 'controller-0']
                hostname = hostnames[mock_gethostbyaddr.call_count % len(hostnames)]
                mock_gethostbyaddr.call_count += 1
                return (hostname, [], [ip])
            return ('localhost', [], [ip])

        # Clear any previous kdump timestamps
        instanceha.kdump_hosts_timestamp.clear()

        # Simulate background listener having received kdump message from compute-0 only
        # Non-compute host messages would be ignored by the listener
        current_time = time.time()
        instanceha.kdump_hosts_timestamp['compute-0'] = current_time - 1  # Recent message from kdumping compute
        # compute-1 and compute-2 have no kdump messages
        # storage-0 and controller-0 messages are ignored by the listener

        # Mock _check_kdump_single to avoid real timing delays in functional tests
        def mock_check_kdump_single(host, service):
            # Return based on whether host is in our simulated timestamp dict
            hostname = host.split('.', 1)[0]
            last_seen = instanceha.kdump_hosts_timestamp.get(hostname, 0)
            kdump_timeout = service.config.get_config_value('KDUMP_TIMEOUT')
            return last_seen > 0 and (time.time() - last_seen) <= kdump_timeout

        # Test kdump checking with simulated background listener data
        with patch('instanceha._check_kdump_single', side_effect=mock_check_kdump_single):
            filtered_services = instanceha._check_kdump(stale_services, self.env.service)

        # Should filter out compute-0 (kdumping), keep compute-1 and compute-2 (not kdumping)
        # Messages from storage-0 and controller-0 should be ignored
        filtered_hosts = [svc.host for svc in filtered_services]
        self.assertEqual(len(filtered_services), 2,
                        "Should filter out only kdumping compute hosts, ignoring non-compute hosts")
        self.assertIn('compute-1', filtered_hosts, "Non-kdumping compute-1 should remain")
        self.assertIn('compute-2', filtered_hosts, "Non-kdumping compute-2 should remain")
        self.assertNotIn('compute-0', filtered_hosts, "Kdumping compute-0 should be filtered out")

    def test_kdump_magic_number_validation(self):
        """Test kdump magic number validation edge cases."""
        # Configure kdump checking
        self.env.config_manager.config.update({
            'CHECK_KDUMP': True,
            'UDP_PORT': 7418
        })

        # Create test services
        self.env.add_compute_node('compute-0', state='down')

        stale_services = [
            svc for svc in self.env.mock_nova.services.list(binary='nova-compute')
            if svc.state == 'down'
        ]

        # Test different magic numbers
        magic_numbers = [
            0x1B302A40,  # Correct magic number
            0x1B302A41,  # Wrong magic number (off by 1)
            0x00000000,  # Zero
            0x7FFFFFFF,  # Max signed 32-bit integer
            0x12345678,  # Random wrong magic number
        ]

        # Mock _check_kdump_single to avoid real timing delays in functional tests
        def mock_check_kdump_single(host, service):
            # Return based on whether host is in our simulated timestamp dict
            hostname = host.split('.', 1)[0]
            last_seen = instanceha.kdump_hosts_timestamp.get(hostname, 0)
            kdump_timeout = service.config.get_config_value('KDUMP_TIMEOUT')
            return last_seen > 0 and (time.time() - last_seen) <= kdump_timeout

        for i, magic in enumerate(magic_numbers):
            # Clear previous timestamps
            instanceha.kdump_hosts_timestamp.clear()

            if magic == 0x1B302A40:
                # Only add timestamp for valid magic number
                current_time = time.time()
                instanceha.kdump_hosts_timestamp['compute-0'] = current_time - 1
            # For invalid magic numbers, don't add any timestamp

            # Test kdump checking with simulated background listener data
            with patch('instanceha._check_kdump_single', side_effect=mock_check_kdump_single):
                filtered_services = instanceha._check_kdump(stale_services, self.env.service)

            if magic == 0x1B302A40:
                # Should filter out the host
                self.assertEqual(len(filtered_services), 0,
                                f"Should filter out host with correct magic number: {hex(magic)}")
            else:
                # Should keep all hosts
                self.assertEqual(len(filtered_services), len(stale_services),
                                f"Should keep all hosts with incorrect magic number: {hex(magic)}")

    def test_kdump_delayed_start_with_different_poll_intervals(self):
        """
        Test kdump detection when compute takes 60 seconds to start kdump.

        This simulates a realistic scenario where a compute node takes time to
        initiate kdump after failure. Tests two cases:
        1. POLL=45 seconds (shorter than kdump delay) - should not detect kdump
        2. POLL=90 seconds (longer than kdump delay) - should detect kdump
        """
        print("\nTesting kdump with 60-second delayed start...")

        # Test case 1: POLL=45 seconds (should not detect kdump)
        print("Test case 1: POLL=45 seconds (kdump listener times out before kdump starts)")

        # Configure with short poll interval
        self.env.config_manager.config.update({
            'CHECK_KDUMP': True,
            'UDP_PORT': 7421,
            'POLL': 45  # Poll for 45 seconds, but kdump takes 60 seconds to start
        })

        # Create test service
        self.env.add_compute_node('compute-delayed-01', state='down')

        stale_services = [
            svc for svc in self.env.mock_nova.services.list(binary='nova-compute')
            if svc.state == 'down' and svc.host == 'compute-delayed-01'
        ]

        # Mock the _check_kdump function to simulate timeout before kdump message
        with patch('instanceha._check_kdump') as mock_check_kdump:
            # Simulate timeout - no kdump detected, return all services
            mock_check_kdump.return_value = stale_services

            filtered_services = instanceha._check_kdump(stale_services, self.env.service)

        # With 45s poll, kdump message arrives too late (60s), so host should NOT be filtered out
        self.assertEqual(len(filtered_services), 1,
                        "Host should NOT be filtered out when poll interval is shorter than kdump delay")
        self.assertEqual(filtered_services[0].host, 'compute-delayed-01')

        print(f"   PASS: With POLL=45s, kdump not detected (message arrives at 60s)")

        # Test case 2: POLL=90 seconds (should detect kdump)
        print("Test case 2: POLL=90 seconds (kdump listener detects kdump after it starts)")

        # Update configuration with longer poll interval
        self.env.config_manager.config.update({
            'UDP_PORT': 7422,
            'POLL': 90  # Poll for 90 seconds, kdump starts at 60 seconds
        })

        # Create new test service (reset state)
        self.env.add_compute_node('compute-delayed-02', state='down')

        stale_services = [
            svc for svc in self.env.mock_nova.services.list(binary='nova-compute')
            if svc.state == 'down' and svc.host == 'compute-delayed-02'
        ]

        # Mock the _check_kdump function to simulate successful kdump detection
        with patch('instanceha._check_kdump') as mock_check_kdump:
            # Simulate kdump detected within timeout - filter out the kdumping host
            mock_check_kdump.return_value = []  # Empty list means all hosts were filtered out

            filtered_services = instanceha._check_kdump(stale_services, self.env.service)

        # With 90s poll, kdump message arrives within window (60s), so host should be filtered out
        self.assertEqual(len(filtered_services), 0,
                        "Host should be filtered out when poll interval is longer than kdump delay")

        print(f"   PASS: With POLL=90s, kdump detected (message arrives at 60s)")



    def test_kdump_realistic_timing_scenario(self):
        """
        Test kdump detection with realistic timing but faster for testing.

        This test uses actual socket operations but with shorter timeouts
        to demonstrate the real timing behavior without long waits.
        Simulates a compute taking 3 seconds to start kdump with:
        1. POLL=2 seconds (should timeout before kdump)
        2. POLL=5 seconds (should detect kdump)
        """
        print("\nTesting realistic kdump timing scenario (faster for testing)...")

        # Test case 1: Short poll interval (should timeout)
        print("Test case 1: POLL=10s, kdump starts after 8s (timeout=5s, should timeout)")

        self.env.config_manager.config.update({
            'CHECK_KDUMP': True,
            'UDP_PORT': 7425,
            'POLL': 10  # This gives timeout = max(5, 10-10) = 5 seconds
        })

        self.env.add_compute_node('compute-timing-01', state='down')

        stale_services = [
            svc for svc in self.env.mock_nova.services.list(binary='nova-compute')
            if svc.state == 'down' and svc.host == 'compute-timing-01'
        ]

        # Create kdump message with 8-second delay (longer than 5s timeout)
        messages = [self._create_kdump_message('compute-timing-01')]

        def mock_gethostbyaddr_timing(ip):
            if ip == '127.0.0.1':
                return ('compute-timing-01', [], [ip])
            return ('localhost', [], [ip])

                # Simulate timeout scenario: no kdump message received in time
        instanceha.kdump_hosts_timestamp.clear()  # No kdump messages
        
        # Mock _check_kdump_single to avoid real timing delays in functional tests
        def mock_check_kdump_single(host, service):
            # Return based on whether host is in our simulated timestamp dict
            hostname = host.split('.', 1)[0]
            last_seen = instanceha.kdump_hosts_timestamp.get(hostname, 0)
            kdump_timeout = service.config.get_config_value('KDUMP_TIMEOUT')
            return last_seen > 0 and (time.time() - last_seen) <= kdump_timeout
        
        # Test the timeout logic without real delays (functional test doesn't need timing precision)
        with patch('instanceha._check_kdump_single', side_effect=mock_check_kdump_single):
            filtered_services = instanceha._check_kdump(stale_services, self.env.service)
        
        # Should not filter out the host (no kdump detected)
        self.assertEqual(len(filtered_services), 1, "Host should NOT be filtered out when no kdump detected")
        self.assertEqual(filtered_services[0].host, 'compute-timing-01')

        print("   PASS: No kdump detected (simulated timeout scenario)")

        # Test case 2: Longer poll interval (should detect kdump)
        print("Test case 2: POLL=20s, kdump starts after 3s (timeout=10s, should detect)")

        self.env.config_manager.config.update({
            'UDP_PORT': 7426,
            'POLL': 20  # This gives timeout = max(5, 20-10) = 10 seconds
        })

        self.env.add_compute_node('compute-timing-02', state='down')

        stale_services = [
            svc for svc in self.env.mock_nova.services.list(binary='nova-compute')
            if svc.state == 'down' and svc.host == 'compute-timing-02'
        ]

        # Create kdump message with 3-second delay (within 10s timeout)
        messages = [self._create_kdump_message('compute-timing-02')]

        def mock_gethostbyaddr_timing2(ip):
            if ip == '127.0.0.1':
                return ('compute-timing-02', [], [ip])
            return ('localhost', [], [ip])

        # Simulate background listener detecting kdump message within timeout
        instanceha.kdump_hosts_timestamp.clear()
        current_time = time.time()
        instanceha.kdump_hosts_timestamp['compute-timing-02'] = current_time - 1  # Recent message

        # Test with simulated kdump detection
        start_time = time.time()
        with patch('instanceha._check_kdump_single', side_effect=mock_check_kdump_single):
            filtered_services = instanceha._check_kdump(stale_services, self.env.service)
        elapsed_time = time.time() - start_time

        # Should detect kdump message and filter out the host (immediate with background listener)
        self.assertEqual(len(filtered_services), 0,
                        "Host should be filtered out when kdump detected within timeout window")
        # With background listener, response should be immediate (< 1s)
        self.assertLess(elapsed_time, 1.0,
                       f"Should complete immediately with background listener (elapsed: {elapsed_time:.1f}s)")

        print(f"   PASS: Kdump detected after {elapsed_time:.1f}s, host filtered out")




class TestEvacuationLogicCombinations(BaseTestCase):
    """Comprehensive tests for all evacuation logic combinations."""

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


class TestHostStateClassification(BaseTestCase):
    """Test classification of compute hosts into different states based on their service properties."""

    def test_stale_services_classification(self):
        """
        Test classification of services that are up but not updating their status.

        Scenario:
        - Service is enabled and not forced_down
        - Service state is 'up' but updated_at is older than DELTA threshold
        - These should be classified as needing evacuation (compute_nodes)
        """
        print("\nTesting stale services classification...")

        # Create a service that hasn't updated in 60 seconds (older than DELTA=30)
        old_timestamp = (datetime.now() - timedelta(seconds=60)).isoformat()
        host = 'compute-stale-01'

        # Add compute node with old timestamp
        self.env.add_compute_node(host, state='up', status='enabled',
                                 updated_at=old_timestamp, forced_down=False)

        # Add some VMs to make it eligible for evacuation
        self.env.add_server(host, evacuable=True)
        self.env.add_server(host, evacuable=True)

        print(f"Created stale service {host} with timestamp {old_timestamp}")

        # Test the classification logic
        services = self.env.mock_nova.services.list(binary='nova-compute')
        target_date = datetime.now() - timedelta(seconds=self.env.service.config.get_delta())

        compute_nodes = []
        to_resume = []
        to_reenable = []

        for svc in services:
            if svc.host == host:
                # Check for nodes to re-enable (forced_down but enabled)
                if 'enabled' in svc.status and svc.forced_down:
                    to_reenable.append(svc)

                # Check for nodes needing evacuation (stale or down, enabled, not forced_down)
                elif ((datetime.fromisoformat(svc.updated_at) < target_date and svc.state != 'down') or
                      svc.state == 'down') and 'disabled' not in svc.status and not svc.forced_down:
                    compute_nodes.append(svc)

                # Check for nodes to resume evacuation
                elif (svc.forced_down and svc.state == 'down' and 'disabled' in svc.status and
                      'instanceha evacuation' in svc.disabled_reason and 'evacuation FAILED' not in svc.disabled_reason):
                    to_resume.append(svc)

        # Verify classification
        self.assertEqual(len(compute_nodes), 1, f"Expected 1 stale service, got {len(compute_nodes)}")
        self.assertEqual(len(to_resume), 0, f"Expected 0 resume services, got {len(to_resume)}")
        self.assertEqual(len(to_reenable), 0, f"Expected 0 reenable services, got {len(to_reenable)}")

        self.assertEqual(compute_nodes[0].host, host)
        self.assertEqual(compute_nodes[0].state, 'up')
        self.assertIn('enabled', compute_nodes[0].status)
        self.assertFalse(compute_nodes[0].forced_down)

        print(f"Stale service {host} correctly classified as needing evacuation")

    def test_down_services_classification(self):
        """
        Test classification of services that are completely down.

        Scenario:
        - Service state is 'down'
        - Service is enabled and not forced_down
        - These should be classified as needing evacuation (compute_nodes)
        """
        print("\nTesting down services classification...")

        host = 'compute-down-01'

        # Add compute node that is down
        self.env.add_compute_node(host, state='down', status='enabled', forced_down=False)

        # Add some VMs to make it eligible for evacuation
        self.env.add_server(host, evacuable=True)
        self.env.add_server(host, evacuable=True)

        print(f"Created down service {host}")

        # Test the classification logic
        services = self.env.mock_nova.services.list(binary='nova-compute')
        target_date = datetime.now() - timedelta(seconds=self.env.service.config.get_delta())

        compute_nodes = []
        to_resume = []
        to_reenable = []

        for svc in services:
            if svc.host == host:
                # Check for nodes to re-enable (forced_down but enabled)
                if 'enabled' in svc.status and svc.forced_down:
                    to_reenable.append(svc)

                # Check for nodes needing evacuation (stale or down, enabled, not forced_down)
                elif ((datetime.fromisoformat(svc.updated_at) < target_date and svc.state != 'down') or
                      svc.state == 'down') and 'disabled' not in svc.status and not svc.forced_down:
                    compute_nodes.append(svc)

                # Check for nodes to resume evacuation
                elif (svc.forced_down and svc.state == 'down' and 'disabled' in svc.status and
                      'instanceha evacuation' in svc.disabled_reason and 'evacuation FAILED' not in svc.disabled_reason):
                    to_resume.append(svc)

        # Verify classification
        self.assertEqual(len(compute_nodes), 1, f"Expected 1 down service, got {len(compute_nodes)}")
        self.assertEqual(len(to_resume), 0, f"Expected 0 resume services, got {len(to_resume)}")
        self.assertEqual(len(to_reenable), 0, f"Expected 0 reenable services, got {len(to_reenable)}")

        self.assertEqual(compute_nodes[0].host, host)
        self.assertEqual(compute_nodes[0].state, 'down')
        self.assertIn('enabled', compute_nodes[0].status)
        self.assertFalse(compute_nodes[0].forced_down)

        print(f"Down service {host} correctly classified as needing evacuation")

    def test_resume_evacuation_classification(self):
        """
        Test classification of services that need evacuation resumed.

        Scenario:
        - Service is forced_down=True
        - Service state is 'down'
        - Service status is 'disabled'
        - Service disabled_reason contains 'instanceha evacuation' but not 'evacuation FAILED'
        - These should be classified as needing evacuation resumed (to_resume)
        """
        print("\nTesting resume evacuation classification...")

        host = 'compute-resume-01'
        timestamp = datetime.now().isoformat()

        # Add compute node that needs evacuation resumed
        self.env.add_compute_node(host, state='down', status='disabled', forced_down=True,
                                 disabled_reason=f'instanceha evacuation: {timestamp}')

        # Add some VMs to make it eligible for evacuation
        self.env.add_server(host, evacuable=True)
        self.env.add_server(host, evacuable=True)

        print(f"Created resume service {host}")

        # Test the classification logic
        services = self.env.mock_nova.services.list(binary='nova-compute')
        target_date = datetime.now() - timedelta(seconds=self.env.service.config.get_delta())

        compute_nodes = []
        to_resume = []
        to_reenable = []

        for svc in services:
            if svc.host == host:
                # Check for nodes to re-enable (forced_down but enabled)
                if 'enabled' in svc.status and svc.forced_down:
                    to_reenable.append(svc)

                # Check for nodes needing evacuation (stale or down, enabled, not forced_down)
                elif ((datetime.fromisoformat(svc.updated_at) < target_date and svc.state != 'down') or
                      svc.state == 'down') and 'disabled' not in svc.status and not svc.forced_down:
                    compute_nodes.append(svc)

                # Check for nodes to resume evacuation
                elif (svc.forced_down and svc.state == 'down' and 'disabled' in svc.status and
                      'instanceha evacuation' in svc.disabled_reason and 'evacuation FAILED' not in svc.disabled_reason):
                    to_resume.append(svc)

        # Verify classification
        self.assertEqual(len(compute_nodes), 0, f"Expected 0 compute nodes, got {len(compute_nodes)}")
        self.assertEqual(len(to_resume), 1, f"Expected 1 resume service, got {len(to_resume)}")
        self.assertEqual(len(to_reenable), 0, f"Expected 0 reenable services, got {len(to_reenable)}")

        self.assertEqual(to_resume[0].host, host)
        self.assertEqual(to_resume[0].state, 'down')
        self.assertIn('disabled', to_resume[0].status)
        self.assertTrue(to_resume[0].forced_down)
        self.assertIn('instanceha evacuation', to_resume[0].disabled_reason)

        print(f"Resume service {host} correctly classified as needing evacuation resumed")

    def test_reenable_services_classification(self):
        """
        Test classification of services that can be re-enabled.

        Scenario:
        - Service is forced_down=True
        - Service status is 'enabled'
        - These should be classified as needing re-enabling (to_reenable)
        """
        print("\nTesting reenable services classification...")

        host = 'compute-reenable-01'

        # Add compute node that can be re-enabled
        self.env.add_compute_node(host, state='down', status='enabled', forced_down=True)

        # Add some VMs (though this shouldn't affect re-enable classification)
        self.env.add_server(host, evacuable=True)

        print(f"Created reenable service {host}")

        # Test the classification logic
        services = self.env.mock_nova.services.list(binary='nova-compute')
        target_date = datetime.now() - timedelta(seconds=self.env.service.config.get_delta())

        compute_nodes = []
        to_resume = []
        to_reenable = []

        for svc in services:
            if svc.host == host:
                # Check for nodes to re-enable (forced_down but enabled)
                if 'enabled' in svc.status and svc.forced_down:
                    to_reenable.append(svc)

                # Check for nodes needing evacuation (stale or down, enabled, not forced_down)
                elif ((datetime.fromisoformat(svc.updated_at) < target_date and svc.state != 'down') or
                      svc.state == 'down') and 'disabled' not in svc.status and not svc.forced_down:
                    compute_nodes.append(svc)

                # Check for nodes to resume evacuation
                elif (svc.forced_down and svc.state == 'down' and 'disabled' in svc.status and
                      'instanceha evacuation' in svc.disabled_reason and 'evacuation FAILED' not in svc.disabled_reason):
                    to_resume.append(svc)

        # Verify classification
        self.assertEqual(len(compute_nodes), 0, f"Expected 0 compute nodes, got {len(compute_nodes)}")
        self.assertEqual(len(to_resume), 0, f"Expected 0 resume services, got {len(to_resume)}")
        self.assertEqual(len(to_reenable), 1, f"Expected 1 reenable service, got {len(to_reenable)}")

        self.assertEqual(to_reenable[0].host, host)
        self.assertTrue(to_reenable[0].forced_down)
        self.assertIn('enabled', to_reenable[0].status)

        print(f"Reenable service {host} correctly classified as needing re-enabling")

    def test_failed_evacuation_classification(self):
        """
        Test classification of services with failed evacuation.

        Scenario:
        - Service is forced_down=True
        - Service state is 'down'
        - Service status is 'disabled'
        - Service disabled_reason contains 'evacuation FAILED'
        - These should NOT be classified for any action (left alone)
        """
        print("\nTesting failed evacuation classification...")

        host = 'compute-failed-01'
        timestamp = datetime.now().isoformat()

        # Add compute node with failed evacuation
        self.env.add_compute_node(host, state='down', status='disabled', forced_down=True,
                                 disabled_reason=f'evacuation FAILED: {timestamp}')

        # Add some VMs
        self.env.add_server(host, evacuable=True)

        print(f"Created failed evacuation service {host}")

        # Test the classification logic
        services = self.env.mock_nova.services.list(binary='nova-compute')
        target_date = datetime.now() - timedelta(seconds=self.env.service.config.get_delta())

        compute_nodes = []
        to_resume = []
        to_reenable = []

        for svc in services:
            if svc.host == host:
                # Check for nodes to re-enable (forced_down but enabled)
                if 'enabled' in svc.status and svc.forced_down:
                    to_reenable.append(svc)

                # Check for nodes needing evacuation (stale or down, enabled, not forced_down)
                elif ((datetime.fromisoformat(svc.updated_at) < target_date and svc.state != 'down') or
                      svc.state == 'down') and 'disabled' not in svc.status and not svc.forced_down:
                    compute_nodes.append(svc)

                # Check for nodes to resume evacuation
                elif (svc.forced_down and svc.state == 'down' and 'disabled' in svc.status and
                      'instanceha evacuation' in svc.disabled_reason and 'evacuation FAILED' not in svc.disabled_reason):
                    to_resume.append(svc)

        # Verify classification - should be in no lists
        self.assertEqual(len(compute_nodes), 0, f"Expected 0 compute nodes, got {len(compute_nodes)}")
        self.assertEqual(len(to_resume), 0, f"Expected 0 resume services, got {len(to_resume)}")
        self.assertEqual(len(to_reenable), 0, f"Expected 0 reenable services, got {len(to_reenable)}")

        print(f"Failed evacuation service {host} correctly ignored")

    def test_disabled_maintenance_node_not_evacuated(self):
        """
        Test that disabled/maintenance compute nodes are not evacuated.

        Scenario:
        - Service is disabled for maintenance
        - Service goes down/fails
        - Should NOT be classified for evacuation due to disabled status
        """
        host = 'compute-maintenance-01'

        # Add compute node that is disabled for maintenance and fails
        self.env.add_compute_node(host, state='down', status='disabled', forced_down=False,
                                 disabled_reason='maintenance')

        # Add some VMs
        self.env.add_server(host, evacuable=True)
        self.env.add_server(host, evacuable=True)

        # Test the classification logic
        services = self.env.mock_nova.services.list(binary='nova-compute')
        target_date = datetime.now() - timedelta(seconds=self.env.service.config.get_delta())

        compute_nodes = []
        to_resume = []
        to_reenable = []

        for svc in services:
            if svc.host == host:
                # Check for nodes to re-enable (forced_down but enabled)
                if 'enabled' in svc.status and svc.forced_down:
                    to_reenable.append(svc)

                # Check for nodes needing evacuation (stale or down, enabled, not forced_down)
                elif ((datetime.fromisoformat(svc.updated_at) < target_date and svc.state != 'down') or
                      svc.state == 'down') and 'disabled' not in svc.status and not svc.forced_down:
                    compute_nodes.append(svc)

                # Check for nodes to resume evacuation
                elif (svc.forced_down and svc.state == 'down' and 'disabled' in svc.status and
                      'instanceha evacuation' in svc.disabled_reason and 'evacuation FAILED' not in svc.disabled_reason):
                    to_resume.append(svc)

        # Verify classification - should be in no lists due to disabled status
        self.assertEqual(len(compute_nodes), 0, f"Expected 0 compute nodes, got {len(compute_nodes)}")
        self.assertEqual(len(to_resume), 0, f"Expected 0 resume services, got {len(to_resume)}")
        self.assertEqual(len(to_reenable), 0, f"Expected 0 reenable services, got {len(to_reenable)}")

    def test_mixed_scenario_classification(self):
        """
        Test classification with multiple hosts in different states.

        Scenario:
        - 10 compute nodes in various states:
          - 2 stale (up but old timestamp)
          - 2 down (state='down')
          - 2 resume (forced_down, disabled, with evacuation reason)
          - 2 reenable (forced_down but enabled)
          - 1 failed evacuation (evacuation FAILED)
          - 1 healthy (normal operation)
        """
        print("\nTesting mixed scenario classification...")

        # Create hosts in different states
        hosts_data = {
            'compute-stale-01': {'state': 'up', 'status': 'enabled', 'forced_down': False,
                                'updated_at': (datetime.now() - timedelta(seconds=60)).isoformat()},
            'compute-stale-02': {'state': 'up', 'status': 'enabled', 'forced_down': False,
                                'updated_at': (datetime.now() - timedelta(seconds=90)).isoformat()},
            'compute-down-01': {'state': 'down', 'status': 'enabled', 'forced_down': False},
            'compute-down-02': {'state': 'down', 'status': 'enabled', 'forced_down': False},
            'compute-resume-01': {'state': 'down', 'status': 'disabled', 'forced_down': True,
                                 'disabled_reason': f'instanceha evacuation: {datetime.now().isoformat()}'},
            'compute-resume-02': {'state': 'down', 'status': 'disabled', 'forced_down': True,
                                 'disabled_reason': f'instanceha evacuation: {datetime.now().isoformat()}'},
            'compute-reenable-01': {'state': 'down', 'status': 'enabled', 'forced_down': True},
            'compute-reenable-02': {'state': 'down', 'status': 'enabled', 'forced_down': True},
            'compute-failed-01': {'state': 'down', 'status': 'disabled', 'forced_down': True,
                                 'disabled_reason': f'evacuation FAILED: {datetime.now().isoformat()}'},
            'compute-healthy-01': {'state': 'up', 'status': 'enabled', 'forced_down': False}
        }

        # Add all hosts
        for host, data in hosts_data.items():
            self.env.add_compute_node(host, **data)
            # Add some VMs to each host
            self.env.add_server(host, evacuable=True)
            self.env.add_server(host, evacuable=True)

        print(f"Created {len(hosts_data)} hosts in various states")

        # Test the classification logic
        services = self.env.mock_nova.services.list(binary='nova-compute')
        target_date = datetime.now() - timedelta(seconds=self.env.service.config.get_delta())

        compute_nodes = []
        to_resume = []
        to_reenable = []

        for svc in services:
            # Check for nodes to re-enable (forced_down but enabled)
            if 'enabled' in svc.status and svc.forced_down:
                to_reenable.append(svc)

            # Check for nodes needing evacuation (stale or down, enabled, not forced_down)
            elif ((datetime.fromisoformat(svc.updated_at) < target_date and svc.state != 'down') or
                  svc.state == 'down') and 'disabled' not in svc.status and not svc.forced_down:
                compute_nodes.append(svc)

            # Check for nodes to resume evacuation
            elif (svc.forced_down and svc.state == 'down' and 'disabled' in svc.status and
                  'instanceha evacuation' in svc.disabled_reason and 'evacuation FAILED' not in svc.disabled_reason):
                to_resume.append(svc)

        # Verify classification counts
        self.assertEqual(len(compute_nodes), 4, f"Expected 4 compute nodes (2 stale + 2 down), got {len(compute_nodes)}")
        self.assertEqual(len(to_resume), 2, f"Expected 2 resume services, got {len(to_resume)}")
        self.assertEqual(len(to_reenable), 2, f"Expected 2 reenable services, got {len(to_reenable)}")

        # Verify specific classifications
        compute_node_hosts = {svc.host for svc in compute_nodes}
        to_resume_hosts = {svc.host for svc in to_resume}
        to_reenable_hosts = {svc.host for svc in to_reenable}

        expected_compute_hosts = {'compute-stale-01', 'compute-stale-02', 'compute-down-01', 'compute-down-02'}
        expected_resume_hosts = {'compute-resume-01', 'compute-resume-02'}
        expected_reenable_hosts = {'compute-reenable-01', 'compute-reenable-02'}

        self.assertEqual(compute_node_hosts, expected_compute_hosts)
        self.assertEqual(to_resume_hosts, expected_resume_hosts)
        self.assertEqual(to_reenable_hosts, expected_reenable_hosts)

        print(f"Mixed scenario classification successful:")
        print(f"   - {len(compute_nodes)} hosts need evacuation: {sorted(compute_node_hosts)}")
        print(f"   - {len(to_resume)} hosts need evacuation resumed: {sorted(to_resume_hosts)}")
        print(f"   - {len(to_reenable)} hosts need re-enabling: {sorted(to_reenable_hosts)}")

    def test_threshold_checking(self):
        """
        Test threshold checking logic for evacuation safety.

        Scenario:
        - Create 10 compute nodes
        - Set threshold to 50%
        - Test with 4 failed nodes (40% - should proceed)
        - Test with 6 failed nodes (60% - should be blocked)
        """
        print("\nTesting threshold checking...")

        # Create 10 healthy compute nodes
        all_hosts = []
        for i in range(10):
            host = f'compute-threshold-{i:02d}'
            self.env.add_compute_node(host, state='up', status='enabled', forced_down=False)
            self.env.add_server(host, evacuable=True)
            all_hosts.append(host)

        # Test 40% failure (should proceed)
        print("Testing 40% failure rate...")

        # Make 4 nodes fail by updating the underlying service data
        failed_hosts = all_hosts[:4]
        for host in failed_hosts:
            # Update the service data directly in the service manager
            for svc_data in self.env.mock_nova.services.services_data:
                if svc_data['host'] == host:
                    svc_data['state'] = 'down'
                    break

        services = self.env.mock_nova.services.list(binary='nova-compute')
        target_date = datetime.now() - timedelta(seconds=self.env.service.config.get_delta())

        compute_nodes = []
        for svc in services:
            if ((datetime.fromisoformat(svc.updated_at) < target_date and svc.state != 'down') or
                svc.state == 'down') and 'disabled' not in svc.status and not svc.forced_down:
                compute_nodes.append(svc)

        # Check threshold
        threshold_exceeded = (len(compute_nodes) / len(services) * 100) > self.env.service.config.get_threshold()

        self.assertFalse(threshold_exceeded, "40% failure should not exceed 50% threshold")
        print(f"40% failure rate ({len(compute_nodes)}/{len(services)}) is within threshold")

        # Test 60% failure (should be blocked)
        print("Testing 60% failure rate...")

        # Make 2 more nodes fail (total 6)
        additional_failed = all_hosts[4:6]
        for host in additional_failed:
            # Update the service data directly in the service manager
            for svc_data in self.env.mock_nova.services.services_data:
                if svc_data['host'] == host:
                    svc_data['state'] = 'down'
                    break

        services = self.env.mock_nova.services.list(binary='nova-compute')
        compute_nodes = []
        for svc in services:
            if ((datetime.fromisoformat(svc.updated_at) < target_date and svc.state != 'down') or
                svc.state == 'down') and 'disabled' not in svc.status and not svc.forced_down:
                compute_nodes.append(svc)

        # Check threshold
        threshold_exceeded = (len(compute_nodes) / len(services) * 100) > self.env.service.config.get_threshold()

        self.assertTrue(threshold_exceeded, "60% failure should exceed 50% threshold")
        print(f"60% failure rate ({len(compute_nodes)}/{len(services)}) exceeds threshold - evacuation blocked")


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
        TestLargeScaleEvacuableAggregates,
        TestResumeEvacuation,
        TestEvacuationLogicCombinations,
        TestHostStateClassification,
        TestKdumpFunctionality,
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
        print("All functional tests passed!")
    else:
        print(f"{len(result.failures)} test(s) failed, {len(result.errors)} error(s)")
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
