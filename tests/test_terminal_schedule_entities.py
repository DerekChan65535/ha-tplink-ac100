"""Native schedule entities of a terminal: readings on by default, controls opt-in and off.

The schedule is the terminal's saved record on the controller, so the entities exist for every
terminal that the controller knows, also one that is away. The real coordinator and platforms run
around a fake API; no controller is contacted.
"""

import json
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
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.update_coordinator import UpdateFailed
from pytest_homeassistant.common import MockConfigEntry

from custom_components.tplink_ac100.api import (
    Ac100Api,
    Ac100ConnectionError,
    Ac100TerminalError,
    Ac100TerminalPartialError,
)
from custom_components.tplink_ac100.const import CONF_ENABLE_CONTROL, DOMAIN
from custom_components.tplink_ac100.models import AccessPoint, Client, Controller, Snapshot
from custom_components.tplink_ac100.monitoring import ConfigurationState, MonitoringState
from custom_components.tplink_ac100.terminal import (
    TerminalConfiguration,
    TerminalField,
    TerminalState,
)
from custom_components.tplink_ac100.terminal_schedule import (
    DAYS,
    TerminalSchedule,
    change_terminal_schedule,
)

pytestmark = pytest.mark.usefixtures("enable_custom_integrations")

CONTROLLER_MAC = "02:00:00:00:00:01"
COMPONENT = Path(__file__).parents[1] / "custom_components/tplink_ac100"
PLATFORMS = [Platform.SELECT, Platform.SENSOR, Platform.SWITCH, Platform.TEXT]

WORKER = "02:00:00:cc:dd:01"  # present, manual schedule on weekdays
KIOSK = "02:00:00:cc:dd:02"  # away, calendar schedule
GUEST = "02:00:00:cc:dd:03"  # present, no schedule: any time
BLIND = "02:00:00:cc:dd:04"  # present, schedule could not be read
LATE = "02:00:00:cc:dd:05"  # first seen after setup

MANUAL = TerminalSchedule("manual", weekdays=DAYS[:5], windows=("08:00-12:00", "13:00-17:30"))
CALENDAR = TerminalSchedule(
    "calendar",
    calendar=(
        ("00:00-06:00", "08:00-24:00"),
        (),
        ("09:00-10:00",),
        (),
        (),
        (),
        ("00:00-24:00",),
    ),
)
NAMES = {
    WORKER: "Example worker",
    KIOSK: "Example kiosk",
    GUEST: "Example guest",
    BLIND: "Example blind",
    LATE: "Example late",
}

READINGS = (
    "allowed_time_mode",
    "manual_weekdays",
    "manual_windows",
    *(f"calendar_{d}" for d in DAYS),
)
SELECTS = ("allowed_time_mode_control",)
SWITCHES = tuple(f"manual_{day}_control" for day in DAYS)
TEXTS = ("manual_windows_control", *(f"calendar_{day}_control" for day in DAYS))
CONTROLS = (*SELECTS, *SWITCHES, *TEXTS)
DOMAIN_OF = {
    **dict.fromkeys(READINGS, "sensor"),
    **dict.fromkeys(SELECTS, "select"),
    **dict.fromkeys(SWITCHES, "switch"),
    **dict.fromkeys(TEXTS, "text"),
}
TRANSLATION_KEY = {
    "allowed_time_mode": "terminal_schedule_mode",
    "manual_weekdays": "terminal_schedule_manual_weekdays",
    "manual_windows": "terminal_schedule_manual_windows",
    "allowed_time_mode_control": "terminal_schedule_mode",
    "manual_windows_control": "terminal_schedule_manual_windows",
    **{f"calendar_{day}": "terminal_schedule_calendar_day" for day in DAYS},
    **{f"calendar_{day}_control": "terminal_schedule_calendar_day" for day in DAYS},
    **{f"manual_{day}_control": "terminal_schedule_manual_day" for day in DAYS},
}
UNREADABLE = "unreadable"
UNKNOWN_MODE = "unknown mode"
EVERYTHING = change_terminal_schedule(None, "allowed_time_mode", "calendar")


