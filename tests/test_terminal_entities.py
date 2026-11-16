"""Native terminal entities: saved readings and opt-in controls on each client's own device page."""

import json
import re
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import create_autospec, patch

import pytest
from homeassistant.const import (
    STATE_OFF,
    STATE_ON,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
    EntityCategory,
    Platform,
)
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_platform
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.update_coordinator import UpdateFailed
from pytest_homeassistant.common import MockConfigEntry

from custom_components.tplink_ac100.api import (
    Ac100AccountLockedError,
    Ac100Api,
    Ac100AuthError,
    Ac100ConnectionError,
    Ac100SessionBusyError,
    Ac100SessionExpiredError,
    Ac100TerminalError,
    Ac100TerminalPartialError,
)
from custom_components.tplink_ac100.const import CONF_ENABLE_CONTROL, DOMAIN
from custom_components.tplink_ac100.diagnostics import async_get_config_entry_diagnostics
from custom_components.tplink_ac100.models import (
    AccessPoint,
    ApRadio,
    Client,
    ClientRadio,
    Controller,
    Snapshot,
)
from custom_components.tplink_ac100.monitoring import ConfigurationState, MonitoringState
from custom_components.tplink_ac100.terminal import (
    LIMIT_RULE,
    NAME_RULE,
    TerminalBindingTarget,
    TerminalConfiguration,
    TerminalField,
    TerminalState,
)
from custom_components.tplink_ac100.terminal_controls import (
    CHOOSE_TARGET,
    Ac100TerminalNameText,
    binding_target_labels,
)
from custom_components.tplink_ac100.terminal_schedule import DAYS

pytestmark = pytest.mark.usefixtures("enable_custom_integrations")

CONTROLLER_MAC = "02:00:00:00:00:01"
COMPONENT = Path(__file__).parents[1] / "custom_components/tplink_ac100"
PLATFORMS = [
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.DEVICE_TRACKER,
    Platform.NUMBER,
    Platform.SELECT,
    Platform.SENSOR,
    Platform.SWITCH,
    Platform.TEXT,
]

# The saved readings of this module, by the key in their unique ID, and their platform.
READINGS = {
    "name": "sensor",
    "upload_limit": "sensor",
    "download_limit": "sensor",
    "bound_access_point": "sensor",
    "bound_radio": "sensor",
    "rate_limit_enabled": "binary_sensor",
    "binding_enabled": "binary_sensor",
    "force_binding": "binary_sensor",
}
# The scalar controls, by the key in their unique ID, and their platform.
CONTROLS = {
    "name_control": "text",
    "disconnect_control": "button",
    "disconnect_and_clear_control": "button",
    "clear_offline_control": "button",
    "rate_limit_control": "switch",
    "upload_limit_control": "number",
    "download_limit_control": "number",
    "binding_control": "switch",
    "force_binding_control": "switch",
    "binding_target_control": "select",
}
# The schedule entities that the schedule module adds through its four hooks.
SCHEDULE_READINGS = {
    "allowed_time_mode": "sensor",
    "manual_weekdays": "sensor",
    "manual_windows": "sensor",
    **{f"calendar_{day}": "sensor" for day in DAYS},
}
SCHEDULE_CONTROLS = {
    "allowed_time_mode_control": "select",
    "manual_windows_control": "text",
    **{f"manual_{day}_control": "switch" for day in DAYS},
    **{f"calendar_{day}_control": "text" for day in DAYS},
}


def access_point(index: int, name: str, units=(("1", "2.4GHz"), ("2", "5GHz"))) -> AccessPoint:
    radios = tuple(ApRadio(unit, band, 0, 6, 10.0) for unit, band in units)
    return AccessPoint(
        str(index),
        name,
        "TL-XAP1502GI-PoE",
        f"02:00:00:aa:bb:{index:02x}",
        f"192.0.2.{index + 10}",
        "1",
        True,
        600,
        radios,
    )


GARAGE = access_point(1, "Garage")
STUDY = access_point(2, "Study")


def target(
    ap: AccessPoint, unit: str | None = None, band: str = "", ids=()
) -> TerminalBindingTarget:
    """A binding target as the API lists it: its token is the AP's MAC and the radio unit or all."""
    return TerminalBindingTarget(f"{ap.mac}/{unit or 'all'}", ap.mac, ap.name, unit, band, ids)


TARGETS = (
    target(GARAGE, ids=("11", "12")),
    target(GARAGE, "1", "2.4GHz", ("11",)),
    target(GARAGE, "2", "5GHz", ("12",)),
    target(STUDY, ids=("21", "22")),
    target(STUDY, "1", "2.4GHz", ("21",)),
    target(STUDY, "2", "5GHz", ("22",)),
)
GARAGE_5GHZ = f"{GARAGE.mac}/2"


def wifi_client(index: int, name: str | None = None, **fields) -> Client:
    values = {
        "entry_id": str(index),
        "mac": f"02:00:00:cc:dd:{index:02x}",
        "name": name,
        "ip": f"192.0.2.{index + 50}",
        "ipv6": "",
        "ssid": "Example wireless",
        "ap_name": "Garage",
        "radios": (ClientRadio("2", "5GHz", -55, 433.0),),
    }
    return Client(**values | fields)


def record(mac: str, **fields) -> TerminalConfiguration:
    """A saved record with rate limiting and binding on, bound to the 5 GHz radio of the Garage."""
    values = {
        "mac": mac,
        "name": "Example phone",
        "rate_limit_enabled": True,
        "upload_limit": 500,
        "download_limit": 2000,
        "binding_enabled": True,
        "force_binding": False,
        "bound_radio_ids": ("12",),
        "binding_targets": TARGETS,
        "binding_available": True,
        "bound_ap": "Garage",
        "bound_radio": "5GHz",
        "bound_target": GARAGE_5GHZ,
        "allowed_time_mode": "any",
        "schedule": None,
        "schedule_available": True,
    }
    return TerminalConfiguration(**values | fields)


