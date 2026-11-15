"""Simulated AC100 sensor platforms on the Home Assistant test runtime."""

from dataclasses import replace
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.const import STATE_OFF, STATE_ON, STATE_UNAVAILABLE, STATE_UNKNOWN, Platform
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.update_coordinator import UpdateFailed
from pytest_homeassistant.common import MockConfigEntry

from custom_components.tplink_ac100.api import Ac100Api
from custom_components.tplink_ac100.const import DOMAIN
from custom_components.tplink_ac100.models import AccessPoint, ApRadio, Client, Controller, Snapshot

pytestmark = pytest.mark.usefixtures("enable_custom_integrations")


@pytest.fixture
def sensor_snapshot():
    """Include master, standby and offline APs with retained telemetry."""
    radios = (
        ApRadio("0", "2.4GHz", 2, 6, 18.0),
        ApRadio("1", "5GHz", 3, 36, 25.0),
    )
    return Snapshot(
        controller=Controller(
            "02:00:00:00:00:10", "TL-AC100", "5.0", "1.0", {"0": 12.5, "1": 27.0}, 40.0, 7200
        ),
        aps=tuple(
            AccessPoint(
                str(index),
                f"AP {index}",
                "TL-XAP1502GI-PoE",
                f"02:00:00:00:00:{index + 20:02x}",
                f"192.0.2.{index + 20}",
                status,
                True,
                3600,
                radios,
            )
            for index, status in enumerate(("1", "0", "2"))
        ),
        clients=tuple(
            Client(
                str(index),
                f"02:00:00:00:01:{index:02x}",
                None,
                f"192.0.2.{index + 30}",
                "",
                "Example wireless",
                "AP 0",
                (),
            )
            for index in range(2)
        ),
    )


