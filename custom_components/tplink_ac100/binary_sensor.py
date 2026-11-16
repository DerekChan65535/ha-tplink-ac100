"""Access point link status from the controller's shared snapshot."""

from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .client_monitoring import add_client_connectivity
from .configuration_entity import add_configuration_entities
from .coordinator import Ac100ConfigEntry, Ac100Coordinator
from .entity import Ac100Entity
from .monitoring import PortStatus
from .radio_entity import add_radio_settings
from .terminal_monitoring import add_terminal_binary_sensors
from .wireless_service import add_wireless_service_binary_sensors

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: Ac100ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add a connectivity sensor for every current and newly discovered AP."""
    coordinator = entry.runtime_data
    known_aps: set[str] = set()
    known_ports: set[str] = set()
    async_add_entities(
        Ac100ControllerStatus(coordinator, description) for description in CONTROLLER_STATUS
    )

    @callback
    def async_add_new_entities() -> None:
        entities = []
        for ap in coordinator.data.aps:
            if ap.mac not in known_aps:
                known_aps.add(ap.mac)
                entities.append(Ac100ApConnectivitySensor(coordinator, ap.mac))
        if (monitoring := coordinator.data.monitoring) is not None:
            for port in monitoring.ports:
                if port.port_id not in known_ports:
                    known_ports.add(port.port_id)
                    entities.append(Ac100PortConnectivity(coordinator, port.port_id, port.name))
        if entities:
            async_add_entities(entities)

    async_add_new_entities()
    entry.async_on_unload(coordinator.async_add_listener(async_add_new_entities))
    add_configuration_entities(entry, async_add_entities, binary=True)
    add_radio_settings(entry, async_add_entities, binary=True)
    add_wireless_service_binary_sensors(entry, async_add_entities)
    add_client_connectivity(entry, async_add_entities)
    add_terminal_binary_sensors(entry, async_add_entities)


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


CONTROLLER_STATUS = (
    BinarySensorEntityDescription(
        key="configuration_not_saved",
        translation_key="configuration_not_saved",
        device_class=BinarySensorDeviceClass.PROBLEM,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    BinarySensorEntityDescription(
        key="unknown_access_points",
        translation_key="unknown_access_points",
        device_class=BinarySensorDeviceClass.PROBLEM,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    BinarySensorEntityDescription(
        key="access_point_limit_reached",
        translation_key="access_point_limit_reached",
        device_class=BinarySensorDeviceClass.PROBLEM,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    BinarySensorEntityDescription(
        key="ap_upgrade_in_progress",
        translation_key="ap_upgrade_in_progress",
        device_class=BinarySensorDeviceClass.RUNNING,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
    ),
    BinarySensorEntityDescription(
        key="radio_optimization_in_progress",
        translation_key="radio_optimization_in_progress",
        device_class=BinarySensorDeviceClass.RUNNING,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
    ),
    BinarySensorEntityDescription(
        key="controller_writable",
        translation_key="controller_writable",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
    ),
    BinarySensorEntityDescription(
        key="cloud_bound",
        translation_key="cloud_bound",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
    ),
    BinarySensorEntityDescription(
        key="cloud_management_enabled",
        translation_key="cloud_management_enabled",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
    ),
)


class Ac100ControllerStatus(Ac100Entity, BinarySensorEntity):
    """A status flag; unavailable when its source was not read, unknown if unrecognised."""

    def __init__(
        self, coordinator: Ac100Coordinator, description: BinarySensorEntityDescription
    ) -> None:
        super().__init__(coordinator, description.key)
        self.entity_description = description

    @property
    def available(self) -> bool:
        return (
            super().available
            and (monitoring := self.coordinator.data.monitoring) is not None
            and self.entity_description.key in monitoring.values
        )

    @property
    def is_on(self) -> bool | None:
        if (monitoring := self.coordinator.data.monitoring) is None:
            return None
        value = monitoring.values.get(self.entity_description.key)
        return value if isinstance(value, bool) else None


class Ac100PortConnectivity(Ac100Entity, BinarySensorEntity):
    """A physical port, identified independently of its display name and row order."""

    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_translation_key = "port_connectivity"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: Ac100Coordinator, port_id: str, name: str) -> None:
        super().__init__(coordinator, f"port_{port_id}_connectivity")
        self._port_id = port_id
        self._attr_translation_placeholders = {"port": name}

    @property
    def port(self) -> PortStatus | None:
        if (monitoring := self.coordinator.data.monitoring) is None:
            return None
        return next((port for port in monitoring.ports if port.port_id == self._port_id), None)

    @property
    def available(self) -> bool:
        return super().available and self.port is not None

    @property
    def is_on(self) -> bool | None:
        return port.connected if (port := self.port) else None
