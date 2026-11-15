"""Device names, links and metadata, and device-less client trackers, on the HA runtime."""

from dataclasses import replace
from unittest.mock import AsyncMock, patch

import attr
import pytest
from homeassistant.const import STATE_HOME, STATE_NOT_HOME, Platform
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import label_registry as lr
from pytest_homeassistant.common import MockConfigEntry, async_capture_events

from custom_components.tplink_ac100.api import Ac100Api, Ac100ConnectionError
from custom_components.tplink_ac100.const import DOMAIN
from custom_components.tplink_ac100.device_info import ap_device_names
from custom_components.tplink_ac100.models import AccessPoint, Client, Controller, Snapshot
from custom_components.tplink_ac100.monitoring import (
    ApFirmware,
    ConfigurationState,
    MonitoringState,
)

pytestmark = pytest.mark.usefixtures("enable_custom_integrations")

CONTROLLER_MAC = "02:00:00:00:00:01"
MODEL = "TL-XAP1502GI-PoE"
FIRMWARE_SOURCE = "apmng_upgrade.ap_list"


def access_point(index: int, name: str) -> AccessPoint:
    return AccessPoint(
        str(index), name, MODEL, f"02:00:00:aa:bb:{index:02x}", f"192.0.2.{index + 10}", "1", True,
        600, (),
    )  # fmt: skip


def wifi_client(index: int, name: str | None) -> Client:
    return Client(
        str(index), f"02:00:00:cc:dd:{index:02x}", name, f"192.0.2.{index + 50}", "",
        "Example wireless", "Garage", (),
    )  # fmt: skip


@pytest.fixture
def ac_snapshot() -> Snapshot:
    return Snapshot(
        Controller(CONTROLLER_MAC, "TL-AC100", "5.0", "1.0.5", {}, 30.0, 3600),
        (access_point(1, "Garage"),),
        (wifi_client(1, "Example phone"), wifi_client(2, None)),
    )


@pytest.fixture
def api(ac_snapshot):
    """Simulate the controller; only the binary sensor and tracker platforms are loaded."""
    api = AsyncMock(spec=Ac100Api)
    api.async_get_snapshot.return_value = ac_snapshot
    api.async_get_monitoring.return_value = MonitoringState()
    api.async_get_configuration.return_value = ConfigurationState()
    with (
        patch("custom_components.tplink_ac100.Ac100Api", return_value=api),
        patch(
            "custom_components.tplink_ac100.PLATFORMS",
            [Platform.BINARY_SENSOR, Platform.DEVICE_TRACKER],
        ),
    ):
        yield api


def make_entry(hass, host: str = "http://192.0.2.1") -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=CONTROLLER_MAC,
        data={"host": host, "username": "example", "password": "example-only"},
    )
    entry.add_to_hass(hass)
    return entry


async def setup(hass, entry):
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry.runtime_data


def device_with_identifier(hass, mac: str) -> dr.DeviceEntry | None:
    entry = hass.config_entries.async_entries(DOMAIN)[0]
    return dr.async_get(hass).async_get_device_by_identifier((DOMAIN, mac), entry.entry_id)


def controller_device(hass) -> dr.DeviceEntry:
    return device_with_identifier(hass, CONTROLLER_MAC)


def ap_device(hass, ap: AccessPoint) -> dr.DeviceEntry:
    return device_with_identifier(hass, ap.mac)


def versions(device: dr.DeviceEntry) -> tuple[str | None, str | None]:
    return device.sw_version, device.hw_version


# Controller device


@pytest.mark.parametrize(
    ("host", "url"),
    [
        ("http://192.0.2.1", "http://192.0.2.1"),
        ("192.0.2.1:8080", "http://192.0.2.1:8080"),
        ("https://ac.example.test:8443/", "https://ac.example.test:8443"),
    ],
)
async def test_controller_device_is_named_by_model_and_links_to_its_web_page(
    hass, api, ac_snapshot, host, url
):
    await setup(hass, make_entry(hass, host))
    device = controller_device(hass)
    assert device.name == "TL-AC100"
    assert (device.manufacturer, device.model) == ("TP-Link", "TL-AC100")
    assert device.configuration_url == url
    assert versions(device) == ("1.0.5", "5.0")
    assert device.connections == {(dr.CONNECTION_NETWORK_MAC, CONTROLLER_MAC)}


