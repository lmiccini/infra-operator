#!/usr/libexec/platform-python -tt

import concurrent.futures
import logging
import os
import sys
import threading
import time
from datetime import datetime, timedelta

from .config import ConfigManager, ConfigurationError
from .models import (
    DISABLED_REASON_EVACUATION_COMPLETE,
    DISABLED_REASON_KDUMP_MARKER,
    KDUMP_REENABLE_DELAY_SECONDS,
    MAX_EVACUATION_TIMEOUT_SECONDS,
    MAX_PROCESSING_TIME_PADDING_SECONDS,
    MIGRATION_QUERY_LIMIT,
    MIGRATION_QUERY_MINUTES,
    MIGRATION_STATUS_COMPLETED,
    MIGRATION_STATUS_ERROR,
)
from .nova import NovaConnectionError
from .validation import _extract_hostname, _safe_log_exception

# Access cross-module functions through the package namespace at call time.
# This allows tests to patch these functions via 'instanceha._host_fence' etc.
# Using sys.modules[__package__] avoids circular import issues.
import sys as _sys
_pkg = _sys.modules[__package__]


def _execute_step(step_name, step_func, host_name, *args, **kwargs):
    """Execute a processing step with unified error handling."""
    try:
        result = step_func(*args, **kwargs)
        if not result:
            logging.error("%s failed for %s", step_name, host_name)
        return result
    except Exception as e:
        logging.error("%s failed for %s: %s", step_name, host_name, e)
        return False


def process_service(failed_service, reserved_hosts, resume, service) -> bool:
    """Process a failed compute service through the complete recovery workflow."""
    if not failed_service or not hasattr(failed_service, 'host'):
        logging.error("Invalid service object provided")
        return False

    host_name = failed_service.host
    hostname = _extract_hostname(host_name)
    reserved_hosts = reserved_hosts or []

    logging.info(f"Processing service {host_name} (resume={resume})")

    with _pkg.track_host_processing(service, hostname):
        try:
            conn = _pkg._get_nova_connection(service)
            if not conn:
                logging.error(f"Nova connection failed for {host_name}")
                return False

            if not resume:
                if not _pkg._execute_step("Fencing", _pkg._host_fence, host_name, host_name, 'off', service):
                    return False

            if not (resume and failed_service.forced_down and 'disabled' in failed_service.status):
                if not _pkg._execute_step("Host disable", _pkg._host_disable, host_name, conn, failed_service, service):
                    return False

            try:
                reserved_result = _pkg._manage_reserved_hosts(conn, failed_service, reserved_hosts, service)
                if not reserved_result.success:
                    logging.error("Reserved host management failed for %s", host_name)
                    return False
                target_host = reserved_result.hostname
            except Exception as e:
                logging.error("Reserved host management failed for %s: %s", host_name, e)
                return False

            if target_host and service.config.get_config_value('FORCE_RESERVED_HOST_EVACUATION'):
                logging.info(f"Forcing evacuation to reserved host: {target_host}")
                if not _pkg._execute_step("Evacuation", _pkg._host_evacuate, host_name,
                                    conn, failed_service, service, target_host):
                    return False
            else:
                if not _pkg._execute_step("Evacuation", _pkg._host_evacuate, host_name,
                                    conn, failed_service, service):
                    return False

            if not _pkg._execute_step("Recovery", _pkg._post_evacuation_recovery, host_name,
                                conn, failed_service, service, resume):
                return False

            logging.debug(f"Service processing completed successfully for {host_name}")
            return True

        except Exception as e:
            logging.error(f"Service processing failed for {host_name}: {e}")
            return False