def client(mac: str) -> Client:
    return Client(
        entry_id=mac[-2:],
        mac=mac,
        name=NAMES[mac],
        ip="192.0.2.10",
        ipv6="",
        ssid="Example wireless",
        ap_name="Garage",
        radios=(),
    )


def saved(mac: str, schedule) -> TerminalConfiguration:
    """The saved record: None is any time and UNREADABLE is a schedule that could not be read."""
    if schedule is UNREADABLE:
        return TerminalConfiguration(mac, name=NAMES.get(mac))
    if schedule is UNKNOWN_MODE:
        return TerminalConfiguration(
            mac, name=NAMES.get(mac), allowed_time_mode="weekly", schedule_available=True
        )
    return TerminalConfiguration(
        mac,
        name=NAMES.get(mac),
        allowed_time_mode="any" if schedule is None else schedule.mode,
        schedule=schedule,
        schedule_available=True,
    )


@pytest.fixture
def world():
    """What the fake controller holds: each terminal's schedule and who is connected."""
    return SimpleNamespace(
        schedules={WORKER: MANUAL, KIOSK: CALENDAR, GUEST: None, BLIND: UNREADABLE},
        present=[WORKER, GUEST, BLIND],
        readable=True,
    )


def snapshot_of(world) -> Snapshot:
    ap = AccessPoint(
        "0", "Garage", "TL-XAP1502GI-PoE", "02:00:00:aa:bb:01", "192.0.2.2", "1", True, 600, ()
    )
    terminals = (
        TerminalState(tuple(saved(mac, item) for mac, item in world.schedules.items()))
        if world.readable
        else None
    )
    clients = tuple(client(mac) for mac in world.present)
    controller = Controller(CONTROLLER_MAC, "TL-AC100", "5.0", "1.0", {}, 40.0, 7200)
    return Snapshot(controller, (ap,), clients, terminals=terminals)


@pytest.fixture
def schedule_setup(hass, world):
    """The real coordinator and platforms around a fake API that stores each schedule."""
    api = create_autospec(Ac100Api, instance=True)  # every call is checked against the real API
    api.async_get_snapshot.side_effect = lambda: snapshot_of(world)
    api.async_get_monitoring.return_value = MonitoringState()
    api.async_get_configuration.return_value = ConfigurationState()

    async def set_field(mac, field, value):
        world.schedules[mac] = change_terminal_schedule(world.schedules[mac], field, value)

    api.async_set_terminal_field.side_effect = set_field

    async def create(*, management=True):
        entry = MockConfigEntry(
            domain=DOMAIN,
            unique_id=CONTROLLER_MAC,
            data={"host": "http://192.0.2.1", "username": "example", "password": "example-only"},
            options={CONF_ENABLE_CONTROL: management},
        )
        entry.add_to_hass(hass)
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        return entry, entry.runtime_data

    async def publish(coordinator):
        coordinator.async_set_updated_data(snapshot_of(world))
        await hass.async_block_till_done()

    with (
        patch("custom_components.tplink_ac100.Ac100Api", return_value=api),
        patch("custom_components.tplink_ac100.PLATFORMS", PLATFORMS),
    ):
        yield SimpleNamespace(api=api, create=create, publish=publish, world=world)


def uid(mac: str, key: str) -> str:
    return f"{CONTROLLER_MAC}_terminal_{mac}_{key}"


def entity_id(registry, mac: str, key: str) -> str:
    result = registry.async_get_entity_id(DOMAIN_OF[key], DOMAIN, uid(mac, key))
    assert result is not None, (mac, key)
    return result


def state_of(hass, registry, mac: str, key: str):
    state = hass.states.get(entity_id(registry, mac, key))
    assert state is not None, (mac, key)
    return state


def states(hass, registry, mac: str, keys) -> dict[str, str]:
    return {key: state_of(hass, registry, mac, key).state for key in keys}


async def call(hass, registry, mac: str, key: str, service: str, **data):
    await hass.services.async_call(
        DOMAIN_OF[key], service, {"entity_id": entity_id(registry, mac, key), **data}, blocking=True
    )


