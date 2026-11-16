"""Read-only readings on each Wi-Fi client's own device page, enabled by default.

They come from the shared snapshot's client list; no further read from the controller is needed.
"""

import ipaddress
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import SIGNAL_STRENGTH_DECIBELS_MILLIWATT, EntityCategory, UnitOfDataRate
from homeassistant.core import callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .client_entity import Ac100ClientEntity, known_client_macs
from .coordinator import Ac100ConfigEntry, Ac100Coordinator
from .models import Client

# The controller's wireless terminal identification page lists these brands and systems. Any
# other identification is shown as the controller reports it.
_BRANDS = {
    "apple": "Apple",
    "samsung": "Samsung",
    "microsoft": "Microsoft",
    "nokia": "Nokia",
    "huawei": "Huawei",
    "vivo": "vivo",
    "oppo": "OPPO",
    "xiaomi": "Xiaomi",
    "honor": "Honor",
}
_SYSTEMS = {
    "ios": "iOS",
    "windows": "Windows",
    "android": "Android",
    "symbian": "Symbian",
    "harmonyos": "HarmonyOS",
}


def _address(value: str, version: int) -> str | None:
    """The address if it is a real one of this IP version; 0.0.0.0, :: and --- mean none."""
    text = value.strip()
    try:
        address = ipaddress.ip_address(text)
    except ValueError:
        return None
    return None if address.version != version or address.is_unspecified else text


def _text(value: str) -> str | None:
    """Text as the controller reports it, unknown when it is blank."""
    return value if value.strip() else None


def _bands(client: Client) -> str | None:
    """The distinct band names that the controller reports, such as 2.4GHz."""
    names = dict.fromkeys(radio.frequency.strip() for radio in client.radios)
    return ", ".join(name for name in names if name and name != "---") or None


def _strongest_signal(client: Client) -> int | None:
    return max((radio.rssi for radio in client.radios if radio.rssi is not None), default=None)


def _highest_link_rate(client: Client) -> float | None:
    """The best negotiated rate of the client's radios; it is not the traffic."""
    return max(
        (radio.negotiated_rate for radio in client.radios if radio.negotiated_rate is not None),
        default=None,
    )


def _identification(names: dict[str, str], value: str | None) -> str | None:
    return None if value is None else names.get(value.casefold(), value)


def _radio_records(client: Client) -> dict[str, Any]:
    """Every radio that the controller reports, whichever one the reading is taken from."""
    return {
        "radios": [
            {
                "unit": radio.unit,
                "frequency": radio.frequency,
                "signal_strength": radio.rssi,
                "negotiated_rate_mbps": radio.negotiated_rate,
            }
            for radio in client.radios
        ]
    }


@dataclass(frozen=True, kw_only=True)
class Ac100ClientSensorDescription(SensorEntityDescription):
    """Describe a reading of one client in the current snapshot."""

    value_fn: Callable[[Client], str | int | float | None]
    attributes_fn: Callable[[Client], dict[str, Any]] | None = None


