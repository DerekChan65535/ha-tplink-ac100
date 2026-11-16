"""Opt-in numeric controls of controller-wide settings."""

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import CONF_ENABLE_CONTROL
from .control_entity import Ac100SettingControl
from .coordinator import Ac100ConfigEntry, Ac100Coordinator
from .management import native_range, writable_fields
from .monitoring import ConfigField
from .terminal_controls import add_terminal_numbers


async def async_setup_entry(
    hass: HomeAssistant,
    entry: Ac100ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add a number for every whole-number setting that a web page of the controller edits."""
    add_terminal_numbers(entry, async_add_entities)
    if not entry.options.get(CONF_ENABLE_CONTROL, False):
        return
    coordinator = entry.runtime_data
    async_add_entities(
        Ac100SettingNumber(coordinator, field) for field in writable_fields("integer")
    )


class Ac100SettingNumber(Ac100SettingControl, NumberEntity):
    """A whole-number setting, limited to the range that its web page accepts."""

    _attr_mode = NumberMode.BOX
    _attr_native_step = 1

    def __init__(self, coordinator: Ac100Coordinator, field: ConfigField) -> None:
        super().__init__(coordinator, field)
        self._attr_native_min_value, self._attr_native_max_value = native_range(field)
        self._attr_native_unit_of_measurement = field.unit

    @property
    def current(self) -> int | float | None:
        value = super().current
        return value if isinstance(value, int | float) and not isinstance(value, bool) else None

    @property
    def native_value(self) -> int | float | None:
        return self.current

    async def async_set_native_value(self, value: float) -> None:
        await self._async_write(int(value) if value.is_integer() else value)
