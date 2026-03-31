import logging
import os
import sys as _sys
from typing import Optional

from novaclient import client
from novaclient.exceptions import Conflict, NotFound, Forbidden, Unauthorized

from keystoneauth1 import loading
from keystoneauth1 import session as ksc_session
from keystoneauth1.exceptions.discovery import DiscoveryFailure

from .models import NovaLoginCredentials, OpenStackClient
from .validation import _safe_log_exception

# Package-level reference for mock-patchability: tests patch 'instanceha.X'
# so runtime calls must go through the package namespace.
_pkg = _sys.modules[__package__]


class NovaConnectionError(Exception):
    """Raised when Nova API connection fails."""
    pass


# Standard path where lib-common mounts the CA bundle from CaBundleSecretName
_CA_BUNDLE_PATH = "/etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem"


def nova_login(credentials: NovaLoginCredentials) -> Optional[OpenStackClient]:
    """Create and return Nova client connection."""
    try:
        loader = loading.get_plugin_loader("password")
        auth = loader.load_from_options(
            auth_url=credentials.auth_url,
            username=credentials.username,
            password=credentials.password,
            project_name=credentials.project_name,
            user_domain_name=credentials.user_domain_name,
            project_domain_name=credentials.project_domain_name,
        )

        session_kwargs = {"auth": auth}
        if credentials.cacert and os.path.exists(credentials.cacert):
            session_kwargs["verify"] = credentials.cacert
        elif os.path.exists(_CA_BUNDLE_PATH):
            session_kwargs["verify"] = _CA_BUNDLE_PATH
        session = ksc_session.Session(**session_kwargs)
        nova = client.Client("2.59", session=session, region_name=credentials.region_name)
        nova.versions.get_current()
        logging.info("Nova login successful")
        return nova
    except (_pkg.DiscoveryFailure, _pkg.Unauthorized) as e:
        _pkg._safe_log_exception(f"Nova login failed: {type(e).__name__}", e)
    except Exception as e:
        _pkg._safe_log_exception("Nova login failed", e, include_traceback=True)
    return None


NOVA_EXCEPTION_MESSAGES = {
    NotFound: "Resource not found",
    Conflict: "Conflicting operation",
}


def _handle_nova_exception(operation: str, service_info: str, e: Exception, is_critical: bool = True) -> bool:
    """Handle Nova API exceptions with consistent logging."""
    error_msg = NOVA_EXCEPTION_MESSAGES.get(type(e))

    if error_msg:
        logging.error("Failed to %s for %s. %s: %s", operation, service_info, error_msg, type(e).__name__)
    else:
        _pkg._safe_log_exception("Failed to %s for %s. Unexpected error" % (operation, service_info), e, include_traceback=True)

    if not is_critical:
        logging.warning("Service %s operation partially failed. Manual cleanup may be needed.", service_info)
    return False


def get_nova_connection(service):
    """Establish a connection to Nova using environment configuration."""
    try:
        cloud_name = os.getenv('OS_CLOUD', 'overcloud')

        if cloud_name not in service.config.clouds or cloud_name not in service.config.secure:
            logging.error("Configuration not found for cloud: %s", cloud_name)
            return None

        auth = service.config.clouds[cloud_name]["auth"]
        secure = service.config.secure[cloud_name]["auth"]
        region_name = service.config.clouds[cloud_name]["region_name"]

        credentials = NovaLoginCredentials(
            username=auth["username"],
            password=secure["password"],
            project_name=auth["project_name"],
            auth_url=auth["auth_url"],
            user_domain_name=auth["user_domain_name"],
            project_domain_name=auth["project_domain_name"],
            region_name=region_name,
        )
        conn = _pkg.nova_login(credentials)

        if not conn:
            logging.error("Nova connection failed")
        return conn

    except Exception as e:
        _pkg._safe_log_exception("Failed to establish Nova connection", e)
        return None
