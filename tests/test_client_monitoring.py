"""Client device pages: readings that are on by default, on the Home Assistant runtime."""

import json
import re
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest
from homeassistant.const import (
    STATE_OFF,
    STATE_ON,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
    EntityCategory,
    Platform,
)
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.update_coordinator import UpdateFailed
from pytest_homeassistant.common import MockConfigEntry

from custom_components.tplink_ac100.api import Ac100Api
from custom_components.tplink_ac100.client_monitoring import CLIENT_SENSORS, Ac100ClientSensor
from custom_components.tplink_ac100.const import DOMAIN
from custom_components.tplink_ac100.diagnostics import async_get_config_entry_diagnostics
from custom_components.tplink_ac100.models import (
    AccessPoint,
    Client,
    ClientRadio,
    Controller,
    Snapshot,
)
from custom_components.tplink_ac100.monitoring import ConfigurationState, MonitoringState
from custom_components.tplink_ac100.terminal_controls import binding_target_labels

pytestmark = pytest.mark.usefixtures("enable_custom_integrations")

CONTROLLER_MAC = "02:00:00:00:00:01"
COMPONENT = Path(__file__).parents[1] / "custom_components/tplink_ac100"
FIXTURES = Path(__file__).parent / "fixtures"
PLATFORMS = [Platform.SENSOR, Platform.BINARY_SENSOR, Platform.DEVICE_TRACKER]


def access_point(index: int, name: str) -> AccessPoint:
    mac = f"02:00:00:aa:bb:{index:02x}"
    return AccessPoint(
        str(index), name, "TL-XAP1502GI-PoE", mac, f"192.0.2.{index + 10}", "1", True, 600, ()
    )


def wifi_client(index: int, name: str | None = None, **fields) -> Client:
    values = {
        "entry_id": str(index),
        "mac": f"02:00:00:cc:dd:{index:02x}",
        "name": name,
        "ip": f"192.0.2.{index + 50}",
        "ipv6": "",
        "ssid": "Example wireless",
        "ap_name": "Garage",
        "radios": (),
    }
    return Client(**values | fields)


PHONE = wifi_client(
    1,
    "Example phone",
    ipv6="2001:db8::51",
    radios=(ClientRadio("1", "2.4GHz", -55, 72.0),),
    upload_kilobytes_per_second=12.0,
    download_kilobytes_per_second=340.0,
    brand="apple",
    operating_system="ios",
    vlan_id=27,
    connected_at="2026/11/16 10:25:33",
    ssid_encoding="1",
)
# Everything the controller reports for this client is a placeholder: 0.0.0.0, ::, --- or blank.
BARE = wifi_client(
    2,
    None,
    ip="0.0.0.0",
    ipv6="::",
    ssid="",
    ap_name="",
    radios=(ClientRadio("1", "---", None, None),),
)

PHONE_STATES = {
    "connectivity": STATE_ON,
    "signal_strength": "-55",
    "link_rate": "72.0",
    "wifi_upload_speed": "12.0",
    "wifi_download_speed": "340.0",
    "ipv4_address": "192.0.2.51",
    "ipv6_address": "2001:db8::51",
    "ssid": "Example wireless",
    "ssid_encoding": "UTF-8",
    "access_point": "Garage",
    "wifi_band": "2.4GHz",
    "radio_unit": "1 (2.4GHz)",
    "vlan_id": "27",
    "vendor": "Apple",
    "operating_system": "iOS",
    "connection_time": "2026/11/16 10:25:33",
}
BARE_STATES = {key: STATE_ON if key == "connectivity" else STATE_UNKNOWN for key in PHONE_STATES}


@pytest.fixture
def ac_snapshot() -> Snapshot:
    return Snapshot(
        Controller(CONTROLLER_MAC, "TL-AC100", "5.0", "1.0.5", {}, 30.0, 3600),
        (access_point(1, "Garage"), access_point(2, "Study")),
        (PHONE, BARE),
    )


@pytest.fixture
def api(ac_snapshot):
    """Simulate the controller; only the sensor, binary sensor and tracker platforms load."""
    api = AsyncMock(spec=Ac100Api)
    api.async_get_snapshot.return_value = ac_snapshot
    api.async_get_monitoring.return_value = MonitoringState()
    api.async_get_configuration.return_value = ConfigurationState()
    with (
        patch("custom_components.tplink_ac100.Ac100Api", return_value=api),
        patch("custom_components.tplink_ac100.PLATFORMS", PLATFORMS),
    ):
        yield api


def make_entry(hass, mac: str = CONTROLLER_MAC) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=mac,
        data={"host": "http://192.0.2.1", "username": "example", "password": "example-only"},
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


