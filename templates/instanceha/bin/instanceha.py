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

from novaclient import client
from novaclient.exceptions import Conflict, NotFound, Forbidden, Unauthorized

from keystoneauth1 import loading
from keystoneauth1 import session as ksc_session
from keystoneauth1.exceptions.discovery import DiscoveryFailure
from keystoneauth1.exceptions.http import Unauthorized


class ConfigurationError(Exception):
    """Raised when configuration loading or validation fails."""
    pass


class InstanceHAException(Exception):
    """Base exception for all InstanceHA-related errors."""
    pass


class EvacuationError(InstanceHAException):
    """Raised when evacuation operations fail."""
    pass


class FencingError(InstanceHAException):
    """Raised when fencing operations fail."""
    pass


class NovaConnectionError(InstanceHAException):
    """Raised when Nova API connection fails."""
    pass


class ServiceProcessingError(InstanceHAException):
    """Raised when service processing operations fail."""
    pass


class SSLConfigurationError(InstanceHAException):
    """Raised when SSL configuration is invalid."""
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
    
    def _load_config(self) -> Dict[str, Any]:
        """Load main configuration file with error handling."""
        try:
            if not os.path.exists(self.config_path):
                logging.warning("Configuration file not found at %s, using defaults", self.config_path)
                return {}
                
            with open(self.config_path, 'r') as stream:
                data = yaml.load(stream, Loader=SafeLoader)
                if not isinstance(data, dict):
                    raise ConfigurationError(f"Invalid configuration format in {self.config_path}")
                return data.get("config", {})
                
        except yaml.YAMLError as e:
            logging.error("Failed to parse configuration file %s: %s", self.config_path, e)
            raise ConfigurationError(f"Configuration parsing failed: {e}")
        except (IOError, OSError) as e:
            logging.error("Failed to read configuration file %s: %s", self.config_path, e)
            raise ConfigurationError(f"Configuration file access failed: {e}")
    
    def _load_clouds_config(self) -> Dict[str, Any]:
        """Load OpenStack clouds configuration."""
        try:
            with open(self.clouds_path, 'r') as stream:
                data = yaml.load(stream, Loader=SafeLoader)
                if not isinstance(data, dict) or "clouds" not in data:
                    raise ConfigurationError(f"Invalid clouds configuration in {self.clouds_path}")
                return data["clouds"]
        except (yaml.YAMLError, IOError, OSError) as e:
            logging.error("Failed to load clouds configuration from %s: %s", self.clouds_path, e)
            raise ConfigurationError(f"Clouds configuration loading failed: {e}")
    
    def _load_secure_config(self) -> Dict[str, Any]:
        """Load secure configuration (passwords, etc.)."""
        try:
            with open(self.secure_path, 'r') as stream:
                data = yaml.load(stream, Loader=SafeLoader)
                if not isinstance(data, dict) or "clouds" not in data:
                    raise ConfigurationError(f"Invalid secure configuration in {self.secure_path}")
                return data["clouds"]
        except (yaml.YAMLError, IOError, OSError) as e:
            logging.error("Failed to load secure configuration from %s: %s", self.secure_path, e)
            raise ConfigurationError(f"Secure configuration loading failed: {e}")
    
    def _load_fencing_config(self) -> Dict[str, Any]:
        """Load fencing configuration."""
        try:
            with open(self.fencing_path, 'r') as stream:
                data = yaml.load(stream, Loader=SafeLoader)
                if not isinstance(data, dict) or "FencingConfig" not in data:
                    raise ConfigurationError(f"Invalid fencing configuration in {self.fencing_path}")
                return data["FencingConfig"]
        except (yaml.YAMLError, IOError, OSError) as e:
            logging.error("Failed to load fencing configuration from %s: %s", self.fencing_path, e)
            raise ConfigurationError(f"Fencing configuration loading failed: {e}")
    
    def _validate_config(self) -> None:
        """Validate critical configuration values."""
        # Validate numeric ranges
        delta = self.get_int('DELTA', 30)
        if delta < 10 or delta > 300:
            raise ConfigurationError(f"DELTA must be between 10 and 300 seconds, got {delta}")
        
        poll = self.get_int('POLL', 45)
        if poll < 15 or poll > 600:
            raise ConfigurationError(f"POLL must be between 15 and 600 seconds, got {poll}")
        
        threshold = self.get_int('THRESHOLD', 50)
        if threshold < 0 or threshold > 100:
            raise ConfigurationError(f"THRESHOLD must be between 0 and 100 percent, got {threshold}")
        
        workers = self.get_int('WORKERS', 4)
        if workers < 1 or workers > 50:
            raise ConfigurationError(f"WORKERS must be between 1 and 50, got {workers}")
        
        # Validate log level
        log_level = self.get_str('LOGLEVEL', 'INFO').upper()
        valid_levels = ['DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL']
        if log_level not in valid_levels:
            raise ConfigurationError(f"LOGLEVEL must be one of {valid_levels}, got {log_level}")
        
        # Validate special conditions
        if self.get_bool('CHECK_KDUMP') and poll == 30:
            logging.warning('CHECK_KDUMP Enabled and POLL set to 30 seconds. This may result in unexpected failures. Please increase POLL to 45 or greater.')
    
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
    
    def get_evacuable_tag(self) -> str:
        """Get the evacuable tag for marking resources."""
        return self.get_str('EVACUABLE_TAG', 'evacuable')
    
    def get_delta(self) -> int:
        """Get the delta time for service staleness detection."""
        return self.get_int('DELTA', 30, min_val=10, max_val=300)
    
    def get_poll_interval(self) -> int:
        """Get the main loop polling interval."""
        return self.get_int('POLL', 45, min_val=15, max_val=600)
    
    def get_threshold(self) -> int:
        """Get the threshold percentage for evacuation safety."""
        return self.get_int('THRESHOLD', 50, min_val=0, max_val=100)
    
    def get_workers(self) -> int:
        """Get the number of worker threads."""
        return self.get_int('WORKERS', 4, min_val=1, max_val=50)
    
    def get_delay(self) -> int:
        """Get the delay before evacuation starts."""
        return self.get_int('DELAY', 0, min_val=0, max_val=300)
    
    def get_log_level(self) -> str:
        """Get the logging level."""
        return self.get_str('LOGLEVEL', 'INFO').upper()
    
    def is_smart_evacuation_enabled(self) -> bool:
        """Check if smart evacuation is enabled."""
        return self.get_bool('SMART_EVACUATION', False)
    
    def is_reserved_hosts_enabled(self) -> bool:
        """Check if reserved hosts feature is enabled."""
        return self.get_bool('RESERVED_HOSTS', False)
    
    def is_tagged_images_enabled(self) -> bool:
        """Check if tagged images filtering is enabled."""
        return self.get_bool('TAGGED_IMAGES', True)
    
    def is_tagged_flavors_enabled(self) -> bool:
        """Check if tagged flavors filtering is enabled."""
        return self.get_bool('TAGGED_FLAVORS', True)
    
    def is_tagged_aggregates_enabled(self) -> bool:
        """Check if tagged aggregates filtering is enabled."""
        return self.get_bool('TAGGED_AGGREGATES', True)
    
    def is_leave_disabled_enabled(self) -> bool:
        """Check if hosts should be left disabled after evacuation."""
        return self.get_bool('LEAVE_DISABLED', False)
    
    def is_force_enable_enabled(self) -> bool:
        """Check if force enable bypasses migration checks."""
        return self.get_bool('FORCE_ENABLE', False)
    
    def is_kdump_check_enabled(self) -> bool:
        """Check if kdump checking is enabled."""
        return self.get_bool('CHECK_KDUMP', False)
    
    def is_disabled(self) -> bool:
        """Check if the entire service is disabled."""
        return self.get_bool('DISABLED', False)
    
    def get_cloud_name(self) -> str:
        """Get the OpenStack cloud name."""
        return os.getenv('OS_CLOUD', 'overcloud')
    
    def get_udp_port(self) -> int:
        """Get the UDP port for health checks."""
        return int(os.getenv('UDP_PORT', 7410))
    
    def is_ssl_verification_enabled(self) -> bool:
        """Check if SSL certificate verification is enabled."""
        return self.get_bool('SSL_VERIFY', True)  # Default to secure
    
    def get_ssl_ca_bundle(self) -> Optional[str]:
        """Get path to SSL CA certificate bundle."""
        ca_bundle = self.get_str('SSL_CA_BUNDLE', '')
        if ca_bundle and os.path.exists(ca_bundle):
            return ca_bundle
        return None
    
    def get_ssl_cert_path(self) -> Optional[str]:
        """Get path to client SSL certificate."""
        cert_path = self.get_str('SSL_CERT_PATH', '')
        if cert_path and os.path.exists(cert_path):
            return cert_path
        return None
    
    def get_ssl_key_path(self) -> Optional[str]:
        """Get path to client SSL key."""
        key_path = self.get_str('SSL_KEY_PATH', '')
        if key_path and os.path.exists(key_path):
            return key_path
        return None
    
    def get_requests_ssl_config(self) -> Union[bool, str, tuple]:
        """
        Get SSL configuration for requests library.
        
        Returns:
            - True: Use default SSL verification
            - False: Disable SSL verification (insecure)
            - str: Path to CA bundle
            - tuple: (cert_path, key_path) for client certificates
        """
        if not self.is_ssl_verification_enabled():
            logging.warning("SSL verification is DISABLED - this is insecure for production use")
            return False
        
        ca_bundle = self.get_ssl_ca_bundle()
        cert_path = self.get_ssl_cert_path()
        key_path = self.get_ssl_key_path()
        
        # Client certificate authentication
        if cert_path and key_path:
            logging.debug("Using client certificate authentication")
            return (cert_path, key_path)
        
        # Custom CA bundle
        if ca_bundle:
            logging.debug("Using custom CA bundle: %s", ca_bundle)
            return ca_bundle
        
        # Default system SSL verification
        logging.debug("Using default system SSL verification")
        return True


