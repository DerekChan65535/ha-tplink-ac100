"""Read-only controller and access point telemetry from a shared snapshot."""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import PERCENTAGE, EntityCategory, UnitOfTime
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import Ac100ConfigEntry, Ac100Coordinator
from .entity import Ac100Entity
from .models import Snapshot

PARALLEL_UPDATES = 0


@dataclass(frozen=True, kw_only=True)
class Ac100ControllerSensorDescription(SensorEntityDescription):
    """Describe a value derived from the current controller snapshot."""

    value_fn: Callable[[Snapshot], int | float | None]


CONTROLLER_SENSORS = (
    Ac100ControllerSensorDescription(
        key="memory_usage",
        translation_key="memory_usage",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda snapshot: snapshot.controller.memory_usage,
    ),
    Ac100ControllerSensorDescription(
        key="uptime",
        translation_key="uptime",
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.SECONDS,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda snapshot: snapshot.controller.uptime,
    ),
    Ac100ControllerSensorDescription(
        key="online_access_points",
        translation_key="online_access_points",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda snapshot: sum(ap.is_online for ap in snapshot.aps),
    ),
    Ac100ControllerSensorDescription(
        key="total_access_points",
        translation_key="total_access_points",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda snapshot: len(snapshot.aps),
    ),
    Ac100ControllerSensorDescription(
        key="connected_clients",
        translation_key="connected_clients",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda snapshot: len(snapshot.clients),
    ),
)

AP_SENSORS = (
    SensorEntityDescription(
        key="connected_clients",
        translation_key="connected_clients",
        state_class=SensorStateClass.MEASUREMENT,
    ),
    SensorEntityDescription(
        key="uptime",
        translation_key="uptime",
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.SECONDS,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: Ac100ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add telemetry, including APs and CPU cores first seen in later polls."""
    coordinator = entry.runtime_data
    known_aps: set[str] = set()
    known_cores: set[str] = set()
    async_add_entities(
        Ac100ControllerSensor(coordinator, description) for description in CONTROLLER_SENSORS
    )

    @callback
    def async_add_new_entities() -> None:
        entities: list[SensorEntity] = []
        for core in coordinator.data.controller.cpu_usage:
            if core in known_cores:
                continue
            known_cores.add(core)
            entities.append(
                Ac100ControllerSensor(
                    coordinator,
                    Ac100ControllerSensorDescription(
                        key=f"cpu_usage_{core}",
                        translation_key="cpu_usage",
                        translation_placeholders={"core": core},
                        native_unit_of_measurement=PERCENTAGE,
                        state_class=SensorStateClass.MEASUREMENT,
                        entity_category=EntityCategory.DIAGNOSTIC,
                        value_fn=lambda snapshot, core=core: snapshot.controller.cpu_usage.get(
                            core
                        ),
                    ),
                )
            )
        for ap in coordinator.data.aps:
            if ap.mac in known_aps:
                continue
            known_aps.add(ap.mac)
            entities.extend(
                Ac100ApSensor(coordinator, description, ap.mac) for description in AP_SENSORS
            )
        if entities:
            async_add_entities(entities)

    async_add_new_entities()
    entry.async_on_unload(coordinator.async_add_listener(async_add_new_entities))


class Ac100ControllerSensor(Ac100Entity, SensorEntity):
    """A controller resource or network count."""

    entity_description: Ac100ControllerSensorDescription

    def __init__(
        self, coordinator: Ac100Coordinator, description: Ac100ControllerSensorDescription
    ) -> None:
        """Initialize a controller sensor."""
        super().__init__(coordinator, description.key)
        self.entity_description = description

    @property
    def native_value(self) -> int | float | None:
        """Return the value from the most recent shared snapshot."""
        return self.entity_description.value_fn(self.coordinator.data)


class Ac100ApSensor(Ac100Entity, SensorEntity):
    """Live AP telemetry that is unavailable while the AP is offline."""

    def __init__(
        self,
        coordinator: Ac100Coordinator,
        description: SensorEntityDescription,
        ap_mac: str,
    ) -> None:
        """Initialize an AP sensor with its stable MAC identity."""
        super().__init__(coordinator, description.key, ap_mac)
        self.entity_description = description

    @property
    def available(self) -> bool:
        """Do not present offline AP counters as live readings."""
        return super().available and (ap := self.access_point) is not None and ap.is_online

    @property
    def native_value(self) -> int | None:
        """Ignore counters retained by the controller for offline APs."""
        if (ap := self.access_point) is None or not ap.is_online:
            return None
        if self.entity_description.key == "uptime":
            return ap.uptime
        return ap.client_count

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Expose radio metadata by stable radio unit, without extra entities."""
        if (
            self.entity_description.key != "connected_clients"
            or (ap := self.access_point) is None
            or not ap.is_online
        ):
            return None
        return {
            "ip_address": ap.ip,
            "radios": {
                radio.unit: {
                    "frequency": radio.frequency,
                    "channel": radio.channel,
                    "channel_load": radio.channel_load,
                }
                for radio in ap.radios
            },
        }
