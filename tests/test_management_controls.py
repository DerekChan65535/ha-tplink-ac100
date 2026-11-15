"""Opt-in controls of controller-wide settings, the controller name, saving and AP radios."""

import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import create_autospec, patch

import pytest
from homeassistant.const import (
    STATE_OFF,
    STATE_ON,
    STATE_UNAVAILABLE,
    EntityCategory,
    Platform,
)
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import entity_platform
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant.common import MockConfigEntry

from custom_components.tplink_ac100.api import (
    Ac100Api,
    Ac100ProtocolError,
    Ac100SaveTimeoutError,
    Ac100SessionExpiredError,
)
from custom_components.tplink_ac100.const import CONF_ENABLE_CONTROL, DOMAIN
from custom_components.tplink_ac100.coordinator import CONFIGURATION_CONTEXT
from custom_components.tplink_ac100.management import select_options, writable_fields
from custom_components.tplink_ac100.models import AccessPoint, ApRadio, Controller, Snapshot
from custom_components.tplink_ac100.monitoring import (
    CONFIGURATION_FIELDS,
    RADIO_SOURCE,
    ConfigurationState,
    MonitoringState,
    RadioSettings,
)

pytestmark = pytest.mark.usefixtures("enable_custom_integrations")

MAC = "02:00:00:00:00:10"
FIELDS = {item.key: item for item in CONFIGURATION_FIELDS}
SWITCHES = [item.key for item in writable_fields("switch")]
NUMBERS = [item.key for item in writable_fields("integer")]
SELECTS = [item.key for item in writable_fields("choice")]
ALL_PLATFORMS = [
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.NUMBER,
    Platform.SELECT,
    Platform.SENSOR,
    Platform.SWITCH,
    Platform.TEXT,
]
START = {
    "band_navigation_enabled": True,
    "band_navigation_5g_threshold": 20,
    "band_navigation_difference_threshold": 4,
    "band_navigation_max_failures": 10,
    "load_balance_enabled": False,
    "load_balance_threshold": 20,
    "load_balance_difference_threshold": 4,
    "load_balance_max_failures": 3,
    "load_balance_rssi_threshold": -75,
    "broadcast_storm_control_enabled": False,
    "broadcast_storm_rate": 1024,
    "igmp_snooping_enabled": True,
    "igmp_unknown_multicast": "drop",
    "igmp_2g_multicast_rate": 1,
    "igmp_5g_multicast_rate": 6,
    "device_alias": "TL-AC100 5A1",
}
RADIOS = {("0", "1"): True, ("0", "2"): False, ("1", "1"): True, ("1", "2"): True, ("2", "1"): True}


def make_ap(entry_id, number, status="1", radios=None):
    return AccessPoint(
        str(entry_id),
        f"AP {number}",
        "TL-XAP1502GI-PoE",
        f"02:00:00:00:00:{number:02x}",
        f"192.0.2.{number}",
        status,
        True,
        3600,
        radios or (ApRadio("1", "2.4GHz", 0, 6, 10.0), ApRadio("2", "5GHz", 0, 36, 10.0)),
    )


@pytest.fixture
def snapshot():
    """An online AP, an offline AP and an online single-radio AP without a band name."""
    controller = Controller(MAC, "TL-AC100", "5.0", "1.0", {"core1": 1.0}, 40.0, 7200)
    aps = (
        make_ap(0, 0x41),
        make_ap(1, 0x42, status="2"),
        make_ap(2, 0x43, radios=(ApRadio("1", "---", 0, 1, 5.0),)),
    )
    return Snapshot(controller, aps, ())


def configuration(settings, radios=RADIOS):
    return ConfigurationState(
        values=dict(settings),
        available_sections=frozenset(
            {item.source for item in CONFIGURATION_FIELDS} | {RADIO_SOURCE}
        ),
        radios=tuple(RadioSettings(a, u, {"enabled": on}) for (a, u), on in radios.items()),
    )


def pinned(state, snapshot):
    """Pin each radio row to its AP's MAC, as the coordinator does after a settings read."""
    macs = {ap.entry_id: ap.mac for ap in snapshot.aps}
    radios = tuple(replace(row, ap_mac=macs.get(row.ap_entry_id)) for row in state.radios)
    return replace(state, radios=radios)


