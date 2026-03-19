"""
Tests for previously uncovered or under-covered functions in InstanceHA.

Covers:
- _try_validate: validation orchestrator with error handling
- _validate_fencing_params: required parameter presence check
- _validate_fencing_inputs: injection prevention for fencing
- _execute_ipmi_fence: IPMI power control with retry logic
- _fence_noop, _fence_ipmi, _fence_redfish, _fence_bmh: agent dispatchers
- _is_service_resume_candidate: evacuation resume eligibility
- _aggregate_ids: aggregate filtering for host selection
- _traditional_evacuate: fire-and-forget evacuation logic
- _execute_step: processing step error handling wrapper
- _build_redfish_url: URL construction error cases
"""

import os
import sys
import unittest
import logging
from unittest.mock import Mock, patch, MagicMock, call

# Mock OpenStack dependencies before importing instanceha
if 'novaclient' not in sys.modules:
    sys.modules['novaclient'] = MagicMock()
    sys.modules['novaclient.client'] = MagicMock()
    class NotFound(Exception):
        pass
    class Conflict(Exception):
        pass
    class Forbidden(Exception):
        pass
    class Unauthorized(Exception):
        pass
    novaclient_exceptions = MagicMock()
    novaclient_exceptions.NotFound = NotFound
    novaclient_exceptions.Conflict = Conflict
    novaclient_exceptions.Forbidden = Forbidden
    novaclient_exceptions.Unauthorized = Unauthorized
    sys.modules['novaclient.exceptions'] = novaclient_exceptions

if 'keystoneauth1' not in sys.modules:
    sys.modules['keystoneauth1'] = MagicMock()
    sys.modules['keystoneauth1.loading'] = MagicMock()
    sys.modules['keystoneauth1.session'] = MagicMock()
    sys.modules['keystoneauth1.exceptions'] = MagicMock()
    sys.modules['keystoneauth1.exceptions.discovery'] = MagicMock()

test_dir = os.path.dirname(os.path.abspath(__file__))
instanceha_path = os.path.join(test_dir, '../../templates/instanceha/bin/')
sys.path.insert(0, os.path.abspath(instanceha_path))

logging.getLogger().setLevel(logging.CRITICAL)
import instanceha
logging.getLogger().setLevel(logging.CRITICAL)


# ============================================================================
# _try_validate tests
# ============================================================================

class TestTryValidate(unittest.TestCase):
    """Tests for the _try_validate helper."""

    def test_returns_true_on_successful_validation(self):
        result = instanceha._try_validate(lambda: True, "err", "ctx")
        self.assertTrue(result)

    def test_returns_false_on_failed_validation(self):
        result = instanceha._try_validate(lambda: False, "err", "ctx")
        self.assertFalse(result)

    def test_catches_value_error_returns_false(self):
        result = instanceha._try_validate(lambda: (_ for _ in ()).throw(ValueError("bad")), "err", "ctx")
        self.assertFalse(result)

    def test_catches_type_error_returns_false(self):
        result = instanceha._try_validate(lambda: (_ for _ in ()).throw(TypeError("bad")), "err", "ctx")
        self.assertFalse(result)

    def test_catches_attribute_error_returns_false(self):
        result = instanceha._try_validate(lambda: (_ for _ in ()).throw(AttributeError("bad")), "err", "ctx")
        self.assertFalse(result)

    def test_does_not_catch_runtime_error(self):
        with self.assertRaises(RuntimeError):
            instanceha._try_validate(lambda: (_ for _ in ()).throw(RuntimeError("bad")), "err", "ctx")

    def test_log_error_true_logs_on_exception(self):
        with patch('instanceha.logging') as mock_log:
            instanceha._try_validate(lambda: (_ for _ in ()).throw(ValueError("oops")),
                                     "Validation failed", "test_ctx", log_error=True)
            mock_log.error.assert_called()

    def test_log_error_false_suppresses_log(self):
        with patch('instanceha.logging') as mock_log:
            instanceha._try_validate(lambda: (_ for _ in ()).throw(ValueError("oops")),
                                     "Validation failed", "test_ctx", log_error=False)
            mock_log.error.assert_not_called()