def unique_id(client: Client, key: str, controller_mac: str = CONTROLLER_MAC) -> str:
    return f"{controller_mac}_client_{client.mac}_{key}"


def client_device(hass, mac: str) -> dr.DeviceEntry | None:
    entry = hass.config_entries.async_entries(DOMAIN)[0]
    return dr.async_get(hass).async_get_device_by_connection(
        (dr.CONNECTION_NETWORK_MAC, mac), entry.entry_id
    )


def page(hass, client: Client, controller_mac: str = CONTROLLER_MAC) -> dict:
    """The state of each enabled reading on the client's device page, by reading key."""
    device = client_device(hass, client.mac)
    prefix = unique_id(client, "", controller_mac)
    return {
        row.unique_id.removeprefix(prefix): hass.states.get(row.entity_id)
        for row in er.async_entries_for_device(er.async_get(hass), device.id)
        if row.unique_id.startswith(prefix)
    }


def states(hass, client: Client, controller_mac: str = CONTROLLER_MAC) -> dict[str, str]:
    return {key: state.state for key, state in page(hass, client, controller_mac).items()}


def reading_id(registry, client: Client, key: str) -> str:
    domain = "binary_sensor" if key == "connectivity" else "sensor"
    entity_id = registry.async_get_entity_id(domain, DOMAIN, unique_id(client, key))
    assert entity_id is not None, key
    return entity_id


def entry_rows(hass, entry) -> dict[str, str]:
    """The entity ID of every registry row of the entry, by unique ID."""
    registry = er.async_get(hass)
    return {
        row.unique_id: row.entity_id
        for row in er.async_entries_for_config_entry(registry, entry.entry_id)
    }


# The page of a client


async def test_a_client_page_has_every_reading_enabled_while_the_tracker_stays_disabled(
    hass, api, entity_registry
):
    entry = make_entry(hass)
    await setup(hass, entry)
    device = client_device(hass, PHONE.mac)
    rows = er.async_entries_for_device(entity_registry, device.id, include_disabled_entities=True)
    # The saved configuration readings of the terminal share the page; test_terminal_monitoring.py
    # covers them.
    client_rows = [row for row in rows if "_terminal_" not in row.unique_id]
    assert {row.unique_id for row in client_rows} == {
        PHONE.mac,
        *(unique_id(PHONE, key) for key in PHONE_STATES),
    }
    for row in rows:
        assert row.config_entry_id == entry.entry_id
        if row.unique_id == PHONE.mac:
            assert row.domain == "device_tracker"
            assert row.disabled_by is er.RegistryEntryDisabler.INTEGRATION
        else:
            assert row.disabled_by is None
    assert states(hass, PHONE) == PHONE_STATES
    assert device.connections == {(dr.CONNECTION_NETWORK_MAC, PHONE.mac)}
    assert not device.identifiers
    assert (device.manufacturer, device.model, device.sw_version, device.hw_version) == (None,) * 4
    assert (
        reading_id(entity_registry, PHONE, "signal_strength")
        == "sensor.example_phone_signal_strength"
    )
    assert (
        reading_id(entity_registry, PHONE, "connectivity")
        == "binary_sensor.example_phone_connectivity"
    )


@pytest.mark.parametrize(
    ("key", "device_class", "unit", "state_class", "category"),
    [
        ("signal_strength", "signal_strength", "dBm", "measurement", None),
        ("link_rate", "data_rate", "Mbit/s", "measurement", None),
        ("wifi_upload_speed", "data_rate", "kB/s", "measurement", None),
        ("wifi_download_speed", "data_rate", "kB/s", "measurement", None),
        ("ipv4_address", None, None, None, EntityCategory.DIAGNOSTIC),
        ("ipv6_address", None, None, None, EntityCategory.DIAGNOSTIC),
        ("ssid", None, None, None, EntityCategory.DIAGNOSTIC),
        ("ssid_encoding", None, None, None, EntityCategory.DIAGNOSTIC),
        ("access_point", None, None, None, EntityCategory.DIAGNOSTIC),
        ("wifi_band", None, None, None, EntityCategory.DIAGNOSTIC),
        ("radio_unit", None, None, None, EntityCategory.DIAGNOSTIC),
        ("vlan_id", None, None, None, EntityCategory.DIAGNOSTIC),
        ("vendor", None, None, None, EntityCategory.DIAGNOSTIC),
        ("operating_system", None, None, None, EntityCategory.DIAGNOSTIC),
        ("connection_time", None, None, None, EntityCategory.DIAGNOSTIC),
        ("connectivity", "connectivity", None, None, None),
    ],
)
async def test_reading_classes_units_and_categories(
    hass, api, entity_registry, key, device_class, unit, state_class, category
):
    await setup(hass, make_entry(hass))
    entity_id = reading_id(entity_registry, PHONE, key)
    attributes = hass.states.get(entity_id).attributes
    assert attributes.get("device_class") == device_class
    assert attributes.get("unit_of_measurement") == unit
    assert attributes.get("state_class") == state_class
    assert entity_registry.async_get(entity_id).entity_category is category


