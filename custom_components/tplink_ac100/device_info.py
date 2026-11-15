"""Device registry fields for the controller and its access points."""

from collections import Counter
from collections.abc import Iterable

from homeassistant.const import CONF_HOST
from homeassistant.helpers.device_registry import CONNECTION_NETWORK_MAC, DeviceInfo
from homeassistant.helpers.typing import UNDEFINED

from .api import normalize_host
from .const import DOMAIN
from .coordinator import Ac100Coordinator
from .models import AccessPoint
from .monitoring import ApFirmware

MANUFACTURER = "TP-Link"


def controller_device_info(coordinator: Ac100Coordinator) -> DeviceInfo:
    """Describe the controller; the model is its name because the manufacturer is separate."""
    controller = coordinator.data.controller
    return DeviceInfo(
        identifiers={(DOMAIN, controller.mac)},
        connections={(CONNECTION_NETWORK_MAC, controller.mac)},
        manufacturer=MANUFACTURER,
        model=controller.model,
        name=controller.model,
        configuration_url=normalize_host(coordinator.entry.data[CONF_HOST]),
        sw_version=controller.firmware_version,
        hw_version=controller.hardware_version,
    )


def ap_device_names(aps: Iterable[AccessPoint]) -> dict[str, str]:
    """Return a device name for each AP MAC.

    Names come from the controller and are not translated. A blank name becomes the model and
    the last six MAC digits. Duplicate names, compared without regard to case, all get those
    digits too, so the devices and the entity IDs derived from them stay distinguishable.
    """
    names = {ap.mac: ap.name.strip() or f"{ap.model} {_mac_suffix(ap.mac)}".strip() for ap in aps}
    counts = Counter(name.casefold() for name in names.values())
    return {
        mac: f"{name} {_mac_suffix(mac)}" if counts[name.casefold()] > 1 else name
        for mac, name in names.items()
    }


def ap_device_info(coordinator: Ac100Coordinator, ap: AccessPoint) -> DeviceInfo:
    """Describe an AP; it has no web page of its own, so the controller's page is its URL."""
    firmware = _ap_firmware(coordinator, ap.mac)
    return DeviceInfo(
        identifiers={(DOMAIN, ap.mac)},
        connections={(CONNECTION_NETWORK_MAC, ap.mac)},
        manufacturer=MANUFACTURER,
        model=ap.model,
        name=ap_device_names(coordinator.data.aps)[ap.mac],
        configuration_url=normalize_host(coordinator.entry.data[CONF_HOST]),
        via_device_id=coordinator.controller_device_id,
        sw_version=(firmware and firmware.firmware_version) or UNDEFINED,
        hw_version=(firmware and firmware.hardware_version) or UNDEFINED,
    )


def _mac_suffix(mac: str) -> str:
    return mac.replace(":", "")[-6:].upper()


def _ap_firmware(coordinator: Ac100Coordinator, mac: str) -> ApFirmware | None:
    """Return an AP's firmware only while the read that provides it is current."""
    if (monitoring := coordinator.data.monitoring) is None:
        return None
    return next((item for item in monitoring.ap_firmware if item.mac == mac), None)
