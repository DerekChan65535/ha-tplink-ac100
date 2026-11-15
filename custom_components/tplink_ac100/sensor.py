"""Read-only controller and access point telemetry from a shared snapshot."""

import ipaddress
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import PERCENTAGE, EntityCategory, UnitOfDataRate, UnitOfTime
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import dt as dt_util

from .configuration_entity import add_configuration_entities
from .coordinator import Ac100ConfigEntry, Ac100Coordinator
from .entity import Ac100Entity, band_label
from .models import AccessPoint, ApRadio, Client, Snapshot
from .monitoring_sensor import add_monitoring_sensors
from .radio_entity import add_radio_settings
from .wireless_service import add_wireless_service_sensors

PARALLEL_UPDATES = 0


def _boot_time(uptime: int | None) -> datetime | None:
    """Convert seconds since boot to the point in time that UPTIME sensors report."""
    return None if uptime is None else dt_util.utcnow() - timedelta(seconds=uptime)


def _usable_ip(address: str) -> str | None:
    """Hide the blank or unspecified address that the controller reports for offline APs."""
    try:
        return None if ipaddress.ip_address(address).is_unspecified else address
    except ValueError:
        return None


def _core_number(core: str) -> str:
    """Show the raw key core1 as 1; the raw key itself stays in the unique ID."""
    return re.sub(r"^(?:cpu|core)", "", core, flags=re.IGNORECASE) or core


def _speed_total(clients: Iterable[Client], field: str) -> float | None:
    """Add up the known speeds in KB/s of a group of clients.

    A client without a known speed adds nothing. The total is unknown when the group has clients
    but none reports a speed, and zero when it has no clients.
    """
    group = tuple(clients)
    speeds = [speed for client in group if (speed := getattr(client, field)) is not None]
    if speeds:
        return sum(speeds)
    return None if group else 0.0


@dataclass(frozen=True, kw_only=True)
class Ac100ControllerSensorDescription(SensorEntityDescription):
    """Describe a value derived from the current controller snapshot."""

    value_fn: Callable[[Snapshot], int | float | datetime | None]


def _average_cpu(snapshot: Snapshot) -> float | None:
    """The web UI shows the mean of the per-core readings."""
    cores = snapshot.controller.cpu_usage.values()
    return sum(cores) / len(cores) if cores else None


CONTROLLER_SENSORS = (
    Ac100ControllerSensorDescription(
        key="cpu_usage",
        translation_key="cpu_usage_average",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=0,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=_average_cpu,
    ),
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
        suggested_display_precision=0,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda snapshot: snapshot.controller.uptime,
    ),
    Ac100ControllerSensorDescription(
        key="last_restart",
        translation_key="last_restart",
        device_class=SensorDeviceClass.UPTIME,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda snapshot: _boot_time(snapshot.controller.uptime),
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
    Ac100ControllerSensorDescription(
        key="wifi_upload_speed",
        translation_key="wifi_upload_speed",
        device_class=SensorDeviceClass.DATA_RATE,
        native_unit_of_measurement=UnitOfDataRate.KILOBYTES_PER_SECOND,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda snapshot: _speed_total(snapshot.clients, "upload_kilobytes_per_second"),
    ),
    Ac100ControllerSensorDescription(
        key="wifi_download_speed",
        translation_key="wifi_download_speed",
        device_class=SensorDeviceClass.DATA_RATE,
        native_unit_of_measurement=UnitOfDataRate.KILOBYTES_PER_SECOND,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda snapshot: _speed_total(snapshot.clients, "download_kilobytes_per_second"),
    ),
)


@dataclass(frozen=True, kw_only=True)
class Ac100ApSensorDescription(SensorEntityDescription):
    """Describe a value of one online access point."""

    value_fn: Callable[[AccessPoint], int | str | datetime | None]
    attributes_fn: Callable[[AccessPoint], dict[str, Any]] | None = None


@dataclass(frozen=True, kw_only=True)
class Ac100RadioSensorDescription(SensorEntityDescription):
    """Describe a value of one radio of an online access point."""

    value_fn: Callable[[ApRadio], int | float | None]


