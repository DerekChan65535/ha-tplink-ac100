"""Local debug logging and explicitly enabled access-point LED controls."""

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import CONF_ENABLE_CONTROL, DOMAIN
from .coordinator import Ac100ConfigEntry, Ac100Coordinator
from .debug_logging import (
    SIGNAL_DEBUG_LOGGING_CHANGED,
    Ac100DebugLogging,
    async_get_debug_logging,
)
from .entity import Ac100Entity


async def async_setup_entry(
    hass: HomeAssistant, entry: Ac100ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator = entry.runtime_data
    async_add_entities([Ac100DebugLoggingSwitch(coordinator, async_get_debug_logging(hass))])
    if not entry.options.get(CONF_ENABLE_CONTROL, False):
        return
    known: set[str] = set()

    @callback
    def add_switches() -> None:
        aps = [
            ap for ap in coordinator.data.aps if ap.mac not in known and ap.led_enabled is not None
        ]
        known.update(ap.mac for ap in aps)
        async_add_entities(Ac100LedSwitch(coordinator, ap.mac) for ap in aps)

    add_switches()
    entry.async_on_unload(coordinator.async_add_listener(add_switches))


class Ac100DebugLoggingSwitch(Ac100Entity, SwitchEntity):
    """Toggle HA-side logging without changing or contacting the controller."""

    _attr_translation_key = "debug_logging"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: Ac100Coordinator, debug_logging: Ac100DebugLogging) -> None:
        """Associate the local setting with this controller's stable identity."""
        super().__init__(coordinator, "debug_logging")
        self._debug_logging = debug_logging

    async def async_added_to_hass(self) -> None:
        """Keep all entries' switches in sync and clean up on removal."""
        await super().async_added_to_hass()
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, SIGNAL_DEBUG_LOGGING_CHANGED, self.async_write_ha_state
            )
        )

    @property
    def available(self) -> bool:
        """Local logging remains usable when controller polling fails."""
        return True

    @property
    def is_on(self) -> bool:
        """Read the current namespace level instead of a cached boolean."""
        return self._debug_logging.is_on

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Enable this integration's diagnostic logging without a device action."""
        self._debug_logging.async_set_enabled(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Restore the previous local level, ensuring DEBUG is disabled."""
        self._debug_logging.async_set_enabled(False)


class Ac100LedSwitch(Ac100Entity, SwitchEntity):
    """Control only the AP's indicator, not its radios or wireless service."""

    _attr_translation_key = "led"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator: Ac100Coordinator, ap_mac: str) -> None:
        super().__init__(coordinator, "led", ap_mac)

    @property
    def available(self) -> bool:
        return (
            super().available
            and (ap := self.access_point) is not None
            and ap.is_manageable
            and ap.led_enabled is not None
        )

    @property
    def is_on(self) -> bool | None:
        return self.access_point.led_enabled if self.available else None

    async def _async_set(self, enabled: bool) -> None:
        async def action() -> None:
            await self.coordinator.async_refresh()
            if not self.available or (ap := self.access_point) is None:
                raise HomeAssistantError(
                    translation_domain=DOMAIN, translation_key="control_unavailable"
                )
            await self.coordinator.api.async_set_ap_led(ap.entry_id, enabled)

        await self.coordinator.async_control(action)

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._async_set(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._async_set(False)