def own_rows(registry, entry, mac: str):
    """The registry rows of the schedule entities of one terminal."""
    keys = {uid(mac, key) for key in (*READINGS, *CONTROLS)}
    return [
        row
        for row in er.async_entries_for_config_entry(registry, entry.entry_id)
        if row.unique_id in keys
    ]


def only_calls(api):
    """Each terminal method of the API that was awaited."""
    return {
        name: getattr(api, name).await_count
        for name in (
            "async_set_terminal_field",
            "async_disconnect_terminal",
            "async_clear_terminal",
        )
        if getattr(api, name).await_count
    }


UNKNOWN_DAYS = {f"calendar_{day}": STATE_UNKNOWN for day in DAYS}
READING_STATES = {
    WORKER: {
        "allowed_time_mode": "manual",
        "manual_weekdays": "monday, tuesday, wednesday, thursday, friday",
        "manual_windows": "08:00-12:00, 13:00-17:30",
    }
    | UNKNOWN_DAYS,
    KIOSK: {
        "allowed_time_mode": "calendar",
        "manual_weekdays": STATE_UNKNOWN,
        "manual_windows": STATE_UNKNOWN,
        "calendar_monday": "00:00-06:00, 08:00-24:00",
        "calendar_tuesday": "none",
        "calendar_wednesday": "09:00-10:00",
        "calendar_thursday": "none",
        "calendar_friday": "none",
        "calendar_saturday": "none",
        "calendar_sunday": "00:00-24:00",
    },
    GUEST: {
        "allowed_time_mode": "any",
        "manual_weekdays": STATE_UNKNOWN,
        "manual_windows": STATE_UNKNOWN,
    }
    | UNKNOWN_DAYS,
    BLIND: dict.fromkeys(READINGS, STATE_UNAVAILABLE),
}


# Where the entities are and what they start as


async def test_each_terminal_has_its_readings_on_and_its_controls_off_on_its_own_device(
    hass, schedule_setup, entity_registry, device_registry
):
    entry, _ = await schedule_setup.create(management=False)
    for mac in (WORKER, KIOSK, GUEST, BLIND):
        device = device_registry.async_get_device_by_connection(
            (dr.CONNECTION_NETWORK_MAC, mac), entry.entry_id
        )
        assert device is not None and not device.identifiers
        rows = {row.unique_id: row for row in own_rows(entity_registry, entry, mac)}
        assert set(rows) == {uid(mac, key) for key in (*READINGS, *CONTROLS)}
        for key in (*READINGS, *CONTROLS):
            row = rows[uid(mac, key)]
            assert row.domain == DOMAIN_OF[key], key
            assert row.device_id == device.id, key
            assert row.config_entry_id == entry.entry_id
            assert row.translation_key == TRANSLATION_KEY[key], key
            if key in READINGS:
                assert row.disabled_by is None, key
                assert row.entity_category is EntityCategory.DIAGNOSTIC, key
                assert hass.states.get(row.entity_id) is not None, key
            else:
                assert row.disabled_by is er.RegistryEntryDisabler.INTEGRATION, key
                assert row.entity_category is EntityCategory.CONFIG, key
                assert hass.states.get(row.entity_id) is None, key


async def test_setting_up_the_entities_sends_nothing_to_the_controller(hass, schedule_setup):
    await schedule_setup.create()
    api = schedule_setup.api
    assert api.async_get_snapshot.await_count == 1
    assert only_calls(api) == {}


async def test_times_are_the_controllers_own_whatever_the_home_assistant_time_zone(
    hass, schedule_setup, entity_registry
):
    await hass.config.async_set_time_zone("Pacific/Kiritimati")
    _, coordinator = await schedule_setup.create(management=False)
    assert states(hass, entity_registry, WORKER, READINGS) == READING_STATES[WORKER]
    await hass.config.async_set_time_zone("America/Adak")
    await schedule_setup.publish(coordinator)
    assert states(hass, entity_registry, KIOSK, READINGS) == READING_STATES[KIOSK]