async def test_unreported_clients_have_unknown_readings_and_are_still_connected(hass, api):
    await setup(hass, make_entry(hass))
    assert states(hass, BARE) == BARE_STATES


@pytest.mark.parametrize(
    ("changes", "key"),
    [
        pytest.param({"ip": "0.0.0.0"}, "ipv4_address", id="IPv4 0.0.0.0"),
        pytest.param({"ip": ""}, "ipv4_address", id="blank IPv4"),
        pytest.param({"ip": "---"}, "ipv4_address", id="IPv4 ---"),
        pytest.param({"ip": "2001:db8::1"}, "ipv4_address", id="IPv6 in the IPv4 field"),
        pytest.param({"ipv6": "::"}, "ipv6_address", id="IPv6 ::"),
        pytest.param({"ipv6": ""}, "ipv6_address", id="blank IPv6"),
        pytest.param({"ipv6": "---"}, "ipv6_address", id="IPv6 ---"),
        pytest.param({"ipv6": "192.0.2.7"}, "ipv6_address", id="IPv4 in the IPv6 field"),
        pytest.param({"ssid": " "}, "ssid", id="blank SSID"),
        pytest.param({"ap_name": ""}, "access_point", id="blank AP name"),
        pytest.param({"brand": None}, "vendor", id="no vendor"),
        pytest.param({"operating_system": None}, "operating_system", id="no operating system"),
        pytest.param({"vlan_id": None}, "vlan_id", id="no VLAN, which the web UI shows as ---"),
        pytest.param({"connected_at": None}, "connection_time", id="no connection time"),
        pytest.param({"ssid_encoding": None}, "ssid_encoding", id="no SSID encoding"),
        pytest.param({"ssid_encoding": " "}, "ssid_encoding", id="blank SSID encoding"),
        pytest.param({"ssid_encoding": "0"}, "ssid_encoding", id="SSID encoding that is no 1 or 2"),
        pytest.param(
            {"radios": (ClientRadio("", "2.4GHz", -50, 72.0),)}, "radio_unit", id="no radio unit"
        ),
        pytest.param(
            {"radios": (ClientRadio("1", "---", -50, 72.0),)},
            "radio_unit",
            id="radio without a band name, which the web UI shows as ---",
        ),
        pytest.param({"upload_kilobytes_per_second": None}, "wifi_upload_speed", id="no upload"),
        pytest.param(
            {"download_kilobytes_per_second": None}, "wifi_download_speed", id="no download"
        ),
        pytest.param({"radios": ()}, "wifi_band", id="no radio"),
        pytest.param({"radios": (ClientRadio("1", "---", -50, 72.0),)}, "wifi_band", id="band ---"),
        pytest.param({"radios": (ClientRadio("1", " ", -50, 72.0),)}, "wifi_band", id="blank band"),
        pytest.param(
            {"radios": (ClientRadio("1", "2.4GHz", None, 72.0),)}, "signal_strength", id="no RSSI"
        ),
        pytest.param(
            {"radios": (ClientRadio("1", "2.4GHz", -50, None),)}, "link_rate", id="no link rate"
        ),
    ],
)
async def test_a_placeholder_is_unknown_not_a_value(hass, api, ac_snapshot, changes, key):
    api.async_get_snapshot.return_value = replace(ac_snapshot, clients=(replace(PHONE, **changes),))
    await setup(hass, make_entry(hass))
    readings = states(hass, PHONE)
    assert readings[key] == STATE_UNKNOWN
    assert readings["connectivity"] == STATE_ON


async def test_zero_is_a_reading_and_the_vlan_is_not_guessed(hass, api, ac_snapshot):
    idle = replace(PHONE, upload_kilobytes_per_second=0.0, download_kilobytes_per_second=0.0)
    api.async_get_snapshot.return_value = replace(ac_snapshot, clients=(idle,))
    await setup(hass, make_entry(hass))
    readings = states(hass, PHONE)
    assert (readings["wifi_upload_speed"], readings["wifi_download_speed"]) == ("0.0", "0.0")
    assert readings["vlan_id"] == "27"


