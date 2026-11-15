"""Local Home Assistant support for TP-Link TL-AC100 controllers."""

from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import Ac100Api
from .const import DOMAIN, PLATFORMS
from .coordinator import Ac100ConfigEntry, Ac100Coordinator
from .device_info import ap_device_info, controller_device_info
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
    try:
        await coordinator.async_config_entry_first_refresh()
        device = registry.async_get_or_create_from_info(
            config_entry_id=entry.entry_id, device_info=controller_device_info(coordinator)
        )
        coordinator.controller_device_id = device.id
        entry.runtime_data = coordinator
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    except BaseException:
        await coordinator.async_shutdown()
        raise

    @callback
    def refresh_devices() -> None:
        """Keep device fields current, as entities describe their device only when added."""
        if not coordinator.last_update_success:
            return
        infos = [controller_device_info(coordinator)]
        infos.extend(ap_device_info(coordinator, ap) for ap in coordinator.data.aps)
        for info in infos:
            registry.async_get_or_create_from_info(config_entry_id=entry.entry_id, device_info=info)

    entry.async_on_unload(coordinator.async_add_listener(refresh_devices))
    return True


async def async_remove_config_entry_device(
    hass: HomeAssistant, entry: Ac100ConfigEntry, device: dr.DeviceEntry
) -> bool:
    """Allow deleting only devices that the controller no longer reports.

    A wireless network counts as reported while the latest SSID read lists it.
    """
    if entry.state is not ConfigEntryState.LOADED:
        return False
    data = entry.runtime_data.data
    mac = data.controller.mac
    services = data.monitoring.wireless_services if data.monitoring is not None else None
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
