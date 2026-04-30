"""
NGS-Agent v2: Configuration Management System
Handles loading, validation, and runtime configuration.
"""

import os
import sys
from typing import Any, Dict, Optional
from pathlib import Path

try:
    import yaml
except ImportError:
    yaml = None


class ConfigError(Exception):
    """Configuration error exception."""
    pass


class ConfigManager:
    """Manages pipeline configuration with validation."""
    
    def __init__(self, config_path: Optional[str] = None):
        """
        Initialize configuration manager.
        
        Args:
            config_path: Path to YAML config file. If None, use default.
        """
        self.config_path = config_path or self._find_default_config()
        self.config: Dict[str, Any] = {}
        self.load()
    
    @staticmethod
    def _find_default_config() -> str:
        """Find default configuration file."""
        candidates = [
            Path(__file__).parent / "default.yaml",
            Path("./config/default.yaml"),
            Path("config/default.yaml"),
        ]
        for path in candidates:
            if path.exists():
                return str(path.resolve())
        raise ConfigError("No default.yaml config found")
    
    def load(self):
        """Load configuration from YAML file."""
        if not yaml:
            raise ConfigError("PyYAML not installed. Run: pip install pyyaml")
        
        if not os.path.exists(self.config_path):
            raise ConfigError(f"Config file not found: {self.config_path}")
        
        with open(self.config_path, 'r') as f:
            self.config = yaml.safe_load(f) or {}
        
        self.validate()
    
    def validate(self):
        """Validate configuration."""
        required_keys = ['paths', 'organism', 'qc']
        for key in required_keys:
            if key not in self.config:
                raise ConfigError(f"Missing required config section: {key}")
    
    def get(self, path: str, default: Any = None) -> Any:
        """
        Get config value using dot notation.
        
        Example:
            config.get("organism.name") -> "human"
            config.get("qc.enabled") -> True
        """
        keys = path.split('.')
        value = self.config
        
        for key in keys:
            if isinstance(value, dict):
                value = value.get(key)
            else:
                return default
            
            if value is None:
                return default
        
        return value
    
    def set(self, path: str, value: Any):
        """Set config value using dot notation."""
        keys = path.split('.')
        target = self.config
        
        for key in keys[:-1]:
            if key not in target:
                target[key] = {}
            target = target[key]
        
        target[keys[-1]] = value
    
    def get_paths(self) -> Dict[str, Path]:
        """Get all configured paths as Path objects."""
        paths_config = self.get('paths', {})
        paths = {}
        for key, value in paths_config.items():
            if value:
                path = Path(value).expanduser()
                path.mkdir(parents=True, exist_ok=True)
                paths[key] = path
        return paths
    
    def get_organism_info(self) -> Dict[str, str]:
        """Get organism configuration."""
        return self.get('organism', {})
    
    def get_qc_settings(self) -> Dict[str, Any]:
        """Get QC settings."""
        return self.get('qc', {})
    
    def to_dict(self) -> Dict[str, Any]:
        """Export configuration as dictionary."""
        return self.config.copy()
    
    def __repr__(self) -> str:
        return f"ConfigManager(config_path={self.config_path})"


# Global config instance
_config_instance: Optional[ConfigManager] = None


def init_config(config_path: Optional[str] = None) -> ConfigManager:
    """Initialize global configuration."""
    global _config_instance
    _config_instance = ConfigManager(config_path)
    return _config_instance


def get_config() -> ConfigManager:
    """Get global configuration instance."""
    global _config_instance
    if _config_instance is None:
        _config_instance = ConfigManager()
    return _config_instance
