import concurrent.futures
import logging
import sys as _sys
import time
from datetime import datetime, timedelta
from typing import List, Optional

from novaclient.exceptions import Conflict, NotFound, Forbidden, Unauthorized

from .models import (
    DISABLED_REASON_EVACUATION,
    DISABLED_REASON_EVACUATION_COMPLETE,
    DISABLED_REASON_KDUMP_MARKER,
    EVACUATION_POLL_INTERVAL_SECONDS,
    EVACUATION_RETRY_WAIT_SECONDS,
    INITIAL_EVACUATION_WAIT_SECONDS,
    KDUMP_REENABLE_DELAY_SECONDS,
    MAX_ENABLE_RETRIES,
    MAX_EVACUATION_RETRIES,
    MAX_EVACUATION_TIMEOUT_SECONDS,
    MIGRATION_QUERY_LIMIT,
    MIGRATION_QUERY_MINUTES,
    MIGRATION_STATUS_COMPLETED,
    MIGRATION_STATUS_ERROR,
    EvacuationResult,
    EvacuationStatus,
    OpenStackClient,
)
from .nova import _handle_nova_exception
from .validation import _extract_hostname

# Package-level reference for mock-patchability: tests patch 'instanceha.X'
# so runtime calls must go through the package namespace.
_pkg = _sys.modules[__package__]


def _update_service_disable_reason(connection, host, service_id=None) -> None:
    """Helper to update service disable reason for evacuation failures."""
    try:
        if service_id is None:
            services = connection.services.list(host=host, binary='nova-compute')
            service_obj = next((s for s in services if s.host == host and s.binary == 'nova-compute'), None)
            if not service_obj:
                logging.warning('Could not find service object for host %s', host)
                return False
            service_id = service_obj.id

        disable_reason = f"evacuation FAILED: {datetime.now().isoformat()}"
        connection.services.disable_log_reason(service_id, disable_reason)
        logging.debug('Updated disabled reason for host %s after evacuation failure', host)
        return True
    except Exception as e:
        logging.error('Failed to update disable_reason for host %s. Error: %s', host, e)
        logging.debug('Exception traceback:', exc_info=True)
        return False


def _get_evacuable_servers(connection, host, service) -> List:
    """Get list of evacuable servers from a host."""
    images = service.get_evacuable_images(connection)
    flavors = service.get_evacuable_flavors(connection)

    servers = connection.servers.list(search_opts={'host': host, 'all_tenants': 1})
    servers = [s for s in servers if s.status in {'ACTIVE', 'ERROR', 'STOPPED'}]

    if flavors or images:
        logging.debug("Filtering images and flavors: %s %s", repr(flavors), repr(images))
        evacuables = [s for s in servers if service.is_server_evacuable(s, flavors, images)]
        logging.debug("Evacuating %s", repr(evacuables))
    else:
        logging.debug("Evacuating all images and flavors")
        evacuables = servers

    return evacuables


def _server_evacuate(connection, server, target_host=None) -> EvacuationResult:
    """Evacuate a server instance."""
    success = False
    error_message = ""

    try:
        if target_host:
            logging.info("Evacuating instance %s to target host %s", server, target_host)
            response, _ = connection.servers.evacuate(server=server, host=target_host)
        else:
            logging.info("Evacuating instance %s", server)
            response, _ = connection.servers.evacuate(server=server)

        if response is None:
            error_message = "No response received while evacuating instance"
        elif response.status_code == 200:
            success = True
            if target_host:
                error_message = response.reason or f"Evacuation to {target_host} initiated successfully"
            else:
                error_message = response.reason or "Evacuation initiated successfully"
        else:
            error_message = response.reason or f"Evacuation failed with status {response.status_code}"
    except NotFound:
        error_message = f"Instance {server} not found"
    except Forbidden:
        error_message = f"Access denied while evacuating instance {server}"
    except Unauthorized:
        error_message = f"Authentication failed while evacuating instance {server}"
    except Exception as e:
        error_message = f"Error while evacuating instance {server}: {e}"

    return EvacuationResult(
        uuid=server,
        accepted=success,
        reason=error_message,
    )


