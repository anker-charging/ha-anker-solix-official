"""Data coordinator."""

import asyncio
import logging
import time
from datetime import timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .config_utils import parse_device_configuration
from .const import (
    DOMAIN,
    EMPTY_FRAME_TOLERANCE,
    LOG_THROTTLE_INTERVAL,
    SCAN_INTERVAL,
)
from .device_config import AnkerSolixDeviceConfig
from .device_logger import DeviceLoggerAdapter
from .mdns_helper import find_device_ip_by_sn
from .modbus_manager import ModbusConnectionManager
from .product_mapping import get_product_name_from_config
from .throttled_logger import ThrottledLogger


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
        self.device_logger.info(
            "Coordinator initialized (scan interval: %ds)", self.scan_interval
        )

        # Device configuration cache
        self._device_config_cache = None
        self._batch_ranges_cache = None
        self._config_cache_valid = False
        self._full_config_cache = (
            None  # Store full YAML config (including product_info)
        )
        self._latest_data: dict[str, Any] = {}
        self._selected_config_file: str | None = None
        self._ever_connected: bool = False
        # Persistent flag: True once the initial auto-mode-set has been
        # successfully delivered (or device was already in target mode).
        # Stored in entry.options so it survives HA restarts.
        self._initial_mode_sent: bool = entry.options.get("initial_mode_sent", False)

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

        self._unavailable_registers: set[int] = set()

        # Consecutive failure count is kept only to drive the mDNS re-discovery
        # trigger; retry scheduling itself belongs to DataUpdateCoordinator.
        self._consecutive_failures = 0
        self._empty_frames = 0
        self._mdns_lookup_done = False
        self._last_mdns_lookup = 0

        sn = self._get_stored_sn()
        if sn:
            self._initial_mdns_sn = sn
        else:
            self._initial_mdns_sn = None

        # Device information - defer model detection until connection is established
        device_model = "--"
        self.device_info = {
            "identifiers": {(DOMAIN, entry.entry_id)},
            "name": self.device_name,
            "manufacturer": "Anker",
            "model": device_model,  # Read from device, just like manufacturer
        }

    def is_connected(self) -> bool:
        """Public connection state for entities.

        Derived from the outcome of the most recent refresh rather than a
        separately maintained flag, so a coordinator that has stopped polling
        cannot keep reporting itself as connected (issue #117).
        """
        return self.last_update_success

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

    def clear_user_selection(self, entity_key: str) -> None:
        """Clear user's selection after successful write.

        Called after Modbus write succeeds so device value takes over UI display.
        """
        self._user_selections.pop(entity_key, None)
        self.logger.debug("User selection cleared for %s", entity_key)

    def _override_model_with_product_name(self, data: dict[str, Any]) -> None:
        """Override model sensor value with user-friendly product name.

        This method extracts product code from SN and replaces the raw PN
        (e.g., "AE103") with friendly name (e.g., "Solarbank 4 E5000 Pro").

        Called from _async_update_data before the frame is returned, so the
        coordinator publishes the friendly name from the very first frame.

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
                    self.logger.debug(
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

    def _persist_initial_mode_sent(self) -> None:
        """Write initial_mode_sent=True to entry.options (idempotent).

        entry.options is used instead of entry.data: options changes do not
        trigger an entry reload, so entities stay available with no flicker.
        To reset (re-trigger auto-set), remove and re-add the integration.
        """
        if self._initial_mode_sent:
            return
        self._initial_mode_sent = True
        self.hass.config_entries.async_update_entry(
            self.entry,
            options={**self.entry.options, "initial_mode_sent": True},
        )
        self.logger.info(
            "initial_mode_sent persisted — auto mode-set will not repeat on future HA restarts"
        )

    @staticmethod
    def _normalize_version(version_str: str) -> str:
        """Strip leading 'v' or 'V' prefix from version string.

        Device hardware_version register stores strings like 'v0.0.5.5';
        this normalizes to '0.0.5.5' before numeric comparison.
        """
        return version_str.strip().lstrip("vV")

    @staticmethod
    def _compare_version(version_str: str, threshold_str: str) -> int:
        """Compare two version strings in X.X.X.X format.

        Each segment is compared as an integer so that
        0.0.5.5 > 0.0.5.4, 0.0.5.5 > 0.0.4.50, 0.0.5.5 < 0.0.6.1.
        Input strings must NOT contain a leading 'v' prefix.

        Returns:
            -1  if version_str <  threshold_str
             0  if version_str == threshold_str
             1  if version_str >  threshold_str
        """
        def _to_tuple(v: str) -> tuple[int, ...]:
            try:
                return tuple(int(x) for x in v.split("."))
            except (ValueError, AttributeError):
                return (0,)

        v = _to_tuple(version_str)
        t = _to_tuple(threshold_str)
        max_len = max(len(v), len(t))
        v = v + (0,) * (max_len - len(v))
        t = t + (0,) * (max_len - len(t))
        if v < t:
            return -1
        if v > t:
            return 1
        return 0

    def _inject_version_gates(self, data: dict[str, Any]) -> None:
        """Inject version gate visibility fields for all entities with version_gate config.

        Supports both single gate (dict) and multiple gates (list) formats:
        - dict: version_gate: {entity: "hardware_version", min_version: "0.0.0.1"}
        - list: version_gate: [{entity: "hardware_version", min_version: "0.0.0.1"},
                                {entity: "firmware_version", min_version: "0.0.7.0"}]

        For multiple gates, ALL must pass (AND logic) for the entity to be visible.
        Injects {entity_key}_visible = 1 only when every gate passes, else 0.
        """
        try:
            if not self._device_config_cache or not isinstance(data, dict):
                return

            for entity_key, config in self._device_config_cache.items():
                version_gate = config.get("version_gate")
                if not version_gate:
                    continue

                if isinstance(version_gate, dict):
                    gates = [version_gate]
                elif isinstance(version_gate, list):
                    gates = version_gate
                else:
                    continue

                visible_key = f"{entity_key}_visible"
                all_visible = True

                for gate in gates:
                    gate_entity = gate.get("entity")
                    min_version = gate.get("min_version")
                    if not gate_entity or not min_version:
                        all_visible = False
                        break

                    version_raw = data.get(gate_entity, "")
                    if not isinstance(version_raw, str) or not version_raw.strip():
                        self.logger.debug(
                            "%s empty, %s gate failed", gate_entity, entity_key
                        )
                        all_visible = False
                        break

                    version = self._normalize_version(version_raw)
                    threshold = self._normalize_version(str(min_version))
                    result = self._compare_version(version, threshold)
                    if result < 0:
                        all_visible = False
                        self.logger.debug(
                            "%s=%s < threshold=%s, %s gate failed",
                            gate_entity, version, threshold, entity_key,
                        )
                        break

                    self.logger.debug(
                        "%s=%s (raw=%s) >= threshold=%s, gate passed",
                        gate_entity, version, version_raw.strip(), threshold,
                    )

                data[visible_key] = 1 if all_visible else 0
                self.logger.debug(
                    "%s=%d (evaluated %d gate(s))",
                    visible_key, data[visible_key], len(gates),
                )
        except Exception as e:
            self.logger.error(
                "Failed to inject version gates: %s", e, exc_info=True
            )

    def is_register_available(self, address: int) -> bool:
        """Check if a register is available (not in unavailable set)."""
        return address not in self._unavailable_registers

    def get_data_point_address(self, entity_key: str) -> int | None:
        """Get the Modbus address for a data point by entity key."""
        if not self._device_config_cache:
            return None
        config = self._device_config_cache.get(entity_key)
        if not config:
            return None
        return config.get("address")

    async def _update_unavailable_registers(self) -> None:
        try:
            client = await self.modbus_manager.get_client()
            if not client or not hasattr(client, 'get_last_failed_registers'):
                return

            failed = client.get_last_failed_registers()
            successful = client.get_last_successful_registers()

            new_failures = failed - self._unavailable_registers
            if new_failures:
                self._unavailable_registers.update(new_failures)
                self.logger.info(
                    "Marked %d registers as unavailable: %s",
                    len(new_failures),
                    ", ".join(f"{a} (0x{a:04X})" for a in sorted(new_failures)),
                )

            recovered = successful & self._unavailable_registers
            if recovered:
                self._unavailable_registers -= recovered
                self.logger.info(
                    "Recovered %d registers, now available: %s",
                    len(recovered),
                    ", ".join(f"{a} (0x{a:04X})" for a in sorted(recovered)),
                )
        except Exception as e:
            self.logger.debug("Failed to update unavailable registers: %s", e)

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

            # Check current mode — skip write if already in target mode
            current_mode = data.get("operating_mode")
            if current_mode is not None and int(current_mode) == auto_mode:
                self.logger.info(
                    "Device already in target mode %d, skip write", auto_mode
                )
                self._persist_initial_mode_sent()
                return

            # Write target mode to device
            self.logger.info(
                "Auto-setting operating mode to %d on first connect (address=%d)",
                auto_mode,
                address,
            )

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
                self._persist_initial_mode_sent()
            else:
                self.logger.warning(
                    "Auto-set operating mode to %d FAILED — will retry on next HA restart",
                    auto_mode,
                )

        except Exception as e:
            self.logger.error("Error in auto-set mode on connect: %s", e)

    def _get_stored_sn(self) -> str:
        """Get device SN from config entry unique_id.

        During config flow, SN is read via Modbus and stored as unique_id.
        If SN read failed during setup, unique_id falls back to IP address.
        """
        unique_id = self.entry.unique_id or ""
        if unique_id and not self._validate_ipv4(unique_id) and len(unique_id) >= 10:
            return unique_id
        return ""

    @staticmethod
    def _validate_ipv4(ip: str) -> bool:
        parts = ip.split(".")
        if len(parts) != 4:
            return False
        try:
            return all(0 <= int(p) <= 255 for p in parts)
        except ValueError:
            return False

    async def _apply_mdns_ip_update(self, new_ip: str) -> None:
        """Update in-memory connection target after mDNS resolves a new IP.

        Not persisted to entry.data: the startup mDNS check re-discovers the
        current IP on every setup, so persistence is unnecessary, and writing
        entry.data here would needlessly mutate the config entry mid-run.
        """
        self.logger.info(
            "mDNS: IP changed %s → %s, updating connection target",
            self.ip_address,
            new_ip,
        )
        self.ip_address = new_ip
        self.device_logger = DeviceLoggerAdapter(
            self.device_logger.logger,
            device_name=self.device_name,
            device_ip=new_ip,
            device_port=self.port,
        )
        await self.modbus_manager.update_ip_address(new_ip)

    async def _async_startup_mdns_check(self) -> None:
        """Resolve the device's current IP via mDNS before the first connect.

        Awaited from the connect step so a device whose DHCP lease moved is found on
        the very first refresh. Only applies the discovered IP when the configured
        one has not already worked (self._ever_connected), so a currently-working
        IP is left untouched.
        """
        if not self._initial_mdns_sn:
            return
        new_ip = await find_device_ip_by_sn(self.hass, self._initial_mdns_sn, timeout=5)
        if not new_ip or new_ip == self.ip_address or self._ever_connected:
            return
        await self._apply_mdns_ip_update(new_ip)

    async def _maybe_mdns_lookup(self) -> None:
        """Attempt mDNS lookup to find device's new IP after refresh failures.

        Triggers when:
        - 3 consecutive failures (first time)
        - Every 55s after that (rate limit)

        Does NOT trigger if:
        - unique_id is not a SN (can't search without SN)
        - Last mDNS lookup was < 55s ago (rate limit)
        """
        if self._consecutive_failures < 3:
            return

        now = time.time()
        if now - self._last_mdns_lookup < 55:
            return

        self._last_mdns_lookup = now

        sn = self._get_stored_sn()
        if not sn:
            return

        new_ip = await find_device_ip_by_sn(self.hass, sn)

        if new_ip is None:
            return

        if new_ip == self.ip_address:
            return

        await self._apply_mdns_ip_update(new_ip)

        self._consecutive_failures = 0

    async def _handle_refresh_failure(self, error_msg: str) -> None:
        """React to a failed refresh: drop the socket and consider mDNS re-discovery.

        Deliberately does NOT schedule retries or mark entities unavailable --
        DataUpdateCoordinator owns retry timing and `last_update_success`. It
        does clear `self.data` directly (not via async_set_updated_data, which
        would reset last_update_success back to True): DataUpdateCoordinator
        leaves `self.data` at its last successful value when `_async_update_data`
        raises, so anything reading coordinator.data without checking
        last_update_success first -- e.g. write-condition gates in
        base_entity.py -- would otherwise see stale pre-outage values.
        """
        self._consecutive_failures += 1
        self._latest_data = {}
        self.data = {}

        try:
            await self.modbus_manager.force_disconnect()
        except Exception as e:
            self.logger.debug("Error during force disconnect: %s", e)

        self.logger.debug(
            "Refresh failure #%d: %s", self._consecutive_failures, error_msg
        )

        await self._maybe_mdns_lookup()

    async def _read_device_pn(self) -> tuple[str, str, str]:
        """Read device PN from register 0x8000 (32768) using unified method.

        Returns:
            tuple: (pn_hash, raw_pn, raw_registers_hex) or ("", "", "") on failure
        """
        try:
            self.logger.debug("Attempting to read device PN")
            result = await self.modbus_manager.read_device_pn()
            pn_hash, _, raw_hex = result
            if pn_hash:
                self.logger.info(
                    "Device PN read successfully - hash: '%s', Registers: [%s]",
                    pn_hash,
                    raw_hex,
                )
            else:
                self.logger.warning(
                    "Failed to read device PN - Registers: [%s]",
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
        pn_hash, _, raw_hex = await self._read_device_pn()
        if not pn_hash:
            self.logger.error(
                "Cannot determine device PN, unable to load configuration"
            )
            return ""

        # Check if device-specific config exists
        config_file = f"config/{pn_hash}.yaml"
        import asyncio
        from pathlib import Path

        config_path = Path(__file__).resolve().parent / config_file

        loop = asyncio.get_event_loop()
        path_exists = await loop.run_in_executor(None, config_path.exists)

        self.logger.debug(
            "Looking for config file - PN='%s', path='%s', exists=%s",
            pn_hash,
            config_path,
            path_exists,
        )

        if path_exists:
            self.logger.info("Found device-specific config: %s", config_file)
            return config_file
        else:
            self.logger.error(
                "Device PN hash '%s' is not supported - Registers: [%s], "
                "config file %s not found at %s",
                pn_hash,
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

        if phase == "initial":
            self.logger.info("%s data fetch succeeded (%d points)", phase, total)
        else:
            self.logger.debug("%s data fetch succeeded (%d points)", phase, total)

        if not data:
            return

        sample_keys = list(data.keys())[:3]
        if sample_keys:
            sample_pairs = ", ".join(f"{key}={data.get(key)}" for key in sample_keys)
            suffix = ", ..." if total > len(sample_keys) else ""
            self.logger.debug("%s sample: %s%s", phase, sample_pairs, suffix)

        if not old_data:
            return

        changed = [
            f"{key}: {old_data.get(key)} -> {value}"
            for key, value in data.items()
            if old_data.get(key) != value
        ]
        if not changed:
            self.logger.debug("%s data unchanged from previous snapshot", phase)
            return

        summary = "; ".join(changed[:3])
        if len(changed) > 3:
            summary = f"{summary}; +{len(changed) - 3} more"

        self._throttled_logger.debug(
            "%s changes detected: %s",
            phase,
            summary,
            throttle_key=f"{phase}_changes",
        )

    def _update_device_registry_info(self) -> None:
        """Push the current detected model/manufacturer/name into the device registry.

        Uses `async_get_device_by_identifier` when available (HA core >= 2026.8.0),
        falling back to the deprecated `async_get_device` on older HA versions where
        the new API does not exist yet. `async_get_device` is scheduled for removal
        in HA 2027.8.0 (see home-assistant/core deprecation notice), but remains
        functional (warning-only for custom integrations) on every HA version this
        integration currently supports (min HA version per hacs.json: 2024.1.6).
        """
        dev_reg = dr.async_get(self.hass)
        if hasattr(dev_reg, "async_get_device_by_identifier"):
            device = dev_reg.async_get_device_by_identifier(
                (DOMAIN, self.entry.entry_id), self.entry.entry_id
            )
        else:
            device = dev_reg.async_get_device(
                identifiers={(DOMAIN, self.entry.entry_id)}
            )
        if device:
            dev_reg.async_update_device(
                device_id=device.id,
                manufacturer=self.device_info.get("manufacturer", "Anker"),
                model=self.device_info.get("model"),
                name=self.device_info.get("name"),
            )
            self.logger.info(
                "Device registry updated with model: %s",
                self.device_info.get("model"),
            )

    async def _load_device_configuration(self) -> dict[str, Any]:
        """Resolve the device's PN, load its YAML config and parse it into caches.

        Returns the parsed data_points, or an empty dict when the device could
        not be identified or its config file is missing/unusable.
        """
        config_file = await self._get_config_file_path()
        if not config_file:
            return {}

        self._selected_config_file = config_file
        cfg = await self.device_config.load_device_config_by_file_async(config_file)
        if not (cfg and isinstance(cfg, dict)):
            return {}

        data_points, batch_ranges = parse_device_configuration(cfg)
        if not data_points:
            return {}

        self._full_config_cache = cfg
        self._device_config_cache = data_points
        self._batch_ranges_cache = batch_ranges
        self._config_cache_valid = True
        return data_points

    async def _async_connect_and_ensure_config(self) -> None:
        """Connect and make sure the device configuration is loaded.

        Called from _async_update_data rather than from DataUpdateCoordinator's
        `_async_setup` hook: that hook only exists from HA core 2024.8.0, while
        hacs.json still declares 2024.1.6 as the minimum supported version, so
        relying on it would silently skip this step on older installs.
        Loading is guarded by the config cache, so the work really happens once.
        """
        client = await self.modbus_manager.get_client()
        if client is None and self._initial_mdns_sn and not self._ever_connected:
            # Only scan once the configured IP has actually failed: a working IP
            # must never be delayed by an unconditional mDNS lookup.
            await self._async_startup_mdns_check()
            client = await self.modbus_manager.get_client()
        if client is None:
            raise UpdateFailed(f"Cannot connect to device at {self.ip_address}")

        if self._is_config_cache_valid():
            return

        # The device needs a moment after connect before it answers reliably.
        await asyncio.sleep(0.7)

        if not await self._load_device_configuration():
            raise UpdateFailed(
                f"Could not load a device configuration for {self.ip_address}"
            )

    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch one full frame of device data.

        Raises UpdateFailed on any read problem; DataUpdateCoordinator translates
        that into last_update_success=False and schedules the next attempt. On the
        first call (via async_config_entry_first_refresh) that becomes
        ConfigEntryNotReady, so HA owns the retry schedule.
        """
        try:
            await self._async_connect_and_ensure_config()

            data = await self.modbus_manager.get_all_data(
                self._device_config_cache,
                batch_ranges=self._batch_ranges_cache,
            )
            await self._update_unavailable_registers()

            if not data:
                # Modbus TCP drops the occasional frame. Failing on the first one
                # would flip every entity to unavailable and back within one scan
                # interval, so a few misses are ridden out on the last good frame
                # (the pre-refactor loop tolerated the same number). Still counted
                # against _consecutive_failures so mDNS re-discovery (gated on 3
                # consecutive problem reads) triggers at the same point as before,
                # rather than only once the tolerance window on top is exhausted.
                if self._latest_data and self._empty_frames < EMPTY_FRAME_TOLERANCE:
                    self._empty_frames += 1
                    self._consecutive_failures += 1
                    self.logger.debug(
                        "Empty data frame from %s (%d/%d tolerated), keeping last values",
                        self.ip_address,
                        self._empty_frames,
                        EMPTY_FRAME_TOLERANCE,
                    )
                    return self._latest_data
                raise UpdateFailed(f"Empty data frame from {self.ip_address}")
        except UpdateFailed as err:
            await self._handle_refresh_failure(str(err))
            raise
        except Exception as err:
            await self._handle_refresh_failure(str(err))
            raise UpdateFailed(f"Error reading from {self.ip_address}: {err}") from err

        self._empty_frames = 0

        # Model name must be overridden before the frame is published so sensors
        # and the device registry show the friendly product name from frame one.
        self._override_model_with_product_name(data)
        self._inject_version_gates(data)

        if not self._initial_mode_sent:
            await self._auto_set_mode_on_connect(data)

        old_data = self._latest_data.copy() if self._latest_data else None
        self._latest_data = data
        self._log_data_update("periodic" if self._ever_connected else "initial", data, old_data)

        if self._consecutive_failures:
            self.logger.info(
                "Device %s is back online (was unavailable for %d attempts)",
                self.ip_address,
                self._consecutive_failures,
            )
            self._consecutive_failures = 0

        try:
            if self.device_info.get("model") and self.device_info.get("model") != "--":
                self._update_device_registry_info()
        except Exception as e:
            self.logger.debug("Failed to update device registry: %s", e)

        self._ever_connected = True
        return data

    def _is_config_cache_valid(self) -> bool:
        """Check if configuration cache is valid."""
        return (
            self._config_cache_valid
            and self._device_config_cache is not None
            and self._batch_ranges_cache is not None
        )

    async def get_device_data_points(self) -> dict[str, Any]:
        """Public method: Get device data points configuration for other platforms to use."""
        if self._is_config_cache_valid():
            return self._device_config_cache
        return {}

    async def async_shutdown(self):
        """Shutdown coordinator.

        super().async_shutdown() first: it sets _shutdown_requested, which stops
        any further refresh from starting, so the disconnect below cannot end up
        competing with an in-flight read for the manager's I/O lock (issue #117).
        """
        await super().async_shutdown()
        await self.modbus_manager.disconnect()