@dataclass(frozen=True, kw_only=True)
class Ac100ApSpeedDescription(SensorEntityDescription):
    """Describe the total of one speed of the clients that report an access point's name."""

    field: str


def _radio_attributes(ap: AccessPoint) -> dict[str, Any]:
    """Keep the attributes that predate the per-radio sensors, keyed by stable radio unit."""
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


AP_SENSORS = (
    Ac100ApSensorDescription(
        key="connected_clients",
        translation_key="connected_clients",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda ap: ap.client_count,
        attributes_fn=_radio_attributes,
    ),
    Ac100ApSensorDescription(
        key="uptime",
        translation_key="uptime",
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.SECONDS,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=0,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda ap: ap.uptime,
    ),
    Ac100ApSensorDescription(
        key="last_restart",
        translation_key="last_restart",
        device_class=SensorDeviceClass.UPTIME,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda ap: _boot_time(ap.uptime),
    ),
    Ac100ApSensorDescription(
        key="ip_address",
        translation_key="ip_address",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda ap: _usable_ip(ap.ip),
    ),
)

AP_SPEED_SENSORS = (
    Ac100ApSpeedDescription(
        key="wifi_upload_speed",
        translation_key="wifi_upload_speed",
        device_class=SensorDeviceClass.DATA_RATE,
        native_unit_of_measurement=UnitOfDataRate.KILOBYTES_PER_SECOND,
        state_class=SensorStateClass.MEASUREMENT,
        field="upload_kilobytes_per_second",
    ),
    Ac100ApSpeedDescription(
        key="wifi_download_speed",
        translation_key="wifi_download_speed",
        device_class=SensorDeviceClass.DATA_RATE,
        native_unit_of_measurement=UnitOfDataRate.KILOBYTES_PER_SECOND,
        state_class=SensorStateClass.MEASUREMENT,
        field="download_kilobytes_per_second",
    ),
)

