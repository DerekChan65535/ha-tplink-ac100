"""Coordinate local controller polling and explicit management actions."""

import logging
from collections.abc import Awaitable, Callable

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryError, HomeAssistantError
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import Ac100Api, Ac100AuthError, Ac100Error
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

    async def _async_update_data(self) -> Snapshot:
        try:
            snapshot = await self.api.async_get_snapshot()
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
        if not self.entry.options.get(CONF_ENABLE_CONTROL, False):
            raise HomeAssistantError(translation_domain=DOMAIN, translation_key="control_disabled")
        try:
            await action()
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