@pytest.fixture
def controller_setup(hass, snapshot):
    """Load the real coordinator and every platform around a fake API that keeps the settings."""
    clock = [100.0]
    settings, radios = dict(START), dict(RADIOS)
    api = create_autospec(Ac100Api, instance=True)  # checks every call against the real signature
    api.async_get_snapshot.return_value = snapshot
    api.async_get_monitoring.return_value = MonitoringState()
    api.async_get_configuration.side_effect = lambda: configuration(settings, radios)

    async def set_setting(field, value):
        settings[field.key] = value

    async def set_alias(name):
        settings["device_alias"] = name

    async def set_radio(ap_entry_id, unit, enabled):
        radios[ap_entry_id, unit] = enabled

    api.async_set_setting.side_effect = set_setting
    api.async_set_controller_alias.side_effect = set_alias
    api.async_set_radio_enabled.side_effect = set_radio

    async def create(*, management=True):
        entry = MockConfigEntry(
            domain=DOMAIN,
            unique_id=MAC,
            data={"host": "http://192.0.2.1", "username": "example", "password": "example-only"},
            options={CONF_ENABLE_CONTROL: management},
        )
        entry.add_to_hass(hass)
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        return entry, entry.runtime_data

    async def read_settings(coordinator):
        """The first settings read is due 60 seconds after setup."""
        clock[0] += 60
        await coordinator.async_refresh()
        await hass.async_block_till_done()

    with (
        patch("custom_components.tplink_ac100.Ac100Api", return_value=api),
        patch("custom_components.tplink_ac100.PLATFORMS", ALL_PLATFORMS),
        patch("custom_components.tplink_ac100.coordinator.monotonic", side_effect=lambda: clock[0]),
    ):
        yield SimpleNamespace(
            create=create,
            read_settings=read_settings,
            api=api,
            settings=settings,
            radios=radios,
            snapshot=snapshot,
        )


def controller_uid(key):
    return f"{MAC}_controller_{key}_control"


def radio_uid(ap, unit):
    return f"{MAC}_{ap.mac}_radio_{unit}_enabled_control"


def control_rows(registry, entry):
    return [
        row
        for row in er.async_entries_for_config_entry(registry, entry.entry_id)
        if row.unique_id.endswith("_control")
    ]


def entity_id(registry, domain, unique_id):
    result = registry.async_get_entity_id(domain, DOMAIN, unique_id)
    assert result is not None, (domain, unique_id)
    return result


def state_of(hass, registry, domain, unique_id):
    state = hass.states.get(entity_id(registry, domain, unique_id))
    assert state is not None, (domain, unique_id)
    return state


async def call(hass, registry, domain, service, unique_id, **data):
    await hass.services.async_call(
        domain,
        service,
        {"entity_id": entity_id(registry, domain, unique_id), **data},
        blocking=True,
    )


def refreshed(ctx):
    """How often the API was asked for each of the three reads."""
    api = ctx.api
    return (
        api.async_get_snapshot.await_count,
        api.async_get_monitoring.await_count,
        api.async_get_configuration.await_count,
    )


def assert_refreshed_once(ctx, before):
    """An action is followed by one refresh of the status, monitoring and settings reads."""
    assert tuple(now - then for now, then in zip(refreshed(ctx), before, strict=True)) == (1, 1, 1)


WRITE_METHODS = (
    "async_set_setting",
    "async_set_controller_alias",
    "async_set_radio_enabled",
    "async_set_ap_led",
    "async_save_configuration",
    "async_reboot_ap",
    "async_reboot_controller",
)


def writes(ctx):
    """Each write method of the API that was awaited, with how often."""
    counts = {name: getattr(ctx.api, name).await_count for name in WRITE_METHODS}
    return {name: count for name, count in counts.items() if count}


async def test_controls_are_absent_without_management(hass, controller_setup, entity_registry):
    entry, coordinator = await controller_setup.create(management=False)
    rows = er.async_entries_for_config_entry(entity_registry, entry.entry_id)
    assert not control_rows(entity_registry, entry)
    assert {row.domain for row in rows} == {"binary_sensor", "sensor", "switch"}
    assert [row.unique_id for row in rows if row.domain == "switch"] == [
        f"{MAC}_controller_debug_logging"
    ]
    assert {entity.domain for entity in hass.states.async_all()} <= {
        "binary_sensor",
        "sensor",
        "switch",
    }
    assert controller_setup.api.async_set_setting.await_count == 0