async def test_existing_controller_device_keeps_identity_and_user_name(hass, api, device_registry):
    entry = make_entry(hass)
    old = device_registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, CONTROLLER_MAC)},
        connections={(dr.CONNECTION_NETWORK_MAC, CONTROLLER_MAC)},
        manufacturer="TP-Link",
        model="TL-AC100",
        name="TP-Link TL-AC100",
        sw_version="1.0.1",
        hw_version="5.0",
    )
    device_registry.async_update_device(old.id, name_by_user="Main controller")
    await setup(hass, entry)
    device = controller_device(hass)
    assert device.id == old.id
    assert device.name == "TL-AC100"
    assert device.name_by_user == "Main controller"
    assert device.identifiers == {(DOMAIN, CONTROLLER_MAC)}
    assert versions(device) == ("1.0.5", "5.0")


async def test_controller_metadata_follows_successful_polls_only(hass, api, ac_snapshot):
    coordinator = await setup(hass, make_entry(hass))
    newer = replace(
        ac_snapshot,
        controller=replace(
            ac_snapshot.controller, firmware_version="1.0.6", hardware_version="5.1"
        ),
    )
    api.async_get_snapshot.return_value = newer
    await coordinator.async_refresh()
    assert versions(controller_device(hass)) == ("1.0.6", "5.1")

    dr.async_get(hass).async_update_device(controller_device(hass).id, sw_version="edited")
    api.async_get_snapshot.side_effect = Ac100ConnectionError("Unavailable")
    await coordinator.async_refresh()
    assert not coordinator.last_update_success
    assert controller_device(hass).sw_version == "edited"
    api.async_get_snapshot.side_effect = None
    await coordinator.async_refresh()
    assert controller_device(hass).sw_version == "1.0.6"


async def test_unchanged_data_leaves_the_device_registry_alone(hass, api, ac_snapshot):
    coordinator = await setup(hass, make_entry(hass))
    events = async_capture_events(hass, dr.EVENT_DEVICE_REGISTRY_UPDATED)
    coordinator.async_update_listeners()
    await hass.async_block_till_done()
    assert not events


# Access point devices


@pytest.mark.parametrize(
    ("aps", "expected"),
    [
        pytest.param([("01", "Garage")], ["Garage"], id="unique name is kept"),
        pytest.param([("01", "")], [f"{MODEL} AABB01"], id="blank name uses model and MAC"),
        pytest.param([("01", "  ")], [f"{MODEL} AABB01"], id="whitespace is blank"),
        pytest.param(
            [("01", "Hall"), ("02", "HALL"), ("03", "Garage")],
            ["Hall AABB01", "HALL AABB02", "Garage"],
            id="duplicates ignore case",
        ),
        pytest.param(
            [("01", " Hall "), ("02", "hall")],
            ["Hall AABB01", "hall AABB02"],
            id="duplicates ignore padding",
        ),
        pytest.param(
            [("01", ""), ("02", "")],
            [f"{MODEL} AABB01", f"{MODEL} AABB02"],
            id="blank names are already distinct",
        ),
    ],
)
def test_ap_device_names(aps, expected):
    items = [access_point(int(suffix), name) for suffix, name in aps]
    names = ap_device_names(items)
    assert [names[item.mac] for item in items] == expected
    assert ap_device_names(reversed(items)) == names


async def test_ap_devices_are_readable_and_distinct(hass, api, ac_snapshot):
    aps = (
        access_point(1, "Hall"),
        access_point(2, "hall"),
        access_point(3, ""),
        access_point(4, "Garage"),
    )
    api.async_get_snapshot.return_value = replace(ac_snapshot, aps=aps)
    await setup(hass, make_entry(hass))
    assert [ap_device(hass, ap).name for ap in aps] == [
        "Hall AABB01",
        "hall AABB02",
        f"{MODEL} AABB03",
        "Garage",
    ]