RADIO_SENSORS = (
    Ac100RadioSensorDescription(
        key="connected_clients",
        translation_key="radio_connected_clients",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda radio: radio.client_count,
    ),
    Ac100RadioSensorDescription(
        key="channel",
        translation_key="radio_channel",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda radio: radio.channel,
    ),
    Ac100RadioSensorDescription(
        key="channel_load",
        translation_key="radio_channel_load",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=0,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda radio: radio.channel_load,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: Ac100ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add telemetry, including APs, radios and CPU cores first seen in later polls."""
    coordinator = entry.runtime_data
    known_aps: set[str] = set()
    known_radios: set[tuple[str, str]] = set()
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
                        translation_placeholders={"core": _core_number(core)},
                        native_unit_of_measurement=PERCENTAGE,
                        state_class=SensorStateClass.MEASUREMENT,
                        entity_category=EntityCategory.DIAGNOSTIC,
                        entity_registry_enabled_default=False,
                        value_fn=lambda snapshot, core=core: snapshot.controller.cpu_usage.get(
                            core
                        ),
                    ),
                )
            )
        for ap in coordinator.data.aps:
            if ap.mac not in known_aps:
                known_aps.add(ap.mac)
                entities.extend(
                    Ac100ApSensor(coordinator, description, ap.mac) for description in AP_SENSORS
                )
                entities.extend(
                    Ac100ApClientSpeed(coordinator, description, ap.mac)
                    for description in AP_SPEED_SENSORS
                )
            for radio in ap.radios:
                if radio.unit and (ap.mac, radio.unit) not in known_radios:
                    known_radios.add((ap.mac, radio.unit))
                    entities.extend(
                        Ac100RadioSensor(coordinator, description, ap.mac, radio)
                        for description in RADIO_SENSORS
                    )
        if entities:
            async_add_entities(entities)

    async_add_new_entities()
    entry.async_on_unload(coordinator.async_add_listener(async_add_new_entities))
    add_monitoring_sensors(entry, async_add_entities)
    add_configuration_entities(entry, async_add_entities, binary=False)
    add_radio_settings(entry, async_add_entities, binary=False)
    add_wireless_service_sensors(entry, async_add_entities)


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
    def native_value(self) -> int | float | datetime | None:
        """Return the value from the most recent shared snapshot."""
        return self.entity_description.value_fn(self.coordinator.data)


class Ac100ApTelemetry(Ac100Entity, SensorEntity):
    """Live AP telemetry; the controller keeps old readings for offline and connecting APs."""

    @property
    def online_access_point(self) -> AccessPoint | None:
        """Return the AP only while it is online."""
        ap = self.access_point
        return ap if ap is not None and ap.is_online else None

    @property
    def available(self) -> bool:
        """Do not present offline AP readings as live."""
        return super().available and self.online_access_point is not None


class Ac100ApSensor(Ac100ApTelemetry):
    """A value of one AP, identified by the AP's MAC."""

    entity_description: Ac100ApSensorDescription

    def __init__(
        self,
        coordinator: Ac100Coordinator,
        description: Ac100ApSensorDescription,
        ap_mac: str,
    ) -> None:
        """Initialize an AP sensor with its stable MAC identity."""
        super().__init__(coordinator, description.key, ap_mac)
        self.entity_description = description

    @property
    def native_value(self) -> int | str | datetime | None:
        """Ignore values retained by the controller for offline APs."""
        if (ap := self.online_access_point) is None:
            return None
        return self.entity_description.value_fn(ap)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Return the radio details that the count sensor exposed before radio sensors."""
        if (
            self.entity_description.attributes_fn is None
            or (ap := self.online_access_point) is None
        ):
            return None
        return self.entity_description.attributes_fn(ap)


class Ac100ApClientSpeed(Ac100ApTelemetry):
    """The total speed of the clients that report an AP's name.

    The controller tells only the name of the AP that a client uses. While another AP has the
    same name, the clients cannot be told apart, so the reading is unavailable.
    """

    entity_description: Ac100ApSpeedDescription

    def __init__(
        self,
        coordinator: Ac100Coordinator,
        description: Ac100ApSpeedDescription,
        ap_mac: str,
    ) -> None:
        """Initialize a speed sensor with the AP's stable MAC identity."""
        super().__init__(coordinator, description.key, ap_mac)
        self.entity_description = description

    @property
    def clients(self) -> list[Client] | None:
        """Return the clients of the AP while its name identifies it, else None."""
        if (ap := self.online_access_point) is None:
            return None
        snapshot = self.coordinator.data
        if not ap.name or any(
            other.name == ap.name for other in snapshot.aps if other.mac != ap.mac
        ):
            return None
        return [client for client in snapshot.clients if client.ap_name == ap.name]

    @property
    def available(self) -> bool:
        """Only an online AP with a name of its own has a reading."""
        return super().available and self.clients is not None

    @property
    def native_value(self) -> float | None:
        """Return the total of the clients' known speeds in KB/s."""
        if (clients := self.clients) is None:
            return None
        return _speed_total(clients, self.entity_description.field)


class Ac100RadioSensor(Ac100ApTelemetry):
    """A value of one AP radio, identified by the radio's unit and not by list position."""

    entity_description: Ac100RadioSensorDescription

    def __init__(
        self,
        coordinator: Ac100Coordinator,
        description: Ac100RadioSensorDescription,
        ap_mac: str,
        radio: ApRadio,
    ) -> None:
        """Initialize a radio sensor; the band is a display label, not an identity."""
        super().__init__(coordinator, f"radio_{radio.unit}_{description.key}", ap_mac)
        self.entity_description = description
        self._radio_unit = radio.unit
        self._attr_translation_placeholders = {"band": band_label(radio)}

    @property
    def radio(self) -> ApRadio | None:
        """Return the radio the AP reports now, or None once it is offline or gone."""
        if (ap := self.online_access_point) is None:
            return None
        return next((radio for radio in ap.radios if radio.unit == self._radio_unit), None)

    @property
    def available(self) -> bool:
        """A radio that the AP no longer reports has no live reading."""
        return super().available and self.radio is not None

    @property
    def native_value(self) -> int | float | None:
        """Return the reading, or unknown when the controller has none."""
        if (radio := self.radio) is None:
            return None
        return self.entity_description.value_fn(radio)
