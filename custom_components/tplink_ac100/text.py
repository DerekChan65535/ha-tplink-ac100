"""Opt-in control of the controller's name."""

from homeassistant.components.text import TextEntity, TextMode
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import CONF_ENABLE_CONTROL
from .control_entity import Ac100SettingControl, invalid_value
from .coordinator import Ac100ConfigEntry, Ac100Coordinator
from .management import ALIAS_FIELD, ALIAS_MAX_LENGTH, ALIAS_RULE, alias_is_valid
from .terminal_controls import add_terminal_texts
from .terminal_schedule_entities import add_terminal_schedule_texts


async def async_setup_entry(
    hass: HomeAssistant,
    entry: Ac100ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add the controller's name as a text entity, and a terminal's name on the controller."""
    add_terminal_texts(entry, async_add_entities)
    add_terminal_schedule_texts(entry, async_add_entities)
    if entry.options.get(CONF_ENABLE_CONTROL, False):
        async_add_entities([Ac100DeviceNameText(entry.runtime_data)])


class Ac100DeviceNameText(Ac100SettingControl, TextEntity):
    """The name that the controller's System page shows, within the page's rules."""

    _attr_mode = TextMode.TEXT
    _attr_native_min = 1
    _attr_native_max = ALIAS_MAX_LENGTH

    def __init__(self, coordinator: Ac100Coordinator) -> None:
        super().__init__(coordinator, ALIAS_FIELD)

    @property
    def current(self) -> str | None:
        value = super().current
        return value if isinstance(value, str) and alias_is_valid(value) else None

    @property
    def native_value(self) -> str | None:
        return self.current

    async def async_set_value(self, value: str) -> None:
        if not alias_is_valid(value):
            raise invalid_value(ALIAS_RULE)

        async def action() -> None:
            await self.coordinator.api.async_set_controller_alias(value)

        await self.coordinator.async_control(action)
