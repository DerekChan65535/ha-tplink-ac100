"""Shared base of the native entities that describe one Wi-Fi client."""

import re

from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .coordinator import Ac100Coordinator
from .device_info import client_device_info, current_client
from .models import Client

_MAC = re.compile(r"(?:[0-9a-f]{2}:){5}[0-9a-f]{2}")


def known_client_macs(coordinator: Ac100Coordinator) -> set[str]:
    """Return the MACs of the clients that have a device: the current ones and restored ones.

    A tracker's unique ID is its client's MAC, and its registry entry outlives the client's
    visit. A client that is away keeps its device and entities through these entries. The
    controller and its APs have devices of their own and are never clients.
    """
    snapshot = coordinator.data
    registry = er.async_get(coordinator.hass)
    restored = {
        entity.unique_id
        for entity in er.async_entries_for_config_entry(registry, coordinator.entry.entry_id)
        if entity.domain == "device_tracker" and _MAC.fullmatch(entity.unique_id)
    }
    infrastructure = {snapshot.controller.mac, *(ap.mac for ap in snapshot.aps)}
    return ({client.mac for client in snapshot.clients} | restored) - infrastructure


class Ac100ClientEntity(CoordinatorEntity[Ac100Coordinator]):
    """An entity of one client, on the client's own device and addressed by its MAC.

    The unique ID is kept apart from the optional tracker, whose unique ID is the bare MAC. The
    device is created and kept current by the integration's shared device refresh, not by the
    entities, so it exists whether or not any entity of the client is enabled.
    """

    _attr_has_entity_name = True

    def __init__(self, coordinator: Ac100Coordinator, mac: str, key: str) -> None:
        super().__init__(coordinator)
        self.mac = mac
        self._attr_unique_id = f"{coordinator.data.controller.mac}_client_{mac}_{key}"

    @property
    def client(self) -> Client | None:
        """Return the client while the controller lists it, never a stale response object."""
        return current_client(self.coordinator, self.mac)

    @property
    def available(self) -> bool:
        return super().available and self.client is not None

    @property
    def device_info(self) -> DeviceInfo:
        return client_device_info(self.coordinator, self.mac)