# ============================================================================
# _validate_fencing_params tests
# ============================================================================

class TestValidateFencingParams(unittest.TestCase):
    """Tests for _validate_fencing_params."""

    def test_all_params_present_returns_true(self):
        data = {"ipaddr": "10.0.0.1", "login": "admin", "passwd": "secret"}
        result = instanceha._validate_fencing_params(data, ["ipaddr", "login", "passwd"], "IPMI", "host1")
        self.assertTrue(result)

    def test_missing_param_returns_false(self):
        data = {"ipaddr": "10.0.0.1"}
        result = instanceha._validate_fencing_params(data, ["ipaddr", "login", "passwd"], "IPMI", "host1")
        self.assertFalse(result)

    def test_all_params_missing_returns_false(self):
        data = {}
        result = instanceha._validate_fencing_params(data, ["ipaddr", "login"], "IPMI", "host1")
        self.assertFalse(result)

    def test_extra_params_ignored(self):
        data = {"ipaddr": "10.0.0.1", "login": "admin", "passwd": "secret", "extra": "value"}
        result = instanceha._validate_fencing_params(data, ["ipaddr", "login"], "IPMI", "host1")
        self.assertTrue(result)

    def test_empty_required_list_returns_true(self):
        data = {"ipaddr": "10.0.0.1"}
        result = instanceha._validate_fencing_params(data, [], "IPMI", "host1")
        self.assertTrue(result)


# ============================================================================
# _validate_fencing_inputs tests
# ============================================================================

class TestValidateFencingInputs(unittest.TestCase):
    """Tests for _validate_fencing_inputs (injection prevention)."""

    def test_valid_ip_and_port_returns_true(self):
        data = {"ipaddr": "192.168.1.10", "ipport": "623", "login": "admin"}
        result = instanceha._validate_fencing_inputs(data, "IPMI")
        self.assertTrue(result)

    def test_invalid_ip_returns_false(self):
        data = {"ipaddr": "not-an-ip", "ipport": "623"}
        result = instanceha._validate_fencing_inputs(data, "IPMI")
        self.assertFalse(result)

    def test_invalid_port_returns_false(self):
        data = {"ipaddr": "192.168.1.10", "ipport": "99999"}
        result = instanceha._validate_fencing_inputs(data, "IPMI")
        self.assertFalse(result)

    def test_no_optional_fields_still_validates_ip(self):
        data = {"ipaddr": "10.0.0.1"}
        result = instanceha._validate_fencing_inputs(data, "Redfish")
        self.assertTrue(result)

    def test_ipv6_address_accepted(self):
        data = {"ipaddr": "::1"}
        result = instanceha._validate_fencing_inputs(data, "IPMI")
        self.assertTrue(result)

    def test_port_zero_returns_false(self):
        data = {"ipaddr": "10.0.0.1", "ipport": "0"}
        result = instanceha._validate_fencing_inputs(data, "IPMI")
        self.assertFalse(result)

    def test_port_negative_returns_false(self):
        data = {"ipaddr": "10.0.0.1", "ipport": "-1"}
        result = instanceha._validate_fencing_inputs(data, "IPMI")
        self.assertFalse(result)


# ============================================================================
# _execute_ipmi_fence tests
# ============================================================================

