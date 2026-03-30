# InstanceHA - OpenStack Instance High Availability
#
# Re-export all public symbols for backwards compatibility.
# Tests and external code that do `import instanceha` and access
# `instanceha.ConfigManager`, `instanceha._host_fence`, etc.
# will continue to work.

# Standard library modules (accessed by tests via instanceha.*)
import os
import sys
import time
import logging
import subprocess
import concurrent
import concurrent.futures
import requests
from datetime import datetime, timedelta

# Third-party imports used by tests
from novaclient import client
from novaclient.exceptions import Conflict, NotFound, Unauthorized
from keystoneauth1 import loading
from keystoneauth1 import session as ksc_session
from keystoneauth1.exceptions.discovery import DiscoveryFailure

# Models and constants
from .models import (
    CACHE_TIMEOUT_SECONDS,
    ConfigItem,
    DISABLED_REASON_EVACUATION,
    DISABLED_REASON_EVACUATION_COMPLETE,
    DISABLED_REASON_EVACUATION_FAILED,
    DISABLED_REASON_KDUMP_MARKER,
    EVACUATION_POLL_INTERVAL_SECONDS,
    EVACUATION_RETRY_WAIT_SECONDS,
    EvacuationResult,
    EvacuationStatus,
    FencingCredentials,
    FENCING_RETRY_DELAY_SECONDS,
    HEALTH_CHECK_PORT,
    INITIAL_EVACUATION_WAIT_SECONDS,
    KDUMP_CLEANUP_AGE_SECONDS,
    KDUMP_CLEANUP_THRESHOLD,
    KDUMP_MAGIC_NUMBER,
    KDUMP_REENABLE_DELAY_SECONDS,
    MatchType,
    MAX_ENABLE_RETRIES,
    MAX_EVACUATION_RETRIES,
    MAX_EVACUATION_TIMEOUT_SECONDS,
    MAX_FENCING_RETRIES,
    MAX_PROCESSING_TIME_PADDING_SECONDS,
    MIGRATION_QUERY_LIMIT,
    MIGRATION_QUERY_MINUTES,
    MIGRATION_STATUS_COMPLETED,
    MIGRATION_STATUS_ERROR,
    MigrationStatus,
    NovaLoginCredentials,
    OpenStackClient,
    ReservedHostResult,
)

# Validation
from .validation import (
    _extract_hostname,
    _safe_log_exception,
    _sanitize_url,
    _try_validate,
    validate_input,
    validate_inputs,
    VALIDATION_PATTERNS,
)

# Config
from .config import ConfigManager, ConfigurationError

# Nova
from .nova import (
    NovaConnectionError,
    nova_login,
    _handle_nova_exception,
    get_nova_connection as _get_nova_connection,
    NOVA_EXCEPTION_MESSAGES,
)

# Service
from .service import (
    CloudConnectionProvider,
    InstanceHAService,
)

# Monitoring
from .monitoring import (
    categorize_services as _categorize_services,
    check_critical_services as _check_critical_services,
    check_kdump as _check_kdump,
    is_service_resume_candidate as _is_service_resume_candidate,
    kdump_udp_listener as _kdump_udp_listener,
    track_host_processing,
    UDPSocketManager,
)

# Fencing
from .fencing import (
    _bmh_fence,
    _bmh_wait_for_power_off,
    _build_redfish_url,
    _execute_fence_operation,
    _execute_ipmi_fence,
    _fence_bmh,
    _fence_ipmi,
    _fence_noop,
    _fence_redfish,
    _get_power_state,
    _make_ssl_request,
    _redfish_reset,
    _validate_fencing_inputs,
    _validate_fencing_params,
    _wait_for_power_off,
    FENCING_AGENTS,
    host_fence as _host_fence,
)

# Evacuation
from .evacuation import (
    _get_evacuable_servers,
    _server_evacuate,
    _server_evacuate_future,
    _server_evacuation_status,
    _smart_evacuate,
    _traditional_evacuate,
    _update_service_disable_reason,
    host_disable as _host_disable,
    host_enable as _host_enable,
    host_evacuate as _host_evacuate,
)

# Reserved hosts
from .reserved_hosts import (
    _aggregate_ids,
    _enable_matching_reserved_host,
    manage_reserved_hosts as _manage_reserved_hosts,
)

# Main
from .main import (
    _cleanup_filtered_hosts,
    _count_evacuable_hosts,
    _establish_nova_connection,
    _execute_step,
    _filter_by_aggregates,
    _filter_processing_hosts,
    _initialize_service,
    _post_evacuation_recovery,
    _prepare_evacuation_resources,
    _process_reenabling,
    _process_stale_services,
    main,
    process_service,
)
