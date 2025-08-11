#!/usr/libexec/platform-python -tt

import os
import sys
import time
import atexit
import logging
import inspect
from datetime import datetime, timedelta
import concurrent.futures
import requests.exceptions
import yaml
import subprocess
import random
from yaml.loader import SafeLoader
import socket
import struct
import threading
import hashlib
from http import server
import json
from typing import Dict, Any, Optional, Union, List
from collections import defaultdict

from novaclient import client
from novaclient.exceptions import Conflict, NotFound, Forbidden, Unauthorized

from keystoneauth1 import loading
from keystoneauth1 import session as ksc_session
from keystoneauth1.exceptions.discovery import DiscoveryFailure
from keystoneauth1.exceptions.http import Unauthorized


class ConfigurationError(Exception):
    """Raised when configuration loading or validation fails."""
    pass


class NovaConnectionError(Exception):
    """Raised when Nova API connection fails."""
    pass


class ConfigManager:
    """
    Secure configuration manager for InstanceHA application.

    This class handles loading, validation, and secure access to configuration
    values with proper type checking and default values.
    """

    def __init__(self, config_path: Optional[str] = None):
        """
        Initialize the configuration manager.

        Args:
            config_path: Path to configuration file, defaults to environment variable or standard path
        """
        self.config_path = config_path or os.getenv('INSTANCEHA_CONFIG_PATH', '/var/lib/instanceha/config.yaml')
        self.clouds_path = os.getenv('CLOUDS_CONFIG_PATH', '/home/cloud-admin/.config/openstack/clouds.yaml')
        self.secure_path = os.getenv('SECURE_CONFIG_PATH', '/home/cloud-admin/.config/openstack/secure.yaml')
        self.fencing_path = os.getenv('FENCING_CONFIG_PATH', '/secrets/fencing.yaml')

        # Load configurations
        self.config = self._load_config()
        self.clouds = self._load_clouds_config()
        self.secure = self._load_secure_config()
        self.fencing = self._load_fencing_config()

        # Validate critical configuration
        self._validate_config()

    def _load_yaml_config(self, path: str, required_key: Optional[str] = None, config_name: str = "configuration") -> Dict[str, Any]:
        """Generic YAML configuration loader with error handling."""
        try:
            if not os.path.exists(path):
                logging.warning("%s file not found at %s, using defaults", config_name.title(), path)
                return {}

            with open(path, 'r') as stream:
                data = yaml.load(stream, Loader=SafeLoader)
                if not isinstance(data, dict):
                    raise ConfigurationError(f"Invalid {config_name} format in {path}")

                if required_key:
                    if required_key not in data:
                        raise ConfigurationError(f"Missing '{required_key}' in {config_name} at {path}")
                    return data[required_key]
                return data.get("config", {})

        except yaml.YAMLError as e:
            logging.error("Failed to parse %s file %s: %s", config_name, path, e)
            raise ConfigurationError(f"{config_name.title()} parsing failed: {e}")
        except (IOError, OSError) as e:
            logging.error("Failed to read %s file %s: %s", config_name, path, e)
            raise ConfigurationError(f"{config_name.title()} file access failed: {e}")

    def _load_config(self) -> Dict[str, Any]:
        return self._load_yaml_config(self.config_path, config_name="configuration")

    def _load_clouds_config(self) -> Dict[str, Any]:
        return self._load_yaml_config(self.clouds_path, "clouds", "clouds configuration")

    def _load_secure_config(self) -> Dict[str, Any]:
        return self._load_yaml_config(self.secure_path, "clouds", "secure configuration")

    def _load_fencing_config(self) -> Dict[str, Any]:
        return self._load_yaml_config(self.fencing_path, "FencingConfig", "fencing configuration")

    def _validate_config(self) -> None:
        """Validate critical configuration values using the configuration map."""
        # Auto-validate all config values with constraints
        validation_keys = ['DELTA', 'POLL', 'THRESHOLD', 'WORKERS', 'LOGLEVEL']
        for key in validation_keys:
            try:
                self.get_config_value(key)  # This will validate against _config_map constraints
            except ValueError as e:
                raise ConfigurationError(str(e))

        # Validate special conditions
        poll_interval = self.get_int('POLL', 45)
        kdump_timeout = self.get_int('KDUMP_TIMEOUT', 30)
        if self.get_bool('CHECK_KDUMP') and poll_interval == 30 and kdump_timeout == 30:
            logging.warning('CHECK_KDUMP is enabled with a KDUMP_TIMEOUT value of 30 seconds and POLL is also set to 30 seconds. This may result in unexpected failures. Please increase POLL to 45 or greater.')

    def get_str(self, key: str, default: str = '') -> str:
        """Get a string configuration value with validation."""
        value = self.config.get(key, default)
        if not isinstance(value, str):
            logging.warning("Configuration key %s should be string, got %s, using default: %s",
                          key, type(value).__name__, default)
            return default
        return value

    def get_int(self, key: str, default: int = 0, min_val: Optional[int] = None, max_val: Optional[int] = None) -> int:
        """Get an integer configuration value with validation."""
        value = self.config.get(key, default)

        try:
            int_value = int(value)
        except (ValueError, TypeError):
            logging.warning("Configuration key %s should be integer, got %s, using default: %s",
                          key, type(value).__name__, default)
            return default

        if min_val is not None and int_value < min_val:
            logging.warning("Configuration key %s value %d is below minimum %d, using minimum",
                          key, int_value, min_val)
            return min_val

        if max_val is not None and int_value > max_val:
            logging.warning("Configuration key %s value %d exceeds maximum %d, using maximum",
                          key, int_value, max_val)
            return max_val

        return int_value

    def get_bool(self, key: str, default: bool = False) -> bool:
        """Get a boolean configuration value with validation."""
        value = self.config.get(key, default)

        if isinstance(value, bool):
            return value
        elif isinstance(value, str):
            return value.lower() in ('true', '1', 'yes', 'on', 'enabled')
        else:
            logging.warning("Configuration key %s should be boolean, got %s, using default: %s",
                          key, type(value).__name__, default)
            return default

    # Configuration mapping with defaults and validation
    _config_map = {
        'EVACUABLE_TAG': ('str', 'evacuable'),
        'DELTA': ('int', 30, 10, 300),
        'POLL': ('int', 45, 15, 600),
        'THRESHOLD': ('int', 50, 0, 100),
        'WORKERS': ('int', 4, 1, 50),
        'DELAY': ('int', 0, 0, 300),
        'LOGLEVEL': ('str', 'INFO'),
        'SMART_EVACUATION': ('bool', False),
        'RESERVED_HOSTS': ('bool', False),
        'TAGGED_IMAGES': ('bool', True),
        'TAGGED_FLAVORS': ('bool', True),
        'TAGGED_AGGREGATES': ('bool', True),
        'LEAVE_DISABLED': ('bool', False),
        'FORCE_ENABLE': ('bool', False),
        'CHECK_KDUMP': ('bool', False),
        'KDUMP_TIMEOUT': ('int', 30, 5, 300),
        'DISABLED': ('bool', False),
        'SSL_VERIFY': ('bool', True),
    }

    def get_config_value(self, key: str) -> Union[str, int, bool]:
        """Get configuration value with automatic type handling and validation."""
        if key not in self._config_map:
            raise ValueError(f"Unknown configuration key: {key}")

        config_def = self._config_map[key]
        value_type, default = config_def[0], config_def[1]

        if value_type == 'str':
            result = self.get_str(key, default)
            if key == 'LOGLEVEL':
                result = result.upper()
                valid_levels = ['DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL']
                if result not in valid_levels:
                    raise ValueError(f"LOGLEVEL must be one of {valid_levels}, got {result}")
            return result
        elif value_type == 'int':
            min_val = config_def[2] if len(config_def) > 2 else None
            max_val = config_def[3] if len(config_def) > 3 else None
            return self.get_int(key, default, min_val, max_val)
        elif value_type == 'bool':
            return self.get_bool(key, default)

        return default

    def get_cloud_name(self) -> str: return os.getenv('OS_CLOUD', 'overcloud')
    def get_udp_port(self) -> int: return int(os.getenv('UDP_PORT', 7410))

    def get_ssl_ca_bundle(self) -> Optional[str]:
        return self._get_ssl_path('SSL_CA_BUNDLE')

    def get_ssl_cert_path(self) -> Optional[str]:
        return self._get_ssl_path('SSL_CERT_PATH')

    def get_ssl_key_path(self) -> Optional[str]:
        return self._get_ssl_path('SSL_KEY_PATH')

    def _get_ssl_path(self, key: str) -> Optional[str]:
        """Get SSL file path if it exists."""
        path = self.get_str(key, '')
        return path if path and os.path.exists(path) else None

    def get_requests_ssl_config(self) -> Union[bool, str, tuple]:
        """Get SSL configuration for requests library."""
        if not self.is_ssl_verification_enabled():
            logging.warning("SSL verification is DISABLED - this is insecure for production use")
            return False

        cert_path, key_path = self.get_ssl_cert_path(), self.get_ssl_key_path()
        ca_bundle = self.get_ssl_ca_bundle()

        if cert_path and key_path:
            return (cert_path, key_path)
        if ca_bundle:
            return ca_bundle
        return True

    # Dynamic property accessors with proper type casting
    def __getattr__(self, name: str):
        """Dynamic accessor for configuration properties."""
        config_mappings = {
            'get_evacuable_tag': ('EVACUABLE_TAG', str), 'get_delta': ('DELTA', int), 'get_poll_interval': ('POLL', int),
            'get_threshold': ('THRESHOLD', int), 'get_workers': ('WORKERS', int), 'get_delay': ('DELAY', int),
            'get_log_level': ('LOGLEVEL', str), 'is_smart_evacuation_enabled': ('SMART_EVACUATION', bool),
            'is_reserved_hosts_enabled': ('RESERVED_HOSTS', bool), 'is_tagged_images_enabled': ('TAGGED_IMAGES', bool),
            'is_tagged_flavors_enabled': ('TAGGED_FLAVORS', bool), 'is_tagged_aggregates_enabled': ('TAGGED_AGGREGATES', bool),
            'is_leave_disabled_enabled': ('LEAVE_DISABLED', bool), 'is_force_enable_enabled': ('FORCE_ENABLE', bool),
            'is_kdump_check_enabled': ('CHECK_KDUMP', bool), 'is_disabled': ('DISABLED', bool),
            'is_ssl_verification_enabled': ('SSL_VERIFY', bool)
        }

        if name in config_mappings:
            key, cast_type = config_mappings[name]
            return lambda: cast_type(self.get_config_value(key))

        raise AttributeError(f"'{self.__class__.__name__}' object has no attribute '{name}'")