@pytest.fixture
async def sensor_setup(hass, sensor_snapshot):
    """Load only the two telemetry platforms; never call the physical device."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=sensor_snapshot.controller.mac,
        data={"host": "http://192.0.2.1", "username": "example", "password": "example-only"},
    )
    entry.add_to_hass(hass)
    api = AsyncMock(spec=Ac100Api)
    api.async_get_snapshot.return_value = sensor_snapshot
    with (
        patch("custom_components.tplink_ac100.Ac100Api", return_value=api),
        patch(
            "custom_components.tplink_ac100.PLATFORMS", [Platform.SENSOR, Platform.BINARY_SENSOR]
        ),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        coordinator = entry.runtime_data
        yield entry, coordinator, api
        if coordinator._listeners:
            assert await hass.config_entries.async_unload(entry.entry_id)


def entity_id(registry, snapshot, key, ap=None, platform="sensor"):
    """Resolve an entity by its stable controller/AP/key identity."""
    unique_id = f"{snapshot.controller.mac}_{ap.mac if ap else 'controller'}_{key}"
    result = registry.async_get_entity_id(platform, DOMAIN, unique_id)
    assert result is not None
    return result


def sensor_state(hass, registry, snapshot, key, ap=None, platform="sensor"):
    """Read the native HA state without depending on translated entity names."""
    state = hass.states.get(entity_id(registry, snapshot, key, ap, platform))
    assert state is not None
    return state


async def test_controller_values_and_shared_snapshot(
    hass, sensor_setup, sensor_snapshot, entity_registry
):
    """CPU is per-core, counts use a single successful poll, uptime is seconds."""
    entry, _, api = sensor_setup
    expected = {
        "cpu_usage_0": "12.5",
        "cpu_usage_1": "27.0",
        "memory_usage": "40.0",
        "uptime": "7200",
        "online_access_points": "2",
        "total_access_points": "3",
        "connected_clients": "2",
    }
    for key, value in expected.items():
        state = sensor_state(hass, entity_registry, sensor_snapshot, key)
        assert state.state == value
        assert state.attributes["state_class"] == "measurement"
    cpu = sensor_state(hass, entity_registry, sensor_snapshot, "cpu_usage_0")
    assert cpu.attributes["unit_of_measurement"] == "%"
    uptime = sensor_state(hass, entity_registry, sensor_snapshot, "uptime")
    assert uptime.attributes["unit_of_measurement"] == "s"
    assert uptime.attributes["device_class"] == "duration"
    assert len(er.async_entries_for_config_entry(entity_registry, entry.entry_id)) == 16
    api.async_get_snapshot.assert_awaited_once_with()
    assert len(api.mock_calls) == 1


@pytest.mark.parametrize("ap_index", [0, 1])
async def test_online_ap_telemetry(hass, sensor_setup, sensor_snapshot, entity_registry, ap_index):
    """Both master and standby APs count as online and expose current values."""
    ap = sensor_snapshot.aps[ap_index]
    clients = sensor_state(hass, entity_registry, sensor_snapshot, "connected_clients", ap)
    assert clients.state == "5"
    assert clients.attributes["ip_address"] == ap.ip
    assert clients.attributes["radios"] == {
        "0": {"frequency": "2.4GHz", "channel": 6, "channel_load": 18.0},
        "1": {"frequency": "5GHz", "channel": 36, "channel_load": 25.0},
    }
    assert sensor_state(hass, entity_registry, sensor_snapshot, "uptime", ap).state == "3600"
    connectivity = sensor_state(
        hass, entity_registry, sensor_snapshot, "connectivity", ap, "binary_sensor"
    )
    assert connectivity.state == STATE_ON
    assert connectivity.attributes["device_class"] == "connectivity"
    assert connectivity.attributes["link_status"] == ap.link_status


async def test_offline_ap_does_not_publish_stale_counters(
    hass, sensor_setup, sensor_snapshot, entity_registry
):
    """Retained controller counters are unavailable, not live AP telemetry."""
    _, coordinator, _ = sensor_setup
    ap = sensor_snapshot.aps[2]
    assert (
        sensor_state(
            hass, entity_registry, sensor_snapshot, "connectivity", ap, "binary_sensor"
        ).state
        == STATE_OFF
    )
    for key in ("connected_clients", "uptime"):
        state = sensor_state(hass, entity_registry, sensor_snapshot, key, ap)
        assert state.state == STATE_UNAVAILABLE
        assert "radios" not in state.attributes
    coordinator.async_set_updated_data(
        replace(sensor_snapshot, aps=(replace(ap, link_status="1"),))
    )
    await hass.async_block_till_done()
    assert (
        sensor_state(hass, entity_registry, sensor_snapshot, "connected_clients", ap).state == "5"
    )
    coordinator.async_set_updated_data(replace(sensor_snapshot, aps=(ap,)))
    await hass.async_block_till_done()
    state = sensor_state(hass, entity_registry, sensor_snapshot, "connected_clients", ap)
    assert state.state == STATE_UNAVAILABLE
    assert "radios" not in state.attributes


async def test_discovery_removal_and_stable_identity(
    hass, sensor_setup, sensor_snapshot, entity_registry
):
    """Discover APs and CPU cores once and keep AP/radio identities across reorder."""
    entry, coordinator, _ = sensor_setup
    ap = sensor_snapshot.aps[0]
    initial_id = entity_id(entity_registry, sensor_snapshot, "connected_clients", ap)
    changed_ap = replace(
        ap,
        entry_id="99",
        name="Renamed AP",
        radios=(replace(ap.radios[1], channel=149), ap.radios[0]),
    )
    new_ap = replace(ap, mac="02:00:00:00:00:99", name="New AP", entry_id="100")
    changed = replace(
        sensor_snapshot,
        controller=replace(sensor_snapshot.controller, cpu_usage={"0": 10.0, "1": 20.0, "2": 30.0}),
        aps=(new_ap, changed_ap, *sensor_snapshot.aps[1:]),
    )
    coordinator.async_set_updated_data(changed)
    await hass.async_block_till_done()
    assert entity_id(entity_registry, changed, "connected_clients", changed_ap) == initial_id
    state = hass.states.get(initial_id)
    assert state.attributes["radios"]["1"]["channel"] == 149
    assert state.attributes["radios"]["0"]["channel"] == 6
    assert sensor_state(hass, entity_registry, changed, "cpu_usage_2").state == "30.0"
    assert sensor_state(hass, entity_registry, changed, "connected_clients", new_ap).state == "5"
    assert sensor_state(hass, entity_registry, changed, "uptime", new_ap).state == "3600"
    assert (
        sensor_state(hass, entity_registry, changed, "connectivity", new_ap, "binary_sensor").state
        == STATE_ON
    )
    count = len(er.async_entries_for_config_entry(entity_registry, entry.entry_id))
    assert count == 20
    coordinator.async_set_updated_data(replace(changed, aps=()))
    await hass.async_block_till_done()
    for key in ("connected_clients", "uptime"):
        assert sensor_state(hass, entity_registry, changed, key, new_ap).state == STATE_UNAVAILABLE
    assert (
        sensor_state(hass, entity_registry, changed, "connectivity", new_ap, "binary_sensor").state
        == STATE_UNAVAILABLE
    )
    coordinator.async_set_updated_data(changed)
    await hass.async_block_till_done()
    assert len(er.async_entries_for_config_entry(entity_registry, entry.entry_id)) == count
    assert sensor_state(hass, entity_registry, changed, "connected_clients", new_ap).state == "5"


async def test_missing_values_are_unknown(hass, sensor_setup, sensor_snapshot, entity_registry):
    """Do not retain old values when a successful poll has missing telemetry."""
    _, coordinator, _ = sensor_setup
    changed = replace(
        sensor_snapshot,
        controller=replace(
            sensor_snapshot.controller, cpu_usage={}, memory_usage=None, uptime=None
        ),
        aps=(replace(sensor_snapshot.aps[0], uptime=None),),
        clients=(),
    )
    coordinator.async_set_updated_data(changed)
    await hass.async_block_till_done()
    for key in ("cpu_usage_0", "cpu_usage_1", "memory_usage", "uptime"):
        assert sensor_state(hass, entity_registry, changed, key).state == STATE_UNKNOWN
    assert (
        sensor_state(hass, entity_registry, changed, "uptime", changed.aps[0]).state
        == STATE_UNKNOWN
    )
    assert sensor_state(hass, entity_registry, changed, "connected_clients").state == "0"


async def test_poll_failure_and_recovery(hass, sensor_setup, sensor_snapshot, entity_registry):
    """A failed shared poll makes every telemetry entity unavailable."""
    entry, coordinator, _ = sensor_setup
    coordinator.async_set_update_error(UpdateFailed("simulated controller outage"))
    await hass.async_block_till_done()
    for entity in er.async_entries_for_config_entry(entity_registry, entry.entry_id):
        assert hass.states.get(entity.entity_id).state == STATE_UNAVAILABLE
    coordinator.async_set_updated_data(sensor_snapshot)
    await hass.async_block_till_done()
    assert sensor_state(hass, entity_registry, sensor_snapshot, "memory_usage").state == "40.0"
    assert (
        sensor_state(
            hass,
            entity_registry,
            sensor_snapshot,
            "connectivity",
            sensor_snapshot.aps[0],
            "binary_sensor",
        ).state
        == STATE_ON
    )


async def test_unload_removes_discovery_listeners(
    hass, sensor_setup, sensor_snapshot, entity_registry
):
    """No discovery or state callbacks remain after the entry unloads."""
    entry, coordinator, _ = sensor_setup
    assert await hass.config_entries.async_unload(entry.entry_id)
    assert not coordinator._listeners
    new_ap = replace(sensor_snapshot.aps[0], mac="02:00:00:00:00:99", entry_id="99")
    coordinator.async_set_updated_data(replace(sensor_snapshot, aps=(new_ap,)))
    await hass.async_block_till_done()
    unique_id = f"{sensor_snapshot.controller.mac}_{new_ap.mac}_connected_clients"
    assert entity_registry.async_get_entity_id("sensor", DOMAIN, unique_id) is None
