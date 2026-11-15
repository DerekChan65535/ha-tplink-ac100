"""Simulated AC100 sensor platforms on the Home Assistant test runtime."""

import json
import re
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.const import (
    STATE_OFF,
    STATE_ON,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
    EntityCategory,
    Platform,
)
from homeassistant.helpers import entity_platform
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.update_coordinator import UpdateFailed
from pytest_homeassistant.common import MockConfigEntry

from custom_components.tplink_ac100.api import Ac100Api
from custom_components.tplink_ac100.const import DOMAIN
from custom_components.tplink_ac100.models import AccessPoint, ApRadio, Client, Controller, Snapshot
from custom_components.tplink_ac100.monitoring import ConfigurationState, MonitoringState
from custom_components.tplink_ac100.sensor import AP_SENSORS, CONTROLLER_SENSORS, RADIO_SENSORS

pytestmark = pytest.mark.usefixtures("enable_custom_integrations")

NOW = datetime(2026, 11, 15, 12, 0, tzinfo=UTC)
ENGLISH = Path(__file__).parents[1] / "custom_components/tplink_ac100/translations/en.json"


@pytest.fixture
def sensor_snapshot():
    """Include online, connecting and offline APs with retained telemetry."""
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
def existing_install():
    """Whether users already have the duration uptime rows in the entity registry, enabled."""
    return True


@pytest.fixture
async def sensor_setup(hass, sensor_snapshot, existing_install, freezer):
    """Load only the two telemetry platforms; never call the physical device."""
    freezer.move_to(NOW)
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=sensor_snapshot.controller.mac,
        data={"host": "http://192.0.2.1", "username": "example", "password": "example-only"},
    )
    entry.add_to_hass(hass)
    if existing_install:
        registry = er.async_get(hass)
        for ap in (None, *sensor_snapshot.aps):
            registry.async_get_or_create(
                "sensor", DOMAIN, unique_id(sensor_snapshot, "uptime", ap), config_entry=entry
            )
        for core in sensor_snapshot.controller.cpu_usage:
            registry.async_get_or_create(
                "sensor",
                DOMAIN,
                unique_id(sensor_snapshot, f"cpu_usage_{core}"),
                config_entry=entry,
            )
    api = AsyncMock(spec=Ac100Api)
    api.async_get_snapshot.return_value = sensor_snapshot
    api.async_get_monitoring.return_value = MonitoringState()
    api.async_get_configuration.return_value = ConfigurationState()
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


def unique_id(snapshot, key, ap=None):
    """Build the stable controller/AP/key identity used for entity unique IDs."""
    return f"{snapshot.controller.mac}_{ap.mac if ap else 'controller'}_{key}"


def entity_id(registry, snapshot, key, ap=None, platform="sensor"):
    """Resolve an entity by its stable controller/AP/key identity."""
    result = registry.async_get_entity_id(platform, DOMAIN, unique_id(snapshot, key, ap))
    assert result is not None
    return result


def entity_object(hass, entity_id):
    """Return the live entity, for properties that are not part of the HA state."""
    for platform in entity_platform.async_get_platforms(hass, DOMAIN):
        if entity := platform.entities.get(entity_id):
            return entity
    raise AssertionError(f"{entity_id} is not loaded")


def sensor_state(hass, registry, snapshot, key, ap=None, platform="sensor"):
    """Read the native HA state without depending on translated entity names."""
    state = hass.states.get(entity_id(registry, snapshot, key, ap, platform))
    assert state is not None
    return state


async def test_controller_values_and_shared_snapshot(
    hass, sensor_setup, sensor_snapshot, entity_registry
):
    """CPU is per-core, counts use a single successful poll, uptime is seconds."""
    _, _, api = sensor_setup
    expected = {
        "cpu_usage": "19.75",
        "cpu_usage_0": "12.5",
        "cpu_usage_1": "27.0",
        "memory_usage": "40.0",
        "uptime": "7200",
        "online_access_points": "1",
        "total_access_points": "3",
        "connected_clients": "2",
    }
    for key, value in expected.items():
        state = sensor_state(hass, entity_registry, sensor_snapshot, key)
        assert state.state == value
        assert state.attributes["state_class"] == "measurement"
    cpu = sensor_state(hass, entity_registry, sensor_snapshot, "cpu_usage_0")
    assert cpu.attributes["unit_of_measurement"] == "%"
    api.async_get_snapshot.assert_awaited_once_with()
    assert {call[0] for call in api.mock_calls} <= {"async_get_snapshot", "async_get_monitoring"}