@pytest.mark.parametrize("mac", [WORKER, KIOSK, GUEST, BLIND])
async def test_readings_show_the_saved_schedule_and_leave_other_fields_unknown(
    hass, schedule_setup, entity_registry, mac
):
    await schedule_setup.create(management=False)
    assert states(hass, entity_registry, mac, READINGS) == READING_STATES[mac]


async def test_the_mode_reading_is_an_enumeration_and_the_others_are_plain_text(
    hass, schedule_setup, entity_registry
):
    await schedule_setup.create(management=False)
    mode = state_of(hass, entity_registry, WORKER, "allowed_time_mode")
    assert mode.attributes["options"] == ["any", "manual", "calendar"]
    assert mode.attributes["device_class"] == "enum"
    for key in READINGS:
        attributes = state_of(hass, entity_registry, WORKER, key).attributes
        assert "state_class" not in attributes and "unit_of_measurement" not in attributes


async def test_an_absent_terminal_keeps_its_saved_schedule_without_being_present(
    hass, schedule_setup, entity_registry
):
    _, coordinator = await schedule_setup.create(management=False)
    assert KIOSK not in {item.mac for item in coordinator.data.clients}
    assert state_of(hass, entity_registry, KIOSK, "allowed_time_mode").state == "calendar"
    assert state_of(hass, entity_registry, KIOSK, "calendar_sunday").state == "00:00-24:00"


async def test_readings_follow_the_schedule_and_cost_no_request(
    hass, schedule_setup, entity_registry
):
    _, coordinator = await schedule_setup.create(management=False)
    world, api = schedule_setup.world, schedule_setup.api
    api.reset_mock()
    world.schedules[WORKER] = TerminalSchedule(
        "manual", weekdays=("monday",), windows=("00:00-24:00",)
    )
    world.schedules[GUEST] = TerminalSchedule("calendar", calendar=(("10:00-11:00",), *((),) * 6))
    world.schedules[BLIND] = None
    await schedule_setup.publish(coordinator)
    assert states(hass, entity_registry, WORKER, READINGS[:3]) == {
        "allowed_time_mode": "manual",
        "manual_weekdays": "monday",
        "manual_windows": "00:00-24:00",
    }
    assert state_of(hass, entity_registry, GUEST, "calendar_monday").state == "10:00-11:00"
    assert state_of(hass, entity_registry, BLIND, "allowed_time_mode").state == "any"
    assert api.mock_calls == []


@pytest.mark.parametrize("failure", ["table", "poll", "record"])
async def test_readings_are_unavailable_when_their_source_is_not_there(
    hass, schedule_setup, entity_registry, failure
):
    _, coordinator = await schedule_setup.create(management=False)
    world = schedule_setup.world
    if failure == "table":
        world.readable = False
        await schedule_setup.publish(coordinator)
    elif failure == "poll":
        coordinator.async_set_update_error(UpdateFailed("poll failed"))
        await hass.async_block_till_done()
    else:
        del world.schedules[WORKER]
        await schedule_setup.publish(coordinator)
    assert set(states(hass, entity_registry, WORKER, READINGS).values()) == {STATE_UNAVAILABLE}
    if failure == "record":
        assert states(hass, entity_registry, KIOSK, READINGS) == READING_STATES[KIOSK]
    world.readable = True
    world.schedules[WORKER] = MANUAL
    await schedule_setup.publish(coordinator)
    assert states(hass, entity_registry, WORKER, READINGS) == READING_STATES[WORKER]


async def test_a_terminal_first_seen_later_gets_one_set_and_the_others_keep_theirs(
    hass, schedule_setup, entity_registry
):
    entry, coordinator = await schedule_setup.create(management=False)
    before = {mac: len(own_rows(entity_registry, entry, mac)) for mac in (WORKER, KIOSK)}
    schedule_setup.world.schedules[LATE] = CALENDAR
    await schedule_setup.publish(coordinator)
    await schedule_setup.publish(coordinator)
    assert len(own_rows(entity_registry, entry, LATE)) == len(READINGS) + len(CONTROLS)
    assert state_of(hass, entity_registry, LATE, "calendar_wednesday").state == "09:00-10:00"
    assert {mac: len(own_rows(entity_registry, entry, mac)) for mac in (WORKER, KIOSK)} == before