def _post_evacuation_recovery(conn, failed_service, service, resume=False):
    """Perform post-evacuation recovery by powering on the host."""
    hostname = _extract_hostname(failed_service.host)
    kdump_fenced = hostname in service.kdump_fenced_hosts
    leave_disabled = service.config.get_config_value('LEAVE_DISABLED')

    logging.info("Evacuation successful. Starting recovery for %s", failed_service.host)

    try:
        if resume:
            logging.debug("Skipping power on for %s (resume)", failed_service.host)
        elif kdump_fenced:
            logging.info("Skipping power on for %s (kdump fenced)", failed_service.host)
            service.kdump_hosts_checking.pop(hostname, None)
        else:
            logging.debug("Powering on host %s", failed_service.host)
            power_on_result = _pkg._host_fence(failed_service.host, 'on', service)
            if not power_on_result:
                logging.error("Failed to power on %s during recovery", failed_service.host)
                return False

        try:
            suffix = f" {DISABLED_REASON_KDUMP_MARKER}" if kdump_fenced else ""
            new_reason = f"{DISABLED_REASON_EVACUATION_COMPLETE}{suffix}: {datetime.now().isoformat()}"
            conn.services.disable_log_reason(failed_service.id, new_reason)
            logging.debug(f"Updated disable reason for {failed_service.host} to indicate evacuation complete")
        except Exception as e:
            logging.warning(f"Failed to update disable reason for {failed_service.host}: {e}")

        if leave_disabled:
            logging.info("Recovery completed successfully for %s (will remain disabled due to LEAVE_DISABLED)",
                        failed_service.host)
        else:
            logging.info("Recovery completed successfully for %s (will re-enable when host is up)",
                        failed_service.host)
        return True

    except Exception as e:
        logging.error("Error during post-evacuation recovery for %s: %s", failed_service.host, e)
        logging.debug("Exception traceback:", exc_info=True)
        return False


def _initialize_service(config_manager=None):
    """Initialize InstanceHA service and supporting threads."""
    from .service import InstanceHAService
    from .monitoring import kdump_udp_listener

    if config_manager is None:
        config_manager = _pkg.config_manager

    try:
        service = InstanceHAService(config_manager)
        logging.info("InstanceHA service initialized successfully")
    except Exception as e:
        logging.error("Failed to initialize InstanceHA service: %s", e)
        sys.exit(1)

    health_check_thread = threading.Thread(target=service.start_health_check_server)
    health_check_thread.daemon = True
    health_check_thread.start()

    if service.config.get_config_value('CHECK_KDUMP'):
        kdump_thread = threading.Thread(target=kdump_udp_listener, args=(service,))
        kdump_thread.daemon = True
        kdump_thread.start()

    return service


def _establish_nova_connection(service):
    """Establish Nova connection using service configuration."""
    try:
        conn = service.create_connection()
        if conn is None:
            logging.error("Failed: Unable to connect to Nova - connection is None")
            sys.exit(1)
        return conn
    except NovaConnectionError as e:
        logging.error("Failed: Unable to connect to Nova")
        sys.exit(1)
    except Exception as e:
        _safe_log_exception("Failed: Unable to connect to Nova", e)
        sys.exit(1)


def _cleanup_filtered_hosts(service, marked_hostnames, final_hostnames, current_time):
    """Clean up hosts from processing tracking that were filtered out."""
    with service.processing_lock:
        candidate_hosts = marked_hostnames - final_hostnames
        to_cleanup = [h for h in candidate_hosts if service.hosts_processing.get(h) == current_time]
        for hostname in to_cleanup:
            service.hosts_processing.pop(hostname, None)
        if to_cleanup:
            logging.debug(f'Cleaned up {len(to_cleanup)} filtered hosts from processing tracking')


def _filter_processing_hosts(service, compute_nodes, to_resume):
    """Filter out hosts already being processed and mark new ones."""
    current_time = time.time()
    max_processing_time = max(service.config.get_config_value('FENCING_TIMEOUT'), MAX_EVACUATION_TIMEOUT_SECONDS)
    marked_hostnames = set()

    with service.processing_lock:
        service._cleanup_dict_by_condition(
            service.hosts_processing,
            lambda h, t: current_time - t > max_processing_time + MAX_PROCESSING_TIME_PADDING_SECONDS,
            'Cleaned up expired processing entry for {}')

        original_count = len(compute_nodes)
        compute_nodes_filtered = []
        to_resume_filtered = []

        for svc in compute_nodes:
            hostname = _extract_hostname(svc.host)
            if hostname not in service.hosts_processing:
                compute_nodes_filtered.append(svc)

        for svc in to_resume:
            hostname = _extract_hostname(svc.host)
            if hostname not in service.hosts_processing:
                to_resume_filtered.append(svc)

        if original_count > len(compute_nodes_filtered):
            skipped_hosts = original_count - len(compute_nodes_filtered)
            logging.info(f'Skipped {skipped_hosts} hosts already being processed by another poll cycle')

        for svc in compute_nodes_filtered + to_resume_filtered:
            hostname = _extract_hostname(svc.host)
            service.hosts_processing[hostname] = current_time
            marked_hostnames.add(hostname)

    return compute_nodes_filtered, to_resume_filtered, marked_hostnames, current_time