async def test_ap_device_keeps_mac_identity_and_links_to_the_controller(hass, api, ac_snapshot):
    ap = ac_snapshot.aps[0]
    await setup(hass, make_entry(hass))
    device = ap_device(hass, ap)
    assert device.identifiers == {(DOMAIN, ap.mac)}
    assert device.connections == {(dr.CONNECTION_NETWORK_MAC, ap.mac)}
    assert device.via_device_id == controller_device(hass).id
    assert (device.manufacturer, device.model) == ("TP-Link", MODEL)
    assert device.configuration_url == "http://192.0.2.1"
    assert versions(device) == (None, None)


async def test_ap_names_follow_later_polls_without_changing_ids(
    hass, api, ac_snapshot, entity_registry, device_registry
):
    coordinator = await setup(hass, make_entry(hass))
    ap = ac_snapshot.aps[0]
    unique_id = f"{CONTROLLER_MAC}_{ap.mac}_connectivity"
    entity_id = entity_registry.async_get_entity_id("binary_sensor", DOMAIN, unique_id)
    assert entity_id == "binary_sensor.garage_connectivity"
    device_registry.async_update_device(ap_device(hass, ap).id, name_by_user="Back door AP")

    namesake = access_point(2, "GARAGE")
    api.async_get_snapshot.return_value = replace(ac_snapshot, aps=(ap, namesake))
    await coordinator.async_refresh()
    assert ap_device(hass, ap).name == "Garage AABB01"
    assert ap_device(hass, ap).name_by_user == "Back door AP"
    assert ap_device(hass, namesake).name == "GARAGE AABB02"

    api.async_get_snapshot.return_value = replace(
        ac_snapshot, aps=(replace(ap, name="Garage annex"),)
    )
    await coordinator.async_refresh()
    assert ap_device(hass, ap).name == "Garage annex"
    assert entity_registry.async_get_entity_id("binary_sensor", DOMAIN, unique_id) == entity_id


def firmware_monitoring(*items: ApFirmware, sections=frozenset({FIRMWARE_SOURCE})):
    return MonitoringState(available_sections=sections, ap_firmware=items)


async def test_ap_firmware_metadata_comes_only_from_a_current_source(hass, api, ac_snapshot):
    ap = ac_snapshot.aps[0]
    api.async_get_monitoring.return_value = firmware_monitoring(ApFirmware(ap.mac, "1.0.4", "2.0"))
    coordinator = await setup(hass, make_entry(hass))
    assert versions(ap_device(hass, ap)) == ("1.0.4", "2.0")

    async def publish(monitoring: MonitoringState | None) -> tuple[str | None, str | None]:
        coordinator.async_set_updated_data(replace(coordinator.data, monitoring=monitoring))
        await hass.async_block_till_done()
        return versions(ap_device(hass, ap))

    assert await publish(firmware_monitoring(ApFirmware(ap.mac, "1.0.5", "2.1"))) == (
        "1.0.5",
        "2.1",
    )
    assert await publish(None) == ("1.0.5", "2.1")
    assert await publish(firmware_monitoring()) == ("1.0.5", "2.1")
    assert await publish(firmware_monitoring(ApFirmware("02:00:00:00:00:ff", "7.0", "7.0"))) == (
        "1.0.5",
        "2.1",
    )
    assert await publish(firmware_monitoring(ApFirmware(ap.mac, "1.0.6", None))) == (
        "1.0.6",
        "2.1",
    )


# Client trackers


def trackers(hass, entry) -> list[er.RegistryEntry]:
    return [
        entity
        for entity in er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)
        if entity.domain == "device_tracker"
    ]


async def test_new_trackers_have_no_device_and_keep_bare_mac_ids(
    hass, api, ac_snapshot, device_registry
):
    entry = make_entry(hass)
    await setup(hass, entry)
    rows = trackers(hass, entry)
    assert {row.unique_id for row in rows} == {client.mac for client in ac_snapshot.clients}
    assert all(row.device_id is None for row in rows)
    assert all(row.disabled_by is er.RegistryEntryDisabler.INTEGRATION for row in rows)
    devices = dr.async_entries_for_config_entry(device_registry, entry.entry_id)
    assert {device.name for device in devices} == {"TL-AC100", "Garage"}