async def test_controls_exist_disabled_by_default_on_the_controller_and_ap_devices(
    hass, controller_setup, snapshot, entity_registry, device_registry
):
    entry, coordinator = await controller_setup.create()
    controller_device = device_registry.async_get_device_by_identifier(
        (DOMAIN, MAC), entry.entry_id
    )
    expected = {
        **{("switch", controller_uid(key)): controller_device for key in SWITCHES},
        **{("number", controller_uid(key)): controller_device for key in NUMBERS},
        **{("select", controller_uid(key)): controller_device for key in SELECTS},
        ("text", controller_uid("device_alias")): controller_device,
        ("button", f"{MAC}_controller_save_configuration"): controller_device,
    }
    for ap in snapshot.aps:
        device = device_registry.async_get_device_by_identifier((DOMAIN, ap.mac), entry.entry_id)
        for radio in ap.radios:
            expected["switch", radio_uid(ap, radio.unit)] = device
    assert (len(SWITCHES), len(NUMBERS), len(SELECTS)) == (4, 8, 3)
    translation_keys = {
        **{("switch", controller_uid(key)): key for key in SWITCHES},
        **{("number", controller_uid(key)): key for key in NUMBERS},
        **{("select", controller_uid(key)): key for key in SELECTS},
        ("text", controller_uid("device_alias")): "device_alias",
        ("button", f"{MAC}_controller_save_configuration"): "save_configuration",
    }
    translation_keys |= {key: "radio_enabled" for key in expected if key not in translation_keys}
    for (domain, unique_id), device in expected.items():
        row = entity_registry.async_get(entity_id(entity_registry, domain, unique_id))
        assert row.disabled_by is er.RegistryEntryDisabler.INTEGRATION, unique_id
        assert row.entity_category is EntityCategory.CONFIG, unique_id
        assert row.device_id == device.id, unique_id
        assert row.translation_key == translation_keys[domain, unique_id], unique_id
        assert hass.states.get(row.entity_id) is None
    assert CONFIGURATION_CONTEXT not in set(coordinator.async_contexts())
    controller_api = controller_setup.api
    controller_api.async_get_configuration.assert_not_awaited()
    assert controller_api.async_set_setting.await_count == 0
    assert controller_api.async_save_configuration.await_count == 0


async def test_read_only_diagnostics_and_the_reboot_buttons_are_unchanged(
    hass, controller_setup, entity_registry
):
    entry, _ = await controller_setup.create()
    for item in CONFIGURATION_FIELDS:
        domain = "binary_sensor" if item.kind == "bool" else "sensor"
        row = entity_registry.async_get(
            entity_id(entity_registry, domain, f"{MAC}_controller_{item.key}")
        )
        assert row.entity_category is EntityCategory.DIAGNOSTIC
        assert row.disabled_by is er.RegistryEntryDisabler.INTEGRATION
        assert row.translation_key == item.key
    assert not [
        row
        for row in er.async_entries_for_config_entry(entity_registry, entry.entry_id)
        if row.domain in {"binary_sensor", "sensor"} and row.unique_id.endswith("_control")
    ]
    reboots = [
        row.unique_id
        for row in er.async_entries_for_config_entry(entity_registry, entry.entry_id)
        if row.domain == "button" and row.unique_id.endswith("_reboot")
    ]
    assert len(reboots) == 4  # the controller and the three APs


@pytest.mark.usefixtures("entity_registry_enabled_by_default")
async def test_states_follow_the_settings_read_and_offer_only_the_pages_choices(
    hass, controller_setup, snapshot, entity_registry
):
    _, coordinator = await controller_setup.create()
    assert CONFIGURATION_CONTEXT in set(coordinator.async_contexts())
    assert (
        state_of(hass, entity_registry, "switch", controller_uid("igmp_snooping_enabled")).state
        == STATE_UNAVAILABLE
    )
    await controller_setup.read_settings(coordinator)
    for key in SWITCHES:
        state = state_of(hass, entity_registry, "switch", controller_uid(key))
        assert state.state == (STATE_ON if START[key] else STATE_OFF), key
    ranges = {
        "band_navigation_5g_threshold": (2, 40, None),
        "band_navigation_difference_threshold": (1, 8, None),
        "band_navigation_max_failures": (0, 100, None),
        "load_balance_threshold": (2, 40, None),
        "load_balance_difference_threshold": (1, 8, None),
        "load_balance_max_failures": (1, 100, None),
        "load_balance_rssi_threshold": (-95, 0, "dBm"),
        "broadcast_storm_rate": (1, 1024, "kB/s"),
    }
    assert set(ranges) == set(NUMBERS)
    for key, (low, high, unit) in ranges.items():
        state = state_of(hass, entity_registry, "number", controller_uid(key))
        assert state.state == str(START[key]), key
        assert (state.attributes["min"], state.attributes["max"]) == (low, high)
        assert state.attributes["step"] == 1
        assert state.attributes["mode"] == "box"
        assert state.attributes.get("unit_of_measurement") == unit
    options = {
        "igmp_unknown_multicast": ["forward", "drop"],
        "igmp_2g_multicast_rate": ["1", "11"],
        "igmp_5g_multicast_rate": ["6", "24"],
    }
    assert set(options) == set(SELECTS)
    for key, expected in options.items():
        state = state_of(hass, entity_registry, "select", controller_uid(key))
        assert state.attributes["options"] == expected == list(select_options(FIELDS[key]))
        assert state.state == str(START[key]), key
    name = state_of(hass, entity_registry, "text", controller_uid("device_alias"))
    assert name.state == "TL-AC100 5A1"
    assert (name.attributes["min"], name.attributes["max"], name.attributes["mode"]) == (
        1,
        20,
        "text",
    )
    for key in (*SWITCHES, *NUMBERS, *SELECTS, "device_alias"):
        control = {
            "switch": SWITCHES,
            "number": NUMBERS,
            "select": SELECTS,
            "text": ["device_alias"],
        }
        domain = next(domain for domain, keys in control.items() if key in keys)
        view = "binary_sensor" if domain == "switch" else "sensor"
        assert (
            state_of(hass, entity_registry, view, f"{MAC}_controller_{key}").state
            == state_of(hass, entity_registry, domain, controller_uid(key)).state
        ), key