class Metrics:
    """Simplified metrics collection and performance monitoring for InstanceHA."""

    def __init__(self):
        """Initialize metrics with counters and timing data."""
        self.counters = {}
        self.durations = {}
        self.timing_history = {}
        self.start_time = time.time()
        self._last_summary = 0.0
        logging.info("Metrics initialized")

    def increment(self, metric: str, value: int = 1) -> None:
        """Increment a counter metric."""
        self.counters[metric] = self.counters.get(metric, 0) + value
        logging.debug("Metric %s: %d", metric, self.counters[metric])

    def record_duration(self, metric: str, duration: float) -> None:
        """Record a duration metric."""
        self.durations[metric] = self.durations.get(metric, 0.0) + duration

        # Keep timing history for percentiles (last 100 values)
        if metric not in self.timing_history:
            self.timing_history[metric] = []
        self.timing_history[metric].append(duration)
        if len(self.timing_history[metric]) > 100:
            self.timing_history[metric].pop(0)

    def timer(self, operation: str):
        """Context manager for timing operations."""
        return TimingContext(self, operation)

    def get_summary(self) -> Dict[str, Any]:
        """Get metrics summary."""
        summary = {
            'uptime_seconds': time.time() - self.start_time,
            'counters': self.counters.copy(),
            'total_durations': self.durations.copy(),
        }

        # Add averages and percentiles
        for metric, history in self.timing_history.items():
            if history:
                sorted_history = sorted(history)
                n = len(sorted_history)
                summary[f'{metric}_avg'] = sum(history) / n
                summary[f'{metric}_p95'] = sorted_history[int(n * 0.95)]

        return summary

    def log_summary(self) -> None:
        """Log metrics summary."""
        summary = self.get_summary()
        logging.info("=== Metrics Summary ===")
        logging.info("Uptime: %.1f seconds", summary['uptime_seconds'])

        evacuations = summary['counters'].get('evacuations_total', 0)
        if evacuations > 0:
            success = summary['counters'].get('evacuations_successful', 0)
            logging.info("Evacuations: %d total, %d successful", evacuations, success)

        logging.info("=== End Summary ===")


class TimingContext:
    """Context manager for timing operations."""

    def __init__(self, metrics: 'Metrics', operation: str):
        self.metrics = metrics
        self.operation = operation
        self.start_time = None

    def __enter__(self):
        self.start_time = time.time()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self.start_time:
            duration = time.time() - self.start_time
            self.metrics.record_duration(f'{self.operation}_duration', duration)
            self.metrics.increment(f'{self.operation}_total')

            if exc_type is None:
                self.metrics.increment(f'{self.operation}_successful')
            else:
                self.metrics.increment(f'{self.operation}_failed')


