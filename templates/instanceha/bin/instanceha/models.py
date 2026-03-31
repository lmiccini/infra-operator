#!/usr/libexec/platform-python -tt

import re
from dataclasses import dataclass
from enum import Enum
from typing import Any, Optional, Protocol


# Constants
CACHE_TIMEOUT_SECONDS = 300
KDUMP_CLEANUP_THRESHOLD = 100
KDUMP_CLEANUP_AGE_SECONDS = 300
MAX_EVACUATION_TIMEOUT_SECONDS = 300
INITIAL_EVACUATION_WAIT_SECONDS = 10
EVACUATION_POLL_INTERVAL_SECONDS = 5
EVACUATION_RETRY_WAIT_SECONDS = 5
MAX_EVACUATION_RETRIES = 5
MAX_ENABLE_RETRIES = 3
MAX_FENCING_RETRIES = 3
HEALTH_CHECK_PORT = 8080
DEFAULT_UDP_PORT = 7410
KDUMP_MAGIC_NUMBER = 0x1B302A40
MAX_PROCESSING_TIME_PADDING_SECONDS = 30
MIGRATION_QUERY_MINUTES = 5
MIGRATION_QUERY_LIMIT = 1000
USERNAME_MAX_LENGTH = 64
FENCING_RETRY_DELAY_SECONDS = 1
KDUMP_REENABLE_DELAY_SECONDS = 60

# Disabled reason markers
DISABLED_REASON_EVACUATION = "instanceha evacuation"
DISABLED_REASON_EVACUATION_COMPLETE = "instanceha evacuation complete"
DISABLED_REASON_EVACUATION_FAILED = "evacuation FAILED"
DISABLED_REASON_KDUMP_MARKER = "(kdump)"


# Enums
class MatchType(Enum):
    """Type of matching to use for reserved hosts."""
    AGGREGATE = "aggregate"
    ZONE = "zone"


class MigrationStatus(Enum):
    """Migration status values."""
    COMPLETED = "completed"
    DONE = "done"
    ERROR = "error"
    FAILED = "failed"
    CANCELLED = "cancelled"


# Migration status constants for checking migration state
MIGRATION_STATUS_COMPLETED = [MigrationStatus.COMPLETED.value, MigrationStatus.DONE.value]
MIGRATION_STATUS_ERROR = [MigrationStatus.ERROR.value, MigrationStatus.FAILED.value, MigrationStatus.CANCELLED.value]


# Dataclasses
@dataclass
class ConfigItem:
    """Configuration item with type and validation constraints."""
    type: str
    default: Any
    min_val: Optional[int] = None
    max_val: Optional[int] = None


@dataclass
class EvacuationResult:
    """Result of a server evacuation request."""
    uuid: str
    accepted: bool
    reason: str


@dataclass
class EvacuationStatus:
    """Status of an ongoing server evacuation."""
    completed: bool
    error: bool


@dataclass
class FencingCredentials:
    """Credentials and connection info for fencing operations."""
    agent: str
    ipaddr: str
    login: str
    passwd: str
    ipport: str = "443"
    timeout: int = 30
    uuid: str = "System.Embedded.1"
    tls: str = "false"
    # BMH-specific fields
    token: Optional[str] = None
    namespace: Optional[str] = None
    host: Optional[str] = None


@dataclass
class NovaLoginCredentials:
    """Credentials for Nova API login."""
    username: str
    password: str
    project_name: str
    auth_url: str
    user_domain_name: str
    project_domain_name: str
    region_name: str
    cacert: Optional[str] = None


@dataclass
class ReservedHostResult:
    """Result of reserved host management operation."""
    success: bool
    hostname: Optional[str] = None


# Protocol / ABC
class OpenStackClient(Protocol):
    """Protocol defining the interface for OpenStack clients."""

    def services(self): ...
    def flavors(self): ...
    def aggregates(self): ...
    def servers(self): ...