# A connected terminal with a record, a connected one without a record, and a record of a terminal
# that is away and has never been seen connected.
PHONE = wifi_client(1, "Example phone")
NEWCOMER = wifi_client(2, "Example tablet")
AWAY = wifi_client(3, "Away laptop")
AWAY_RECORD = record(
    AWAY.mac,
    name="Away laptop",
    rate_limit_enabled=False,
    upload_limit=0,
    download_limit=0,
    binding_enabled=False,
    force_binding=False,
    bound_radio_ids=(),
    binding_targets=(),
    binding_available=False,
    bound_ap=None,
    bound_radio=None,
    bound_target=None,
)
PHONE_READINGS = {
    "name": "Example phone",
    "upload_limit": "500",
    "download_limit": "2000",
    "bound_access_point": "Garage",
    "bound_radio": "5GHz",
    "rate_limit_enabled": STATE_ON,
    "binding_enabled": STATE_ON,
    "force_binding": STATE_OFF,
}


@pytest.fixture
def ac_snapshot() -> Snapshot:
    return Snapshot(
        Controller(CONTROLLER_MAC, "TL-AC100", "5.0", "1.0.5", {}, 30.0, 3600),
        (GARAGE, STUDY),
        (PHONE, NEWCOMER),
        terminals=TerminalState((record(PHONE.mac), AWAY_RECORD)),
    )


@pytest.fixture
def api(ac_snapshot):
    """Simulate the controller; every call is checked against the real API's signatures."""
    api = create_autospec(Ac100Api, instance=True)
    api.async_get_snapshot.return_value = ac_snapshot
    api.async_get_monitoring.return_value = MonitoringState()
    api.async_get_configuration.return_value = ConfigurationState()
    with (
        patch("custom_components.tplink_ac100.Ac100Api", return_value=api),
        patch("custom_components.tplink_ac100.PLATFORMS", PLATFORMS),
    ):
        yield api


def make_entry(hass, *, management: bool = False) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=CONTROLLER_MAC,
        data={"host": "http://192.0.2.1", "username": "example", "password": "example-only"},
        options={CONF_ENABLE_CONTROL: management},
    )
    entry.add_to_hass(hass)
    return entry


async def setup(hass, entry):
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry.runtime_data


async def publish(hass, coordinator, snapshot):
    coordinator.async_set_updated_data(snapshot)
    await hass.async_block_till_done()


def uid(mac: str, key: str) -> str:
    return f"{CONTROLLER_MAC}_terminal_{mac}_{key}"


def entity_id(registry, domain: str, unique_id: str) -> str:
    result = registry.async_get_entity_id(domain, DOMAIN, unique_id)
    assert result is not None, (domain, unique_id)
    return result


def state_of(hass, registry, domain: str, unique_id: str):
    state = hass.states.get(entity_id(registry, domain, unique_id))
    assert state is not None, (domain, unique_id)
    return state


def readings(hass, registry, mac: str, skip: tuple[str, ...] = ()) -> dict[str, str]:
    """The state of each saved reading of a terminal, by key."""
    return {
        key: state_of(hass, registry, domain, uid(mac, key)).state
        for key, domain in READINGS.items()
        if key not in skip
    }


def controls(hass, registry, mac: str) -> dict[str, str]:
    """The state of each scalar control of a terminal, by key."""
    return {
        key: state_of(hass, registry, domain, uid(mac, key)).state
        for key, domain in CONTROLS.items()
    }


def client_state(hass, registry, mac: str, key: str) -> str:
    domain = "binary_sensor" if key == "connectivity" else "sensor"
    return state_of(hass, registry, domain, f"{CONTROLLER_MAC}_client_{mac}_{key}").state


def device_of(hass, mac: str) -> dr.DeviceEntry:
    entry = hass.config_entries.async_entries(DOMAIN)[0]
    device = dr.async_get(hass).async_get_device_by_connection(
        (dr.CONNECTION_NETWORK_MAC, mac), entry.entry_id
    )
    assert device is not None, mac
    return device


def entry_rows(hass, entry) -> dict[tuple[str, str], str]:
    """The entity ID of every registry row of the entry, by domain and unique ID."""
    registry = er.async_get(hass)
    return {
        (row.domain, row.unique_id): row.entity_id
        for row in er.async_entries_for_config_entry(registry, entry.entry_id)
    }


def entity_object(hass, entity_id: str):
    for platform in entity_platform.async_get_platforms(hass, DOMAIN):
        if entity := platform.entities.get(entity_id):
            return entity
    raise AssertionError(f"{entity_id} is not loaded")


def available_controls(hass, registry, mac: str) -> set[str]:
    return {
        key for key, state in controls(hass, registry, mac).items() if state != STATE_UNAVAILABLE
    }


async def call(hass, registry, domain: str, service: str, key: str, mac: str = PHONE.mac, **data):
    await hass.services.async_call(
        domain,
        service,
        {"entity_id": entity_id(registry, domain, uid(mac, key)), **data},
        blocking=True,
    )


def write_calls(api) -> dict[str, int]:
    """Each terminal write method of the API that was awaited, with how often."""
    counts = {
        name: getattr(api, name).await_count
        for name in (
            "async_set_terminal_field",
            "async_disconnect_terminal",
            "async_clear_terminal",
        )
    }
    return {name: count for name, count in counts.items() if count}


# The saved readings


async def test_saved_readings_are_enabled_on_the_clients_own_device_and_the_tracker_stays_off(
    hass, api, entity_registry
):
    entry = make_entry(hass)
    await setup(hass, entry)
    device = device_of(hass, PHONE.mac)
    prefix = uid(PHONE.mac, "")
    rows = {
        row.unique_id.removeprefix(prefix): row
        for row in er.async_entries_for_device(
            entity_registry, device.id, include_disabled_entities=True
        )
        if row.unique_id.startswith(prefix)
    }
    assert set(READINGS) <= set(rows)
    for key, domain in READINGS.items():
        row = rows[key]
        assert row.domain == domain, key
        assert row.disabled_by is None, key
        assert row.entity_category is EntityCategory.DIAGNOSTIC, key
        assert row.config_entry_id == entry.entry_id
    assert readings(hass, entity_registry, PHONE.mac) == PHONE_READINGS
    tracker = entity_registry.async_get(entity_id(entity_registry, "device_tracker", PHONE.mac))
    assert tracker.disabled_by is er.RegistryEntryDisabler.INTEGRATION
    assert device.connections == {(dr.CONNECTION_NETWORK_MAC, PHONE.mac)}
    assert not device.identifiers
    assert (
        entity_id(entity_registry, "sensor", uid(PHONE.mac, "upload_limit"))
        == "sensor.example_phone_upload_limit"
    )