class TestExecuteIpmiFence(unittest.TestCase):
    """Tests for _execute_ipmi_fence (IPMI power control with retries)."""

    def _make_fencing_data(self):
        return {
            "ipaddr": "192.168.1.10",
            "ipport": "623",
            "login": "admin",
            "passwd": "secret",
        }

    @patch('instanceha.subprocess.run')
    def test_power_on_success(self, mock_run):
        """Successful power on returns True."""
        mock_run.return_value = Mock(returncode=0)
        result = instanceha._execute_ipmi_fence("host1", "on", self._make_fencing_data(), 30)
        self.assertTrue(result)
        mock_run.assert_called_once()
        cmd = mock_run.call_args[0][0]
        self.assertIn("power", cmd)
        self.assertIn("on", cmd)

    @patch('instanceha.subprocess.run')
    def test_password_passed_via_env_not_cmdline(self, mock_run):
        """IPMI password passed via environment variable, not command line."""
        mock_run.return_value = Mock(returncode=0)
        instanceha._execute_ipmi_fence("host1", "on", self._make_fencing_data(), 30)
        env = mock_run.call_args[1]['env']
        self.assertEqual(env['IPMITOOL_PASSWORD'], 'secret')
        cmd = mock_run.call_args[0][0]
        self.assertNotIn("secret", cmd)

    @patch('instanceha._wait_for_power_off', return_value=True)
    @patch('instanceha.subprocess.run')
    def test_power_off_waits_for_state(self, mock_run, mock_wait):
        """Power off calls _wait_for_power_off after command succeeds."""
        mock_run.return_value = Mock(returncode=0)
        result = instanceha._execute_ipmi_fence("host1", "off", self._make_fencing_data(), 30)
        self.assertTrue(result)
        mock_wait.assert_called_once()

    @patch('instanceha.time.sleep')
    @patch('instanceha.subprocess.run')
    def test_retries_on_timeout(self, mock_run, mock_sleep):
        """Retries on subprocess.TimeoutExpired."""
        mock_run.side_effect = [
            __import__('subprocess').TimeoutExpired(cmd="ipmitool", timeout=30),
            Mock(returncode=0),
        ]
        result = instanceha._execute_ipmi_fence("host1", "on", self._make_fencing_data(), 30)
        self.assertTrue(result)
        self.assertEqual(mock_run.call_count, 2)

    @patch('instanceha.time.sleep')
    @patch('instanceha.subprocess.run')
    def test_retries_on_called_process_error(self, mock_run, mock_sleep):
        """Retries on subprocess.CalledProcessError."""
        mock_run.side_effect = [
            __import__('subprocess').CalledProcessError(returncode=1, cmd="ipmitool"),
            Mock(returncode=0),
        ]
        result = instanceha._execute_ipmi_fence("host1", "on", self._make_fencing_data(), 30)
        self.assertTrue(result)
        self.assertEqual(mock_run.call_count, 2)

    @patch('instanceha.time.sleep')
    @patch('instanceha.subprocess.run')
    def test_all_retries_exhausted_returns_false(self, mock_run, mock_sleep):
        """Returns False after all retries exhausted."""
        mock_run.side_effect = __import__('subprocess').TimeoutExpired(cmd="ipmitool", timeout=30)
        result = instanceha._execute_ipmi_fence("host1", "on", self._make_fencing_data(), 30)
        self.assertFalse(result)
        self.assertEqual(mock_run.call_count, instanceha.MAX_FENCING_RETRIES)


# ============================================================================
# _fence_noop tests
# ============================================================================

class TestFenceNoop(unittest.TestCase):
    """Tests for _fence_noop agent."""

    def test_returns_true(self):
        service = Mock()
        result = instanceha._fence_noop("host1", "off", {}, service)
        self.assertTrue(result)

    def test_returns_true_for_any_action(self):
        service = Mock()
        for action in ["off", "on", "status"]:
            result = instanceha._fence_noop("host1", action, {}, service)
            self.assertTrue(result)


# ============================================================================
# _fence_ipmi tests
# ============================================================================

