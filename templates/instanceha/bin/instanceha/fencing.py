import json
import logging
import os
import subprocess
import sys as _sys
import time
from typing import Optional

import requests
import requests.exceptions

from .models import FENCING_RETRY_DELAY_SECONDS, MAX_FENCING_RETRIES
from .validation import (
    _safe_log_exception,
    _sanitize_url,
    _extract_hostname,
    validate_input,
    validate_inputs,
)

# Package-level reference for mock-patchability: tests patch 'instanceha.X'
# so runtime calls must go through the package namespace.
_pkg = _sys.modules[__package__]


def _make_ssl_request(method: str, url: str, auth: tuple, timeout: int,
                      config_mgr, **kwargs):
    """Make HTTP request with SSL configuration from config manager."""
    ssl_config = config_mgr.get_requests_ssl_config()
    request_kwargs = {'auth': auth, 'timeout': timeout, **kwargs}

    if isinstance(ssl_config, tuple):
        request_kwargs['cert'] = ssl_config
    else:
        request_kwargs['verify'] = ssl_config

    return getattr(requests, method)(url, **request_kwargs)


def _wait_for_power_off(check_func, timeout: int,
                       host_identifier: str, expected_state: str, agent_type: str) -> bool:
    """Wait for host to power off by polling power state."""
    for _ in range(timeout):
        time.sleep(1)
        power_state = check_func()
        if power_state == expected_state:
            logging.info("%s power off successful for %s", agent_type, host_identifier)
            return True
    logging.error('Power off of %s timed out', host_identifier)
    return False


def _build_redfish_url(fencing_data: dict) -> Optional[str]:
    """Build and validate Redfish URL from fencing data."""
    ip = fencing_data["ipaddr"]
    port = fencing_data.get("ipport", "443")
    uuid = fencing_data.get("uuid", "System.Embedded.1")
    tls = fencing_data.get("tls", "false").lower() == "true"
    protocol = "https" if tls else "http"
    url = f'{protocol}://{ip}:{port}/redfish/v1/Systems/{uuid}'

    if not validate_input(url, 'url', "Redfish fencing"):
        logging.error("Redfish fencing failed: invalid URL")
        return None
    return url


def _get_power_state(agent_type: str, **kwargs) -> Optional[str]:
    """Unified power state retrieval interface."""
    if agent_type == 'redfish':
        url, user, passwd, timeout, config_mgr = (
            kwargs['url'], kwargs['user'], kwargs['passwd'],
            kwargs['timeout'], kwargs['config_mgr']
        )

        if not validate_input(url, 'url', "Redfish power state check"):
            logging.error("Redfish power state check failed: invalid URL")
            return None

        try:
            response = _pkg._make_ssl_request('get', url, (user, passwd), timeout, config_mgr)
            if response.status_code == 200:
                data = response.json()
                return data.get('PowerState', '').upper()
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
            logging.debug('Power state check network error: %s', type(e).__name__)
        except Exception as e:
            _pkg._safe_log_exception('Failed to get power state', e)

    elif agent_type == 'ipmi':
        ip, port, user, passwd, timeout = (
            kwargs['ip'], kwargs['port'], kwargs['user'],
            kwargs['passwd'], kwargs['timeout']
        )

        try:
            env = os.environ.copy()
            env['IPMITOOL_PASSWORD'] = passwd
            cmd = ['ipmitool', '-I', 'lanplus', '-H', ip, '-U', user, '-E', '-p', port, 'power', 'status']
            cmd_output = subprocess.run(cmd, timeout=timeout, capture_output=True, text=True, check=True, env=env)
            return cmd_output.stdout.strip().upper()
        except subprocess.TimeoutExpired:
            logging.error('Failed to get IPMI power state: timeout expired')
        except subprocess.CalledProcessError as e:
            logging.error('Failed to get IPMI power state: command failed with return code %d' % e.returncode)

    return None