@pytest.mark.usefixtures("entity_registry_enabled_by_default")
async def test_a_value_that_is_unread_unknown_or_not_offered_makes_its_control_unavailable(
    hass, controller_setup, snapshot, entity_registry
):
    _, coordinator = await controller_setup.create()

    async def publish(values, sections=None):
        state = configuration(values)
        if sections is not None:
            state = replace(state, available_sections=frozenset(sections))
        coordinator.async_set_updated_data(replace(snapshot, configuration=state))
        await hass.async_block_till_done()

    def states():
        return {
            key: state_of(hass, entity_registry, domain, controller_uid(key)).state
            for domain, keys in (
                ("switch", SWITCHES),
                ("number", NUMBERS),
                ("select", SELECTS),
                ("text", ["device_alias"]),
            )
            for key in keys
        }

    coordinator.async_set_updated_data(replace(snapshot, configuration=None))
    await hass.async_block_till_done()
    assert set(states().values()) == {STATE_UNAVAILABLE}
    await publish(START, sections=())
    assert set(states().values()) == {STATE_UNAVAILABLE}
    await publish({})
    assert set(states().values()) == {STATE_UNAVAILABLE}
    await publish(START)
    assert STATE_UNAVAILABLE not in states().values()
    await publish(START | {"band_navigation_max_failures": 0, "load_balance_rssi_threshold": 0})
    seen = states()
    assert (seen["band_navigation_max_failures"], seen["load_balance_rssi_threshold"]) == ("0", "0")
    unusable = {
        "band_navigation_enabled": "on",
        "band_navigation_5g_threshold": True,
        "load_balance_rssi_threshold": None,
        "igmp_unknown_multicast": "maybe",
        "igmp_2g_multicast_rate": 5,
        "igmp_5g_multicast_rate": 6.5,
        "device_alias": "x" * 21,
    }
    await publish(START | unusable)
    seen = states()
    assert {key for key, state in seen.items() if state == STATE_UNAVAILABLE} == set(unusable)
    await publish(START | {"device_alias": "name\nwith newline"})
    assert states()["device_alias"] == STATE_UNAVAILABLE


@pytest.mark.usefixtures("entity_registry_enabled_by_default")
@pytest.mark.parametrize("key", SWITCHES)
@pytest.mark.parametrize("service", ["turn_on", "turn_off"])
async def test_a_switch_sends_exactly_one_call_then_refreshes_and_shows_the_new_state(
    hass, controller_setup, entity_registry, key, service
):
    ctx = controller_setup
    _, coordinator = await ctx.create()
    await ctx.read_settings(coordinator)
    reads = refreshed(ctx)
    wanted = service == "turn_on"
    await call(hass, entity_registry, "switch", service, controller_uid(key))
    await hass.async_block_till_done()
    ctx.api.async_set_setting.assert_awaited_once_with(FIELDS[key], wanted)
    assert writes(ctx) == {"async_set_setting": 1}
    assert_refreshed_once(ctx, reads)
    assert ctx.settings[key] is wanted
    assert state_of(hass, entity_registry, "switch", controller_uid(key)).state == (
        STATE_ON if wanted else STATE_OFF
    )
    assert state_of(hass, entity_registry, "binary_sensor", f"{MAC}_controller_{key}").state == (
        STATE_ON if wanted else STATE_OFF
    )


@pytest.mark.usefixtures("entity_registry_enabled_by_default")
@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("band_navigation_5g_threshold", 2),
        ("band_navigation_difference_threshold", 8),
        ("band_navigation_max_failures", 0),
        ("load_balance_threshold", 40),
        ("load_balance_difference_threshold", 1),
        ("load_balance_max_failures", 100),
        ("load_balance_rssi_threshold", -95),
        ("broadcast_storm_rate", 300),
    ],
)
async def test_a_number_sends_one_whole_number_then_refreshes(
    hass, controller_setup, entity_registry, key, value
):
    ctx = controller_setup
    _, coordinator = await ctx.create()
    await ctx.read_settings(coordinator)
    reads = refreshed(ctx)
    await call(
        hass, entity_registry, "number", "set_value", controller_uid(key), value=float(value)
    )
    await hass.async_block_till_done()
    ctx.api.async_set_setting.assert_awaited_once_with(FIELDS[key], value)
    assert writes(ctx) == {"async_set_setting": 1}
    assert type(ctx.api.async_set_setting.await_args.args[1]) is int
    assert_refreshed_once(ctx, reads)
    assert state_of(hass, entity_registry, "number", controller_uid(key)).state == str(value)


