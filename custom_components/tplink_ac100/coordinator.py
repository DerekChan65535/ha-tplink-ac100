"""Coordinate local controller polling and explicit management actions."""

import logging
from collections.abc import Awaitable, Callable
from dataclasses import replace
from time import monotonic

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

from .api import (
    Ac100AccountLockedError,
    Ac100Api,
    Ac100AuthError,
    Ac100ConnectionError,
    Ac100Error,
    Ac100ProtocolError,
    Ac100SessionBusyError,
    Ac100SessionExpiredError,
    Ac100UnsafeLedError,
)
from .const import (
    CONF_ENABLE_CONTROL,
    CONFIGURATION_INTERVAL,
    DOMAIN,
    MONITORING_INTERVAL,
    UPDATE_INTERVAL,
)
from .models import Snapshot
from .monitoring import ConfigurationState, MonitoringState

_LOGGER = logging.getLogger(__name__)

# Coordinator listener context of entities that need the slow settings read.
CONFIGURATION_CONTEXT = "configuration"


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
        self._monitoring: MonitoringState | None = None
        self._configuration: ConfigurationState | None = None
        self._next_monitoring = 0.0
        self._next_configuration = monotonic() + UPDATE_INTERVAL.total_seconds()
        self._failed_reads: set[str] = set()

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
            if snapshot.controller.model != "TL-AC100":
                raise ConfigEntryError("The host is not a supported TL-AC100 controller")
            if snapshot.controller.mac != self.entry.unique_id:
                raise ConfigEntryError("Controller identity changed; reconfigure the integration")
            snapshot = await self._async_update_optional(snapshot)
        except Ac100SessionBusyError as err:
            raise ConfigEntryNotReady(
                "Another controller management session is active; sign out there or wait for expiry"
            ) from err
        except Ac100AccountLockedError as err:
            raise UpdateFailed("The controller account is temporarily locked") from err
        except Ac100SessionExpiredError as err:
            raise UpdateFailed(
                "The controller session expired; retrying on the next update"
            ) from err
        except Ac100AuthError as err:
            raise ConfigEntryAuthFailed("Controller authentication failed") from err
        except Ac100Error as err:
            raise UpdateFailed("Cannot update the controller") from err
        return snapshot

    async def _async_update_optional(self, snapshot: Snapshot) -> Snapshot:
        """Run at most one slow read per poll, without a catch-up burst after downtime.

        Settings are read only while an enabled entity uses them.
        """
        now = monotonic()
        if now >= self._next_monitoring:
            self._monitoring = await self._async_read_optional(
                "monitoring", self.api.async_get_monitoring
            )
            self._next_monitoring = now + MONITORING_INTERVAL.total_seconds()
        elif now >= self._next_configuration and CONFIGURATION_CONTEXT in set(
            self.async_contexts()
        ):
            self._configuration = await self._async_read_optional(
                "configuration", self.api.async_get_configuration
            )
            self._next_configuration = now + CONFIGURATION_INTERVAL.total_seconds()
        return replace(snapshot, monitoring=self._monitoring, configuration=self._configuration)

    async def _async_read_optional[T](
        self, name: str, read: Callable[[], Awaitable[T]]
    ) -> T | None:
        """A failed optional read makes its entities unavailable; core polling continues."""
        try:
            result = await read()
        except Ac100SessionBusyError:
            raise
        except Ac100ConnectionError, Ac100ProtocolError, Ac100AuthError:
            # The core read just succeeded, so an authorization error here means the account
            # may not read this source, not that the credentials are wrong.
            if name not in self._failed_reads:
                self._failed_reads.add(name)
                _LOGGER.warning("Controller %s data is unavailable", name)
            return None
        if name in self._failed_reads:
            self._failed_reads.discard(name)
            _LOGGER.info("Controller %s data is available again", name)
        return result

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
        except Ac100UnsafeLedError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="led_wifi_sync_unsafe"
            ) from err
        except Ac100AccountLockedError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="account_locked"
            ) from err
        except Ac100SessionExpiredError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="session_expired"
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
        self._next_monitoring = 0.0
        await self.async_refresh()


type Ac100ConfigEntry = ConfigEntry[Ac100Coordinator]
