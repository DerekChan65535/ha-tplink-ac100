"""Optional native Wi-Fi client presence tracking."""

import re
from typing import Any

from homeassistant.components.device_tracker import ScannerEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.typing import UNDEFINED
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .coordinator import Ac100ConfigEntry, Ac100Coordinator
from .models import Client

_MAC = re.compile(r"(?:[0-9a-f]{2}:){5}[0-9a-f]{2}")


async def async_setup_entry(
    hass: HomeAssistant, entry: Ac100ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator = entry.runtime_data
    known: set[str] = set()
    registry = er.async_get(hass)
    restored = {
        entity.unique_id
        for entity in er.async_entries_for_config_entry(registry, entry.entry_id)
        if entity.domain == "device_tracker" and _MAC.fullmatch(entity.unique_id)
    }

    @callback
    def add_clients() -> None:
        current = {client.mac for client in coordinator.data.clients} | restored
        new = current - known
        known.update(new)
        async_add_entities(Ac100ClientTracker(coordinator, mac) for mac in sorted(new))

    add_clients()
    entry.async_on_unload(coordinator.async_add_listener(add_clients))


class Ac100ClientTracker(CoordinatorEntity[Ac100Coordinator], ScannerEntity):
    """Presence is based on the controller's observed association list."""

    _attr_has_entity_name = True
    _attr_name = None
    _attr_entity_registry_enabled_default = False

    def __init__(self, coordinator: Ac100Coordinator, mac: str) -> None:
        super().__init__(coordinator)
        self._attr_mac_address = mac

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
        """Describe the client's device, linked to the controller.

        A client that is away leaves the name of its device as it is. If it has no device yet,
        the MAC address names it, as Home Assistant would name any scanned device. Without a
        name the registry would use the title of the config entry.
        """
        connection = (dr.CONNECTION_NETWORK_MAC, self.mac_address)
        name = UNDEFINED
        if client := self.client:
            name = client.name or client.mac
        elif not dr.async_get(self.hass).async_get_device_by_connection(
            connection, self.coordinator.entry.entry_id
        ):
            name = self.mac_address
        return dr.DeviceInfo(
            connections={connection},
            name=name,
            via_device_id=self.coordinator.controller_device_id,
        )

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        if not (client := self.client):
            return {}
        strengths = [radio.rssi for radio in client.radios if radio.rssi is not None]
        attributes: dict[str, Any] = {
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
        if client.upload_kilobytes_per_second is not None:
            attributes["upload_kilobytes_per_second"] = client.upload_kilobytes_per_second
        if client.download_kilobytes_per_second is not None:
            attributes["download_kilobytes_per_second"] = client.download_kilobytes_per_second
        return attributes