@pytest.mark.usefixtures("entity_registry_enabled_by_default")
@pytest.mark.parametrize(
    ("key", "value", "error"),
    [
        ("band_navigation_5g_threshold", 20.5, "invalid_value"),
        ("broadcast_storm_rate", 100.25, "invalid_value"),
        ("band_navigation_5g_threshold", 41, None),
        ("band_navigation_5g_threshold", 1, None),
        ("load_balance_max_failures", 0, None),
        ("load_balance_rssi_threshold", 1, None),
        ("broadcast_storm_rate", 1025, None),
    ],
)
async def test_a_number_the_page_would_refuse_sends_nothing(
    hass, controller_setup, entity_registry, key, value, error
):
    ctx = controller_setup
    _, coordinator = await ctx.create()
    await ctx.read_settings(coordinator)
    reads = refreshed(ctx)
    with pytest.raises(ServiceValidationError) as raised:
        await call(hass, entity_registry, "number", "set_value", controller_uid(key), value=value)
    if error:
        assert raised.value.translation_key == error
        assert raised.value.translation_domain == DOMAIN
        assert raised.value.translation_placeholders == {"reason": "Enter a whole number"}
    ctx.api.async_set_setting.assert_not_awaited()
    assert refreshed(ctx) == reads


@pytest.mark.usefixtures("entity_registry_enabled_by_default")
@pytest.mark.parametrize(
    ("key", "option", "expected"),
    [
        ("igmp_unknown_multicast", "forward", "forward"),
        ("igmp_unknown_multicast", "drop", "drop"),
        ("igmp_2g_multicast_rate", "11", 11),
        ("igmp_5g_multicast_rate", "24", 24),
        ("igmp_5g_multicast_rate", "6", 6),
    ],
)
async def test_a_select_sends_one_of_its_pages_choices_then_refreshes(
    hass, controller_setup, entity_registry, key, option, expected
):
    ctx = controller_setup
    _, coordinator = await ctx.create()
    await ctx.read_settings(coordinator)
    reads = refreshed(ctx)
    await call(hass, entity_registry, "select", "select_option", controller_uid(key), option=option)
    await hass.async_block_till_done()
    ctx.api.async_set_setting.assert_awaited_once_with(FIELDS[key], expected)
    assert writes(ctx) == {"async_set_setting": 1}
    assert_refreshed_once(ctx, reads)
    assert state_of(hass, entity_registry, "select", controller_uid(key)).state == option


@pytest.mark.usefixtures("entity_registry_enabled_by_default")
@pytest.mark.parametrize(
    ("key", "option"),
    [
        ("igmp_unknown_multicast", "1"),
        ("igmp_2g_multicast_rate", "6"),
        ("igmp_5g_multicast_rate", "11"),
    ],
)
async def test_a_select_refuses_what_its_page_does_not_offer(
    hass, controller_setup, entity_registry, key, option
):
    ctx = controller_setup
    _, coordinator = await ctx.create()
    await ctx.read_settings(coordinator)
    with pytest.raises(ServiceValidationError):
        await call(
            hass, entity_registry, "select", "select_option", controller_uid(key), option=option
        )
    ctx.api.async_set_setting.assert_not_awaited()


@pytest.mark.usefixtures("entity_registry_enabled_by_default")
async def test_the_controller_name_is_set_once_then_the_reads_are_refreshed(
    hass, controller_setup, entity_registry
):
    ctx = controller_setup
    _, coordinator = await ctx.create()
    await ctx.read_settings(coordinator)
    reads = refreshed(ctx)
    await call(
        hass,
        entity_registry,
        "text",
        "set_value",
        controller_uid("device_alias"),
        value="Office AC",
    )
    await hass.async_block_till_done()
    ctx.api.async_set_controller_alias.assert_awaited_once_with("Office AC")
    assert writes(ctx) == {"async_set_controller_alias": 1}
    assert_refreshed_once(ctx, reads)
    assert (
        state_of(hass, entity_registry, "text", controller_uid("device_alias")).state == "Office AC"
    )
    assert (
        state_of(hass, entity_registry, "sensor", f"{MAC}_controller_device_alias").state
        == "Office AC"
    )


