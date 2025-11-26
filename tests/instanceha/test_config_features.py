#!/usr/bin/env python3
"""
Configuration feature tests for InstanceHA.

Tests configuration parameters from lines 461-483 that control
service behavior and ensure all features are properly tested.
"""

import os
import sys
import unittest
from unittest.mock import Mock, patch, MagicMock
from datetime import datetime, timedelta

# Mock OpenStack dependencies
if 'novaclient' not in sys.modules:
    sys.modules['novaclient'] = MagicMock()
    sys.modules['novaclient.client'] = MagicMock()
    class NotFound(Exception):
        pass
    class Conflict(Exception):
        pass
    novaclient_exceptions = MagicMock()
    novaclient_exceptions.NotFound = NotFound
    novaclient_exceptions.Conflict = Conflict
    sys.modules['novaclient.exceptions'] = novaclient_exceptions

if 'keystoneauth1' not in sys.modules:
    sys.modules['keystoneauth1'] = MagicMock()
    sys.modules['keystoneauth1.loading'] = MagicMock()
    sys.modules['keystoneauth1.session'] = MagicMock()
    sys.modules['keystoneauth1.exceptions'] = MagicMock()
    sys.modules['keystoneauth1.exceptions.discovery'] = MagicMock()

# Add module path
test_dir = os.path.dirname(os.path.abspath(__file__))
instanceha_path = os.path.join(test_dir, '../../templates/instanceha/bin/')
sys.path.insert(0, os.path.abspath(instanceha_path))

import instanceha


class TestDisabledConfig(unittest.TestCase):
    """Test DISABLED configuration parameter (line 478, used at line 2567-2568)."""

    def test_disabled_true_skips_evacuation(self):
        """Test that DISABLED=True skips all evacuations."""
        mock_conn = Mock()
        mock_service = Mock()
        mock_service.config = Mock()
        mock_service.config.is_disabled.return_value = True
        mock_service.config.get_threshold.return_value = 50
        mock_service.processing_lock = Mock()
        mock_service.hosts_processing = {}

        mock_failed_service = Mock()
        mock_failed_service.host = 'test-host.example.com'

        # Create mock services list with one down service
        services = [mock_failed_service, Mock(), Mock()]  # Add more to avoid threshold
        compute_nodes = [mock_failed_service]

        with patch('instanceha._filter_processing_hosts', return_value=(compute_nodes, [], set(['test-host']), 0)):
            with patch('instanceha._prepare_evacuation_resources', return_value=(compute_nodes, [], [], [])):
                with patch('instanceha._cleanup_filtered_hosts'):
                    with patch('instanceha.process_service') as mock_process:
                        instanceha._process_stale_services(
                            mock_conn,
                            mock_service,
                            services,
                            compute_nodes,
                            []
                        )

        # Should NOT process any services when disabled
        mock_process.assert_not_called()

    def test_disabled_false_processes_evacuation(self):
        """Test that DISABLED=False processes evacuations normally."""
        mock_conn = Mock()
        mock_service = Mock()
        mock_service.config = Mock()
        mock_service.config.is_disabled.return_value = False
        mock_service.config.is_kdump_check_enabled.return_value = False
        mock_service.config.get_threshold.return_value = 50
        mock_service.config.is_tagged_images_enabled.return_value = False
        mock_service.config.is_tagged_flavors_enabled.return_value = False
        mock_service.config.is_tagged_aggregates_enabled.return_value = False
        mock_service.config.get_poll_interval.return_value = 45
        mock_service.processing_lock = Mock()
        mock_service.hosts_processing = {}

        mock_failed_service = Mock()
        mock_failed_service.host = 'test-host.example.com'

        services = [mock_failed_service, Mock(), Mock()]  # Add more to avoid threshold
        compute_nodes = [mock_failed_service]

        with patch('instanceha._filter_processing_hosts', return_value=(compute_nodes, [], set(['test-host']), 0)):
            with patch('instanceha._prepare_evacuation_resources', return_value=(compute_nodes, [], [], [])):
                with patch('instanceha._cleanup_filtered_hosts'):
                    with patch('instanceha.process_service', return_value=True) as mock_process:
                        instanceha._process_stale_services(
                            mock_conn,
                            mock_service,
                            services,
                            compute_nodes,
                            []
                        )

        # Should process services when not disabled
        mock_process.assert_called()