def _server_evacuation_status(connection, server) -> EvacuationStatus:
    """Check the status of a server evacuation by querying recent migrations."""
    if not connection or not server:
        return EvacuationStatus(completed=False, error=True)

    try:
        query_time = (datetime.now() - timedelta(minutes=MIGRATION_QUERY_MINUTES)).isoformat()
        migrations = connection.migrations.list(
            instance_uuid=str(server),
            migration_type='evacuation',
            changes_since=query_time,
            limit=str(MIGRATION_QUERY_LIMIT)
        )

        if not migrations:
            return EvacuationStatus(completed=False, error=True)

        migration = migrations[0]
        status = getattr(migration, 'status', None) or getattr(migration, '_info', {}).get('status')

        return EvacuationStatus(
            completed=status in MIGRATION_STATUS_COMPLETED if status else False,
            error=status in MIGRATION_STATUS_ERROR if status else True
        )

    except Exception as e:
        logging.error("Failed to check evacuation status for %s: %s", server, e)
        return EvacuationStatus(completed=False, error=True)


def _server_evacuate_future(connection, server, target_host=None) -> bool:
    """Evacuate a server and monitor the evacuation process until completion."""
    error_count = 0
    start_time = time.time()

    if not hasattr(server, 'id'):
        logging.warning("Could not evacuate instance - missing server ID: %s",
                       getattr(server, 'to_dict', lambda: str(server))())
        return False

    if target_host:
        logging.info("Processing evacuation for server %s to target host %s", server.id, target_host)
    else:
        logging.info("Processing evacuation for server %s", server.id)

    try:
        response = _server_evacuate(connection, server.id, target_host=target_host)

        if not response.accepted:
            logging.warning("Evacuation of %s on %s failed: %s",
                           response.uuid, server.id, response.reason)
            return False

        logging.debug("Starting evacuation of %s", response.uuid)
        time.sleep(INITIAL_EVACUATION_WAIT_SECONDS)

        while True:
            if time.time() - start_time > MAX_EVACUATION_TIMEOUT_SECONDS:
                logging.error("Evacuation of %s timed out after %d seconds. Giving up.",
                             response.uuid, MAX_EVACUATION_TIMEOUT_SECONDS)
                return False

            try:
                status = _server_evacuation_status(connection, server.id)

                if status.completed:
                    logging.info("Evacuation of %s completed successfully", response.uuid)
                    return True

                if status.error:
                    error_count += 1
                    if error_count >= MAX_EVACUATION_RETRIES:
                        logging.error("Failed evacuating %s %d times. Giving up.",
                                     response.uuid, MAX_EVACUATION_RETRIES)
                        return False

                    logging.warning("Evacuation of instance %s failed %d times. Retrying...",
                                   response.uuid, error_count)
                    time.sleep(EVACUATION_RETRY_WAIT_SECONDS)
                    continue

                logging.debug("Evacuation of %s still in progress", response.uuid)
                time.sleep(EVACUATION_POLL_INTERVAL_SECONDS)

            except Exception as e:
                error_count += 1
                logging.error("Error checking evacuation status for %s: %s",
                             response.uuid, str(e))
                logging.debug('Exception traceback:', exc_info=True)

                if error_count >= MAX_EVACUATION_RETRIES:
                    logging.error("Too many errors checking evacuation status for %s. Giving up.",
                                 response.uuid)
                    return False

                logging.warning("Retrying evacuation status check for %s in %d seconds (attempt %d/%d)...",
                               response.uuid, EVACUATION_RETRY_WAIT_SECONDS, error_count, MAX_EVACUATION_RETRIES)
                time.sleep(EVACUATION_RETRY_WAIT_SECONDS)
                continue

    except Exception as e:
        logging.error("Unexpected error during evacuation of server %s: %s",
                     server.id, str(e))
        logging.debug('Exception traceback:', exc_info=True)
        return False

    return False


def _smart_evacuate(connection, evacuables, service, host, service_id, target_host=None) -> bool:
    """Execute smart evacuation with migration tracking."""
    if target_host:
        logging.debug("Using smart evacuation with %d workers, targeting host %s",
                     service.config.get_config_value('WORKERS'), target_host)
    else:
        logging.debug("Using smart evacuation with %d workers", service.config.get_config_value('WORKERS'))

    with concurrent.futures.ThreadPoolExecutor(max_workers=service.config.get_config_value('WORKERS')) as executor:
        future_to_server = {executor.submit(_pkg._server_evacuate_future, connection, s, target_host): s for s in evacuables}

        for future in concurrent.futures.as_completed(future_to_server):
            server = future_to_server[future]
            try:
                if not future.result():
                    logging.debug('Evacuation of %s failed', server.id)
                    _pkg._update_service_disable_reason(connection, host, service_id)
                    return False
                logging.info('%r evacuated successfully', server.id)
            except Exception as exc:
                logging.error('Evacuation generated an exception: %s', exc)
                logging.debug('Exception traceback:', exc_info=True)
                _pkg._update_service_disable_reason(connection, host, service_id)
                return False

    return True


