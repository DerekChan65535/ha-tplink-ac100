"""Allow-listed diagnostics without credentials or network identities."""

from typing import Any

from homeassistant.core import HomeAssistant

from .const import CONF_ENABLE_CONTROL, UPDATE_INTERVAL
from .coordinator import Ac100ConfigEntry


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: Ac100ConfigEntry
) -> dict[str, Any]:
    """Do not include raw responses or config-entry data in downloadable diagnostics."""
    coordinator = entry.runtime_data
    controller = coordinator.data.controller
    return {
        "model": controller.model,
        "hardware_version": controller.hardware_version,
        "firmware_version": controller.firmware_version,
        "cpu_usage": controller.cpu_usage,
        "memory_usage": controller.memory_usage,
        "uptime": controller.uptime,
        "last_update_success": coordinator.last_update_success,
        "poll_interval_seconds": int(UPDATE_INTERVAL.total_seconds()),
        "management_enabled": entry.options.get(CONF_ENABLE_CONTROL, False),
        "access_points": {
            "total": len(coordinator.data.aps),
            "online": sum(ap.is_online for ap in coordinator.data.aps),
        },
        "connected_clients": len(coordinator.data.clients),
    }
