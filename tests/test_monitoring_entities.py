"""Native HA entities for safe optional monitoring; no physical controller calls."""

from dataclasses import replace
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.components.update import UpdateEntityFeature
from homeassistant.const import STATE_OFF, STATE_ON, STATE_UNAVAILABLE, STATE_UNKNOWN, Platform
from homeassistant.exceptions import HomeAssistantError
from pytest_homeassistant.common import MockConfigEntry

from custom_components.tplink_ac100.api import Ac100Api
from custom_components.tplink_ac100.const import DOMAIN
from custom_components.tplink_ac100.diagnostics import async_get_config_entry_diagnostics
from custom_components.tplink_ac100.models import AccessPoint, Controller, Snapshot
from custom_components.tplink_ac100.monitoring import (
    ApFirmware,
    ConfigurationState,
    FirmwareAvailability,
    InterfaceTraffic,
    MonitoringState,
    PortStatus,
)

pytestmark = pytest.mark.usefixtures("enable_custom_integrations")


@pytest.fixture
async def monitoring_setup(hass):
    ap_mac = "02:00:00:00:00:02"
    monitoring = MonitoringState(
        values={
            "configuration_not_saved": True,
            "unknown_access_points": False,
            "access_point_limit_reached": None,
        },
        available_sections=frozenset(
            {
                "system.cfg_status",
                "apmng_set_status.status",
                "apmng_upgrade.ap_list",
                "port.port",
                "system.ifstat_list",
                "cloud_config.upgrade_info",
            }
        ),
        firmware=FirmwareAvailability(latest_version="1.0.7"),
        ap_firmware=(
            ApFirmware(
                mac=ap_mac,
                firmware_version="1.0.4",
                hardware_version="2.0",
                latest_version=None,
            ),
        ),
        ports=(
            PortStatus("switch1_port_1", "1", True, 1000),
            PortStatus("switch1_port_2", "2", False),
        ),
        interfaces=(InterfaceTraffic("lan", 5.0, 4.0, 17.0, 29.0, 1000, 2000),),
    )
    snapshot = Snapshot(
        Controller("02:00:00:00:00:01", "TL-AC100", "5.0", "1.0.5", {}, 30.0, 3600),
        (AccessPoint("0", "Example AP", "Example model", ap_mac, "192.0.2.2", "1", True, 600, ()),),
        (),
    )
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=snapshot.controller.mac,
        data={"host": "http://192.0.2.1", "username": "example", "password": "example-only"},
    )
    entry.add_to_hass(hass)
    api = AsyncMock(spec=Ac100Api)
    api.async_get_snapshot.return_value = snapshot
    api.async_get_monitoring.return_value = monitoring
    api.async_get_configuration.return_value = ConfigurationState()
    with (
        patch("custom_components.tplink_ac100.Ac100Api", return_value=api),
        patch(
            "custom_components.tplink_ac100.PLATFORMS",
            [Platform.SENSOR, Platform.BINARY_SENSOR, Platform.UPDATE],
        ),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        yield entry, entry.runtime_data, api
        assert await hass.config_entries.async_unload(entry.entry_id)


def get_state(hass, registry, entry, domain, key, ap_mac=None):
    unique_id = f"{entry.unique_id}_{ap_mac or 'controller'}_{key}"
    entity_id = registry.async_get_entity_id(domain, DOMAIN, unique_id)
    assert entity_id is not None
    state = hass.states.get(entity_id)
    assert state is not None
    return state


async def test_native_status_and_availability_only_firmware(
    hass, monitoring_setup, entity_registry
):
    entry, _, api = monitoring_setup
    assert (
        get_state(hass, entity_registry, entry, "binary_sensor", "configuration_not_saved").state
        == STATE_ON
    )
    assert (
        get_state(hass, entity_registry, entry, "binary_sensor", "unknown_access_points").state
        == STATE_OFF
    )
    assert (
        get_state(hass, entity_registry, entry, "binary_sensor", "access_point_limit_reached").state
        == STATE_UNKNOWN
    )
    controller = get_state(hass, entity_registry, entry, "update", "firmware")
    assert controller.state == STATE_ON
    assert controller.attributes["installed_version"] == "1.0.5"
    assert controller.attributes["latest_version"] == "1.0.7"
    assert not controller.attributes["supported_features"] & UpdateEntityFeature.INSTALL
    ap = get_state(hass, entity_registry, entry, "update", "firmware", "02:00:00:00:00:02")
    assert ap.state == STATE_OFF
    assert ap.attributes["installed_version"] == ap.attributes["latest_version"] == "1.0.4"
    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(
            "update", "install", {"entity_id": controller.entity_id}, blocking=True
        )
    api.async_reboot_ap.assert_not_awaited()
    api.async_reboot_controller.assert_not_awaited()
    api.async_set_ap_led.assert_not_awaited()


async def test_port_links_and_rates_are_not_configuration_speeds(
    hass, monitoring_setup, entity_registry
):
    entry, coordinator, _ = monitoring_setup
    assert (
        get_state(
            hass, entity_registry, entry, "binary_sensor", "port_switch1_port_1_connectivity"
        ).state
        == STATE_ON
    )
    assert (
        get_state(
            hass, entity_registry, entry, "binary_sensor", "port_switch1_port_2_connectivity"
        ).state
        == STATE_OFF
    )
    connected = get_state(hass, entity_registry, entry, "sensor", "port_switch1_port_1_link_speed")
    assert connected.state == "1000"
    assert connected.attributes["unit_of_measurement"] == "Mbit/s"
    assert (
        get_state(hass, entity_registry, entry, "sensor", "port_switch1_port_2_link_speed").state
        == STATE_UNKNOWN
    )
    transmit = get_state(hass, entity_registry, entry, "sensor", "interface_lan_transmit_rate")
    assert transmit.state == "5.0"
    assert transmit.attributes["unit_of_measurement"] == "kB/s"
    monitor = coordinator.data.monitoring
    coordinator.async_set_updated_data(
        replace(coordinator.data, monitoring=replace(monitor, ports=tuple(reversed(monitor.ports))))
    )
    await hass.async_block_till_done()
    assert (
        get_state(
            hass, entity_registry, entry, "sensor", "port_switch1_port_1_link_speed"
        ).entity_id
        == connected.entity_id
    )


async def test_optional_failure_does_not_present_cached_readings_as_current(
    hass, monitoring_setup, entity_registry
):
    entry, coordinator, _ = monitoring_setup
    coordinator.async_set_updated_data(replace(coordinator.data, monitoring=None))
    await hass.async_block_till_done()
    for domain, key in (
        ("binary_sensor", "configuration_not_saved"),
        ("binary_sensor", "port_switch1_port_1_connectivity"),
        ("sensor", "port_switch1_port_1_link_speed"),
        ("sensor", "interface_lan_transmit_rate"),
        ("update", "firmware"),
    ):
        assert get_state(hass, entity_registry, entry, domain, key).state == STATE_UNAVAILABLE
    assert coordinator.last_update_success


async def test_status_flag_without_its_source_is_unavailable(
    hass, monitoring_setup, entity_registry
):
    entry, coordinator, _ = monitoring_setup
    values = {"configuration_not_saved": True}
    monitor = replace(coordinator.data.monitoring, values=values)
    coordinator.async_set_updated_data(replace(coordinator.data, monitoring=monitor))
    await hass.async_block_till_done()
    assert (
        get_state(hass, entity_registry, entry, "binary_sensor", "configuration_not_saved").state
        == STATE_ON
    )
    assert (
        get_state(hass, entity_registry, entry, "binary_sensor", "unknown_access_points").state
        == STATE_UNAVAILABLE
    )


async def test_firmware_versions_compare_numerically(hass, monitoring_setup, entity_registry):
    entry, coordinator, _ = monitoring_setup
    controller = replace(coordinator.data.controller, firmware_version="1.0.7 Build 260208 Rel.1n")
    monitor = replace(
        coordinator.data.monitoring,
        firmware=FirmwareAvailability(latest_version="1.0.5 Build 250814 Rel.2n"),
    )
    coordinator.async_set_updated_data(
        replace(coordinator.data, controller=controller, monitoring=monitor)
    )
    await hass.async_block_till_done()
    assert get_state(hass, entity_registry, entry, "update", "firmware").state == STATE_OFF


async def test_optional_diagnostics_never_contain_values_or_network_identity(
    hass, monitoring_setup
):
    import json

    entry, coordinator, _ = monitoring_setup
    monitor = replace(coordinator.data.monitoring, values={"canary": "private-value-canary"})
    coordinator.async_set_updated_data(replace(coordinator.data, monitoring=monitor))
    diagnostic = await async_get_config_entry_diagnostics(hass, entry)
    encoded = json.dumps(diagnostic)
    for value in ("private-value-canary", "192.0.2.1", "02:00:00:00:00:02", "Example AP"):
        assert value not in encoded
    assert diagnostic["monitoring"]["physical_ports"] == 2


async def test_no_firmware_announcement_means_installed_is_latest(
    hass, monitoring_setup, entity_registry
):
    entry, coordinator, _ = monitoring_setup
    monitor = replace(coordinator.data.monitoring, firmware=FirmwareAvailability())
    coordinator.async_set_updated_data(replace(coordinator.data, monitoring=monitor))
    await hass.async_block_till_done()
    controller = get_state(hass, entity_registry, entry, "update", "firmware")
    assert controller.state == STATE_OFF
    assert controller.attributes["latest_version"] == "1.0.5"
