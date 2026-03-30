import logging
import re
from typing import Callable

from .models import USERNAME_MAX_LENGTH


# Pre-compiled regex patterns for credential sanitization
_SECRET_PATTERNS = {
    'password': re.compile(r'\bpassword=[^\s)\'\"]+', re.IGNORECASE),
    'token': re.compile(r'\btoken=[^\s)\'\"]+', re.IGNORECASE),
    'secret': re.compile(r'\bsecret=[^\s)\'\"]+', re.IGNORECASE),
    'credential': re.compile(r'\bcredential=[^\s)\'\"]+', re.IGNORECASE),
    'auth': re.compile(r'\bauth=[^\s)\'\"]+', re.IGNORECASE),
}


def _safe_log_exception(msg: str, e: Exception, include_traceback: bool = False) -> None:
    """Log exception without exposing secrets in messages or tracebacks."""
    safe_msg = str(e)
    for secret_word, pattern in _SECRET_PATTERNS.items():
        safe_msg = pattern.sub(f'{secret_word}=***', safe_msg)
    logging.error("%s: %s", msg, safe_msg)
    if include_traceback:
        logging.debug("Exception traceback (sanitized): %s", type(e).__name__)


# Security validation patterns
VALIDATION_PATTERNS = {
    'k8s_namespace': (r'^[a-z0-9]([-a-z0-9]*[a-z0-9])?$', 63),
    'k8s_resource': (r'^[a-z0-9]([-a-z0-9]*[a-z0-9])?(\.[a-z0-9]([-a-z0-9]*[a-z0-9])?)*$', 253),
    'power_action': (['on', 'off', 'status', 'reset', 'cycle', 'soft', 'On', 'ForceOff',
                      'GracefulShutdown', 'GracefulRestart', 'ForceRestart', 'Nmi',
                      'ForceOn', 'PushPowerButton'], None),
    'ip_address': ('ip', None),  # Special marker for IP address validation
    'port': ('port', None),  # Special marker for port validation
    'username': (r'^[a-zA-Z0-9_-]{1,64}$', USERNAME_MAX_LENGTH)
}


def _try_validate(validator_func: Callable[[], bool], error_msg: str, context: str, log_error: bool = True) -> bool:
    """Helper to execute validation with consistent error handling."""
    try:
        return validator_func()
    except (ValueError, AttributeError, TypeError) as e:
        if log_error:
            logging.error(f"{error_msg} for {context}: {e}")
        return False


def validate_input(value: str, validation_type: str, context: str) -> bool:
    """Unified validation to prevent SSRF and injection attacks."""
    from urllib.parse import urlparse
    import ipaddress

    if not value:
        return False

    # Special validation types
    if validation_type == 'url':
        def _validate_url():
            p = urlparse(value)
            if p.scheme not in ['http', 'https'] or not p.netloc:
                return False
            # Block localhost and link-local
            h = p.hostname
            if h and (h.lower() in ['localhost', '127.0.0.1', '::1', '0.0.0.0'] or h.startswith('169.254.')):
                logging.error(f"Blocked localhost/link-local access in {context}")
                return False
            return True
        return _try_validate(_validate_url, "Invalid URL", context, log_error=False)

    if validation_type == 'ip_address':
        return _try_validate(lambda: ipaddress.ip_address(value) and True,
                            f"Invalid IP address", context)

    if validation_type == 'port':
        def _validate_port():
            port_int = int(value)
            if 1 <= port_int <= 65535:
                return True
            logging.error(f"Port out of range for {context}: {value}")
            return False
        return _try_validate(_validate_port, "Invalid port format", context)

    # Pattern-based validation
    pattern_data = VALIDATION_PATTERNS.get(validation_type)
    if not pattern_data:
        return True  # Unknown type, skip validation

    pattern_or_list, max_len = pattern_data

    # List-based validation (for actions)
    if isinstance(pattern_or_list, list):
        return value in pattern_or_list

    # Regex-based validation (for k8s resources, usernames)
    if max_len and len(value) > max_len:
        return False
    return bool(re.match(pattern_or_list, value))


def validate_inputs(validations: dict, context: str) -> bool:
    """Validate multiple inputs at once. Returns True if all valid."""
    return all(validate_input(val, vtype, context) for vtype, val in validations.items())


def _sanitize_url(url: str) -> str:
    """Remove credentials from URL if present (e.g., http://user:pass@host -> http://***@host)."""
    return re.sub(r'://([^/:]+):([^@]+)@', r'://***@', url)


def _extract_hostname(host: str) -> str:
    """Extract short hostname from FQDN or hostname."""
    return host.split('.', 1)[0]