@pytest.mark.usefixtures("entity_registry_enabled_by_default")
async def test_tracker_names_follow_the_client_and_survive_its_absence(
    hass, api, ac_snapshot, entity_registry
):
    coordinator = await setup(hass, make_entry(hass))
    named, unnamed = ac_snapshot.clients
    named_id = entity_registry.async_get_entity_id("device_tracker", DOMAIN, named.mac)
    unnamed_id = entity_registry.async_get_entity_id("device_tracker", DOMAIN, unnamed.mac)
    assert named_id == "device_tracker.example_phone"
    assert unnamed_id == "device_tracker.02_00_00_cc_dd_02"
    assert hass.states.get(named_id).state == STATE_HOME
    assert hass.states.get(named_id).attributes["friendly_name"] == "Example phone"
    assert hass.states.get(unnamed_id).attributes["friendly_name"] == unnamed.mac

    renamed = replace(named, name="Alice phone")
    coordinator.async_set_updated_data(replace(ac_snapshot, clients=(renamed, unnamed)))
    await hass.async_block_till_done()
    assert hass.states.get(named_id).attributes["friendly_name"] == "Alice phone"

    coordinator.async_set_updated_data(replace(ac_snapshot, clients=()))
    await hass.async_block_till_done()
    away = hass.states.get(named_id)
    assert away.state == STATE_NOT_HOME
    assert away.attributes["friendly_name"] == "Alice phone"
    assert hass.states.get(unnamed_id).attributes["friendly_name"] == unnamed.mac


@pytest.mark.usefixtures("entity_registry_enabled_by_default")
async def test_absent_tracker_keeps_its_last_name_after_a_reload(
    hass, api, ac_snapshot, entity_registry
):
    entry = make_entry(hass)
    await setup(hass, entry)
    entity_id = entity_registry.async_get_entity_id(
        "device_tracker", DOMAIN, ac_snapshot.clients[0].mac
    )
    assert await hass.config_entries.async_unload(entry.entry_id)
    api.async_get_snapshot.return_value = replace(ac_snapshot, clients=())
    await setup(hass, entry)
    state = hass.states.get(entity_id)
    assert state.state == STATE_NOT_HOME
    assert state.attributes["friendly_name"] == "Example phone"


# Existing tracker-only devices

OLD_MAC = "02:00:00:ee:ee:01"


def add_old_tracker(
    hass, entry, *, enabled: bool = False
) -> tuple[dr.DeviceEntry, er.RegistryEntry]:
    """Create what earlier versions did: a MAC-only device holding one tracker."""
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id,
        connections={(dr.CONNECTION_NETWORK_MAC, OLD_MAC)},
        name="Old phone",
    )
    tracker = er.async_get(hass).async_get_or_create(
        "device_tracker",
        DOMAIN,
        OLD_MAC,
        config_entry=entry,
        device_id=device.id,
        disabled_by=None if enabled else er.RegistryEntryDisabler.INTEGRATION,
        has_entity_name=True,
    )
    return device, tracker


async def test_unused_tracker_device_is_released_and_other_integrations_are_untouched(
    hass, api, device_registry, entity_registry
):
    entry = make_entry(hass)
    device, tracker = add_old_tracker(hass, entry)
    other_entry = MockConfigEntry(domain="other_integration")
    other_entry.add_to_hass(hass)
    other_device = device_registry.async_get_or_create(
        config_entry_id=other_entry.entry_id,
        connections={(dr.CONNECTION_NETWORK_MAC, OLD_MAC)},
        name="Other phone",
    )
    other_tracker = entity_registry.async_get_or_create(
        "device_tracker",
        "other_integration",
        OLD_MAC,
        config_entry=other_entry,
        device_id=other_device.id,
    )

    for _ in range(2):
        await setup(hass, entry)
        kept = entity_registry.async_get(tracker.entity_id)
        assert kept.id == tracker.id
        assert kept.unique_id == OLD_MAC
        assert kept.disabled_by is er.RegistryEntryDisabler.INTEGRATION
        assert kept.device_id is None
        assert kept.original_name == "Old phone"
        assert device_registry.async_get(device.id) is None
        assert entity_registry.async_get(other_tracker.entity_id).device_id == other_device.id
        assert device_registry.async_get(other_device.id).name == "Other phone"
        assert await hass.config_entries.async_unload(entry.entry_id)


