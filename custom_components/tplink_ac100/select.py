"""Opt-in choices for controller-wide settings."""

from homeassistant.components.select import SelectEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import CONF_ENABLE_CONTROL
from .control_entity import Ac100SettingControl
from .coordinator import Ac100ConfigEntry, Ac100Coordinator
from .management import select_options, writable_fields
from .monitoring import ConfigField
from .terminal_controls import add_terminal_selects
from .terminal_schedule_entities import add_terminal_schedule_selects


async def async_setup_entry(
    hass: HomeAssistant,
    entry: Ac100ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add a select for every setting for which a web page of the controller offers a choice."""
    add_terminal_selects(entry, async_add_entities)
    add_terminal_schedule_selects(entry, async_add_entities)
    if not entry.options.get(CONF_ENABLE_CONTROL, False):
        return
    coordinator = entry.runtime_data
    async_add_entities(
        Ac100SettingSelect(coordinator, field) for field in writable_fields("choice")
    )


class Ac100SettingSelect(Ac100SettingControl, SelectEntity):
    """A setting with the choices that its web page offers and no others.

    A setting that the controller reports as a state name (such as forward) is offered by those
    names. One that it reports as a number (a rate in Mbit/s) is offered as digit strings.
    """

    def __init__(self, coordinator: Ac100Coordinator, field: ConfigField) -> None:
        super().__init__(coordinator, field)
        self._attr_options = list(select_options(field))

    @property
    def current(self) -> str | None:
        value = super().current
        if value is None:
            return None
        option = value if isinstance(value, str) else str(value)
        return option if option in self._attr_options else None

    @property
    def current_option(self) -> str | None:
        return self.current

    async def async_select_option(self, option: str) -> None:
        await self._async_write(int(option) if self.field.kind == "number" else option)