class TestFenceIpmi(unittest.TestCase):
    """Tests for _fence_ipmi dispatcher."""

    def _make_service(self):
        svc = Mock()
        svc.config.get_config_value.return_value = 30
        return svc

    def test_missing_params_returns_false(self):
        """Returns False when required IPMI params are missing."""
        result = instanceha._fence_ipmi("host1", "off", {"ipaddr": "10.0.0.1"}, self._make_service())
        self.assertFalse(result)

    def test_invalid_ip_returns_false(self):
        """Returns False when IP address is invalid."""
        data = {"ipaddr": "not-an-ip", "ipport": "623", "login": "admin", "passwd": "secret"}
        result = instanceha._fence_ipmi("host1", "off", data, self._make_service())
        self.assertFalse(result)

    @patch('instanceha._execute_ipmi_fence', return_value=True)
    def test_valid_params_calls_execute(self, mock_exec):
        """Valid params delegates to _execute_ipmi_fence."""
        data = {"ipaddr": "192.168.1.10", "ipport": "623", "login": "admin", "passwd": "secret"}
        result = instanceha._fence_ipmi("host1", "off", data, self._make_service())
        self.assertTrue(result)
        mock_exec.assert_called_once_with("host1", "off", data, 30)

    @patch('instanceha._execute_ipmi_fence', return_value=True)
    def test_uses_custom_timeout_from_fencing_data(self, mock_exec):
        """Uses timeout from fencing_data if provided."""
        data = {"ipaddr": "192.168.1.10", "ipport": "623", "login": "admin", "passwd": "secret", "timeout": 60}
        instanceha._fence_ipmi("host1", "off", data, self._make_service())
        mock_exec.assert_called_once_with("host1", "off", data, 60)

    @patch('instanceha._execute_ipmi_fence', return_value=False)
    def test_returns_false_when_execute_fails(self, mock_exec):
        """Returns False when _execute_ipmi_fence fails."""
        data = {"ipaddr": "192.168.1.10", "ipport": "623", "login": "admin", "passwd": "secret"}
        result = instanceha._fence_ipmi("host1", "off", data, self._make_service())
        self.assertFalse(result)


# ============================================================================
# _fence_redfish tests
# ============================================================================

class TestFenceRedfish(unittest.TestCase):
    """Tests for _fence_redfish dispatcher."""

    def _make_service(self):
        svc = Mock()
        svc.config.get_config_value.return_value = 30
        svc.config = Mock()
        svc.config.get_config_value.return_value = 30
        return svc

    def test_missing_params_returns_false(self):
        """Returns False when required Redfish params are missing."""
        data = {"ipaddr": "10.0.0.1"}
        result = instanceha._fence_redfish("host1", "off", data, self._make_service())
        self.assertFalse(result)

    def test_invalid_ip_returns_false(self):
        """Returns False when IP is invalid."""
        data = {"ipaddr": "bad-ip", "login": "root", "passwd": "secret"}
        result = instanceha._fence_redfish("host1", "off", data, self._make_service())
        self.assertFalse(result)

    @patch('instanceha._redfish_reset', return_value=True)
    @patch('instanceha._build_redfish_url', return_value="https://10.0.0.1:443/redfish/v1/Systems/System.Embedded.1")
    def test_power_off_maps_to_force_off(self, mock_url, mock_reset):
        """Action 'off' is mapped to Redfish 'ForceOff'."""
        data = {"ipaddr": "192.168.1.10", "login": "root", "passwd": "secret"}
        svc = self._make_service()
        instanceha._fence_redfish("host1", "off", data, svc)
        mock_reset.assert_called_once()
        self.assertEqual(mock_reset.call_args[0][3], 30)  # timeout
        self.assertEqual(mock_reset.call_args[0][4], "ForceOff")  # action

    @patch('instanceha._redfish_reset', return_value=True)
    @patch('instanceha._build_redfish_url', return_value="https://10.0.0.1:443/redfish/v1/Systems/System.Embedded.1")
    def test_power_on_maps_to_on(self, mock_url, mock_reset):
        """Action 'on' is mapped to Redfish 'On'."""
        data = {"ipaddr": "192.168.1.10", "login": "root", "passwd": "secret"}
        svc = self._make_service()
        instanceha._fence_redfish("host1", "on", data, svc)
        self.assertEqual(mock_reset.call_args[0][4], "On")

    @patch('instanceha._build_redfish_url', return_value=None)
    def test_invalid_url_returns_false(self, mock_url):
        """Returns False when URL construction fails."""
        data = {"ipaddr": "192.168.1.10", "login": "root", "passwd": "secret"}
        result = instanceha._fence_redfish("host1", "off", data, self._make_service())
        self.assertFalse(result)


# ============================================================================
# _fence_bmh tests
# ============================================================================

