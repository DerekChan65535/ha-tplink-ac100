"""Per-radio settings entities of access points on the Home Assistant test runtime."""

import json
import re
from dataclasses import replace
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
from pytest_homeassistant.common import MockConfigEntry

from custom_components.tplink_ac100.api import Ac100Api
from custom_components.tplink_ac100.const import DOMAIN
from custom_components.tplink_ac100.coordinator import CONFIGURATION_CONTEXT
from custom_components.tplink_ac100.diagnostics import async_get_config_entry_diagnostics
from custom_components.tplink_ac100.models import AccessPoint, ApRadio, Controller, Snapshot
from custom_components.tplink_ac100.monitoring import (
    RADIO_FIELDS,
    RADIO_SOURCE,
    ConfigurationState,
    MonitoringState,
    RadioSettings,
)

pytestmark = pytest.mark.usefixtures("enable_custom_integrations")

CONTROLLER_MAC = "02:00:00:00:00:10"
FIELDS = {field.key: field for field in RADIO_FIELDS}
RADIO_ENTITY = re.compile(r"_radio_[0-9]+_(.+)$")
VALUES = {
    "enabled": True,
    "mode": "802_11b_g_n",
    "channel_width": "auto",
    "configured_channel": "auto",
    "channel_switching": "auto",
    "power_level": 10,
    "client_limit": 64,
    "beacon_interval": 100,
    "dtim_period": 1,
    "rts_threshold": 2346,
    "fragmentation_threshold": 2346,
    "short_guard_interval": True,
    "wmm": True,
    "airtime_scheduling": False,
    "weak_signal_restriction": False,
    "weak_signal_restriction_threshold": None,
    "weak_signal_disconnect": True,
    "weak_signal_disconnect_threshold": -80,
    "client_forward_access": True,
    "client_forward_access_threshold": -60,
    "broadcast_probe_response": True,
}


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


MAC_BY_ENTRY = {0: "02:00:00:00:00:41", 1: "02:00:00:00:00:42", 2: "02:00:00:00:00:43"}


def make_settings(entry_id, unit, channel, **overrides):
    """A radio's settings as the coordinator publishes them; the channel tells radios apart."""
    return RadioSettings(
        str(entry_id),
        str(unit),
        {**VALUES, "configured_channel": channel, **overrides},
        MAC_BY_ENTRY[entry_id],
    )


def make_configuration(*radios):
    return ConfigurationState(available_sections=frozenset({RADIO_SOURCE}), radios=radios)


@pytest.fixture
def snapshot():
    """An online AP, an offline AP and an online single-radio AP without a band name."""
    controller = Controller(CONTROLLER_MAC, "TL-AC100", "5.0", "1.0", {"core1": 1.0}, 40.0, 7200)
    aps = (
        make_ap(0, 0x41),
        make_ap(1, 0x42, status="2"),
        make_ap(2, 0x43, radios=(ApRadio("1", "---", 0, 1, 5.0),)),
    )
    return Snapshot(controller, aps, ())


@pytest.fixture
def configuration():
    """Settings of every radio of the three APs, told apart by their configured channel."""
    return make_configuration(
        make_settings(0, 1, 1),
        make_settings(0, 2, 36),
        make_settings(1, 1, 6),
        make_settings(1, 2, 44),
        make_settings(2, 1, 11),
    )