@pytest.mark.parametrize(
    ("brand", "system", "vendor", "operating_system"),
    [
        ("apple", "ios", "Apple", "iOS"),
        ("Samsung", "android", "Samsung", "Android"),
        ("oppo", "harmonyOS", "OPPO", "HarmonyOS"),
        ("vivo", "windows", "vivo", "Windows"),
        ("others", "others", "Others", "Others"),
        ("Others", "OTHERS", "Others", "Others"),
        ("Example vendor", "Example OS", "Example vendor", "Example OS"),
    ],
)
async def test_vendor_and_system_use_the_controllers_own_names_or_its_raw_text(
    hass, api, ac_snapshot, entity_registry, brand, system, vendor, operating_system
):
    api.async_get_snapshot.return_value = replace(
        ac_snapshot, clients=(replace(PHONE, brand=brand, operating_system=system),)
    )
    await setup(hass, make_entry(hass))
    readings = states(hass, PHONE)
    assert (readings["vendor"], readings["operating_system"]) == (vendor, operating_system)


async def test_others_is_a_category_of_its_own_and_not_an_unidentified_client(
    hass, api, ac_snapshot
):
    """The controller's Others category is shown; a client it did not identify stays unknown."""
    other = replace(PHONE, brand="others", operating_system="others")
    unidentified = replace(PHONE, brand=None, operating_system=None)
    api.async_get_snapshot.return_value = replace(ac_snapshot, clients=(other,))
    coordinator = await setup(hass, make_entry(hass))
    readings = states(hass, PHONE)
    assert (readings["vendor"], readings["operating_system"]) == ("Others", "Others")

    await publish(hass, coordinator, replace(ac_snapshot, clients=(unidentified,)))
    readings = states(hass, PHONE)
    assert (readings["vendor"], readings["operating_system"]) == (STATE_UNKNOWN, STATE_UNKNOWN)


@pytest.mark.parametrize(
    ("encoding", "state"),
    [("1", "UTF-8"), ("2", "GBK"), (" 2 ", "GBK"), ("3", STATE_UNKNOWN), ("", STATE_UNKNOWN)],
)
async def test_the_ssid_encoding_is_utf_8_for_1_and_gbk_for_2_and_otherwise_unknown(
    hass, api, ac_snapshot, encoding, state
):
    api.async_get_snapshot.return_value = replace(
        ac_snapshot, clients=(replace(PHONE, ssid_encoding=encoding),)
    )
    await setup(hass, make_entry(hass))
    assert states(hass, PHONE)["ssid_encoding"] == state


async def test_the_radio_unit_is_shown_with_its_band_as_the_web_ui_shows_it(hass, api, ac_snapshot):
    radios = (
        ClientRadio("2", "5GHz", -48, 866.0),
        ClientRadio("1", "2.4GHz", -60, 72.0),
        ClientRadio("2", "5GHz", -50, 400.0),
        ClientRadio("3", "---", None, None),
    )
    api.async_get_snapshot.return_value = replace(
        ac_snapshot, clients=(replace(PHONE, radios=radios),)
    )
    await setup(hass, make_entry(hass))
    state = page(hass, PHONE)["radio_unit"]
    assert state.state == "2 (5GHz), 1 (2.4GHz)"
    assert "radios" not in state.attributes


async def test_connection_time_is_the_controllers_local_text(hass, api, ac_snapshot):
    api.async_get_snapshot.return_value = replace(ac_snapshot, clients=(PHONE,))
    await setup(hass, make_entry(hass))
    state = page(hass, PHONE)["connection_time"]
    assert state.state == "2026/11/16 10:25:33"
    assert not {"device_class", "unit_of_measurement"} & set(state.attributes)


async def test_a_client_on_two_radios_shows_the_best_readings_and_every_radio(
    hass, api, ac_snapshot
):
    radios = (
        ClientRadio("1", "2.4GHz", -60, 72.0),
        ClientRadio("2", "5GHz", -48, 866.0),
        ClientRadio("3", "2.4GHz", None, None),
    )
    api.async_get_snapshot.return_value = replace(
        ac_snapshot, clients=(replace(PHONE, radios=radios),)
    )
    await setup(hass, make_entry(hass))
    readings = page(hass, PHONE)
    assert readings["signal_strength"].state == "-48"
    assert readings["link_rate"].state == "866.0"
    assert readings["wifi_band"].state == "2.4GHz, 5GHz"
    records = [
        {"unit": "1", "frequency": "2.4GHz", "signal_strength": -60, "negotiated_rate_mbps": 72.0},
        {"unit": "2", "frequency": "5GHz", "signal_strength": -48, "negotiated_rate_mbps": 866.0},
        {"unit": "3", "frequency": "2.4GHz", "signal_strength": None, "negotiated_rate_mbps": None},
    ]
    for key, state in readings.items():
        if key in ("signal_strength", "link_rate"):
            assert state.attributes["radios"] == records
        else:
            assert "radios" not in state.attributes
    assert "radios" in Ac100ClientSensor._unrecorded_attributes