class TestFenceBmh(unittest.TestCase):
    """Tests for _fence_bmh dispatcher."""

    def test_missing_params_returns_false(self):
        """Returns False when required BMH params are missing."""
        data = {"token": "tok123"}
        svc = Mock()
        result = instanceha._fence_bmh("host1", "off", data, svc)
        self.assertFalse(result)

    @patch('instanceha._bmh_fence', return_value=True)
    def test_valid_params_calls_bmh_fence(self, mock_bmh):
        """Valid params delegates to _bmh_fence."""
        data = {"token": "tok123", "host": "metal3-0", "namespace": "openshift-machine-api"}
        svc = Mock()
        result = instanceha._fence_bmh("host1", "off", data, svc)
        self.assertTrue(result)
        mock_bmh.assert_called_once_with("tok123", "openshift-machine-api", "metal3-0", "off", svc)

    @patch('instanceha._bmh_fence', return_value=False)
    def test_returns_false_when_bmh_fence_fails(self, mock_bmh):
        """Returns False when underlying _bmh_fence fails."""
        data = {"token": "tok123", "host": "metal3-0", "namespace": "openshift-machine-api"}
        svc = Mock()
        result = instanceha._fence_bmh("host1", "off", data, svc)
        self.assertFalse(result)


# ============================================================================
# _is_service_resume_candidate tests
# ============================================================================

class TestIsServiceResumeCandidate(unittest.TestCase):
    """Tests for _is_service_resume_candidate."""

    def _make_svc(self, forced_down=True, state='down', status='disabled',
                  disabled_reason='instanceha evacuation: 2024-01-01T00:00:00'):
        svc = Mock()
        svc.forced_down = forced_down
        svc.state = state
        svc.status = status
        svc.disabled_reason = disabled_reason
        return svc

    def test_valid_resume_candidate(self):
        """Service with evacuation marker, forced_down, down, disabled is a candidate."""
        svc = self._make_svc()
        self.assertTrue(instanceha._is_service_resume_candidate(svc))

    def test_not_forced_down_is_not_candidate(self):
        svc = self._make_svc(forced_down=False)
        self.assertFalse(instanceha._is_service_resume_candidate(svc))

    def test_state_up_is_not_candidate(self):
        svc = self._make_svc(state='up')
        self.assertFalse(instanceha._is_service_resume_candidate(svc))

    def test_status_enabled_is_not_candidate(self):
        svc = self._make_svc(status='enabled')
        self.assertFalse(instanceha._is_service_resume_candidate(svc))

    def test_complete_marker_excludes_candidate(self):
        svc = self._make_svc(disabled_reason='instanceha evacuation complete: 2024-01-01T00:00:00')
        self.assertFalse(instanceha._is_service_resume_candidate(svc))

    def test_failed_marker_excludes_candidate(self):
        svc = self._make_svc(disabled_reason='evacuation FAILED: 2024-01-01T00:00:00')
        self.assertFalse(instanceha._is_service_resume_candidate(svc))

    def test_no_evacuation_marker_not_candidate(self):
        svc = self._make_svc(disabled_reason='some other reason')
        self.assertFalse(instanceha._is_service_resume_candidate(svc))

    def test_empty_disabled_reason_not_candidate(self):
        svc = self._make_svc(disabled_reason='')
        self.assertFalse(instanceha._is_service_resume_candidate(svc))

    def test_kdump_evacuation_is_candidate(self):
        """Kdump evacuation marker is still a valid resume candidate."""
        svc = self._make_svc(disabled_reason='instanceha evacuation (kdump): 2024-01-01T00:00:00')
        self.assertTrue(instanceha._is_service_resume_candidate(svc))

    def test_no_disabled_reason_attr_not_candidate(self):
        """Service without disabled_reason attribute is not a candidate."""
        svc = Mock()
        svc.forced_down = True
        svc.state = 'down'
        svc.status = 'disabled'
        # delattr to ensure getattr returns ''
        del svc.disabled_reason
        svc.disabled_reason = ''
        self.assertFalse(instanceha._is_service_resume_candidate(svc))


# ============================================================================
# _aggregate_ids tests
# ============================================================================