def _redfish_reset(url, user, passwd, timeout, action, config_mgr):
    """Perform a Redfish reset operation on a computer system."""
    if not all([url, user, passwd, action]):
        logging.error("Redfish reset failed: missing required parameters")
        return False

    if not _pkg.validate_inputs({'url': url, 'power_action': action}, "Redfish reset"):
        logging.error("Redfish reset failed: invalid inputs")
        return False

    timeout = max(5, min(300, int(timeout or 30)))
    url = url.rstrip('/')
    reset_url = f"{url}/Actions/ComputerSystem.Reset"
    safe_url = _sanitize_url(url)

    payload = {"ResetType": action}
    headers = {'Content-Type': 'application/json', 'Accept': 'application/json'}

    for attempt in range(MAX_FENCING_RETRIES):
        try:
            response = _pkg._make_ssl_request('post', reset_url, (user, passwd), timeout,
                                        config_mgr, json=payload, headers=headers)

            if response.status_code in [200, 202, 204]:
                if action == "ForceOff":
                    return _wait_for_power_off(
                        lambda: _pkg._get_power_state('redfish', url=url, user=user, passwd=passwd, timeout=timeout, config_mgr=config_mgr),
                        timeout, safe_url, 'OFF', 'Redfish')
                else:
                    logging.debug("Redfish reset successful: %s on %s", action, safe_url)
                    logging.info("Redfish reset successful: %s", action)
                    return True
            elif response.status_code in [400, 409]:
                power_state = _pkg._get_power_state('redfish', url=url, user=user, passwd=passwd, timeout=timeout, config_mgr=config_mgr)
                if power_state == 'OFF':
                    logging.debug("Redfish reset successful: %s on %s (already off)", action, safe_url)
                    logging.info("Redfish reset successful: %s (already off)", action)
                    return True
                else:
                    logging.error("Redfish reset failed: %s conflict but not OFF (status: %s) for %s", response.status_code, power_state, safe_url)
                    return False
            elif response.status_code in [401, 403]:
                logging.error("Redfish reset failed: authentication error (status %d) for %s", response.status_code, safe_url)
                return False
            elif response.status_code in [500, 502, 503, 504] and attempt < MAX_FENCING_RETRIES - 1:
                logging.warning("Redfish reset failed: server error %d (attempt %d/%d), retrying...",
                              response.status_code, attempt + 1, MAX_FENCING_RETRIES)
                time.sleep(FENCING_RETRY_DELAY_SECONDS * (attempt + 1))
                continue
            else:
                logging.error("Redfish reset failed: status %d for %s", response.status_code, safe_url)
                return False

        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
            if attempt < MAX_FENCING_RETRIES - 1:
                logging.warning("Redfish reset network error (attempt %d/%d), retrying...", attempt + 1, MAX_FENCING_RETRIES)
                time.sleep(FENCING_RETRY_DELAY_SECONDS * (attempt + 1))
                continue
            else:
                _pkg._safe_log_exception("Redfish reset failed for %s" % safe_url, e)
                return False
        except Exception as e:
            _pkg._safe_log_exception("Redfish reset failed for %s" % safe_url, e)
            return False

    return False


def _bmh_fence(token, namespace, host, action, service):
    """Fence a BareMetal Host using Kubernetes API."""
    if not all([token, namespace, host]) or action not in ['on', 'off']:
        logging.error("BMH fencing failed: invalid parameters")
        return False

    if not _pkg.validate_inputs({'k8s_namespace': namespace, 'k8s_resource': host, 'power_action': action}, "BMH fencing"):
        logging.error(f"BMH fencing failed: invalid inputs")
        return False

    base_url = f"https://kubernetes.default.svc/apis/metal3.io/v1alpha1/namespaces/{namespace}/baremetalhosts/{host}"
    headers = {'Authorization': f'Bearer {token}', 'Content-Type': 'application/merge-patch+json'}
    cacert = '/var/run/secrets/kubernetes.io/serviceaccount/ca.crt'

    if not os.path.exists(cacert):
        logging.error("BMH fencing failed: CA certificate not found")
        return False

    try:
        if action == 'off':
            data = {
                "spec": {"online": False},
                "metadata": {"annotations": {"reboot.metal3.io/iha": '{"mode": "hard"}'}}
            }
        else:
            data = {
                "spec": {"online": True},
                "metadata": {"annotations": {"reboot.metal3.io/iha": None}}
            }

        fencing_timeout = service.config.get_config_value('FENCING_TIMEOUT')
        response = requests.patch(f"{base_url}?fieldManager=kubectl-patch",
                                headers=headers, json=data, verify=cacert, timeout=fencing_timeout)
        response.raise_for_status()

        if action == 'off':
            fencing_timeout = service.config.get_config_value('FENCING_TIMEOUT')
            return _pkg._bmh_wait_for_power_off(base_url, headers, cacert, host, fencing_timeout, 3, service)
        else:
            logging.info("BMH power on successful for %s", host)
            return True

    except Exception as e:
        _pkg._safe_log_exception("BMH fencing failed for %s" % host, e)
        return False