class MetricsCollector:
    """
    Metrics collection and observability system for InstanceHA.
    
    This class provides comprehensive metrics collection for production monitoring,
    performance analysis, and operational insights.
    """
    
    def __init__(self):
        """Initialize metrics collector with counters and timers."""
        self.metrics = {
            # Operation counters
            'evacuations_total': 0,
            'evacuations_successful': 0,
            'evacuations_failed': 0,
            'hosts_processed_total': 0,
            'hosts_processed_successful': 0,
            'hosts_processed_failed': 0,
            'fencing_operations_total': 0,
            'fencing_operations_successful': 0,
            'fencing_operations_failed': 0,
            
            # Performance metrics
            'evacuation_duration_seconds_total': 0,
            'host_processing_duration_seconds_total': 0,
            'api_calls_total': 0,
            'api_calls_failed': 0,
            
            # Service state metrics
            'services_down_total': 0,
            'services_resumed_total': 0,
            'services_cached_total': 0,
            
            # Configuration metrics
            'config_reload_total': 0,
            'config_validation_errors': 0,
            
            # Health metrics
            'health_checks_total': 0,
            'health_checks_failed': 0,
        }
        
        # Timing data for percentile calculation
        self.timing_data = {
            'evacuation_durations': [],
            'host_processing_durations': [],
            'api_call_durations': [],
        }
        
        # Start time for uptime calculation
        self.start_time = time.time()
        
        logging.info("Metrics collector initialized")
    
    def increment_counter(self, metric_name: str, value: int = 1) -> None:
        """
        Increment a counter metric.
        
        Args:
            metric_name: Name of the metric to increment
            value: Value to increment by (default: 1)
        """
        if metric_name in self.metrics:
            self.metrics[metric_name] += value
            logging.debug("Incremented metric %s by %d (total: %d)", 
                         metric_name, value, self.metrics[metric_name])
        else:
            logging.warning("Unknown metric: %s", metric_name)
    
    def record_duration(self, metric_name: str, duration_seconds: float) -> None:
        """
        Record a duration metric.
        
        Args:
            metric_name: Name of the duration metric
            duration_seconds: Duration in seconds
        """
        base_metric = metric_name.replace('_duration_seconds', '_duration_seconds_total')
        if base_metric in self.metrics:
            self.metrics[base_metric] += duration_seconds
            
            # Store for percentile calculation (keep last 1000 values)
            timing_key = metric_name.replace('_seconds', 's')
            if timing_key in self.timing_data:
                self.timing_data[timing_key].append(duration_seconds)
                if len(self.timing_data[timing_key]) > 1000:
                    self.timing_data[timing_key].pop(0)
            
            logging.debug("Recorded duration %s: %.3f seconds", metric_name, duration_seconds)
    
    def get_metrics_summary(self) -> Dict[str, Any]:
        """
        Get comprehensive metrics summary for monitoring systems.
        
        Returns:
            Dictionary containing all metrics and calculated statistics
        """
        summary = self.metrics.copy()
        
        # Calculate uptime
        summary['uptime_seconds'] = time.time() - self.start_time
        
        # Calculate success rates
        if summary['evacuations_total'] > 0:
            summary['evacuation_success_rate'] = summary['evacuations_successful'] / summary['evacuations_total']
        else:
            summary['evacuation_success_rate'] = 0.0
        
        if summary['hosts_processed_total'] > 0:
            summary['host_processing_success_rate'] = summary['hosts_processed_successful'] / summary['hosts_processed_total']
        else:
            summary['host_processing_success_rate'] = 0.0
        
        if summary['api_calls_total'] > 0:
            summary['api_success_rate'] = (summary['api_calls_total'] - summary['api_calls_failed']) / summary['api_calls_total']
        else:
            summary['api_success_rate'] = 0.0
        
        # Calculate average durations
        if summary['evacuations_total'] > 0:
            summary['avg_evacuation_duration_seconds'] = summary['evacuation_duration_seconds_total'] / summary['evacuations_total']
        else:
            summary['avg_evacuation_duration_seconds'] = 0.0
        
        # Add percentile calculations for key metrics
        for metric_key, durations in self.timing_data.items():
            if durations:
                sorted_durations = sorted(durations)
                n = len(sorted_durations)
                summary[f'{metric_key}_p50'] = sorted_durations[int(n * 0.5)]
                summary[f'{metric_key}_p90'] = sorted_durations[int(n * 0.9)]
                summary[f'{metric_key}_p95'] = sorted_durations[int(n * 0.95)]
                summary[f'{metric_key}_p99'] = sorted_durations[int(n * 0.99)]
        
        return summary
    
    def log_metrics_summary(self) -> None:
        """Log a comprehensive metrics summary for operational monitoring."""
        summary = self.get_metrics_summary()
        
        logging.info("=== InstanceHA Metrics Summary ===")
        logging.info("Uptime: %.1f seconds", summary['uptime_seconds'])
        logging.info("Evacuations: %d total, %d successful (%.1f%% success rate)", 
                    summary['evacuations_total'], 
                    summary['evacuations_successful'],
                    summary['evacuation_success_rate'] * 100)
        logging.info("Hosts processed: %d total, %d successful (%.1f%% success rate)",
                    summary['hosts_processed_total'],
                    summary['hosts_processed_successful'], 
                    summary['host_processing_success_rate'] * 100)
        logging.info("API calls: %d total, %d failed (%.1f%% success rate)",
                    summary['api_calls_total'],
                    summary['api_calls_failed'],
                    summary['api_success_rate'] * 100)
        logging.info("Average evacuation duration: %.2f seconds", 
                    summary['avg_evacuation_duration_seconds'])
        
        # Log percentiles if available
        if summary.get('evacuation_durations_p95'):
            logging.info("Evacuation duration P95: %.2f seconds", summary['evacuation_durations_p95'])
        
        logging.info("=== End Metrics Summary ===")
    
    def export_prometheus_metrics(self) -> str:
        """
        Export metrics in Prometheus format for monitoring integration.
        
        Returns:
            String containing Prometheus-formatted metrics
        """
        summary = self.get_metrics_summary()
        prometheus_metrics = []
        
        # Counter metrics
        for metric_name, value in summary.items():
            if metric_name.endswith('_total') or metric_name.endswith('_count'):
                prometheus_metrics.append(f'instanceha_{metric_name} {value}')
            elif isinstance(value, (int, float)) and not metric_name.startswith('avg_'):
                prometheus_metrics.append(f'instanceha_{metric_name} {value}')
        
        return '\n'.join(prometheus_metrics)


class PerformanceProfiler:
    """
    Performance profiling and timing utilities for InstanceHA operations.
    
    This class provides context managers and decorators for performance monitoring.
    """
    
    def __init__(self, metrics_collector: MetricsCollector):
        """Initialize profiler with metrics collector."""
        self.metrics = metrics_collector
        self.active_timers = {}
    
    def timer(self, operation_name: str):
        """
        Context manager for timing operations.
        
        Args:
            operation_name: Name of the operation being timed
            
        Usage:
            with profiler.timer('evacuation'):
                # perform evacuation
                pass
        """
        return TimingContext(self.metrics, operation_name)
    
    def profile_function(self, operation_name: str):
        """
        Decorator for profiling function execution time.
        
        Args:
            operation_name: Name of the operation for metrics
            
        Usage:
            @profiler.profile_function('host_disable')
            def disable_host(host):
                pass
        """
        def decorator(func):
            def wrapper(*args, **kwargs):
                with self.timer(operation_name):
                    return func(*args, **kwargs)
            return wrapper
        return decorator