class TestAggregateIds(unittest.TestCase):
    """Tests for _aggregate_ids."""

    def test_returns_matching_aggregate_ids(self):
        agg1 = Mock(id=1, hosts=['host1', 'host2'])
        agg2 = Mock(id=2, hosts=['host2', 'host3'])
        agg3 = Mock(id=3, hosts=['host1'])
        conn = Mock()
        conn.aggregates.list.return_value = [agg1, agg2, agg3]
        service = Mock(host='host1')

        result = instanceha._aggregate_ids(conn, service)
        self.assertEqual(result, [1, 3])

    def test_no_matching_aggregates(self):
        agg1 = Mock(id=1, hosts=['host2', 'host3'])
        conn = Mock()
        conn.aggregates.list.return_value = [agg1]
        service = Mock(host='host1')

        result = instanceha._aggregate_ids(conn, service)
        self.assertEqual(result, [])

    def test_empty_aggregates_list(self):
        conn = Mock()
        conn.aggregates.list.return_value = []
        service = Mock(host='host1')

        result = instanceha._aggregate_ids(conn, service)
        self.assertEqual(result, [])

    def test_host_in_all_aggregates(self):
        agg1 = Mock(id=1, hosts=['host1'])
        agg2 = Mock(id=2, hosts=['host1'])
        conn = Mock()
        conn.aggregates.list.return_value = [agg1, agg2]
        service = Mock(host='host1')

        result = instanceha._aggregate_ids(conn, service)
        self.assertEqual(result, [1, 2])


# ============================================================================
# _traditional_evacuate tests
# ============================================================================

class TestTraditionalEvacuate(unittest.TestCase):
    """Tests for _traditional_evacuate (fire-and-forget)."""

    def _make_server(self, server_id):
        s = Mock()
        s.id = server_id
        return s

    @patch('instanceha._server_evacuate')
    def test_all_succeed_returns_true(self, mock_evac):
        """Returns True when all evacuations succeed."""
        mock_evac.return_value = Mock(accepted=True, uuid='srv1', reason='ok')
        servers = [self._make_server('srv1'), self._make_server('srv2')]
        conn = Mock()

        result = instanceha._traditional_evacuate(conn, servers, 'host1')
        self.assertTrue(result)
        self.assertEqual(mock_evac.call_count, 2)

    @patch('instanceha._server_evacuate')
    def test_partial_failure_returns_false(self, mock_evac):
        """Returns False when any evacuation fails."""
        mock_evac.side_effect = [
            Mock(accepted=True, uuid='srv1', reason='ok'),
            Mock(accepted=False, uuid='srv2', reason='error'),
        ]
        servers = [self._make_server('srv1'), self._make_server('srv2')]
        conn = Mock()

        result = instanceha._traditional_evacuate(conn, servers, 'host1')
        self.assertFalse(result)

    @patch('instanceha._server_evacuate')
    def test_empty_server_list_returns_true(self, mock_evac):
        """Returns True for empty server list (nothing to evacuate)."""
        result = instanceha._traditional_evacuate(Mock(), [], 'host1')
        self.assertTrue(result)
        mock_evac.assert_not_called()

    @patch('instanceha._server_evacuate')
    def test_server_without_id_returns_false(self, mock_evac):
        """Server without 'id' attribute is logged as error, returns False."""
        server = Mock(spec=[])  # spec=[] means no attributes
        server.to_dict = Mock(return_value={'name': 'bad_server'})
        conn = Mock()

        result = instanceha._traditional_evacuate(conn, [server], 'host1')
        self.assertFalse(result)
        mock_evac.assert_not_called()

    @patch('instanceha._server_evacuate')
    def test_target_host_passed_to_evacuate(self, mock_evac):
        """target_host parameter is forwarded to _server_evacuate."""
        mock_evac.return_value = Mock(accepted=True, uuid='srv1', reason='ok')
        servers = [self._make_server('srv1')]
        conn = Mock()

        instanceha._traditional_evacuate(conn, servers, 'host1', target_host='reserved-01')
        mock_evac.assert_called_once_with(conn, 'srv1', target_host='reserved-01')

    @patch('instanceha._server_evacuate')
    def test_all_fail_returns_false(self, mock_evac):
        """Returns False when all evacuations fail."""
        mock_evac.return_value = Mock(accepted=False, uuid='srv1', reason='error')
        servers = [self._make_server('srv1'), self._make_server('srv2')]
        conn = Mock()

        result = instanceha._traditional_evacuate(conn, servers, 'host1')
        self.assertFalse(result)


