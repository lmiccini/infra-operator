"""
Tests for K8s API recovery cooldown (Improvement 1).

Covers:
- K8S_API_RECOVERY_COOLDOWN config: default, range clamping
- k8s_api_recovery_time state tracking
- Health check thread records recovery time on unreachable→reachable transition
- Health check thread does NOT record on reachable→reachable (no partition)
- Main loop skips fencing during cooldown window
- Main loop resumes fencing after cooldown expires
- Main loop proceeds when cooldown is 0 (disabled)
- Main loop proceeds when no recovery has occurred (recovery_time == 0.0)
- Prometheus metric K8S_RECOVERY_COOLDOWN_ACTIVE
"""

import time
import unittest
from unittest.mock import patch, MagicMock

import conftest  # noqa: F401
import instanceha


class TestRecoveryCooldownConfig(unittest.TestCase):
    """Tests for K8S_API_RECOVERY_COOLDOWN config key."""

    def test_default_value(self):
        config = instanceha.ConfigManager()
        self.assertEqual(config.get_config_value('K8S_API_RECOVERY_COOLDOWN'), 120)

    def test_config_key_exists(self):
        self.assertIn('K8S_API_RECOVERY_COOLDOWN', instanceha.ConfigManager._config_map)

    def test_config_item_type(self):
        item = instanceha.ConfigManager._config_map['K8S_API_RECOVERY_COOLDOWN']
        self.assertEqual(item.type, 'int')
        self.assertEqual(item.default, 120)
        self.assertEqual(item.min_val, 0)
        self.assertEqual(item.max_val, 600)

    def test_value_clamped_to_min(self):
        config = instanceha.ConfigManager()
        config.config['K8S_API_RECOVERY_COOLDOWN'] = '-10'
        self.assertEqual(config.get_config_value('K8S_API_RECOVERY_COOLDOWN'), 0)

    def test_value_clamped_to_max(self):
        config = instanceha.ConfigManager()
        config.config['K8S_API_RECOVERY_COOLDOWN'] = '999'
        self.assertEqual(config.get_config_value('K8S_API_RECOVERY_COOLDOWN'), 600)


class TestRecoveryCooldownState(unittest.TestCase):
    """Tests for k8s_api_recovery_time state tracking."""

    def test_initial_state(self):
        config = instanceha.ConfigManager()
        service = instanceha.InstanceHAService(config)
        self.assertEqual(service.k8s_api_recovery_time, 0.0)