def _count_evacuable_hosts(conn, service, services):
    """Count total number of compute services in evacuable aggregates."""
    try:
        aggregates = conn.aggregates.list()
        evacuable_hosts = set()

        for agg in aggregates:
            if service._is_resource_evacuable(agg, service.evacuable_tag, ['metadata']):
                evacuable_hosts.update(agg.hosts)

        return sum(1 for svc in services if svc.host in evacuable_hosts)

    except Exception as e:
        logging.warning(f"Failed to count evacuable hosts: {e}")
        return len(services)


def _filter_by_aggregates(conn, service, compute_nodes, services):
    """Filter compute nodes by aggregate evacuability."""
    try:
        aggregates = conn.aggregates.list()
        evacuable_hosts = set()

        for agg in aggregates:
            if service._is_resource_evacuable(agg, service.evacuable_tag, ['metadata']):
                evacuable_hosts.update(agg.hosts)

        compute_nodes_down = list(compute_nodes)
        compute_nodes = [svc for svc in compute_nodes if svc.host in evacuable_hosts]

        down_not_tagged = [svc.host for svc in compute_nodes_down if svc not in compute_nodes]
        if down_not_tagged:
            logging.warning(f'Computes not part of evacuable aggregate: {down_not_tagged}')

    except Exception as e:
        logging.warning(f"Failed to check aggregate evacuability: {e}")

    return compute_nodes


def _prepare_evacuation_resources(conn, service, services, compute_nodes):
    """Prepare and filter resources for evacuation."""
    if not compute_nodes:
        return compute_nodes, [], [], []

    service.refresh_evacuable_cache(conn, force=True)

    host_servers_cache = service.get_hosts_with_servers_cached(conn, compute_nodes)
    original_count = len(compute_nodes)
    compute_nodes = service.filter_hosts_with_servers(compute_nodes, host_servers_cache)
    filtered_count = len(compute_nodes)
    logging.debug("Filtered compute nodes: %d -> %d (removed %d hosts with no servers)",
                original_count, filtered_count, original_count - filtered_count)

    if not compute_nodes:
        logging.debug("No compute nodes with servers to evacuate - all filtered out")
        return [], [], [], []

    reserved_hosts = []
    if service.config.get_config_value('RESERVED_HOSTS'):
        reserved_hosts = [svc for svc in services
                          if 'disabled' in svc.status and 'reserved' in svc.disabled_reason]

    images_enabled = service.config.get_config_value('TAGGED_IMAGES')
    flavors_enabled = service.config.get_config_value('TAGGED_FLAVORS')
    images = service.get_evacuable_images(conn) if images_enabled else []
    flavors = service.get_evacuable_flavors(conn) if flavors_enabled else []

    if (images_enabled or flavors_enabled) and host_servers_cache:
        compute_nodes = service.filter_hosts_with_evacuable_servers(compute_nodes, host_servers_cache, flavors, images)

    if service.config.get_config_value('TAGGED_AGGREGATES'):
        compute_nodes = _pkg._filter_by_aggregates(conn, service, compute_nodes, services)

    return compute_nodes, reserved_hosts, images, flavors


