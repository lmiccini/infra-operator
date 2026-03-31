import logging
import socket
import struct
import time
from collections import defaultdict
from contextlib import contextmanager
from datetime import datetime
from typing import Any, List, Tuple

from .models import (
    DISABLED_REASON_EVACUATION,
    DISABLED_REASON_EVACUATION_COMPLETE,
    DISABLED_REASON_EVACUATION_FAILED,
    KDUMP_CLEANUP_AGE_SECONDS,
    KDUMP_CLEANUP_THRESHOLD,
    KDUMP_MAGIC_NUMBER,
)
from .validation import _extract_hostname


class UDPSocketManager:
    """Context manager for UDP socket handling with proper resource cleanup."""

    def __init__(self, udp_ip, udp_port):
        self.udp_ip = udp_ip
        self.udp_port = udp_port
        self.socket = None

    def __enter__(self):
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.socket.settimeout(1.0)
        self.socket.bind((self.udp_ip, self.udp_port))
        logging.info(f'Kdump UDP listener started on {self.udp_ip}:{self.udp_port}')
        return self.socket

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self.socket:
            try:
                self.socket.close()
            except (OSError, AttributeError):
                pass
        logging.info('Kdump UDP listener stopped')


def kdump_udp_listener(service) -> None:
    """Background UDP listener for kdump messages."""
    udp_ip = service.UDP_IP if service.UDP_IP else '0.0.0.0'
    udp_port = service.config.get_udp_port()

    try:
        with UDPSocketManager(udp_ip, udp_port) as sock:
            while not service.kdump_listener_stop_event.is_set():
                try:
                    data, _, _, address = sock.recvmsg(65535, 1024, 0)
                    logging.debug(f'Received UDP message from {address[0]}, length: {len(data)}')

                    if len(data) >= 8:
                        magic_native = struct.unpack('I', data[:4])[0]
                        magic_network = struct.unpack('!I', data[:4])[0]

                        if magic_native == KDUMP_MAGIC_NUMBER or magic_network == KDUMP_MAGIC_NUMBER:
                            try:
                                hostname = _extract_hostname(socket.gethostbyaddr(address[0])[0])
                                service.kdump_hosts_timestamp[hostname] = time.time()
                                logging.debug(f'Kdump message received from host: {hostname}')
                                _publish_kdump_event(hostname)

                                if len(service.kdump_hosts_timestamp) > KDUMP_CLEANUP_THRESHOLD:
                                    cutoff = time.time() - KDUMP_CLEANUP_AGE_SECONDS
                                    service.kdump_hosts_timestamp = defaultdict(float,
                                        {k: v for k, v in service.kdump_hosts_timestamp.items() if v >= cutoff})
                            except Exception as e:
                                logging.debug(f'Failed to process kdump message from {address[0]}: {e}')
                        else:
                            logging.debug(f'Invalid magic number from {address[0]}: 0x{magic_native:x} / 0x{magic_network:x}')
                except socket.timeout:
                    continue
                except OSError as e:
                    if not service.kdump_listener_stop_event.is_set():
                        logging.debug(f'UDP listener socket error: {e}')
                    continue
    except Exception as e:
        logging.error(f'Kdump listener failed to start: {e}')


def check_kdump(stale_services, service) -> Tuple[List[Any], List[Any]]:
    """Check for kdump messages. Wait KDUMP_TIMEOUT for messages; evacuate immediately once received."""
    if not stale_services:
        return [], []

    logging.info(f"Checking {len(stale_services)} hosts for kdump activity")
    kdump_fenced = []
    waiting = []
    current_time = time.time()
    kdump_timeout = service.config.get_config_value('KDUMP_TIMEOUT')

    for svc in stale_services:
        hostname = _extract_hostname(svc.host)
        last_msg = service.kdump_hosts_timestamp.get(hostname, 0)
        check_start = service.kdump_hosts_checking.get(hostname, 0)

        # State 3a: Kdump message received - host is fenced, evacuate immediately
        if last_msg > 0 and (current_time - last_msg) <= kdump_timeout:
            kdump_fenced.append(svc.host)
            service.kdump_fenced_hosts.add(hostname)
            service.kdump_hosts_checking.pop(hostname, None)
            logging.info(f'Host {svc.host} fenced via kdump - evacuating')
        # State 1: First time seeing host down - start waiting
        elif check_start == 0:
            service.kdump_hosts_checking[hostname] = current_time
            waiting.append(svc.host)
            logging.info(f'Host {svc.host} down, waiting {kdump_timeout}s for kdump')
        # State 2: Still waiting for timeout
        elif (current_time - check_start) < kdump_timeout:
            waiting.append(svc.host)
            logging.info(f'Host {svc.host} waiting ({current_time - check_start:.1f}s/{kdump_timeout}s)')
        # State 3b: Timeout expired - proceed with evacuation
        else:
            service.kdump_hosts_checking.pop(hostname, None)
            logging.info(f'Host {svc.host} timeout expired, no kdump - evacuating')

    kdump_fenced_services = [s for s in stale_services if s.host in kdump_fenced]
    to_evacuate = [s for s in stale_services if s.host not in waiting and s.host not in kdump_fenced]
    if kdump_fenced:
        logging.info(f'Total kdump-fenced: {len(kdump_fenced)}')
    return to_evacuate, kdump_fenced_services


def is_service_resume_candidate(svc) -> bool:
    """Check if a service is a candidate for resuming evacuation."""
    if not (svc.forced_down and svc.state == 'down' and 'disabled' in svc.status):
        return False

    reason = getattr(svc, 'disabled_reason', '')
    return (DISABLED_REASON_EVACUATION in reason and
            DISABLED_REASON_EVACUATION_FAILED not in reason and
            DISABLED_REASON_EVACUATION_COMPLETE not in reason)


def categorize_services(services, target_date: datetime) -> tuple:
    """Categorize services into compute nodes, resume candidates, and re-enable candidates."""
    compute_nodes = (svc for svc in services
                     if not ('disabled' in svc.status or svc.forced_down)
                     and (svc.state == 'down' or datetime.fromisoformat(svc.updated_at) < target_date))

    resume = (svc for svc in services if is_service_resume_candidate(svc))

    reenable = (svc for svc in services
                if (('enabled' in svc.status and svc.forced_down)
                   or ('disabled' in svc.status and DISABLED_REASON_EVACUATION_COMPLETE in svc.disabled_reason))
                and not is_service_resume_candidate(svc))

    return compute_nodes, resume, reenable


def check_critical_services(conn, services, compute_nodes):
    """
    Check if critical Nova services are operational for evacuation.

    Returns (bool, str): (can_evacuate, error_message)
    """
    schedulers = [s for s in services if s.binary == 'nova-scheduler']
    if schedulers and not any(s.state == 'up' for s in schedulers):
        return False, "All nova-scheduler services are down"

    return True, ""


def _publish_kdump_event(host):
    """Publish a kdump detection event to the event bus (best-effort)."""
    try:
        from .ai.event_bus import Event, EventType, get_event_bus
        bus = get_event_bus()
        bus.publish(Event(
            event_type=EventType.KDUMP_DETECTED,
            host=host,
            data={"type": "kernel_crash_dump"},
            source="monitoring",
        ))
    except Exception:
        pass


@contextmanager
def track_host_processing(service, hostname: str):
    """Context manager for tracking host processing state with automatic cleanup."""
    try:
        yield
    finally:
        with service.processing_lock:
            service.hosts_processing.pop(hostname, None)
            logging.debug(f'Cleaned up processing tracking for {hostname}')