# Presence


async def test_an_absent_client_is_disconnected_and_its_readings_are_unavailable(
    hass, api, ac_snapshot
):
    coordinator = await setup(hass, make_entry(hass))
    await publish(hass, coordinator, replace(ac_snapshot, clients=(BARE,)))
    assert client_device(hass, PHONE.mac) is not None
    assert states(hass, PHONE) == {
        key: STATE_OFF if key == "connectivity" else STATE_UNAVAILABLE for key in PHONE_STATES
    }
    for state in page(hass, PHONE).values():
        assert "radios" not in state.attributes

    await publish(hass, coordinator, ac_snapshot)
    assert states(hass, PHONE) == PHONE_STATES


async def test_a_failed_poll_makes_every_reading_unavailable_including_connectivity(
    hass, api, ac_snapshot
):
    coordinator = await setup(hass, make_entry(hass))
    coordinator.async_set_update_error(UpdateFailed("simulated controller outage"))
    await hass.async_block_till_done()
    for client in (PHONE, BARE):
        assert set(states(hass, client).values()) == {STATE_UNAVAILABLE}

    await publish(hass, coordinator, ac_snapshot)
    assert states(hass, PHONE) == PHONE_STATES
    assert states(hass, BARE) == BARE_STATES


async def test_a_client_that_appears_later_gets_its_page(hass, api, ac_snapshot, entity_registry):
    entry = make_entry(hass)
    coordinator = await setup(hass, entry)
    newcomer = wifi_client(3, "New tablet", radios=(ClientRadio("2", "5GHz", -60, 433.0),))
    await publish(hass, coordinator, replace(ac_snapshot, clients=(*ac_snapshot.clients, newcomer)))
    readings = states(hass, newcomer)
    assert set(readings) == set(PHONE_STATES)
    assert readings["connectivity"] == STATE_ON
    assert (readings["wifi_band"], readings["signal_strength"]) == ("5GHz", "-60")
    assert (
        entity_registry.async_get(reading_id(entity_registry, newcomer, "ssid")).disabled_by is None
    )
    assert states(hass, PHONE) == PHONE_STATES


async def test_a_restored_client_has_a_page_while_it_is_away(
    hass, api, ac_snapshot, entity_registry
):
    """A tracker from an earlier run is enough: its client gets readings, unavailable at first."""
    entry = make_entry(hass)
    away = wifi_client(7, "Away phone")
    entity_registry.async_get_or_create(
        "device_tracker",
        DOMAIN,
        away.mac,
        config_entry=entry,
        disabled_by=er.RegistryEntryDisabler.INTEGRATION,
    )
    coordinator = await setup(hass, entry)
    assert states(hass, away) == {
        key: STATE_OFF if key == "connectivity" else STATE_UNAVAILABLE for key in PHONE_STATES
    }

    returned = replace(away, radios=(ClientRadio("1", "2.4GHz", -70, 54.0),))
    await publish(hass, coordinator, replace(ac_snapshot, clients=(*ac_snapshot.clients, returned)))
    readings = states(hass, away)
    assert (readings["connectivity"], readings["signal_strength"]) == (STATE_ON, "-70")


async def test_an_upgrade_adds_the_readings_to_the_existing_client_device(
    hass, api, entity_registry, device_registry
):
    """Version 0.2.1 left a disabled tracker on a device that the user may have renamed."""
    entry = make_entry(hass)
    connection = (dr.CONNECTION_NETWORK_MAC, PHONE.mac)
    old = device_registry.async_get_or_create(
        config_entry_id=entry.entry_id, connections={connection}, name=PHONE.name
    )
    device_registry.async_update_device(old.id, name_by_user="Alice phone")
    tracker = entity_registry.async_get_or_create(
        "device_tracker",
        DOMAIN,
        PHONE.mac,
        config_entry=entry,
        device_id=old.id,
        disabled_by=er.RegistryEntryDisabler.INTEGRATION,
    )
    await setup(hass, entry)
    device = client_device(hass, PHONE.mac)
    assert device.id == old.id
    assert device.name_by_user == "Alice phone"
    restored = entity_registry.async_get(tracker.entity_id)
    assert restored is not None
    assert (restored.id, restored.entity_id, restored.unique_id, restored.device_id) == (
        tracker.id,
        tracker.entity_id,
        tracker.unique_id,
        old.id,
    )
    assert restored.disabled_by is er.RegistryEntryDisabler.INTEGRATION
    assert states(hass, PHONE) == PHONE_STATES
    assert reading_id(entity_registry, PHONE, "ssid") == "sensor.alice_phone_ssid"
    macs = [
        value
        for other in dr.async_entries_for_config_entry(device_registry, entry.entry_id)
        for kind, value in other.connections
        if kind == dr.CONNECTION_NETWORK_MAC
    ]
    assert macs.count(PHONE.mac) == 1