def _process_stale_services(conn, service, services, compute_nodes, to_resume):
    """Process stale compute services for evacuation."""
    compute_nodes = list(compute_nodes)
    to_resume = list(to_resume)

    if not (compute_nodes or to_resume):
        return

    compute_nodes, to_resume, marked_hostnames, current_time = _pkg._filter_processing_hosts(service, compute_nodes, to_resume)

    if not (compute_nodes or to_resume):
        _pkg._cleanup_filtered_hosts(service, marked_hostnames, set(), current_time)
        return

    if compute_nodes:
        logging.warning(f'The following computes are down: {[svc.host for svc in compute_nodes]}')

    compute_nodes, reserved_hosts, images, flavors = _pkg._prepare_evacuation_resources(conn, service, services, compute_nodes)

    if services and compute_nodes:
        if service.config.get_config_value('TAGGED_AGGREGATES'):
            total_evacuable = _pkg._count_evacuable_hosts(conn, service, services)
            threshold_percent = (len(compute_nodes) / total_evacuable * 100) if total_evacuable > 0 else 0
        else:
            threshold_percent = (len(compute_nodes) / len(services)) * 100

        threshold = service.config.get_config_value('THRESHOLD')
        if threshold_percent > threshold:
            logging.error(f'Number of impacted computes ({threshold_percent:.1f}%) exceeds threshold ({threshold}%). Not evacuating.')
            _publish_threshold_event(threshold_percent, threshold, len(compute_nodes))
            _pkg._cleanup_filtered_hosts(service, marked_hostnames, set(), current_time)
            return

    if not service.config.get_config_value('DISABLED'):
        can_evacuate, error_msg = _pkg._check_critical_services(conn, services, compute_nodes)
        if not can_evacuate:
            logging.error(f'Cannot evacuate: {error_msg}. Skipping evacuation.')
            _pkg._cleanup_filtered_hosts(service, marked_hostnames, set(), current_time)
            return

        if service.config.get_config_value('CHECK_KDUMP'):
            to_evacuate, kdump_fenced = _pkg._check_kdump(compute_nodes, service)
        else:
            to_evacuate = compute_nodes
            kdump_fenced = []

        with concurrent.futures.ThreadPoolExecutor() as executor:
            poll_interval = service.config.get_config_value('POLL')
            results = list(executor.map(lambda svc: _pkg.process_service(svc, reserved_hosts, False, service), to_evacuate))
            if not all(results):
                logging.warning(f'Some services failed to evacuate. Retrying in {poll_interval} seconds.')
            results = list(executor.map(lambda svc: _pkg.process_service(svc, reserved_hosts, True, service), kdump_fenced))
            if not all(results):
                logging.warning(f'Some kdump-fenced services failed to evacuate. Retrying in {poll_interval} seconds.')
            results = list(executor.map(lambda svc: _pkg.process_service(svc, reserved_hosts, True, service), to_resume))
            if not all(results):
                logging.warning(f'Some services failed to evacuate. Retrying in {poll_interval} seconds.')

        final_hostnames = {_extract_hostname(svc.host) for svc in to_evacuate + kdump_fenced + to_resume}
        _pkg._cleanup_filtered_hosts(service, marked_hostnames, final_hostnames, current_time)
    else:
        logging.info('InstanceHA DISABLED is true, not evacuating')
        _pkg._cleanup_filtered_hosts(service, marked_hostnames, set(), current_time)


