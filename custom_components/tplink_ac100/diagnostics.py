"""Allow-listed diagnostics without credentials or network identities."""

from typing import Any

from homeassistant.core import HomeAssistant

from .const import (
    CONF_ENABLE_CONTROL,
    CONF_MONITOR_WIRELESS_SERVICES,
    CONFIGURATION_INTERVAL,
    MONITORING_INTERVAL,
)
from .coordinator import Ac100ConfigEntry


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: Ac100ConfigEntry
) -> dict[str, Any]:
    """Do not include raw responses or config-entry data in downloadable diagnostics."""
    coordinator = entry.runtime_data
    controller = coordinator.data.controller
    monitoring = coordinator.data.monitoring
    configuration = coordinator.data.configuration
    services = monitoring.wireless_services if monitoring else None
    return {
        "model": controller.model,
        "hardware_version": controller.hardware_version,
        "firmware_version": controller.firmware_version,
        "cpu_usage": controller.cpu_usage,
        "memory_usage": controller.memory_usage,
        "uptime": controller.uptime,
        "last_update_success": coordinator.last_update_success,
        "poll_interval_seconds": coordinator.update_interval.total_seconds(),
        "management_enabled": entry.options.get(CONF_ENABLE_CONTROL, False),
        "wireless_services_monitored": entry.options.get(CONF_MONITOR_WIRELESS_SERVICES, False),
        "optional_poll_intervals_seconds": {
            "monitoring": int(MONITORING_INTERVAL.total_seconds()),
            "configuration": int(CONFIGURATION_INTERVAL.total_seconds()),
        },
        "monitoring": {
            "available": monitoring is not None,
            "available_sources": sorted(monitoring.available_sections) if monitoring else [],
            "physical_ports": len(monitoring.ports) if monitoring else 0,
            "interfaces": len(monitoring.interfaces) if monitoring else 0,
            "access_point_firmware_records": len(monitoring.ap_firmware) if monitoring else 0,
            # None when the SSID table was not read; 0 when it was read and is empty.
            "wireless_services": None if services is None else len(services),
        },
        "configuration": {
            "available": configuration is not None,
            "available_sources": (
                sorted(configuration.available_sections) if configuration else []
            ),
            "scalar_fields": len(configuration.values) if configuration else 0,
            "radio_settings": len(configuration.radios) if configuration else 0,
        },
        "access_points": {
            "total": len(coordinator.data.aps),
            "online": sum(ap.is_online for ap in coordinator.data.aps),
        },
        "connected_clients": len(coordinator.data.clients),
    }