def _bmh_wait_for_power_off(get_url, headers, cacert, host, timeout, poll_interval, service):
    """Wait for BareMetal Host to power off by polling its status."""
    fencing_timeout = service.config.get_config_value('FENCING_TIMEOUT')

    logging.debug("Waiting up to %d seconds for %s to power off", timeout, host)

    timeout_end = time.time() + timeout

    with requests.Session() as session:
        while time.time() < timeout_end:
            time.sleep(poll_interval)

            try:
                response = session.get(get_url, headers=headers, verify=cacert, timeout=fencing_timeout)
                response.raise_for_status()

            except requests.exceptions.Timeout:
                logging.warning("Timeout checking power status for %s, continuing to wait", host)
                continue
            except requests.exceptions.HTTPError as e:
                logging.warning("HTTP error checking power status for %s, continuing to wait", host)
                continue
            except requests.exceptions.RequestException as e:
                logging.warning("Request error checking power status for %s, continuing to wait", host)
                continue

            try:
                status_data = response.json()
            except (json.JSONDecodeError, ValueError) as e:
                logging.warning("Invalid JSON response checking power status for %s: %s, continuing to wait", host, e)
                continue

            try:
                power_status = status_data['status']['poweredOn']
                logging.debug("Power status for %s: %s", host, power_status)

                if not power_status:
                    logging.info("BMH power off confirmed for %s", host)
                    return True

            except KeyError as e:
                logging.warning("Missing power status field for %s: %s, continuing to wait", host, e)
                continue
            except Exception as e:
                logging.warning("Error parsing power status for %s: %s, continuing to wait", host, e)
                continue

    logging.error("BMH power off timeout: %s did not power off within %d seconds", host, timeout)
    return False


def _validate_fencing_params(fencing_data, required_params, agent_name, host) -> bool:
    """Validate required fencing parameters are present."""
    missing = [p for p in required_params if p not in fencing_data]
    if missing:
        logging.error("Missing %s params for %s: %s", agent_name, host, missing)
        return False
    return True


def _validate_fencing_inputs(fencing_data, agent_name) -> bool:
    """Validate fencing input parameters to prevent injection attacks."""
    validations = {'ip_address': fencing_data["ipaddr"]}
    if "ipport" in fencing_data:
        validations['port'] = fencing_data["ipport"]
    if "login" in fencing_data:
        validations['username'] = fencing_data["login"]
    return _pkg.validate_inputs(validations, agent_name)


def _execute_ipmi_fence(host, action, fencing_data, timeout) -> bool:
    """Execute IPMI fencing operation with retry logic."""
    ipaddr, ipport, login, passwd = fencing_data["ipaddr"], fencing_data["ipport"], \
                                     fencing_data["login"], fencing_data["passwd"]

    env = os.environ.copy()
    env['IPMITOOL_PASSWORD'] = passwd
    cmd = ["ipmitool", "-I", "lanplus", "-H", ipaddr, "-U", login, "-E", "-p", ipport, "power", action]

    for attempt in range(MAX_FENCING_RETRIES):
        try:
            subprocess.run(cmd, timeout=timeout, env=env, capture_output=True, text=True, check=True)
            break
        except subprocess.TimeoutExpired as e:
            if attempt < MAX_FENCING_RETRIES - 1:
                logging.warning("IPMI %s timeout for %s (attempt %d/%d), retrying...",
                              action, host, attempt + 1, MAX_FENCING_RETRIES)
                time.sleep(FENCING_RETRY_DELAY_SECONDS * (attempt + 1))
            else:
                _pkg._safe_log_exception("IPMI %s failed for %s: timeout after %d attempts" %
                                  (action, host, MAX_FENCING_RETRIES), e)
                return False
        except subprocess.CalledProcessError as e:
            if attempt < MAX_FENCING_RETRIES - 1:
                logging.warning("IPMI %s failed for %s with return code %d (attempt %d/%d), retrying...",
                              action, host, e.returncode, attempt + 1, MAX_FENCING_RETRIES)
                time.sleep(FENCING_RETRY_DELAY_SECONDS * (attempt + 1))
            else:
                _pkg._safe_log_exception("IPMI %s failed for %s: command failed with return code %d after %d attempts" %
                                  (action, host, e.returncode, MAX_FENCING_RETRIES), e)
                return False

    if action == 'off':
        return _wait_for_power_off(
            lambda: _pkg._get_power_state('ipmi', ip=ipaddr, port=ipport, user=login, passwd=passwd, timeout=timeout),
            timeout, host, 'CHASSIS POWER IS OFF', 'IPMI')

    logging.info("IPMI %s successful for %s", action, host)
    return True


