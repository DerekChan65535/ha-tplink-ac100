"""Optional native Wi-Fi client presence tracking."""

import re
from typing import Any

from homeassistant.components.device_tracker import ScannerEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .coordinator import Ac100ConfigEntry, Ac100Coordinator
from .models import Client

_MAC = re.compile(r"(?:[0-9a-f]{2}:){5}[0-9a-f]{2}")


async def async_setup_entry(
    hass: HomeAssistant, entry: Ac100ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator = entry.runtime_data
    _async_release_client_devices(hass, entry)
    known: set[str] = set()
    restored = {tracker.unique_id: tracker for tracker in _async_client_trackers(hass, entry)}

    @callback
    def add_clients() -> None:
        current = {client.mac for client in coordinator.data.clients} | set(restored)
        new = current - known
        known.update(new)
        async_add_entities(
            Ac100ClientTracker(coordinator, mac, restored.get(mac)) for mac in sorted(new)
        )

    add_clients()
    entry.async_on_unload(coordinator.async_add_listener(add_clients))


@callback
def _async_client_trackers(hass: HomeAssistant, entry: Ac100ConfigEntry) -> list[er.RegistryEntry]:
    """Return this entry's registered trackers, whose unique ID is the client MAC."""
    return [
        tracker
        for tracker in er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)
        if tracker.domain == "device_tracker" and _MAC.fullmatch(tracker.unique_id)
    ]


@callback
def _async_release_client_devices(hass: HomeAssistant, entry: Ac100ConfigEntry) -> None:
    """Detach trackers from the MAC-only devices that earlier versions created for them.

    A device is removed only when it exists for one tracker that was never enabled: it belongs
    to this entry alone, is not the controller or an AP, has no user settings and no other
    entity. Every other tracker keeps its device, which a user may have renamed, placed in an
    area or used in an automation. The tracker is detached first, as removing a device also
    removes the entities of its own config entry that are still attached to it.
    """
    entity_registry = er.async_get(hass)
    device_registry = dr.async_get(hass)
    for tracker in _async_client_trackers(hass, entry):
        if (
            tracker.device_id is None
            or tracker.disabled_by is not er.RegistryEntryDisabler.INTEGRATION
        ):
            continue
        device = device_registry.async_get(
            tracker.device_id, include_child_devices=False, include_composite_devices=False
        )
        if device is None or not _exists_only_for(device, tracker, entry, entity_registry):
            continue
        entity_registry.async_update_entity(
            tracker.entity_id, device_id=None, original_name=device.name
        )
        device_registry.async_remove_device(device.id)


def _exists_only_for(
    device: dr.DeviceEntry,
    tracker: er.RegistryEntry,
    entry: Ac100ConfigEntry,
    entity_registry: er.EntityRegistry,
) -> bool:
    """Return whether a device holds nothing but this tracker and none of a user's choices."""
    attached = er.async_entries_for_device(
        entity_registry, device.id, include_disabled_entities=True
    )
    return (
        device.config_entry_id == entry.entry_id
        and device.composite_device_id is None
        and not device.identifiers
        and device.connections == {(dr.CONNECTION_NETWORK_MAC, tracker.unique_id)}
        and device.name_by_user is None
        and device.area_id is None
        and not device.labels
        and device.disabled_by is None
        and [entity.entity_id for entity in attached] == [tracker.entity_id]
    )


class Ac100ClientTracker(CoordinatorEntity[Ac100Coordinator], ScannerEntity):
    """Presence is based on the controller's observed association list.

    A tracker has no device. Only one that an earlier version attached to a device keeps it.
    """

    _attr_has_entity_name = True
    _attr_name = None
    _attr_entity_registry_enabled_default = False

    def __init__(
        self, coordinator: Ac100Coordinator, mac: str, previous: er.RegistryEntry | None = None
    ) -> None:
        super().__init__(coordinator)
        self._attr_mac_address = mac
        self._has_device = previous is not None and previous.device_id is not None
        if not self._has_device:
            client = self.client
            self._attr_name = (
                (client and client.name) or (previous and previous.original_name) or mac
            )

    @callback
    def _handle_coordinator_update(self) -> None:
        if not self._has_device and (client := self.client) and client.name:
            self._attr_name = client.name
        super()._handle_coordinator_update()

    @property
    def client(self) -> Client | None:
        return next(
            (client for client in self.coordinator.data.clients if client.mac == self.mac_address),
            None,
        )

    @property
    def is_connected(self) -> bool:
        return self.client is not None

    @property
    def ip_address(self) -> str | None:
        return self.client.ip or None if self.client else None

    @property
    def hostname(self) -> str | None:
        return self.client.name if self.client else None

    @property
    def device_info(self) -> dr.DeviceInfo:
        if not self._has_device:
            return dr.DeviceInfo()
        connections = {(dr.CONNECTION_NETWORK_MAC, self.mac_address)}
        if client := self.client:
            return dr.DeviceInfo(
                connections=connections,
                name=client.name or client.mac,
                via_device_id=self.coordinator.controller_device_id,
            )
        return dr.DeviceInfo(
            connections=connections, via_device_id=self.coordinator.controller_device_id
        )

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        if not (client := self.client):
            return {}
        strengths = [radio.rssi for radio in client.radios if radio.rssi is not None]
        return {
            "ssid": client.ssid,
            "access_point": client.ap_name,
            "signal_strength": max(strengths) if strengths else None,
            "radios": [
                {
                    "frequency": radio.frequency,
                    "signal_strength": radio.rssi,
                    "negotiated_rate_mbps": radio.negotiated_rate,
                }
                for radio in client.radios
            ],
        }