class TimingContext:
    """Context manager for timing operations and recording metrics."""
    
    def __init__(self, metrics_collector: MetricsCollector, operation_name: str):
        """Initialize timing context."""
        self.metrics = metrics_collector
        self.operation_name = operation_name
        self.start_time = None
    
    def __enter__(self):
        """Start timing."""
        self.start_time = time.time()
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        """Stop timing and record metrics."""
        if self.start_time:
            duration = time.time() - self.start_time
            metric_name = f'{self.operation_name}_duration_seconds'
            self.metrics.record_duration(metric_name, duration)
            
            # Also increment the operation counter
            self.metrics.increment_counter(f'{self.operation_name}_total')
            
            # Track success/failure based on exception
            if exc_type is None:
                self.metrics.increment_counter(f'{self.operation_name}_successful')
            else:
                self.metrics.increment_counter(f'{self.operation_name}_failed')


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
        
        # Constants
        self.UDP_IP = ''
        
        # Threading
        self.health_check_thread = None
        
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

        # Server doesn't match evacuable criteria
        logging.warning("Instance %s is not evacuable: not using either of the defined flavors or images tagged with the %s attribute", server.id, self.config.get_evacuable_tag())
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
                        if hasattr(image, 'tags') and self._check_tag_in_tags(image.tags, evacuable_tag):
                            self._evacuable_images_cache.append(image.id)
                        # Check for metadata/properties (fallback) - these might have composite keys
                        elif hasattr(image, 'metadata') and self._check_tag_in_dict(image.metadata, evacuable_tag):
                            self._evacuable_images_cache.append(image.id)
                        elif hasattr(image, 'properties') and self._check_tag_in_dict(image.properties, evacuable_tag):
                            self._evacuable_images_cache.append(image.id)
                    
                    logging.debug("Cached %d evacuable images from %d total images", 
                                 len(self._evacuable_images_cache), len(images))
                else:
                    # No images retrieved, fall back to per-server checking
                    logging.warning("Could not retrieve images for caching. Image evacuation will use per-server checking.")
                    self._evacuable_images_cache = []
                    
            except Exception as e:
                logging.error("Failed to get evacuable images: %s", e)
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
    
    def _check_tag_in_dict(self, data_dict, evacuable_tag):
        """
        Check if evacuable tag exists in a dictionary (exact match or as part of a composite key).
        
        Args:
            data_dict: Dictionary to search in
            evacuable_tag: Tag to search for
            
        Returns:
            bool: True if tag is found with value 'true', False otherwise
        """
        try:
            # Check for exact key match
            if evacuable_tag in data_dict:
                return str(data_dict[evacuable_tag]).lower() == 'true'
            
            # Check for composite keys (e.g., "trait:CUSTOM_HA")
            for key in data_dict:
                if evacuable_tag in key:
                    return str(data_dict[key]).lower() == 'true'
            
            return False
        except (AttributeError, TypeError):
            return False
    
    def _check_tag_in_tags(self, tags, evacuable_tag):
        """
        Check if evacuable tag exists in image tags list (exact match or as part of a tag string).
        
        Args:
            tags: List of image tags (strings)
            evacuable_tag: Tag to search for
            
        Returns:
            bool: True if tag is found, False otherwise
        """
        try:
            # Check for exact tag match
            if evacuable_tag in tags:
                return True
            
            # Check for composite tags (e.g., "trait:CUSTOM_HA")
            for tag in tags:
                if evacuable_tag in tag:
                    return True
            
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
            
        try:
            server_image_id = self._get_server_image_id(server)
            if not server_image_id:
                logging.debug("Server %s has no image ID", server.id)
                return False
                
            evacuable_tag = self.config.get_evacuable_tag()
            
            # Try to get image details through Nova
            try:
                # Some Nova clients can get image info
                image = connection.glance.get(server_image_id)
                if hasattr(image, 'tags') and self._check_tag_in_tags(image.tags, evacuable_tag):
                    return True
            except:
                # If direct Glance access fails, try Nova's image interface
                try:
                    image = connection.images.get(server_image_id)
                    if hasattr(image, 'metadata'):
                        # Check if image has evacuable tag in metadata
                        metadata = getattr(image, 'metadata', {})
                        if self._check_tag_in_dict(metadata, evacuable_tag):
                            return True
                    if hasattr(image, 'properties'):
                        # Check if image has evacuable tag in properties
                        properties = getattr(image, 'properties', {})
                        if self._check_tag_in_dict(properties, evacuable_tag):
                            return True
                except:
                    # If all else fails, we can't determine image evacuability
                    logging.debug("Could not determine evacuability of image %s for server %s", 
                                 server_image_id, server.id)
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
            evacuable_aggregates = []
            
            for aggregate in aggregates:
                # Check if evacuable_tag is an exact key match or part of a key
                matching_key = None
                if evacuable_tag in aggregate.metadata:
                    matching_key = evacuable_tag
                else:
                    # Look for the tag in composite keys
                    for key in aggregate.metadata:
                        if evacuable_tag in key:
                            matching_key = key
                            break
                
                if matching_key:
                    value = aggregate.metadata[matching_key]
                    if str(value).lower() == 'true':
                        evacuable_aggregates.append(aggregate)
            
            result = any(host in i.hosts for i in evacuable_aggregates)
            return result
        except Exception as e:
            logging.error("Failed to check aggregate evacuability for %s: %s", host, e)
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
        """Internal method to run the health check server."""
        logging.debug("Starting health check server on port 8080...")
        server_address = ('', 8080)
        
        # Create a custom handler that has access to this service instance
        class InstanceHAHealthCheckHandler(server.BaseHTTPRequestHandler):
            def __init__(self, service_instance):
                self.service = service_instance
                
            def __call__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                
            def do_GET(self):
                logging.debug("Health check request received.")
                
                if self.service.hash_update_successful:
                    self.send_response(200)
                    self.send_header("Content-type", "text/plain")
                    self.end_headers()
                    self.wfile.write(self.service.current_hash.encode('utf-8'))
                else:
                    self.send_response(500)
                    self.send_header("Content-type", "text/plain")
                    self.end_headers()
                    self.wfile.write(b"Error: Hash not updated properly.")
        
        handler = lambda *args, **kwargs: InstanceHAHealthCheckHandler(self)(*args, **kwargs)
        httpd = server.HTTPServer(server_address, handler)
        httpd.serve_forever()
    
    def clear_cache(self):
        """Clear all cached data to force refresh."""
        self._host_servers_cache.clear()
        self._evacuable_flavors_cache = None
        self._evacuable_images_cache = None
        self._cache_timestamp = 0
        logging.debug("Service cache cleared")
    
    def refresh_evacuable_cache(self, connection=None):
        """Force refresh of evacuable flavors and images cache."""
        logging.info("Refreshing evacuable flavors and images cache")
        self._evacuable_flavors_cache = None
        self._evacuable_images_cache = None
        
        # Force re-cache
        evac_flavors = self.get_evacuable_flavors(connection)
        evac_images = self.get_evacuable_images(connection)
        
        logging.info("Cache refreshed: %d flavors, %d images", 
                    len(evac_flavors), len(evac_images))


# Initialize global configuration manager
try:
    config_manager = ConfigManager()
    logging.basicConfig(format='%(asctime)s %(levelname)s %(message)s', level=config_manager.get_log_level())
    logging.info("Configuration loaded successfully")
except ConfigurationError as e:
    logging.error("Configuration failed: %s", e)
    sys.exit(1)

# Initialize global service instance
try:
    instanceha_service = InstanceHAService(config_manager)
    logging.info("InstanceHA service initialized successfully")
except Exception as e:
    logging.error("Failed to initialize InstanceHA service: %s", e)
    sys.exit(1)

# Legacy function wrappers for backward compatibility
# These functions delegate to the service instance methods

def _is_server_evacuable(server, evac_flavors=None, evac_images=None):
    """Legacy wrapper for server evacuability check."""
    return instanceha_service.is_server_evacuable(server, evac_flavors, evac_images)

def _get_evacuable_flavors(connection):
    """Legacy wrapper for getting evacuable flavors."""
    return instanceha_service.get_evacuable_flavors(connection)

def _get_evacuable_images(connection):
    """Legacy wrapper for getting evacuable images."""
    return instanceha_service.get_evacuable_images(connection)

def _is_aggregate_evacuable(connection, host):
    """Legacy wrapper for checking if host is in evacuable aggregate."""
    return instanceha_service.is_aggregate_evacuable(connection, host)

def _aggregate_ids(conn, service):

    aggregates = conn.aggregates.list()
    return [ag.id for ag in aggregates if service.host in ag.hosts]


def _custom_check():
    logging.info("Ran _custom_check()")
    return True


