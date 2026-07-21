"""Simplified Anker Solix configuration flow for testing."""

from __future__ import annotations

import logging
import re
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowResult

from .const import (
    DOMAIN,
    ERROR_INVALID_IP,
    ERROR_CANNOT_CONNECT,
    ERROR_CONNECTION_TIMEOUT,
    ERROR_DEVICE_NOT_SUPPORTED,
)
from .device_config import AnkerSolixDeviceConfig
from .modbus_client import AnkerSolixModbusClient

_LOGGER = logging.getLogger(__name__)


class AnkerSolixOfficialConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle Anker Solix device configuration flow."""

    VERSION = 1

    def _validate_ipv4(self, ip_address: str) -> bool:
        """Validate IPv4 address using regex."""
        # IPv4 regex pattern: 0-255.0-255.0-255.0-255
        ipv4_pattern = r"^((25[0-5]|2[0-4][0-9]|[01]?[0-9][0-9]?)\.){3}(25[0-5]|2[0-4][0-9]|[01]?[0-9][0-9]?)$"
        return bool(re.match(ipv4_pattern, ip_address.strip()))

    async def _test_modbus_connection(self, ip_address: str, port: int = 502) -> bool:
        """Test Modbus connection to the device."""
        import asyncio

        loop = asyncio.get_event_loop()
        client = None
        try:
            client = AnkerSolixModbusClient(ip_address, port)
            return await loop.run_in_executor(None, client.connect)
        except Exception as e:
            _LOGGER.warning(
                "Failed to test Modbus connection to %s:%d: %s", ip_address, port, e
            )
            return False
        finally:
            try:
                if client:
                    await loop.run_in_executor(None, client.disconnect)
            except Exception as e:
                _LOGGER.debug("Error disconnecting during connection test: %s", e)

    async def _check_device_support(self, ip_address: str, port: int = 502) -> tuple[bool, str]:
        import asyncio

        loop = asyncio.get_event_loop()
        client = None
        try:
            client = AnkerSolixModbusClient(ip_address, port)

            # Run blocking connect() in executor to avoid blocking event loop
            connected = await loop.run_in_executor(None, client.connect)
            if not connected:
                _LOGGER.warning(
                    "Failed to connect to device at %s:%d for PN detection",
                    ip_address,
                    port,
                )
                return False, ""

            # Read device PN using unified method (returns tuple: pn_hash, raw_pn, raw_hex)
            result = await loop.run_in_executor(None, client.read_device_pn)
            pn_hash, raw_pn, raw_hex = result
            if not pn_hash:
                _LOGGER.warning(
                    "Failed to read device PN from %s:%d - Registers: [%s]",
                    ip_address,
                    port,
                    raw_hex,
                )
                return False, ""

            _LOGGER.info(
                "Device PN read at %s:%d - hash: '%s', Registers: [%s]",
                ip_address,
                port,
                pn_hash,
                raw_hex,
            )

            from pathlib import Path
            import yaml as _yaml

            config_file = f"config/{pn_hash}.yaml"
            config_path = Path(__file__).resolve().parent / config_file

            def _check_and_load_config():
                if not config_path.exists():
                    return None
                with open(config_path, encoding="utf-8") as f:
                    return _yaml.safe_load(f)

            device_cfg = await loop.run_in_executor(None, _check_and_load_config)

            if device_cfg is None:
                _LOGGER.warning(
                    "Device PN hash '%s' detected at %s:%d, but config file not found at %s. "
                    "Registers: [%s]",
                    pn_hash,
                    ip_address,
                    port,
                    config_path,
                    raw_hex,
                )
                return False, ""

            _LOGGER.info(
                "Device PN hash '%s' detected at %s:%d, config file found",
                pn_hash,
                ip_address,
                port,
            )

            sn = ""
            try:
                sn_key = device_cfg.get("product_info", {}).get("sn_register_key")
                if sn_key:
                    sn_cfg = device_cfg.get("read_quantities", {}).get(sn_key, {})
                    sn_address = sn_cfg.get("address")
                    sn_count = sn_cfg.get("count", 12)
                    if sn_address:
                        sn_result = await loop.run_in_executor(
                            None,
                            lambda: client.client.read_input_registers(
                                address=sn_address, count=sn_count
                            ),
                        )
                        if sn_result and not sn_result.isError():
                            regs = getattr(sn_result, "registers", []) or []
                            raw = b""
                            for r in regs:
                                raw += bytes([(r >> 8) & 0xFF, r & 0xFF])
                            sn = raw.decode("utf-8", errors="ignore").rstrip("\x00").strip()
                            _LOGGER.info(
                                "Device SN read at %s:%d: '%s'",
                                ip_address,
                                port,
                                sn[:6] + "***" if len(sn) > 6 else "***",
                            )
                        else:
                            _LOGGER.info(
                                "Failed to read SN from %s:%d, will use IP as unique_id",
                                ip_address,
                                port,
                            )
            except Exception as e:
                _LOGGER.info(
                    "Exception reading SN from %s:%d: %s, will use IP as unique_id",
                    ip_address,
                    port,
                    e,
                )
                sn = ""

            return True, sn

        except Exception as e:
            _LOGGER.error(
                "Exception while checking device support at %s:%d: %s",
                ip_address,
                port,
                e,
            )
            return False, ""
        finally:
            try:
                if client:
                    await loop.run_in_executor(None, client.disconnect)
            except Exception as e:
                _LOGGER.debug("Error disconnecting during device check: %s", e)

    async def async_step_user(self, user_input=None) -> FlowResult:
        """Handle initial step."""
        errors = {}

        if user_input is not None:
            ip_address = user_input.get("ip_address", "").strip()

            # Validate IP address format
            if not ip_address:
                errors["base"] = ERROR_INVALID_IP
            elif not self._validate_ipv4(ip_address):
                errors["base"] = ERROR_INVALID_IP
            else:
                # Test Modbus connection before creating entry
                if not await self._test_modbus_connection(ip_address):
                    errors["base"] = ERROR_CANNOT_CONNECT
                else:
                    supported, sn = await self._check_device_support(ip_address)
                    if not supported:
                        errors["base"] = ERROR_DEVICE_NOT_SUPPORTED
                    else:
                        unique_id = sn if sn else ip_address
                        await self.async_set_unique_id(unique_id)
                        for entry in self._async_current_entries():
                            if entry.unique_id != unique_id:
                                continue
                            if entry.disabled_by is not None:
                                # Re-enable disabled entries instead of aborting silently.
                                self.hass.config_entries.async_update_entry(
                                    entry,
                                    data={**entry.data, "ip_address": ip_address},
                                )
                                await self.hass.config_entries.async_set_disabled_by(
                                    entry.entry_id, None
                                )
                                return self.async_abort(reason="reconfigure_successful")
                            return self.async_abort(reason="already_configured")
                        self._abort_if_unique_id_configured(updates={"ip_address": ip_address})
                        return self.async_create_entry(
                            title=f"Anker Solix {ip_address}",
                            data={
                                "ip_address": ip_address,
                                "port": 502,
                                "device_name": f"Anker Solix Device {ip_address}",
                            },
                        )

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required("ip_address"): str,
                }
            ),
            errors=errors,
        )

    async def async_step_import(self, import_data=None) -> FlowResult:
        """Handle import from configuration.yaml."""
        ip_address = import_data.get("ip_address", "").strip()

        # Validate IP address format
        if not ip_address or not self._validate_ipv4(ip_address):
            return self.async_abort(reason="invalid_ip")

        # Test Modbus connection and device support before creating entry
        port = import_data.get("port", 502)
        if not await self._test_modbus_connection(ip_address, port):
            return self.async_abort(reason="cannot_connect")

        supported, sn = await self._check_device_support(ip_address, port)
        if not supported:
            return self.async_abort(reason="device_not_supported")

        unique_id = sn if sn else ip_address
        await self.async_set_unique_id(unique_id)
        for entry in self._async_current_entries():
            if entry.unique_id != unique_id:
                continue
            if entry.disabled_by is not None:
                # Re-enable disabled entries instead of aborting silently.
                self.hass.config_entries.async_update_entry(
                    entry, data={**entry.data, "ip_address": ip_address}
                )
                await self.hass.config_entries.async_set_disabled_by(
                    entry.entry_id, None
                )
                return self.async_abort(reason="reconfigure_successful")
            return self.async_abort(reason="already_configured")
        self._abort_if_unique_id_configured(updates={"ip_address": ip_address})

        return self.async_create_entry(
            title=f"Anker Solix {ip_address}",
            data={
                "ip_address": ip_address,
                "port": port,
                "device_name": import_data.get(
                    "device_name", f"Anker Solix Device {ip_address}"
                ),
            },
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle reconfiguration."""
        errors: dict[str, str] = {}
        reconfigure_entry = self._get_reconfigure_entry()

        if user_input is not None:
            new_ip = user_input.get("ip_address", "").strip()
            new_port = user_input.get("port", 502)

            if not new_ip or not self._validate_ipv4(new_ip):
                errors["base"] = ERROR_INVALID_IP
            else:
                if not await self._test_modbus_connection(new_ip, new_port):
                    errors["base"] = ERROR_CANNOT_CONNECT
                else:
                    supported, sn = await self._check_device_support(new_ip, new_port)
                    if not supported:
                        errors["base"] = ERROR_DEVICE_NOT_SUPPORTED
                    else:
                        new_data = {
                            **reconfigure_entry.data,
                            "ip_address": new_ip,
                            "port": new_port,
                        }
                        self.hass.config_entries.async_update_entry(
                            reconfigure_entry, data=new_data
                        )
                        self.hass.config_entries.async_schedule_reload(
                            reconfigure_entry.entry_id
                        )
                        return self.async_abort(reason="reconfigure_successful")

        return self.async_show_form(
            step_id="reconfigure",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        "ip_address",
                        default=reconfigure_entry.data.get("ip_address", ""),
                    ): str,
                    vol.Required(
                        "port",
                        default=reconfigure_entry.data.get("port", 502),
                    ): int,
                }
            ),
            errors=errors,
            description_placeholders={
                "device_name": reconfigure_entry.data.get(
                    "device_name", "Anker Solix Device"
                ),
            },
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        """Get options flow."""
        return AnkerSolixOfficialOptionsFlowHandler(config_entry)


class AnkerSolixOfficialOptionsFlowHandler(config_entries.OptionsFlow):
    """Handle Anker Solix options flow."""

    def __init__(self, config_entry):
        """Initialize options flow."""
        # Do not keep a direct reference to the config_entry (deprecated)
        # Store only the entry_id and fetch the entry from hass when needed
        self._entry_id = config_entry.entry_id

    async def async_step_init(self, user_input=None):
        """Manage Anker Solix device options."""
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        # Fetch current entry to populate defaults
        entry = self.hass.config_entries.async_get_entry(self._entry_id)
        default_name = "Anker Solix Device"
        if entry and entry.data:
            default_name = entry.data.get("device_name", default_name)

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Optional("device_name", default=default_name): str,
                }
            ),
        )