class InstanceHAService:
    """
    Main service class that encapsulates all InstanceHA functionality.

    This class eliminates global variables and provides dependency injection
    for better testability, maintainability, and architectural cleanliness.
    """

    def __init__(self, config_manager: ConfigManager, nova_client=None):
        """
        Initialize the InstanceHA service.

        Args:
            config_manager: Configuration manager instance
            nova_client: Optional Nova client for testing (None for production)
        """
        self.config = config_manager
        self.nova_client = nova_client

        # Service state
        self.current_hash = ""
        self.hash_update_successful = True

        # Cache for performance optimization
        self._host_servers_cache = {}
        self._evacuable_flavors_cache = None
        self._evacuable_images_cache = None
        self._cache_timestamp = 0
        self._cache_lock = threading.Lock()

        # Constants
        self.UDP_IP = ''

        # Threading
        self.health_check_thread = None
        
        # Kdump state management
        self.kdump_hosts_timestamp = defaultdict(float)
        self.kdump_listener_stop_event = threading.Event()

        logging.info("InstanceHA service initialized successfully")

    def get_nova_connection(self):
        """
        Get Nova client connection with dependency injection support.

        Returns:
            Nova client connection or None if failed
        """
        if self.nova_client:
            return self.nova_client

        return self._create_nova_connection()

    def _create_nova_connection(self):
        """Create a new Nova connection using configuration."""
        try:
            cloud_name = self.config.get_cloud_name()

            username = self.config.clouds[cloud_name]["auth"]["username"]
            projectname = self.config.clouds[cloud_name]["auth"]["project_name"]
            auth_url = self.config.clouds[cloud_name]["auth"]["auth_url"]
            user_domain_name = self.config.clouds[cloud_name]["auth"]["user_domain_name"]
            project_domain_name = self.config.clouds[cloud_name]["auth"]["project_domain_name"]
            password = self.config.secure[cloud_name]["auth"]["password"]

            return nova_login(username, password, projectname, auth_url, user_domain_name, project_domain_name)
        except Exception as e:
            logging.error("Failed to create Nova connection: %s", e)
            logging.debug('Exception traceback:', exc_info=True)
            raise NovaConnectionError(f"Nova connection failed: {e}")

    def is_server_evacuable(self, server, evac_flavors=None, evac_images=None):
        """
        Check if a server is evacuable based on flavor and image tags.

        Args:
            server: Server instance to check
            evac_flavors: Optional list of evacuable flavor IDs
            evac_images: Optional list of evacuable image IDs

        Returns:
            bool: True if server is evacuable, False otherwise
        """
        if evac_flavors is None:
            evac_flavors = self.get_evacuable_flavors()
        if evac_images is None:
            evac_images = self.get_evacuable_images()

        # Determine evacuation logic based on configuration
        images_enabled = self.config.is_tagged_images_enabled()
        flavors_enabled = self.config.is_tagged_flavors_enabled()

        # If NEITHER tagged images NOR tagged flavors are enabled, evacuate all instances (backward compatibility)
        if not images_enabled and not flavors_enabled:
            return True

        # If tagging is enabled but no tagged resources exist, evacuate all servers
        if images_enabled and flavors_enabled:
            # Both enabled - if neither has tagged resources, evacuate all
            if not evac_images and not evac_flavors:
                logging.info("No tagged images or flavors found - evacuating all servers")
                return True
        elif images_enabled:
            # Only image tagging enabled - if no tagged images, evacuate all
            if not evac_images:
                logging.info("No tagged images found - evacuating all servers")
                return True
        elif flavors_enabled:
            # Only flavor tagging enabled - if no tagged flavors, evacuate all
            if not evac_flavors:
                logging.info("No tagged flavors found - evacuating all servers")
                return True

        # Tagged resources exist, so we need to check if this server matches

        # Check if server uses evacuable image
        image_matches = False
        if images_enabled and evac_images:
            # Use pre-cached list if available
            server_image_id = self._get_server_image_id(server)
            if server_image_id and server_image_id in evac_images:
                image_matches = True
            else:
                # Fall back to per-server checking if cache is empty
                if self.is_server_image_evacuable(server):
                    image_matches = True

        # Check if server uses evacuable flavor
        flavor_matches = False
        if flavors_enabled and evac_flavors:
            try:
                flavor_extra_specs = server.flavor.get('extra_specs', {})
                evacuable_tag = self.config.get_evacuable_tag()

                # Check if evacuable_tag is an exact key match or part of a key (e.g., trait:CUSTOM_HA)
                matching_key = None
                if evacuable_tag in flavor_extra_specs:
                    matching_key = evacuable_tag
                else:
                    # Look for the tag in composite keys like "trait:CUSTOM_HA"
                    for key in flavor_extra_specs:
                        if evacuable_tag in key:
                            matching_key = key
                            break

                if matching_key:
                    value = flavor_extra_specs[matching_key]
                    if str(value).lower() == 'true':
                        flavor_matches = True
            except (AttributeError, KeyError, TypeError):
                logging.debug("Could not check flavor extra specs for server %s", server.id)

        # Determine if server should be evacuated based on enabled tagging
        should_evacuate = False

        if images_enabled and flavors_enabled:
            # Both enabled: evacuate if server matches EITHER criteria (OR logic)
            should_evacuate = image_matches or flavor_matches
        elif images_enabled:
            # Only image tagging enabled: evacuate only if image matches
            should_evacuate = image_matches
        elif flavors_enabled:
            # Only flavor tagging enabled: evacuate only if flavor matches
            should_evacuate = flavor_matches

        if should_evacuate:
            return True

        # Server doesn't match evacuable criteria - provide specific feedback
        criteria = []
        if images_enabled and evac_images:
            criteria.append("evacuable images")
        if flavors_enabled and evac_flavors:
            criteria.append("evacuable flavors")

        if criteria:
            criteria_str = " or ".join(criteria)
            logging.warning("Instance %s is not evacuable: not using any of the defined %s tagged with '%s'",
                           server.id, criteria_str, self.config.get_evacuable_tag())
        return False

    def get_evacuable_flavors(self, connection=None):
        """
        Get list of evacuable flavor IDs with caching.

        Args:
            connection: Optional Nova connection

        Returns:
            List of evacuable flavor IDs
        """
        if not self.config.is_tagged_flavors_enabled():
            return []

        if connection is None:
            connection = self.get_nova_connection()

        if connection is None:
            logging.error("No Nova connection available for flavor caching")
            return []

        if self._evacuable_flavors_cache is None:
            try:
                flavors = connection.flavors.list(is_public=None)
                evacuable_tag = self.config.get_evacuable_tag()

                self._evacuable_flavors_cache = []
                for flavor in flavors:
                    try:
                        flavor_keys = flavor.get_keys()
                        # Check if evacuable_tag is an exact key match or part of a key (e.g., trait:CUSTOM_HA)
                        matching_key = None
                        if evacuable_tag in flavor_keys:
                            matching_key = evacuable_tag
                        else:
                            # Look for the tag in composite keys like "trait:CUSTOM_HA"
                            for key in flavor_keys:
                                if evacuable_tag in key:
                                    matching_key = key
                                    break

                        if matching_key:
                            value = flavor_keys[matching_key]
                            if str(value).lower() == 'true':
                                self._evacuable_flavors_cache.append(flavor.id)
                                logging.debug("Added flavor %s to evacuable cache (key: %s, value: %s)", flavor.id, matching_key, value)
                    except Exception as e:
                        logging.debug("Could not check keys for flavor %s: %s", flavor.id, e)

                logging.debug("Cached %d evacuable flavors from %d total flavors",
                             len(self._evacuable_flavors_cache), len(flavors))
            except Exception as e:
                logging.error("Failed to get evacuable flavors: %s", e)
                logging.debug('Exception traceback:', exc_info=True)
                self._evacuable_flavors_cache = []

        return self._evacuable_flavors_cache

    def get_evacuable_images(self, connection=None):
        """
        Get list of evacuable image IDs with caching.

        Attempts multiple methods to access image information and cache evacuable image IDs.

        Args:
            connection: Optional Nova connection

        Returns:
            List of evacuable image IDs
        """
        if not self.config.is_tagged_images_enabled():
            return []

        if connection is None:
            connection = self.get_nova_connection()

        if connection is None:
            logging.error("No Nova connection available for image caching")
            return []

        if self._evacuable_images_cache is None:
            try:
                images = []
                evacuable_tag = self.config.get_evacuable_tag()

                # Method 1: Try direct Glance access through Nova client
                try:
                    if hasattr(connection, 'glance'):
                        images = connection.glance.list()
                        logging.debug("Retrieved %d images via Nova glance client", len(images))
                except Exception as e:
                    logging.debug("Nova glance client access failed: %s", e)

                # Method 2: Try Nova's images interface
                if not images:
                    try:
                        if hasattr(connection, 'images'):
                            images = connection.images.list()
                            logging.debug("Retrieved %d images via Nova images interface", len(images))
                    except Exception as e:
                        logging.debug("Nova images interface access failed: %s", e)

                # Method 3: Try to create a separate Glance client
                if not images:
                    try:
                        from glanceclient import client as glance_client
                        from keystoneauth1 import session as ksc_session

                        # Get session from Nova client
                        if hasattr(connection, 'api') and hasattr(connection.api, 'session'):
                            session = connection.api.session
                            glance = glance_client.Client('2', session=session)
                            images = list(glance.images.list())
                            logging.debug("Retrieved %d images via separate Glance client", len(images))
                    except ImportError:
                        logging.debug("Glance client not available for import")
                    except Exception as e:
                        logging.debug("Separate Glance client access failed: %s", e)

                # Process images to find evacuable ones
                if images:
                    self._evacuable_images_cache = []
                    for image in images:
                        # Check for tags (Glance v2 API) - tags are just strings, not key-value pairs
                        # Need to check if evacuable_tag is contained in any of the tag strings
                        if hasattr(image, 'tags') and self._check_evacuable_tag(image.tags, evacuable_tag):
                            self._evacuable_images_cache.append(image.id)
                        # Check for metadata/properties (fallback) - these might have composite keys
                        elif hasattr(image, 'metadata') and self._check_evacuable_tag(image.metadata, evacuable_tag):
                            self._evacuable_images_cache.append(image.id)
                        elif hasattr(image, 'properties') and self._check_evacuable_tag(image.properties, evacuable_tag):
                            self._evacuable_images_cache.append(image.id)

                    logging.debug("Cached %d evacuable images from %d total images",
                                 len(self._evacuable_images_cache), len(images))
                else:
                    # No images retrieved, fall back to per-server checking
                    logging.warning("Could not retrieve images for caching. Image evacuation will use per-server checking.")
                    self._evacuable_images_cache = []

            except Exception as e:
                logging.error("Failed to get evacuable images: %s", e)
                logging.debug('Exception traceback:', exc_info=True)
                self._evacuable_images_cache = []

        return self._evacuable_images_cache

    def _get_server_image_id(self, server):
        """
        Extract image ID from server object, handling different data formats.

        Args:
            server: Server instance

        Returns:
            str: Image ID or None if not found
        """
        try:
            if hasattr(server, 'image') and server.image:
                if isinstance(server.image, dict):
                    return server.image.get('id')
                elif isinstance(server.image, str):
                    return server.image
                elif hasattr(server.image, 'id'):
                    return server.image.id
        except (AttributeError, TypeError):
            pass
        return None

    def _check_evacuable_tag(self, data, evacuable_tag):
        """Generic method to check for evacuable tag in different data structures."""
        try:
            if isinstance(data, dict):
                # Dictionary - check for exact key match or composite keys
                if evacuable_tag in data:
                    return str(data[evacuable_tag]).lower() == 'true'
                return any(evacuable_tag in key and str(data[key]).lower() == 'true' for key in data)
            elif hasattr(data, '__iter__') and not isinstance(data, str):
                # List/tags - check for exact match or substring match
                return evacuable_tag in data or any(evacuable_tag in item for item in data)
            return False
        except (AttributeError, TypeError):
            return False

    def is_server_image_evacuable(self, server, connection=None):
        """
        Check if a server's image is tagged as evacuable (fallback method).

        This method is used when pre-caching fails. It attempts to check
        individual server images for evacuable tags.

        Args:
            server: Server instance to check
            connection: Optional Nova connection

        Returns:
            bool: True if server's image is evacuable, False otherwise
        """
        if not self.config.is_tagged_images_enabled():
            return False

        if connection is None:
            connection = self.get_nova_connection()

        if connection is None:
            logging.debug("No Nova connection available for image evacuability check")
            return False

        try:
            server_image_id = self._get_server_image_id(server)
            if not server_image_id:
                logging.debug("Server %s has no image ID", server.id)
                return False

            evacuable_tag = self.config.get_evacuable_tag()

            # Try multiple methods to get image details and check evacuability
            for get_method, check_attrs in [
                (lambda: connection.glance.get(server_image_id), ['tags']),
                (lambda: connection.images.get(server_image_id), ['metadata', 'properties'])
            ]:
                try:
                    image = get_method()
                    for attr in check_attrs:
                        if hasattr(image, attr) and self._check_evacuable_tag(getattr(image, attr, {}), evacuable_tag):
                            return True
                except (AttributeError, TypeError, KeyError) as e:
                    logging.debug("Error checking image attributes: %s", e)
                    continue

            logging.debug("Could not determine evacuability of image %s for server %s", server_image_id, server.id)
            return False

        except Exception as e:
            logging.debug("Error checking image evacuability for server %s: %s", server.id, e)
            return False

        return False

    def is_aggregate_evacuable(self, connection, host):
        """
        Check if a host is part of an aggregate that has been tagged as evacuable.

        Args:
            connection: Nova connection
            host: Host name to check

        Returns:
            bool: True if host is in evacuable aggregate, False otherwise
        """
        try:
            aggregates = connection.aggregates.list()
            evacuable_tag = self.config.get_evacuable_tag()

            # Use existing tag checking logic and filter in one pass
            return any(host in agg.hosts for agg in aggregates
                      if self._check_evacuable_tag(agg.metadata, evacuable_tag))
        except Exception as e:
            logging.error("Failed to check aggregate evacuability for %s: %s", host, e)
            logging.debug('Exception traceback:', exc_info=True)
            return False

    def get_hosts_with_servers_cached(self, connection, services):
        """
        Efficiently cache server lists for all hosts to avoid repeated API calls.

        Args:
            connection: Nova client connection
            services: List of compute services

        Returns:
            Dict mapping host names to their server lists
        """
        host_servers = {}

        for service in services:
            if service.host not in host_servers:
                try:
                    servers = connection.servers.list(search_opts={
                        'host': service.host,
                        'all_tenants': 1
                    })
                    host_servers[service.host] = servers
                    logging.debug("Cached %d servers for host %s", len(servers), service.host)
                except Exception as e:
                    logging.warning("Failed to get servers for host %s: %s", service.host, e)
                    logging.debug('Exception traceback:', exc_info=True)
                    host_servers[service.host] = []

        return host_servers

    def filter_hosts_with_servers(self, services, host_servers_cache):
        """
        Efficiently filter out hosts that have no running servers.

        Args:
            services: List of compute services to filter
            host_servers_cache: Pre-cached mapping of host -> servers

        Returns:
            List of services that have servers running
        """
        return [
            service for service in services
            if host_servers_cache.get(service.host, [])
        ]

    def filter_hosts_with_evacuable_servers(self, services, host_servers_cache, flavors=None, images=None):
        """
        Efficiently filter hosts that have evacuable servers.

        Args:
            services: List of compute services to filter
            host_servers_cache: Pre-cached mapping of host -> servers
            flavors: Optional list of evacuable flavor IDs
            images: Optional list of evacuable image IDs

        Returns:
            List of services that have evacuable servers
        """
        if flavors is None:
            flavors = self.get_evacuable_flavors()
        if images is None:
            images = self.get_evacuable_images()

        services_with_evacuable = []

        for service in services:
            servers = host_servers_cache.get(service.host, [])

            # Check if any server on this host is evacuable
            has_evacuable = any(
                self.is_server_evacuable(server, flavors, images)
                for server in servers
            )

            if has_evacuable:
                services_with_evacuable.append(service)
                logging.debug("Host %s has evacuable servers", service.host)

        return services_with_evacuable

    def start_health_check_server(self):
        """Start the health check server in a separate thread."""
        if self.health_check_thread is None or not self.health_check_thread.is_alive():
            self.health_check_thread = threading.Thread(target=self._run_health_check_server)
            self.health_check_thread.daemon = True
            self.health_check_thread.start()
            logging.info("Health check server started")

    def _run_health_check_server(self):
        """Simple health check server implementation."""
        from http.server import HTTPServer, BaseHTTPRequestHandler

        # Capture self reference for use in handler
        service_instance = self

        class HealthHandler(BaseHTTPRequestHandler):
            def do_GET(self):
                if service_instance.hash_update_successful:
                    self.send_response(200)
                    self.send_header("Content-type", "text/plain")
                    self.end_headers()
                    self.wfile.write(service_instance.current_hash.encode('utf-8'))
                else:
                    self.send_response(500)
                    self.send_header("Content-type", "text/plain")
                    self.end_headers()
                    self.wfile.write(b"Error: Hash not updated properly.")

        HTTPServer(('', 8080), HealthHandler).serve_forever()

    def clear_cache(self):
        """Clear all cached data to force refresh."""
        self._host_servers_cache.clear()
        self._evacuable_flavors_cache = None
        self._evacuable_images_cache = None
        self._cache_timestamp = 0
        logging.debug("Service cache cleared")

    def refresh_evacuable_cache(self, connection=None, force=False):
        """Refresh evacuable flavors and images cache with intelligent timing."""
        current_time = time.time()
        cache_age = current_time - self._cache_timestamp

        # Refresh cache every 300 seconds (5 minutes) or when forced
        if force or cache_age > 300 or not self._evacuable_flavors_cache:
            logging.info("Refreshing evacuable cache (age: %.1f seconds)", cache_age)
            self._evacuable_flavors_cache = None
            self._evacuable_images_cache = None
            self._cache_timestamp = current_time

            # Force re-cache
            evac_flavors = self.get_evacuable_flavors(connection)
            evac_images = self.get_evacuable_images(connection)

            logging.info("Cache refreshed: %d flavors, %d images", len(evac_flavors), len(evac_images))
            return True
        return False