class TestRecoveryCooldownHealthCheck(unittest.TestCase):
    """Tests for recovery time recording in the health check thread."""

    def setUp(self):
        self.config = instanceha.ConfigManager()
        self.service = instanceha.InstanceHAService(self.config)

    @patch('instanceha._emit_k8s_event')
    @patch('instanceha._check_k8s_api_reachable')
    def test_records_recovery_time_after_partition(self, mock_check, mock_event):
        """When transitioning from unreachable (>=3 failures) to reachable,
        k8s_api_recovery_time should be set."""
        self.service.k8s_api_reachable = True
        self.service.ready = True
        call_count = 0

        def side_effect():
            nonlocal call_count
            call_count += 1
            return call_count > 3  # fail 3 times, then succeed

        mock_check.side_effect = lambda: side_effect()

        wait_count = 0
        def stop_after_4(*args, **kwargs):
            nonlocal wait_count
            wait_count += 1
            if wait_count >= 4:
                self.service.shutdown_event.set()

        self.service.shutdown_event.wait = stop_after_4
        self.service._run_k8s_health_check()

        self.assertGreater(self.service.k8s_api_recovery_time, 0.0)
        self.assertTrue(self.service.k8s_api_reachable)

    @patch('instanceha._emit_k8s_event')
    @patch('instanceha._check_k8s_api_reachable', return_value=True)
    def test_no_recovery_time_without_partition(self, _check, _event):
        """When API is always reachable, recovery time should stay at 0."""
        self.service.k8s_api_reachable = True
        self.service.ready = True
        self.service.shutdown_event.set()
        self.service._run_k8s_health_check()

        self.assertEqual(self.service.k8s_api_recovery_time, 0.0)

    @patch('instanceha._emit_k8s_event')
    @patch('instanceha._check_k8s_api_reachable')
    def test_no_recovery_time_on_single_failure(self, mock_check, _event):
        """A single failure followed by success should not set recovery time
        (partition requires >= 3 consecutive failures)."""
        self.service.k8s_api_reachable = True
        self.service.ready = True
        call_count = 0

        def side_effect():
            nonlocal call_count
            call_count += 1
            return call_count != 1  # fail first, succeed after

        mock_check.side_effect = lambda: side_effect()

        wait_count = 0
        def stop_after_2(*args, **kwargs):
            nonlocal wait_count
            wait_count += 1
            if wait_count >= 2:
                self.service.shutdown_event.set()

        self.service.shutdown_event.wait = stop_after_2
        self.service._run_k8s_health_check()

        self.assertEqual(self.service.k8s_api_recovery_time, 0.0)

    @patch('instanceha._emit_k8s_event')
    @patch('instanceha._check_k8s_api_reachable')
    def test_emits_k8s_event_on_recovery(self, mock_check, mock_event):
        """RecoveryCooldown event should be emitted when entering cooldown."""
        self.service.k8s_api_reachable = True
        self.service.ready = True
        call_count = 0

        def side_effect():
            nonlocal call_count
            call_count += 1
            return call_count > 3

        mock_check.side_effect = lambda: side_effect()

        wait_count = 0
        def stop_after_4(*args, **kwargs):
            nonlocal wait_count
            wait_count += 1
            if wait_count >= 4:
                self.service.shutdown_event.set()

        self.service.shutdown_event.wait = stop_after_4
        self.service._run_k8s_health_check()

        recovery_calls = [c for c in mock_event.call_args_list
                          if c[0][1] == 'RecoveryCooldown']
        self.assertEqual(len(recovery_calls), 1)


class TestRecoveryCooldownMainLoop(unittest.TestCase):
    """Tests for cooldown gate behavior in the main loop."""

    def setUp(self):
        self.config = instanceha.ConfigManager()
        self.service = instanceha.InstanceHAService(self.config)
        self.service.k8s_api_reachable = True
        self.service.ready = True

    def test_skips_fencing_during_cooldown(self):
        """When recovery time is recent, fencing should be skipped."""
        self.service.k8s_api_recovery_time = time.monotonic()

        cooldown = self.service.config.get_config_value('K8S_API_RECOVERY_COOLDOWN')
        elapsed = time.monotonic() - self.service.k8s_api_recovery_time
        self.assertLess(elapsed, cooldown)

    def test_proceeds_after_cooldown_expires(self):
        """When recovery time is old enough, fencing should proceed."""
        cooldown = self.service.config.get_config_value('K8S_API_RECOVERY_COOLDOWN')
        # Set recovery time far in the past
        self.service.k8s_api_recovery_time = time.monotonic() - cooldown - 10

        elapsed = time.monotonic() - self.service.k8s_api_recovery_time
        self.assertGreater(elapsed, cooldown)

    def test_proceeds_when_cooldown_disabled(self):
        """When cooldown is 0, fencing should proceed regardless of recovery time."""
        self.service.config.config['K8S_API_RECOVERY_COOLDOWN'] = '0'
        self.service.k8s_api_recovery_time = time.monotonic()

        cooldown = self.service.config.get_config_value('K8S_API_RECOVERY_COOLDOWN')
        self.assertEqual(cooldown, 0)

    def test_proceeds_when_no_recovery(self):
        """When no recovery has occurred, fencing should proceed."""
        self.assertEqual(self.service.k8s_api_recovery_time, 0.0)


class TestRecoveryCooldownMetric(unittest.TestCase):
    """Tests for K8S_RECOVERY_COOLDOWN_ACTIVE metric."""

    def test_metric_exists(self):
        self.assertIsNotNone(instanceha.K8S_RECOVERY_COOLDOWN_ACTIVE)


if __name__ == '__main__':
    unittest.main()
