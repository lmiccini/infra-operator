"""
Tests for correlated staleness detection (Improvement 2).

Covers:
- _detect_correlated_staleness returns True when >=3 services stale within window
- Returns False when <3 services are stale
- Returns False when timestamps spread beyond window
- Returns False with empty list
- Handles services with invalid updated_at gracefully
- STALENESS_CORRELATION_WINDOW config defaults and validation
- CORRELATED_STALENESS_TOTAL Prometheus counter
"""

import unittest
from datetime import datetime, timedelta
from unittest.mock import MagicMock

import conftest  # noqa: F401
import instanceha


def _make_service(host, updated_at):
    svc = MagicMock()
    svc.host = host
    svc.updated_at = updated_at.isoformat()
    svc.status = 'enabled'
    svc.forced_down = False
    svc.state = 'up'
    return svc


class TestDetectCorrelatedStaleness(unittest.TestCase):
    """Tests for _detect_correlated_staleness function."""

    def test_returns_true_for_3_services_within_window(self):
        """3+ services with timestamps within window = correlated."""
        base = datetime(2024, 1, 1, 12, 0, 0)
        services = [
            _make_service('compute-1', base),
            _make_service('compute-2', base + timedelta(seconds=5)),
            _make_service('compute-3', base + timedelta(seconds=10)),
        ]
        self.assertTrue(instanceha._detect_correlated_staleness(services, 30))

    def test_returns_true_for_5_services_within_window(self):
        base = datetime(2024, 1, 1, 12, 0, 0)
        services = [
            _make_service(f'compute-{i}', base + timedelta(seconds=i * 2))
            for i in range(5)
        ]
        self.assertTrue(instanceha._detect_correlated_staleness(services, 30))

    def test_returns_false_for_2_services(self):
        """Fewer than 3 services = not correlated."""
        base = datetime(2024, 1, 1, 12, 0, 0)
        services = [
            _make_service('compute-1', base),
            _make_service('compute-2', base + timedelta(seconds=5)),
        ]
        self.assertFalse(instanceha._detect_correlated_staleness(services, 30))

    def test_returns_false_for_1_service(self):
        base = datetime(2024, 1, 1, 12, 0, 0)
        services = [_make_service('compute-1', base)]
        self.assertFalse(instanceha._detect_correlated_staleness(services, 30))

    def test_returns_false_for_empty_list(self):
        self.assertFalse(instanceha._detect_correlated_staleness([], 30))

    def test_returns_false_when_timestamps_spread_beyond_window(self):
        """Timestamps spread over minutes = independent failures."""
        base = datetime(2024, 1, 1, 12, 0, 0)
        services = [
            _make_service('compute-1', base),
            _make_service('compute-2', base + timedelta(minutes=5)),
            _make_service('compute-3', base + timedelta(minutes=10)),
        ]
        self.assertFalse(instanceha._detect_correlated_staleness(services, 30))

    def test_returns_false_at_boundary(self):
        """Timestamps spread exactly at window boundary."""
        base = datetime(2024, 1, 1, 12, 0, 0)
        services = [
            _make_service('compute-1', base),
            _make_service('compute-2', base + timedelta(seconds=15)),
            _make_service('compute-3', base + timedelta(seconds=31)),
        ]
        self.assertFalse(instanceha._detect_correlated_staleness(services, 30))

    def test_returns_true_at_boundary_inclusive(self):
        """Timestamps spread exactly at window = correlated."""
        base = datetime(2024, 1, 1, 12, 0, 0)
        services = [
            _make_service('compute-1', base),
            _make_service('compute-2', base + timedelta(seconds=15)),
            _make_service('compute-3', base + timedelta(seconds=30)),
        ]
        self.assertTrue(instanceha._detect_correlated_staleness(services, 30))

    def test_handles_invalid_updated_at(self):
        """Services with invalid updated_at should be skipped gracefully."""
        base = datetime(2024, 1, 1, 12, 0, 0)
        svc_invalid = MagicMock()
        svc_invalid.host = 'compute-bad'
        svc_invalid.updated_at = 'not-a-date'

        services = [
            _make_service('compute-1', base),
            _make_service('compute-2', base + timedelta(seconds=5)),
            svc_invalid,
        ]
        # Only 2 valid timestamps, so not correlated
        self.assertFalse(instanceha._detect_correlated_staleness(services, 30))

    def test_handles_none_updated_at(self):
        svc_none = MagicMock()
        svc_none.host = 'compute-none'
        svc_none.updated_at = None

        base = datetime(2024, 1, 1, 12, 0, 0)
        services = [
            _make_service('compute-1', base),
            _make_service('compute-2', base + timedelta(seconds=5)),
            _make_service('compute-3', base + timedelta(seconds=10)),
            svc_none,
        ]
        # 3 valid timestamps within window = correlated
        self.assertTrue(instanceha._detect_correlated_staleness(services, 30))


