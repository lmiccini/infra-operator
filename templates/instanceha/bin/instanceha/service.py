import hashlib
import logging
import sys as _sys
import threading
import time
from abc import ABC, abstractmethod
from collections import defaultdict
from typing import Any, Callable, Dict, List, Optional

from .config import ConfigManager
from .models import (
    CACHE_TIMEOUT_SECONDS,
    HEALTH_CHECK_PORT,
    NovaLoginCredentials,
    OpenStackClient,
)
from .nova import NovaConnectionError, nova_login
from .validation import _safe_log_exception

# Package-level reference for mock-patchability: tests patch 'instanceha.X'
# so runtime calls must go through the package namespace.
_pkg = _sys.modules[__package__]


class CloudConnectionProvider(ABC):
    """Abstract interface for cloud connection management."""

    @abstractmethod
    def get_connection(self) -> Optional[OpenStackClient]:
        """Get a connection to the cloud provider."""
        pass

    @abstractmethod
    def create_connection(self) -> Optional[OpenStackClient]:
        """Create a new connection to the cloud provider."""
        pass


class InstanceHAService(CloudConnectionProvider):
    """
    Main service class that encapsulates all InstanceHA functionality.

    This class eliminates global variables and provides dependency injection
    for better testability, maintainability, and architectural cleanliness.
    """

    def __init__(self, config_manager: ConfigManager, cloud_client: Optional[OpenStackClient] = None):
        """
        Initialize the InstanceHA service.

        Args:
            config_manager: Configuration manager instance
            cloud_client: Optional OpenStack client for testing (None for production)
        """
        self.config = config_manager
        self.cloud_client = cloud_client

        self._initialize_health_state()
        self._initialize_cache()
        self._initialize_threading()
        self._initialize_kdump_state()
        self._initialize_processing_state()

        logging.info("InstanceHA service initialized successfully")

    def _initialize_health_state(self) -> None:
        """Initialize health monitoring state."""
        self.current_hash = ""
        self.hash_update_successful = True
        self._last_hash_time = 0
        self._previous_hash = ""

    def _initialize_cache(self) -> None:
        """Initialize caching infrastructure and cached configuration values."""
        self._host_servers_cache = {}
        self._evacuable_flavors_cache = None
        self._evacuable_images_cache = None
        self._cache_timestamp = 0
        self._cache_lock = threading.Lock()

        # Cache frequently accessed config values
        self.evacuable_tag = self.config.get_config_value('EVACUABLE_TAG')

    def _initialize_threading(self) -> None:
        """Initialize threading infrastructure."""
        self.health_check_thread = None
        self.UDP_IP = ''  # UDP listener IP

    def _initialize_kdump_state(self) -> None:
        """Initialize kdump detection and management state."""
        self.kdump_hosts_timestamp = defaultdict(float)
        self.kdump_hosts_checking = defaultdict(float)
        self.kdump_listener_stop_event = threading.Event()
        self.kdump_fenced_hosts = set()

    def _initialize_processing_state(self) -> None:
        """Initialize host processing tracking state."""
        self.hosts_processing = defaultdict(float)
        self.processing_lock = threading.Lock()

    def update_health_hash(self, hash_interval: Optional[int] = None) -> None:
        """Update health monitoring hash for service status tracking."""
        if hash_interval is None:
            hash_interval = self.config.get_config_value('HASH_INTERVAL')

        current_timestamp = time.time()

        if current_timestamp - self._last_hash_time > hash_interval:
            new_hash = hashlib.sha256(str(current_timestamp).encode()).hexdigest()
            if new_hash == self._previous_hash:
                logging.error("Hash has not changed. Something went wrong.")
                self.hash_update_successful = False
            else:
                self.current_hash = new_hash
                self.hash_update_successful = True
                self._previous_hash = self.current_hash
                self._last_hash_time = current_timestamp

    def get_connection(self) -> Optional[OpenStackClient]:
        """
        Get cloud client connection with dependency injection support.

        Returns:
            OpenStack client connection or None if failed
        """
        if self.cloud_client:
            return self.cloud_client

        return self.create_connection()

    def create_connection(self) -> Optional[OpenStackClient]:
        """Create a new Nova connection using configuration."""
        try:
            cloud_name = self.config.get_cloud_name()
            auth = self.config.clouds[cloud_name]["auth"]
            password = self.config.secure[cloud_name]["auth"]["password"]
            region_name = self.config.clouds[cloud_name]["region_name"]

            credentials = NovaLoginCredentials(
                username=auth["username"],
                password=password,
                project_name=auth["project_name"],
                auth_url=auth["auth_url"],
                user_domain_name=auth["user_domain_name"],
                project_domain_name=auth["project_domain_name"],
                region_name=region_name
            )
            return _pkg.nova_login(credentials)
        except Exception as e:
            _pkg._safe_log_exception("Failed to create Nova connection", e, include_traceback=True)
            raise NovaConnectionError("Nova connection failed")

    def _check_image_match(self, server, evac_images) -> bool:
        """Check if server image matches evacuable criteria."""
        server_image_id = self._get_server_image_id(server)
        return (evac_images and server_image_id and server_image_id in evac_images) or \
               self.is_server_image_evacuable(server)

    def _check_flavor_match(self, server, evac_flavors) -> bool:
        """Check if server flavor matches evacuable criteria."""
        if not evac_flavors:
            return False

        try:
            flavor_extra_specs = server.flavor.get('extra_specs', {})

            # Check for exact key match or substring match in composite keys
            matching_key = next((k for k in flavor_extra_specs
                               if k == self.evacuable_tag or self.evacuable_tag in k), None)

            return matching_key and str(flavor_extra_specs[matching_key]).lower() == 'true'
        except (AttributeError, KeyError, TypeError):
            logging.debug("Could not check flavor extra specs for server %s", server.id)
            return False

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
        evac_flavors = evac_flavors if evac_flavors is not None else self.get_evacuable_flavors()
        evac_images = evac_images if evac_images is not None else self.get_evacuable_images()

        images_enabled = self.config.get_config_value('TAGGED_IMAGES')
        flavors_enabled = self.config.get_config_value('TAGGED_FLAVORS')

        # When tagging is disabled, evacuate all servers (default behavior)
        if not (images_enabled or flavors_enabled):
            return True

        # Early return if no tagged resources
        if not ((images_enabled and evac_images) or (flavors_enabled and evac_flavors)):
            logging.info("No tagged resources found - evacuating all servers")
            return True

        # Check matches
        matches = [
            self._check_image_match(server, evac_images) if images_enabled else False,
            self._check_flavor_match(server, evac_flavors) if flavors_enabled else False
        ]

        if any(matches):
            return True

        # Provide feedback on why server is not evacuable
        criteria = [c for c, enabled in [
            ("evacuable images", images_enabled and evac_images),
            ("evacuable flavors", flavors_enabled and evac_flavors)
        ] if enabled]
        if criteria:
            logging.warning("Instance %s is not evacuable: not using any of the defined %s tagged with '%s'",
                           server.id, " or ".join(criteria), self.evacuable_tag)
        return False

    def get_evacuable_flavors(self, connection: Optional[OpenStackClient] = None):
        """
        Get list of evacuable flavor IDs with caching.

        Args:
            connection: Optional Nova connection

        Returns:
            List of evacuable flavor IDs
        """
        if not self.config.get_config_value('TAGGED_FLAVORS'):
            return []

        if connection is None:
            connection = self.get_connection()

        if connection is None:
            logging.error("No Nova connection available for flavor caching")
            return []

        # Lock granularity pattern: read check -> API call outside lock -> write update
        with self._cache_lock:
            if self._evacuable_flavors_cache is not None:
                return self._evacuable_flavors_cache

        # Perform expensive API call outside lock (no blocking)
        try:
            flavors = connection.flavors.list(is_public=None)

            cache_data = []
            for flavor in flavors:
                try:
                    if self._is_flavor_evacuable(flavor, self.evacuable_tag):
                        cache_data.append(flavor.id)
                        logging.debug("Added flavor %s to evacuable cache", flavor.id)
                except Exception as e:
                    logging.debug("Could not check keys for flavor %s: %s", flavor.id, e)

            logging.debug("Cached %d evacuable flavors from %d total flavors",
                         len(cache_data), len(flavors))
        except Exception as e:
            logging.error("Failed to get evacuable flavors: %s", e)
            logging.debug('Exception traceback:', exc_info=True)
            cache_data = []

        # Update cache with lock
        with self._cache_lock:
            self._evacuable_flavors_cache = cache_data

        return self._evacuable_flavors_cache

    def get_evacuable_images(self, connection: Optional[OpenStackClient] = None):
        """
        Get list of evacuable image IDs with caching.

        Args:
            connection: Optional Nova connection

        Returns:
            List of evacuable image IDs
        """
        if not self.config.get_config_value('TAGGED_IMAGES'):
            return []

        if connection is None:
            connection = self.get_connection()

        if connection is None:
            logging.error("No Nova connection available for image caching")
            return []

        # Check cache with lock
        with self._cache_lock:
            if self._evacuable_images_cache is not None:
                return self._evacuable_images_cache

        # Image retrieval strategies (in order of preference)
        def _try_nova_glance():
            if hasattr(connection, 'glance'):
                images = connection.glance.list()
                logging.debug("Retrieved %d images via Nova glance client", len(images))
                return images
            return None

        def _try_nova_images():
            if hasattr(connection, 'images'):
                images = connection.images.list()
                logging.debug("Retrieved %d images via Nova images interface", len(images))
                return images
            return None

        def _try_separate_glance():
            from glanceclient import client as glance_client
            if hasattr(connection, 'api') and hasattr(connection.api, 'session'):
                session = connection.api.session
                region_name = connection.region_name
                glance = glance_client.Client('2', session=session, region_name=region_name)
                images = list(glance.images.list())
                logging.debug("Retrieved %d images via separate Glance client", len(images))
                return images
            return None

        retrieval_strategies = [
            ("Nova glance client", _try_nova_glance),
            ("Nova images interface", _try_nova_images),
            ("Separate Glance client", _try_separate_glance),
        ]

        # Try each strategy until one succeeds
        images = []
        for strategy_name, strategy_func in retrieval_strategies:
            try:
                result = strategy_func()
                if result:
                    images = result
                    break
            except ImportError:
                logging.debug("%s not available for import", strategy_name)
            except Exception as e:
                logging.debug("%s access failed: %s", strategy_name, e)

        # Perform expensive API call outside lock
        try:
            # Process images to find evacuable ones
            cache_data = []
            if images:
                for image in images:
                    # Check image for evacuable tags using unified method
                    if self._is_resource_evacuable(image, self.evacuable_tag, ['tags', 'metadata', 'properties']):
                        cache_data.append(image.id)

                logging.debug("Cached %d evacuable images from %d total images",
                             len(cache_data), len(images))
            else:
                # No images retrieved, fall back to per-server checking
                logging.warning("Could not retrieve images for caching. Image evacuation will use per-server checking.")

        except Exception as e:
            logging.error("Failed to get evacuable images: %s", e)
            logging.debug('Exception traceback:', exc_info=True)
            cache_data = []

        # Update cache with lock
        with self._cache_lock:
            self._evacuable_images_cache = cache_data

        return self._evacuable_images_cache

    def _get_server_image_id(self, server):
        """Extract image ID from server object, handling different data formats."""
        try:
            image = getattr(server, 'image', None)
            if not image:
                return None
            if isinstance(image, dict):
                return image.get('id')
            if isinstance(image, str):
                return image
            return getattr(image, 'id', None)
        except (AttributeError, TypeError):
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

    def _is_resource_evacuable(self, resource, evacuable_tag, attribute_checks):
        """
        Unified method to check if a resource (image, flavor, aggregate) is evacuable.

        Args:
            resource: The resource object to check
            evacuable_tag: The tag to look for
            attribute_checks: List of attribute names to check on the resource

        Returns:
            bool: True if resource is evacuable, False otherwise
        """
        for attr_name in attribute_checks:
            if hasattr(resource, attr_name):
                attr_value = getattr(resource, attr_name, {})
                if self._check_evacuable_tag(attr_value, evacuable_tag):
                    return True
        return False

    def _is_flavor_evacuable(self, flavor, evacuable_tag):
        """Check if a flavor is evacuable based on its extra specs."""
        try:
            flavor_keys = flavor.get_keys()

            # Check if evacuable_tag is an exact key match or part of a key
            matching_key = None
            if evacuable_tag in flavor_keys:
                matching_key = evacuable_tag
            else:
                for key in flavor_keys:
                    if evacuable_tag in key:
                        matching_key = key
                        break

            if matching_key:
                value = flavor_keys[matching_key]
                return str(value).lower() == 'true'
            return False
        except (AttributeError, KeyError, TypeError):
            return False

    def is_server_image_evacuable(self, server, connection: Optional[OpenStackClient] = None):
        """
        Check if a server's image is tagged as evacuable (fallback method).

        This method is used when pre-caching fails.
        """
        if not self.config.get_config_value('TAGGED_IMAGES'):
            return False

        if connection is None:
            connection = self.get_connection()

        if connection is None:
            logging.debug("No Nova connection available for image evacuability check")
            return False

        try:
            server_image_id = self._get_server_image_id(server)
            if not server_image_id:
                logging.debug("Server %s has no image ID", server.id)
                return False

            # Try multiple methods to get image details and check evacuability
            for get_method, check_attrs in [
                (lambda: connection.glance.get(server_image_id), ['tags']),
                (lambda: connection.images.get(server_image_id), ['metadata', 'properties'])
            ]:
                try:
                    image = get_method()
                    if self._is_resource_evacuable(image, self.evacuable_tag, check_attrs):
                        return True
                except (AttributeError, TypeError, KeyError) as e:
                    logging.debug("Error checking image attributes: %s", e)
                    continue

            logging.debug("Could not determine evacuability of image %s for server %s", server_image_id, server.id)
            return False

        except Exception as e:
            logging.debug("Error checking image evacuability for server %s: %s", server.id, e)
            return False

    def is_aggregate_evacuable(self, connection: OpenStackClient, host: str) -> bool:
        """Check if a host is part of an aggregate tagged as evacuable."""
        try:
            aggregates = connection.aggregates.list()
            return any(host in agg.hosts for agg in aggregates
                      if self._is_resource_evacuable(agg, self.evacuable_tag, ['metadata']))
        except Exception as e:
            logging.error("Failed to check aggregate evacuability for %s: %s", host, e)
            logging.debug('Exception traceback:', exc_info=True)
            return False

    def get_hosts_with_servers_cached(self, connection, services):
        """Efficiently cache server lists for all hosts to avoid repeated API calls."""
        host_servers = {}

        for service in services:
            if service.host not in host_servers:
                try:
                    servers = connection.servers.list(search_opts={
                        'host': service.host,
                        'all_tenants': 1
                    })
                    host_servers[service.host] = servers
                    if servers:
                        server_info = [(s.id, s.name, s.status) for s in servers]
                        logging.info("Found %d servers on host %s: %s", len(servers), service.host, server_info)
                    else:
                        logging.info("No servers found on host %s", service.host)
                except Exception as e:
                    logging.warning("Failed to get servers for host %s: %s", service.host, e)
                    logging.debug('Exception traceback:', exc_info=True)
                    host_servers[service.host] = []

        return host_servers

    def filter_hosts_with_servers(self, services, host_servers_cache):
        """Efficiently filter out hosts that have no running servers."""
        return [
            service for service in services
            if host_servers_cache.get(service.host, [])
        ]

    def filter_hosts_with_evacuable_servers(self, services, host_servers_cache, flavors=None, images=None):
        """Efficiently filter hosts that have evacuable servers."""
        if flavors is None:
            flavors = self.get_evacuable_flavors()
        if images is None:
            images = self.get_evacuable_images()

        services_with_evacuable = []

        for service in services:
            servers = host_servers_cache.get(service.host, [])

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

        HTTPServer(('', HEALTH_CHECK_PORT), HealthHandler).serve_forever()

    def _cleanup_dict_by_condition(self, dictionary: dict, condition_func: Callable[[Any, Any], bool],
                                   log_message: Optional[str] = None) -> int:
        """Remove dictionary entries matching condition function."""
        to_remove = [k for k, v in dictionary.items() if condition_func(k, v)]
        for k in to_remove:
            del dictionary[k]
            if log_message:
                logging.debug(log_message.format(k))
        return len(to_remove)

    def refresh_evacuable_cache(self, connection=None, force=False, cache_timeout=CACHE_TIMEOUT_SECONDS):
        """Refresh evacuable flavors and images cache with intelligent timing."""
        current_time = time.time()

        with self._cache_lock:
            cache_age = current_time - self._cache_timestamp

            if force or cache_age > cache_timeout or not self._evacuable_flavors_cache:
                logging.debug("Refreshing evacuable cache (age: %.1f seconds)", cache_age)
                self._evacuable_flavors_cache = None
                self._evacuable_images_cache = None
                self._cache_timestamp = current_time
            else:
                logging.debug("Cache is fresh (age: %.1f seconds), skipping refresh", cache_age)
                return False

        # Force re-cache (done outside lock to avoid blocking)
        evac_flavors = self.get_evacuable_flavors(connection)
        evac_images = self.get_evacuable_images(connection)

        logging.debug("Cache refreshed: %d flavors, %d images", len(evac_flavors), len(evac_images))
        return True