def _process_reenabling(conn, service, to_reenable) -> None:
    """Process services that can be re-enabled."""
    to_reenable = list(to_reenable)

    if not to_reenable:
        return

    if service.config.get_config_value('LEAVE_DISABLED'):
        to_reenable = [svc for svc in to_reenable
                      if not ('disabled' in svc.status and 'instanceha evacuation complete' in svc.disabled_reason)]
        if not to_reenable:
            return

    logging.debug(f'Checking {len(to_reenable)} computes for re-enabling')
    force_enable = service.config.get_config_value('FORCE_ENABLE')

    for svc in to_reenable:
        try:
            if force_enable:
                migrations_complete = True
            else:
                query_time = (datetime.now() - timedelta(minutes=MIGRATION_QUERY_MINUTES)).isoformat()
                migrations = conn.migrations.list(source_compute=svc.host, migration_type='evacuation',
                                                 changes_since=query_time, limit=MIGRATION_QUERY_LIMIT)
                incomplete = [m for m in migrations if m.status not in MIGRATION_STATUS_COMPLETED and m.status not in MIGRATION_STATUS_ERROR]
                migrations_complete = len(incomplete) == 0

            if not migrations_complete:
                logging.debug(f'{len(incomplete)}/{len(migrations)} migration(s) incomplete for {svc.host}, not re-enabling')
                continue

            if 'kdump' in getattr(svc, 'disabled_reason', ''):
                hostname = _extract_hostname(svc.host)
                last_kdump = service.kdump_hosts_timestamp.get(hostname, 0)
                time_since_kdump = time.time() - last_kdump if last_kdump > 0 else float('inf')
                if time_since_kdump < KDUMP_REENABLE_DELAY_SECONDS:
                    logging.info(f'{svc.host} waiting for kdump to complete ({time_since_kdump:.0f}s since last message, waiting for {KDUMP_REENABLE_DELAY_SECONDS}s)')
                    continue
                else:
                    logging.info(f'{svc.host} kdump messages stopped ({time_since_kdump:.0f}s since last message), proceeding with re-enable')

            if svc.forced_down:
                _pkg._host_enable(conn, svc, reenable=True, service=service)

            if 'disabled' in svc.status:
                if svc.state == 'up':
                    _pkg._host_enable(conn, svc, reenable=False)
                    logging.info(f'Enabled {svc.host} (migrations complete, service is up)')
                else:
                    logging.debug(f'{svc.host} still down, will enable once up')
        except Exception as e:
            logging.error(f'Failed to enable {svc.host}: {e}')


def _publish_service_state_event(host, state):
    """Publish a service state change event to the event bus (best-effort)."""
    try:
        from .ai.event_bus import Event, EventType, get_event_bus
        bus = get_event_bus()
        bus.publish(Event(
            event_type=EventType.SERVICE_STATE_CHANGE,
            host=_extract_hostname(host),
            data={"state": state},
            source="main",
        ))
    except Exception:
        pass


def _publish_threshold_event(percent, threshold, host_count):
    """Publish a threshold exceeded event to the event bus (best-effort)."""
    try:
        from .ai.event_bus import Event, EventType, get_event_bus
        bus = get_event_bus()
        bus.publish(Event(
            event_type=EventType.THRESHOLD_EXCEEDED,
            data={"percent": round(percent, 1), "threshold": threshold, "host_count": host_count},
            source="main",
        ))
    except Exception:
        pass


def _start_observer():
    """Start the AI observer, subscribing to the event bus (best-effort)."""
    try:
        from .ai.event_bus import get_event_bus
        from .ai.observer import Observer
        from . import ai as _ai_pkg
        bus = get_event_bus()
        observer = Observer(bus)
        observer.start()
        _ai_pkg._observer = observer
        return observer
    except Exception as e:
        logging.warning("Failed to start AI observer: %s", e)
        return None


def _start_mcp_server(conn, service):
    """Start the MCP server as a daemon thread (best-effort)."""
    try:
        from .ai.mcp_server import InstanceHAMCPServer
        from .ai.safety import ApprovalManager, AuditLogger
        from .ai.tools import ApprovalLevel

        audit_logger = AuditLogger()
        approval_manager = ApprovalManager(
            auto_approve_level=ApprovalLevel.NONE,
            audit_logger=audit_logger,
            dry_run=True,
        )

        mcp_server = InstanceHAMCPServer(
            nova_connection=conn,
            service=service,
            approval_manager=approval_manager,
            allow_writes=False,
        )

        if not mcp_server.is_available:
            logging.info("MCP SDK not installed, MCP server disabled")
            return None

        mcp_server.start()
        return mcp_server
    except Exception as e:
        logging.warning("Failed to start MCP server: %s", e)
        return None


