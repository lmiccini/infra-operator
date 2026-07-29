"""
Unit tests for _check_fencing_coverage.

Validates that every host in evacuable aggregates has a fencing resource
configured, and that the check fails the readiness probe when coverage
is incomplete.
"""

import unittest
from unittest.mock import Mock, patch

import conftest  # noqa: F401
import instanceha


def _make_aggregate(name, hosts, metadata=None):
    agg = Mock()
    agg.name = name
    agg.hosts = hosts
    agg.metadata = metadata or {}
    return agg


def _make_service():
    service = Mock()
    service.evacuable_tag = 'evacuable'
    service._is_resource_evacuable = instanceha.InstanceHAService._is_resource_evacuable.__get__(service)
    service._check_evacuable_tag = instanceha.InstanceHAService._check_evacuable_tag.__get__(service)
    return service


class TestCheckFencingCoverage(unittest.TestCase):

    def test_all_hosts_have_fencing(self):
        """Returns True when every evacuable host has a fencing resource."""
        conn = Mock()
        conn.aggregates.list.return_value = [
            _make_aggregate('agg1', ['compute-0.example.com', 'compute-1.example.com'],
                            {'evacuable': 'true'}),
        ]
        service = _make_service()
        service.config.fencing = {
            'compute-0.example.com': {'agent': 'ipmi'},
            'compute-1.example.com': {'agent': 'ipmi'},
        }

        result = instanceha._check_fencing_coverage(conn, service)

        self.assertTrue(result)

    def test_missing_fencing_for_one_host(self):
        """Returns False when an evacuable host has no fencing resource."""
        conn = Mock()
        conn.aggregates.list.return_value = [
            _make_aggregate('agg1', ['compute-0.example.com', 'compute-1.example.com'],
                            {'evacuable': 'true'}),
        ]
        service = _make_service()
        service.config.fencing = {
            'compute-0.example.com': {'agent': 'ipmi'},
        }

        result = instanceha._check_fencing_coverage(conn, service)

        self.assertFalse(result)

    def test_missing_fencing_for_all_hosts(self):
        """Returns False when no evacuable host has a fencing resource."""
        conn = Mock()
        conn.aggregates.list.return_value = [
            _make_aggregate('agg1', ['compute-0.example.com', 'compute-1.example.com'],
                            {'evacuable': 'true'}),
        ]
        service = _make_service()
        service.config.fencing = {}

        result = instanceha._check_fencing_coverage(conn, service)

        self.assertFalse(result)

    def test_non_evacuable_aggregates_ignored(self):
        """Hosts in non-evacuable aggregates are not checked."""
        conn = Mock()
        conn.aggregates.list.return_value = [
            _make_aggregate('agg1', ['compute-0.example.com'],
                            {'evacuable': 'true'}),
            _make_aggregate('agg2', ['compute-99.example.com'],
                            {}),
        ]
        service = _make_service()
        service.config.fencing = {
            'compute-0.example.com': {'agent': 'ipmi'},
        }

        result = instanceha._check_fencing_coverage(conn, service)

        self.assertTrue(result)

    def test_short_hostname_matching(self):
        """Fencing keys match by short hostname even if FQDNs differ."""
        conn = Mock()
        conn.aggregates.list.return_value = [
            _make_aggregate('agg1', ['compute-0.example.com'],
                            {'evacuable': 'true'}),
        ]
        service = _make_service()
        service.config.fencing = {
            'compute-0.other.domain': {'agent': 'redfish'},
        }

        result = instanceha._check_fencing_coverage(conn, service)

        self.assertTrue(result)

    def test_multiple_aggregates_merged(self):
        """Hosts across multiple evacuable aggregates are all checked."""
        conn = Mock()
        conn.aggregates.list.return_value = [
            _make_aggregate('agg1', ['compute-0.example.com'],
                            {'evacuable': 'true'}),
            _make_aggregate('agg2', ['compute-1.example.com'],
                            {'evacuable': 'true'}),
        ]
        service = _make_service()
        service.config.fencing = {
            'compute-0.example.com': {'agent': 'ipmi'},
        }

        result = instanceha._check_fencing_coverage(conn, service)

        self.assertFalse(result)

    def test_no_evacuable_aggregates(self):
        """Returns True when no aggregates are tagged as evacuable."""
        conn = Mock()
        conn.aggregates.list.return_value = [
            _make_aggregate('agg1', ['compute-0.example.com'], {}),
        ]
        service = _make_service()
        service.config.fencing = {}

        result = instanceha._check_fencing_coverage(conn, service)

        self.assertTrue(result)

    def test_empty_aggregates_list(self):
        """Returns True when there are no aggregates at all."""
        conn = Mock()
        conn.aggregates.list.return_value = []
        service = _make_service()
        service.config.fencing = {}

        result = instanceha._check_fencing_coverage(conn, service)

        self.assertTrue(result)

    def test_aggregate_fetch_failure(self):
        """Returns False when the aggregates API call fails."""
        conn = Mock()
        conn.aggregates.list.side_effect = Exception("API error")
        service = _make_service()

        result = instanceha._check_fencing_coverage(conn, service)

        self.assertFalse(result)

    def test_duplicate_host_across_aggregates(self):
        """A host in multiple evacuable aggregates only needs one fencing entry."""
        conn = Mock()
        conn.aggregates.list.return_value = [
            _make_aggregate('agg1', ['compute-0.example.com'],
                            {'evacuable': 'true'}),
            _make_aggregate('agg2', ['compute-0.example.com'],
                            {'evacuable': 'true'}),
        ]
        service = _make_service()
        service.config.fencing = {
            'compute-0.example.com': {'agent': 'ipmi'},
        }

        result = instanceha._check_fencing_coverage(conn, service)

        self.assertTrue(result)


