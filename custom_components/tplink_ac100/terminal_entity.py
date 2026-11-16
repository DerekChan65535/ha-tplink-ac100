"""Shared bases of the native entities that describe or change one terminal's saved configuration.

A terminal is a Wi-Fi client addressed by its MAC. Its entities sit on the client's own device and
read the controller's saved record for that MAC (the host table). That record is separate from the
client's current telemetry: it can outlive a visit, and the client can be present without it.
"""

from collections.abc import Awaitable, Callable, Iterable

from homeassistant.const import EntityCategory
from homeassistant.core import callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .api import Ac100Api
from .client_entity import known_client_macs
from .const import CONF_ENABLE_CONTROL, DEFAULT_ENABLE_CONTROL
from .coordinator import Ac100ConfigEntry, Ac100Coordinator
from .device_info import client_device_info, current_client, terminal_configuration
from .models import Client
from .terminal import TerminalConfiguration


def add_for_each_terminal(
    entry: Ac100ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
    create: Callable[[Ac100Coordinator, str], Iterable[Entity]],
) -> None:
    """Create entities once per known client MAC, now and for every MAC that appears later.

    The entities stay while the client is away, and for a saved record whose client was never seen.
    """
    coordinator = entry.runtime_data
    known: set[str] = set()

    @callback
    def add_terminals() -> None:
        new = known_client_macs(coordinator) - known
        known.update(new)
        if entities := [entity for mac in sorted(new) for entity in create(coordinator, mac)]:
            async_add_entities(entities)

    add_terminals()
    entry.async_on_unload(coordinator.async_add_listener(add_terminals))


class Ac100TerminalEntity(CoordinatorEntity[Ac100Coordinator]):
    """An entity of one terminal's saved configuration, on the terminal's own device.

    The configuration and the client are looked up by MAC in the latest snapshot each time, so
    no stale response object is kept. The entity is available while the last poll worked and the
    source that it needs is there: by default the terminal's saved record.
    """

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: Ac100Coordinator, mac: str, key: str) -> None:
        super().__init__(coordinator)
        self.mac = mac
        self._attr_unique_id = f"{coordinator.data.controller.mac}_terminal_{mac}_{key}"

    @property
    def configuration(self) -> TerminalConfiguration | None:
        """The saved record, which does not say that the terminal is present."""
        return terminal_configuration(self.coordinator, self.mac)

    @property
    def client(self) -> Client | None:
        """The terminal while the controller lists it as connected."""
        return current_client(self.coordinator, self.mac)

    @property
    def source_available(self) -> bool:
        """Whether the data that this entity shows or acts on is there."""
        return self.configuration is not None

    @property
    def available(self) -> bool:
        return super().available and self.source_available

    @property
    def device_info(self) -> DeviceInfo:
        return client_device_info(self.coordinator, self.mac)


class Ac100TerminalControl(Ac100TerminalEntity):
    """A control of one terminal: off until the user enables it, and a management action.

    It is registered whether or not device control is on, so that it can be found, but it is
    unavailable until the integration option is on. Its action runs through the coordinator, which
    refuses it without that option, sends it once and never repeats it.
    """

    _attr_entity_category = EntityCategory.CONFIG
    _attr_entity_registry_enabled_default = False

    @property
    def management_enabled(self) -> bool:
        return bool(self.coordinator.entry.options.get(CONF_ENABLE_CONTROL, DEFAULT_ENABLE_CONTROL))

    @property
    def available(self) -> bool:
        return super().available and self.management_enabled

    async def async_control_terminal(self, action: Callable[[Ac100Api], Awaitable[None]]) -> None:
        """Run one terminal action; the API checks the terminal again before it writes."""
        await self.coordinator.async_control(lambda: action(self.coordinator.api))
