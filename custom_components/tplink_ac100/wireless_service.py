"""Opt-in wireless network (SSID) devices and entities.

With the ``monitor_wireless_services`` option the coordinator also reads the controller's SSID
table. Each network in it becomes a device behind the controller. The entities of that device
belong to the network's ``serv_id``, not to its name, which users can edit on the controller.
"""

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.components.switch import SwitchEntity, SwitchEntityDescription
from homeassistant.const import CONF_HOST, EntityCategory
from homeassistant.core import callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .api import normalize_host
from .const import CONF_MONITOR_WIRELESS_SERVICES, DOMAIN
from .coordinator import Ac100ConfigEntry, Ac100Coordinator
from .device_info import MANUFACTURER
from .entity import Ac100Entity
from .models import Snapshot
from .monitoring import (
    WIRELESS_SERVICE_FLAGS,
    WIRELESS_SERVICE_SECURITY,
    WirelessService,
    WirelessServiceFlag,
)


def wireless_service_device_key(controller_mac: str, service_id: str) -> str:
    """The device identifier of one wireless network, and the start of its entities' unique IDs."""
    return f"{controller_mac}_wlan_{service_id}"


def _services(coordinator: Ac100Coordinator) -> tuple[WirelessService, ...] | None:
    """The networks of the latest SSID read; None while that read is missing or has failed."""
    monitoring = coordinator.data.monitoring
    return None if monitoring is None else monitoring.wireless_services


def wireless_service_device_info(
    coordinator: Ac100Coordinator, service: WirelessService
) -> DeviceInfo:
    """Describe a network as a device behind the controller; the name is the SSID as sent."""
    key = wireless_service_device_key(coordinator.data.controller.mac, service.service_id)
    return DeviceInfo(
        identifiers={(DOMAIN, key)},
        manufacturer=MANUFACTURER,
        model="Wireless network",
        name=service.name,
        configuration_url=normalize_host(coordinator.entry.data[CONF_HOST]),
        via_device_id=coordinator.controller_device_id,
    )


def _on_services(
    entry: Ac100ConfigEntry, handle: Callable[[tuple[WirelessService, ...]], None]
) -> None:
    """Call ``handle`` with the listed networks now and after every poll, while monitored."""
    if not entry.options.get(CONF_MONITOR_WIRELESS_SERVICES, False):
        return
    coordinator = entry.runtime_data

    @callback
    def update() -> None:
        if (services := _services(coordinator)) is not None:
            handle(services)

    update()
    entry.async_on_unload(coordinator.async_add_listener(update))


def _add_when_listed(
    entry: Ac100ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
    build: Callable[[Ac100Coordinator, str], Iterable[Entity]],
) -> None:
    """Add the entities of each network the first time the SSID table lists it."""
    coordinator = entry.runtime_data
    known: set[str] = set()

    def add_new(services: tuple[WirelessService, ...]) -> None:
        entities: list[Entity] = []
        for service in services:
            if service.service_id not in known:
                known.add(service.service_id)
                entities.extend(build(coordinator, service.service_id))
        if entities:
            async_add_entities(entities)

    _on_services(entry, add_new)


def _keep_devices_current(entry: Ac100ConfigEntry) -> None:
    """Update the devices of renamed networks, as entities describe a device only when added."""
    coordinator = entry.runtime_data
    registry = dr.async_get(coordinator.hass)

    def refresh(services: tuple[WirelessService, ...]) -> None:
        if not coordinator.last_update_success:
            return
        for service in services:
            registry.async_get_or_create_from_info(
                config_entry_id=entry.entry_id,
                device_info=wireless_service_device_info(coordinator, service),
            )

    _on_services(entry, refresh)


class Ac100WirelessServiceEntity(Ac100Entity):
    """An entity of one wireless network, which the controller identifies by its service ID."""

    def __init__(self, coordinator: Ac100Coordinator, service_id: str, key: str) -> None:
        super().__init__(coordinator, key)
        self.service_id = service_id
        device_key = wireless_service_device_key(coordinator.data.controller.mac, service_id)
        self._attr_unique_id = f"{device_key}_{key}"

    @property
    def service(self) -> WirelessService | None:
        """The network as the latest SSID read lists it, never a stale object."""
        return next(
            (
                item
                for item in _services(self.coordinator) or ()
                if item.service_id == self.service_id
            ),
            None,
        )

    @property
    def available(self) -> bool:
        return super().available and self.service is not None

    @property
    def device_info(self) -> DeviceInfo:
        if (service := self.service) is None:
            key = wireless_service_device_key(self.coordinator.data.controller.mac, self.service_id)
            return DeviceInfo(identifiers={(DOMAIN, key)})
        return wireless_service_device_info(self.coordinator, service)


@dataclass(frozen=True, kw_only=True)
class WirelessServiceSensorDescription(SensorEntityDescription):
    """Describe a value of one wireless network."""

    value_fn: Callable[[Snapshot, WirelessService], str | int | None]


