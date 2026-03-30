import logging
import sys as _sys
from typing import List

from .evacuation import host_enable
from .models import MatchType, ReservedHostResult

# Package-level reference for mock-patchability: tests patch 'instanceha.X'
# so runtime calls must go through the package namespace.
_pkg = _sys.modules[__package__]


def _aggregate_ids(conn, service) -> List[int]:
    """Get aggregate IDs for a service's host."""
    return [ag.id for ag in conn.aggregates.list() if service.host in ag.hosts]


def _enable_matching_reserved_host(conn, failed_service, reserved_hosts, service, match_type: MatchType = MatchType.AGGREGATE) -> ReservedHostResult:
    """
    Enable a reserved host that matches the failed service criteria.

    Args:
        conn: Nova client connection
        failed_service: Failed service to match
        reserved_hosts: List of available reserved hosts
        service: Service instance for configuration access
        match_type: Type of matching to use (AGGREGATE or ZONE)

    Returns:
        ReservedHostResult: Result with success flag and optional hostname
    """
    try:
        matching_hosts = []

        if match_type == MatchType.AGGREGATE:
            compute_aggregate_ids = _aggregate_ids(conn, failed_service)
            if not compute_aggregate_ids:
                return ReservedHostResult(success=False)

            for host in reserved_hosts:
                if (service.is_aggregate_evacuable(conn, host.host) and
                    set(_aggregate_ids(conn, host)).intersection(compute_aggregate_ids)):
                    matching_hosts.append(host)
        else:
            matching_hosts = [host for host in reserved_hosts
                            if host.zone == failed_service.zone]

        if not matching_hosts:
            logging.warning(f"No reserved compute found in the same {match_type.value} as {failed_service.host}")
            return ReservedHostResult(success=False)

        selected_host = matching_hosts[0]
        reserved_hosts.remove(selected_host)

        if not _pkg._host_enable(conn, selected_host, reenable=False):
            logging.error(f"Failed to enable reserved host {selected_host.host}")
            return ReservedHostResult(success=False)

        logging.info(f"Enabled host {selected_host.host} from the reserved pool (same {match_type.value} as {failed_service.host})")
        return ReservedHostResult(success=True, hostname=selected_host.host)

    except Exception as e:
        logging.error(f"Error enabling reserved host by {match_type.value}: {e}")
        return ReservedHostResult(success=False)


def manage_reserved_hosts(conn, failed_service, reserved_hosts, service) -> ReservedHostResult:
    """
    Manage reserved hosts by enabling a replacement host if available.

    Args:
        conn: Nova client connection
        failed_service: Failed service that needs replacement capacity
        reserved_hosts: List of available reserved hosts
        service: Service instance for configuration access

    Returns:
        ReservedHostResult: Result with success flag and optional hostname
    """
    if not service.config.get_config_value('RESERVED_HOSTS'):
        logging.debug("Reserved hosts feature is disabled")
        return ReservedHostResult(success=True)

    if not reserved_hosts:
        logging.warning("Not enough hosts available from the reserved pool")
        return ReservedHostResult(success=True)

    try:
        match_type = MatchType.AGGREGATE if service.config.get_config_value('TAGGED_AGGREGATES') else MatchType.ZONE
        result = _pkg._enable_matching_reserved_host(conn, failed_service, reserved_hosts, service, match_type)
        if result.success:
            return result

        logging.debug(f"No matching reserved host found for {failed_service.host}, continuing with evacuation")
        return ReservedHostResult(success=True)

    except Exception as e:
        logging.error(f"Error managing reserved hosts for {failed_service.host}: {e}")
        return ReservedHostResult(success=False)
