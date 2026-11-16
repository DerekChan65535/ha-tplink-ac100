"""Local Home Assistant support for TP-Link TL-AC100 controllers."""

from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import Ac100Api
from .client_entity import known_client_macs
from .const import CONF_MONITOR_WIRELESS_SERVICES, DOMAIN, PLATFORMS
from .coordinator import Ac100ConfigEntry, Ac100Coordinator
from .device_info import ap_device_info, client_device_info, controller_device_info
from .wireless_service import wireless_service_device_key


async def async_setup_entry(hass: HomeAssistant, entry: Ac100ConfigEntry) -> bool:
    """Initialize one controller without changing its configuration."""
    api = Ac100Api(
        async_get_clientsession(hass),
        entry.data[CONF_HOST],
        entry.data[CONF_USERNAME],
        entry.data[CONF_PASSWORD],
    )
    coordinator = Ac100Coordinator(hass, entry, api)
    registry = dr.async_get(hass)

    @callback
    def refresh_devices() -> None:
        """Keep the devices and their links current, as entities describe a device only when added.

        The controller has the APs and each AP has its clients, so parents are registered
        before their children. A client's device does not wait for any of its entities: the
        optional tracker is usually disabled, and the links must not depend on it.
        """
        if not coordinator.last_update_success:
            return
        controller = registry.async_get_or_create_from_info(
            config_entry_id=entry.entry_id, device_info=controller_device_info(coordinator)
        )
        coordinator.controller_device_id = controller.id
        for ap in coordinator.data.aps:
            registry.async_get_or_create_from_info(
                config_entry_id=entry.entry_id, device_info=ap_device_info(coordinator, ap)
            )
        for mac in sorted(known_client_macs(coordinator)):
            registry.async_get_or_create_from_info(
                config_entry_id=entry.entry_id, device_info=client_device_info(coordinator, mac)
            )

    try:
        await coordinator.async_config_entry_first_refresh()
        entry.runtime_data = coordinator
        refresh_devices()
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    except BaseException:
        await coordinator.async_shutdown()
        raise

    entry.async_on_unload(coordinator.async_add_listener(refresh_devices))
    return True


async def async_remove_config_entry_device(
    hass: HomeAssistant, entry: Ac100ConfigEntry, device: dr.DeviceEntry
) -> bool:
    """Allow deleting only devices that the controller no longer reports.

    A wireless network counts as reported while the latest SSID read lists it, and also while
    that read failed, since its state is then unknown.
    """
    if entry.state is not ConfigEntryState.LOADED:
        return False
    data = entry.runtime_data.data
    mac = data.controller.mac
    services = data.monitoring.wireless_services if data.monitoring is not None else None
    ssid_prefix = wireless_service_device_key(mac, "")
    if (
        services is None
        and entry.options.get(CONF_MONITOR_WIRELESS_SERVICES, False)
        and any(
            value.startswith(ssid_prefix)
            for domain, value in device.identifiers
            if domain == DOMAIN
        )
    ):
        return False
    current = {
        mac,
        *(ap.mac for ap in data.aps),
        *(client.mac for client in data.clients),
        *(wireless_service_device_key(mac, item.service_id) for item in services or ()),
    }
    addresses = {value for domain, value in device.identifiers if domain == DOMAIN} | {
        value for kind, value in device.connections if kind == dr.CONNECTION_NETWORK_MAC
    }
    return not addresses & current


async def async_unload_entry(hass: HomeAssistant, entry: Ac100ConfigEntry) -> None:
    """Stop polling before releasing only this entry's own management session."""
    await hass.config_entries.async_unload_platforms_or_raise(entry, PLATFORMS)
    coordinator = entry.runtime_data
    await coordinator.async_shutdown()
