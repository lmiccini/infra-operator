"""
Tests for evacuation of hosts Nova auto-disabled due to libvirt connection loss.

Covers:
- _is_auto_disabled_libvirt: detection of the Nova "AUTO: Connection to libvirt
  lost" disable reason (and rejection of other/instanceha reasons)
- _detect_auto_disabled_services: filtering a service list
- _filter_auto_disabled_grace: grace-period state machine (first-seen, timeout,
  pruning of recovered hosts)
- _admit_stale_services: auto-disabled hosts are submitted for processing once
  past the grace period, gated by EVACUATE_AUTO_DISABLED, and bypass the
  heartbeat "reachable" skip
"""

import threading
import time
import unittest
import logging
from collections import defaultdict
from unittest.mock import Mock, patch

logging.getLogger().setLevel(logging.CRITICAL)
import conftest  # noqa: F401
import instanceha
logging.getLogger().setLevel(logging.CRITICAL)


LIBVIRT_LOST_REASON = "AUTO: Connection to libvirt lost: connection closed due to keepalive timeout"


def _make_svc(host='host-1.example.com', status='disabled', forced_down=False,
              state='up', disabled_reason=LIBVIRT_LOST_REASON):
    svc = Mock()
    svc.host = host
    svc.status = status
    svc.forced_down = forced_down
    svc.state = state
    svc.disabled_reason = disabled_reason
    return svc


# ============================================================================
# _is_auto_disabled_libvirt tests
# ============================================================================

class TestIsAutoDisabledLibvirt(unittest.TestCase):

    def test_libvirt_lost_is_detected(self):
        self.assertTrue(instanceha._is_auto_disabled_libvirt(_make_svc()))

    def test_detection_is_case_insensitive(self):
        svc = _make_svc(disabled_reason="auto: connection to libvirt LOST: whatever")
        self.assertTrue(instanceha._is_auto_disabled_libvirt(svc))

    def test_enabled_host_not_detected(self):
        self.assertFalse(instanceha._is_auto_disabled_libvirt(_make_svc(status='enabled')))

    def test_forced_down_not_detected(self):
        self.assertFalse(instanceha._is_auto_disabled_libvirt(_make_svc(forced_down=True)))

    def test_build_failure_auto_disable_not_detected(self):
        """Consecutive-build-failure auto-disable is a different scenario."""
        svc = _make_svc(disabled_reason="Auto-disabled due to 10 build failures")
        self.assertFalse(instanceha._is_auto_disabled_libvirt(svc))

    def test_instanceha_reason_not_detected(self):
        """Our own evacuation marker must never be treated as auto-disabled."""
        svc = _make_svc(disabled_reason="instanceha evacuation: 2024-01-01T00:00:00")
        self.assertFalse(instanceha._is_auto_disabled_libvirt(svc))

    def test_operator_disabled_not_detected(self):
        svc = _make_svc(disabled_reason="disabled by admin for maintenance")
        self.assertFalse(instanceha._is_auto_disabled_libvirt(svc))

    def test_empty_reason_not_detected(self):
        self.assertFalse(instanceha._is_auto_disabled_libvirt(_make_svc(disabled_reason='')))

    def test_reserved_host_not_detected(self):
        svc = _make_svc(disabled_reason="reserved")
        self.assertFalse(instanceha._is_auto_disabled_libvirt(svc))


# ============================================================================
# _detect_auto_disabled_services tests
# ============================================================================

class TestDetectAutoDisabledServices(unittest.TestCase):

    def test_filters_only_matching_services(self):
        good = _make_svc(host='bad.example.com')
        healthy = _make_svc(host='ok.example.com', status='enabled')
        operator = _make_svc(host='maint.example.com', disabled_reason='maintenance')
        result = instanceha._detect_auto_disabled_services([good, healthy, operator])
        self.assertEqual([s.host for s in result], ['bad.example.com'])

    def test_empty_input(self):
        self.assertEqual(instanceha._detect_auto_disabled_services([]), [])


# ============================================================================
# _filter_auto_disabled_grace tests
# ============================================================================

class TestFilterAutoDisabledGrace(unittest.TestCase):

    def _make_service(self, timeout=60):
        svc = Mock()
        svc.config = Mock()
        svc.config.get_config_value = Mock(side_effect=lambda key: {
            'AUTO_DISABLE_TIMEOUT': timeout,
        }.get(key, Mock()))
        svc.auto_disable_lock = threading.Lock()
        svc.auto_disable_first_seen = defaultdict(float)
        return svc

    def test_empty_returns_empty_without_touching_lock(self):
        svc = Mock()  # no real lock -- must not be touched
        self.assertEqual(instanceha._filter_auto_disabled_grace(svc, []), [])

    def test_first_sighting_records_and_waits(self):
        service = self._make_service()
        host = _make_svc()
        result = instanceha._filter_auto_disabled_grace(service, [host])
        self.assertEqual(result, [])
        self.assertIn('host-1', service.auto_disable_first_seen)

    def test_returns_host_after_grace_period(self):
        service = self._make_service(timeout=60)
        host = _make_svc()
        # Pretend it was first seen well beyond the timeout
        service.auto_disable_first_seen['host-1'] = time.monotonic() - 120
        result = instanceha._filter_auto_disabled_grace(service, [host])
        self.assertEqual([s.host for s in result], ['host-1.example.com'])

    def test_still_within_grace_period_waits(self):
        service = self._make_service(timeout=60)
        host = _make_svc()
        service.auto_disable_first_seen['host-1'] = time.monotonic() - 5
        result = instanceha._filter_auto_disabled_grace(service, [host])
        self.assertEqual(result, [])

    def test_recovered_host_is_pruned(self):
        """A host no longer auto-disabled is dropped so a re-occurrence restarts
        the grace clock."""
        service = self._make_service()
        service.auto_disable_first_seen['gone'] = time.monotonic() - 200
        # 'gone' is absent from this cycle's candidates -> pruned
        instanceha._filter_auto_disabled_grace(service, [_make_svc(host='other')])
        self.assertNotIn('gone', service.auto_disable_first_seen)
        self.assertIn('other', service.auto_disable_first_seen)