# ============================================================================
# _execute_step tests
# ============================================================================

class TestExecuteStep(unittest.TestCase):
    """Tests for _execute_step error handling wrapper."""

    def test_returns_result_on_success(self):
        result = instanceha._execute_step("Fencing", lambda: True, "host1")
        self.assertTrue(result)

    def test_returns_false_on_step_failure(self):
        result = instanceha._execute_step("Fencing", lambda: False, "host1")
        self.assertFalse(result)

    def test_returns_none_on_step_returning_none(self):
        result = instanceha._execute_step("Fencing", lambda: None, "host1")
        self.assertIsNone(result)

    def test_catches_exception_returns_false(self):
        def bad_step():
            raise RuntimeError("step exploded")
        result = instanceha._execute_step("Fencing", bad_step, "host1")
        self.assertFalse(result)

    def test_passes_args_and_kwargs(self):
        def step_func(a, b, key=None):
            return a + b + (key or 0)
        result = instanceha._execute_step("Add", step_func, "host1", 1, 2, key=3)
        self.assertEqual(result, 6)

    def test_logs_error_on_failure(self):
        with patch('instanceha.logging') as mock_log:
            instanceha._execute_step("Fencing", lambda: False, "host1")
            mock_log.error.assert_called()

    def test_logs_error_on_exception(self):
        with patch('instanceha.logging') as mock_log:
            instanceha._execute_step("Fencing", lambda: (_ for _ in ()).throw(RuntimeError("boom")), "host1")
            mock_log.error.assert_called()


# ============================================================================
# _build_redfish_url edge cases
# ============================================================================

class TestBuildRedfishUrl(unittest.TestCase):
    """Tests for _build_redfish_url edge cases."""

    def test_valid_tls_url(self):
        data = {"ipaddr": "192.168.1.10", "ipport": "443", "tls": "true", "uuid": "System.Embedded.1"}
        url = instanceha._build_redfish_url(data)
        self.assertIsNotNone(url)
        self.assertTrue(url.startswith("https://"))

    def test_valid_no_tls_url(self):
        data = {"ipaddr": "192.168.1.10", "ipport": "8080", "tls": "false", "uuid": "System.Embedded.1"}
        url = instanceha._build_redfish_url(data)
        self.assertIsNotNone(url)
        self.assertTrue(url.startswith("http://"))

    def test_defaults_port_443(self):
        data = {"ipaddr": "192.168.1.10"}
        url = instanceha._build_redfish_url(data)
        self.assertIn(":443", url)

    def test_defaults_uuid(self):
        data = {"ipaddr": "192.168.1.10"}
        url = instanceha._build_redfish_url(data)
        self.assertIn("System.Embedded.1", url)

    def test_missing_ipaddr_raises(self):
        with self.assertRaises(KeyError):
            instanceha._build_redfish_url({})

    def test_localhost_ip_blocked(self):
        """SSRF: localhost IP is blocked by URL validation."""
        data = {"ipaddr": "127.0.0.1", "tls": "true"}
        url = instanceha._build_redfish_url(data)
        self.assertIsNone(url)

    def test_link_local_ip_blocked(self):
        """SSRF: link-local IP is blocked by URL validation."""
        data = {"ipaddr": "169.254.1.1", "tls": "true"}
        url = instanceha._build_redfish_url(data)
        self.assertIsNone(url)

    def test_custom_uuid(self):
        data = {"ipaddr": "192.168.1.10", "uuid": "Custom.System.1"}
        url = instanceha._build_redfish_url(data)
        self.assertIn("Custom.System.1", url)


if __name__ == '__main__':
    unittest.main()