class TestFencingCoverageStartupIntegration(unittest.TestCase):
    """Test that fencing coverage failure prevents the pod from starting."""

    @patch('instanceha.sys')
    @patch('instanceha._reconcile_orphaned_hosts')
    @patch('instanceha._establish_nova_connection')
    @patch('instanceha._initialize_service')
    @patch('instanceha._check_fencing_coverage', return_value=False)
    def test_startup_exits_on_fencing_gap(self, mock_check, mock_init_svc,
                                          mock_conn, mock_reconcile, mock_sys):
        """main() calls sys.exit(1) when fencing coverage check fails."""
        mock_config = Mock()
        mock_config.get_config_value = Mock(side_effect=lambda key: {
            'LOGLEVEL': 'INFO',
            'TAGGED_AGGREGATES': True,
        }.get(key, Mock()))

        with patch('instanceha.ConfigManager', return_value=mock_config):
            instanceha.main()

        mock_check.assert_called_once()
        mock_sys.exit.assert_called_with(1)

    @patch('instanceha._reconcile_orphaned_hosts')
    @patch('instanceha._establish_nova_connection')
    @patch('instanceha._initialize_service')
    @patch('instanceha._check_fencing_coverage', return_value=True)
    def test_startup_continues_on_full_coverage(self, mock_check, mock_init_svc,
                                                 mock_conn, mock_reconcile):
        """main() enters the poll loop when fencing coverage passes."""
        mock_service = Mock()
        mock_service.shutdown_event.is_set.return_value = True
        mock_service.processing_executor = Mock()
        mock_init_svc.return_value = mock_service

        mock_config = Mock()
        mock_config.get_config_value = Mock(side_effect=lambda key: {
            'LOGLEVEL': 'INFO',
            'TAGGED_AGGREGATES': True,
        }.get(key, Mock()))

        with patch('instanceha.ConfigManager', return_value=mock_config), \
             patch('instanceha.signal.signal'):
            instanceha.main()

        mock_check.assert_called_once()
        mock_service.processing_executor.shutdown.assert_called_once_with(wait=True)


if __name__ == '__main__':
    unittest.main()