@pytest.mark.usefixtures("entity_registry_enabled_by_default")
async def test_cpu_core_display_number_keeps_the_raw_key_in_the_unique_id(
    hass, sensor_setup, sensor_snapshot, entity_registry
):
    """The controller's core1 is shown as core 1 while the unique ID still carries core1."""
    _, coordinator, _ = sensor_setup
    changed = replace(
        sensor_snapshot,
        controller=replace(
            sensor_snapshot.controller, cpu_usage={"core1": 1.0, "cpu2": 2.0, "3": 3.0}
        ),
    )
    coordinator.async_set_updated_data(changed)
    await hass.async_block_till_done()
    for raw, shown in (("core1", "1"), ("cpu2", "2"), ("3", "3")):
        identity = entity_id(entity_registry, changed, f"cpu_usage_{raw}")
        assert entity_object(hass, identity).translation_placeholders == {"core": shown}
        assert hass.states.get(identity).state == f"{float(shown)}"


async def test_existing_uptime_entities_keep_identity_type_and_enabled_state(
    hass, sensor_setup, sensor_snapshot, entity_registry
):
    """Long-term statistics need the same unique ID, seconds and MEASUREMENT state class."""
    for ap, expected in ((None, "7200"), (sensor_snapshot.aps[0], "3600")):
        row = entity_registry.async_get(entity_id(entity_registry, sensor_snapshot, "uptime", ap))
        assert row.disabled_by is None
        state = hass.states.get(row.entity_id)
        assert state.state == expected
        assert state.attributes["device_class"] == "duration"
        assert state.attributes["unit_of_measurement"] == "s"
        assert state.attributes["state_class"] == "measurement"
        assert row.entity_category is EntityCategory.DIAGNOSTIC
        assert row.options["sensor"]["suggested_display_precision"] == 0


@pytest.mark.parametrize("existing_install", [False])
async def test_new_installs_disable_the_duration_but_enable_last_restart(
    hass, sensor_setup, sensor_snapshot, entity_registry
):
    """Only newly registered duration rows start disabled; boot-time rows are enabled."""
    for ap in (None, *sensor_snapshot.aps):
        duration = entity_registry.async_get(
            entity_id(entity_registry, sensor_snapshot, "uptime", ap)
        )
        assert duration.disabled_by is er.RegistryEntryDisabler.INTEGRATION
        assert hass.states.get(duration.entity_id) is None
        restart = entity_registry.async_get(
            entity_id(entity_registry, sensor_snapshot, "last_restart", ap)
        )
        assert restart.disabled_by is None
        assert restart.entity_category is EntityCategory.DIAGNOSTIC
        assert hass.states.get(restart.entity_id) is not None


async def test_last_restart_is_a_boot_time_that_ignores_poll_jitter(
    hass, sensor_setup, sensor_snapshot, entity_registry, freezer
):
    """UPTIME sensors take an aware boot time and the core suppresses drift of up to 60 seconds."""
    _, coordinator, _ = sensor_setup
    ap = sensor_snapshot.aps[0]
    controller_restart = sensor_state(hass, entity_registry, sensor_snapshot, "last_restart")
    assert controller_restart.state == "2026-11-15T10:00:00+00:00"
    assert controller_restart.attributes["device_class"] == "uptime"
    assert "unit_of_measurement" not in controller_restart.attributes
    assert "state_class" not in controller_restart.attributes
    ap_restart = sensor_state(hass, entity_registry, sensor_snapshot, "last_restart", ap)
    assert ap_restart.state == "2026-11-15T11:00:00+00:00"

    freezer.tick(timedelta(seconds=60))
    jitter = replace(
        sensor_snapshot,
        controller=replace(sensor_snapshot.controller, uptime=7263),
        aps=(replace(ap, uptime=3657), *sensor_snapshot.aps[1:]),
    )
    coordinator.async_set_updated_data(jitter)
    await hass.async_block_till_done()
    assert hass.states.get(controller_restart.entity_id).state == controller_restart.state
    assert hass.states.get(ap_restart.entity_id).state == ap_restart.state

    freezer.tick(timedelta(seconds=60))
    restarted = replace(
        sensor_snapshot,
        controller=replace(sensor_snapshot.controller, uptime=15),
        aps=(replace(ap, uptime=5), *sensor_snapshot.aps[1:]),
    )
    coordinator.async_set_updated_data(restarted)
    await hass.async_block_till_done()
    assert hass.states.get(controller_restart.entity_id).state == "2026-11-15T12:01:45+00:00"
    assert hass.states.get(ap_restart.entity_id).state == "2026-11-15T12:01:55+00:00"