async def test_what_the_user_disabled_or_enabled_stays_as_it_is(
    hass, schedule_setup, entity_registry
):
    entry, _ = await schedule_setup.create(management=True)
    reading = entity_id(entity_registry, WORKER, "calendar_monday")
    control = entity_id(entity_registry, WORKER, "manual_windows_control")
    entity_registry.async_update_entity(reading, disabled_by=er.RegistryEntryDisabler.USER)
    entity_registry.async_update_entity(control, disabled_by=None)
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert entity_registry.async_get(reading).disabled_by is er.RegistryEntryDisabler.USER
    assert hass.states.get(reading) is None
    assert entity_registry.async_get(control).disabled_by is None
    assert hass.states.get(control).state == "08:00-12:00, 13:00-17:30"
    other = entity_id(entity_registry, WORKER, "manual_friday_control")
    assert entity_registry.async_get(other).disabled_by is er.RegistryEntryDisabler.INTEGRATION


# Controls: when they are available


@pytest.mark.usefixtures("entity_registry_enabled_by_default")
async def test_controls_are_listed_but_unavailable_until_device_control_is_on(
    hass, schedule_setup, entity_registry
):
    await schedule_setup.create(management=False)
    for mac in (WORKER, KIOSK, GUEST, BLIND):
        assert set(states(hass, entity_registry, mac, CONTROLS).values()) == {STATE_UNAVAILABLE}
    for key, service, data in (
        ("allowed_time_mode_control", "select_option", {"option": "any"}),
        ("manual_monday_control", "turn_off", {}),
        ("manual_windows_control", "set_value", {"value": "08:00-09:00"}),
    ):
        await call(hass, entity_registry, WORKER, key, service, **data)
    assert only_calls(schedule_setup.api) == {}


@pytest.mark.usefixtures("entity_registry_enabled_by_default")
async def test_the_controls_of_a_terminal_follow_the_mode_that_is_saved(
    hass, schedule_setup, entity_registry
):
    await schedule_setup.create()
    unavailable = STATE_UNAVAILABLE
    assert states(hass, entity_registry, WORKER, CONTROLS) == {
        "allowed_time_mode_control": "manual",
        **{f"manual_{day}_control": STATE_ON if day in DAYS[:5] else STATE_OFF for day in DAYS},
        "manual_windows_control": "08:00-12:00, 13:00-17:30",
        **{f"calendar_{day}_control": unavailable for day in DAYS},
    }
    assert states(hass, entity_registry, KIOSK, CONTROLS) == {
        "allowed_time_mode_control": "calendar",
        **{f"manual_{day}_control": unavailable for day in DAYS},
        "manual_windows_control": unavailable,
        "calendar_monday_control": "00:00-06:00, 08:00-24:00",
        "calendar_tuesday_control": "",
        "calendar_wednesday_control": "09:00-10:00",
        "calendar_thursday_control": "",
        "calendar_friday_control": "",
        "calendar_saturday_control": "",
        "calendar_sunday_control": "00:00-24:00",
    }
    assert states(hass, entity_registry, GUEST, CONTROLS) == {
        "allowed_time_mode_control": "any",
        **dict.fromkeys(CONTROLS[1:], unavailable),
    }
    assert set(states(hass, entity_registry, BLIND, CONTROLS).values()) == {unavailable}


@pytest.mark.usefixtures("entity_registry_enabled_by_default")
async def test_a_mode_that_the_page_does_not_have_is_unknown_and_nothing_can_be_changed(
    hass, schedule_setup, entity_registry
):
    _, coordinator = await schedule_setup.create()
    schedule_setup.world.schedules[GUEST] = UNKNOWN_MODE
    await schedule_setup.publish(coordinator)
    assert states(hass, entity_registry, GUEST, READINGS) == dict.fromkeys(READINGS, STATE_UNKNOWN)
    assert set(states(hass, entity_registry, GUEST, CONTROLS).values()) == {STATE_UNAVAILABLE}