# Initialize global configuration manager
try:
    config_manager = ConfigManager()
    logging.basicConfig(format='%(asctime)s %(levelname)s %(message)s', level=config_manager.get_log_level())
    logging.info("Configuration loaded successfully")
except ConfigurationError as e:
    logging.error("Configuration failed: %s", e)
    sys.exit(1)

# Global service instance will be created in main()

def _kdump_udp_listener(service):
    """Background UDP listener for kdump messages."""

    try:
        udp_ip = service.UDP_IP if service.UDP_IP else '0.0.0.0'
        udp_port = service.config.get_udp_port()

        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(1.0)
        sock.bind((udp_ip, udp_port))
        logging.info('Kdump UDP listener started on %s:%s' % (udp_ip, udp_port))

        while not service.kdump_listener_stop_event.is_set():
            try:
                data, _, _, address = sock.recvmsg(65535, 1024, 0)
                logging.debug('Received UDP message from %s, length: %d' % (address[0], len(data)))

                if len(data) >= 8:
                    # Try both byte orders for magic number (fence_kdump compatibility)
                    magic_native = struct.unpack('I', data[:4])[0]
                    magic_network = struct.unpack('!I', data[:4])[0]

                    if magic_native == 0x1B302A40 or magic_network == 0x1B302A40:
                        try:
                            hostname = socket.gethostbyaddr(address[0])[0].split('.', 1)[0]
                            service.kdump_hosts_timestamp[hostname] = time.time()
                            logging.info('Kdump message received from host: %s' % hostname)

                            # Cleanup old entries every 100 messages (approx)
                            if len(service.kdump_hosts_timestamp) > 100:
                                cutoff = time.time() - 300
                                old_keys = [k for k, v in service.kdump_hosts_timestamp.items() if v < cutoff]
                                for k in old_keys:
                                    del service.kdump_hosts_timestamp[k]
                        except Exception as e:
                            logging.debug('Failed to process kdump message from %s: %s' % (address[0], e))
                    else:
                        logging.debug('Invalid magic number from %s: 0x%x / 0x%x' % (address[0], magic_native, magic_network))
            except socket.timeout:
                continue
            except OSError as e:
                if not service.kdump_listener_stop_event.is_set():
                    logging.debug('UDP listener socket error: %s' % e)
                continue
    except Exception as e:
        logging.error('Kdump listener failed to start: %s' % e)
    finally:
        try:
            sock.close()
        except (OSError, AttributeError):
            # Socket may already be closed or never opened
            pass
        logging.info('Kdump UDP listener stopped')

def _check_kdump_single(host, service):
    """Check if host is kdumping within timeout period."""
    hostname = host.split('.', 1)[0]
    last_seen = service.kdump_hosts_timestamp.get(hostname, 0)
    kdump_timeout = service.config.get_config_value('KDUMP_TIMEOUT')

    if last_seen > 0 and (time.time() - last_seen) <= kdump_timeout:
        return True

    start_time = time.time()
    while (time.time() - start_time) < kdump_timeout:
        time.sleep(1)
        if service.kdump_hosts_timestamp.get(hostname, 0) > start_time:
            return True

    return False

def _aggregate_ids(conn, service):

    aggregates = conn.aggregates.list()
    return [ag.id for ag in aggregates if service.host in ag.hosts]


def _host_evacuate(connection, failed_service, service):
    """
    Evacuate all instances from a failed host.

    This function handles the evacuation of instances from a failed compute host,
    with support for filtering by tags and smart evacuation with threading.

    Args:
        connection: Nova client connection
        service: Compute service object for the failed host

    Returns:
        bool: True if evacuation was successful, False otherwise
    """
    host = failed_service.host
    result = True

    # Get evacuable images and flavors using service instance
    images = service.get_evacuable_images(connection)
    flavors = service.get_evacuable_flavors(connection)

    # Get all servers on the failed host
    servers = connection.servers.list(search_opts={'host': host, 'all_tenants': 1})
    servers = [server for server in servers if server.status in {'ACTIVE', 'ERROR', 'STOPPED'}]

    if flavors or images:
        logging.debug("Filtering images and flavors: %s %s", repr(flavors), repr(images))
        # Identify all evacuable servers
        logging.debug("Checking %s", repr(servers))
        evacuables = [server for server in servers if service.is_server_evacuable(server, flavors, images)]
        logging.debug("Evacuating %s", repr(evacuables))
    else:
        logging.debug("Evacuating all images and flavors")
        evacuables = servers

    if evacuables == []:
        logging.info("Nothing to evacuate")
        return True

    # Sleep for configured delay
    time.sleep(service.config.get_delay())

    # Use smart evacuation if enabled
    if service.config.is_smart_evacuation_enabled():
        logging.debug("Using smart evacuation with %d workers", service.config.get_workers())

        # Use ThreadPoolExecutor to poll the evacuation status
        with concurrent.futures.ThreadPoolExecutor(max_workers=service.config.get_workers()) as executor:
            future_to_server = {executor.submit(_server_evacuate_future, connection, server): server for server in evacuables}

            for future in concurrent.futures.as_completed(future_to_server):
                server = future_to_server[future]
                try:
                    data = future.result()
                    if data is False:
                        logging.debug('Evacuation of %s failed 5 times in a row', server.id)
                        # Update DISABLED reason so we don't try to evacuate again
                        try:
                            connection.services.disable_log_reason(failed_service.id, "evacuation FAILED: %s" % datetime.now().isoformat())
                            logging.info('Evacuation failed. Updated disabled reason for host %s', host)
                        except Exception as e:
                            logging.error('Failed to update disable_reason for host %s. Error: %s', host, e)
                            logging.debug('Exception traceback:', exc_info=True)
                        return data
                except Exception as exc:
                    logging.error('Evacuation generated an exception: %s', exc)
                    logging.debug('Exception traceback:', exc_info=True)
                    # Try to update service status to reflect evacuation failure
                    try:
                        failed_service = [s for s in connection.services.list(host=host, binary='nova-compute')
                                        if s.host == host and s.binary == 'nova-compute'][0]
                        connection.services.disable_log_reason(failed_service.id, "evacuation FAILED: %s" % datetime.now().isoformat())
                        logging.info('Updated service status after evacuation exception for host %s', host)
                    except Exception as status_update_error:
                        logging.warning('Failed to update service status after evacuation error: %s', status_update_error)
                    return False
                else:
                    logging.info('%r evacuated successfully', server.id)
    else:
        # Use traditional fire-and-forget approach
        logging.debug("Using traditional evacuation approach")
        for server in evacuables:
            logging.debug("Processing %s", server)
            if hasattr(server, 'id'):
                response = _server_evacuate(connection, server.id)
                if response["accepted"]:
                    logging.debug("Evacuated %s from %s: %s", response["uuid"], host, response["reason"])
                else:
                    logging.warning("Evacuation of %s on %s failed: %s", response["uuid"], host, response["reason"])
                    result = False
            else:
                logging.error("Could not evacuate instance: %s", server.to_dict())
                # Should a malformed instance result in a failed evacuation?
                # result = False
        return result

    return result