# ============================================================================
# _admit_stale_services integration for auto-disabled hosts
# ============================================================================

class TestAdmitAutoDisabled(unittest.TestCase):

    def _make_service(self, **cfg):
        defaults = {
            'DISABLED': False, 'THRESHOLD': 50, 'CHECK_KDUMP': False,
            'CHECK_HEARTBEAT': False, 'WORKERS': 2, 'POLL': 5,
            'FENCING_TIMEOUT': 30, 'EVACUATION_TIMEOUT': 300,
            'TAGGED_IMAGES': False, 'TAGGED_FLAVORS': False,
            'TAGGED_AGGREGATES': False, 'RESERVED_HOSTS': False,
            'EVACUATION_STAGGER': 0, 'EVACUATION_MAX_THREADS': 32,
            'MAX_HOSTS_PER_CYCLE': 10, 'AUTO_DISABLE_TIMEOUT': 60,
        }
        defaults.update(cfg)
        svc = Mock()
        svc.config = Mock()
        svc.config.get_config_value = Mock(side_effect=lambda key: defaults.get(key, Mock()))
        svc.processing_lock = threading.Lock()
        svc.hosts_processing = defaultdict(float)
        svc.auto_disable_lock = threading.Lock()
        svc.auto_disable_first_seen = defaultdict(float)
        svc.refresh_evacuable_cache = Mock()
        svc.get_hosts_with_servers_cached = Mock(return_value={'host-1': ['server-1']})
        svc.filter_hosts_with_servers = Mock(side_effect=lambda nodes, cache: list(nodes))
        svc.processing_executor = Mock()
        return svc

    def _healthy_cluster(self, down_host):
        """A services list with several healthy hosts plus the impacted one, so the
        cluster-wide THRESHOLD gate does not block evacuation."""
        healthy = [_make_svc(host=f'ok-{i}', status='enabled', state='up')
                   for i in range(4)]
        return healthy + [down_host]

    def test_auto_disabled_submitted_after_grace(self):
        service = self._make_service()
        auto = _make_svc(host='host-1')
        # Past grace period
        service.auto_disable_first_seen['host-1'] = time.monotonic() - 120
        conn = Mock()
        services = self._healthy_cluster(auto)
        with patch('instanceha._emit_k8s_event'), \
             patch('instanceha._check_critical_services', return_value=(True, "")):
            instanceha._admit_stale_services(conn, service, services, [], [], auto_disabled=[auto])
        service.processing_executor.submit.assert_called()
        submitted_hosts = [c.args[1].host for c in service.processing_executor.submit.call_args_list]
        self.assertIn('host-1', submitted_hosts)

    def test_auto_disabled_not_submitted_during_grace(self):
        service = self._make_service()
        auto = _make_svc(host='host-1')  # first sighting -> waits
        conn = Mock()
        services = self._healthy_cluster(auto)
        with patch('instanceha._emit_k8s_event'), \
             patch('instanceha._check_critical_services', return_value=(True, "")):
            instanceha._admit_stale_services(conn, service, services, [], [], auto_disabled=[auto])
        service.processing_executor.submit.assert_not_called()

    def test_auto_disabled_bypasses_heartbeat_skip(self):
        """An auto-disabled host still sending heartbeats is fenced anyway.

        Heartbeat filtering targets stale-detected hosts; auto-disabled hosts are
        merged in after that gate, so a live heartbeat must not spare them.
        """
        service = self._make_service(CHECK_HEARTBEAT=True)
        auto = _make_svc(host='host-1')
        service.auto_disable_first_seen['host-1'] = time.monotonic() - 120
        conn = Mock()
        services = self._healthy_cluster(auto)
        with patch('instanceha._emit_k8s_event'), \
             patch('instanceha._check_critical_services', return_value=(True, "")), \
             patch('instanceha._filter_reachable_hosts', return_value=([], [], False)) as mock_hb:
            instanceha._admit_stale_services(conn, service, services, [], [], auto_disabled=[auto])
            # Heartbeat filter is only applied to stale compute_nodes (empty here),
            # never to the auto-disabled set.
            for call_args in mock_hb.call_args_list:
                self.assertNotIn(auto, call_args.args[1])
        service.processing_executor.submit.assert_called()


if __name__ == '__main__':
    unittest.main()