async def test_limits_are_kilobytes_per_second_and_zero_means_unlimited(
    hass, api, ac_snapshot, entity_registry
):
    await setup(hass, make_entry(hass))
    for key, value in (("upload_limit", "500"), ("download_limit", "2000")):
        state = state_of(hass, entity_registry, "sensor", uid(PHONE.mac, key))
        assert state.state == value
        assert state.attributes["unit_of_measurement"] == "kB/s"
        assert state.attributes["device_class"] == "data_rate"
        # A setting is not a measurement: no long-term statistics, no extra attributes.
        assert "state_class" not in state.attributes
    unlimited = readings(hass, entity_registry, AWAY.mac)
    assert (unlimited["upload_limit"], unlimited["download_limit"]) == ("0", "0")
    assert unlimited["rate_limit_enabled"] == STATE_OFF
    for key, domain in READINGS.items():
        extra = set(state_of(hass, entity_registry, domain, uid(PHONE.mac, key)).attributes)
        assert extra <= {"friendly_name", "device_class", "unit_of_measurement"}, key


async def test_a_field_that_the_controller_does_not_report_is_unknown_not_invented(
    hass, api, ac_snapshot, entity_registry
):
    bare = TerminalConfiguration(PHONE.mac)
    api.async_get_snapshot.return_value = replace(ac_snapshot, terminals=TerminalState((bare,)))
    await setup(hass, make_entry(hass))
    assert set(readings(hass, entity_registry, PHONE.mac).values()) == {STATE_UNKNOWN}
    assert client_state(hass, entity_registry, PHONE.mac, "connectivity") == STATE_ON


async def test_the_name_on_the_controller_is_the_saved_name_and_not_the_device_name(
    hass, api, ac_snapshot, entity_registry, device_registry
):
    entry = make_entry(hass)
    coordinator = await setup(hass, entry)
    device = device_of(hass, PHONE.mac)
    device_registry.async_update_device(device.id, name_by_user="Alice phone")
    await hass.async_block_till_done()
    renamed = replace(PHONE, name="Kitchen phone")
    await publish(
        hass,
        coordinator,
        replace(
            ac_snapshot,
            clients=(renamed, NEWCOMER),
            terminals=TerminalState((record(PHONE.mac, name="Kitchen phone"), AWAY_RECORD)),
        ),
    )
    name = state_of(hass, entity_registry, "sensor", uid(PHONE.mac, "name"))
    assert name.state == "Kitchen phone"
    assert device_registry.async_get(device.id).name_by_user == "Alice phone"
    assert (
        entity_id(entity_registry, "sensor", uid(PHONE.mac, "name"))
        == "sensor.example_phone_name_on_controller"
    )


async def test_a_blank_saved_name_is_unknown(hass, api, ac_snapshot, entity_registry):
    api.async_get_snapshot.return_value = replace(
        ac_snapshot, terminals=TerminalState((record(PHONE.mac, name="  "),))
    )
    await setup(hass, make_entry(hass))
    assert readings(hass, entity_registry, PHONE.mac)["name"] == STATE_UNKNOWN


async def test_saved_readings_stay_while_the_terminal_is_away_and_its_telemetry_does_not(
    hass, api, ac_snapshot, entity_registry
):
    coordinator = await setup(hass, make_entry(hass))
    assert client_state(hass, entity_registry, PHONE.mac, "signal_strength") == "-55"
    await publish(hass, coordinator, replace(ac_snapshot, clients=(NEWCOMER,)))
    assert readings(hass, entity_registry, PHONE.mac) == PHONE_READINGS
    assert client_state(hass, entity_registry, PHONE.mac, "connectivity") == STATE_OFF
    for key in ("signal_strength", "wifi_upload_speed", "access_point", "radio_unit"):
        assert client_state(hass, entity_registry, PHONE.mac, key) == STATE_UNAVAILABLE


async def test_a_saved_record_of_a_terminal_never_seen_connected_has_a_page(
    hass, api, ac_snapshot, entity_registry
):
    await setup(hass, make_entry(hass))
    device = device_of(hass, AWAY.mac)
    assert device.name == "Away laptop"
    assert device.via_device_id is None
    assert readings(hass, entity_registry, AWAY.mac)["name"] == "Away laptop"
    assert client_state(hass, entity_registry, AWAY.mac, "connectivity") == STATE_OFF
    assert client_state(hass, entity_registry, AWAY.mac, "signal_strength") == STATE_UNAVAILABLE


@pytest.mark.parametrize("terminals", [None, TerminalState(())], ids=["unreadable", "empty"])
async def test_without_a_saved_record_the_saved_readings_are_unavailable_but_telemetry_is_not(
    hass, api, ac_snapshot, entity_registry, terminals
):
    api.async_get_snapshot.return_value = replace(ac_snapshot, terminals=terminals)
    await setup(hass, make_entry(hass))
    assert set(readings(hass, entity_registry, PHONE.mac).values()) == {STATE_UNAVAILABLE}
    assert client_state(hass, entity_registry, PHONE.mac, "connectivity") == STATE_ON
    assert client_state(hass, entity_registry, PHONE.mac, "signal_strength") == "-55"


async def test_a_connected_terminal_without_a_record_has_unavailable_saved_readings(
    hass, api, entity_registry
):
    await setup(hass, make_entry(hass))
    assert set(readings(hass, entity_registry, NEWCOMER.mac).values()) == {STATE_UNAVAILABLE}
    assert client_state(hass, entity_registry, NEWCOMER.mac, "connectivity") == STATE_ON


async def test_a_failed_poll_makes_the_saved_readings_unavailable_and_they_recover(
    hass, api, ac_snapshot, entity_registry
):
    coordinator = await setup(hass, make_entry(hass))
    coordinator.async_set_update_error(UpdateFailed("simulated controller outage"))
    await hass.async_block_till_done()
    for mac in (PHONE.mac, AWAY.mac):
        assert set(readings(hass, entity_registry, mac).values()) == {STATE_UNAVAILABLE}
    await publish(hass, coordinator, ac_snapshot)
    assert readings(hass, entity_registry, PHONE.mac) == PHONE_READINGS


