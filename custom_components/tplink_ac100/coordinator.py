"""Coordinate local controller polling and explicit management actions."""

import logging
from collections.abc import Awaitable, Callable

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.core import Event, HomeAssistant
from homeassistant.exceptions import (
    ConfigEntryAuthFailed,
    ConfigEntryError,
    ConfigEntryNotReady,
    HomeAssistantError,
)
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import Ac100Api, Ac100AuthError, Ac100Error, Ac100SessionBusyError
from .const import CONF_ENABLE_CONTROL, DOMAIN, UPDATE_INTERVAL
from .models import Snapshot

_LOGGER = logging.getLogger(__name__)


class Ac100Coordinator(DataUpdateCoordinator[Snapshot]):
    """Fetch one consistent snapshot for all native entities."""

    controller_device_id: str

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, api: Ac100Api) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=UPDATE_INTERVAL,
            always_update=False,
        )
        self.api = api
        self.entry = entry

        async def stop(event: Event) -> None:
            await self.async_shutdown()

        entry.async_on_unload(hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, stop))

    async def async_shutdown(self) -> None:
        """Stop refreshes and release this client's session on unload or HA stop."""
        await super().async_shutdown()
        try:
            await self.api.async_logout()
        except Ac100Error:
            _LOGGER.warning(
                "Controller session could not be released; it will expire automatically"
            )

    async def _async_update_data(self) -> Snapshot:
        try:
            snapshot = await self.api.async_get_snapshot()
        except Ac100SessionBusyError as err:
            raise ConfigEntryNotReady(
                "Another controller management session is active; sign out there or wait for expiry"
            ) from err
        except Ac100AuthError as err:
            raise ConfigEntryAuthFailed("Controller authentication failed") from err
        except Ac100Error as err:
            raise UpdateFailed("Cannot update the controller") from err
        if snapshot.controller.model != "TL-AC100":
            raise ConfigEntryError("The host is not a supported TL-AC100 controller")
        if snapshot.controller.mac != self.entry.unique_id:
            raise ConfigEntryError("Controller identity changed; reconfigure the integration")
        return snapshot

    async def async_control(self, action: Callable[[], Awaitable[None]]) -> None:
        """Run an explicit action once, then refresh the observed state."""
        if self._shutdown_requested:
            raise HomeAssistantError(translation_domain=DOMAIN, translation_key="control_failed")
        if not self.entry.options.get(CONF_ENABLE_CONTROL, False):
            raise HomeAssistantError(translation_domain=DOMAIN, translation_key="control_disabled")
        try:
            await action()
        except Ac100SessionBusyError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="session_busy"
            ) from err
        except Ac100AuthError as err:
            self.entry.async_start_reauth(self.hass)
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="invalid_auth"
            ) from err
        except Ac100Error as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="control_failed"
            ) from err
        await self.async_refresh()


type Ac100ConfigEntry = ConfigEntry[Ac100Coordinator]