def _server_evacuate(connection, server):
    success = False  # Initialize success variable
    error_message = ""

    try:
        logging.debug("Resurrecting instance: %s" % server)
        response, dictionary = connection.servers.evacuate(server=server)
        if response is None:
            error_message = "No response received while evacuating instance"
        elif response.status_code == 200:
            success = True
            error_message = response.reason
        else:
            error_message = response.reason
    except NotFound:
        error_message = "Instance %s not found" % server
    except Forbidden:
        error_message = "Access denied while evacuating instance %s" % server
    except Unauthorized:
        error_message = "Authentication failed while evacuating instance %s" % server
    except Exception as e:
        error_message = "Error while evacuating instance %s: %s" % (server, e)

    return {
        "uuid": server,
        "accepted": success,
        "reason": error_message,
    }


def _server_evacuation_status(connection, server):
    """Check the status of a server evacuation by querying recent migrations."""
    if not connection or not server:
        return {"completed": False, "error": True}

    try:
        query_time = (datetime.now() - timedelta(minutes=5)).isoformat()
        migrations = connection.migrations.list(
            instance_uuid=str(server),
            migration_type='evacuation',
            changes_since=query_time,
            limit='1000'
        )

        if not migrations:
            return {"completed": False, "error": True}

        # Get status from most recent migration
        migration = migrations[0]
        status = getattr(migration, 'status', None) or getattr(migration, '_info', {}).get('status')

        if not status:
            return {"completed": False, "error": True}

        completed = status in ['completed', 'done']
        error = status in ['error', 'failed', 'cancelled']

        return {"completed": completed, "error": error}

    except Exception as e:
        logging.error("Failed to check evacuation status for %s: %s", server, e)
        return {"completed": False, "error": True}


def _server_evacuate_future(connection, server):
    """
    Evacuate a server and monitor the evacuation process until completion.

    Args:
        connection: Nova client connection
        server: Server object to evacuate

    Returns:
        bool: True if evacuation completed successfully, False otherwise
    """
    # Constants for evacuation process
    MAX_RETRIES = 5
    INITIAL_WAIT_TIME = 10  # seconds
    POLL_INTERVAL = 5  # seconds
    RETRY_WAIT_TIME = 5  # seconds
    MAX_TOTAL_TIMEOUT = 300  # 5 minutes maximum timeout

    # Initialize variables
    error_count = 0
    result = False
    start_time = time.time()

    # Validate server object
    if not hasattr(server, 'id'):
        logging.warning("Could not evacuate instance - missing server ID: %s",
                       getattr(server, 'to_dict', lambda: str(server))())
        return False

    logging.info("Processing evacuation for server %s", server.id)

    try:
        # Initiate evacuation
        response = _server_evacuate(connection, server.id)

        if not response["accepted"]:
            logging.warning("Evacuation of %s on %s failed: %s",
                           response["uuid"], server.id, response["reason"])
            return False

        logging.debug("Starting evacuation of %s", response["uuid"])

        # Initial wait before polling
        time.sleep(INITIAL_WAIT_TIME)

        # Monitor evacuation progress
        while True:
            # Check for overall timeout
            if time.time() - start_time > MAX_TOTAL_TIMEOUT:
                logging.error("Evacuation of %s timed out after %d seconds. Giving up.",
                             response["uuid"], MAX_TOTAL_TIMEOUT)
                return False

            try:
                status = _server_evacuation_status(connection, server.id)

                if status["completed"]:
                    logging.info("Evacuation of %s completed successfully", response["uuid"])
                    return True

                if status["error"]:
                    error_count += 1
                    if error_count >= MAX_RETRIES:
                        logging.error("Failed evacuating %s %d times. Giving up.",
                                     response["uuid"], MAX_RETRIES)
                        return False

                    logging.warning("Evacuation of instance %s failed %d times. Retrying...",
                                   response["uuid"], error_count)
                    time.sleep(RETRY_WAIT_TIME)
                    continue

                # Evacuation still in progress
                logging.debug("Evacuation of %s still in progress", response["uuid"])
                time.sleep(POLL_INTERVAL)

            except Exception as e:
                error_count += 1
                logging.error("Error checking evacuation status for %s: %s",
                             response["uuid"], str(e))
                logging.debug('Exception traceback:', exc_info=True)

                if error_count >= MAX_RETRIES:
                    logging.error("Too many errors checking evacuation status for %s. Giving up.",
                                 response["uuid"])
                    return False

                logging.warning("Retrying evacuation status check for %s in %d seconds (attempt %d/%d)...",
                               response["uuid"], RETRY_WAIT_TIME, error_count, MAX_RETRIES)
                time.sleep(RETRY_WAIT_TIME)
                continue

    except Exception as e:
        logging.error("Unexpected error during evacuation of server %s: %s",
                     server.id, str(e))
        logging.debug('Exception traceback:', exc_info=True)
        return False

    # This should not be reached, but return False as fallback
    return False


def nova_login(username, password, projectname, auth_url, user_domain_name, project_domain_name):
    try:
        loader = loading.get_plugin_loader("password")
        auth = loader.load_from_options(
            auth_url=auth_url,
            username=username,
            password=password,
            project_name=projectname,
            user_domain_name=user_domain_name,
            project_domain_name=project_domain_name,
        )

        session = ksc_session.Session(auth=auth)
        nova = client.Client("2.59", session=session)
        nova.versions.get_current()
    except DiscoveryFailure as e:
        logging.error("Nova login failed: Discovery Failure: %s" % e)
        return None
    except Unauthorized as e:
        logging.error("Nova login failed: Unauthorized: %s" % e)
        return None
    except Exception as e:
        logging.error("Nova login failed: %s" % e)
        logging.debug('Exception traceback:', exc_info=True)
        return None

    logging.info("Nova login successful")
    return nova


def _host_disable(connection, service):
    """
    Disable a compute service by forcing it down and logging the reason.

    This function performs two operations:
    1. Forces the compute service down (required for evacuation)
    2. Logs the reason for the disable operation

    Args:
        connection: Nova client connection
        service: Service object to disable

    Returns:
        bool: True if both operations succeeded, False otherwise
    """
    # Input validation
    if not connection:
        logging.error("Cannot disable service - no connection provided")
        return False

    if not service:
        logging.error("Cannot disable service - no service object provided")
        return False

    if not hasattr(service, 'id'):
        logging.error("Cannot disable service - service object missing ID: %s",
                     getattr(service, 'to_dict', lambda: str(service))())
        return False

    if not hasattr(service, 'host'):
        logging.error("Cannot disable service - service object missing host: %s",
                     getattr(service, 'to_dict', lambda: str(service))())
        return False

    service_info = f"service {getattr(service, 'binary', 'unknown')} on host {service.host}"

    # Step 1: Force the service down
    logging.info("Forcing %s down before evacuation", service.host)

    try:
        connection.services.force_down(service.id, True)
        logging.debug("Successfully forced down %s", service_info)
        force_down_success = True

    except NotFound as e:
        logging.error("Failed to force-down %s. Resource not found: %s", service_info, e)
        logging.debug("Exception traceback:", exc_info=True)
        return False

    except Conflict as e:
        logging.error("Failed to force-down %s. Conflicting operation: %s", service_info, e)
        logging.debug("Exception traceback:", exc_info=True)
        return False

    except Exception as e:
        logging.error("Failed to force-down %s. Unexpected error: %s", service_info, e)
        logging.debug("Exception traceback:", exc_info=True)
        return False

    # Step 2: Log the reason for disabling (only if force_down succeeded)
    try:
        disable_reason = f"instanceha evacuation: {datetime.now().isoformat()}"
        connection.services.disable_log_reason(service.id, disable_reason)
        logging.info("Successfully disabled %s with reason: %s", service_info, disable_reason)
        return True

    except NotFound as e:
        logging.error("Failed to log disable reason for %s. Resource not found: %s", service_info, e)
        logging.debug("Exception traceback:", exc_info=True)
        # Service is forced down but reason logging failed - this is not critical
        logging.warning("Service %s is forced down but reason logging failed. Manual cleanup may be needed.", service_info)
        return False

    except Conflict as e:
        logging.error("Failed to log disable reason for %s. Conflicting operation: %s", service_info, e)
        logging.debug("Exception traceback:", exc_info=True)
        # Service is forced down but reason logging failed
        logging.warning("Service %s is forced down but reason logging failed. Manual cleanup may be needed.", service_info)
        return False

    except Exception as e:
        logging.error("Failed to log disable reason for %s. Unexpected error: %s", service_info, e)
        logging.debug("Exception traceback:", exc_info=True)
        # Service is forced down but reason logging failed
        logging.warning("Service %s is forced down but reason logging failed. Manual cleanup may be needed.", service_info)
        return False