async def test_a_reading_disabled_by_the_user_stays_disabled_through_a_reload(
    hass, api, entity_registry
):
    entry = make_entry(hass)
    await setup(hass, entry)
    disabled = entity_id(entity_registry, "sensor", uid(PHONE.mac, "upload_limit"))
    entity_registry.async_update_entity(disabled, disabled_by=er.RegistryEntryDisabler.USER)
    await hass.async_block_till_done()
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert entity_registry.async_get(disabled).disabled_by is er.RegistryEntryDisabler.USER
    assert hass.states.get(disabled) is None
    kept = {key: value for key, value in PHONE_READINGS.items() if key != "upload_limit"}
    assert readings(hass, entity_registry, PHONE.mac, skip=("upload_limit",)) == kept


async def test_a_terminal_that_appears_later_gets_its_readings_and_controls_once(
    hass, api, ac_snapshot, entity_registry
):
    entry = make_entry(hass)
    coordinator = await setup(hass, entry)
    before = entry_rows(hass, entry)
    late = wifi_client(7, "Late phone")
    grown = replace(
        ac_snapshot,
        clients=(*ac_snapshot.clients, late),
        terminals=TerminalState((*ac_snapshot.terminals.configurations, record(late.mac))),
    )
    for _ in range(3):
        await publish(hass, coordinator, grown)
    added = set(entry_rows(hass, entry)) - set(before)
    prefix = uid(late.mac, "")
    assert {key.removeprefix(prefix) for _, key in added if key.startswith(prefix)} >= (
        set(READINGS) | set(CONTROLS) | set(SCHEDULE_READINGS) | set(SCHEDULE_CONTROLS)
    )
    for key, domain in READINGS.items():
        row = entity_registry.async_get(entity_id(entity_registry, domain, uid(late.mac, key)))
        assert row.disabled_by is None
    assert all(entry_rows(hass, entry)[key] == value for key, value in before.items())


async def test_identifiers_are_unique_stable_and_never_get_a_numeric_suffix(
    hass, api, ac_snapshot, entity_registry
):
    entry = make_entry(hass)
    coordinator = await setup(hass, entry)
    before = entry_rows(hass, entry)
    legacy = {
        ("sensor", f"{CONTROLLER_MAC}_client_{PHONE.mac}_signal_strength"): (
            "sensor.example_phone_signal_strength"
        ),
        ("binary_sensor", f"{CONTROLLER_MAC}_client_{PHONE.mac}_connectivity"): (
            "binary_sensor.example_phone_connectivity"
        ),
        ("device_tracker", PHONE.mac): "device_tracker.example_phone",
    }
    assert {key: before[key] for key in legacy} == legacy
    roamed = replace(PHONE, ap_name="Study", name="Renamed phone")
    await publish(hass, coordinator, replace(ac_snapshot, clients=(roamed, NEWCOMER)))
    await publish(hass, coordinator, replace(ac_snapshot, clients=(NEWCOMER,)))
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry_rows(hass, entry) == before
    assert len(set(before.values())) == len(before)
    suffixed = [
        entity
        for (_, unique_id), entity in before.items()
        if "_terminal_" in unique_id and re.search(r"_\d+$", entity)
    ]
    assert not suffixed


async def test_saved_readings_add_no_request_and_no_private_values_to_diagnostics(
    hass, api, entity_registry
):
    entry = make_entry(hass)
    await setup(hass, entry)
    api.async_get_snapshot.assert_awaited_once_with()
    assert not write_calls(api)
    encoded = json.dumps(await async_get_config_entry_diagnostics(hass, entry))
    private = [PHONE.mac, AWAY.mac, "Example phone", "Away laptop", "Garage", "2000", "500"]
    assert not [value for value in private if value in encoded]


# The controls


@pytest.mark.parametrize("management", [False, True])
async def test_controls_are_registered_disabled_on_each_terminals_device_whatever_the_option(
    hass, api, entity_registry, management
):
    entry = make_entry(hass, management=management)
    await setup(hass, entry)
    for mac in (PHONE.mac, NEWCOMER.mac, AWAY.mac):
        device = device_of(hass, mac)
        for key, domain in CONTROLS.items():
            row = entity_registry.async_get(entity_id(entity_registry, domain, uid(mac, key)))
            assert row.disabled_by is er.RegistryEntryDisabler.INTEGRATION, (mac, key)
            assert row.entity_category is EntityCategory.CONFIG, key
            assert row.device_id == device.id, key
            assert row.config_entry_id == entry.entry_id
            assert hass.states.get(row.entity_id) is None
    assert not write_calls(api)


@pytest.mark.usefixtures("entity_registry_enabled_by_default")
async def test_enabled_controls_are_unavailable_and_send_nothing_until_management_is_on(
    hass, api, entity_registry
):
    await setup(hass, make_entry(hass, management=False))
    assert set(controls(hass, entity_registry, PHONE.mac).values()) == {STATE_UNAVAILABLE}
    await call(hass, entity_registry, "switch", "turn_off", "rate_limit_control")
    await call(hass, entity_registry, "button", "press", "disconnect_control")
    assert not write_calls(api)
    # Even a call that bypasses the availability check is refused by the coordinator.
    switch = entity_object(
        hass, entity_id(entity_registry, "switch", uid(PHONE.mac, "rate_limit_control"))
    )
    with pytest.raises(HomeAssistantError) as raised:
        await switch.async_turn_off()
    assert raised.value.translation_key == "control_disabled"
    assert not write_calls(api)