@pytest.fixture
async def radio_setup(hass, snapshot):
    """Load the sensor platforms with the real coordinator; never call the physical device."""
    clock = [100.0]
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=CONTROLLER_MAC,
        data={"host": "http://192.0.2.1", "username": "example", "password": "example-only"},
    )
    entry.add_to_hass(hass)
    api = AsyncMock(spec=Ac100Api)
    api.async_get_snapshot.return_value = snapshot
    api.async_get_monitoring.return_value = MonitoringState()
    api.async_get_configuration.return_value = ConfigurationState()
    with (
        patch("custom_components.tplink_ac100.Ac100Api", return_value=api),
        patch(
            "custom_components.tplink_ac100.PLATFORMS", [Platform.SENSOR, Platform.BINARY_SENSOR]
        ),
        # Without the controller's own settings entities only the radio entities can start the read.
        patch("custom_components.tplink_ac100.configuration_entity.CONFIGURATION_FIELDS", ()),
        patch("custom_components.tplink_ac100.coordinator.monotonic", side_effect=lambda: clock[0]),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        yield entry, entry.runtime_data, api, clock
        assert await hass.config_entries.async_unload(entry.entry_id)


def domain_of(field):
    return "binary_sensor" if field.kind == "bool" else "sensor"


def unique_id(ap, unit, key):
    return f"{CONTROLLER_MAC}_{ap.mac}_radio_{unit}_{key}"


def entity_id(registry, ap, unit, key):
    result = registry.async_get_entity_id(domain_of(FIELDS[key]), DOMAIN, unique_id(ap, unit, key))
    assert result is not None, (ap.mac, unit, key)
    return result


def state_of(hass, registry, ap, unit, key):
    state = hass.states.get(entity_id(registry, ap, unit, key))
    assert state is not None, (ap.mac, unit, key)
    return state


def expected_state(value):
    if value is None:
        return STATE_UNKNOWN
    if isinstance(value, bool):
        return STATE_ON if value else STATE_OFF
    return str(value)


def radio_rows(registry, entry):
    """The entry's radio setting entities by unique ID, apart from the status radio sensors."""
    return {
        row.unique_id: row
        for row in er.async_entries_for_config_entry(registry, entry.entry_id)
        if (match := RADIO_ENTITY.search(row.unique_id)) and match[1] in FIELDS
    }


def entity_object(hass, entity_id):
    for platform in entity_platform.async_get_platforms(hass, DOMAIN):
        if entity := platform.entities.get(entity_id):
            return entity
    raise AssertionError(f"{entity_id} is not loaded")


async def update(hass, coordinator, snapshot):
    coordinator.async_set_updated_data(snapshot)
    await hass.async_block_till_done()


async def test_entities_exist_before_any_settings_read_and_start_disabled(
    hass, radio_setup, snapshot, entity_registry, device_registry
):
    """The status poll names the radios, so a user can enable a setting before any read."""
    entry, coordinator, api, clock = radio_setup
    rows = radio_rows(entity_registry, entry)
    expected = {
        unique_id(ap, radio.unit, field.key)
        for ap in snapshot.aps
        for radio in ap.radios
        for field in RADIO_FIELDS
    }
    assert set(rows) == expected
    assert len(expected) == 5 * len(RADIO_FIELDS)
    for ap in snapshot.aps:
        device = device_registry.async_get_device_by_identifier((DOMAIN, ap.mac), entry.entry_id)
        for radio in ap.radios:
            for field in RADIO_FIELDS:
                row = rows[unique_id(ap, radio.unit, field.key)]
                assert row.domain == domain_of(field)
                assert row.disabled_by is er.RegistryEntryDisabler.INTEGRATION
                assert row.entity_category is EntityCategory.DIAGNOSTIC
                assert row.translation_key == f"radio_{field.key}"
                assert row.device_id == device.id
                assert hass.states.get(row.entity_id) is None
    assert CONFIGURATION_CONTEXT not in set(coordinator.async_contexts())
    for now in (160, 1000, 5000):
        clock[0] = now
        await coordinator.async_refresh()
    api.async_get_configuration.assert_not_awaited()
    assert coordinator.data.configuration is None


@pytest.mark.usefixtures("entity_registry_enabled_by_default")
async def test_an_enabled_radio_setting_starts_the_settings_read(
    hass, radio_setup, snapshot, configuration, entity_registry
):
    _, coordinator, api, clock = radio_setup
    ap_a = snapshot.aps[0]
    assert CONFIGURATION_CONTEXT in set(coordinator.async_contexts())
    assert state_of(hass, entity_registry, ap_a, "1", "mode").state == STATE_UNAVAILABLE
    api.async_get_configuration.return_value = configuration
    api.async_get_configuration.assert_not_awaited()
    clock[0] = 160
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    api.async_get_configuration.assert_awaited_once_with()
    assert state_of(hass, entity_registry, ap_a, "1", "configured_channel").state == "1"
    assert state_of(hass, entity_registry, ap_a, "2", "configured_channel").state == "36"
    assert state_of(hass, entity_registry, ap_a, "1", "enabled").state == STATE_ON


@pytest.mark.usefixtures("entity_registry_enabled_by_default")
async def test_values_units_enum_options_and_names(
    hass, radio_setup, snapshot, configuration, entity_registry
):
    _, coordinator, _, _ = radio_setup
    await update(hass, coordinator, replace(snapshot, configuration=configuration))
    ap_a = snapshot.aps[0]
    private = (CONTROLLER_MAC, ap_a.mac, ap_a.ip)
    for field in RADIO_FIELDS:
        state = state_of(hass, entity_registry, ap_a, "1", field.key)
        value = 1 if field.key == "configured_channel" else VALUES[field.key]
        assert state.state == expected_state(value), field.key
        assert state.attributes.get("unit_of_measurement") == field.unit
        is_enum = field.kind == "enum"
        assert state.attributes.get("device_class") == ("enum" if is_enum else None)
        assert state.attributes.get("options") == (list(field.options) if is_enum else None)
        assert "state_class" not in state.attributes
        assert not [item for item in private if item in repr(state.attributes) + state.state]
    mode = state_of(hass, entity_registry, ap_a, "1", "mode")
    assert mode.attributes["friendly_name"] == "AP 65 2.4GHz radio mode"
    assert state_of(hass, entity_registry, ap_a, "1", "beacon_interval").state == "100"


@pytest.mark.usefixtures("entity_registry_enabled_by_default")
async def test_unknown_values_are_unknown_not_off_or_zero(
    hass, radio_setup, snapshot, entity_registry
):
    _, coordinator, _, _ = radio_setup
    ap_a = snapshot.aps[0]
    unknown = RadioSettings("0", "1", {field.key: None for field in RADIO_FIELDS}, MAC_BY_ENTRY[0])
    await update(hass, coordinator, replace(snapshot, configuration=make_configuration(unknown)))
    for field in RADIO_FIELDS:
        assert state_of(hass, entity_registry, ap_a, "1", field.key).state == STATE_UNKNOWN
    partial = RadioSettings("0", "1", {"mode": "802_11b_g_n"}, MAC_BY_ENTRY[0])
    await update(hass, coordinator, replace(snapshot, configuration=make_configuration(partial)))
    assert state_of(hass, entity_registry, ap_a, "1", "mode").state == "802_11b_g_n"
    assert state_of(hass, entity_registry, ap_a, "1", "wmm").state == STATE_UNKNOWN


@pytest.mark.usefixtures("entity_registry_enabled_by_default")
async def test_entities_follow_the_ap_mac_through_renumbering_and_reordering(
    hass, radio_setup, snapshot, entity_registry
):
    """The coordinator pins each radio row to the AP that holds its entry_id in the same poll."""
    entry, coordinator, api, clock = radio_setup
    ap_a, ap_b, ap_c = snapshot.aps
    online_b = replace(ap_b, link_status="1")
    channels = {
        (ap_a.mac, "1"): "1",
        (ap_a.mac, "2"): "36",
        (ap_b.mac, "1"): "6",
        (ap_b.mac, "2"): "44",
        (ap_c.mac, "1"): "11",
    }

    def api_rows(aps):
        """Rows as the API returns them: addressed by entry_id only."""
        return make_configuration(
            *(
                RadioSettings(
                    ap.entry_id,
                    radio.unit,
                    {**VALUES, "configured_channel": int(channels[(ap.mac, radio.unit)])},
                )
                for ap in reversed(aps)
                for radio in ap.radios
            )
        )

    def observed():
        return {
            (ap.mac, radio.unit): state_of(
                hass, entity_registry, ap, radio.unit, "configured_channel"
            ).state
            for ap in snapshot.aps
            for radio in ap.radios
        }

    async def read(aps, now):
        api.async_get_snapshot.return_value = replace(snapshot, aps=aps)
        api.async_get_configuration.return_value = api_rows(aps)
        for tick in (now, now + 60):  # the status read comes first, then the settings read
            clock[0] = tick
            await coordinator.async_refresh()
        await hass.async_block_till_done()

    await read((ap_a, online_b, ap_c), 1000)
    assert observed() == channels
    count = len(er.async_entries_for_config_entry(entity_registry, entry.entry_id))
    swapped = (replace(online_b, entry_id="0"), replace(ap_a, entry_id="1"), ap_c)
    await read(swapped, 2000)
    assert observed() == channels
    await read(swapped[::-1], 3000)
    assert observed() == channels
    assert len(er.async_entries_for_config_entry(entity_registry, entry.entry_id)) == count


@pytest.mark.usefixtures("entity_registry_enabled_by_default")
async def test_unavailable_while_the_ap_is_not_online_or_the_radio_row_is_missing(
    hass, radio_setup, snapshot, configuration, entity_registry
):
    _, coordinator, _, _ = radio_setup
    ap_a, ap_b, ap_c = snapshot.aps
    base = replace(snapshot, configuration=configuration)
    await update(hass, coordinator, base)
    for unit in ("1", "2"):
        assert state_of(hass, entity_registry, ap_b, unit, "mode").state == STATE_UNAVAILABLE
        assert state_of(hass, entity_registry, ap_a, unit, "mode").state == "802_11b_g_n"
    for status in ("0", "2"):
        not_online = replace(ap_a, link_status=status)
        await update(hass, coordinator, replace(base, aps=(not_online, ap_b, ap_c)))
        for key in FIELDS:
            assert state_of(hass, entity_registry, ap_a, "1", key).state == STATE_UNAVAILABLE
    await update(hass, coordinator, base)
    assert state_of(hass, entity_registry, ap_a, "1", "wmm").state == STATE_ON
    rows = [r for r in configuration.radios if (r.ap_entry_id, r.unit) != ("0", "2")]
    await update(hass, coordinator, replace(base, configuration=make_configuration(*rows)))
    for key in FIELDS:
        assert state_of(hass, entity_registry, ap_a, "2", key).state == STATE_UNAVAILABLE
        assert state_of(hass, entity_registry, ap_a, "1", key).state != STATE_UNAVAILABLE
    for lost in (None, ConfigurationState(), make_configuration()):
        await update(hass, coordinator, replace(base, configuration=lost))
        for key in FIELDS:
            assert state_of(hass, entity_registry, ap_a, "1", key).state == STATE_UNAVAILABLE
    await update(hass, coordinator, replace(base, aps=()))
    assert state_of(hass, entity_registry, ap_a, "1", "mode").state == STATE_UNAVAILABLE


async def test_radios_and_aps_first_seen_later_get_entities_once(
    hass, radio_setup, snapshot, entity_registry, caplog
):
    entry, coordinator, _, _ = radio_setup
    ap_a = snapshot.aps[0]
    third = ApRadio("3", "5GHz", 0, 100, 5.0)
    new_ap = make_ap(3, 0x44, radios=(third,))
    aps = (replace(ap_a, radios=(*ap_a.radios, third)), *snapshot.aps[1:], new_ap)
    known = set(radio_rows(entity_registry, entry))
    await update(hass, coordinator, replace(snapshot, aps=aps))
    added = set(radio_rows(entity_registry, entry)) - known
    assert added == {
        unique_id(ap, "3", field.key) for ap in (ap_a, new_ap) for field in RADIO_FIELDS
    }
    await update(hass, coordinator, replace(snapshot, aps=aps))
    assert set(radio_rows(entity_registry, entry)) == known | added
    assert "does not generate unique IDs" not in caplog.text


async def test_a_radio_without_a_unit_has_no_stable_identity(
    hass, radio_setup, snapshot, entity_registry
):
    entry, coordinator, _, _ = radio_setup
    ap_a = snapshot.aps[0]
    unnamed = replace(ap_a, radios=(*ap_a.radios, ApRadio("", "5GHz", 0, 100, 5.0)))
    known = set(radio_rows(entity_registry, entry))
    await update(hass, coordinator, replace(snapshot, aps=(unnamed, *snapshot.aps[1:])))
    assert set(radio_rows(entity_registry, entry)) == known
    rows = er.async_entries_for_config_entry(entity_registry, entry.entry_id)
    assert not [row for row in rows if "radio__" in row.unique_id]


@pytest.mark.usefixtures("entity_registry_enabled_by_default")
async def test_band_label_is_the_controller_name_with_a_plain_fallback(
    hass, radio_setup, snapshot, entity_registry
):
    ap_a, _, ap_c = snapshot.aps
    for ap, unit, band in ((ap_a, "1", "2.4GHz"), (ap_a, "2", "5GHz"), (ap_c, "1", "Radio 1")):
        for key in ("enabled", "mode", "beacon_interval"):
            entity = entity_object(hass, entity_id(entity_registry, ap, unit, key))
            assert entity.translation_placeholders == {"band": band}


def test_every_radio_field_has_english_names_states_and_icons():
    component = Path(__file__).parents[1] / "custom_components" / "tplink_ac100"
    strings = json.loads((component / "strings.json").read_text())
    icons = json.loads((component / "icons.json").read_text())
    assert json.loads((component / "translations" / "en.json").read_text()) == strings
    for field in RADIO_FIELDS:
        translation = strings["entity"][domain_of(field)][f"radio_{field.key}"]
        assert translation["name"].startswith("{band} ")
        if field.kind == "enum":
            assert set(translation["state"]) == set(field.options)
            assert all(re.fullmatch(r"[a-z0-9]+(_[a-z0-9]+)*", state) for state in field.options)
        else:
            assert "state" not in translation
    for platform, entries in icons["entity"].items():
        radio_icons = {key for key in entries if key.startswith("radio_")}
        assert radio_icons <= set(strings["entity"][platform])


async def test_diagnostics_count_radio_settings_without_identity(
    hass, radio_setup, snapshot, configuration
):
    entry, coordinator, _, _ = radio_setup
    await update(hass, coordinator, replace(snapshot, configuration=configuration))
    diagnostics = await async_get_config_entry_diagnostics(hass, entry)
    assert diagnostics["configuration"]["radio_settings"] == 5
    assert RADIO_SOURCE in diagnostics["configuration"]["available_sources"]
    encoded = json.dumps(diagnostics)
    private = [CONTROLLER_MAC, *entry.data.values()]
    private += [value for ap in snapshot.aps for value in (ap.mac, ap.ip, ap.name)]
    assert not [value for value in private if value in encoded]