def _host_evacuate(connection, service):
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
    host = service.host
    result = True
    
    # Get evacuable images and flavors using service instance
    images = instanceha_service.get_evacuable_images(connection)
    flavors = instanceha_service.get_evacuable_flavors(connection)
    
    # Get all servers on the failed host
    servers = connection.servers.list(search_opts={'host': host, 'all_tenants': 1})
    servers = [server for server in servers if server.status in {'ACTIVE', 'ERROR', 'STOPPED'}]

    if flavors or images:
        logging.debug("Filtering images and flavors: %s %s", repr(flavors), repr(images))
        # Identify all evacuable servers
        logging.debug("Checking %s", repr(servers))
        evacuables = [server for server in servers if _is_server_evacuable(server, flavors, images)]
        logging.debug("Evacuating %s", repr(evacuables))
    else:
        logging.debug("Evacuating all images and flavors")
        evacuables = servers

    if evacuables == []:
        logging.info("Nothing to evacuate")
        return True

    # Sleep for configured delay
    time.sleep(instanceha_service.config.get_delay())

    # Use smart evacuation if enabled
    if instanceha_service.config.is_smart_evacuation_enabled():
        logging.debug("Using smart evacuation with %d workers", instanceha_service.config.get_workers())
        
        # Use ThreadPoolExecutor to poll the evacuation status
        with concurrent.futures.ThreadPoolExecutor(max_workers=instanceha_service.config.get_workers()) as executor:
            future_to_server = {executor.submit(_server_evacuate_future, connection, server): server for server in evacuables}

            for future in concurrent.futures.as_completed(future_to_server):
                server = future_to_server[future]
                try:
                    data = future.result()
                    if data is False:
                        logging.debug('Evacuation of %s failed 5 times in a row', server.id)
                        # Update DISABLED reason so we don't try to evacuate again
                        try:
                            connection.services.disable_log_reason(service.id, "evacuation FAILED: %s" % datetime.now().isoformat())
                            logging.info('Evacuation failed. Updated disabled reason for host %s', host)
                        except Exception as e:
                            logging.error('Failed to update disable_reason for host %s. Error: %s', host, e)
                            logging.debug('Exception traceback:', exc_info=True)
                        return data
                except Exception as exc:
                    logging.error('Evacuation generated an exception: %s', exc)
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
    """
    Check the status of a server evacuation by querying recent migrations.
    
    This function queries Nova for recent evacuation migrations for the specified
    server instance and determines if the evacuation is completed, in progress, or failed.
    
    Args:
        connection: Nova client connection
        server: Server ID or UUID to check evacuation status for
        
    Returns:
        dict: Dictionary with keys:
            - "completed": bool - True if evacuation completed successfully
            - "error": bool - True if evacuation failed or error occurred
    """
    # Constants
    MIGRATION_QUERY_WINDOW_MINUTES = 5  # Query migrations from last 5 minutes
    MAX_MIGRATION_LIMIT = 1000  # Reasonable limit instead of 100000
    
    # Valid evacuation status values
    COMPLETED_STATUSES = ['completed', 'done']
    IN_PROGRESS_STATUSES = ['migrating', 'running', 'pre-migrating']
    ERROR_STATUSES = ['error', 'failed', 'cancelled']
    
    # Initialize return values
    completed = False
    error_msg = False
    
    # Input validation
    if not connection:
        logging.error("Cannot check evacuation status - no Nova connection provided")
        return {"completed": completed, "error": True}
    
    if not server:
        logging.error("Cannot check evacuation status - no server ID provided")
        return {"completed": completed, "error": True}
    
    # Convert server to string to ensure consistency
    server_id = str(server)
    
    logging.debug("Checking evacuation status for instance: %s", server_id)
    
    try:
        # Calculate time window for migration query
        # Note: We query the last 5 minutes to find recent evacuation migrations
        query_start_time = (datetime.now() - timedelta(minutes=MIGRATION_QUERY_WINDOW_MINUTES)).isoformat()
        
        logging.debug("Querying migrations for instance %s since %s", server_id, query_start_time)
        
        # Query Nova for recent evacuation migrations
        try:
            migrations = connection.migrations.list(
                instance_uuid=server_id,
                migration_type='evacuation',
                changes_since=query_start_time,
                limit=str(MAX_MIGRATION_LIMIT)
            )
            
        except Exception as e:
            logging.error("Failed to query migrations for instance %s: %s", server_id, e)
            return {"completed": completed, "error": True}
        
        # Check if any migrations were found
        if not migrations:
            logging.warning("No evacuation migrations found for instance %s in the last %d minutes", 
                           server_id, MIGRATION_QUERY_WINDOW_MINUTES)
            return {"completed": completed, "error": True}
        
        # Get the most recent migration (Nova API v2.59 lists them in reverse time order)
        try:
            last_migration = migrations[0]
        except (IndexError, TypeError) as e:
            logging.error("Error accessing migration data for instance %s: %s", server_id, e)
            return {"completed": completed, "error": True}
        
        # Extract migration status
        try:
            if hasattr(last_migration, '_info') and 'status' in last_migration._info:
                migration_status = last_migration._info['status']
            elif hasattr(last_migration, 'status'):
                migration_status = last_migration.status
            else:
                logging.error("Migration object for instance %s missing status information", server_id)
                return {"completed": completed, "error": True}
                
        except (AttributeError, KeyError, TypeError) as e:
            logging.error("Failed to extract evacuation status for instance %s: %s", server_id, e)
            return {"completed": completed, "error": True}
        
        # Log the current migration status
        logging.debug("Instance %s evacuation status: %s", server_id, migration_status)
        
        # Determine evacuation state based on status
        if migration_status in COMPLETED_STATUSES:
            logging.debug("Instance %s evacuation completed successfully", server_id)
            completed = True
            error_msg = False
            
        elif migration_status in IN_PROGRESS_STATUSES:
            logging.debug("Instance %s evacuation in progress", server_id)
            completed = False
            error_msg = False
            
        elif migration_status in ERROR_STATUSES:
            logging.warning("Instance %s evacuation failed with status: %s", server_id, migration_status)
            completed = False
            error_msg = True
            
        else:
            # Unknown status - treat as error to be safe
            logging.warning("Instance %s has unknown evacuation status: %s", server_id, migration_status)
            completed = False
            error_msg = True
        
        # Add migration ID to debug logs for tracking
        try:
            migration_id = getattr(last_migration, 'id', 'unknown')
            logging.debug("Instance %s migration ID: %s, status: %s", 
                         server_id, migration_id, migration_status)
        except Exception:
            # Don't fail if we can't get migration ID
            pass
            
    except Exception as e:
        logging.error("Unexpected error checking evacuation status for instance %s: %s", server_id, e)
        logging.debug("Exception traceback:", exc_info=True)
        error_msg = True
    
    result = {
        "completed": completed,
        "error": error_msg,
    }
    
    logging.debug("Evacuation status result for instance %s: %s", server_id, result)
    return result


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
                
                if error_count >= MAX_RETRIES:
                    logging.error("Too many errors checking evacuation status for %s. Giving up.",
                                 response["uuid"])
                    return False
                
                time.sleep(RETRY_WAIT_TIME)
                continue
                
    except Exception as e:
        logging.error("Unexpected error during evacuation of server %s: %s",
                     server.id, str(e))
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


def _check_kdump(stale_services):
    """
    Check for kdump messages from crashed compute nodes to identify which nodes are currently kdumping.
    
    This function listens for fence_kdump UDP messages to determine if compute nodes are in the process
    of creating kernel dumps. Nodes that are kdumping should not be evacuated immediately.
    
    Args:
        stale_services: List of stale compute services to check
        
    Returns:
        list: Filtered list of stale services excluding those that are kdumping
    """
    # Constants for kdump checking
    FENCE_KDUMP_MAGIC = "0x1B302A40"
    KDUMP_TIMEOUT = 30  # seconds
    UDP_RECV_BUFFER_SIZE = 65535
    UDP_ANCDATA_SIZE = 1024
    STRUCT_INT_SIZE = 4  # Size of integer in struct
    
    # Input validation
    if not stale_services:
        logging.debug("No stale services provided for kdump checking")
        return []
    
    # Extract hostnames from stale services
    broken_computes = {service.host for service in stale_services}
    if not broken_computes:
        logging.debug("No compute hosts to check for kdump")
        return stale_services
    
    logging.info("Checking for kdump messages from %d compute(s): %s", 
                 len(broken_computes), list(broken_computes))
    
    # Set to track hosts that are kdumping
    kdumping_hosts = set()
    
    # Create and configure socket
    sock = None
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(KDUMP_TIMEOUT)
        
        try:
            udp_ip = instanceha_service.UDP_IP
            udp_port = instanceha_service.config.get_udp_port()
            sock.bind((udp_ip, udp_port))
            logging.debug("Successfully bound to %s:%s for kdump messages", udp_ip, udp_port)
        except (OSError, socket.error) as e:
            udp_ip = instanceha_service.UDP_IP
            udp_port = instanceha_service.config.get_udp_port()
            logging.error("Could not bind to %s:%s for kdump checking: %s", udp_ip, udp_port, e)
            return stale_services  # Return original list if we can't check kdump
        
        # Listen for kdump messages
        timeout_end = time.time() + KDUMP_TIMEOUT
        
        while time.time() < timeout_end:
            try:
                data, ancdata, msg_flags, address = sock.recvmsg(UDP_RECV_BUFFER_SIZE, UDP_ANCDATA_SIZE, 0)
                
                # Validate we have enough data for struct unpacking
                if len(data) < STRUCT_INT_SIZE * 2:
                    logging.debug("Received insufficient data for kdump message: %d bytes", len(data))
                    continue
                
                # Extract and validate magic number
                try:
                    magic_number = struct.unpack('ii', data[:STRUCT_INT_SIZE * 2])[0]
                    magic_hex = hex(magic_number).upper()
                    
                    if magic_hex != FENCE_KDUMP_MAGIC.upper():
                        logging.debug("Invalid magic number received: %s (expected %s)", 
                                     magic_hex, FENCE_KDUMP_MAGIC)
                        continue
                        
                except struct.error as e:
                    logging.debug("Failed to unpack kdump message data: %s", e)
                    continue
                
                # Resolve hostname from source IP
                if not address or len(address) < 1:
                    logging.debug("Invalid address in kdump message")
                    continue
                    
                source_ip = address[0]
                try:
                    full_hostname = socket.gethostbyaddr(source_ip)[0]
                    short_hostname = full_hostname.split('.', 1)[0]
                except (socket.herror, socket.gaierror, OSError) as e:
                    logging.debug("Failed reverse DNS lookup for %s: %s", source_ip, e)
                    continue
                
                # Check if this hostname matches any of our broken computes
                matching_hosts = [host for host in broken_computes if short_hostname in host]
                if matching_hosts:
                    kdumping_host = matching_hosts[0]  # Take the first match
                    kdumping_hosts.add(kdumping_host)
                    logging.debug("Kdump message received from known host: %s (IP: %s)", 
                                 kdumping_host, source_ip)
                else:
                    logging.debug("Kdump message received from unknown host: %s (IP: %s)", 
                                 short_hostname, source_ip)
                
            except socket.timeout:
                # This is expected when no more messages arrive
                logging.debug("Socket timeout reached while waiting for kdump messages")
                break
                
            except (OSError, socket.error) as e:
                logging.warning("Error receiving kdump message: %s", e)
                break
                
    except Exception as e:
        logging.error("Unexpected error during kdump checking: %s", e)
        return stale_services  # Return original list on unexpected errors
        
    finally:
        # Ensure socket is always closed
        if sock:
            try:
                sock.close()
            except Exception as e:
                logging.debug("Error closing kdump socket: %s", e)
    
    # Calculate which hosts are not kdumping
    non_kdumping_hosts = broken_computes - kdumping_hosts
    
    # Log results
    if kdumping_hosts:
        logging.info("The following compute(s) are kdumping: %s", sorted(kdumping_hosts))
    else:
        logging.info("No compute nodes are currently kdumping")
    
    if non_kdumping_hosts:
        logging.info("The following compute(s) are not kdumping: %s", sorted(non_kdumping_hosts))
    else:
        logging.info("All broken compute nodes are currently kdumping")
    
    # Filter stale services to only include those not kdumping
    if non_kdumping_hosts:
        filtered_services = [service for service in stale_services 
                           if service.host in non_kdumping_hosts]
    else:
        filtered_services = []
    
    logging.info("Filtered %d services down to %d services after kdump check", 
                 len(stale_services), len(filtered_services))
    
    return filtered_services


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
    """
    Perform a Redfish reset operation on a computer system.
    
    This function sends a reset command to a Redfish-compliant system using the 
    ComputerSystem.Reset action. It performs comprehensive input validation,
    error handling, and proper HTTP status checking.
    
    Args:
        url (str): Base Redfish URL for the computer system
        user (str): Username for authentication
        passwd (str): Password for authentication
        timeout (int): Request timeout in seconds
        action (str): Reset action to perform (e.g., 'On', 'ForceOff', 'GracefulRestart')
        
    Returns:
        bool: True if reset operation was successful, False otherwise
    """
    # Constants
    VALID_RESET_ACTIONS = [
        'On', 'ForceOff', 'GracefulShutdown', 'GracefulRestart',
        'ForceRestart', 'Nmi', 'ForceOn', 'PushPowerButton'
    ]
    MIN_TIMEOUT = 5  # Minimum reasonable timeout
    MAX_TIMEOUT = 300  # Maximum reasonable timeout
    
    # Input validation
    if not url:
        logging.error("Redfish reset failed: no URL provided")
        return False
    
    if not isinstance(url, str):
        logging.error("Redfish reset failed: URL must be a string, got %s", type(url).__name__)
        return False
    
    if not user:
        logging.error("Redfish reset failed: no username provided")
        return False
    
    if not isinstance(user, str):
        logging.error("Redfish reset failed: username must be a string, got %s", type(user).__name__)
        return False
    
    if not passwd:
        logging.error("Redfish reset failed: no password provided")
        return False
    
    if not isinstance(passwd, str):
        logging.error("Redfish reset failed: password must be a string, got %s", type(passwd).__name__)
        return False
    
    if not action:
        logging.error("Redfish reset failed: no action provided")
        return False
    
    if not isinstance(action, str):
        logging.error("Redfish reset failed: action must be a string, got %s", type(action).__name__)
        return False
    
    if action not in VALID_RESET_ACTIONS:
        logging.error("Redfish reset failed: invalid action '%s'. Valid actions: %s", 
                     action, ', '.join(VALID_RESET_ACTIONS))
        return False
    
    # Validate timeout
    if timeout is None:
        logging.error("Redfish reset failed: no timeout provided")
        return False
    
    try:
        timeout_int = int(timeout)
        if timeout_int < MIN_TIMEOUT or timeout_int > MAX_TIMEOUT:
            logging.error("Redfish reset failed: timeout %d must be between %d and %d seconds", 
                         timeout_int, MIN_TIMEOUT, MAX_TIMEOUT)
            return False
    except (ValueError, TypeError):
        logging.error("Redfish reset failed: timeout must be an integer, got %s", type(timeout).__name__)
        return False
    
    # Validate and normalize URL
    url_str = str(url).strip()
    if not url_str.startswith(('http://', 'https://')):
        logging.error("Redfish reset failed: URL must start with http:// or https://")
        return False
    
    # Remove trailing slash to ensure consistent URL construction
    if url_str.endswith('/'):
        url_str = url_str[:-1]
    
    # Construct the reset action URL
    reset_url = f"{url_str}/Actions/ComputerSystem.Reset"
    
    # Prepare request data
    payload = {"ResetType": action}
    headers = {
        'Content-Type': 'application/json',
        'Accept': 'application/json'
    }
    
    logging.info("Redfish reset: attempting %s action on %s", action, url_str)
    logging.debug("Redfish reset URL: %s", reset_url)
    
    try:
        # Get SSL configuration from ConfigManager
        ssl_config = config_manager.get_requests_ssl_config()
        
        # Make the HTTP request with secure SSL configuration
        response = requests.post(
            reset_url,
            data=json.dumps(payload),
            headers=headers,
            auth=(user, passwd),
            verify=ssl_config,
            timeout=timeout_int
        )
        
        # Check for HTTP errors
        if response.status_code == 200:
            logging.info("Redfish reset successful: %s action completed on %s", action, url_str)
            return True
        elif response.status_code == 204:
            # 204 No Content is also a successful response for some Redfish implementations
            logging.info("Redfish reset successful: %s action completed on %s (no content)", action, url_str)
            return True
        elif response.status_code == 401:
            logging.error("Redfish reset failed: authentication failed for %s (check credentials)", url_str)
            return False
        elif response.status_code == 403:
            logging.error("Redfish reset failed: insufficient permissions for %s", url_str)
            return False
        elif response.status_code == 404:
            logging.error("Redfish reset failed: reset endpoint not found on %s", url_str)
            return False
        elif response.status_code == 405:
            logging.error("Redfish reset failed: reset action not supported by %s", url_str)
            return False
        elif response.status_code == 409:
            logging.error("Redfish reset failed: conflicting state on %s (system may already be in target state)", url_str)
            return False
        elif response.status_code >= 500:
            logging.error("Redfish reset failed: server error %d on %s", response.status_code, url_str)
            return False
        else:
            logging.error("Redfish reset failed: unexpected status code %d on %s", response.status_code, url_str)
            return False
            
    except requests.exceptions.Timeout:
        logging.error("Redfish reset failed: request timeout after %d seconds for %s", timeout_int, url_str)
        return False
    except requests.exceptions.ConnectionError as e:
        logging.error("Redfish reset failed: connection error for %s: %s", url_str, e)
        return False
    except requests.exceptions.HTTPError as e:
        logging.error("Redfish reset failed: HTTP error for %s: %s", url_str, e)
        return False
    except requests.exceptions.RequestException as e:
        logging.error("Redfish reset failed: request error for %s: %s", url_str, e)
        return False
    except json.JSONEncodeError as e:
        logging.error("Redfish reset failed: JSON encoding error for %s: %s", url_str, e)
        return False
    except Exception as e:
        logging.error("Redfish reset failed: unexpected error for %s: %s", url_str, e)
        logging.debug("Exception traceback:", exc_info=True)
        return False