class TestCorrelatedStalenessConfig(unittest.TestCase):
    """Tests for STALENESS_CORRELATION_WINDOW config."""

    def test_default_value(self):
        config = instanceha.ConfigManager()
        self.assertEqual(config.get_config_value('STALENESS_CORRELATION_WINDOW'), 30)

    def test_config_key_exists(self):
        self.assertIn('STALENESS_CORRELATION_WINDOW', instanceha.ConfigManager._config_map)

    def test_config_item_type(self):
        item = instanceha.ConfigManager._config_map['STALENESS_CORRELATION_WINDOW']
        self.assertEqual(item.type, 'int')
        self.assertEqual(item.min_val, 5)
        self.assertEqual(item.max_val, 120)


class TestCorrelatedStalenessMinPercentConfig(unittest.TestCase):
    """Tests for STALENESS_CORRELATION_MIN_PERCENT config."""

    def test_default_value(self):
        config = instanceha.ConfigManager()
        self.assertEqual(config.get_config_value('STALENESS_CORRELATION_MIN_PERCENT'), 10)

    def test_config_key_exists(self):
        self.assertIn('STALENESS_CORRELATION_MIN_PERCENT', instanceha.ConfigManager._config_map)

    def test_config_item_type(self):
        item = instanceha.ConfigManager._config_map['STALENESS_CORRELATION_MIN_PERCENT']
        self.assertEqual(item.type, 'int')
        self.assertEqual(item.min_val, 1)
        self.assertEqual(item.max_val, 50)

    def test_value_clamped_to_min(self):
        config = instanceha.ConfigManager()
        config.config['STALENESS_CORRELATION_MIN_PERCENT'] = '0'
        self.assertEqual(config.get_config_value('STALENESS_CORRELATION_MIN_PERCENT'), 1)

    def test_value_clamped_to_max(self):
        config = instanceha.ConfigManager()
        config.config['STALENESS_CORRELATION_MIN_PERCENT'] = '75'
        self.assertEqual(config.get_config_value('STALENESS_CORRELATION_MIN_PERCENT'), 50)


