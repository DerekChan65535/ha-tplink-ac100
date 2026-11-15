"""Firmware availability only; installation stays in the controller's website."""

import re

from homeassistant.components.update import (
    UpdateDeviceClass,
    UpdateEntity,
    UpdateEntityFeature,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import Ac100ConfigEntry, Ac100Coordinator
from .entity import Ac100Entity
from .monitoring import ApFirmware

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: Ac100ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Discover firmware without enabling installation or checking outside the AC."""
    coordinator = entry.runtime_data
    async_add_entities([Ac100FirmwareUpdate(coordinator)])
    known: set[str] = set()

    @callback
    def add_access_points() -> None:
        monitoring = coordinator.data.monitoring
        if monitoring is None:
            return
        current = {ap.mac for ap in coordinator.data.aps}
        new = {item.mac for item in monitoring.ap_firmware} & current - known
        known.update(new)
        if new:
            async_add_entities(Ac100FirmwareUpdate(coordinator, mac) for mac in sorted(new))

    add_access_points()
    entry.async_on_unload(coordinator.async_add_listener(add_access_points))


class Ac100FirmwareUpdate(Ac100Entity, UpdateEntity):
    """A native update entity deliberately without INSTALL support."""

    _attr_translation_key = "firmware"
    _attr_device_class = UpdateDeviceClass.FIRMWARE
    _attr_entity_category = EntityCategory.CONFIG
    _attr_supported_features = UpdateEntityFeature(0)

    def __init__(self, coordinator: Ac100Coordinator, ap_mac: str | None = None) -> None:
        super().__init__(coordinator, "firmware", ap_mac)

    @property
    def access_point_firmware(self) -> ApFirmware | None:
        if (monitoring := self.coordinator.data.monitoring) is None:
            return None
        return next((item for item in monitoring.ap_firmware if item.mac == self.ap_mac), None)

    @property
    def available(self) -> bool:
        if not super().available or (monitoring := self.coordinator.data.monitoring) is None:
            return False
        if self.ap_mac is None:
            return monitoring.firmware is not None
        return (
            self.access_point_firmware is not None
            and (ap := self.access_point) is not None
            and ap.is_online
        )

    @property
    def installed_version(self) -> str | None:
        if self.ap_mac is None:
            return self.coordinator.data.controller.firmware_version or None
        return item.firmware_version if (item := self.access_point_firmware) else None

    @property
    def latest_version(self) -> str | None:
        """The controller's announced version; without an announcement, the installed one."""
        if self.ap_mac is None:
            monitoring = self.coordinator.data.monitoring
            if monitoring is None or monitoring.firmware is None:
                return None
            return monitoring.firmware.latest_version or self.installed_version
        if (item := self.access_point_firmware) is None:
            return None
        return item.latest_version or item.firmware_version

    def version_is_newer(self, latest_version: str, installed_version: str) -> bool:
        """Compare the numeric part of versions like "1.0.7 Build 260208 Rel.50128n"."""
        latest, installed = _version_key(latest_version), _version_key(installed_version)
        if latest is None or installed is None:
            return latest_version != installed_version
        return latest > installed


def _version_key(version: str) -> tuple[int, ...] | None:
    match = re.match(r"(\d+(?:\.\d+)*)(?: Build (\d+))?", version)
    if match is None:
        return None
    numbers = tuple(int(part) for part in match[1].split("."))
    return numbers + ((int(match[2]),) if match[2] else ())
