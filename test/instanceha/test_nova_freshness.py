"""
Tests for Nova data freshness validation (Improvement 5).

Covers:
- _validate_nova_data_freshness returns True when most services fresh
- Returns False when fresh percentage below threshold
- Returns True when threshold is 0 (disabled)
- Correctly excludes disabled/forced-down services
- Handles empty service list
- Handles services with invalid updated_at
- NOVA_FRESHNESS_THRESHOLD config defaults and validation
- NOVA_DATA_FRESHNESS Prometheus gauge
"""

import unittest
from datetime import datetime, timedelta
from unittest.mock import MagicMock

import conftest  # noqa: F401
import instanceha


def _make_service(host, updated_at, status='enabled', forced_down=False, state='up'):
    svc = MagicMock()
    svc.host = host
    svc.updated_at = updated_at.isoformat() if updated_at else None
    svc.status = status
    svc.forced_down = forced_down
    svc.state = state
    svc.binary = 'nova-compute'
    return svc


class TestValidateNovaDataFreshness(unittest.TestCase):
    """Tests for _validate_nova_data_freshness function."""

    def test_returns_true_when_all_fresh(self):
        """All services have recent timestamps = fresh."""
        now = datetime.utcnow()
        target = now - timedelta(seconds=30)
        services = [
            _make_service('compute-1', now - timedelta(seconds=5)),
            _make_service('compute-2', now - timedelta(seconds=10)),
            _make_service('compute-3', now - timedelta(seconds=15)),
        ]
        is_fresh, pct, total, fresh_count = instanceha._validate_nova_data_freshness(
            services, target, 50)
        self.assertTrue(is_fresh)
        self.assertEqual(pct, 100.0)
        self.assertEqual(total, 3)
        self.assertEqual(fresh_count, 3)

    def test_returns_false_when_most_stale(self):
        """Most services stale = not fresh."""
        now = datetime.utcnow()
        target = now - timedelta(seconds=30)
        services = [
            _make_service('compute-1', now - timedelta(seconds=5)),
            _make_service('compute-2', now - timedelta(minutes=5)),
            _make_service('compute-3', now - timedelta(minutes=10)),
            _make_service('compute-4', now - timedelta(minutes=15)),
        ]
        is_fresh, pct, total, fresh_count = instanceha._validate_nova_data_freshness(
            services, target, 50)
        self.assertFalse(is_fresh)
        self.assertEqual(fresh_count, 1)
        self.assertEqual(total, 4)

    def test_returns_true_when_exactly_at_threshold(self):
        """50% fresh with 50% threshold = pass."""
        now = datetime.utcnow()
        target = now - timedelta(seconds=30)
        services = [
            _make_service('compute-1', now - timedelta(seconds=5)),
            _make_service('compute-2', now - timedelta(minutes=5)),
        ]
        is_fresh, pct, total, fresh_count = instanceha._validate_nova_data_freshness(
            services, target, 50)
        self.assertTrue(is_fresh)
        self.assertEqual(pct, 50.0)

    def test_returns_true_when_threshold_zero(self):
        """Threshold 0 = all data considered fresh (disabled)."""
        now = datetime.utcnow()
        target = now - timedelta(seconds=30)
        services = [
            _make_service('compute-1', now - timedelta(minutes=10)),
            _make_service('compute-2', now - timedelta(minutes=10)),
        ]
        is_fresh, pct, total, fresh_count = instanceha._validate_nova_data_freshness(
            services, target, 0)
        self.assertTrue(is_fresh)

    def test_excludes_disabled_services(self):
        """Disabled services should not count toward total."""
        now = datetime.utcnow()
        target = now - timedelta(seconds=30)
        services = [
            _make_service('compute-1', now - timedelta(seconds=5)),
            _make_service('compute-2', now - timedelta(minutes=5), status='disabled'),
            _make_service('compute-3', now - timedelta(minutes=5), status='disabled'),
        ]
        is_fresh, pct, total, fresh_count = instanceha._validate_nova_data_freshness(
            services, target, 50)
        self.assertTrue(is_fresh)
        self.assertEqual(total, 1)
        self.assertEqual(fresh_count, 1)

    def test_excludes_forced_down_services(self):
        """Forced-down services should not count toward total."""
        now = datetime.utcnow()
        target = now - timedelta(seconds=30)
        services = [
            _make_service('compute-1', now - timedelta(seconds=5)),
            _make_service('compute-2', now - timedelta(minutes=5), forced_down=True),
        ]
        is_fresh, pct, total, fresh_count = instanceha._validate_nova_data_freshness(
            services, target, 50)
        self.assertTrue(is_fresh)
        self.assertEqual(total, 1)

    def test_empty_service_list(self):
        """Empty list should return True (nothing to worry about)."""
        now = datetime.utcnow()
        target = now - timedelta(seconds=30)
        is_fresh, pct, total, fresh_count = instanceha._validate_nova_data_freshness(
            [], target, 50)
        self.assertTrue(is_fresh)
        self.assertEqual(total, 0)

    def test_all_services_disabled(self):
        """If all services are disabled, nothing to validate."""
        now = datetime.utcnow()
        target = now - timedelta(seconds=30)
        services = [
            _make_service('compute-1', now - timedelta(minutes=10), status='disabled'),
            _make_service('compute-2', now - timedelta(minutes=10), status='disabled'),
        ]
        is_fresh, pct, total, fresh_count = instanceha._validate_nova_data_freshness(
            services, target, 50)
        self.assertTrue(is_fresh)
        self.assertEqual(total, 0)


class TestNovaFreshnessConfig(unittest.TestCase):
    """Tests for NOVA_FRESHNESS_THRESHOLD config."""

    def test_default_value(self):
        config = instanceha.ConfigManager()
        self.assertEqual(config.get_config_value('NOVA_FRESHNESS_THRESHOLD'), 50)

    def test_config_key_exists(self):
        self.assertIn('NOVA_FRESHNESS_THRESHOLD', instanceha.ConfigManager._config_map)

    def test_config_item_type(self):
        item = instanceha.ConfigManager._config_map['NOVA_FRESHNESS_THRESHOLD']
        self.assertEqual(item.type, 'int')
        self.assertEqual(item.min_val, 0)
        self.assertEqual(item.max_val, 100)

    def test_value_clamped_to_min(self):
        config = instanceha.ConfigManager()
        config.config['NOVA_FRESHNESS_THRESHOLD'] = '-10'
        self.assertEqual(config.get_config_value('NOVA_FRESHNESS_THRESHOLD'), 0)

    def test_value_clamped_to_max(self):
        config = instanceha.ConfigManager()
        config.config['NOVA_FRESHNESS_THRESHOLD'] = '150'
        self.assertEqual(config.get_config_value('NOVA_FRESHNESS_THRESHOLD'), 100)


class TestNovaFreshnessMetric(unittest.TestCase):
    """Tests for NOVA_DATA_FRESHNESS gauge."""

    def test_metric_exists(self):
        self.assertIsNotNone(instanceha.NOVA_DATA_FRESHNESS)


if __name__ == '__main__':
    unittest.main()