def _check_kdump(stale_services, service):
    """Check for kdump messages using background UDP listener and filter hosts individually."""
    if not stale_services:
        logging.debug("No stale services to check for kdump")
        return []

    logging.info("Checking %d hosts for kdump activity", len(stale_services))
    kdumping_hosts = []
    uncertain_hosts = []

    # Quick check: hosts with recent kdump messages
    for svc in stale_services:
        hostname = svc.host.split('.', 1)[0]
        last_seen = service.kdump_hosts_timestamp.get(hostname, 0)
        kdump_timeout = service.config.get_config_value('KDUMP_TIMEOUT')
        if last_seen > 0 and (time.time() - last_seen) <= kdump_timeout:
            kdumping_hosts.append(svc.host)
            logging.info('Host %s is kdumping, skipping evacuation' % svc.host)
        else:
            uncertain_hosts.append(svc)

    # Wait for delayed kdump starts (parallel)
    if uncertain_hosts:
        logging.info('Checking %d hosts for delayed kdump start' % len(uncertain_hosts))
        workers = service.config.get_workers()
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {executor.submit(_check_kdump_single, s.host, service): s for s in uncertain_hosts}
            kdump_timeout = service.config.get_config_value('KDUMP_TIMEOUT')

            # Process results as they complete, handling timeouts gracefully
            try:
                for future in concurrent.futures.as_completed(futures, timeout=kdump_timeout + 5):
                    try:
                        if future.result():
                            kdumping_hosts.append(futures[future].host)
                            logging.info('Host %s started kdumping, skipping evacuation' % futures[future].host)
                    except Exception:
                        pass
            except concurrent.futures.TimeoutError:
                # Some futures didn't complete within timeout - that's OK, just log it
                pending = [futures[f] for f in futures if not f.done()]
                if pending:
                    logging.debug('Kdump check timed out for %d hosts: %s' % (len(pending), [s.host for s in pending]))

    # Remove kdumping hosts from evacuation
    to_evacuate = [s for s in stale_services if s.host not in kdumping_hosts]
    if kdumping_hosts:
        logging.info('Total hosts skipped due to kdumping: %d' % len(kdumping_hosts))

    return to_evacuate


def _host_enable(connection, service, reenable=False):
    if reenable:
        try:
            logging.info('Unsetting force-down on host %s after evacuation', service.host)
            connection.services.force_down(service.id, False)
            logging.info('Successfully unset force-down on host %s', service.host)
        except Exception as e:
            logging.error('Could not unset force-down for %s. Please check the host status and perform manual cleanup if necessary: %s', service.host, e)
            return False

    else:
        last_error = None
        for _ in range(3):
            try:
                logging.info('Trying to enable %s', service.host)
                connection.services.enable(service.id)
                logging.info('Host %s is now enabled', service.host)
                break
            except Exception as e:
                last_error = e
                logging.warning('Failed to enable %s, retrying: %s', service.host, e)
                continue
        else:
            # All retries failed
            logging.error('Failed to enable %s after 3 attempts. Last error: %s', service.host, last_error)
            return False

    return True


def _redfish_reset(url, user, passwd, timeout, action):
    """Perform a Redfish reset operation on a computer system."""
    if not all([url, user, passwd, action]):
        logging.error("Redfish reset failed: missing required parameters")
        return False

    valid_actions = ['On', 'ForceOff', 'GracefulShutdown', 'GracefulRestart', 'ForceRestart', 'Nmi', 'ForceOn', 'PushPowerButton']
    if action not in valid_actions:
        logging.error("Redfish reset failed: invalid action '%s'", action)
        return False

    timeout = max(5, min(300, int(timeout or 30)))  # Clamp between 5-300 seconds
    url = url.rstrip('/')
    reset_url = f"{url}/Actions/ComputerSystem.Reset"

    payload = {"ResetType": action}
    headers = {'Content-Type': 'application/json', 'Accept': 'application/json'}

    try:
        # Handle SSL config type issue - convert tuple to cert parameter
        ssl_config = config_manager.get_requests_ssl_config()
        if isinstance(ssl_config, tuple):
            # Client cert authentication
            response = requests.post(reset_url, json=payload, headers=headers,
                                   auth=(user, passwd), cert=ssl_config, timeout=timeout)
        else:
            # Standard SSL verification
            response = requests.post(reset_url, json=payload, headers=headers,
                                   auth=(user, passwd), verify=ssl_config, timeout=timeout)

        if response.status_code in [200, 204]:
            logging.info("Redfish reset successful: %s on %s", action, url)
            return True
        else:
            logging.error("Redfish reset failed: status %d for %s", response.status_code, url)
            return False

    except Exception as e:
        logging.error("Redfish reset failed for %s: %s", url, e)
        return False

def _bmh_fence(token, namespace, host, action):
    """Fence a BareMetal Host using Kubernetes API."""
    if not all([token, namespace, host]) or action not in ['on', 'off']:
        logging.error("BMH fencing failed: invalid parameters")
        return False

    base_url = f"https://kubernetes.default.svc/apis/metal3.io/v1alpha1/namespaces/{namespace}/baremetalhosts/{host}"
    headers = {'Authorization': f'Bearer {token}', 'Content-Type': 'application/merge-patch+json'}
    cacert = '/var/run/secrets/kubernetes.io/serviceaccount/ca.crt'

    if not os.path.exists(cacert):
        logging.error("BMH fencing failed: CA certificate not found")
        return False

    try:
        if action == 'off':
            annotation_value = '{"mode": "hard"}'
        else:
            annotation_value = None

        data = {"metadata": {"annotations": {"reboot.metal3.io/iha": annotation_value}}}

        response = requests.patch(f"{base_url}?fieldManager=kubectl-patch",
                                headers=headers, json=data, verify=cacert, timeout=10)
        response.raise_for_status()

        if action == 'off':
            return _bmh_wait_for_power_off(base_url, headers, cacert, host, 30, 3)
        else:
            logging.info("BMH power on successful for %s", host)
            return True

    except Exception as e:
        logging.error("BMH fencing failed for %s: %s", host, e)
        return False


def _bmh_wait_for_power_off(get_url, headers, cacert, host, timeout, poll_interval):
    """
    Wait for BareMetal Host to power off by polling its status.

    Args:
        get_url: URL to get BMH status
        headers: HTTP headers for authentication
        cacert: Path to CA certificate file
        host: Host name for logging
        timeout: Maximum time to wait in seconds
        poll_interval: Time between status checks in seconds

    Returns:
        bool: True if host powered off within timeout, False otherwise
    """
    REQUEST_TIMEOUT = 10  # seconds for individual HTTP requests

    logging.debug("Waiting up to %d seconds for %s to power off", timeout, host)

    timeout_end = time.time() + timeout

    while time.time() < timeout_end:
        time.sleep(poll_interval)

        try:
            response = requests.get(get_url, headers=headers, verify=cacert, timeout=REQUEST_TIMEOUT)
            response.raise_for_status()

        except requests.exceptions.Timeout:
            logging.warning("Timeout checking power status for %s, continuing to wait", host)
            continue
        except requests.exceptions.HTTPError as e:
            logging.warning("HTTP error checking power status for %s: %s, continuing to wait", host, e)
            continue
        except requests.exceptions.RequestException as e:
            logging.warning("Request error checking power status for %s: %s, continuing to wait", host, e)
            continue

        # Parse JSON response
        try:
            status_data = response.json()
        except (json.JSONDecodeError, ValueError) as e:
            logging.warning("Invalid JSON response checking power status for %s: %s, continuing to wait", host, e)
            continue

        # Extract power status
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

    # Timeout reached
    logging.error("BMH power off timeout: %s did not power off within %d seconds", host, timeout)
    return False


