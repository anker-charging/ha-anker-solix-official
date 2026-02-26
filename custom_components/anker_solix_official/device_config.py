"""Anker Solix device configuration manager."""

import logging
from pathlib import Path
from typing import Any

import yaml

 

_LOGGER = logging.getLogger(__name__)

class AnkerSolixDeviceConfig:
    """Anker Solix device configuration manager."""
    def __init__(self, hass, config_path: str | None = None):
        """Initialize configuration manager."""
        self.hass = hass
        self.devices_dir = Path(__file__).resolve().parent / "config"
        self._device_configs = {}
        # Unified configuration: always use config/anker.yaml

    
    


    async def load_device_config_by_file_async(self, config_file: str) -> dict[str, Any] | None:
        """Load device configuration by file path (async version)."""
        try:
            # Check cache
            if config_file in self._device_configs:
                return self._device_configs[config_file]

            # Handle relative paths
            if not Path(config_file).is_absolute():
                config_file = str(self.devices_dir.parent / config_file)

            if not Path(config_file).exists():
                _LOGGER.warning("Device configuration file does not exist: %s", config_file)
                return None

            # Load file content asynchronously using executor
            import asyncio
            import concurrent.futures
            
            def _load_file():
                with Path(config_file).open(encoding='utf-8') as file:
                    return yaml.safe_load(file)
            
            # Run file operation in executor to avoid blocking
            loop = asyncio.get_event_loop()
            device_config = await loop.run_in_executor(None, _load_file)
            self._device_configs[config_file] = device_config

            _LOGGER.info("Successfully loaded device configuration file: %s", config_file)
            return device_config

        except (OSError, yaml.YAMLError, UnicodeDecodeError) as e:
            _LOGGER.error("Failed to load device configuration file: %s", e)
            return None