@pytest.mark.usefixtures("entity_registry_enabled_by_default")
async def test_the_controls_follow_a_change_of_mode_and_an_unreadable_table(
    hass, schedule_setup, entity_registry
):
    _, coordinator = await schedule_setup.create()
    world = schedule_setup.world
    world.schedules[WORKER] = None
    await schedule_setup.publish(coordinator)
    assert state_of(hass, entity_registry, WORKER, "allowed_time_mode_control").state == "any"
    assert (
        state_of(hass, entity_registry, WORKER, "manual_monday_control").state == STATE_UNAVAILABLE
    )
    world.schedules[WORKER] = EVERYTHING
    await schedule_setup.publish(coordinator)
    assert state_of(hass, entity_registry, WORKER, "calendar_monday_control").state == "00:00-24:00"
    assert (
        state_of(hass, entity_registry, WORKER, "manual_windows_control").state == STATE_UNAVAILABLE
    )
    world.readable = False
    await schedule_setup.publish(coordinator)
    assert set(states(hass, entity_registry, WORKER, CONTROLS).values()) == {STATE_UNAVAILABLE}


# Controls: what they send


@pytest.mark.usefixtures("entity_registry_enabled_by_default")
@pytest.mark.parametrize(
    ("mac", "key", "service", "data", "field", "value"),
    [
        (
            WORKER,
            "allowed_time_mode_control",
            "select_option",
            {"option": "any"},
            "allowed_time_mode",
            "any",
        ),
        (
            GUEST,
            "allowed_time_mode_control",
            "select_option",
            {"option": "manual"},
            "allowed_time_mode",
            "manual",
        ),
        (
            GUEST,
            "allowed_time_mode_control",
            "select_option",
            {"option": "calendar"},
            "allowed_time_mode",
            "calendar",
        ),
        (
            KIOSK,
            "allowed_time_mode_control",
            "select_option",
            {"option": "any"},
            "allowed_time_mode",
            "any",
        ),
        (WORKER, "manual_wednesday_control", "turn_off", {}, "manual_wednesday", False),
        (WORKER, "manual_saturday_control", "turn_on", {}, "manual_saturday", True),
        (
            WORKER,
            "manual_windows_control",
            "set_value",
            {"value": "07:00-09:00,18:00-22:00"},
            "manual_windows",
            "07:00-09:00,18:00-22:00",
        ),
        (
            KIOSK,
            "calendar_thursday_control",
            "set_value",
            {"value": "10:00-11:00"},
            "calendar_thursday",
            "10:00-11:00",
        ),
        (KIOSK, "calendar_monday_control", "set_value", {"value": ""}, "calendar_monday", ""),
    ],
)
async def test_a_control_sends_one_field_once_and_the_state_follows_the_refresh(
    hass, schedule_setup, entity_registry, mac, key, service, data, field, value
):
    await schedule_setup.create()
    api = schedule_setup.api
    refreshes = api.async_get_snapshot.await_count
    await call(hass, entity_registry, mac, key, service, **data)
    await hass.async_block_till_done()
    api.async_set_terminal_field.assert_awaited_once_with(mac, TerminalField(field), value)
    assert only_calls(api) == {"async_set_terminal_field": 1}
    assert api.async_get_snapshot.await_count == refreshes + 1
    assert isinstance(api.async_set_terminal_field.await_args.args[1], TerminalField)


@pytest.mark.usefixtures("entity_registry_enabled_by_default")
@pytest.mark.parametrize(("mac", "field"), [(WORKER, "manual_windows"), (KIOSK, "calendar_monday")])
async def test_the_largest_manual_or_calendar_text_can_be_edited_through_its_entity(
    hass, schedule_setup, entity_registry, mac, field
):
    await schedule_setup.create()
    text = ", ".join(f"{hour:02d}:00-{hour + 1:02d}:00" for hour in range(0, 24, 2))
    before = state_of(hass, entity_registry, mac, f"{field}_control")
    assert len(text) <= before.attributes["max"] <= 255
    assert before.attributes["min"] == 0
    await call(hass, entity_registry, mac, f"{field}_control", "set_value", value=text)
    await hass.async_block_till_done()
    schedule_setup.api.async_set_terminal_field.assert_awaited_once_with(
        mac, TerminalField(field), text
    )
    assert state_of(hass, entity_registry, mac, field).state == text
    assert state_of(hass, entity_registry, mac, f"{field}_control").state == text