async def test_online_ap_telemetry(hass, sensor_setup, sensor_snapshot, entity_registry):
    """Only a fully connected AP exposes current values."""
    ap = sensor_snapshot.aps[0]
    clients = sensor_state(hass, entity_registry, sensor_snapshot, "connected_clients", ap)
    assert clients.state == "5"
    assert clients.attributes["ip_address"] == ap.ip
    assert clients.attributes["radios"] == {
        "0": {"frequency": "2.4GHz", "channel": 6, "channel_load": 18.0},
        "1": {"frequency": "5GHz", "channel": 36, "channel_load": 25.0},
    }
    assert sensor_state(hass, entity_registry, sensor_snapshot, "uptime", ap).state == "3600"
    address = sensor_state(hass, entity_registry, sensor_snapshot, "ip_address", ap)
    assert address.state == ap.ip
    assert entity_registry.async_get(address.entity_id).entity_category is EntityCategory.DIAGNOSTIC
    connectivity = sensor_state(
        hass, entity_registry, sensor_snapshot, "connectivity", ap, "binary_sensor"
    )
    assert connectivity.state == STATE_ON
    assert connectivity.attributes["device_class"] == "connectivity"
    assert connectivity.attributes["link_status"] == ap.link_status


@pytest.mark.parametrize("ap_index", [1, 2])
async def test_disconnected_ap_does_not_publish_stale_counters(
    hass, sensor_setup, sensor_snapshot, entity_registry, ap_index
):
    """Connecting and offline counters are not live AP telemetry."""
    _, coordinator, _ = sensor_setup
    ap = sensor_snapshot.aps[ap_index]
    assert (
        sensor_state(
            hass, entity_registry, sensor_snapshot, "connectivity", ap, "binary_sensor"
        ).state
        == STATE_OFF
    )
    for key in ("connected_clients", "uptime", "last_restart", "ip_address"):
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
    assert sensor_state(hass, entity_registry, changed, "cpu_usage").state == "20.0"
    new_core = entity_registry.async_get(entity_id(entity_registry, changed, "cpu_usage_2"))
    assert new_core.disabled_by is er.RegistryEntryDisabler.INTEGRATION
    assert sensor_state(hass, entity_registry, changed, "connected_clients", new_ap).state == "5"
    assert (
        sensor_state(hass, entity_registry, changed, "last_restart", new_ap).state
        == "2026-11-15T11:00:00+00:00"
    )
    new_duration = entity_registry.async_get(entity_id(entity_registry, changed, "uptime", new_ap))
    assert new_duration.disabled_by is er.RegistryEntryDisabler.INTEGRATION
    assert (
        sensor_state(hass, entity_registry, changed, "connectivity", new_ap, "binary_sensor").state
        == STATE_ON
    )
    count = len(er.async_entries_for_config_entry(entity_registry, entry.entry_id))
    coordinator.async_set_updated_data(replace(changed, aps=()))
    await hass.async_block_till_done()
    for key in ("connected_clients", "last_restart", "ip_address"):
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
    for key in (
        "cpu_usage",
        "cpu_usage_0",
        "cpu_usage_1",
        "memory_usage",
        "uptime",
        "last_restart",
    ):
        assert sensor_state(hass, entity_registry, changed, key).state == STATE_UNKNOWN
    for key in ("uptime", "last_restart"):
        state = sensor_state(hass, entity_registry, changed, key, changed.aps[0])
        assert state.state == STATE_UNKNOWN
    assert sensor_state(hass, entity_registry, changed, "connected_clients").state == "0"


async def test_poll_failure_and_recovery(hass, sensor_setup, sensor_snapshot, entity_registry):
    """A failed shared poll makes every telemetry entity unavailable."""
    entry, coordinator, _ = sensor_setup
    coordinator.async_set_update_error(UpdateFailed("simulated controller outage"))
    await hass.async_block_till_done()
    rows = [
        row
        for row in er.async_entries_for_config_entry(entity_registry, entry.entry_id)
        if row.disabled_by is None
    ]
    assert rows
    for entity in rows:
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
    identity = unique_id(sensor_snapshot, "connected_clients", new_ap)
    assert entity_registry.async_get_entity_id("sensor", DOMAIN, identity) is None


def test_every_sensor_translation_key_has_an_english_name_with_its_placeholders():
    """An entity whose translation key has no name is shown as just its device name."""
    names = json.loads(ENGLISH.read_text())["entity"]["sensor"]
    descriptions = (*CONTROLLER_SENSORS, *AP_SENSORS, *RADIO_SENSORS)
    keys = {description.translation_key for description in descriptions} | {"cpu_usage"}
    for key in keys:
        assert key in names, key
        placeholders = set(re.findall(r"{(\w+)}", names[key]["name"]))
        expected = (
            {"band"} if key.startswith("radio_") else {"core"} if key == "cpu_usage" else set()
        )
        assert placeholders == expected, key