def _execute_fence_operation(host, action, fencing_data):
    """Unified fencing execution with agent-specific dispatch and validation."""
    agent = fencing_data.get("agent", "").lower()

    def _validate_params(required_params, agent_name):
        missing = [p for p in required_params if p not in fencing_data]
        if missing:
            logging.error("Missing %s params for %s: %s", agent_name, host, missing)
            return False
        return True

    def _validate_input_params(agent_name):
        """Validate input parameters to prevent injection attacks."""
        import ipaddress
        import re
        
        # Validate IP address
        try:
            ipaddress.ip_address(fencing_data["ipaddr"])
        except ValueError:
            logging.error("Invalid IP address for %s: %s", agent_name, fencing_data["ipaddr"])
            return False
        
        # Validate port (if present)
        if "ipport" in fencing_data:
            try:
                port = int(fencing_data["ipport"])
                if not (1 <= port <= 65535):
                    raise ValueError
            except ValueError:
                logging.error("Invalid port for %s: %s", agent_name, fencing_data["ipport"])
                return False
        
        # Validate username (alphanumeric, underscore, dash, max 64 chars)
        if "login" in fencing_data:
            username = fencing_data["login"]
            if not re.match(r'^[a-zA-Z0-9_-]{1,64}$', username):
                logging.error("Invalid username for %s: %s", agent_name, username)
                return False
        
        return True

    try:
        if "noop" in agent:
            logging.warning("Using noop fencing agent for %s %s. VMs may get corrupted.", host, action)
            return True

        elif "ipmi" in agent:
            if not _validate_params(["ipaddr", "ipport", "login", "passwd"], "IPMI"):
                return False
            if not _validate_input_params("IPMI"):
                return False
            # Use environment variable to avoid password exposure in process list
            env = os.environ.copy()
            env['IPMITOOL_PASSWORD'] = fencing_data["passwd"]
            cmd = ["ipmitool", "-I", "lanplus", "-H", fencing_data["ipaddr"],
                   "-U", fencing_data["login"], "-E",  # Use environment variable
                   "-p", fencing_data["ipport"], "power", action]
            subprocess.run(cmd, timeout=30, env=env, capture_output=True, text=True, check=True)
            logging.info("IPMI %s successful for %s", action, host)
            return True

        elif "redfish" in agent:
            if not _validate_params(["ipaddr", "login", "passwd"], "Redfish"):
                return False
            if not _validate_input_params("Redfish"):
                return False
            ip = fencing_data["ipaddr"]
            port = fencing_data.get("ipport", "443")
            uuid = fencing_data.get("uuid", "System.Embedded.1")
            tls = fencing_data.get("tls", "false").lower() == "true"
            protocol = "https" if tls else "http"
            url = f'{protocol}://{ip}:{port}/redfish/v1/Systems/{uuid}'
            redfish_action = "ForceOff" if action == "off" else "On"
            return _redfish_reset(url, fencing_data["login"], fencing_data["passwd"],
                                fencing_data.get("timeout", 30), redfish_action)

        elif "bmh" in agent:
            if not _validate_params(["token", "host", "namespace"], "BMH"):
                return False
            return _bmh_fence(fencing_data["token"], fencing_data["namespace"],
                             fencing_data["host"], action)

        else:
            logging.error("Unknown fencing agent: %s", agent)
            return False

    except Exception as e:
        logging.error("%s fencing failed for %s: %s", agent.upper(), host, e)
        return False


def _host_fence(host, action, service):
    """Fence a host using the configured fencing agent."""
    if not host or action not in ['on', 'off']:
        logging.error("Invalid fence parameters: host=%s, action=%s", host, action)
        return False

    logging.info("Fencing host %s %s", host, action)

    try:
        # Look up fencing configuration
        short_hostname = host.split('.', 1)[0]
        matching_configs = [v for k, v in service.config.fencing.items() if k in short_hostname]

        if not matching_configs:
            logging.error("No fencing data found for %s", host)
            return False

        fencing_data = matching_configs[0]

        if not isinstance(fencing_data, dict) or "agent" not in fencing_data:
            logging.error("Invalid fencing data for %s", host)
            return False

        logging.debug("Using fencing agent '%s' for %s", fencing_data["agent"], host)
        return _execute_fence_operation(host, action, fencing_data)

    except Exception as e:
        logging.error("Fencing failed for %s: %s", host, e)
        return False



def _get_nova_connection(service):
    """Establish a connection to Nova using environment configuration."""
    try:
        cloud_name = os.getenv('OS_CLOUD', 'overcloud')

        if cloud_name not in service.config.clouds or cloud_name not in service.config.secure:
            logging.error("Configuration not found for cloud: %s", cloud_name)
            return None

        auth = service.config.clouds[cloud_name]["auth"]
        secure = service.config.secure[cloud_name]["auth"]

        conn = nova_login(auth["username"], secure["password"], auth["project_name"],
                         auth["auth_url"], auth["user_domain_name"], auth["project_domain_name"])

        if not conn:
            logging.error("Nova connection failed")
        return conn

    except Exception as e:
        logging.error("Failed to establish Nova connection: %s", e)
        return None


def _enable_matching_reserved_host(conn, failed_service, reserved_hosts, service, use_aggregates=True):
    """
    Enable a reserved host that matches the failed service criteria.

    Args:
        conn: Nova client connection
        failed_service: Failed service to match
        reserved_hosts: List of available reserved hosts
        service: Service instance for configuration access
        use_aggregates: True to match by aggregate, False to match by zone

    Returns:
        bool: True if a host was enabled, False otherwise
    """
    try:
        matching_hosts = []

        if use_aggregates:
            # Match by aggregate
            compute_aggregate_ids = _aggregate_ids(conn, failed_service)
            if not compute_aggregate_ids:
                return False

            for host in reserved_hosts:
                if (service.is_aggregate_evacuable(conn, host.host) and
                    set(_aggregate_ids(conn, host)).intersection(compute_aggregate_ids)):
                    matching_hosts.append(host)

            match_type = "aggregate"
        else:
            # Match by availability zone
            matching_hosts = [host for host in reserved_hosts
                            if host.zone == failed_service.zone]
            match_type = "AZ"

        if not matching_hosts:
            logging.warning("No reserved compute found in the same %s as %s",
                          match_type, failed_service.host)
            return False

        # Enable the first matching host
        selected_host = matching_hosts[0]
        reserved_hosts.remove(selected_host)

        if not _host_enable(conn, selected_host, reenable=False):
            logging.error("Failed to enable reserved host %s", selected_host.host)
            return False

        logging.info("Enabled host %s from the reserved pool (same %s as %s)",
                    selected_host.host, match_type, failed_service.host)
        return True

    except Exception as e:
        logging.error("Error enabling reserved host by %s: %s", match_type, e)
        return False


def _manage_reserved_hosts(conn, failed_service, reserved_hosts, service):
    """
    Manage reserved hosts by enabling a replacement host if available.

    Args:
        conn: Nova client connection
        service: Failed service that needs replacement capacity
        reserved_hosts: List of available reserved hosts

    Returns:
        bool: True if operation completed successfully (regardless of whether a host was enabled)
    """
    if not service.config.is_reserved_hosts_enabled():
        logging.debug("Reserved hosts feature is disabled")
        return True

    if not reserved_hosts:
        logging.warning("Not enough hosts available from the reserved pool")
        return True  # This is not a failure condition

    try:
        # Try to enable a matching host based on configuration
        use_aggregates = service.config.is_tagged_aggregates_enabled()
        if _enable_matching_reserved_host(conn, failed_service, reserved_hosts, service, use_aggregates):
            return True

        # If no specific match found, it's still not a failure
        logging.debug("No matching reserved host found for %s, continuing with evacuation", failed_service.host)
        return True

    except Exception as e:
        logging.error("Error managing reserved hosts for %s: %s", failed_service.host, e)
        return False


def _post_evacuation_recovery(conn, failed_service, service):
    """
    Perform post-evacuation recovery by powering on and re-enabling the host.

    Args:
        conn: Nova client connection
        service: Service to recover

    Returns:
        bool: True if recovery completed successfully, False otherwise
    """
    if service.config.is_leave_disabled_enabled():
        logging.info("Evacuation successful. Not re-enabling %s since LEAVE_DISABLED is enabled",
                    failed_service.host)
        return True

    logging.info("Evacuation successful. Starting recovery for %s", failed_service.host)

    try:
        # Step 1: Power on the host
        logging.debug("Powering on host %s", failed_service.host)
        power_on_result = _host_fence(failed_service.host, 'on', service)

        if not power_on_result:
            logging.error("Failed to power on %s during recovery", failed_service.host)
            return False

        # Step 2: Re-enable the host in Nova
        logging.debug("Re-enabling host %s in Nova", failed_service.host)
        enable_result = _host_enable(conn, failed_service, reenable=False)

        if not enable_result:
            logging.error("Failed to re-enable %s during recovery", failed_service.host)
            return False

        logging.info("Recovery completed successfully for %s", failed_service.host)
        return True

    except Exception as e:
        logging.error("Error during post-evacuation recovery for %s: %s", failed_service.host, e)
        logging.debug("Exception traceback:", exc_info=True)
        return False


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


def process_service(failed_service, reserved_hosts, resume, service):
    """Process a failed compute service through the complete recovery workflow."""
    if not failed_service or not hasattr(failed_service, 'host'):
        logging.error("Invalid service object provided")
        return False

    host_name = failed_service.host
    reserved_hosts = reserved_hosts or []

    logging.info("Processing service %s (resume=%s)", host_name, resume)

    # Get Nova connection first
    conn = _get_nova_connection(service)
    if not conn:
        logging.error("Nova connection failed for %s", host_name)
        return False

    # Execute processing steps
    if not resume:
        if not _execute_step("Fencing", _host_fence, host_name, host_name, 'off', service):
            return False
        if not _execute_step("Host disable", _host_disable, host_name, conn, failed_service):
            return False

    if not _execute_step("Reserved host management", _manage_reserved_hosts, host_name,
                        conn, failed_service, reserved_hosts, service):
        return False

    if not _execute_step("Evacuation", _host_evacuate, host_name,
                        conn, failed_service, service):
        return False

    if not _execute_step("Recovery", _post_evacuation_recovery, host_name,
                        conn, failed_service, service):
        return False

    logging.info("Service processing completed successfully for %s", host_name)
    return True