class TestForceEnableConfig(unittest.TestCase):
    """Test FORCE_ENABLE configuration parameter (line 475, used at line 2615)."""

    def test_force_enable_true_bypasses_migration_check(self):
        """Test that FORCE_ENABLE=True bypasses migration completion check."""
        mock_conn = Mock()
        mock_config = Mock()
        mock_config.is_leave_disabled_enabled.return_value = False
        mock_config.is_force_enable_enabled.return_value = True  # FORCE_ENABLE=True

        service = instanceha.InstanceHAService(mock_config)

        mock_svc = Mock()
        mock_svc.host = 'test-host.example.com'
        mock_svc.forced_down = True
        mock_svc.status = 'disabled'
        mock_svc.disabled_reason = 'instanceha evacuation complete: 2025-01-01'

        # Mock incomplete migrations (normally would prevent re-enable)
        incomplete_migration = Mock()
        incomplete_migration.status = 'running'
        mock_conn.migrations.list.return_value = [incomplete_migration]

        with patch('instanceha._host_enable') as mock_enable:
            instanceha._process_reenabling(mock_conn, service, [mock_svc])
            # Should attempt to enable despite incomplete migrations
            mock_enable.assert_called_once_with(mock_conn, mock_svc, reenable=True, service=service)

    def test_force_enable_false_waits_for_migrations(self):
        """Test that FORCE_ENABLE=False waits for migration completion."""
        mock_conn = Mock()
        mock_config = Mock()
        mock_config.is_leave_disabled_enabled.return_value = False
        mock_config.is_force_enable_enabled.return_value = False  # FORCE_ENABLE=False

        service = instanceha.InstanceHAService(mock_config)

        mock_svc = Mock()
        mock_svc.host = 'test-host.example.com'
        mock_svc.forced_down = True
        mock_svc.status = 'disabled'
        mock_svc.disabled_reason = 'instanceha evacuation complete: 2025-01-01'

        # Mock incomplete migrations
        incomplete_migration = Mock()
        incomplete_migration.status = 'running'
        mock_conn.migrations.list.return_value = [incomplete_migration]

        with patch('instanceha._host_enable') as mock_enable:
            instanceha._process_reenabling(mock_conn, service, [mock_svc])
            # Should NOT attempt to enable with incomplete migrations
            mock_enable.assert_not_called()

    def test_force_enable_still_respects_kdump_delay(self):
        """Test that FORCE_ENABLE=True still respects kdump re-enable delay."""
        mock_conn = Mock()
        mock_config = Mock()
        mock_config.is_leave_disabled_enabled.return_value = False
        mock_config.is_force_enable_enabled.return_value = True  # FORCE_ENABLE=True

        service = instanceha.InstanceHAService(mock_config)
        service.kdump_hosts_timestamp['test-host'] = __import__('time').time() - 30  # 30s ago (< 60s)

        mock_svc = Mock()
        mock_svc.host = 'test-host.example.com'
        mock_svc.forced_down = True
        mock_svc.status = 'disabled'
        mock_svc.disabled_reason = 'instanceha evacuation complete (kdump): 2025-01-01'

        with patch('instanceha._host_enable') as mock_enable:
            instanceha._process_reenabling(mock_conn, service, [mock_svc])
            # Should NOT enable - kdump delay overrides FORCE_ENABLE
            mock_enable.assert_not_called()


class TestLeaveDisabledConfig(unittest.TestCase):
    """Test LEAVE_DISABLED configuration parameter (line 474, used at line 2603-2606)."""

    def test_leave_disabled_true_filters_instanceha_services(self):
        """Test that LEAVE_DISABLED=True filters instanceha-evacuated services from re-enable."""
        mock_conn = Mock()
        mock_config = Mock()
        mock_config.is_leave_disabled_enabled.return_value = True  # LEAVE_DISABLED=True

        service = instanceha.InstanceHAService(mock_config)

        # Service evacuated by instanceha
        instanceha_svc = Mock()
        instanceha_svc.host = 'test-host-1.example.com'
        instanceha_svc.forced_down = True
        instanceha_svc.status = 'disabled'
        instanceha_svc.disabled_reason = 'instanceha evacuation complete: 2025-01-01'

        # Service disabled by other means
        other_svc = Mock()
        other_svc.host = 'test-host-2.example.com'
        other_svc.forced_down = True
        other_svc.status = 'disabled'
        other_svc.disabled_reason = 'manual maintenance'

        # Mock migrations complete for both
        mock_conn.migrations.list.return_value = []

        with patch('instanceha._host_enable') as mock_enable:
            instanceha._process_reenabling(mock_conn, service, [instanceha_svc, other_svc])

            # Should only enable the non-instanceha service
            self.assertEqual(mock_enable.call_count, 1)
            # Verify it was called for the 'other_svc', not 'instanceha_svc'
            mock_enable.assert_called_with(mock_conn, other_svc, reenable=True, service=service)

    def test_leave_disabled_false_enables_all_services(self):
        """Test that LEAVE_DISABLED=False enables all services including instanceha-evacuated."""
        mock_conn = Mock()
        mock_config = Mock()
        mock_config.is_leave_disabled_enabled.return_value = False  # LEAVE_DISABLED=False

        service = instanceha.InstanceHAService(mock_config)

        # Service evacuated by instanceha
        instanceha_svc = Mock()
        instanceha_svc.host = 'test-host.example.com'
        instanceha_svc.forced_down = True
        instanceha_svc.status = 'disabled'
        instanceha_svc.disabled_reason = 'instanceha evacuation complete: 2025-01-01'

        # Mock migrations complete
        mock_conn.migrations.list.return_value = []

        with patch('instanceha._host_enable') as mock_enable:
            instanceha._process_reenabling(mock_conn, service, [instanceha_svc])

            # Should enable instanceha-evacuated service when LEAVE_DISABLED=False
            mock_enable.assert_called_once_with(mock_conn, instanceha_svc, reenable=True, service=service)