@pytest.mark.usefixtures("entity_registry_enabled_by_default")
@pytest.mark.parametrize(
    ("name", "error"),
    [
        ("x" * 21, None),
        ("", None),
        (" ", "invalid_value"),
        ("two\nlines", "invalid_value"),
        ("tab\there", "invalid_value"),
        ("\U0001f600" * 11, "invalid_value"),
    ],
)
async def test_a_name_the_page_would_refuse_sends_nothing(
    hass, controller_setup, entity_registry, name, error
):
    ctx = controller_setup
    _, coordinator = await ctx.create()
    await ctx.read_settings(coordinator)
    reads = refreshed(ctx)
    with pytest.raises(ServiceValidationError) as raised:
        await call(
            hass, entity_registry, "text", "set_value", controller_uid("device_alias"), value=name
        )
    if error:
        assert raised.value.translation_key == error
        assert "1 to 20 visible characters" in raised.value.translation_placeholders["reason"]
    ctx.api.async_set_controller_alias.assert_not_awaited()
    assert refreshed(ctx) == reads


@pytest.mark.usefixtures("entity_registry_enabled_by_default")
@pytest.mark.parametrize("state", ["Saved", "Not Saved", None])
async def test_the_save_button_is_available_whether_or_not_changes_are_unsaved(
    hass, controller_setup, entity_registry, state
):
    ctx = controller_setup
    values = {} if state is None else {"configuration_not_saved": state == "Not Saved"}
    sections = frozenset() if state is None else frozenset({"system.cfg_status"})
    ctx.api.async_get_monitoring.return_value = MonitoringState(values, sections)
    await ctx.create()
    button = state_of(hass, entity_registry, "button", f"{MAC}_controller_save_configuration")
    assert button.state != STATE_UNAVAILABLE
    reads = refreshed(ctx)
    await call(hass, entity_registry, "button", "press", f"{MAC}_controller_save_configuration")
    await hass.async_block_till_done()
    ctx.api.async_save_configuration.assert_awaited_once_with()
    assert writes(ctx) == {"async_save_configuration": 1}
    assert_refreshed_once(ctx, reads)


@pytest.mark.usefixtures("entity_registry_enabled_by_default")
async def test_an_unconfirmed_save_is_reported_after_the_reads_were_refreshed(
    hass, controller_setup, entity_registry
):
    ctx = controller_setup
    ctx.api.async_save_configuration.side_effect = Ac100SaveTimeoutError("not confirmed")
    _, coordinator = await ctx.create()
    reads = refreshed(ctx)
    with pytest.raises(HomeAssistantError) as raised:
        await call(hass, entity_registry, "button", "press", f"{MAC}_controller_save_configuration")
    assert raised.value.translation_key == "save_outcome_unknown"
    assert raised.value.translation_domain == DOMAIN
    ctx.api.async_save_configuration.assert_awaited_once_with()
    assert refreshed(ctx)[:2] == (reads[0] + 1, reads[1] + 1)


@pytest.mark.usefixtures("entity_registry_enabled_by_default")
async def test_a_second_save_press_while_saving_is_refused(hass, controller_setup, entity_registry):
    import asyncio

    ctx = controller_setup
    started, release = asyncio.Event(), asyncio.Event()

    async def slow_save():
        started.set()
        await release.wait()

    ctx.api.async_save_configuration.side_effect = slow_save
    await ctx.create()
    first = hass.async_create_task(
        call(hass, entity_registry, "button", "press", f"{MAC}_controller_save_configuration")
    )
    await started.wait()
    with pytest.raises(HomeAssistantError) as raised:
        await call(hass, entity_registry, "button", "press", f"{MAC}_controller_save_configuration")
    assert raised.value.translation_key == "save_in_progress"
    release.set()
    await first
    ctx.api.async_save_configuration.assert_awaited_once_with()


@pytest.mark.usefixtures("entity_registry_enabled_by_default")
@pytest.mark.parametrize(
    ("error", "translation_key"),
    [
        (Ac100SessionExpiredError("expired"), "session_expired"),
        (Ac100ProtocolError("rejected"), "control_failed"),
    ],
)
async def test_a_failed_save_is_reported_by_the_coordinator_and_not_repeated(
    hass, controller_setup, entity_registry, error, translation_key
):
    ctx = controller_setup
    ctx.api.async_save_configuration.side_effect = error
    await ctx.create()
    reads = refreshed(ctx)
    with pytest.raises(HomeAssistantError) as raised:
        await call(hass, entity_registry, "button", "press", f"{MAC}_controller_save_configuration")
    assert raised.value.translation_key == translation_key
    ctx.api.async_save_configuration.assert_awaited_once_with()
    assert refreshed(ctx) == reads


@pytest.mark.usefixtures("entity_registry_enabled_by_default")
@pytest.mark.parametrize(
    ("error", "translation_key"),
    [
        (Ac100SessionExpiredError("expired"), "session_expired"),
        (Ac100ProtocolError("rejected"), "control_failed"),
    ],
)
async def test_a_failed_write_is_reported_and_never_repeated(
    hass, controller_setup, entity_registry, error, translation_key
):
    ctx = controller_setup
    ctx.api.async_set_setting.side_effect = error
    _, coordinator = await ctx.create()
    await ctx.read_settings(coordinator)
    reads = refreshed(ctx)
    with pytest.raises(HomeAssistantError) as raised:
        await call(
            hass, entity_registry, "switch", "turn_off", controller_uid("band_navigation_enabled")
        )
    assert raised.value.translation_key == translation_key
    ctx.api.async_set_setting.assert_awaited_once()
    assert refreshed(ctx) == reads
    assert ctx.settings["band_navigation_enabled"] is True


