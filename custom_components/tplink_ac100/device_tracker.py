"""Optional native Wi-Fi client presence tracking."""

from typing import Any

from homeassistant.components.device_tracker import ScannerEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .client_entity import known_client_macs
from .coordinator import Ac100ConfigEntry, Ac100Coordinator
from .device_info import client_device_info, current_client
from .models import Client


async def async_setup_entry(
    hass: HomeAssistant, entry: Ac100ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator = entry.runtime_data
    known: set[str] = set()

    @callback
    def add_clients() -> None:
        new = known_client_macs(coordinator) - known
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
        return current_client(self.coordinator, self.mac_address)

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
    def device_info(self) -> DeviceInfo:
        """Describe the client's device, which every other entity of the client shares."""
        return client_device_info(self.coordinator, self.mac_address)

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