class TestCorrelatedStalenessHeartbeatCrossCheck(unittest.TestCase):
    """Tests for heartbeat cross-check in correlated staleness path."""

    def _make_service(self, check_heartbeat=True, heartbeat_timeout=120,
                      heartbeat_timestamps=None):
        import threading
        from collections import defaultdict
        svc = MagicMock()
        svc.config = MagicMock()
        svc.config.get_config_value = MagicMock(side_effect=lambda key: {
            'DISABLED': False, 'THRESHOLD': 50, 'CHECK_KDUMP': False,
            'CHECK_HEARTBEAT': check_heartbeat,
            'HEARTBEAT_TIMEOUT': heartbeat_timeout,
            'WORKERS': 2, 'POLL': 5, 'FENCING_TIMEOUT': 30,
            'TAGGED_IMAGES': False, 'TAGGED_FLAVORS': False,
            'TAGGED_AGGREGATES': False, 'RESERVED_HOSTS': False,
            'STALENESS_CORRELATION_WINDOW': 30,
            'STALENESS_CORRELATION_MIN_PERCENT': 10,
        }[key])
        svc.processing_lock = threading.Lock()
        svc.hosts_processing = defaultdict(float)
        svc.heartbeat_lock = threading.Lock()
        svc.heartbeat_hosts_timestamp = defaultdict(float)
        if heartbeat_timestamps:
            svc.heartbeat_hosts_timestamp.update(heartbeat_timestamps)
        svc.refresh_evacuable_cache = MagicMock()
        svc.get_hosts_with_servers_cached = MagicMock(return_value={})
        svc.filter_hosts_with_servers = MagicMock(return_value=[])
        return svc

    def test_suppresses_when_heartbeats_present(self):
        """Correlated staleness + heartbeats = suppress fencing (control plane issue)."""
        import time as time_mod
        now = time_mod.time()
        base = datetime(2024, 1, 1, 12, 0, 0)
        services = [
            _make_service(f'compute-{i}', base + timedelta(seconds=i * 2))
            for i in range(4)
        ]
        service = self._make_service(
            check_heartbeat=True,
            heartbeat_timestamps={
                f'compute-{i}': now - 10 for i in range(4)
            })
        conn = MagicMock()

        with unittest.mock.patch('instanceha._emit_k8s_event'), \
             unittest.mock.patch('instanceha._cleanup_filtered_hosts') as mock_cleanup:
            instanceha._process_stale_services(conn, service, services, services, [])
            mock_cleanup.assert_called_once()

    def test_proceeds_when_no_heartbeats(self):
        """Correlated staleness but no heartbeats = proceed (genuine failure)."""
        base = datetime(2024, 1, 1, 12, 0, 0)
        services = [
            _make_service(f'compute-{i}', base + timedelta(seconds=i * 2))
            for i in range(4)
        ]
        service = self._make_service(check_heartbeat=True, heartbeat_timestamps={})
        service.get_hosts_with_servers_cached.return_value = {
            f'compute-{i}': [f'server-{i}'] for i in range(4)
        }
        service.filter_hosts_with_servers.return_value = services
        conn = MagicMock()

        with unittest.mock.patch('instanceha._emit_k8s_event'), \
             unittest.mock.patch('instanceha._check_critical_services', return_value=(True, "")), \
             unittest.mock.patch('instanceha._filter_reachable_hosts',
                                 return_value=(services, [], False)), \
             unittest.mock.patch('instanceha._cleanup_filtered_hosts'):
            instanceha._process_stale_services(conn, service, services, services, [])
            service.get_hosts_with_servers_cached.assert_called()

    def test_skipped_when_heartbeat_disabled(self):
        """Correlated staleness is skipped without CHECK_HEARTBEAT — fencing proceeds."""
        base = datetime(2024, 1, 1, 12, 0, 0)
        services = [
            _make_service(f'compute-{i}', base + timedelta(seconds=i * 2))
            for i in range(4)
        ]
        service = self._make_service(check_heartbeat=False)
        service.get_hosts_with_servers_cached.return_value = {
            f'compute-{i}': [f'server-{i}'] for i in range(4)
        }
        service.filter_hosts_with_servers.return_value = services
        conn = MagicMock()

        with unittest.mock.patch('instanceha._emit_k8s_event'), \
             unittest.mock.patch('instanceha._check_critical_services', return_value=(True, "")), \
             unittest.mock.patch('instanceha._cleanup_filtered_hosts'):
            instanceha._process_stale_services(conn, service, services, services, [])
            service.get_hosts_with_servers_cached.assert_called()


