"""Data coordinator."""

import asyncio
import logging
import time
from datetime import timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, CoreState
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.const import EVENT_HOMEASSISTANT_STARTED
from homeassistant.helpers import device_registry as dr

from .const import DOMAIN, SCAN_INTERVAL, LOG_THROTTLE_INTERVAL, CONNECTION_RETRY_DELAY
from .modbus_manager import ModbusConnectionManager
from .device_config import AnkerSolixDeviceConfig
from .config_utils import parse_device_configuration
from .async_resource_manager import AsyncResourceManager
from .throttled_logger import ThrottledLogger
from .product_mapping import get_product_name_from_config
from .device_logger import DeviceLoggerAdapter


class AnkerSolixOfficialCoordinator(DataUpdateCoordinator):
    """Modbus local device data coordinator."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry):
        """Initialize coordinator."""
        device_name = entry.data.get("device_name", "Modbus Virtual Device")
        ip_address = entry.data.get("ip_address", "127.0.0.1")
        port = entry.data.get("port", 502)

        super().__init__(
            hass,
            logging.getLogger(__name__),
            name=f"{DOMAIN}_{device_name}_{ip_address}",
            update_interval=timedelta(seconds=SCAN_INTERVAL),
        )

        self.entry = entry
        self.config_entry = entry
        self.device_name = device_name
        self.ip_address = ip_address
        self.port = port
        self.scan_interval = SCAN_INTERVAL

        self.device_logger = DeviceLoggerAdapter(
            logging.getLogger(__name__),
            device_name=self.device_name,
            device_ip=self.ip_address,
            device_port=self.port,
        )

        self.device_config = AnkerSolixDeviceConfig(hass)

        self.modbus_manager = ModbusConnectionManager()
        self.modbus_manager.initialize(self.ip_address, self.port, self.device_name)

        self.update_interval = timedelta(seconds=self.scan_interval)
        self.device_logger.warning(
            "Coordinator initialized (scan interval: %ds)", self.scan_interval
        )

        # Device configuration cache
        self._device_config_cache = None
        self._batch_ranges_cache = None
        self._config_cache_valid = False
        self._full_config_cache = (
            None  # Store full YAML config (including product_info)
        )
        # Background connection/data loop
        self._bg_task = None
        self._stop_bg = False
        self._status = "disconnected"  # disconnected | connecting | connected
        self._latest_data: dict[str, Any] = {}
        # Serialize all modbus I/O
        self._io_lock = asyncio.Lock()
        self._selected_config_file: str | None = None
        self._ever_connected: bool = False

        # Use async resource manager for background task management
        self._resource_manager = AsyncResourceManager()

        # Write protection: protect specific entity values after write operations
        # This prevents the UI from "flashing back" when device is still processing
        # Key: entity_key, Value: (protected_until_timestamp, protected_value)
        self._protected_values: dict[str, tuple[float, Any]] = {}
        self._write_protection_duration: float = 10.0  # seconds to protect after write

        # User selections: store user's input for control entities (never read from device)
        # Key: entity_key, Value: user's selected value (e.g., "charge", "discharge", 1000)
        # Unlike write_protection, this has no expiration - it persists until user changes it or HA restarts
        self._user_selections: dict[str, Any] = {}

        # Use throttled logger to reduce log spam
        self._throttled_logger = ThrottledLogger(
            self.logger, default_interval=LOG_THROTTLE_INTERVAL
        )

        # Connection state tracking (initialize before reading device model)
        self._connection_failed = False
        self._last_connection_attempt = 0
        self._connection_retry_interval = CONNECTION_RETRY_DELAY
        self._consecutive_failures = 0
        self._device_unavailable_logged = (
            False  # HA best practice: log once on unavailable
        )

        # Device information - defer model detection until connection is established
        device_model = "--"
        self.device_info = {
            "identifiers": {(DOMAIN, entry.entry_id)},
            "name": self.device_name,
            "manufacturer": "Anker",
            "model": device_model,  # Read from device, just like manufacturer
        }

        # Start background loop: start immediately if HA is running, otherwise wait for HA startup completion
        def _start_bg(event=None):
            try:
                # Create task in thread-safe context to avoid creating coroutine objects in non-event loop threads
                def _spawn_task():
                    try:
                        if not self._bg_task or self._bg_task.done():
                            # Use resource manager to track background task
                            self._bg_task = self._resource_manager.create_task(
                                self._connection_loop(), name="connection_loop"
                            )
                    except Exception:
                        pass

                # Prefer thread-safe scheduling using the main event loop
                if (
                    hasattr(self.hass, "loop")
                    and self.hass.loop
                    and self.hass.loop.is_running()
                ):
                    try:
                        self.hass.loop.call_soon_threadsafe(_spawn_task)
                    except Exception:
                        _spawn_task()
                else:
                    # Fallback: try directly if loop is unavailable (usually in event loop thread)
                    _spawn_task()
            except Exception:
                pass

        try:
            if (
                getattr(self.hass, "is_running", False)
                or getattr(self.hass, "state", None) == CoreState.running
            ):
                _start_bg()
            else:
                self.hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STARTED, _start_bg)
        except Exception:
            # Fallback: start directly
            _start_bg()

    def is_connected(self) -> bool:
        """Public connection state for entities."""
        return self._status == "connected" and not self._connection_failed

    def set_write_protection(
        self, entity_key: str, protected_value: Any, duration: float | None = None
    ) -> None:
        """Protect a specific entity's value after write operations.

        This prevents UI "flash back" when device is still processing the command.
        Only the specified entity is protected; other data continues to update normally.

        Args:
            entity_key: The entity key to protect
            protected_value: The value to preserve during protection period
            duration: Protection duration in seconds. If None, uses default (10s).
        """
        if duration is None:
            duration = self._write_protection_duration
        protected_until = time.time() + duration
        self._protected_values[entity_key] = (protected_until, protected_value)
        self.logger.debug(
            "Write protection enabled for %s (value=%s) for %.1f seconds",
            entity_key,
            protected_value,
            duration,
        )

    def get_protected_value(self, entity_key: str) -> tuple[bool, Any]:
        """Get protected value for an entity if protection is active.

        Args:
            entity_key: The entity key to check

        Returns:
            tuple: (is_protected, protected_value)
                   If protected, returns (True, protected_value)
                   If not protected, returns (False, None)
        """
        if entity_key not in self._protected_values:
            return (False, None)

        protected_until, protected_value = self._protected_values[entity_key]
        if time.time() < protected_until:
            return (True, protected_value)

        # Protection expired, clean up
        del self._protected_values[entity_key]
        return (False, None)

    def clear_write_protection(self, entity_key: str) -> None:
        """Clear write protection for a specific entity."""
        if entity_key in self._protected_values:
            del self._protected_values[entity_key]
            self.logger.debug("Write protection cleared for %s", entity_key)

    def set_user_selection(self, entity_key: str, value: Any) -> None:
        """Store user's selection for control entities.

        This is used for control entities that never read from device,
        only remember user's last input (e.g., direction selector, power setpoint).

        Args:
            entity_key: The entity key
            value: User's selected value
        """
        self._user_selections[entity_key] = value
        self.logger.debug("User selection stored for %s: %s", entity_key, value)

    def get_user_selection(self, entity_key: str) -> Any | None:
        """Get user's selection for control entities.

        Args:
            entity_key: The entity key

        Returns:
            User's selected value, or None if not set
        """
        return self._user_selections.get(entity_key)

    def _override_model_with_product_name(self, data: dict[str, Any]) -> None:
        """Override model sensor value with user-friendly product name.


        Args:
            data: Data dictionary to modify in-place
        """
        try:
            if not self._full_config_cache or not isinstance(data, dict):
                self.logger.debug("Skip model override: no config or data")
                return

            product_info = self._full_config_cache.get("product_info", {})
            if not product_info:
                self.logger.debug("Skip model override: no product_info in config")
                return

            # Get SN register key from config (may vary by device)
            sn_register_key = product_info.get("sn_register_key")

            sn_clean = ""
            if sn_register_key:
                raw_sn = data.get(sn_register_key)
                if isinstance(raw_sn, str):
                    sn_clean = "".join(ch for ch in raw_sn.strip() if ch.isprintable())

            # Get product name from SN or use default
            if sn_clean:
                product_name = get_product_name_from_config(
                    sn=sn_clean,
                    device_config=self._full_config_cache,
                    fallback_name=None,
                )
                self.logger.debug(
                    "Product name from SN %s***: %s", sn_clean[:6], product_name
                )
            else:
                product_name = product_info.get("default_name", "Unknown Device")
                self.logger.debug("No SN, using default_name: %s", product_name)

            # Get model register key (which data point to override)
            model_register_key = product_info.get("model_register_key", "device_model")

            # Override the model sensor value with product name
            if model_register_key in data:
                old_value = data[model_register_key]
                if old_value != product_name:
                    data[model_register_key] = product_name
                    self.logger.info(
                        "📝 Overrode %s sensor: %s → %s",
                        model_register_key,
                        old_value,
                        product_name,
                    )

            # Update device_info for device registry
            if product_name and self.device_info.get("model") != product_name:
                self.device_info["model"] = product_name
                self.logger.debug("Updated device_info model: %s", product_name)

            # Update device name: "Product Name (SN末3位)" replaces generic "Anker Solix Device IP"
            if product_name:
                sn_suffix = sn_clean[-3:] if len(sn_clean) >= 3 else sn_clean
                if sn_suffix:
                    friendly_name = f"{product_name} ({sn_suffix})"
                else:
                    friendly_name = product_name
                if (
                    self.device_name != friendly_name
                    or self.entry.title != friendly_name
                ):
                    old_name = self.device_name
                    self.device_name = friendly_name
                    self.device_info["name"] = friendly_name
                    self.logger.info(
                        "Device name updated: %s → %s", old_name, friendly_name
                    )
                    # Also update config entry title (shown in Hubs list)
                    self.hass.config_entries.async_update_entry(
                        self.entry, title=friendly_name
                    )

        except Exception as e:
            self.logger.error(
                "Failed to override model with product name: %s", e, exc_info=True
            )

    async def _auto_set_mode_on_connect(self, data: dict[str, Any]) -> None:
        """Auto-set operating mode on first connect if configured in YAML.

        Writes the mode BEFORE data is published to HA, so UI shows
        the target mode from the first frame (zero flicker).
        """
        try:
            if not self._full_config_cache:
                return

            product_info = self._full_config_cache.get("product_info", {})
            auto_mode = product_info.get("auto_mode_on_connect")
            if auto_mode is None:
                return

            auto_mode = int(auto_mode)

            # Find operating_mode config to get register address
            write_quantities = self._full_config_cache.get("write_quantities", {})
            enum_selection = write_quantities.get("enumeration_selection", {})
            mode_config = enum_selection.get("operating_mode")
            if not mode_config:
                self.logger.debug("No operating_mode config found, skip auto-set")
                return

            address = int(mode_config.get("address"))
            data_type = mode_config.get("data_type", "UINT16")

            # Check current mode — skip if already in target mode
            current_mode = data.get("operating_mode")
            if current_mode is not None and int(current_mode) == auto_mode:
                self.logger.info(
                    "Device already in target mode %d, skip auto-set", auto_mode
                )
                return

            # Write target mode to device
            self.logger.info(
                "Auto-setting operating mode to %d on first connect (address=%d)",
                auto_mode,
                address,
            )

            async with self._io_lock:
                success = await self.modbus_manager.write_register(
                    address, auto_mode, data_type
                )

            if success:
                # Update data dict so UI first frame shows target mode
                data["operating_mode"] = auto_mode

                # Set write protection with translation key (not numeric value)
                # select entity's current_option returns translation keys, not numbers
                options = mode_config.get("options", {})
                translation_key = options.get(str(auto_mode), auto_mode)
                protection = mode_config.get("write_protection_duration", 15.0)
                self.set_write_protection("operating_mode", translation_key, protection)

                self.logger.info("Auto-set operating mode to %d succeeded", auto_mode)
            else:
                self.logger.warning("Auto-set operating mode to %d FAILED", auto_mode)

        except Exception as e:
            self.logger.error("Error in auto-set mode on connect: %s", e)

    def _should_attempt_reconnection(self) -> bool:
        """Check if reconnection should be attempted."""
        current_time = time.time()

        # If connection is normal, no need to reconnect
        if not self._connection_failed:
            return False

        # Check reconnection interval first (most important for quick recovery)
        time_since_last_attempt = current_time - self._last_connection_attempt
        if time_since_last_attempt < self._connection_retry_interval:
            return False

        return True

    def _handle_connection_failure(self, error_msg: str):
        """Handle connection failure with HA best-practice logging.

        HA Integration Quality Scale rule 'log-when-unavailable':
        Log once at INFO when device becomes unavailable, then stay silent.
        Log once at INFO when device comes back online.
        """
        current_time = time.time()
        self._consecutive_failures += 1
        self._last_failure_time = current_time
        self._connection_failed = True
        self._status = "disconnected"
        self._latest_data = {}

        # Immediately notify HA that data is no longer valid
        # This makes entities show "unavailable" instead of stale values
        self.async_set_updated_data({})

        # Force disconnect modbus connection to ensure clean reconnection
        try:
            self.modbus_manager.force_disconnect()
        except Exception as e:
            self.logger.debug("Error during force disconnect: %s", e)

        # HA best practice: log only ONCE when device becomes unavailable
        if not self._device_unavailable_logged:
            self.logger.info(
                "Device %s is unavailable: %s (will retry with backoff)",
                self.ip_address,
                error_msg,
            )
            self._device_unavailable_logged = True
        else:
            self.logger.debug(
                "Connection failure #%d: %s", self._consecutive_failures, error_msg
            )

        # Exponential backoff: 10s → 30s → 60s → 300s (max)
        if self._consecutive_failures <= 3:
            self._connection_retry_interval = CONNECTION_RETRY_DELAY  # 10s
        elif self._consecutive_failures <= 10:
            self._connection_retry_interval = 30
        elif self._consecutive_failures <= 30:
            self._connection_retry_interval = 60
        else:
            self._connection_retry_interval = 300

    async def _read_device_pn(self) -> tuple[str, str, str]:
        """Read device PN from register 0x8000 (32768) using unified method.

        Returns:
            tuple: (pn_hash, raw_pn, raw_registers_hex) or ("", "", "") on failure
        """
        try:
            async with self._io_lock:
                self.logger.debug("Attempting to read device PN")
                result = await self.modbus_manager.read_device_pn()
                pn_hash, raw_pn, raw_hex = result
                if pn_hash:
                    self.logger.info(
                        "Device PN read successfully - Raw PN: '%s', MD5: '%s', Registers: [%s]",
                        raw_pn,
                        pn_hash,
                        raw_hex,
                    )
                else:
                    self.logger.warning(
                        "Failed to read device PN - Raw: '%s', Registers: [%s]",
                        raw_pn,
                        raw_hex,
                    )
                return result
        except Exception as e:
            self.logger.error(
                "Exception reading device PN: %s (type: %s)", e, type(e).__name__
            )
            return ("", "", "")

    async def _get_config_file_path(self) -> str:
        """Get configuration file path based on device PN."""
        pn_hash, raw_pn, raw_hex = await self._read_device_pn()
        if not pn_hash:
            self.logger.error(
                "Cannot determine device PN, unable to load configuration"
            )
            return ""

        # Check if device-specific config exists
        config_file = f"config/{pn_hash}.yaml"
        from pathlib import Path

        config_path = Path(__file__).resolve().parent / config_file

        self.logger.debug(
            "Looking for config file - PN='%s', path='%s', exists=%s",
            pn_hash,
            config_path,
            config_path.exists(),
        )

        if config_path.exists():
            self.logger.info("Found device-specific config: %s", config_file)
            return config_file
        else:
            self.logger.error(
                "Device PN '%s' is not supported - Raw PN: '%s', Registers: [%s], "
                "config file %s not found at %s",
                pn_hash,
                raw_pn,
                raw_hex,
                config_file,
                config_path,
            )
            return ""

    def _log_data_update(
        self, phase: str, data: dict[str, Any], old_data: dict[str, Any] | None
    ) -> None:
        """Log data update details with consistent verbosity."""
        total = len(data) if data else 0
        phase_label = f"[bg] {phase}"

        if phase == "initial":
            self.logger.info("%s data fetch succeeded (%d points)", phase_label, total)
        else:
            self.logger.debug("%s data fetch succeeded (%d points)", phase_label, total)

        if not data:
            return

        sample_keys = list(data.keys())[:3]
        if sample_keys:
            sample_pairs = ", ".join(f"{key}={data.get(key)}" for key in sample_keys)
            suffix = ", ..." if total > len(sample_keys) else ""
            self.logger.debug("%s sample: %s%s", phase_label, sample_pairs, suffix)

        if not old_data:
            return

        changed = [
            f"{key}: {old_data.get(key)} -> {value}"
            for key, value in data.items()
            if old_data.get(key) != value
        ]
        if not changed:
            self.logger.debug("%s data unchanged from previous snapshot", phase_label)
            return

        summary = "; ".join(changed[:3])
        if len(changed) > 3:
            summary = f"{summary}; +{len(changed) - 3} more"

        self._throttled_logger.debug(
            "%s changes detected: %s",
            phase_label,
            summary,
            throttle_key=f"{phase}_changes",
        )

    async def _connection_loop(self) -> None:
        """Unified background loop: retry connect every 10s until a successful data read, then poll at scan interval."""
        while not self._stop_bg:
            self.logger.debug("[bg] Background loop iteration starting")
            try:
                if self._status != "connected":
                    # Retry every 10s until we can both connect and fetch data
                    if not self._ever_connected:
                        self.logger.info("[bg] Connection attempt starting")
                    else:
                        self.logger.debug("[bg] Reconnection attempt starting")
                    # Test connection using device-specific modbus manager
                    async with self._io_lock:
                        client = await self.modbus_manager.get_client()
                        connected = client is not None
                    if not connected:
                        self._handle_connection_failure(
                            "[bg] modbus_manager.get_client() failed"
                        )
                        await asyncio.sleep(self._connection_retry_interval)
                        continue

                    # Connected: ensure config
                    if not self._is_config_cache_valid():
                        # Dynamic configuration file based on device PN
                        await asyncio.sleep(0.7)
                        config_file = await self._get_config_file_path()
                        if not config_file:
                            self._handle_connection_failure(
                                "[bg] Failed to determine config file path"
                            )
                            await asyncio.sleep(self._connection_retry_interval)
                            continue
                        self._selected_config_file = config_file
                        # Load config file (no extra TCP used)
                        cfg = await self.device_config.load_device_config_by_file_async(
                            config_file
                        )
                        if cfg and isinstance(cfg, dict):
                            # Store full config (including product_info)
                            self._full_config_cache = cfg
                            # Build unified data_points from configuration using utility function
                            data_points, batch_ranges = parse_device_configuration(cfg)
                            if data_points:
                                self._device_config_cache = data_points
                                self._batch_ranges_cache = batch_ranges
                                self._config_cache_valid = True
                            else:
                                self._handle_connection_failure(
                                    "[bg] parsed config has no data_points"
                                )
                                await asyncio.sleep(self._connection_retry_interval)
                                continue
                        else:
                            self._handle_connection_failure(
                                "[bg] load device config file failed"
                            )
                            await asyncio.sleep(self._connection_retry_interval)
                            continue

                    # Try one data fetch to validate using the device-specific connection
                    self.logger.debug(
                        "[bg] Attempting initial data fetch with %d data points",
                        len(self._device_config_cache)
                        if self._device_config_cache
                        else 0,
                    )
                    async with self._io_lock:
                        data = await self.modbus_manager.get_all_data(
                            self._device_config_cache,
                            batch_ranges=self._batch_ranges_cache,
                            use_batch_optimization=True,
                        )
                    if not data:
                        # Treat as failure
                        self.logger.warning(
                            "[bg] initial data fetch returned empty data"
                        )
                        self._handle_connection_failure(
                            "[bg] initial data fetch returned empty"
                        )
                        await asyncio.sleep(self._connection_retry_interval)
                        continue

                    # Success: mark connected and push data
                    self._handle_connection_success()
                    self._status = "connected"

                    # IMPORTANT: Override model sensor value BEFORE publishing data
                    # This ensures sensor entities also display friendly product names
                    self._override_model_with_product_name(data)

                    # Auto-set operating mode on first connect (if configured)
                    if not self._ever_connected:
                        await self._auto_set_mode_on_connect(data)

                    # Log data comparison for debugging
                    old_data = self._latest_data.copy() if self._latest_data else None
                    self._latest_data = data
                    self._log_data_update("initial", data, old_data)

                    # Publish data to Home Assistant (data already has overridden model name)
                    self.async_set_updated_data(data)
                    self.logger.debug(
                        "[bg] Data published to Home Assistant via async_set_updated_data"
                    )

                    # Reset consecutive failures on successful reconnection
                    self._consecutive_failures = 0

                    # Update device registry (async, non-blocking)
                    try:
                        if (
                            self.device_info.get("model")
                            and self.device_info.get("model") != "--"
                        ):
                            dev_reg = dr.async_get(self.hass)
                            device = dev_reg.async_get_device(
                                identifiers={(DOMAIN, self.entry.entry_id)}
                            )
                            if device:
                                dev_reg.async_update_device(
                                    device_id=device.id,
                                    manufacturer=self.device_info.get(
                                        "manufacturer", "Anker"
                                    ),
                                    model=self.device_info.get("model"),
                                    name=self.device_info.get("name"),
                                )
                                self.logger.info(
                                    "Device registry updated with model: %s",
                                    self.device_info.get("model"),
                                )
                    except Exception as e:
                        self.logger.debug("Failed to update device registry: %s", e)
                    if not self._ever_connected:
                        self._ever_connected = True
                        self.logger.info(
                            "[bg] Connection successful, data fetched and published"
                        )
                    else:
                        self.logger.info(
                            "[bg] Reconnection successful, data fetched and published"
                        )

                # If connected, poll at scan interval
                self.logger.debug(
                    "[bg] Sleeping for %d seconds before next read", self.scan_interval
                )
                await asyncio.sleep(self.scan_interval)
                self.logger.debug("[bg] Sleep completed, checking if should read data")

                # Periodic read
                self.logger.debug(
                    "[bg] Status check: connected=%s, config_valid=%s",
                    self._status == "connected",
                    self._is_config_cache_valid(),
                )

                if self._status == "connected":
                    if not self._is_config_cache_valid():
                        self.logger.info(
                            "[bg] Config cache expired, reloading configuration"
                        )
                        # Reload configuration
                        config_file = await self._get_config_file_path()
                        if not config_file:
                            self._handle_connection_failure(
                                "[bg] Failed to determine config file path during reload"
                            )
                            await asyncio.sleep(self._connection_retry_interval)
                            continue
                        cfg = await self.device_config.load_device_config_by_file_async(
                            config_file
                        )
                        if cfg and isinstance(cfg, dict):
                            # Store full config (including product_info)
                            self._full_config_cache = cfg
                            # Build unified data_points from configuration using utility function
                            data_points, batch_ranges = parse_device_configuration(cfg)
                            if data_points:
                                self._device_config_cache = data_points
                                self._batch_ranges_cache = batch_ranges
                                self._config_cache_valid = True
                                self.logger.info(
                                    "[bg] Configuration reloaded successfully, %d data points",
                                    len(data_points),
                                )
                            else:
                                self.logger.error(
                                    "[bg] Failed to reload configuration - no data points"
                                )
                                await asyncio.sleep(self._connection_retry_interval)
                                continue
                        else:
                            self.logger.error(
                                "[bg] Failed to reload configuration - invalid config file"
                            )
                            await asyncio.sleep(self._connection_retry_interval)
                            continue

                    if self._is_config_cache_valid():
                        self.logger.debug("[bg] Starting periodic data read")
                        async with self._io_lock:
                            data = await self.modbus_manager.get_all_data(
                                self._device_config_cache,
                                batch_ranges=self._batch_ranges_cache,
                                use_batch_optimization=True,
                            )
                        if data:
                            # Override model sensor value with product name (MUST do before publishing)
                            self._override_model_with_product_name(data)

                            # Log data comparison for periodic reads
                            old_data = (
                                self._latest_data.copy() if self._latest_data else None
                            )
                            self._latest_data = data
                            self._log_data_update("periodic", data, old_data)

                            self.async_set_updated_data(data)
                            self.logger.debug(
                                "[bg] Periodic data published to Home Assistant"
                            )

                            # Reset failure count on successful read
                            if self._consecutive_failures > 0:
                                self._consecutive_failures = 0

                            self.logger.debug(
                                "[bg] Periodic read completed successfully, continuing loop"
                            )
                        else:
                            self.logger.debug(
                                "[bg] periodic data fetch failed (attempt %d)",
                                self._consecutive_failures + 1,
                            )
                            if self._consecutive_failures >= 2:
                                self._handle_connection_failure(
                                    "[bg] periodic data fetch failed after multiple attempts"
                                )
                            else:
                                self._consecutive_failures += 1
            except asyncio.CancelledError:
                break
            except Exception as e:
                self._handle_connection_failure(f"[bg] loop exception: {e}")
                await asyncio.sleep(self._connection_retry_interval)

    def _handle_connection_success(self):
        """Handle connection success."""
        if self._connection_failed:
            # HA best practice: log once when device comes back online
            self.logger.info(
                "Device %s is back online (was unavailable for %d retries)",
                self.ip_address,
                self._consecutive_failures,
            )
            self._connection_failed = False
            self._consecutive_failures = 0
            self._device_unavailable_logged = False  # Reset for next unavailability
            # Reset retry interval to fast recovery
            self._connection_retry_interval = CONNECTION_RETRY_DELAY
            # Reset connection attempt time to allow immediate retry if needed
            self._last_connection_attempt = 0
        else:
            self.logger.debug("Connection success - already in connected state")

    def _is_config_cache_valid(self) -> bool:
        """Check if configuration cache is valid."""
        return (
            self._config_cache_valid
            and self._device_config_cache is not None
            and self._batch_ranges_cache is not None
        )

    async def ensure_config_ready(self) -> dict[str, Any]:
        """Ensure device configuration is loaded synchronously for platform setup.
        Returns data_points dict if ready, otherwise empty dict.
        """
        if self._is_config_cache_valid():
            return self._device_config_cache  # type: ignore[return-value]

        # Attempt to connect using device-specific modbus manager, then load device-specific config file
        try:
            async with self._io_lock:
                client = await self.modbus_manager.get_client()
                connected = client is not None
            if not connected:
                return {}

            # small stabilization delay
            await asyncio.sleep(0.5)
            config_file = await self._get_config_file_path()
            if not config_file:
                return {}
            self._selected_config_file = config_file
            cfg = await self.device_config.load_device_config_by_file_async(config_file)
            if not (cfg and isinstance(cfg, dict)):
                return {}

            # Store full config (including product_info)
            self._full_config_cache = cfg
            # Build data_points using utility function
            data_points, batch_ranges = parse_device_configuration(cfg)
            if data_points:
                self._device_config_cache = data_points
                self._batch_ranges_cache = batch_ranges
                self._config_cache_valid = True
                return data_points
        except Exception:
            return {}
        return {}

    async def _get_device_config_with_cache(self) -> dict[str, Any]:
        """Return cached device configuration if available; background loop loads it."""
        if self._is_config_cache_valid():
            return self._device_config_cache
        return {}

    async def get_device_data_points(self) -> dict[str, Any]:
        """Public method: Get device data points configuration for other platforms to use."""
        return await self._get_device_config_with_cache()

    async def _async_update_data(self) -> dict[str, Any]:
        """Return latest known data; background loop manages IO and reconnection."""
        self.logger.debug(
            "_async_update_data called, returning %d data points",
            len(self._latest_data) if self._latest_data else 0,
        )
        return self._latest_data

    async def async_shutdown(self):
        """Shutdown coordinator."""
        # Disconnect device-specific modbus connection
        await self.modbus_manager.disconnect()
        # Stop background loop
        self._stop_bg = True
        # Use resource manager for proper cleanup
        await self._resource_manager.shutdown()
        await super().async_shutdown()
