"""Configuration flows for local TP-Link TL-AC100 controllers."""

import logging
from collections.abc import Mapping
from typing import Any

import probatio
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigEntryState,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlowWithReload,
)
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.device_registry import format_mac
from homeassistant.helpers.selector import TextSelector, TextSelectorConfig, TextSelectorType

from .api import (
    Ac100Api,
    Ac100AuthError,
    Ac100ConnectionError,
    Ac100Error,
    Ac100SessionBusyError,
    normalize_host,
)
from .const import CONF_ENABLE_CONTROL, DEFAULT_ENABLE_CONTROL, DOMAIN

_LOGGER = logging.getLogger(__name__)

_PASSWORD_SELECTOR = TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD))
USER_SCHEMA = probatio.Schema(
    {
        probatio.Required(CONF_HOST): str,
        probatio.Required(CONF_USERNAME): str,
        probatio.Required(CONF_PASSWORD): _PASSWORD_SELECTOR,
    }
)
RECONFIGURE_SCHEMA = probatio.Schema({probatio.Required(CONF_HOST): str})
REAUTH_SCHEMA = probatio.Schema(
    {
        probatio.Required(CONF_USERNAME): str,
        probatio.Required(CONF_PASSWORD): _PASSWORD_SELECTOR,
    }
)
OPTIONS_SCHEMA = probatio.Schema(
    {probatio.Required(CONF_ENABLE_CONTROL, default=DEFAULT_ENABLE_CONTROL): bool}
)


class UnsupportedDevice(Exception):
    """The host is not a supported TL-AC100 controller."""


async def async_validate_input(hass: HomeAssistant, data: Mapping[str, Any]) -> str:
    """Validate credentials and return the controller's stable hardware identity."""
    api = Ac100Api(
        async_get_clientsession(hass),
        data[CONF_HOST],
        data[CONF_USERNAME],
        data[CONF_PASSWORD],
    )
    try:
        await api.async_login()
        snapshot = await api.async_get_snapshot()
        if snapshot.controller.model != "TL-AC100":
            raise UnsupportedDevice
        return format_mac(snapshot.controller.mac)
    finally:
        await api.async_logout()


class Ac100ConfigFlow(ConfigFlow, domain=DOMAIN):
    """Set up, reconnect, or reauthenticate one local controller."""

    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> Ac100OptionsFlow:
        """Return the options flow without a separate update listener."""
        return Ac100OptionsFlow()

    async def _async_validate(self, data: Mapping[str, Any], errors: dict[str, str]) -> str | None:
        """Map validation errors without exposing credentials or response bodies."""
        try:
            return await async_validate_input(self.hass, data)
        except Ac100SessionBusyError:
            errors["base"] = "session_busy"
        except Ac100AuthError:
            errors["base"] = "invalid_auth"
        except Ac100ConnectionError:
            errors["base"] = "cannot_connect"
        except UnsupportedDevice:
            errors["base"] = "unsupported_device"
        except Ac100Error:
            errors["base"] = "unknown"
        except Exception as err:  # Unexpected validation errors must not break the UI.
            _LOGGER.error("Unexpected controller validation error: %s", type(err).__name__)
            errors["base"] = "unknown"
        return None

    async def _async_update_existing_entry(
        self,
        entry: ConfigEntry,
        data_updates: dict[str, Any],
        errors: dict[str, str],
    ) -> ConfigFlowResult | None:
        """Release our own session for validation and restore it on every failed update."""
        unloaded = False
        updated = False
        try:
            if entry.state is ConfigEntryState.LOADED:
                if not await self.hass.config_entries.async_unload(entry.entry_id):
                    errors["base"] = "unknown"
                    return None
                unloaded = True
            unique_id = await self._async_validate({**entry.data, **data_updates}, errors)
            if unique_id is None:
                return None
            await self.async_set_unique_id(unique_id)
            self._abort_if_unique_id_mismatch(reason="wrong_device")
            if CONF_HOST in data_updates:
                data_updates = {**data_updates, CONF_HOST: normalize_host(data_updates[CONF_HOST])}
            result = self.async_update_reload_and_abort(entry, data_updates=data_updates)
            updated = True
            return result
        finally:
            if unloaded and not updated:
                await self.hass.config_entries.async_setup(entry.entry_id)

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Handle an explicitly requested controller setup."""
        errors: dict[str, str] = {}
        if user_input is not None and (unique_id := await self._async_validate(user_input, errors)):
            await self.async_set_unique_id(unique_id)
            self._abort_if_unique_id_configured()
            return self.async_create_entry(
                title="TP-Link TL-AC100",
                data={**user_input, CONF_HOST: normalize_host(user_input[CONF_HOST])},
                options={CONF_ENABLE_CONTROL: DEFAULT_ENABLE_CONTROL},
            )
        return self.async_show_form(step_id="user", data_schema=USER_SCHEMA, errors=errors)

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Update connection details only if the host is the same controller."""
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            if result := await self._async_update_existing_entry(entry, user_input, errors):
                return result
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=self.add_suggested_values_to_schema(RECONFIGURE_SCHEMA, entry.data),
            errors=errors,
        )

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        """Start credential renewal without removing the existing entry."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Validate replacement credentials against the existing controller."""
        entry = self._get_reauth_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            if result := await self._async_update_existing_entry(entry, user_input, errors):
                return result
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=self.add_suggested_values_to_schema(
                REAUTH_SCHEMA, {CONF_USERNAME: entry.data[CONF_USERNAME]}
            ),
            errors=errors,
        )


class Ac100OptionsFlow(OptionsFlowWithReload):
    """Control whether explicit device management is allowed."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Save control opt-in and let Home Assistant reload the entry."""
        if user_input is not None:
            return self.async_create_entry(data=user_input)
        return self.async_show_form(
            step_id="init",
            data_schema=self.add_suggested_values_to_schema(
                OPTIONS_SCHEMA, self.config_entry.options
            ),
        )