class TestCorrelatedStalenessScaling(unittest.TestCase):
    """Tests for dynamic threshold scaling with cluster size."""

    def _make_service_with_min_pct(self, min_pct=10, stale_hosts=None):
        import threading
        import time as time_mod
        from collections import defaultdict
        svc = MagicMock()
        svc.config = MagicMock()
        svc.config.get_config_value = MagicMock(side_effect=lambda key: {
            'DISABLED': False, 'THRESHOLD': 50, 'CHECK_KDUMP': False,
            'CHECK_HEARTBEAT': True,
            'HEARTBEAT_TIMEOUT': 120,
            'WORKERS': 2, 'POLL': 5, 'FENCING_TIMEOUT': 30,
            'TAGGED_IMAGES': False, 'TAGGED_FLAVORS': False,
            'TAGGED_AGGREGATES': False, 'RESERVED_HOSTS': False,
            'STALENESS_CORRELATION_WINDOW': 30,
            'STALENESS_CORRELATION_MIN_PERCENT': min_pct,
        }[key])
        svc.processing_lock = threading.Lock()
        svc.hosts_processing = defaultdict(float)
        svc.heartbeat_lock = threading.Lock()
        hb_ts = defaultdict(float)
        if stale_hosts:
            now = time_mod.time()
            for host in stale_hosts:
                hb_ts[host] = now - 10
        svc.heartbeat_hosts_timestamp = hb_ts
        svc.refresh_evacuable_cache = MagicMock()
        svc.get_hosts_with_servers_cached = MagicMock(return_value={})
        svc.filter_hosts_with_servers = MagicMock(return_value=[])
        return svc

    def test_small_cluster_uses_floor_of_3(self):
        """At 10 nodes, floor of 3 dominates over 10% (=1)."""
        base = datetime(2024, 1, 1, 12, 0, 0)
        stale = [_make_service(f'compute-{i}', base + timedelta(seconds=i * 2))
                 for i in range(3)]
        all_services = stale + [
            _make_service(f'compute-ok-{i}', base + timedelta(seconds=100))
            for i in range(7)]
        stale_hosts = [f'compute-{i}' for i in range(3)]
        service = self._make_service_with_min_pct(10, stale_hosts=stale_hosts)
        conn = MagicMock()

        with unittest.mock.patch('instanceha._emit_k8s_event'), \
             unittest.mock.patch('instanceha._cleanup_filtered_hosts') as mock_cleanup:
            instanceha._process_stale_services(conn, service, all_services, stale, [])
            mock_cleanup.assert_called_once()

    def test_large_cluster_requires_more_stale(self):
        """At 100 nodes with 10%, need >=10 stale to trigger. 5 stale should NOT trigger."""
        base = datetime(2024, 1, 1, 12, 0, 0)
        stale = [_make_service(f'compute-{i}', base + timedelta(seconds=i * 2))
                 for i in range(5)]
        all_services = stale + [
            _make_service(f'compute-ok-{i}', base + timedelta(seconds=100))
            for i in range(95)]
        service = self._make_service_with_min_pct(10)
        service.get_hosts_with_servers_cached.return_value = {
            f'compute-{i}': [f'server-{i}'] for i in range(5)
        }
        service.filter_hosts_with_servers.return_value = stale
        conn = MagicMock()

        with unittest.mock.patch('instanceha._emit_k8s_event'), \
             unittest.mock.patch('instanceha._check_critical_services', return_value=(True, "")), \
             unittest.mock.patch('instanceha._filter_reachable_hosts',
                                 return_value=(stale, [], False)), \
             unittest.mock.patch('instanceha._cleanup_filtered_hosts'):
            instanceha._process_stale_services(conn, service, all_services, stale, [])
            service.get_hosts_with_servers_cached.assert_called()

    def test_large_cluster_triggers_at_threshold(self):
        """At 100 nodes with 10%, 10 stale within window SHOULD trigger."""
        base = datetime(2024, 1, 1, 12, 0, 0)
        stale = [_make_service(f'compute-{i}', base + timedelta(seconds=i))
                 for i in range(10)]
        all_services = stale + [
            _make_service(f'compute-ok-{i}', base + timedelta(seconds=100))
            for i in range(90)]
        stale_hosts = [f'compute-{i}' for i in range(10)]
        service = self._make_service_with_min_pct(10, stale_hosts=stale_hosts)
        conn = MagicMock()

        with unittest.mock.patch('instanceha._emit_k8s_event'), \
             unittest.mock.patch('instanceha._cleanup_filtered_hosts') as mock_cleanup:
            instanceha._process_stale_services(conn, service, all_services, stale, [])
            mock_cleanup.assert_called_once()

    def test_1000_node_cluster(self):
        """At 1000 nodes with 10%, need >=100 stale. 50 should NOT trigger."""
        base = datetime(2024, 1, 1, 12, 0, 0)
        stale = [_make_service(f'compute-{i}', base + timedelta(seconds=i))
                 for i in range(50)]
        all_services = stale + [
            _make_service(f'compute-ok-{i}', base + timedelta(seconds=100))
            for i in range(950)]
        service = self._make_service_with_min_pct(10)
        service.get_hosts_with_servers_cached.return_value = {
            f'compute-{i}': [f'server-{i}'] for i in range(50)
        }
        service.filter_hosts_with_servers.return_value = stale
        conn = MagicMock()

        with unittest.mock.patch('instanceha._emit_k8s_event'), \
             unittest.mock.patch('instanceha._check_critical_services', return_value=(True, "")), \
             unittest.mock.patch('instanceha._filter_reachable_hosts',
                                 return_value=(stale, [], False)), \
             unittest.mock.patch('instanceha._cleanup_filtered_hosts'):
            instanceha._process_stale_services(conn, service, all_services, stale, [])
            service.get_hosts_with_servers_cached.assert_called()


class TestCorrelatedStalenessMetric(unittest.TestCase):
    """Tests for CORRELATED_STALENESS_TOTAL counter."""

    def test_metric_exists(self):
        self.assertIsNotNone(instanceha.CORRELATED_STALENESS_TOTAL)


if __name__ == '__main__':
    unittest.main()