@dataclass(frozen=True, kw_only=True)
class WirelessServiceBinarySensorDescription(BinarySensorEntityDescription):
    """Describe a state of one wireless network."""

    value_fn: Callable[[WirelessService], bool | None]


WIRELESS_SERVICE_SENSORS = (
    WirelessServiceSensorDescription(
        key="connected_clients",
        translation_key="connected_clients",
        state_class=SensorStateClass.MEASUREMENT,
        # Clients name their SSID, not the service ID.
        value_fn=lambda snapshot, service: sum(
            client.ssid == service.name for client in snapshot.clients
        ),
    ),
    WirelessServiceSensorDescription(
        key="security",
        translation_key="wireless_service_security",
        device_class=SensorDeviceClass.ENUM,
        options=list(WIRELESS_SERVICE_SECURITY.values()),
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda snapshot, service: service.security,
    ),
    WirelessServiceSensorDescription(
        key="vlan",
        translation_key="wireless_service_vlan",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda snapshot, service: service.vlan,
    ),
)

WIRELESS_SERVICE_BINARY_SENSORS = (
    WirelessServiceBinarySensorDescription(
        key="enabled",
        translation_key="wireless_service_enabled",
        value_fn=lambda service: service.enabled,
    ),
    WirelessServiceBinarySensorDescription(
        key="hidden",
        translation_key="wireless_service_hidden",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda service: None if service.broadcast is None else not service.broadcast,
    ),
    WirelessServiceBinarySensorDescription(
        key="isolation",
        translation_key="wireless_service_isolation",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda service: service.isolation,
    ),
)


class Ac100WirelessServiceSensor(Ac100WirelessServiceEntity, SensorEntity):
    """A value of one wireless network."""

    entity_description: WirelessServiceSensorDescription

    def __init__(
        self,
        coordinator: Ac100Coordinator,
        service_id: str,
        description: WirelessServiceSensorDescription,
    ) -> None:
        super().__init__(coordinator, service_id, description.key)
        self.entity_description = description

    @property
    def native_value(self) -> str | int | None:
        if (service := self.service) is None:
            return None
        return self.entity_description.value_fn(self.coordinator.data, service)


class Ac100WirelessServiceBinarySensor(Ac100WirelessServiceEntity, BinarySensorEntity):
    """A read-only state of one wireless network, with unknown represented by None."""

    entity_description: WirelessServiceBinarySensorDescription

    def __init__(
        self,
        coordinator: Ac100Coordinator,
        service_id: str,
        description: WirelessServiceBinarySensorDescription,
    ) -> None:
        super().__init__(coordinator, service_id, description.key)
        self.entity_description = description

    @property
    def is_on(self) -> bool | None:
        if (service := self.service) is None:
            return None
        return self.entity_description.value_fn(service)


class Ac100WirelessServiceSwitch(Ac100WirelessServiceEntity, SwitchEntity):
    """One on/off setting of a wireless network; an action changes nothing else."""

    def __init__(
        self, coordinator: Ac100Coordinator, service_id: str, flag: WirelessServiceFlag
    ) -> None:
        super().__init__(coordinator, service_id, flag.key)
        self.flag = flag
        self.entity_description = SwitchEntityDescription(
            key=flag.key,
            translation_key=f"wireless_service_{flag.key}",
            entity_category=EntityCategory.CONFIG,
            entity_registry_enabled_default=False,
        )

    @property
    def is_on(self) -> bool | None:
        if (service := self.service) is None:
            return None
        return getattr(service, self.flag.key)

    async def _async_set(self, value: bool) -> None:
        async def action() -> None:
            if (service := self.service) is None:
                raise HomeAssistantError(
                    translation_domain=DOMAIN, translation_key="wireless_service_unavailable"
                )
            await self.coordinator.api.async_set_wireless_service(
                service.service_id, self.flag.key, value, name=service.name
            )

        await self.coordinator.async_control(action)

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._async_set(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._async_set(False)


def add_wireless_service_sensors(
    entry: Ac100ConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    """Add SSID sensors once wireless networks are monitored.

    This always-loaded platform also keeps the networks' devices current.
    """
    _keep_devices_current(entry)
    _add_when_listed(
        entry,
        async_add_entities,
        lambda coordinator, service_id: [
            Ac100WirelessServiceSensor(coordinator, service_id, description)
            for description in WIRELESS_SERVICE_SENSORS
        ],
    )


def add_wireless_service_binary_sensors(
    entry: Ac100ConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    """Add SSID binary sensors once wireless networks are monitored."""
    _add_when_listed(
        entry,
        async_add_entities,
        lambda coordinator, service_id: [
            Ac100WirelessServiceBinarySensor(coordinator, service_id, description)
            for description in WIRELESS_SERVICE_BINARY_SENSORS
        ],
    )


def add_wireless_service_switches(
    entry: Ac100ConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    """Add SSID switches once wireless networks are monitored and management is enabled."""
    _add_when_listed(
        entry,
        async_add_entities,
        lambda coordinator, service_id: [
            Ac100WirelessServiceSwitch(coordinator, service_id, flag)
            for flag in WIRELESS_SERVICE_FLAGS
        ],
    )