@pytest.mark.usefixtures("entity_registry_enabled_by_default")
async def test_with_management_on_the_controls_show_the_saved_record(hass, api, entity_registry):
    await setup(hass, make_entry(hass, management=True))
    assert controls(hass, entity_registry, PHONE.mac) == {
        "name_control": "Example phone",
        "disconnect_control": STATE_UNKNOWN,
        "disconnect_and_clear_control": STATE_UNKNOWN,
        "clear_offline_control": STATE_UNAVAILABLE,
        "rate_limit_control": STATE_ON,
        "upload_limit_control": "500",
        "download_limit_control": "2000",
        "binding_control": STATE_ON,
        "force_binding_control": STATE_OFF,
        "binding_target_control": "Garage - 5GHz",
    }
    for key in ("upload_limit_control", "download_limit_control"):
        state = state_of(hass, entity_registry, "number", uid(PHONE.mac, key))
        assert state.attributes["unit_of_measurement"] == "kB/s"
        assert (state.attributes["min"], state.attributes["max"], state.attributes["step"]) == (
            0,
            125000,
            1,
        )
        assert state.attributes["mode"] == "box"
    select = state_of(hass, entity_registry, "select", uid(PHONE.mac, "binding_target_control"))
    assert select.attributes["options"] == [
        "Garage - All radios",
        "Garage - 2.4GHz",
        "Garage - 5GHz",
        "Study - All radios",
        "Study - 2.4GHz",
        "Study - 5GHz",
    ]
    name = state_of(hass, entity_registry, "text", uid(PHONE.mac, "name_control"))
    assert (name.attributes["min"], name.attributes["max"], name.attributes["mode"]) == (
        1,
        64,
        "text",
    )
    assert not write_calls(api)


BROKEN_BINDING = {
    "binding_available": False,
    "binding_targets": (),
    "bound_target": None,
}
SCENARIOS = {
    "connected with a record": ({}, set(CONTROLS) - {"clear_offline_control"}),
    "binding cannot be edited": (
        BROKEN_BINDING,
        set(CONTROLS)
        - {"clear_offline_control", "binding_control", "force_binding_control"}
        - {"binding_target_control"},
    ),
    "rate limiting off": (
        {"rate_limit_enabled": False},
        set(CONTROLS) - {"clear_offline_control", "upload_limit_control", "download_limit_control"},
    ),
    "rate limiting unknown": (
        {"rate_limit_enabled": None},
        set(CONTROLS)
        - {"clear_offline_control", "rate_limit_control"}
        - {"upload_limit_control", "download_limit_control"},
    ),
    "binding off": (
        {"binding_enabled": False},
        set(CONTROLS) - {"clear_offline_control", "force_binding_control"},
    ),
    "binding unknown": (
        {"binding_enabled": None},
        set(CONTROLS) - {"clear_offline_control", "binding_control", "force_binding_control"},
    ),
    "cap unknown": (
        {"upload_limit": None},
        set(CONTROLS) - {"clear_offline_control", "upload_limit_control"},
    ),
    "no saved target": (
        {"binding_targets": ()},
        set(CONTROLS) - {"clear_offline_control", "binding_target_control"},
    ),
}


@pytest.mark.usefixtures("entity_registry_enabled_by_default")
@pytest.mark.parametrize("scenario", list(SCENARIOS))
async def test_each_control_is_available_only_when_its_source_and_state_allow_it(
    hass, api, ac_snapshot, entity_registry, scenario
):
    changes, expected = SCENARIOS[scenario]
    api.async_get_snapshot.return_value = replace(
        ac_snapshot, terminals=TerminalState((record(PHONE.mac, **changes), AWAY_RECORD))
    )
    await setup(hass, make_entry(hass, management=True))
    assert available_controls(hass, entity_registry, PHONE.mac) == expected


@pytest.mark.usefixtures("entity_registry_enabled_by_default")
async def test_availability_follows_the_connection_and_the_saved_record_separately(
    hass, api, ac_snapshot, entity_registry
):
    coordinator = await setup(hass, make_entry(hass, management=True))
    connected = set(CONTROLS) - {"clear_offline_control"}
    assert available_controls(hass, entity_registry, PHONE.mac) == connected
    # Connected without a record in a table that was read: only what needs no record. A rename
    # needs the record, which the API would refuse to do without.
    assert available_controls(hass, entity_registry, NEWCOMER.mac) == {"disconnect_control"}
    # Away with a record that cannot be bound: the record can be cleared, not a connection ended.
    assert available_controls(hass, entity_registry, AWAY.mac) == {
        "name_control",
        "clear_offline_control",
        "rate_limit_control",
    }
    # The same terminal after it left: its record can be cleared, and binding cannot be edited.
    left = record(PHONE.mac, **BROKEN_BINDING)
    await publish(
        hass,
        coordinator,
        replace(
            ac_snapshot,
            clients=(NEWCOMER,),
            terminals=TerminalState((left, AWAY_RECORD)),
        ),
    )
    assert available_controls(hass, entity_registry, PHONE.mac) == {
        "name_control",
        "clear_offline_control",
        "rate_limit_control",
        "upload_limit_control",
        "download_limit_control",
    }
    # Unreadable records: only the actions on the connection remain, and the name, which the API
    # checks against the record when it writes.
    await publish(hass, coordinator, replace(ac_snapshot, terminals=None))
    assert available_controls(hass, entity_registry, PHONE.mac) == {
        "name_control",
        "disconnect_control",
    }
    coordinator.async_set_update_error(UpdateFailed("simulated controller outage"))
    await hass.async_block_till_done()
    assert not available_controls(hass, entity_registry, PHONE.mac)


@pytest.mark.usefixtures("entity_registry_enabled_by_default")
async def test_binding_needs_a_current_client_even_when_the_saved_record_still_allows_it(
    hass, api, ac_snapshot, entity_registry
):
    # A positive service and AP name in the host table can outlive the real connection.
    snapshot = replace(ac_snapshot, clients=(NEWCOMER,))
    assert snapshot.terminals.configurations[0].binding_available
    api.async_get_snapshot.return_value = snapshot
    await setup(hass, make_entry(hass, management=True))

    assert available_controls(hass, entity_registry, PHONE.mac) == {
        "name_control",
        "clear_offline_control",
        "rate_limit_control",
        "upload_limit_control",
        "download_limit_control",
    }
    assert readings(hass, entity_registry, PHONE.mac) == PHONE_READINGS
    assert client_state(hass, entity_registry, PHONE.mac, "connectivity") == STATE_OFF
    assert (
        state_of(hass, entity_registry, "select", uid(PHONE.mac, "allowed_time_mode_control")).state
        == "any"
    )
    for key in ("binding_control", "force_binding_control"):
        for service in ("turn_on", "turn_off"):
            await call(hass, entity_registry, "switch", service, key)
    await call(
        hass,
        entity_registry,
        "select",
        "select_option",
        "binding_target_control",
        option="Study - 2.4GHz",
    )
    assert not write_calls(api)