def _traditional_evacuate(connection, evacuables, host, target_host=None) -> bool:
    """Execute traditional fire-and-forget evacuation."""
    if target_host:
        logging.debug("Using traditional evacuation approach, targeting host %s", target_host)
    else:
        logging.debug("Using traditional evacuation approach")
    all_succeeded = True

    for server in evacuables:
        logging.debug("Processing %s", server)
        if hasattr(server, 'id'):
            response = _server_evacuate(connection, server.id, target_host=target_host)
            if response.accepted:
                logging.debug("Evacuated %s from %s: %s", response.uuid, host, response.reason)
            else:
                logging.warning("Evacuation of %s on %s failed: %s", response.uuid, host, response.reason)
                all_succeeded = False
        else:
            logging.error("Could not evacuate instance: %s", server.to_dict())
            all_succeeded = False

    return all_succeeded


def host_evacuate(connection, failed_service, service, target_host=None) -> bool:
    """Evacuate all instances from a failed host."""
    host = failed_service.host

    evacuables = _pkg._get_evacuable_servers(connection, host, service)
    if not evacuables:
        logging.info("Nothing to evacuate")
        return True

    time.sleep(service.config.get_config_value('DELAY'))

    if service.config.get_config_value('SMART_EVACUATION'):
        return _pkg._smart_evacuate(connection, evacuables, service, host, failed_service.id, target_host=target_host)
    else:
        return _pkg._traditional_evacuate(connection, evacuables, host, target_host=target_host)


def host_disable(connection, service, instanceha_service=None):
    """
    Disable a compute service by forcing it down and logging the reason.
    """
    if not connection or not service:
        logging.error("Cannot disable service - missing connection or service object")
        return False

    if not hasattr(service, 'id') or not hasattr(service, 'host'):
        missing = 'id' if not hasattr(service, 'id') else 'host'
        logging.error("Cannot disable service - service object missing %s: %s",
                     missing, getattr(service, 'to_dict', lambda: str(service))())
        return False

    service_info = f"service {getattr(service, 'binary', 'unknown')} on host {service.host}"

    logging.info("Forcing %s down before evacuation", service.host)
    try:
        connection.services.force_down(service.id, True)
        logging.debug("Successfully forced down %s", service_info)
    except Exception as e:
        return _handle_nova_exception("force-down", service_info, e, is_critical=True)

    try:
        is_kdump = instanceha_service and _extract_hostname(service.host) in instanceha_service.kdump_fenced_hosts
        suffix = f" {DISABLED_REASON_KDUMP_MARKER}" if is_kdump else ""
        disable_reason = f"{DISABLED_REASON_EVACUATION}{suffix}: {datetime.now().isoformat()}"
        connection.services.disable_log_reason(service.id, disable_reason)
        logging.info("Successfully disabled %s with reason: %s", service_info, disable_reason)
        return True
    except Exception as e:
        return _handle_nova_exception("log disable reason", service_info, e, is_critical=False)


def host_enable(connection, nova_service, reenable: bool = False, service=None) -> bool:
    """Enable a host service, optionally unsetting force-down."""
    if reenable:
        try:
            logging.debug(f'Unsetting force-down on host {nova_service.host} after evacuation')
            connection.services.force_down(nova_service.id, False)
            logging.info(f'Unset force-down for {nova_service.host}')
            if 'kdump' in getattr(nova_service, 'disabled_reason', ''):
                try:
                    connection.services.disable_log_reason(nova_service.id, f"{DISABLED_REASON_EVACUATION_COMPLETE}: {datetime.now().isoformat()}")
                    if service:
                        hostname = _extract_hostname(nova_service.host)
                        service.kdump_fenced_hosts.discard(hostname)
                        service.kdump_hosts_timestamp.pop(hostname, None)
                except (AttributeError, KeyError):
                    pass
            return True
        except Exception as e:
            logging.warning(f'Could not unset force-down for {nova_service.host} as not all the migrations are complete yet. Will try again the next poll cycle.')
            logging.debug(f'Full error details for {nova_service.host}: {e}')
            return False

    for attempt in range(MAX_ENABLE_RETRIES):
        try:
            logging.info(f'Trying to enable {nova_service.host} (attempt {attempt + 1}/{MAX_ENABLE_RETRIES})')
            connection.services.enable(nova_service.id)
            logging.info(f'Host {nova_service.host} is now enabled')
            return True
        except Exception as e:
            if attempt < MAX_ENABLE_RETRIES - 1:
                logging.warning(f'Failed to enable {nova_service.host}, retrying: {e}')
            else:
                logging.error(f'Failed to enable {nova_service.host} after {MAX_ENABLE_RETRIES} attempts. Last error: {e}')
            continue

    return False