@pytest.mark.usefixtures("entity_registry_enabled_by_default")
async def test_an_unavailable_control_sends_nothing(hass, controller_setup, entity_registry):
    ctx = controller_setup
    await ctx.create()
    entity = controller_uid("band_navigation_enabled")
    assert state_of(hass, entity_registry, "switch", entity).state == STATE_UNAVAILABLE
    await call(hass, entity_registry, "switch", "turn_on", entity)
    ctx.api.async_set_setting.assert_not_awaited()


@pytest.mark.usefixtures("entity_registry_enabled_by_default")
async def test_radio_switches_show_each_radios_configured_state_by_ap_and_unit(
    hass, controller_setup, snapshot, entity_registry
):
    ctx = controller_setup
    _, coordinator = await ctx.create()
    ap_a, ap_b, ap_c = snapshot.aps
    assert (
        state_of(hass, entity_registry, "switch", radio_uid(ap_a, "1")).state == STATE_UNAVAILABLE
    )
    await ctx.read_settings(coordinator)
    assert state_of(hass, entity_registry, "switch", radio_uid(ap_a, "1")).state == STATE_ON
    assert state_of(hass, entity_registry, "switch", radio_uid(ap_a, "2")).state == STATE_OFF
    assert state_of(hass, entity_registry, "switch", radio_uid(ap_c, "1")).state == STATE_ON
    for unit in ("1", "2"):
        assert (
            state_of(hass, entity_registry, "switch", radio_uid(ap_b, unit)).state
            == STATE_UNAVAILABLE
        )
    for ap, unit, band in ((ap_a, "1", "2.4GHz"), (ap_a, "2", "5GHz"), (ap_c, "1", "Radio 1")):
        entity = entity_object(hass, entity_id(entity_registry, "switch", radio_uid(ap, unit)))
        assert entity.translation_key == "radio_enabled"
        assert entity.translation_placeholders == {"band": band}
    unknown = replace(
        configuration(START, RADIOS),
        radios=tuple(
            RadioSettings(a, u, {"enabled": None if (a, u) == ("0", "1") else on})
            for (a, u), on in RADIOS.items()
        ),
    )
    coordinator.async_set_updated_data(replace(snapshot, configuration=pinned(unknown, snapshot)))
    await hass.async_block_till_done()
    assert (
        state_of(hass, entity_registry, "switch", radio_uid(ap_a, "1")).state == STATE_UNAVAILABLE
    )
    assert state_of(hass, entity_registry, "switch", radio_uid(ap_a, "2")).state == STATE_OFF
    for lost in (None, configuration(START, {})):
        coordinator.async_set_updated_data(replace(snapshot, configuration=lost))
        await hass.async_block_till_done()
        assert (
            state_of(hass, entity_registry, "switch", radio_uid(ap_a, "2")).state
            == STATE_UNAVAILABLE
        )


@pytest.mark.usefixtures("entity_registry_enabled_by_default")
@pytest.mark.parametrize(("service", "wanted"), [("turn_on", True), ("turn_off", False)])
async def test_a_radio_switch_sends_one_call_for_the_aps_fresh_address_and_the_radios_unit(
    hass, controller_setup, snapshot, entity_registry, service, wanted
):
    ctx = controller_setup
    _, coordinator = await ctx.create()
    await ctx.read_settings(coordinator)
    ap_a = snapshot.aps[0]
    renumbered = replace(ap_a, entry_id="9")
    ctx.api.async_get_snapshot.return_value = replace(snapshot, aps=(renumbered, *snapshot.aps[1:]))
    reads = refreshed(ctx)
    await call(hass, entity_registry, "switch", service, radio_uid(ap_a, "2"))
    await hass.async_block_till_done()
    ctx.api.async_set_radio_enabled.assert_awaited_once_with("9", "2", wanted)
    assert writes(ctx) == {"async_set_radio_enabled": 1}
    # The action first refreshes the status read to find the AP's address, then the usual refresh.
    assert tuple(now - then for now, then in zip(refreshed(ctx), reads, strict=True)) == (2, 1, 1)


