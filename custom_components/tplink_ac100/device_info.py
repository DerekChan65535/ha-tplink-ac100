"""Device registry fields for the controller, its access points and their clients."""

from collections import Counter
from collections.abc import Iterable

from homeassistant.const import CONF_HOST
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.device_registry import CONNECTION_NETWORK_MAC, DeviceInfo
from homeassistant.helpers.typing import UNDEFINED, UndefinedType

from .api import normalize_host
from .const import DOMAIN
from .coordinator import Ac100Coordinator
from .models import AccessPoint, Client
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


def current_client(coordinator: Ac100Coordinator, mac: str) -> Client | None:
    """Return a client from the latest snapshot, never a stale response object."""
    return next((client for client in coordinator.data.clients if client.mac == mac), None)


def client_device_info(coordinator: Ac100Coordinator, mac: str) -> DeviceInfo:
    """Describe a client's device behind the AP named by the controller.

    The MAC stays the device's only identity. Its parent is known only while the client is
    present and exactly one AP record, including offline APs, has the exact reported name.
    An absent or uncertain client has no parent; the controller is never a fallback.
    """
    client = current_client(coordinator, mac)
    return DeviceInfo(
        connections={(CONNECTION_NETWORK_MAC, mac)},
        name=_client_name(coordinator, mac, client),
        via_device_id=_client_parent_id(coordinator, client),
    )


def _client_parent_id(coordinator: Ac100Coordinator, client: Client | None) -> str | None:
    """Return the device ID of the one AP whose name is the client's AP name."""
    if client is None or not client.ap_name:
        return None
    matches = [ap for ap in coordinator.data.aps if ap.name == client.ap_name]
    if len(matches) != 1:
        return None
    parent = dr.async_get(coordinator.hass).async_get_device_by_identifier(
        (DOMAIN, matches[0].mac), coordinator.entry.entry_id
    )
    return parent.id if parent else None


def _client_name(
    coordinator: Ac100Coordinator, mac: str, client: Client | None
) -> str | UndefinedType:
    """Return the name for a client's device, or UNDEFINED to leave an existing name as it is.

    A client that the controller names gives its device that name. The MAC names a device only
    when nothing better is known: an away or unnamed client keeps the name its device has, then
    the name a tracker of version 0.2.0 kept, and only then is called by its MAC.
    """
    if client is not None and (client.name or "").strip():
        return client.name
    device = dr.async_get(coordinator.hass).async_get_device_by_connection(
        (CONNECTION_NETWORK_MAC, mac), coordinator.entry.entry_id
    )
    if device is not None and _is_given_name(device.name, mac):
        return UNDEFINED
    entities = er.async_get(coordinator.hass)
    tracker_id = entities.async_get_entity_id("device_tracker", DOMAIN, mac)
    tracker = entities.async_get(tracker_id) if tracker_id else None
    if (
        tracker is not None
        and tracker.config_entry_id == coordinator.entry.entry_id
        and _is_given_name(tracker.original_name, mac)
    ):
        return tracker.original_name
    return mac


def _is_given_name(name: str | None, mac: str) -> bool:
    """Return whether a name says more than the MAC address."""
    return bool(name and name.strip() and name != mac)


def _mac_suffix(mac: str) -> str:
    return mac.replace(":", "")[-6:].upper()


def _ap_firmware(coordinator: Ac100Coordinator, mac: str) -> ApFirmware | None:
    """Return an AP's firmware only while the read that provides it is current."""
    if (monitoring := coordinator.data.monitoring) is None:
        return None
    return next((item for item in monitoring.ap_firmware if item.mac == mac), None)