def _bmh_fence(token, namespace, host, action):
    """
    Fence a BareMetal Host using Kubernetes API.
    
    This function sends a reboot annotation to a BareMetal Host resource in Kubernetes
    to power it on or off, and optionally waits for confirmation.
    
    Args:
        token: Kubernetes service account token
        namespace: Kubernetes namespace containing the BMH resource
        host: BareMetal Host name
        action: 'on' or 'off'
        
    Returns:
        bool: True if operation was successful, False otherwise
    """
    # Constants
    POWER_OFF_TIMEOUT = 30  # seconds to wait for power off confirmation
    POLL_INTERVAL = 3  # seconds between status checks
    REQUEST_TIMEOUT = 10  # seconds for individual HTTP requests
    
    # Input validation
    if not token:
        logging.error("BMH fencing failed: no token provided")
        return False
        
    if not namespace:
        logging.error("BMH fencing failed: no namespace provided") 
        return False
        
    if not host:
        logging.error("BMH fencing failed: no host provided")
        return False
        
    if action not in ['on', 'off']:
        logging.error("BMH fencing failed: invalid action '%s' (must be 'on' or 'off')", action)
        return False
    
    # Build URLs and headers
    patch_url = (f"https://kubernetes.default.svc/apis/metal3.io/v1alpha1/"
                f"namespaces/{namespace}/baremetalhosts/{host}?fieldManager=kubectl-patch")
    
    get_url = (f"https://kubernetes.default.svc/apis/metal3.io/v1alpha1/"
              f"namespaces/{namespace}/baremetalhosts/{host}")
    
    headers = {
        'Authorization': f'Bearer {token}',
        'Content-Type': 'application/merge-patch+json'
    }
    
    cacert = '/var/run/secrets/kubernetes.io/serviceaccount/ca.crt'
    
    # Validate cert file exists
    if not os.path.exists(cacert):
        logging.error("BMH fencing failed: CA certificate not found at %s", cacert)
        return False
    
    logging.info("BMH fencing: attempting to power %s host %s in namespace %s", action, host, namespace)
    
    try:
        if action == 'off':
            # Power off: set reboot annotation
            annotation_data = {
                "metadata": {
                    "annotations": {
                        "reboot.metal3.io/iha": '{"mode": "hard"}'
                    }
                }
            }
            
            try:
                response = requests.patch(
                    patch_url, 
                    headers=headers, 
                    verify=cacert, 
                    data=json.dumps(annotation_data),
                    timeout=REQUEST_TIMEOUT
                )
                response.raise_for_status()  # Raises HTTPError for bad status codes
                
            except requests.exceptions.Timeout:
                logging.error("BMH power off failed: request timeout for %s", host)
                return False
            except requests.exceptions.HTTPError as e:
                logging.error("BMH power off failed: HTTP error %d for %s: %s", 
                             response.status_code, host, e)
                return False
            except requests.exceptions.RequestException as e:
                logging.error("BMH power off failed: request error for %s: %s", host, e)
                return False
            
            if response.status_code != 200:
                logging.error("BMH power off failed: unexpected status code %d for %s", 
                             response.status_code, host)
                return False
            
            logging.debug("BMH power off annotation set successfully for %s", host)
            
            # Wait for power off confirmation
            return _bmh_wait_for_power_off(get_url, headers, cacert, host, POWER_OFF_TIMEOUT, POLL_INTERVAL)
            
        else:  # action == 'on'
            # Power on: remove reboot annotation
            annotation_data = {
                "metadata": {
                    "annotations": {
                        "reboot.metal3.io/iha": None
                    }
                }
            }
            
            try:
                response = requests.patch(
                    patch_url, 
                    headers=headers, 
                    verify=cacert, 
                    data=json.dumps(annotation_data),
                    timeout=REQUEST_TIMEOUT
                )
                response.raise_for_status()
                
            except requests.exceptions.Timeout:
                logging.error("BMH power on failed: request timeout for %s", host)
                return False
            except requests.exceptions.HTTPError as e:
                logging.error("BMH power on failed: HTTP error %d for %s: %s", 
                             response.status_code, host, e)
                return False
            except requests.exceptions.RequestException as e:
                logging.error("BMH power on failed: request error for %s: %s", host, e)
                return False
            
            if response.status_code == 200:
                logging.info("BMH power on successful for %s", host)
                return True
            else:
                logging.error("BMH power on failed: unexpected status code %d for %s", 
                             response.status_code, host)
                return False
                
    except Exception as e:
        logging.error("Unexpected error during BMH fencing of %s: %s", host, e)
        logging.debug("Exception traceback:", exc_info=True)
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