class TestTaggedAggregatesConfig(unittest.TestCase):
    """Test TAGGED_AGGREGATES configuration parameter (line 473)."""

    def test_tagged_aggregates_true_filters_by_aggregate(self):
        """Test that TAGGED_AGGREGATES=True filters compute nodes by aggregate metadata."""
        mock_conn = Mock()

        # Create real InstanceHAService instance for proper _is_resource_evacuable behavior
        mock_config = Mock()
        mock_config.get_evacuable_tag.return_value = 'evacuable'
        mock_service = instanceha.InstanceHAService(mock_config)

        # Create aggregates - one evacuable, one not
        evacuable_agg = Mock()
        evacuable_agg.hosts = ['host1', 'host2']
        evacuable_agg.metadata = {'evacuable': 'true'}

        non_evacuable_agg = Mock()
        non_evacuable_agg.hosts = ['host3']
        non_evacuable_agg.metadata = {'evacuable': 'false'}

        mock_conn.aggregates.list.return_value = [evacuable_agg, non_evacuable_agg]

        # Create services on different hosts
        svc1 = Mock()
        svc1.host = 'host1'
        svc2 = Mock()
        svc2.host = 'host2'
        svc3 = Mock()
        svc3.host = 'host3'

        compute_nodes = [svc1, svc2, svc3]
        services = compute_nodes

        # Filter by aggregates
        result = instanceha._filter_by_aggregates(mock_conn, mock_service, compute_nodes, services)

        # Should only include hosts from evacuable aggregate
        self.assertEqual(len(result), 2)
        result_hosts = {svc.host for svc in result}
        self.assertIn('host1', result_hosts)
        self.assertIn('host2', result_hosts)
        self.assertNotIn('host3', result_hosts)

    def test_tagged_aggregates_false_does_not_filter(self):
        """Test that TAGGED_AGGREGATES=False does not filter by aggregate."""
        mock_conn = Mock()
        mock_service = Mock()
        mock_service.config = Mock()
        mock_service.config.is_disabled.return_value = False
        mock_service.config.is_kdump_check_enabled.return_value = False
        mock_service.config.get_threshold.return_value = 50
        mock_service.config.is_tagged_images_enabled.return_value = False
        mock_service.config.is_tagged_flavors_enabled.return_value = False
        mock_service.config.is_tagged_aggregates_enabled.return_value = False  # Disabled
        mock_service.processing_lock = Mock()
        mock_service.hosts_processing = {}

        svc1 = Mock()
        svc1.host = 'host1'
        svc2 = Mock()
        svc2.host = 'host2'

        services = [svc1, svc2]
        compute_nodes = [svc1, svc2]

        with patch('instanceha._filter_processing_hosts', return_value=(compute_nodes, [], set(['host1', 'host2']), 0)):
            with patch('instanceha._prepare_evacuation_resources', return_value=(compute_nodes, [], [], [])):
                with patch('instanceha._cleanup_filtered_hosts'):
                    with patch('instanceha.process_service', return_value=True):
                        # Should not call _filter_by_aggregates when disabled
                        with patch('instanceha._filter_by_aggregates') as mock_filter:
                            instanceha._process_stale_services(
                                mock_conn,
                                mock_service,
                                services,
                                compute_nodes,
                                []
                            )
                            mock_filter.assert_not_called()


if __name__ == '__main__':
    unittest.main()