async def test_roaming_and_renaming_update_the_readings_without_new_entities(
    hass, api, ac_snapshot
):
    entry = make_entry(hass)
    coordinator = await setup(hass, entry)
    before = entry_rows(hass, entry)
    roamed = replace(
        PHONE,
        name="Renamed phone",
        ip="192.0.2.99",
        ssid="Example 5 GHz",
        ap_name="Study",
        radios=(ClientRadio("2", "5GHz", -48, 866.0),),
        connected_at="2026/11/16 11:00:00",
    )
    await publish(hass, coordinator, replace(ac_snapshot, clients=(roamed, BARE)))
    assert states(hass, PHONE) == PHONE_STATES | {
        "ipv4_address": "192.0.2.99",
        "ssid": "Example 5 GHz",
        "access_point": "Study",
        "wifi_band": "5GHz",
        "radio_unit": "2 (5GHz)",
        "signal_strength": "-48",
        "link_rate": "866.0",
        "connection_time": "2026/11/16 11:00:00",
    }
    assert entry_rows(hass, entry) == before


async def test_a_reading_disabled_by_the_user_stays_disabled_through_a_reload(
    hass, api, entity_registry
):
    entry = make_entry(hass)
    await setup(hass, entry)
    entity_id = reading_id(entity_registry, PHONE, "ipv4_address")
    entity_registry.async_update_entity(entity_id, disabled_by=er.RegistryEntryDisabler.USER)
    await hass.async_block_till_done()
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert entity_registry.async_get(entity_id).disabled_by is er.RegistryEntryDisabler.USER
    assert hass.states.get(entity_id) is None
    assert states(hass, PHONE) == {k: v for k, v in PHONE_STATES.items() if k != "ipv4_address"}
    assert states(hass, BARE) == {k: v for k, v in BARE_STATES.items() if k != "ipv4_address"} | {
        "ipv4_address": STATE_UNKNOWN
    }


# Devices and entities


async def test_every_reading_belongs_to_the_device_of_its_client_only(
    hass, api, ac_snapshot, device_registry
):
    entry = make_entry(hass)
    await setup(hass, entry)
    client_rows = [
        row
        for row in er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)
        if "_client_" in row.unique_id and row.domain != "device_tracker"
    ]
    assert len(client_rows) == 2 * len(PHONE_STATES)
    for row in client_rows:
        mac = row.unique_id.split("_client_")[1].split("_")[0]
        device = device_registry.async_get(row.device_id)
        assert device.connections == {(dr.CONNECTION_NETWORK_MAC, mac)}
        assert not device.identifiers


async def test_readings_never_duplicate_devices(hass, api, ac_snapshot, device_registry):
    entry = make_entry(hass)
    coordinator = await setup(hass, entry)

    def devices() -> list[dr.DeviceEntry]:
        return dr.async_entries_for_config_entry(device_registry, entry.entry_id)

    assert len(devices()) == 1 + len(ac_snapshot.aps) + len(ac_snapshot.clients)
    newcomer = wifi_client(3, "New tablet")
    grown = replace(ac_snapshot, clients=(*ac_snapshot.clients, newcomer))
    await publish(hass, coordinator, grown)
    await publish(hass, coordinator, replace(grown, clients=(PHONE,)))
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    macs = [
        value
        for device in devices()
        for kind, value in device.connections
        if kind == dr.CONNECTION_NETWORK_MAC
    ]
    assert len(devices()) == 1 + len(ac_snapshot.aps) + 3
    assert len(macs) == len(set(macs))


async def test_unload_removes_the_discovery_listeners(hass, api, ac_snapshot, entity_registry):
    entry = make_entry(hass)
    coordinator = await setup(hass, entry)
    assert await hass.config_entries.async_unload(entry.entry_id)
    assert not coordinator._listeners
    newcomer = wifi_client(3, "New tablet")
    coordinator.async_set_updated_data(
        replace(ac_snapshot, clients=(*ac_snapshot.clients, newcomer))
    )
    await hass.async_block_till_done()
    assert (
        entity_registry.async_get_entity_id("sensor", DOMAIN, unique_id(newcomer, "ssid")) is None
    )