@pytest.mark.usefixtures("entity_registry_enabled_by_default")
async def test_a_radio_switch_follows_the_settings_read_after_its_action(
    hass, controller_setup, snapshot, entity_registry
):
    ctx = controller_setup
    _, coordinator = await ctx.create()
    await ctx.read_settings(coordinator)
    ap_a = snapshot.aps[0]
    await call(hass, entity_registry, "switch", "turn_off", radio_uid(ap_a, "1"))
    await hass.async_block_till_done()
    assert ctx.radios == RADIOS | {("0", "1"): False}
    assert state_of(hass, entity_registry, "switch", radio_uid(ap_a, "1")).state == STATE_OFF
    assert state_of(hass, entity_registry, "switch", radio_uid(ap_a, "2")).state == STATE_OFF
    assert (
        state_of(hass, entity_registry, "switch", radio_uid(snapshot.aps[2], "1")).state == STATE_ON
    )


@pytest.mark.usefixtures("entity_registry_enabled_by_default")
@pytest.mark.parametrize("change", ["offline", "connecting", "gone"])
async def test_a_radio_whose_ap_is_not_online_after_the_refresh_is_not_switched(
    hass, controller_setup, snapshot, entity_registry, change
):
    ctx = controller_setup
    _, coordinator = await ctx.create()
    await ctx.read_settings(coordinator)
    ap_a = snapshot.aps[0]
    if change == "gone":
        aps = snapshot.aps[1:]
    else:
        aps = (replace(ap_a, link_status="2" if change == "offline" else "0"), *snapshot.aps[1:])
    ctx.api.async_get_snapshot.return_value = replace(snapshot, aps=aps)
    with pytest.raises(HomeAssistantError) as raised:
        await call(hass, entity_registry, "switch", "turn_off", radio_uid(ap_a, "1"))
    assert raised.value.translation_key == "control_unavailable"
    ctx.api.async_set_radio_enabled.assert_not_awaited()


@pytest.mark.usefixtures("entity_registry_enabled_by_default")
async def test_a_radio_without_a_known_state_is_unavailable_and_sends_nothing(
    hass, controller_setup, snapshot, entity_registry
):
    ctx = controller_setup
    _, coordinator = await ctx.create()
    await ctx.read_settings(coordinator)
    ctx.radios.pop(("0", "1"))
    state = pinned(configuration(START, ctx.radios), snapshot)
    coordinator.async_set_updated_data(replace(snapshot, configuration=state))
    await hass.async_block_till_done()
    ap_a = snapshot.aps[0]
    assert state_of(hass, entity_registry, "switch", radio_uid(ap_a, "2")).state == STATE_OFF
    assert (
        state_of(hass, entity_registry, "switch", radio_uid(ap_a, "1")).state == STATE_UNAVAILABLE
    )
    await call(hass, entity_registry, "switch", "turn_off", radio_uid(ap_a, "1"))
    ctx.api.async_set_radio_enabled.assert_not_awaited()


def entity_object(hass, entity_id):
    for platform in entity_platform.async_get_platforms(hass, DOMAIN):
        if entity := platform.entities.get(entity_id):
            return entity
    raise AssertionError(f"{entity_id} is not loaded")


@pytest.mark.usefixtures("entity_registry_enabled_by_default")
async def test_radios_and_aps_first_seen_later_get_one_switch_each(
    hass, controller_setup, snapshot, entity_registry
):
    ctx = controller_setup
    entry, coordinator = await ctx.create()
    known = {
        row.unique_id for row in control_rows(entity_registry, entry) if row.domain == "switch"
    }
    third = ApRadio("3", "5GHz", 0, 100, 5.0)
    ap_a = snapshot.aps[0]
    unnamed = ApRadio("", "5GHz", 0, 100, 5.0)
    new_ap = make_ap(3, 0x44, radios=(third,))
    aps = (replace(ap_a, radios=(*ap_a.radios, third, unnamed)), *snapshot.aps[1:], new_ap)
    for _ in range(2):
        coordinator.async_set_updated_data(replace(snapshot, aps=aps))
        await hass.async_block_till_done()
    added = {
        row.unique_id for row in control_rows(entity_registry, entry) if row.domain == "switch"
    } - known
    assert added == {radio_uid(ap_a, "3"), radio_uid(new_ap, "3")}


def test_every_control_has_english_names_states_and_exception_texts():
    root = Path(__file__).parents[1]
    strings = json.loads((root / "custom_components" / "tplink_ac100" / "strings.json").read_text())
    entities = strings["entity"]
    for key in SWITCHES:
        assert entities["switch"][key]["name"]
    for key in NUMBERS:
        assert entities["number"][key]["name"]
    for key in SELECTS:
        translation = entities["select"][key]
        assert translation["name"]
        assert set(translation["state"]) == set(select_options(FIELDS[key]))
    assert entities["text"]["device_alias"]["name"]
    assert entities["button"]["save_configuration"]["name"]
    assert entities["switch"]["radio_enabled"]["name"].startswith("{band} ")
    for key in ("invalid_value", "save_outcome_unknown", "control_unavailable", "control_failed"):
        assert strings["exceptions"][key]["message"]
    assert "{reason}" in strings["exceptions"]["invalid_value"]["message"]