# One request per action


WRITES = [
    (
        "text",
        "set_value",
        "name_control",
        {"value": "Kitchen phone"},
        TerminalField.NAME,
        "Kitchen phone",
    ),
    ("switch", "turn_off", "rate_limit_control", {}, TerminalField.RATE_LIMIT_ENABLED, False),
    ("switch", "turn_on", "rate_limit_control", {}, TerminalField.RATE_LIMIT_ENABLED, True),
    (
        "number",
        "set_value",
        "upload_limit_control",
        {"value": 750},
        TerminalField.UPLOAD_LIMIT,
        750,
    ),
    ("number", "set_value", "upload_limit_control", {"value": 0}, TerminalField.UPLOAD_LIMIT, 0),
    ("number", "set_value", "upload_limit_control", {"value": 15}, TerminalField.UPLOAD_LIMIT, 15),
    (
        "number",
        "set_value",
        "download_limit_control",
        {"value": 125000.0},
        TerminalField.DOWNLOAD_LIMIT,
        125000,
    ),
    ("switch", "turn_off", "binding_control", {}, TerminalField.BINDING_ENABLED, False),
    ("switch", "turn_on", "binding_control", {}, TerminalField.BINDING_ENABLED, True),
    ("switch", "turn_on", "force_binding_control", {}, TerminalField.FORCE_BINDING, True),
    ("switch", "turn_off", "force_binding_control", {}, TerminalField.FORCE_BINDING, False),
    (
        "select",
        "select_option",
        "binding_target_control",
        {"option": "Study - 2.4GHz"},
        TerminalField.BINDING_TARGET,
        f"{STUDY.mac}/1",
    ),
    (
        "select",
        "select_option",
        "binding_target_control",
        {"option": "Garage - All radios"},
        TerminalField.BINDING_TARGET,
        f"{GARAGE.mac}/all",
    ),
]


def refreshed(api) -> tuple[int, int, int]:
    return (
        api.async_get_snapshot.await_count,
        api.async_get_monitoring.await_count,
        api.async_get_configuration.await_count,
    )


@pytest.mark.usefixtures("entity_registry_enabled_by_default")
@pytest.mark.parametrize(("domain", "service", "key", "data", "field", "value"), WRITES)
async def test_a_field_control_sends_one_write_for_its_own_field_and_then_reads_again(
    hass, api, entity_registry, domain, service, key, data, field, value
):
    await setup(hass, make_entry(hass, management=True))
    before = refreshed(api)
    await call(hass, entity_registry, domain, service, key, **data)
    api.async_set_terminal_field.assert_awaited_once_with(PHONE.mac, field, value)
    assert type(api.async_set_terminal_field.await_args.args[2]) is type(value)
    assert write_calls(api) == {"async_set_terminal_field": 1}
    assert tuple(now - then for now, then in zip(refreshed(api), before, strict=True)) == (1, 1, 1)


@pytest.mark.usefixtures("entity_registry_enabled_by_default")
async def test_the_disconnect_and_clear_buttons_call_their_own_api_method(
    hass, api, entity_registry
):
    await setup(hass, make_entry(hass, management=True))
    await call(hass, entity_registry, "button", "press", "disconnect_control")
    api.async_disconnect_terminal.assert_awaited_once_with(PHONE.mac)
    assert write_calls(api) == {"async_disconnect_terminal": 1}
    await call(hass, entity_registry, "button", "press", "disconnect_and_clear_control")
    api.async_clear_terminal.assert_awaited_once_with(PHONE.mac, disconnect=True)
    await call(hass, entity_registry, "button", "press", "clear_offline_control", AWAY.mac)
    assert api.async_clear_terminal.await_args_list[1].args == (AWAY.mac,)
    assert api.async_clear_terminal.await_args_list[1].kwargs == {"disconnect": False}
    assert write_calls(api) == {"async_disconnect_terminal": 1, "async_clear_terminal": 2}
    # A button that does not fit the terminal's connection sends nothing.
    await call(hass, entity_registry, "button", "press", "clear_offline_control")
    await call(hass, entity_registry, "button", "press", "disconnect_control", AWAY.mac)
    await call(
        hass, entity_registry, "button", "press", "disconnect_and_clear_control", NEWCOMER.mac
    )
    assert write_calls(api) == {"async_disconnect_terminal": 1, "async_clear_terminal": 2}


@pytest.mark.usefixtures("entity_registry_enabled_by_default")
async def test_the_name_of_a_connected_terminal_can_be_set_while_the_records_are_unreadable(
    hass, api, ac_snapshot, entity_registry
):
    api.async_get_snapshot.return_value = replace(ac_snapshot, terminals=None)
    await setup(hass, make_entry(hass, management=True))
    state = state_of(hass, entity_registry, "text", uid(PHONE.mac, "name_control"))
    assert state.state == "Example phone"
    await call(hass, entity_registry, "text", "set_value", "name_control", value="Den")
    api.async_set_terminal_field.assert_awaited_once_with(PHONE.mac, TerminalField.NAME, "Den")


@pytest.mark.usefixtures("entity_registry_enabled_by_default")
async def test_a_name_is_not_offered_for_a_terminal_that_a_readable_table_does_not_list(
    hass, api, entity_registry
):
    await setup(hass, make_entry(hass, management=True))
    state = state_of(hass, entity_registry, "text", uid(NEWCOMER.mac, "name_control"))
    assert state.state == STATE_UNAVAILABLE
    await call(
        hass, entity_registry, "text", "set_value", "name_control", NEWCOMER.mac, value="Den"
    )
    assert not write_calls(api)


# Values that are refused before anything is sent