def _validate_fencing_params(fencing_data, required_params, agent_name):
    """
    Validate that all required parameters are present in fencing data.
    
    Args:
        fencing_data: Dictionary containing fencing configuration
        required_params: List of required parameter names
        agent_name: Name of the fencing agent (for error messages)
        
    Returns:
        bool: True if all parameters are present, False otherwise
    """
    missing_params = [param for param in required_params if param not in fencing_data]
    if missing_params:
        logging.error("Missing required parameters for %s fencing agent: %s", 
                     agent_name, missing_params)
        return False
    return True


def _fence_ipmi(host, action, fencing_data):
    """
    Fence a host using IPMI.
    
    Args:
        host: Hostname to fence
        action: 'on' or 'off'
        fencing_data: IPMI fencing configuration
        
    Returns:
        bool: True if successful, False otherwise
    """
    # Constants
    IPMI_TIMEOUT = 30
    REQUIRED_PARAMS = ["ipaddr", "ipport", "login", "passwd"]
    
    # Validate parameters
    if not _validate_fencing_params(fencing_data, REQUIRED_PARAMS, "IPMI"):
        return False
    
    # Extract parameters
    ip = str(fencing_data["ipaddr"])
    port = str(fencing_data["ipport"])
    user = str(fencing_data["login"])
    passwd = str(fencing_data["passwd"])
    
    # Build command
    base_cmd = ["ipmitool", "-I", "lanplus", "-H", ip, "-U", user, "-P", passwd, "-p", port]
    
    if action == 'off':
        cmd = base_cmd + ["power", "off"]
        operation = "power off"
    elif action == 'on':
        cmd = base_cmd + ["power", "on"]
        operation = "power on"
    else:
        logging.error("Invalid IPMI action: %s (must be 'on' or 'off')", action)
        return False
    
    logging.debug("Executing IPMI %s command for %s", operation, host)
    
    try:
        result = subprocess.run(cmd, timeout=IPMI_TIMEOUT, capture_output=True, text=True, check=True)
        logging.info("Successfully executed IPMI %s for %s", operation, host)
        return True
        
    except subprocess.TimeoutExpired:
        logging.error("Timeout expired while sending IPMI %s command for %s", operation, host)
        return False
        
    except subprocess.CalledProcessError as e:
        logging.error("IPMI %s command failed for %s: %s", operation, host, e)
        if e.stderr:
            logging.debug("IPMI stderr: %s", e.stderr)
        return False
        
    except Exception as e:
        logging.error("Unexpected error during IPMI %s for %s: %s", operation, host, e)
        return False


def _fence_redfish(host, action, fencing_data):
    """
    Fence a host using Redfish.
    
    Args:
        host: Hostname to fence
        action: 'on' or 'off'
        fencing_data: Redfish fencing configuration
        
    Returns:
        bool: True if successful, False otherwise
    """
    # Constants
    DEFAULT_PORT = "443"
    DEFAULT_UUID = "System.Embedded.1"
    DEFAULT_TIMEOUT = 30
    DEFAULT_TLS = "false"
    REQUIRED_PARAMS = ["ipaddr", "login", "passwd"]
    
    # Validate parameters
    if not _validate_fencing_params(fencing_data, REQUIRED_PARAMS, "Redfish"):
        return False
    
    # Extract parameters with defaults
    ip = str(fencing_data["ipaddr"])
    port = str(fencing_data.get("ipport", DEFAULT_PORT))
    user = str(fencing_data["login"])
    passwd = str(fencing_data["passwd"])
    uuid = str(fencing_data.get("uuid", DEFAULT_UUID))
    timeout = fencing_data.get("timeout", DEFAULT_TIMEOUT)
    tls = str(fencing_data.get("tls", DEFAULT_TLS))
    
    # Build URL
    if tls.lower() == "true":
        url = f'https://{ip}:{port}/redfish/v1/Systems/{uuid}'
    else:
        url = f'http://{ip}:{port}/redfish/v1/Systems/{uuid}'
    
    # Determine action
    if action == 'off':
        redfish_action = "ForceOff"
        operation = "power off"
    elif action == 'on':
        redfish_action = "On"
        operation = "power on"
    else:
        logging.error("Invalid Redfish action: %s (must be 'on' or 'off')", action)
        return False
    
    logging.debug("Executing Redfish %s command for %s", operation, host)
    
    try:
        result = _redfish_reset(url, user, passwd, timeout, redfish_action)
        
        if result:
            logging.info("Successfully executed Redfish %s for %s", operation, host)
            return True
        else:
            logging.error("Redfish %s failed for %s", operation, host)
            return False
            
    except Exception as e:
        logging.error("Unexpected error during Redfish %s for %s: %s", operation, host, e)
        return False


def _fence_bmh(host, action, fencing_data):
    """
    Fence a host using BareMetal Host (BMH).
    
    Args:
        host: Hostname to fence
        action: 'on' or 'off'
        fencing_data: BMH fencing configuration
        
    Returns:
        bool: True if successful, False otherwise
    """
    REQUIRED_PARAMS = ["token", "host", "namespace"]
    
    # Validate parameters
    if not _validate_fencing_params(fencing_data, REQUIRED_PARAMS, "BMH"):
        return False
    
    # Extract parameters
    token = str(fencing_data["token"])
    bmh_host = str(fencing_data["host"])  # Use different variable name to avoid shadowing
    namespace = str(fencing_data["namespace"])
    
    operation = f"power {action}"
    logging.debug("Executing BMH %s command for %s", operation, host)
    
    try:
        if action == 'off':
            result = _bmh_fence(token, namespace, bmh_host, "off")
            if result:
                logging.info("Successfully executed BMH power off for %s", host)
                return True
            else:
                logging.error("BMH power off failed for %s", host)
                return False
                
        elif action == 'on':
            result = _bmh_fence(token, namespace, bmh_host, "on")
            if result:
                logging.info("Successfully executed BMH power on for %s", host)
                return True
            else:
                logging.error("BMH power on failed for %s", host)
                return False
                
        else:
            logging.error("Invalid BMH action: %s (must be 'on' or 'off')", action)
            return False
            
    except Exception as e:
        logging.error("Unexpected error during BMH %s for %s: %s", operation, host, e)
        return False


def _fence_noop(host, action):
    """
    No-op fencing agent that logs a warning and returns success.
    
    Args:
        host: Hostname to fence
        action: 'on' or 'off'
        
    Returns:
        bool: Always returns True
    """
    logging.warning("Using noop fencing agent for %s %s. VMs may get corrupted.", host, action)
    return True


def _host_fence(host, action):
    """
    Fence a host using the configured fencing agent.
    
    This function looks up the fencing configuration for the specified host
    and uses the appropriate fencing agent to power the host on or off.
    
    Args:
        host: Hostname to fence
        action: 'on' or 'off'
        
    Returns:
        bool: True if fencing was successful, False otherwise
    """
    # Input validation
    if not host:
        logging.error("Cannot fence host - no hostname provided")
        return False
        
    if action not in ['on', 'off']:
        logging.error("Cannot fence host %s - invalid action: %s (must be 'on' or 'off')", host, action)
        return False
    
    logging.info("Fencing host %s %s", host, action)
    
    # Look up fencing configuration
    try:
        short_hostname = host.split('.', 1)[0]
        matching_configs = [value for key, value in instanceha_service.config.fencing.items() if key in short_hostname]
        
        if not matching_configs:
            logging.error("No fencing data found for %s", host)
            return False
            
        fencing_data = matching_configs[0]
        
    except (IndexError, KeyError, AttributeError) as e:
        logging.error("Error accessing fencing data for %s: %s", host, e)
        return False
    except Exception as e:
        logging.error("Unexpected error accessing fencing data for %s: %s", host, e)
        return False
    
    # Validate fencing data structure
    if not isinstance(fencing_data, dict):
        logging.error("Invalid fencing data format for %s: expected dict, got %s", host, type(fencing_data))
        return False
        
    if "agent" not in fencing_data:
        logging.error("Missing 'agent' field in fencing data for %s", host)
        return False
    
    agent = fencing_data["agent"]
    logging.debug("Using fencing agent '%s' for %s", agent, host)
    
    # Route to appropriate fencing agent
    try:
        if 'noop' in agent:
            return _fence_noop(host, action)
        elif 'ipmi' in agent:
            return _fence_ipmi(host, action, fencing_data)
        elif 'redfish' in agent:
            return _fence_redfish(host, action, fencing_data)
        elif 'bmh' in agent:
            return _fence_bmh(host, action, fencing_data)
        else:
            logging.error("No valid fencing method detected for %s: %s", host, agent)
            return False
            
    except Exception as e:
        logging.error("Unexpected error during fencing of %s with agent %s: %s", host, agent, e)
        logging.debug("Exception traceback:", exc_info=True)
        return False


def _is_config_enabled(config_var):
    """
    Check if a configuration variable is enabled.
    
    Args:
        config_var: Configuration variable to check
        
    Returns:
        bool: True if config variable contains 'true' (case-insensitive), False otherwise
    """
    if not config_var:
        return False
    return 'true' in str(config_var).lower()