def _fence_noop(host, action, fencing_data, service):
    """Noop fencing agent (no actual fencing)."""
    logging.warning("Using noop fencing agent for %s %s. VMs may get corrupted.", host, action)
    return True


def _fence_ipmi(host, action, fencing_data, service):
    """IPMI fencing agent."""
    if not _validate_fencing_params(fencing_data, ["ipaddr", "ipport", "login", "passwd"], "IPMI", host):
        return False
    if not _validate_fencing_inputs(fencing_data, "IPMI"):
        return False
    timeout = fencing_data.get("timeout", service.config.get_config_value('FENCING_TIMEOUT'))
    return _execute_ipmi_fence(host, action, fencing_data, timeout)


def _fence_redfish(host, action, fencing_data, service):
    """Redfish fencing agent."""
    if not _validate_fencing_params(fencing_data, ["ipaddr", "login", "passwd"], "Redfish", host):
        return False
    if not _validate_fencing_inputs(fencing_data, "Redfish"):
        return False
    url = _build_redfish_url(fencing_data)
    if not url:
        return False
    redfish_action = "ForceOff" if action == "off" else "On"
    timeout = fencing_data.get("timeout", service.config.get_config_value('FENCING_TIMEOUT'))
    return _redfish_reset(url, fencing_data["login"], fencing_data["passwd"],
                        timeout, redfish_action, service.config)


def _fence_bmh(host, action, fencing_data, service):
    """BareMetal Host fencing agent."""
    if not _validate_fencing_params(fencing_data, ["token", "host", "namespace"], "BMH", host):
        return False
    return _bmh_fence(fencing_data["token"], fencing_data["namespace"],
                     fencing_data["host"], action, service)


# Fencing agent dispatch table
FENCING_AGENTS = {
    'noop': _fence_noop,
    'ipmi': _fence_ipmi,
    'redfish': _fence_redfish,
    'bmh': _fence_bmh,
}


def _execute_fence_operation(host, action, fencing_data, service):
    """Unified fencing execution with agent-specific dispatch and validation."""
    agent = fencing_data.get("agent", "").lower()

    try:
        if not validate_input(action, 'power_action', "host fencing"):
            logging.error(f"Invalid fence action: {action}")
            return False

        for agent_key, agent_func in FENCING_AGENTS.items():
            if agent_key in agent:
                return agent_func(host, action, fencing_data, service)

        logging.error("Unknown fencing agent: %s", agent)
        return False

    except Exception as e:
        logging.error("%s fencing failed for %s: %s", agent.upper(), host, e)
        return False


def host_fence(host, action, service):
    """Fence a host using the configured fencing agent."""
    if not host:
        logging.error("Invalid fence parameters: missing host")
        return False

    if not validate_input(action, 'power_action', "host fencing"):
        logging.error(f"Invalid fence action: {action}")
        return False

    if action not in ['on', 'off']:
        logging.error(f"Unsupported fence action: {action} (only 'on' and 'off' supported)")
        return False

    logging.info(f"Fencing host {host} {action}")
    _publish_fence_event(host, action, "start")

    try:
        short_hostname = _extract_hostname(host)
        matching_configs = [v for k, v in service.config.fencing.items()
                           if _extract_hostname(k) == short_hostname]

        if not matching_configs:
            logging.error("No fencing data found for %s", host)
            _publish_fence_event(host, action, "result", success=False)
            return False

        fencing_data = matching_configs[0]

        if not isinstance(fencing_data, dict) or "agent" not in fencing_data:
            logging.error("Invalid fencing data for %s", host)
            _publish_fence_event(host, action, "result", success=False)
            return False

        logging.debug("Using fencing agent '%s' for %s", fencing_data["agent"], host)
        result = _pkg._execute_fence_operation(host, action, fencing_data, service)
        _publish_fence_event(host, action, "result", success=result)
        return result

    except Exception as e:
        logging.error("Fencing failed for %s: %s", host, e)
        _publish_fence_event(host, action, "result", success=False)
        return False


def _publish_fence_event(host, action, phase, success=None):
    """Publish a fencing event to the event bus (best-effort)."""
    try:
        from .ai.event_bus import Event, EventType, get_event_bus
        bus = get_event_bus()
        if phase == "start":
            bus.publish(Event(
                event_type=EventType.FENCE_START,
                host=_extract_hostname(host),
                data={"action": action},
                source="fencing",
            ))
        else:
            bus.publish(Event(
                event_type=EventType.FENCE_RESULT,
                host=_extract_hostname(host),
                data={"action": action, "success": success},
                source="fencing",
            ))
    except Exception:
        pass  # Event bus is optional