async def test_readings_add_no_request_and_no_private_values_to_diagnostics(hass, api, ac_snapshot):
    entry = make_entry(hass)
    await setup(hass, entry)
    api.async_get_snapshot.assert_awaited_once_with()
    assert {call[0] for call in api.mock_calls} <= {"async_get_snapshot", "async_get_monitoring"}
    encoded = json.dumps(await async_get_config_entry_diagnostics(hass, entry))
    private = [PHONE.mac, PHONE.ip, PHONE.ipv6, PHONE.ssid, PHONE.name, PHONE.connected_at]
    private += ["apple", "Apple", "ios", "iOS"]
    assert not [value for value in private if value in encoded]


# A populated page from the fixtures


class Response:
    """Small aiohttp context-manager double; it never opens a network socket."""

    def __init__(self, data):
        self.status = 200
        self.body = json.dumps(data)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def text(self):
        return self.body


def fixture_snapshot_responses():
    """The anonymized controller answers of one poll, with one client given every detail."""
    second_page = json.loads((FIXTURES / "clients_page2.json").read_text())
    row = next(
        row
        for wrapper in second_page["apmng_client"]["client_list"]
        for row in wrapper.values()
        if row["mac"] == "02-00-00-00-00-11"
    )
    row.update(operating_system="ios", vlan_id="27", ipv6="2001%3adb8%3a%3a25")
    answers = {
        name: json.loads((FIXTURES / name).read_text())
        for name in (
            "controller.json",
            "identity.json",
            "aps.json",
            "clients.json",
            "hosts.json",
            "hosts_page2.json",
            "radio_bind_list.json",
        )
    }
    ordered = [
        answers["controller.json"],
        answers["identity.json"],
        answers["aps.json"],
        answers["clients.json"],
        second_page,
        answers["hosts.json"],
        answers["hosts_page2.json"],
        answers["radio_bind_list.json"],
    ]
    return [
        Response({"error_code": 0, "encrypt_type": ["-3"], "username_type": "one_username"}),
        Response({"error_code": 0, "stok": "synthetic-session", "role": "sys_admin"}),
        *map(Response, ordered),
    ]


FIXTURE_CONTROLLER_MAC = "02:00:00:00:00:15"


async def load_fixture_entry(hass):
    """Load one entry through the real API over the anonymized fixture answers of one poll."""
    entry = make_entry(hass, FIXTURE_CONTROLLER_MAC)
    session = MagicMock(spec=aiohttp.ClientSession)
    session.post.side_effect = fixture_snapshot_responses()
    live = Ac100Api(session, "controller.example", "admin", "example-password")
    live.async_get_monitoring = AsyncMock(return_value=MonitoringState())
    live.async_get_configuration = AsyncMock(return_value=ConfigurationState())
    live.async_logout = AsyncMock()
    with (
        patch("custom_components.tplink_ac100.Ac100Api", return_value=live),
        patch("custom_components.tplink_ac100.PLATFORMS", PLATFORMS),
    ):
        coordinator = await setup(hass, entry)
    return entry, coordinator


async def test_fixture_clients_fill_their_pages_from_the_wire_format(hass, entity_registry):
    controller_mac = FIXTURE_CONTROLLER_MAC
    entry, coordinator = await load_fixture_entry(hass)

    clients = {client.mac[-2:]: client for client in coordinator.data.clients}
    assert len(clients) == 14
    for client in clients.values():
        assert set(states(hass, client, controller_mac)) == set(PHONE_STATES)
    assert states(hass, clients["11"], controller_mac) == {
        "connectivity": STATE_ON,
        "signal_strength": "-71",
        "link_rate": "737.0",
        "wifi_upload_speed": "4.0",
        "wifi_download_speed": "20.0",
        "ipv4_address": "192.0.2.25",
        "ipv6_address": "2001:db8::25",
        "ssid": "Example WiFi 3",
        "ssid_encoding": "UTF-8",
        "access_point": "Example AP 3",
        "wifi_band": "5GHz",
        "radio_unit": "2 (5GHz)",
        "vlan_id": "27",
        "vendor": "Apple",
        "operating_system": "iOS",
        "connection_time": "2026/11/15 03:50:03",
    }
    prefix = unique_id(clients["11"], "", controller_mac)
    sections: dict[EntityCategory | None, set[str]] = {}
    for row in er.async_entries_for_device(
        entity_registry, client_device(hass, clients["11"].mac).id
    ):
        if row.unique_id.startswith(prefix):
            sections.setdefault(row.entity_category, set()).add(row.unique_id.removeprefix(prefix))
    assert sections == {
        None: {"connectivity", "signal_strength", "link_rate"}
        | {"wifi_upload_speed", "wifi_download_speed"},
        EntityCategory.DIAGNOSTIC: {"ipv4_address", "ipv6_address", "ssid", "ssid_encoding"}
        | {"access_point", "wifi_band", "radio_unit", "vlan_id", "vendor", "operating_system"}
        | {"connection_time"},
    }
    windows = states(hass, clients["10"], controller_mac)
    assert (windows["vendor"], windows["operating_system"]) == ("Others", "Windows")
    assert windows["vlan_id"] == STATE_UNKNOWN
    assert windows["ipv6_address"] == STATE_UNKNOWN
    idle = states(hass, clients["0e"], controller_mac)
    assert (idle["wifi_upload_speed"], idle["wifi_download_speed"]) == (STATE_UNKNOWN,) * 2
    trackers = [
        row
        for row in er.async_entries_for_config_entry(entity_registry, entry.entry_id)
        if row.domain == "device_tracker"
    ]
    # The 14 clients, and the one saved record of the host table whose terminal is not listed.
    assert len(trackers) == 15
    assert all(row.disabled_by is er.RegistryEntryDisabler.INTEGRATION for row in trackers)


