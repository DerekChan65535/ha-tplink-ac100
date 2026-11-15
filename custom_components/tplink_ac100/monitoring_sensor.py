"""Credential-free physical-port and interface telemetry from slower polls."""

from dataclasses import dataclass

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import EntityCategory, UnitOfDataRate, UnitOfInformation
from homeassistant.core import callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import Ac100ConfigEntry, Ac100Coordinator
from .entity import Ac100Entity
from .monitoring import InterfaceTraffic, PortStatus


@dataclass(frozen=True, kw_only=True)
class TrafficDescription(SensorEntityDescription):
    """A measured value on one named network interface."""

    field: str


TRAFFIC_SENSORS = (
    TrafficDescription(
        key="transmit_rate",
        field="tx_kilobytes_per_second",
        translation_key="interface_transmit_rate",
        device_class=SensorDeviceClass.DATA_RATE,
        native_unit_of_measurement=UnitOfDataRate.KILOBYTES_PER_SECOND,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    TrafficDescription(
        key="receive_rate",
        field="rx_kilobytes_per_second",
        translation_key="interface_receive_rate",
        device_class=SensorDeviceClass.DATA_RATE,
        native_unit_of_measurement=UnitOfDataRate.KILOBYTES_PER_SECOND,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    TrafficDescription(
        key="transmit_packet_rate",
        field="tx_packets_per_second",
        translation_key="interface_transmit_packet_rate",
        native_unit_of_measurement="packets/s",
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
    ),
    TrafficDescription(
        key="receive_packet_rate",
        field="rx_packets_per_second",
        translation_key="interface_receive_packet_rate",
        native_unit_of_measurement="packets/s",
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
    ),
    TrafficDescription(
        key="transmitted_bytes",
        field="tx_bytes",
        translation_key="interface_transmitted_bytes",
        device_class=SensorDeviceClass.DATA_SIZE,
        native_unit_of_measurement=UnitOfInformation.BYTES,
        state_class=SensorStateClass.TOTAL_INCREASING,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
    ),
    TrafficDescription(
        key="received_bytes",
        field="rx_bytes",
        translation_key="interface_received_bytes",
        device_class=SensorDeviceClass.DATA_SIZE,
        native_unit_of_measurement=UnitOfInformation.BYTES,
        state_class=SensorStateClass.TOTAL_INCREASING,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
    ),
)


def add_monitoring_sensors(
    entry: Ac100ConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    """Add sensors as interfaces and physical ports are first observed."""
    coordinator = entry.runtime_data
    known_interfaces: set[str] = set()
    known_ports: set[str] = set()

    @callback
    def add_sensors() -> None:
        if (monitoring := coordinator.data.monitoring) is None:
            return
        entities: list[SensorEntity] = []
        for interface in monitoring.interfaces:
            if interface.name in known_interfaces:
                continue
            known_interfaces.add(interface.name)
            entities.extend(
                Ac100InterfaceSensor(coordinator, interface.name, description)
                for description in TRAFFIC_SENSORS
            )
        for port in monitoring.ports:
            if port.port_id not in known_ports:
                known_ports.add(port.port_id)
                entities.append(Ac100PortSpeed(coordinator, port.port_id, port.name))
        if entities:
            async_add_entities(entities)

    add_sensors()
    entry.async_on_unload(coordinator.async_add_listener(add_sensors))


class Ac100InterfaceSensor(Ac100Entity, SensorEntity):
    """Interface counters and rates, not inferred from Wi-Fi negotiation speeds."""

    entity_description: TrafficDescription

    def __init__(
        self,
        coordinator: Ac100Coordinator,
        interface_name: str,
        description: TrafficDescription,
    ) -> None:
        super().__init__(coordinator, f"interface_{interface_name}_{description.key}")
        self._interface_name = interface_name
        self.entity_description = description
        self._attr_translation_placeholders = {"interface": interface_name.upper()}

    @property
    def interface(self) -> InterfaceTraffic | None:
        if (monitoring := self.coordinator.data.monitoring) is None:
            return None
        return next(
            (
                interface
                for interface in monitoring.interfaces
                if interface.name == self._interface_name
            ),
            None,
        )

    @property
    def available(self) -> bool:
        return super().available and self.interface is not None

    @property
    def native_value(self) -> int | float | None:
        if (interface := self.interface) is None:
            return None
        return getattr(interface, self.entity_description.field)


class Ac100PortSpeed(Ac100Entity, SensorEntity):
    """The negotiated link speed of a physical port, not its configured limit."""

    _attr_translation_key = "port_link_speed"
    _attr_device_class = SensorDeviceClass.DATA_RATE
    _attr_native_unit_of_measurement = UnitOfDataRate.MEGABITS_PER_SECOND
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: Ac100Coordinator, port_id: str, name: str) -> None:
        super().__init__(coordinator, f"port_{port_id}_link_speed")
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
    def native_value(self) -> int | None:
        if (port := self.port) is None or port.connected is not True:
            return None
        return port.speed_mbps
