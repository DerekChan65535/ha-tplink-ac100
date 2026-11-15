"""Debug logging and the opt-in controls: AP LEDs, AP radios and controller settings."""

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import CONF_ENABLE_CONTROL, DOMAIN
from .control_entity import Ac100SettingControl
from .coordinator import CONFIGURATION_CONTEXT, Ac100ConfigEntry, Ac100Coordinator
from .debug_logging import (
    SIGNAL_DEBUG_LOGGING_CHANGED,
    Ac100DebugLogging,
    async_get_debug_logging,
)
from .entity import Ac100Entity, band_label
from .management import writable_fields
from .models import ApRadio
from .wireless_service import add_wireless_service_switches


async def async_setup_entry(
    hass: HomeAssistant, entry: Ac100ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator = entry.runtime_data
    async_add_entities([Ac100DebugLoggingSwitch(coordinator, async_get_debug_logging(hass))])
    if not entry.options.get(CONF_ENABLE_CONTROL, False):
        return
    add_wireless_service_switches(entry, async_add_entities)
    async_add_entities(
        Ac100SettingSwitch(coordinator, field) for field in writable_fields("switch")
    )
    known: set[str] = set()
    known_radios: set[tuple[str, str]] = set()

    @callback
    def add_switches() -> None:
        aps = [
            ap for ap in coordinator.data.aps if ap.mac not in known and ap.led_enabled is not None
        ]
        known.update(ap.mac for ap in aps)
        async_add_entities(Ac100LedSwitch(coordinator, ap.mac) for ap in aps)

    @callback
    def add_radio_switches() -> None:
        entities = []
        for ap in coordinator.data.aps:
            for radio in ap.radios:
                if radio.unit and (ap.mac, radio.unit) not in known_radios:
                    known_radios.add((ap.mac, radio.unit))
                    entities.append(Ac100RadioSwitch(coordinator, ap.mac, radio))
        if entities:
            async_add_entities(entities)

    add_switches()
    entry.async_on_unload(coordinator.async_add_listener(add_switches))
    add_radio_switches()
    entry.async_on_unload(coordinator.async_add_listener(add_radio_switches))


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


class Ac100SettingSwitch(Ac100SettingControl, SwitchEntity):
    """An on/off setting of the whole controller, such as band navigation."""

    @property
    def current(self) -> bool | None:
        value = super().current
        return value if isinstance(value, bool) else None

    @property
    def is_on(self) -> bool | None:
        return self.current

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._async_write(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._async_write(False)


class Ac100RadioSwitch(Ac100Entity, SwitchEntity):
    """Switch one radio of an AP on or off, as the controller's radio page does.

    The state is the radio's configured state from the settings read, so it is unknown while
    that read has no row for the radio or the AP is not online. Switching a radio off
    disconnects the clients that use it.
    """

    _attr_translation_key = "radio_enabled"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_entity_registry_enabled_default = False

    def __init__(self, coordinator: Ac100Coordinator, ap_mac: str, radio: ApRadio) -> None:
        super().__init__(coordinator, f"radio_{radio.unit}_enabled_control", ap_mac)
        self._radio_unit = radio.unit
        self._attr_translation_placeholders = {"band": band_label(radio)}
        # Only an enabled settings entity makes the coordinator read the settings.
        self.coordinator_context = CONFIGURATION_CONTEXT

    @property
    def enabled(self) -> bool | None:
        """The radio's configured state, or None while it is not known."""
        configuration = self.coordinator.data.configuration
        if (ap := self.access_point) is None or not ap.is_manageable or configuration is None:
            return None
        row = next(
            (
                radio
                for radio in configuration.radios
                if radio.ap_mac == self.ap_mac and radio.unit == self._radio_unit
            ),
            None,
        )
        value = None if row is None else row.values.get("enabled")
        return value if isinstance(value, bool) else None

    @property
    def available(self) -> bool:
        return super().available and self.enabled is not None

    @property
    def is_on(self) -> bool | None:
        return self.enabled

    async def _async_set(self, enabled: bool) -> None:
        async def action() -> None:
            await self.coordinator.async_refresh()
            if not self.available or (ap := self.access_point) is None:
                raise HomeAssistantError(
                    translation_domain=DOMAIN, translation_key="control_unavailable"
                )
            await self.coordinator.api.async_set_radio_enabled(
                ap.entry_id, self._radio_unit, enabled
            )

        await self.coordinator.async_control(action)

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._async_set(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._async_set(False)