async def test_fixture_saved_records_fill_the_terminal_pages_from_the_wire_format(
    hass, entity_registry
):
    entry, coordinator = await load_fixture_entry(hass)
    records = {item.mac[-2:]: item for item in coordinator.data.terminals.configurations}
    assert len(records) == 15
    saved = records["11"]
    assert saved.binding_available
    assert (saved.rate_limit_enabled, saved.binding_enabled, saved.force_binding) == (False,) * 3
    assert (saved.upload_limit, saved.download_limit) == (0, 0)
    assert (saved.bound_ap, saved.bound_radio, saved.bound_target) == (None, None, None)

    def reading(mac: str, key: str) -> str:
        domain = "binary_sensor" if key.endswith(("_enabled", "_binding")) else "sensor"
        unique = f"{FIXTURE_CONTROLLER_MAC}_terminal_{mac}_{key}"
        entity_id = entity_registry.async_get_entity_id(domain, DOMAIN, unique)
        assert entity_id is not None, unique
        return hass.states.get(entity_id).state

    present = "02:00:00:00:00:11"
    assert {
        key: reading(present, key)
        for key in ("upload_limit", "download_limit", "bound_access_point", "bound_radio")
        + ("rate_limit_enabled", "binding_enabled", "force_binding", "name")
    } == {
        "upload_limit": "0",
        "download_limit": "0",
        "bound_access_point": STATE_UNKNOWN,
        "bound_radio": STATE_UNKNOWN,
        "rate_limit_enabled": STATE_OFF,
        "binding_enabled": STATE_OFF,
        "force_binding": STATE_OFF,
        "name": STATE_UNKNOWN,
    }
    # The saved record of a terminal that the client list does not show has a page of its own.
    retained = "02:00:00:00:00:30"
    assert retained not in {client.mac for client in coordinator.data.clients}
    assert client_device(hass, retained) is not None
    assert reading(retained, "rate_limit_enabled") == STATE_OFF
    connectivity = entity_registry.async_get_entity_id(
        "binary_sensor", DOMAIN, f"{FIXTURE_CONTROLLER_MAC}_client_{retained}_connectivity"
    )
    assert hass.states.get(connectivity).state == STATE_OFF
    # The binding choices come from the radio list: the AP's device name tells the two APs apart
    # that the controller named alike, and an AP with one radio offers no all-radios choice.
    labels = binding_target_labels(coordinator, saved.binding_targets)
    assert list(labels.values()) == [
        "Example AP 1 - All radios",
        "Example AP 1 - 2.4GHz",
        "Example AP 1 - 5GHz",
        "Example AP 3 000003 - All radios",
        "Example AP 3 000003 - 2.4GHz",
        "Example AP 3 000003 - 5GHz",
        "Example AP 4 - 2.4GHz",
        "Example AP 5 - 2.4GHz",
    ]
    assert list(labels) == [
        f"02:00:00:00:00:{number:02x}/{radio}"
        for number, radio in (
            (1, "all"),
            (1, "1"),
            (1, "2"),
            (3, "all"),
            (3, "1"),
            (3, "2"),
            (4, "1"),
            (6, "1"),
        )
    ]


# Translations


def test_every_client_reading_has_an_english_name_and_a_known_icon_key():
    strings = json.loads((COMPONENT / "strings.json").read_text())
    icons = json.loads((COMPONENT / "icons.json").read_text())
    assert json.loads((COMPONENT / "translations/en.json").read_text()) == strings
    names = strings["entity"]["sensor"]
    for description in CLIENT_SENSORS:
        assert names[description.translation_key]["name"], description.key
        assert not re.search(r"{\w+}", names[description.translation_key]["name"])
    assert strings["entity"]["binary_sensor"]["connectivity"]["name"] == "Connectivity"
    client_icons = {key for key in icons["entity"]["sensor"] if key.startswith("client_")}
    assert client_icons <= set(names)