def _get_nova_connection():
    """
    Establish a connection to Nova using environment configuration.
    
    Returns:
        Nova client connection or None if connection fails
    """
    try:
        cloud_name = os.getenv('OS_CLOUD', 'overcloud')
        
        if cloud_name not in instanceha_service.config.clouds:
            logging.error("Cloud configuration not found for: %s", cloud_name)
            return None
        
        if cloud_name not in instanceha_service.config.secure:
            logging.error("Secure configuration not found for: %s", cloud_name)
            return None
        
        # Extract connection parameters
        auth_config = instanceha_service.config.clouds[cloud_name]["auth"]
        username = auth_config["username"]
        project_name = auth_config["project_name"]
        auth_url = auth_config["auth_url"]
        user_domain_name = auth_config["user_domain_name"]
        project_domain_name = auth_config["project_domain_name"]
        password = instanceha_service.config.secure[cloud_name]["auth"]["password"]
        
        conn = nova_login(username, password, project_name, auth_url, 
                         user_domain_name, project_domain_name)
        
        if conn is None:
            logging.error("Nova connection failed - received None from nova_login")
            return None
        
        logging.debug("Nova connection established successfully")
        return conn
        
    except KeyError as e:
        logging.error("Missing configuration key: %s", e)
        return None
    except Exception as e:
        logging.error("Failed to establish Nova connection: %s", e)
        logging.debug("Exception traceback:", exc_info=True)
        return None


def _enable_reserved_host_by_aggregate(conn, service, reserved_hosts):
    """
    Enable a reserved host from the same aggregate as the failed service.
    
    Args:
        conn: Nova client connection
        service: Failed service to find matching aggregate for
        reserved_hosts: List of available reserved hosts
        
    Returns:
        bool: True if a host was enabled, False otherwise
    """
    try:
        # Get aggregate IDs of the failed compute
        compute_aggregate_ids = _aggregate_ids(conn, service)
        if not compute_aggregate_ids:
            logging.debug("No aggregates found for failed compute %s", service.host)
            return False
            
        logging.debug("Aggregate IDs of failed compute %s: %s", service.host, compute_aggregate_ids)
        
        # Find reserved hosts in evacuable aggregates with matching IDs
        matching_hosts = []
        for host in reserved_hosts:
            if not _is_aggregate_evacuable(conn, host.host):
                continue
                
            host_aggregate_ids = _aggregate_ids(conn, host)
            if set(host_aggregate_ids).intersection(compute_aggregate_ids):
                matching_hosts.append(host)
        
        if not matching_hosts:
            logging.warning("No reserved compute found in the same host aggregate of %s", service.host)
            return False
        
        # Enable the first matching host
        selected_host = matching_hosts[0]
        reserved_hosts.remove(selected_host)
        
        if not _host_enable(conn, selected_host, reenable=False):
            logging.error("Failed to enable reserved host %s", selected_host.host)
            return False
            
        logging.info("Enabled host %s from the reserved pool (same aggregate as %s)", 
                    selected_host.host, service.host)
        return True
        
    except Exception as e:
        logging.error("Error enabling reserved host by aggregate: %s", e)
        logging.debug("Exception traceback:", exc_info=True)
        return False


def _enable_reserved_host_by_zone(conn, service, reserved_hosts):
    """
    Enable a reserved host from the same availability zone as the failed service.
    
    Args:
        conn: Nova client connection
        service: Failed service to find matching zone for
        reserved_hosts: List of available reserved hosts
        
    Returns:
        bool: True if a host was enabled, False otherwise
    """
    try:
        # Find reserved hosts in the same availability zone
        matching_hosts = [host for host in reserved_hosts if host.zone == service.zone]
        
        if not matching_hosts:
            logging.warning("No reserved compute found in the same AZ as %s", service.host)
            return False
        
        # Enable the first matching host
        selected_host = matching_hosts[0]
        reserved_hosts.remove(selected_host)
        
        if not _host_enable(conn, selected_host, reenable=False):
            logging.error("Failed to enable reserved host %s", selected_host.host)
            return False
            
        logging.info("Enabled host %s from the reserved pool (same AZ as %s)", 
                    selected_host.host, service.host)
        return True
        
    except Exception as e:
        logging.error("Error enabling reserved host by zone: %s", e)
        logging.debug("Exception traceback:", exc_info=True)
        return False


def _manage_reserved_hosts(conn, service, reserved_hosts):
    """
    Manage reserved hosts by enabling a replacement host if available.
    
    Args:
        conn: Nova client connection
        service: Failed service that needs replacement capacity
        reserved_hosts: List of available reserved hosts
        
    Returns:
        bool: True if operation completed successfully (regardless of whether a host was enabled)
    """
    if not instanceha_service.config.is_reserved_hosts_enabled():
        logging.debug("Reserved hosts feature is disabled")
        return True
    
    if not reserved_hosts:
        logging.warning("Not enough hosts available from the reserved pool")
        return True  # This is not a failure condition
    
    try:
        # Try to enable a host from the same aggregate if tagged aggregates are enabled
        if instanceha_service.config.is_tagged_aggregates_enabled():
            if _enable_reserved_host_by_aggregate(conn, service, reserved_hosts):
                return True
        else:
            # Try to enable a host from the same availability zone
            if _enable_reserved_host_by_zone(conn, service, reserved_hosts):
                return True
        
        # If no specific match found, it's still not a failure
        logging.debug("No matching reserved host found for %s, continuing with evacuation", service.host)
        return True
        
    except Exception as e:
        logging.error("Error managing reserved hosts for %s: %s", service.host, e)
        logging.debug("Exception traceback:", exc_info=True)
        return False


def _post_evacuation_recovery(conn, service):
    """
    Perform post-evacuation recovery by powering on and re-enabling the host.
    
    Args:
        conn: Nova client connection
        service: Service to recover
        
    Returns:
        bool: True if recovery completed successfully, False otherwise
    """
    if instanceha_service.config.is_leave_disabled_enabled():
        logging.info("Evacuation successful. Not re-enabling %s since LEAVE_DISABLED is enabled", 
                    service.host)
        return True
    
    logging.info("Evacuation successful. Starting recovery for %s", service.host)
    
    try:
        # Step 1: Power on the host
        logging.debug("Powering on host %s", service.host)
        power_on_result = _host_fence(service.host, 'on')
        
        if not power_on_result:
            logging.error("Failed to power on %s during recovery", service.host)
            return False
        
        # Step 2: Re-enable the host in Nova
        logging.debug("Re-enabling host %s in Nova", service.host)
        enable_result = _host_enable(conn, service, reenable=False)
        
        if not enable_result:
            logging.error("Failed to re-enable %s during recovery", service.host)
            return False
        
        logging.info("Recovery completed successfully for %s", service.host)
        return True
        
    except Exception as e:
        logging.error("Error during post-evacuation recovery for %s: %s", service.host, e)
        logging.debug("Exception traceback:", exc_info=True)
        return False


def process_service(service, reserved_hosts, resume):
    """
    Process a failed compute service through the complete recovery workflow.
    
    This function orchestrates the complete recovery process for a failed compute service:
    1. Fence the host (if not resuming)
    2. Establish Nova connection
    3. Disable the host (if not resuming)
    4. Manage reserved hosts (enable replacement capacity)
    5. Evacuate instances from the failed host
    6. Recover the host (power on + re-enable, unless disabled by config)
    
    Args:
        service: The failed compute service to process
        reserved_hosts: List of available reserved hosts for replacement capacity
        resume: bool - True if resuming a previous evacuation, False for new failure
        
    Returns:
        bool: True if the complete process succeeded, False otherwise
    """
    # Input validation
    if not service:
        logging.error("Cannot process service - no service object provided")
        return False
    
    if not hasattr(service, 'host'):
        logging.error("Cannot process service - service object missing 'host' attribute")
        return False
    
    if reserved_hosts is None:
        reserved_hosts = []
    
    host_name = service.host
    logging.info("Starting service processing for %s (resume=%s)", host_name, resume)
    
    # Step 1: Fence the host (only if not resuming)
    if not resume:
        logging.info("Fencing host %s", host_name)
        
        try:
            fence_result = _host_fence(host_name, 'off')
            if not fence_result:
                logging.error("Fencing failed for %s, cannot proceed with evacuation", host_name)
                return False
        except Exception as e:
            logging.error("Failed to fence %s: %s", host_name, e)
            logging.debug("Exception traceback:", exc_info=True)
            return False
    
    # Step 2: Establish Nova connection
    logging.debug("Establishing Nova connection for %s", host_name)
    conn = _get_nova_connection()
    if not conn:
        logging.error("Cannot proceed with %s - Nova connection failed", host_name)
        return False
    
    # Step 3: Disable the host (only if not resuming)
    if not resume:
        logging.info("Disabling host %s before evacuation", host_name)
        
        try:
            disable_result = _host_disable(conn, service)
            if not disable_result:
                logging.error("Failed to disable %s, cannot proceed with evacuation", host_name)
                return False
        except Exception as e:
            logging.error("Failed to disable %s: %s", host_name, e)
            logging.debug("Exception traceback:", exc_info=True)
            return False
    
    # Step 4: Manage reserved hosts (enable replacement capacity)
    logging.debug("Managing reserved hosts for %s", host_name)
    if not _manage_reserved_hosts(conn, service, reserved_hosts):
        logging.error("Failed to manage reserved hosts for %s", host_name)
        return False
    
    # Step 5: Evacuate instances from the failed host
    logging.info("Starting evacuation of instances from %s", host_name)
    
    try:
        evacuation_result = _host_evacuate(conn, service)
        if not evacuation_result:
            logging.error("Evacuation failed for %s", host_name)
            return False
    except Exception as e:
        logging.error("Failed to evacuate %s: %s", host_name, e)
        logging.debug("Exception traceback:", exc_info=True)
        return False
    
    # Step 6: Post-evacuation recovery
    logging.info("Starting post-evacuation recovery for %s", host_name)
    
    try:
        recovery_result = _post_evacuation_recovery(conn, service)
        if not recovery_result:
            logging.error("Post-evacuation recovery failed for %s", host_name)
            return False
    except Exception as e:
        logging.error("Failed post-evacuation recovery for %s: %s", host_name, e)
        logging.debug("Exception traceback:", exc_info=True)
        return False
    
    logging.info("Service processing completed successfully for %s", host_name)
    return True