@pytest.mark.usefixtures("entity_registry_enabled_by_default")
@pytest.mark.parametrize(
    ("domain", "key", "data", "reason"),
    [
        ("number", "upload_limit_control", {"value": 1}, LIMIT_RULE),
        ("number", "upload_limit_control", {"value": 14}, LIMIT_RULE),
        ("number", "download_limit_control", {"value": 7.5}, LIMIT_RULE),
        ("text", "name_control", {"value": "\u6d4b" * 22}, NAME_RULE),
        ("text", "name_control", {"value": "a\u0007b"}, NAME_RULE),
        ("text", "name_control", {"value": "\u3000"}, NAME_RULE),
    ],
)
async def test_a_value_that_the_terminal_page_would_refuse_is_refused_before_it_is_sent(
    hass, api, entity_registry, domain, key, data, reason
):
    await setup(hass, make_entry(hass, management=True))
    with pytest.raises(ServiceValidationError) as raised:
        await call(hass, entity_registry, domain, "set_value", key, **data)
    assert raised.value.translation_key == "invalid_value"
    assert raised.value.translation_placeholders == {"reason": reason}
    assert not write_calls(api)


@pytest.mark.usefixtures("entity_registry_enabled_by_default")
@pytest.mark.parametrize(
    ("domain", "key", "data"),
    [
        ("number", "upload_limit_control", {"value": -1}),
        ("number", "upload_limit_control", {"value": 125001}),
        ("text", "name_control", {"value": ""}),
        ("text", "name_control", {"value": "x" * 65}),
        ("select", "binding_target_control", {"option": "Nowhere - 5GHz"}),
    ],
)
async def test_values_outside_the_ranges_that_the_entity_declares_are_refused_by_home_assistant(
    hass, api, entity_registry, domain, key, data
):
    await setup(hass, make_entry(hass, management=True))
    service = "select_option" if domain == "select" else "set_value"
    with pytest.raises(ServiceValidationError):
        await call(hass, entity_registry, domain, service, key, **data)
    assert not write_calls(api)


@pytest.mark.usefixtures("entity_registry_enabled_by_default")
async def test_names_are_limited_in_bytes_like_the_terminal_page_and_not_in_characters(
    hass, api, entity_registry
):
    await setup(hass, make_entry(hass, management=True))
    name = "\u6d4b" * 21
    assert len(name.encode()) == 63
    await call(hass, entity_registry, "text", "set_value", "name_control", value=name)
    api.async_set_terminal_field.assert_awaited_once_with(PHONE.mac, TerminalField.NAME, name)


@pytest.mark.usefixtures("entity_registry_enabled_by_default")
async def test_binding_cannot_be_turned_on_without_a_saved_target_but_can_be_turned_off(
    hass, api, ac_snapshot, entity_registry
):
    api.async_get_snapshot.return_value = replace(
        ac_snapshot,
        terminals=TerminalState(
            (record(PHONE.mac, binding_enabled=False, bound_target=None, bound_radio_ids=()),)
        ),
    )
    await setup(hass, make_entry(hass, management=True))
    with pytest.raises(ServiceValidationError) as raised:
        await call(hass, entity_registry, "switch", "turn_on", "binding_control")
    assert raised.value.translation_placeholders == {"reason": CHOOSE_TARGET}
    assert not write_calls(api)
    # Choosing a target is the way to turn binding on; the choice is one write for the target.
    await call(
        hass,
        entity_registry,
        "select",
        "select_option",
        "binding_target_control",
        option="Study - 5GHz",
    )
    api.async_set_terminal_field.assert_awaited_once_with(
        PHONE.mac, TerminalField.BINDING_TARGET, f"{STUDY.mac}/2"
    )


# Refusals and failures


ONE_CONTROL_EACH = [
    ("text", "set_value", "name_control", PHONE.mac, {"value": "Kitchen phone"}),
    ("switch", "turn_off", "rate_limit_control", PHONE.mac, {}),
    ("number", "set_value", "upload_limit_control", PHONE.mac, {"value": 750}),
    ("number", "set_value", "download_limit_control", PHONE.mac, {"value": 750}),
    ("switch", "turn_off", "binding_control", PHONE.mac, {}),
    ("switch", "turn_on", "force_binding_control", PHONE.mac, {}),
    ("select", "select_option", "binding_target_control", PHONE.mac, {"option": "Study - 5GHz"}),
    ("button", "press", "disconnect_control", PHONE.mac, {}),
    ("button", "press", "disconnect_and_clear_control", PHONE.mac, {}),
    ("button", "press", "clear_offline_control", AWAY.mac, {}),
]
METHODS = ("async_set_terminal_field", "async_disconnect_terminal", "async_clear_terminal")


@pytest.mark.usefixtures("entity_registry_enabled_by_default")
@pytest.mark.parametrize(("domain", "service", "key", "mac", "data"), ONE_CONTROL_EACH)
@pytest.mark.parametrize(
    ("error", "translation_key"),
    [
        (Ac100TerminalError("The terminal is not connected."), "terminal_action_refused"),
        (
            Ac100TerminalPartialError("The first step was accepted.", "link"),
            "terminal_action_partial",
        ),
    ],
)
async def test_a_refused_or_partial_terminal_action_is_reported_once_in_english_and_not_repeated(
    hass, api, entity_registry, domain, service, key, mac, data, error, translation_key
):
    for method in METHODS:
        getattr(api, method).side_effect = error
    await setup(hass, make_entry(hass, management=True))
    before_state = controls(hass, entity_registry, mac)
    reads = refreshed(api)
    with pytest.raises(HomeAssistantError) as raised:
        await call(hass, entity_registry, domain, service, key, mac, **data)
    assert raised.value.translation_domain == DOMAIN
    assert raised.value.translation_key == translation_key
    assert raised.value.translation_placeholders == {"reason": str(error)}
    assert sum(write_calls(api).values()) == 1
    # The outcome is read on the next scheduled update; nothing is shown as done in the meantime.
    # Home Assistant itself stamps the time of a button press, whatever its outcome.
    assert refreshed(api) == reads
    after_state = controls(hass, entity_registry, mac)
    if domain == "button":
        before_state.pop(key)
        after_state.pop(key)
    assert after_state == before_state