def _initialize_service():
    """Initialize InstanceHA service and supporting threads."""
    try:
        service = InstanceHAService(config_manager)
        metrics = Metrics()
        logging.info("InstanceHA service initialized successfully")
    except Exception as e:
        logging.error("Failed to initialize InstanceHA service: %s", e)
        sys.exit(1)

    # Start health check server
    health_check_thread = threading.Thread(target=service.start_health_check_server)
    health_check_thread.daemon = True
    health_check_thread.start()

    # Start kdump listener if enabled
    if service.config.is_kdump_check_enabled():
        kdump_thread = threading.Thread(target=_kdump_udp_listener, args=(service,))
        kdump_thread.daemon = True
        kdump_thread.start()

    return service, metrics

def _establish_nova_connection(service):
    """Establish Nova connection using service configuration."""
    cloud = os.getenv('OS_CLOUD', 'overcloud')
    
    try:
        auth = service.config.clouds[cloud]["auth"]
        secure = service.config.secure[cloud]["auth"]
        
        conn = nova_login(auth["username"], secure["password"], auth["project_name"],
                         auth["auth_url"], auth["user_domain_name"], auth["project_domain_name"])
        
        if conn is None:
            logging.error("Failed: Unable to connect to Nova - connection is None")
            sys.exit(1)
        return conn
        
    except KeyError as e:
        logging.error("Could not find valid data for Cloud: %s", cloud)
        sys.exit(1)
    except Exception as e:
        logging.error("Failed: Unable to connect to Nova: %s", e)
        sys.exit(1)

def _categorize_services(services, target_date):
    """Categorize services into compute nodes, resume candidates, and re-enable candidates."""
    compute_nodes, to_resume, to_reenable = [], [], []
    
    for svc in services:
        # Check for nodes to re-enable (forced_down but enabled)
        if 'enabled' in svc.status and svc.forced_down:
            to_reenable.append(svc)
        # Check for nodes needing evacuation
        elif ((datetime.fromisoformat(svc.updated_at) < target_date and svc.state != 'down') or
              svc.state == 'down') and 'disabled' not in svc.status and not svc.forced_down:
            compute_nodes.append(svc)
        # Check for nodes to resume evacuation
        elif (svc.forced_down and svc.state == 'down' and 'disabled' in svc.status and
              'instanceha evacuation' in svc.disabled_reason and 'evacuation FAILED' not in svc.disabled_reason):
            to_resume.append(svc)
    
    return compute_nodes, to_resume, to_reenable

def _process_stale_services(conn, service, services, compute_nodes, to_resume):
    """Process stale compute services for evacuation."""
    if not (compute_nodes or to_resume):
        return
        
    logging.warning('The following computes are down: %s', [svc.host for svc in compute_nodes])
    
    # Filter compute nodes with servers
    host_servers_cache = None
    if compute_nodes:
        host_servers_cache = service.get_hosts_with_servers_cached(conn, compute_nodes)
        compute_nodes = service.filter_hosts_with_servers(compute_nodes, host_servers_cache)
        if not compute_nodes:
            logging.debug("No compute nodes with servers to evacuate")
    
    # Get reserved hosts if enabled
    reserved_hosts = ([svc for svc in services if 'disabled' in svc.status and 'reserved' in svc.disabled_reason]
                     if service.config.is_reserved_hosts_enabled() and compute_nodes else [])
    
    # Refresh cache and get evacuable resources
    service.refresh_evacuable_cache(conn)
    images_enabled = service.config.is_tagged_images_enabled()
    flavors_enabled = service.config.is_tagged_flavors_enabled()
    images = service.get_evacuable_images(conn) if images_enabled else []
    flavors = service.get_evacuable_flavors(conn) if flavors_enabled else []
    
    # Filter evacuable servers if tagging is enabled
    if (images_enabled or flavors_enabled) and compute_nodes and host_servers_cache:
        compute_nodes = service.filter_hosts_with_evacuable_servers(compute_nodes, host_servers_cache, flavors, images)
        if not images and not flavors:
            logging.info("No tagged resources found - will evacuate all servers")
    
    # Apply aggregate filtering if enabled
    if service.config.is_tagged_aggregates_enabled() and compute_nodes:
        compute_nodes = _filter_by_aggregates(conn, service, compute_nodes, services)
    
    # Check evacuation threshold
    if services and (len(compute_nodes) / len(services) * 100) > service.config.get_threshold():
        logging.error('Number of impacted computes exceeds threshold. Not evacuating.')
        return
    
    # Process evacuations
    if not service.config.is_disabled():
        to_evacuate = _check_kdump(compute_nodes, service) if service.config.is_kdump_check_enabled() else compute_nodes
        
        with concurrent.futures.ThreadPoolExecutor() as executor:
            # Process new evacuations
            results = list(executor.map(lambda svc: process_service(svc, reserved_hosts, False, service), to_evacuate))
            if not all(results):
                logging.warning('Some services failed to evacuate. Retrying in %s seconds.', service.config.get_poll_interval())
            # Process resumed evacuations
            results = list(executor.map(lambda svc: process_service(svc, reserved_hosts, True, service), to_resume))
            if not all(results):
                logging.warning('Some services failed to evacuate. Retrying in %s seconds.', service.config.get_poll_interval())
    else:
        logging.info('InstanceHA DISABLED is true, not evacuating')

def _filter_by_aggregates(conn, service, compute_nodes, services):
    """Filter compute nodes by aggregate evacuability."""
    try:
        aggregates = conn.aggregates.list()
        evacuable_tag = service.config.get_evacuable_tag()
        evacuable_hosts = set()
        
        for agg in aggregates:
            is_evacuable = (evacuable_tag in agg.metadata and
                          str(agg.metadata[evacuable_tag]).lower() == 'true') or \
                         any(evacuable_tag in key and str(agg.metadata[key]).lower() == 'true'
                             for key in agg.metadata)
            if is_evacuable:
                evacuable_hosts.update(agg.hosts)
        
        compute_nodes_down = compute_nodes
        compute_nodes = [svc for svc in compute_nodes if svc.host in evacuable_hosts]
        
        down_not_tagged = [svc.host for svc in compute_nodes_down if svc not in compute_nodes]
        if down_not_tagged:
            logging.warning('Computes not part of evacuable aggregate: %s', down_not_tagged)
            
    except Exception as e:
        logging.warning("Failed to check aggregate evacuability: %s", e)
    
    return compute_nodes

def _process_reenabling(conn, service, to_reenable):
    """Process services that can be re-enabled."""
    if not to_reenable:
        return
        
    logging.debug('Checking %d computes for re-enabling', len(to_reenable))
    force_enable = service.config.is_force_enable_enabled()
    
    for svc in to_reenable:
        try:
            if force_enable:
                _host_enable(conn, svc, reenable=True)
                logging.info('Force re-enabled %s', svc.host)
            else:
                migrations = conn.migrations.list(source_compute=svc.host, migration_type='evacuation', limit=10)
                incomplete = [m for m in migrations if m.status not in ['completed', 'error', 'failed']]
                
                if not incomplete:
                    _host_enable(conn, svc, reenable=True)
                    logging.info('All migrations completed, re-enabled %s', svc.host)
                else:
                    logging.debug('Migration(s) incomplete for %s, not re-enabling', svc.host)
        except Exception as e:
            logging.error('Failed to enable %s: %s', svc.host, e)

def main():
    global current_hash, hash_update_successful
    
    # Initialize service and establish connections
    service, metrics = _initialize_service()
    conn = _establish_nova_connection(service)
    
    # Hash tracking for health checks
    previous_hash = ""
    last_hash_time = 0
    hash_interval = 60

    while True:
        current_timestamp = time.time()

        # Update hash less frequently for better performance
        if current_timestamp - last_hash_time > hash_interval:
            new_hash = hashlib.sha256(str(current_timestamp).encode()).hexdigest()
            if new_hash == previous_hash:
                logging.error("Hash has not changed. Something went wrong.")
                hash_update_successful = False
            else:
                current_hash = new_hash
                hash_update_successful = True
                previous_hash = current_hash
                last_hash_time = current_timestamp

        try:
            with metrics.timer('main_loop'):
                services = conn.services.list(binary="nova-compute")
                if not services:
                    continue

                target_date = datetime.now() - timedelta(seconds=service.config.get_delta())
                compute_nodes, to_resume, to_reenable = _categorize_services(services, target_date)

            logging.debug('List of stale services: %s', [svc.host for svc in compute_nodes])
            
            # Process stale services for evacuation
            _process_stale_services(conn, service, services, compute_nodes, to_resume)
            
            # Process services that can be re-enabled
            _process_reenabling(conn, service, to_reenable)

        except Exception as e:
            logging.warning("Failed to query compute status. Please check the Nova API availability.")
            metrics.increment('main_loop_errors')

        # Log performance metrics periodically
        if not hasattr(metrics, '_last_summary'):
            metrics._last_summary = 0
        if time.time() - metrics._last_summary > 3600:
            metrics.log_summary()
            metrics._last_summary = time.time()

        time.sleep(service.config.get_poll_interval())
    
    return service


if __name__ == "__main__":
    service_instance = None
    try:
        # Get service instance from main for cleanup
        service_instance = main()
    except KeyboardInterrupt:
        if service_instance:
            service_instance.kdump_listener_stop_event.set()
    except Exception as e:
        logging.error('Error: %s' % e)
        if service_instance:
            service_instance.kdump_listener_stop_event.set()
        raise
