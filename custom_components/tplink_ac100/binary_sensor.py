"""Access point link status from the controller's shared snapshot."""

from typing import Any

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import Ac100ConfigEntry, Ac100Coordinator
from .entity import Ac100Entity

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: Ac100ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add a connectivity sensor for every current and newly discovered AP."""
    coordinator = entry.runtime_data
    known_aps: set[str] = set()

    @callback
    def async_add_new_entities() -> None:
        entities = []
        for ap in coordinator.data.aps:
            if ap.mac not in known_aps:
                known_aps.add(ap.mac)
                entities.append(Ac100ApConnectivitySensor(coordinator, ap.mac))
        if entities:
            async_add_entities(entities)

    async_add_new_entities()
    entry.async_on_unload(coordinator.async_add_listener(async_add_new_entities))


class Ac100ApConnectivitySensor(Ac100Entity, BinarySensorEntity):
    """An AP is online on either the master or standby controller link."""

    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_translation_key = "connectivity"

    def __init__(self, coordinator: Ac100Coordinator, ap_mac: str) -> None:
        """Initialize an AP's stable connectivity entity."""
        super().__init__(coordinator, "connectivity", ap_mac)

    @property
    def is_on(self) -> bool | None:
        """Return unknown if this AP has been removed from the controller."""
        if (ap := self.access_point) is None:
            return None
        return ap.is_online

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Keep the raw master, standby or offline link status visible."""
        if (ap := self.access_point) is None:
            return None
        return {"link_status": ap.link_status}