@pytest.mark.usefixtures("entity_registry_enabled_by_default")
@pytest.mark.parametrize(
    ("error", "translation_key"),
    [
        (Ac100SessionExpiredError("expired"), "session_expired"),
        (Ac100SessionBusyError("busy"), "session_busy"),
        (Ac100AccountLockedError("locked"), "account_locked"),
        (Ac100AuthError("denied"), "control_rejected"),
        (Ac100ConnectionError("down"), "control_failed"),
    ],
)
async def test_the_existing_session_and_permission_errors_apply_to_terminal_actions_too(
    hass, api, entity_registry, error, translation_key
):
    api.async_set_terminal_field.side_effect = error
    await setup(hass, make_entry(hass, management=True))
    with pytest.raises(HomeAssistantError) as raised:
        await call(hass, entity_registry, "switch", "turn_off", "rate_limit_control")
    assert raised.value.translation_key == translation_key
    api.async_set_terminal_field.assert_awaited_once()


# The binding choices


def test_binding_targets_are_named_by_ap_device_name_and_band_and_stay_unique(
    hass, api, ac_snapshot
):
    living = [access_point(1, "Living"), access_point(2, "living")]
    tri_band = access_point(3, "Hall", units=(("1", "2.4GHz"), ("2", "5GHz"), ("3", "5GHz")))
    targets = (
        target(living[0], ids=("1",)),
        target(living[0], "2", "5GHz", ("1",)),
        target(living[1], "2", "5GHz", ("2",)),
        target(tri_band, "1", "2.4GHz", ("3",)),
        target(tri_band, "2", "5GHz", ("4",)),
        target(tri_band, "3", "5GHz", ("5",)),
        target(tri_band, "4", "", ("6",)),
        target(tri_band, "5", "---", ("7",)),
    )
    coordinator = SimpleNamespace(
        data=replace(ac_snapshot, aps=(*living, tri_band)),
    )
    labels = binding_target_labels(coordinator, targets)
    assert list(labels.values()) == [
        "Living AABB01 - All radios",
        "Living AABB01 - 5GHz",
        "living AABB02 - 5GHz",
        "Hall - 2.4GHz",
        "Hall - 5GHz (radio 2)",
        "Hall - 5GHz (radio 3)",
        "Hall - Radio 4",
        "Hall - Radio 5",
    ]
    assert len(set(labels.values())) == len(targets)
    assert list(labels) == [item.token for item in targets]


@pytest.mark.usefixtures("entity_registry_enabled_by_default")
async def test_the_binding_choice_follows_the_saved_target_and_is_unknown_when_none_matches(
    hass, api, ac_snapshot, entity_registry
):
    coordinator = await setup(hass, make_entry(hass, management=True))
    key = uid(PHONE.mac, "binding_target_control")
    assert state_of(hass, entity_registry, "select", key).state == "Garage - 5GHz"
    for token, label in (
        (f"{STUDY.mac}/all", "Study - All radios"),
        (f"{STUDY.mac}/1", "Study - 2.4GHz"),
        (None, STATE_UNKNOWN),
    ):
        changed = record(PHONE.mac, bound_target=token)
        await publish(
            hass,
            coordinator,
            replace(ac_snapshot, terminals=TerminalState((changed, AWAY_RECORD))),
        )
        assert state_of(hass, entity_registry, "select", key).state == label


@pytest.mark.usefixtures("entity_registry_enabled_by_default")
async def test_the_name_control_is_unknown_for_a_name_the_page_would_not_accept(
    hass, api, ac_snapshot, entity_registry
):
    long_name = "x" * 70
    api.async_get_snapshot.return_value = replace(
        ac_snapshot,
        clients=(replace(PHONE, name=long_name), NEWCOMER),
        terminals=TerminalState((record(PHONE.mac, name=None), AWAY_RECORD)),
    )
    await setup(hass, make_entry(hass, management=True))
    # The connection's name is the fallback; it is too long, so the control shows nothing.
    state = state_of(hass, entity_registry, "text", uid(PHONE.mac, "name_control"))
    assert state.state == STATE_UNKNOWN
    entity = entity_object(hass, state.entity_id)
    assert isinstance(entity, Ac100TerminalNameText)
    assert entity.available


# Schedule hooks, names and icons


@pytest.mark.usefixtures("entity_registry_enabled_by_default")
async def test_every_platform_adds_the_scalar_and_the_schedule_entities_of_each_terminal(
    hass, api, entity_registry
):
    entry = make_entry(hass, management=True)
    await setup(hass, entry)
    rows = entry_rows(hass, entry)
    for mac in (PHONE.mac, NEWCOMER.mac, AWAY.mac):
        for table in (READINGS, CONTROLS, SCHEDULE_READINGS, SCHEDULE_CONTROLS):
            for key, domain in table.items():
                assert (domain, uid(mac, key)) in rows, (mac, key)
    terminal_rows = [key for key in rows if "_terminal_" in key[1]]
    expected = 3 * (len(READINGS) + len(CONTROLS) + len(SCHEDULE_READINGS) + len(SCHEDULE_CONTROLS))
    assert len(terminal_rows) == expected


@pytest.mark.usefixtures("entity_registry_enabled_by_default")
async def test_every_terminal_entity_has_an_english_name_and_every_scalar_one_an_icon(
    hass, api, entity_registry
):
    strings = json.loads((COMPONENT / "strings.json").read_text())
    icons = json.loads((COMPONENT / "icons.json").read_text())
    assert json.loads((COMPONENT / "translations/en.json").read_text()) == strings
    entry = make_entry(hass, management=True)
    await setup(hass, entry)
    scalar = set(READINGS) | set(CONTROLS)
    checked = 0
    for (domain, unique_id), entity in entry_rows(hass, entry).items():
        if "_terminal_" not in unique_id:
            continue
        row = entity_registry.async_get(entity)
        assert row.translation_key, unique_id
        translation = strings["entity"][domain][row.translation_key]
        assert translation["name"].strip(), unique_id
        placeholders = set(re.findall(r"{(\w+)}", translation["name"]))
        assert placeholders <= set(entity_object(hass, entity).translation_placeholders), unique_id
        if unique_id.rsplit("_terminal_", 1)[1].split("_", 1)[1] in scalar:
            assert icons["entity"][domain][row.translation_key]["default"].startswith("mdi:")
        if domain == "select":
            options = entity_object(hass, entity).options
            if row.translation_key == "terminal_schedule_mode":
                assert set(translation["state"]) == set(options)
        checked += 1
    assert checked
    for key in ("terminal_action_refused", "terminal_action_partial"):
        assert "{reason}" in strings["exceptions"][key]["message"]