def rename(hass, device, tracker):
    dr.async_get(hass).async_update_device(device.id, name_by_user="Alice phone")


def place_in_area(hass, device, tracker):
    area = ar.async_get(hass).async_create("Office")
    dr.async_get(hass).async_update_device(device.id, area_id=area.id)


def add_label(hass, device, tracker):
    label = lr.async_get(hass).async_create("Keep")
    dr.async_get(hass).async_update_device(device.id, labels={label.label_id})


def disable_device(hass, device, tracker):
    dr.async_get(hass).async_update_device(device.id, disabled_by=dr.DeviceEntryDisabler.USER)


def enable_tracker(hass, device, tracker):
    er.async_get(hass).async_update_entity(tracker.entity_id, disabled_by=None)


def add_other_entity(hass, device, tracker):
    other_entry = MockConfigEntry(domain="other_integration")
    other_entry.add_to_hass(hass)
    er.async_get(hass).async_get_or_create(
        "sensor", "other_integration", "other", config_entry=other_entry, device_id=device.id
    )


def add_identifier(hass, device, tracker):
    dr.async_get(hass).async_update_device(device.id, new_identifiers={(DOMAIN, "an-access-point")})


def add_connection(hass, device, tracker):
    connections = {
        (dr.CONNECTION_NETWORK_MAC, OLD_MAC),
        (dr.CONNECTION_BLUETOOTH, "AA:BB:CC:DD:EE:FF"),
    }
    dr.async_get(hass).async_update_device(device.id, new_connections=connections)


def mark_split_from_a_shared_device(hass, device, tracker):
    # Home Assistant has no public way to create the leftover of a pre-migration shared device.
    registry = dr.async_get(hass)
    registry._devices[device.id] = attr.evolve(device, composite_device_id="shared-device-id")


@pytest.mark.parametrize(
    "change",
    [
        rename,
        place_in_area,
        add_label,
        disable_device,
        enable_tracker,
        add_other_entity,
        add_identifier,
        add_connection,
        mark_split_from_a_shared_device,
    ],
)
async def test_tracker_devices_with_user_choices_or_other_users_are_kept(
    hass, api, device_registry, entity_registry, change
):
    def attached_entities(device):
        return {
            row.entity_id
            for row in er.async_entries_for_device(
                entity_registry, device.id, include_disabled_entities=True
            )
        }

    entry = make_entry(hass)
    device, tracker = add_old_tracker(hass, entry)
    change(hass, device, tracker)
    attached = attached_entities(device)
    await setup(hass, entry)
    assert device_registry.async_get(device.id) is not None
    assert entity_registry.async_get(tracker.entity_id).device_id == device.id
    assert attached_entities(device) == attached


async def test_tracker_on_another_integrations_device_never_removes_that_device(
    hass, api, device_registry, entity_registry
):
    entry = make_entry(hass)
    other_entry = MockConfigEntry(domain="other_integration")
    other_entry.add_to_hass(hass)
    other_device = device_registry.async_get_or_create(
        config_entry_id=other_entry.entry_id,
        connections={(dr.CONNECTION_NETWORK_MAC, OLD_MAC)},
        name="Other phone",
    )
    entity_registry.async_get_or_create(
        "device_tracker",
        DOMAIN,
        OLD_MAC,
        config_entry=entry,
        device_id=other_device.id,
        disabled_by=er.RegistryEntryDisabler.INTEGRATION,
    )
    await setup(hass, entry)
    assert device_registry.async_get(other_device.id).name == "Other phone"