CLIENT_SENSORS = (
    Ac100ClientSensorDescription(
        key="signal_strength",
        translation_key="client_signal_strength",
        device_class=SensorDeviceClass.SIGNAL_STRENGTH,
        native_unit_of_measurement=SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=_strongest_signal,
        attributes_fn=_radio_records,
    ),
    Ac100ClientSensorDescription(
        key="link_rate",
        translation_key="client_link_rate",
        device_class=SensorDeviceClass.DATA_RATE,
        native_unit_of_measurement=UnitOfDataRate.MEGABITS_PER_SECOND,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=_highest_link_rate,
        attributes_fn=_radio_records,
    ),
    Ac100ClientSensorDescription(
        key="wifi_upload_speed",
        translation_key="wifi_upload_speed",
        device_class=SensorDeviceClass.DATA_RATE,
        native_unit_of_measurement=UnitOfDataRate.KILOBYTES_PER_SECOND,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda client: client.upload_kilobytes_per_second,
    ),
    Ac100ClientSensorDescription(
        key="wifi_download_speed",
        translation_key="wifi_download_speed",
        device_class=SensorDeviceClass.DATA_RATE,
        native_unit_of_measurement=UnitOfDataRate.KILOBYTES_PER_SECOND,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda client: client.download_kilobytes_per_second,
    ),
    Ac100ClientSensorDescription(
        key="ipv4_address",
        translation_key="client_ipv4_address",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda client: _address(client.ip, 4),
    ),
    Ac100ClientSensorDescription(
        key="ipv6_address",
        translation_key="client_ipv6_address",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda client: _address(client.ipv6, 6),
    ),
    Ac100ClientSensorDescription(
        key="ssid",
        translation_key="client_ssid",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda client: _text(client.ssid),
    ),
    Ac100ClientSensorDescription(
        key="access_point",
        translation_key="client_access_point",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda client: _text(client.ap_name),
    ),
    Ac100ClientSensorDescription(
        key="wifi_band",
        translation_key="client_wifi_band",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=_bands,
    ),
    Ac100ClientSensorDescription(
        key="vlan_id",
        translation_key="client_vlan_id",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda client: client.vlan_id,
    ),
    Ac100ClientSensorDescription(
        key="vendor",
        translation_key="client_vendor",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda client: _identification(_BRANDS, client.brand),
    ),
    Ac100ClientSensorDescription(
        key="operating_system",
        translation_key="client_operating_system",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda client: _identification(_SYSTEMS, client.operating_system),
    ),
    Ac100ClientSensorDescription(
        key="connection_time",
        translation_key="client_connection_time",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda client: client.connected_at,
    ),
)


def add_client_sensors(
    entry: Ac100ConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    """Add the readings for every current, restored and later discovered client."""
    _add_for_each_client(
        entry,
        async_add_entities,
        lambda coordinator, mac: [
            Ac100ClientSensor(coordinator, mac, description) for description in CLIENT_SENSORS
        ],
    )


def add_client_connectivity(
    entry: Ac100ConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    """Add the connectivity sensor for every current, restored and later discovered client."""
    _add_for_each_client(
        entry,
        async_add_entities,
        lambda coordinator, mac: [Ac100ClientConnectivity(coordinator, mac)],
    )


def _add_for_each_client(
    entry: Ac100ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
    create: Callable[[Ac100Coordinator, str], list[Any]],
) -> None:
    """Create entities once per client MAC; they stay while the client is away."""
    coordinator = entry.runtime_data
    known: set[str] = set()

    @callback
    def add_clients() -> None:
        new = known_client_macs(coordinator) - known
        known.update(new)
        if entities := [entity for mac in sorted(new) for entity in create(coordinator, mac)]:
            async_add_entities(entities)

    add_clients()
    entry.async_on_unload(coordinator.async_add_listener(add_clients))


class Ac100ClientSensor(Ac100ClientEntity, SensorEntity):
    """A reading of one client: unknown if the controller reports none, unavailable if away."""

    entity_description: Ac100ClientSensorDescription
    # The radio records change with every signal reading; keep them out of the recorder.
    _unrecorded_attributes = frozenset({"radios"})

    def __init__(
        self,
        coordinator: Ac100Coordinator,
        mac: str,
        description: Ac100ClientSensorDescription,
    ) -> None:
        super().__init__(coordinator, mac, description.key)
        self.entity_description = description

    @property
    def native_value(self) -> str | int | float | None:
        if (client := self.client) is None:
            return None
        return self.entity_description.value_fn(client)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        if self.entity_description.attributes_fn is None or (client := self.client) is None:
            return None
        return self.entity_description.attributes_fn(client)


class Ac100ClientConnectivity(Ac100ClientEntity, BinarySensorEntity):
    """Whether the controller lists the client now, unavailable only after a failed poll."""

    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_translation_key = "connectivity"

    def __init__(self, coordinator: Ac100Coordinator, mac: str) -> None:
        super().__init__(coordinator, mac, "connectivity")

    @property
    def available(self) -> bool:
        """A client that is away is known to be disconnected, so it is not unavailable."""
        return self.coordinator.last_update_success

    @property
    def is_on(self) -> bool:
        return self.client is not None
