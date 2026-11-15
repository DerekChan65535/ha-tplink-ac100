"""Local Home Assistant support for TP-Link TL-AC100 controllers."""

from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import Ac100Api
from .const import DOMAIN, PLATFORMS
from .coordinator import Ac100ConfigEntry, Ac100Coordinator


async def async_setup_entry(hass: HomeAssistant, entry: Ac100ConfigEntry) -> bool:
    """Initialize one controller without changing its configuration."""
    api = Ac100Api(
        async_get_clientsession(hass),
        entry.data[CONF_HOST],
        entry.data[CONF_USERNAME],
        entry.data[CONF_PASSWORD],
    )
    coordinator = Ac100Coordinator(hass, entry, api)
    try:
        await coordinator.async_config_entry_first_refresh()
        controller = coordinator.data.controller
        device = dr.async_get(hass).async_get_or_create(
            config_entry_id=entry.entry_id,
            identifiers={(DOMAIN, controller.mac)},
            connections={(dr.CONNECTION_NETWORK_MAC, controller.mac)},
            manufacturer="TP-Link",
            model=controller.model,
            name=f"TP-Link {controller.model}",
            sw_version=controller.firmware_version,
            hw_version=controller.hardware_version,
        )
        coordinator.controller_device_id = device.id
        entry.runtime_data = coordinator
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    except BaseException:
        await coordinator.async_shutdown()
        raise
    return True


async def async_unload_entry(hass: HomeAssistant, entry: Ac100ConfigEntry) -> None:
    """Stop polling before releasing only this entry's own management session."""
    await hass.config_entries.async_unload_platforms_or_raise(entry, PLATFORMS)
    coordinator = entry.runtime_data
    await coordinator.async_shutdown()