def _get_hosts_with_servers_cached(connection, services: List) -> Dict[str, List]:
    """
    Efficiently cache server lists for all hosts to avoid repeated API calls.
    
    This function replaces inefficient nested loops that call conn.servers.list()
    multiple times with a single batch operation and caching.
    
    Args:
        connection: Nova client connection
        services: List of compute services
        
    Returns:
        Dict mapping host names to their server lists
    """
    host_servers = {}
    
    # Cache server lists for all hosts in one pass
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
                host_servers[service.host] = []
    
    return host_servers


def _filter_hosts_with_servers(services: List, host_servers_cache: Dict[str, List]) -> List:
    """
    Efficiently filter out hosts that have no running servers.
    
    This replaces the O(n²) nested list comprehension with O(n) operation.
    
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


def _filter_hosts_with_evacuable_servers(services: List, host_servers_cache: Dict[str, List], 
                                       flavors: List, images: List) -> List:
    """
    Efficiently filter hosts that have evacuable servers.
    
    This replaces inefficient nested list comprehensions with cached data access.
    
    Args:
        services: List of compute services to filter
        host_servers_cache: Pre-cached mapping of host -> servers
        flavors: List of evacuable flavor IDs
        images: List of evacuable image IDs
        
    Returns:
        List of services that have evacuable servers
    """
    services_with_evacuable = []
    
    for service in services:
        servers = host_servers_cache.get(service.host, [])
        
        # Check if any server on this host is evacuable
        has_evacuable = any(
            _is_server_evacuable(server, flavors, images) 
            for server in servers
        )
        
        if has_evacuable:
            services_with_evacuable.append(service)
            logging.debug("Host %s has evacuable servers", service.host)
    
    return services_with_evacuable


def main():
    global current_hash
    global hash_update_successful

    health_check_thread = threading.Thread(target=instanceha_service.start_health_check_server)
    health_check_thread.daemon = True
    health_check_thread.start()

    previous_hash = ""

    CLOUD = os.getenv('OS_CLOUD', 'overcloud')

    try:
        username = instanceha_service.config.clouds[CLOUD]["auth"]["username"]
        projectname = instanceha_service.config.clouds[CLOUD]["auth"]["project_name"]
        auth_url = instanceha_service.config.clouds[CLOUD]["auth"]["auth_url"]
        user_domain_name = instanceha_service.config.clouds[CLOUD]["auth"]["user_domain_name"]
        project_domain_name = instanceha_service.config.clouds[CLOUD]["auth"]["project_domain_name"]
        password = instanceha_service.config.secure[CLOUD]["auth"]["password"]
    except Exception as e:
        logging.error("Could not find valid data for Cloud: %s" % CLOUD)
        sys.exit(1)

    try:
        conn = nova_login(username, password, projectname, auth_url, user_domain_name, project_domain_name)
        if conn is None:
            logging.error("Failed: Unable to connect to Nova - connection is None")
            sys.exit(1)

    except Exception as e:
        logging.error("Failed: Unable to connect to Nova: " + str(e))
        sys.exit(1)

    while True:
        current_time = str(time.time()).encode('utf-8')
        new_hash = hashlib.sha256(current_time).hexdigest()

        if new_hash == previous_hash:
            logging.error("Hash has not changed. Something went wrong.")
            hash_update_successful = False
        else:
            logging.debug("Hash updated successfully.")
            current_hash = new_hash
            hash_update_successful = True

        previous_hash = current_hash

        try:
            services = conn.services.list(binary="nova-compute")

            # How fast do we want to react / how much do we want to wait before considering a host worth of our attention
            # We take the current time and subtract a DELTA amount of seconds to have a point in time threshold
            target_date = datetime.now() - timedelta(seconds=instanceha_service.config.get_delta())

            # We check if a host is still up but has not been reporting its state for the last DELTA seconds or if it is down.
            # We filter previously disabled hosts or the ones that are forced_down.
            compute_nodes = [service for service in services if (datetime.fromisoformat(service.updated_at) < target_date and service.state != 'down') or (service.state == 'down') and 'disabled' not in service.status and not service.forced_down]

            # Let's check if there are computes nodes that were already being processed, we want to check them again in case the pod was restarted
            to_resume = [service for service in services if service.forced_down and (service.state == 'down') and 'disabled' in service.status and 'instanceha evacuation' in service.disabled_reason and 'evacuation FAILED' not in service.disabled_reason]

            if not (compute_nodes + to_resume) == []:
                logging.warning('The following computes are down:' + str([service.host for service in compute_nodes]))

                # Filter out computes that have no vms running (we don't want to waste time evacuating those anyway)
                # Use performance-optimized caching to avoid O(n²) API calls
                host_servers_cache = instanceha_service.get_hosts_with_servers_cached(conn, compute_nodes)
                compute_nodes = instanceha_service.filter_hosts_with_servers(compute_nodes, host_servers_cache)

                # Get list of reserved hosts (if feature is enabled)
                reserved_hosts = [service for service in services if ('disabled' in service.status and 'reserved' in service.disabled_reason )] if instanceha_service.config.is_reserved_hosts_enabled() else []
                logging.debug('List of reserved hosts: %s' % [h.host for h in reserved_hosts])

                # Check if there are images, flavors or aggregates configured with the EVACUABLE tag
                images = instanceha_service.get_evacuable_images(conn) if instanceha_service.config.is_tagged_images_enabled() else []
                flavors = instanceha_service.get_evacuable_flavors(conn) if instanceha_service.config.is_tagged_flavors_enabled() else []
                
                logging.debug("Using cached evacuable resources: %d flavors, %d images", len(flavors), len(images))

                # Filter hosts if tagged evacuation is enabled
                images_enabled = instanceha_service.config.is_tagged_images_enabled()
                flavors_enabled = instanceha_service.config.is_tagged_flavors_enabled()
                
                # Filter if ANY tagging is enabled
                if images_enabled or flavors_enabled:
                    # Use cached server data to avoid repeated API calls
                    compute_nodes = instanceha_service.filter_hosts_with_evacuable_servers(compute_nodes, host_servers_cache, flavors, images)
                    logging.debug("After evacuable filtering: %d hosts remain (tagged evacuation enabled)", len(compute_nodes))
                    
                    # Note: If no tagged resources are found, all servers will be evacuated
                    # This is handled in the is_server_evacuable method
                    if not flavors and not images:
                        logging.info("No tagged resources found - will evacuate all servers")
                else:
                    logging.debug("No tagged evacuation enabled, processing all %d hosts", len(compute_nodes))

                if instanceha_service.config.is_tagged_aggregates_enabled():
                    compute_nodes_down = compute_nodes
                    # Filter out computes not part of evacuable aggregates (if any aggregate is tagged, otherwise evacuate them all)
                    compute_nodes = [service for service in compute_nodes if instanceha_service.is_aggregate_evacuable(conn, service.host)]
                    # Override services to only account the ones that are part of evacuable aggregates
                    services = [service for service in services if instanceha_service.is_aggregate_evacuable(conn, service.host)]
                    # warn user about non tagged computes
                    down_not_tagged = [service.host for service in compute_nodes_down if service not in compute_nodes]
                    if down_not_tagged:
                        logging.warning('The following computes are not part of an evacuable aggregate, so they will not be recovered: %s' % down_not_tagged)

                logging.debug('List of stale services is %s' % [service.host for service in compute_nodes])

                if compute_nodes or to_resume:
                    if (len(compute_nodes) / len(services) * 100) > instanceha_service.config.get_threshold():
                        logging.error('Number of impacted computes exceeds the defined threshold. There is something wrong. Not evacuating.')
                        pass
                    else:
                        # Check if some of these computes are crashed and currently kdumping
                        to_evacuate = _check_kdump(compute_nodes) if instanceha_service.config.is_kdump_check_enabled() else compute_nodes

                        if not instanceha_service.config.is_disabled():
                            # process computes that are seen as down for the first time
                            with concurrent.futures.ThreadPoolExecutor() as executor:
                                results = list(executor.map(lambda service: process_service(service, reserved_hosts, False), to_evacuate))
                            if not all(results):
                                logging.warning('Some services failed to evacuate. Retrying in 30 seconds.')
                            # process computes that were half-evacuated
                            with concurrent.futures.ThreadPoolExecutor() as executor:
                                results = list(executor.map(lambda service: process_service(service, reserved_hosts, True), to_resume))
                            if not all(results):
                                logging.warning('Some services failed to evacuate. Retrying in %s seconds.' % instanceha_service.config.get_poll_interval())

                        else:
                            logging.info('InstanceHa DISABLE is true, not evacuating')

            # We need to wait until a compute is back and for the migrations to move from 'done' to 'completed' before we can force_down=false

            to_reenable = [service for service in services if 'enabled' in service.status and service.forced_down]
            if to_reenable:
                logging.debug('The following computes have forced_down=true, checking if they can be re-enabled: %s' % repr(to_reenable))

                # list all the migrations having each compute as source, if they are all completed (or failed) go ahead and re-enable it
                for i in to_reenable:
                    migr = conn.migrations.list(source_compute=i.host, migration_type='evacuation', limit='100')
                    # users can bypass the safety net by setting FORCE_ENABLE=true
                    incomplete = [a.id for a in migr if 'completed' not in a.status and 'error' not in a.status] if not instanceha_service.config.is_force_enable_enabled() else []

                    if incomplete == []:
                        logging.info('All migrations completed, reenabling %s' % i.host)
                        try:
                            _host_enable(conn, i, reenable=True)
                        except Exception as e:
                            logging.error('Failed to enable %s: %s' % (i.host, e))
                            logging.debug('Exception traceback:', exc_info=True)
                    else:
                        logging.warning('At least one migration not completed %s, not reenabling %s' % (incomplete, i.host) )

        except Exception as e:
            logging.warning("Failed to query compute status. Please check the Nova Api availability.")

        time.sleep(instanceha_service.config.get_poll_interval())


if __name__ == "__main__":
    main()