@pytest.mark.usefixtures("entity_registry_enabled_by_default")
async def test_editing_one_field_keeps_every_other_weekday_window_and_hour(
    hass, schedule_setup, entity_registry
):
    _, coordinator = await schedule_setup.create()
    world, api = schedule_setup.world, schedule_setup.api
    world.schedules[WORKER] = TerminalSchedule(
        "manual", weekdays=DAYS[:5], windows=("08:00-12:00", "13:00-17:00")
    )
    await schedule_setup.publish(coordinator)

    async def act(key, service, **data):
        await call(hass, entity_registry, WORKER, key, service, **data)
        await hass.async_block_till_done()

    def manual():
        return states(hass, entity_registry, WORKER, ("manual_weekdays", "manual_windows"))

    def calendar():
        return {
            day: state_of(hass, entity_registry, WORKER, f"calendar_{day}").state for day in DAYS
        }

    await act("manual_wednesday_control", "turn_off")
    await act("manual_sunday_control", "turn_on")
    assert manual() == {
        "manual_weekdays": "monday, tuesday, thursday, friday, sunday",
        "manual_windows": "08:00-12:00, 13:00-17:00",
    }
    await act("manual_windows_control", "set_value", value="09:00-10:00")
    assert manual() == {
        "manual_weekdays": "monday, tuesday, thursday, friday, sunday",
        "manual_windows": "09:00-10:00",
    }
    await act("allowed_time_mode_control", "select_option", option="calendar")
    assert state_of(hass, entity_registry, WORKER, "allowed_time_mode").state == "calendar"
    chosen = ("monday", "tuesday", "thursday", "friday", "sunday")
    assert calendar() == {day: "09:00-10:00" if day in chosen else "none" for day in DAYS}
    await act("calendar_tuesday_control", "set_value", value="00:00-01:00, 23:00-24:00")
    assert calendar()["tuesday"] == "00:00-01:00, 23:00-24:00"
    assert calendar()["monday"] == "09:00-10:00"
    writes = api.async_set_terminal_field.await_count
    with pytest.raises(ServiceValidationError):
        await act("allowed_time_mode_control", "select_option", option="manual")
    assert api.async_set_terminal_field.await_count == writes
    await act("calendar_tuesday_control", "set_value", value="09:00-10:00")
    await act("allowed_time_mode_control", "select_option", option="manual")
    assert manual() == {
        "manual_weekdays": "monday, tuesday, thursday, friday, sunday",
        "manual_windows": "09:00-10:00",
    }
    await act("allowed_time_mode_control", "select_option", option="any")
    assert state_of(hass, entity_registry, WORKER, "allowed_time_mode").state == "any"
    assert state_of(hass, entity_registry, WORKER, "manual_weekdays").state == STATE_UNKNOWN
    assert only_calls(api) == {"async_set_terminal_field": api.async_set_terminal_field.await_count}