def _create_llm_engine(config_manager):
    """Create an LLM engine from the InstanceHA config file.

    Reads AI settings from the 'ai' section of config.yaml (the same
    ConfigMap used for all InstanceHA settings):

        config:
          # ... existing settings ...
        ai:
          enabled: true
          endpoint: http://ollama.openstack.svc:11434
          model: llama3.1:8b
          # api_key: optional
          # model_path: /path/to/model.gguf  (for local backend)
          # n_ctx: 4096
          # n_threads: 4
    """
    try:
        from .ai.engine import create_engine

        # Read the ai: section from the same YAML config file
        ai_section = {}
        try:
            import yaml
            with open(config_manager.config_path, 'r') as f:
                data = yaml.load(f, Loader=yaml.SafeLoader) or {}
            ai_section = data.get("ai", {})
        except Exception as e:
            logging.debug("No AI section in config: %s", e)

        config = {
            "ai_enabled": str(ai_section.get("enabled", "False")),
            "ai_model_path": str(ai_section.get("model_path", "")),
            "ai_endpoint": str(ai_section.get("endpoint", "")),
            "ai_api_key": str(ai_section.get("api_key", "")),
            "ai_model": str(ai_section.get("model", "default")),
            "ai_n_ctx": str(ai_section.get("n_ctx", "4096")),
            "ai_n_threads": str(ai_section.get("n_threads", "4")),
        }

        engine = create_engine(config)
        if engine and engine.is_available():
            logging.info("LLM engine ready: %s", engine.model_info())
        elif engine:
            logging.warning("LLM engine created but not available: %s", engine.model_info())
        return engine
    except Exception as e:
        logging.warning("Failed to create LLM engine: %s", e)
        return None


def _start_chat_server(conn, service, config_manager):
    """Start the AI chat server as a daemon thread if the socket directory exists."""
    try:
        from .ai.chat_server import ChatServer
        from .ai.safety import ApprovalManager, AuditLogger
        from .ai.tools import ApprovalLevel

        socket_dir = "/var/run/instanceha"
        if not os.path.isdir(socket_dir):
            logging.info("Chat server socket directory %s not found, skipping", socket_dir)
            return None

        audit_logger = AuditLogger()
        approval_manager = ApprovalManager(
            auto_approve_level=ApprovalLevel.NONE,
            audit_logger=audit_logger,
            dry_run=True,
        )

        llm_engine = _create_llm_engine(config_manager)

        chat_server = ChatServer(
            nova_connection=conn,
            service=service,
            approval_manager=approval_manager,
            llm_engine=llm_engine,
        )
        chat_server.start()
        return chat_server
    except Exception as e:
        logging.warning("Failed to start chat server: %s", e)
        return None


def main():
    from .monitoring import categorize_services

    try:
        config_manager = _pkg.ConfigManager()
        logging.basicConfig(format='%(asctime)s %(levelname)s %(message)s', level=config_manager.get_config_value('LOGLEVEL'))
        logging.info("Configuration loaded successfully")
    except ConfigurationError as e:
        logging.error("Configuration failed: %s", e)
        sys.exit(1)

    service = _pkg._initialize_service(config_manager)
    conn = _pkg._establish_nova_connection(service)

    # Start AI observer, MCP server, and chat server (daemon threads, non-blocking)
    _start_observer()
    _start_mcp_server(conn, service)
    _start_chat_server(conn, service, config_manager)

    while True:
        service.update_health_hash()

        try:
            services = conn.services.list(binary="nova-compute")
            if not services:
                time.sleep(service.config.get_config_value('POLL'))
                continue

            target_date = datetime.now() - timedelta(seconds=service.config.get_config_value('DELTA'))
            compute_nodes, to_resume, to_reenable = categorize_services(services, target_date)

            compute_nodes_list = list(compute_nodes)

            for svc in compute_nodes_list:
                _publish_service_state_event(svc.host, "down")

            _pkg._process_stale_services(conn, service, services, compute_nodes_list, to_resume)

            _pkg._process_reenabling(conn, service, to_reenable)

        except Exception as e:
            logging.warning(f"Failed to query compute status from Nova API: {e}. Please check the Nova API availability.")
            logging.debug('Exception traceback:', exc_info=True)

        time.sleep(service.config.get_config_value('POLL'))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        logging.info("Shutting down due to keyboard interrupt")
    except Exception as e:
        logging.error(f'Error: {e}')
        raise
