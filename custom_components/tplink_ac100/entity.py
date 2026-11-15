"""Shared native entity helpers for TP-Link AC100."""

from homeassistant.helpers.device_registry import CONNECTION_NETWORK_MAC, DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import Ac100Coordinator
from .models import AccessPoint


class Ac100Entity(CoordinatorEntity[Ac100Coordinator]):
    """An entity belonging to the controller or one of its access points."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: Ac100Coordinator, key: str, ap_mac: str | None = None) -> None:
        super().__init__(coordinator)
        self.ap_mac = ap_mac
        controller_mac = coordinator.data.controller.mac
        self._attr_unique_id = f"{controller_mac}_{ap_mac or 'controller'}_{key}"

    @property
    def access_point(self) -> AccessPoint | None:
        """Return the current access point, never a stale response object."""
        return next((ap for ap in self.coordinator.data.aps if ap.mac == self.ap_mac), None)

    @property
    def available(self) -> bool:
        return super().available and (self.ap_mac is None or self.access_point is not None)

    @property
    def device_info(self) -> DeviceInfo:
        if self.ap_mac is None:
            return DeviceInfo(identifiers={(DOMAIN, self.coordinator.data.controller.mac)})
        if (ap := self.access_point) is None:
            return DeviceInfo(identifiers={(DOMAIN, self.ap_mac)})
        return DeviceInfo(
            identifiers={(DOMAIN, self.ap_mac)},
            connections={(CONNECTION_NETWORK_MAC, self.ap_mac)},
            name=ap.name,
            manufacturer="TP-Link",
            model=ap.model,
            via_device_id=self.coordinator.controller_device_id,
        )
