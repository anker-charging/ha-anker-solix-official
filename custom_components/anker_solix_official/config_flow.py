"""Simplified Anker Solix configuration flow for testing."""

from __future__ import annotations

import logging
import re
import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowResult

from .const import DOMAIN, ERROR_INVALID_IP, ERROR_CANNOT_CONNECT, ERROR_CONNECTION_TIMEOUT, ERROR_DEVICE_NOT_SUPPORTED
from .device_config import AnkerSolixDeviceConfig
from .modbus_client import AnkerSolixModbusClient

_LOGGER = logging.getLogger(__name__)


class AnkerSolixOfficialConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle Anker Solix device configuration flow."""

    VERSION = 1

    def _validate_ipv4(self, ip_address: str) -> bool:
        """Validate IPv4 address using regex."""
        # IPv4 regex pattern: 0-255.0-255.0-255.0-255
        ipv4_pattern = r'^((25[0-5]|2[0-4][0-9]|[01]?[0-9][0-9]?)\.){3}(25[0-5]|2[0-4][0-9]|[01]?[0-9][0-9]?)$'
        return bool(re.match(ipv4_pattern, ip_address.strip()))

    async def _test_modbus_connection(self, ip_address: str, port: int = 502) -> bool:
        """Test Modbus connection to the device."""
        import asyncio
        client = None
        try:
            client = AnkerSolixModbusClient(ip_address, port)
            # Run blocking connect() in executor to avoid blocking event loop
            loop = asyncio.get_event_loop()
            return await loop.run_in_executor(None, client.connect)
        except Exception as e:
            _LOGGER.warning("Failed to test Modbus connection to %s:%d: %s", ip_address, port, e)
            return False
        finally:
            # Ensure client is properly closed
            try:
                if client:
                    client.disconnect()
            except Exception as e:
                _LOGGER.debug("Error disconnecting during connection test: %s", e)

    async def _check_device_support(self, ip_address: str, port: int = 502) -> bool:
        """Check if device is supported by reading PN and verifying config file exists."""
        import asyncio
        client = None
        try:
            client = AnkerSolixModbusClient(ip_address, port)

            # Run blocking connect() in executor to avoid blocking event loop
            loop = asyncio.get_event_loop()
            connected = await loop.run_in_executor(None, client.connect)
            if not connected:
                _LOGGER.warning("Failed to connect to device at %s:%d for PN detection", ip_address, port)
                return False

            # Read device PN using unified method (returns tuple: pn_hash, raw_pn, raw_hex)
            result = await loop.run_in_executor(None, client.read_device_pn)
            pn_hash, raw_pn, raw_hex = result
            if not pn_hash:
                _LOGGER.warning("Failed to read device PN from %s:%d - Raw: '%s', Registers: [%s]",
                              ip_address, port, raw_pn, raw_hex)
                return False

            _LOGGER.info("Device PN read at %s:%d - Raw PN: '%s', MD5: '%s', Registers: [%s]",
                        ip_address, port, raw_pn, pn_hash, raw_hex)

            # Check if configuration file exists (file I/O is quick, no need for executor)
            from pathlib import Path
            config_file = f"config/{pn_hash}.yaml"
            config_path = Path(__file__).resolve().parent / config_file

            if config_path.exists():
                _LOGGER.info("Device PN '%s' (Raw: '%s') detected at %s:%d, config file found",
                            pn_hash, raw_pn, ip_address, port)
                return True
            else:
                _LOGGER.warning(
                    "Device PN '%s' (Raw: '%s') detected at %s:%d, but config file not found at %s. "
                    "Raw registers: [%s]",
                    pn_hash, raw_pn, ip_address, port, config_path, raw_hex
                )
                return False

        except Exception as e:
            _LOGGER.error("Exception while checking device support at %s:%d: %s", ip_address, port, e)
            return False
        finally:
            # Ensure client is properly closed
            try:
                if client:
                    client.disconnect()
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
                # Set unique ID to prevent duplicate entries
                await self.async_set_unique_id(ip_address)
                self._abort_if_unique_id_configured()
                
                # Test Modbus connection before creating entry
                if not await self._test_modbus_connection(ip_address):
                    errors["base"] = ERROR_CANNOT_CONNECT
                elif not await self._check_device_support(ip_address):
                    errors["base"] = ERROR_DEVICE_NOT_SUPPORTED
                else:
                    # Create entry only if connection is successful and device is supported
                    return self.async_create_entry(
                        title=f"Anker Solix {ip_address}",
                        data={
                            "ip_address": ip_address,
                            "port": 502,
                            "device_name": f"Anker Solix Device {ip_address}"
                        }
                    )

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({
                vol.Required("ip_address"): str,
            }),
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
        if not await self._check_device_support(ip_address, port):
            return self.async_abort(reason="device_not_supported")
        
        # Set unique ID to prevent duplicate entries
        await self.async_set_unique_id(ip_address)
        self._abort_if_unique_id_configured()
        
        return self.async_create_entry(
            title=f"Anker Solix {ip_address}",
            data={
                "ip_address": ip_address,
                "port": port,
                "device_name": import_data.get("device_name", f"Anker Solix Device {ip_address}")
            }
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
            data_schema=vol.Schema({
                vol.Optional("device_name", default=default_name): str,
            }),
        )