@pytest.mark.usefixtures("entity_registry_enabled_by_default")
@pytest.mark.parametrize(
    ("mac", "key", "service", "data", "reason"),
    [
        (
            WORKER,
            "manual_windows_control",
            "set_value",
            {"value": "25:00-26:00"},
            "between 00:00 and 23:59",
        ),
        (
            WORKER,
            "manual_windows_control",
            "set_value",
            {"value": "08:00-12:00, 11:00-13:00"},
            "overlap",
        ),
        (
            WORKER,
            "manual_windows_control",
            "set_value",
            {"value": "13:00-12:00"},
            "end is not after",
        ),
        (WORKER, "manual_windows_control", "set_value", {"value": ""}, "At least one time window"),
        (
            WORKER,
            "manual_windows_control",
            "set_value",
            {"value": ", ".join(f"{h:02d}:00-{h:02d}:30" for h in range(13))},
            "At most 12",
        ),
        (
            WORKER,
            "allowed_time_mode_control",
            "select_option",
            {"option": "calendar"},
            "whole hour",
        ),
        (KIOSK, "calendar_monday_control", "set_value", {"value": "08:30-09:00"}, "whole hour"),
        (KIOSK, "calendar_monday_control", "set_value", {"value": "noon"}, "time range"),
        (KIOSK, "allowed_time_mode_control", "select_option", {"option": "manual"}, "same hours"),
    ],
)
async def test_a_value_that_the_page_would_refuse_is_refused_before_any_request(
    hass, schedule_setup, entity_registry, mac, key, service, data, reason
):
    await schedule_setup.create()
    with pytest.raises(ServiceValidationError) as raised:
        await call(hass, entity_registry, mac, key, service, **data)
    assert raised.value.translation_key == "invalid_value"
    assert reason in raised.value.translation_placeholders["reason"]
    assert only_calls(schedule_setup.api) == {}


@pytest.mark.usefixtures("entity_registry_enabled_by_default")
async def test_the_last_weekday_and_the_last_allowed_hour_cannot_be_removed(
    hass, schedule_setup, entity_registry
):
    _, coordinator = await schedule_setup.create()
    world = schedule_setup.world
    world.schedules[WORKER] = TerminalSchedule(
        "manual", weekdays=("friday",), windows=("08:00-09:00",)
    )
    world.schedules[KIOSK] = TerminalSchedule("calendar", calendar=((),) * 6 + (("12:00-13:00",),))
    await schedule_setup.publish(coordinator)
    with pytest.raises(ServiceValidationError) as raised:
        await call(hass, entity_registry, WORKER, "manual_friday_control", "turn_off")
    assert "At least one weekday" in raised.value.translation_placeholders["reason"]
    with pytest.raises(ServiceValidationError) as raised:
        await call(hass, entity_registry, KIOSK, "calendar_sunday_control", "set_value", value="")
    assert "At least one hour" in raised.value.translation_placeholders["reason"]
    assert only_calls(schedule_setup.api) == {}


@pytest.mark.usefixtures("entity_registry_enabled_by_default")
@pytest.mark.parametrize(
    "failure",
    [
        Ac100ConnectionError("x"),
        Ac100TerminalError("x"),
        Ac100TerminalPartialError("x", "link"),
    ],
    ids=lambda failure: type(failure).__name__,
)
async def test_a_failed_write_is_reported_once_and_never_sent_again(
    hass, schedule_setup, entity_registry, failure
):
    await schedule_setup.create()
    api = schedule_setup.api
    api.async_set_terminal_field.side_effect = failure
    with pytest.raises(HomeAssistantError):
        await call(hass, entity_registry, WORKER, "manual_saturday_control", "turn_on")
    assert api.async_set_terminal_field.await_count == 1
    await hass.async_block_till_done()
    assert api.async_set_terminal_field.await_count == 1


# Static strings


def test_every_schedule_entity_has_english_strings_and_an_icon():
    strings = json.loads((COMPONENT / "strings.json").read_text())
    assert json.loads((COMPONENT / "translations" / "en.json").read_text()) == strings
    icons = json.loads((COMPONENT / "icons.json").read_text())
    used = {
        ("sensor", "terminal_schedule_mode"),
        ("sensor", "terminal_schedule_manual_weekdays"),
        ("sensor", "terminal_schedule_manual_windows"),
        ("sensor", "terminal_schedule_calendar_day"),
        ("select", "terminal_schedule_mode"),
        ("switch", "terminal_schedule_manual_day"),
        ("text", "terminal_schedule_manual_windows"),
        ("text", "terminal_schedule_calendar_day"),
    }
    for platform, key in used:
        translation = strings["entity"][platform][key]
        assert translation["name"], (platform, key)
        assert icons["entity"][platform][key]["default"].startswith("mdi:"), (platform, key)
        if key.endswith("_day"):
            assert "{day}" in translation["name"], (platform, key)
        if key == "terminal_schedule_mode":
            assert set(translation["state"]) == {"any", "manual", "calendar"}
