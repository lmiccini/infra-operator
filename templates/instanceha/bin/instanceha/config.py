import logging
import os
import yaml
from yaml.loader import SafeLoader
from typing import Dict, Any, Optional, Union

from .models import ConfigItem, DEFAULT_UDP_PORT


class ConfigurationError(Exception):
    """Raised when configuration loading or validation fails."""
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

    def _load_yaml_file(self, path: str, name: str) -> Dict[str, Any]:
        """Load and parse a YAML file with error handling."""
        try:
            if not os.path.exists(path):
                logging.warning("%s file not found at %s, using defaults", name, path)
                return {}

            with open(path, 'r') as stream:
                data = yaml.load(stream, Loader=SafeLoader)
                if not isinstance(data, dict):
                    raise ConfigurationError(f"Invalid {name} format in {path}")
                return data

        except yaml.YAMLError as e:
            logging.error("Failed to parse %s file %s: %s", name, path, e)
            raise ConfigurationError(f"{name} parsing failed: {e}")
        except (IOError, OSError) as e:
            logging.error("Failed to read %s file %s: %s", name, path, e)
            raise ConfigurationError(f"{name} file access failed: {e}")

    def _load_config(self) -> Dict[str, Any]:
        data = self._load_yaml_file(self.config_path, "configuration")
        config = data.get("config", {})
        # Allow the INSTANCEHA_DISABLED env var (set from the CR spec) to override
        # the config file value. The env var is always set (either "True" or
        # "False"), so we only override when it is explicitly "True".
        if os.getenv('INSTANCEHA_DISABLED') == 'True':
            config['DISABLED'] = True
        return config

    def _load_clouds_config(self) -> Dict[str, Any]:
        data = self._load_yaml_file(self.clouds_path, "clouds configuration")
        if not data:
            return {}
        if "clouds" not in data:
            raise ConfigurationError(f"Missing 'clouds' key in clouds configuration at {self.clouds_path}")
        return data["clouds"]

    def _load_secure_config(self) -> Dict[str, Any]:
        data = self._load_yaml_file(self.secure_path, "secure configuration")
        if not data:
            return {}
        if "clouds" not in data:
            raise ConfigurationError(f"Missing 'clouds' key in secure configuration at {self.secure_path}")
        return data["clouds"]

    def _load_fencing_config(self) -> Dict[str, Any]:
        data = self._load_yaml_file(self.fencing_path, "fencing configuration")
        if not data:
            return {}
        if "FencingConfig" not in data:
            raise ConfigurationError(f"Missing 'FencingConfig' key in fencing configuration at {self.fencing_path}")
        return data["FencingConfig"]

    def _validate_config(self) -> None:
        """Validate critical configuration values using the configuration map."""
        # Auto-validate all config values with constraints
        validation_keys = ['DELTA', 'POLL', 'THRESHOLD', 'WORKERS', 'LOGLEVEL']
        for key in validation_keys:
            try:
                self.get_config_value(key)  # This will validate against _config_map constraints
            except ValueError as e:
                raise ConfigurationError(str(e))

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
            logging.warning(f"Configuration {key} should be integer, got {type(value).__name__}, using default: {default}")
            return default

        # Clamp value to min/max bounds
        if min_val is not None:
            int_value = max(min_val, int_value)
        if max_val is not None:
            int_value = min(max_val, int_value)
        return int_value

    def get_bool(self, key: str, default: bool = False) -> bool:
        """Get a boolean configuration value with validation."""
        value = self.config.get(key, default)

        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.lower() in ('true', '1', 'yes', 'on', 'enabled')

        logging.warning(f"Configuration {key} should be boolean, got {type(value).__name__}, using default: {default}")
        return default

    # Configuration mapping with defaults and validation
    _config_map: Dict[str, ConfigItem] = {
        'EVACUABLE_TAG': ConfigItem('str', 'evacuable'),
        'DELTA': ConfigItem('int', 30, 10, 300),
        'POLL': ConfigItem('int', 45, 15, 600),
        'THRESHOLD': ConfigItem('int', 50, 0, 100),
        'WORKERS': ConfigItem('int', 4, 1, 50),
        'DELAY': ConfigItem('int', 0, 0, 300),
        'LOGLEVEL': ConfigItem('str', 'INFO'),
        'SMART_EVACUATION': ConfigItem('bool', False),
        'RESERVED_HOSTS': ConfigItem('bool', False),
        'FORCE_RESERVED_HOST_EVACUATION': ConfigItem('bool', False),
        'TAGGED_IMAGES': ConfigItem('bool', True),
        'TAGGED_FLAVORS': ConfigItem('bool', True),
        'TAGGED_AGGREGATES': ConfigItem('bool', True),
        'LEAVE_DISABLED': ConfigItem('bool', False),
        'FORCE_ENABLE': ConfigItem('bool', False),
        'CHECK_KDUMP': ConfigItem('bool', False),
        'KDUMP_TIMEOUT': ConfigItem('int', 30, 5, 300),
        'DISABLED': ConfigItem('bool', False),
        'SSL_VERIFY': ConfigItem('bool', True),
        'FENCING_TIMEOUT': ConfigItem('int', 30, 5, 120),
        'HASH_INTERVAL': ConfigItem('int', 60, 30, 300),
    }

    def get_config_value(self, key: str) -> Union[str, int, bool]:
        """Get configuration value with automatic type handling and validation."""
        if key not in self._config_map:
            raise ValueError(f"Unknown configuration key: {key}")

        config_item = self._config_map[key]

        if config_item.type == 'str':
            result = self.get_str(key, config_item.default)
            if key == 'LOGLEVEL':
                result = result.upper()
                valid_levels = ['DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL']
                if result not in valid_levels:
                    raise ValueError(f"LOGLEVEL must be one of {valid_levels}, got {result}")
            return result
        elif config_item.type == 'int':
            return self.get_int(key, config_item.default, config_item.min_val, config_item.max_val)
        elif config_item.type == 'bool':
            return self.get_bool(key, config_item.default)

        return config_item.default

    def get_cloud_name(self) -> str:
        return os.getenv('OS_CLOUD', 'overcloud')

    def get_udp_port(self) -> int:
        return int(os.getenv('UDP_PORT', DEFAULT_UDP_PORT))

    @property
    def ssl_ca_bundle(self) -> Optional[str]:
        return self._get_ssl_path('SSL_CA_BUNDLE')

    @property
    def ssl_cert_path(self) -> Optional[str]:
        return self._get_ssl_path('SSL_CERT_PATH')

    @property
    def ssl_key_path(self) -> Optional[str]:
        return self._get_ssl_path('SSL_KEY_PATH')

    def _get_ssl_path(self, key: str) -> Optional[str]:
        """Get SSL file path if it exists."""
        path = self.get_str(key, '')
        return path if path and os.path.exists(path) else None

    def get_requests_ssl_config(self) -> Union[bool, str, tuple]:
        """Get SSL configuration for requests library."""
        if not self.get_config_value('SSL_VERIFY'):
            logging.warning("SSL verification is DISABLED - this is insecure for production use")
            return False

        if self.ssl_cert_path and self.ssl_key_path:
            return (self.ssl_cert_path, self.ssl_key_path)
        if self.ssl_ca_bundle:
            return self.ssl_ca_bundle
        return True
