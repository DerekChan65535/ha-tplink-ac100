"""The device tree controller -> access point -> client on the actual HA registries."""

from dataclasses import replace
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.const import STATE_HOME, Platform
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant.common import MockConfigEntry, async_capture_events

from custom_components.tplink_ac100 import async_remove_config_entry_device
from custom_components.tplink_ac100.api import Ac100Api, Ac100ConnectionError
from custom_components.tplink_ac100.client_entity import Ac100ClientEntity, known_client_macs
from custom_components.tplink_ac100.const import DOMAIN
from custom_components.tplink_ac100.models import AccessPoint, Client, Controller, Snapshot
from custom_components.tplink_ac100.monitoring import ConfigurationState, MonitoringState
from custom_components.tplink_ac100.terminal import TerminalConfiguration, TerminalState

pytestmark = pytest.mark.usefixtures("enable_custom_integrations")

CONTROLLER_MAC = "02:00:00:00:00:01"
NETWORK_MAC = dr.CONNECTION_NETWORK_MAC
DISABLED = er.RegistryEntryDisabler.INTEGRATION


def ap_mac(index: int) -> str:
    return f"02:00:00:aa:bb:{index:02x}"


def client_mac(index: int) -> str:
    return f"02:00:00:cc:dd:{index:02x}"


def access_point(index: int, name: str, link_status: str = "1") -> AccessPoint:
    return AccessPoint(
        str(index), name, "TL-XAP1502GI-PoE", ap_mac(index), f"192.0.2.{index + 10}", link_status,
        True, 600, (),
    )  # fmt: skip


def wifi_client(index: int, ap_name: str, name: str | None = None) -> Client:
    return Client(
        str(index), client_mac(index), name, f"192.0.2.{index + 50}", "", "Example wireless",
        ap_name, (),
    )  # fmt: skip


@pytest.fixture
def ac_snapshot() -> Snapshot:
    return Snapshot(
        Controller(CONTROLLER_MAC, "TL-AC100", "5.0", "1.0.5", {}, 30.0, 3600),
        (access_point(1, "Garage"), access_point(2, "Study")),
        (
            wifi_client(1, "Garage", "Example phone"),
            wifi_client(2, "Study"),
            wifi_client(3, "Garage"),
        ),
        terminals=TerminalState(),
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


def make_entry(hass, unique_id: str = CONTROLLER_MAC) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=unique_id,
        data={"host": "http://192.0.2.1", "username": "example", "password": "example-only"},
    )
    entry.add_to_hass(hass)
    return entry


async def setup(hass, entry):
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry.runtime_data


async def publish(hass, api, coordinator, snapshot) -> None:
    """Make the next poll return a snapshot and let everything react to it."""
    api.async_get_snapshot.return_value = snapshot
    await coordinator.async_refresh()
    await hass.async_block_till_done()


def entry_devices(hass) -> list[dr.DeviceEntry]:
    entry = hass.config_entries.async_entries(DOMAIN)[0]
    return dr.async_entries_for_config_entry(dr.async_get(hass), entry.entry_id)


def client_device(hass, address: str) -> dr.DeviceEntry | None:
    entry = hass.config_entries.async_entries(DOMAIN)[0]
    return dr.async_get(hass).async_get_device_by_connection((NETWORK_MAC, address), entry.entry_id)


def ap_device(hass, index: int) -> dr.DeviceEntry:
    entry = hass.config_entries.async_entries(DOMAIN)[0]
    return dr.async_get(hass).async_get_device_by_identifier(
        (DOMAIN, ap_mac(index)), entry.entry_id
    )


def named_device(hass, name: str) -> dr.DeviceEntry:
    return next(device for device in entry_devices(hass) if device.name == name)


def tree(hass) -> dict[str, str | None]:
    """Map the name of each device of the entry to the name of the device it is behind."""
    devices = entry_devices(hass)
    names = {device.id: device.name for device in devices}
    return {
        device.name: names[device.via_device_id] if device.via_device_id else None
        for device in devices
    }


def trackers(hass, entry) -> dict[str, er.RegistryEntry]:
    return {
        row.unique_id: row
        for row in er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)
        if row.domain == "device_tracker"
    }


TREE = {
    "TL-AC100": None,
    "Garage": "TL-AC100",
    "Study": "TL-AC100",
    "Example phone": "Garage",
    client_mac(2): "Study",
    client_mac(3): "Garage",
}


async def test_clients_are_behind_their_access_points_and_never_the_controller(
    hass, api, ac_snapshot
):
    entry = make_entry(hass)
    await setup(hass, entry)
    assert tree(hass) == TREE
    controller = named_device(hass, "TL-AC100")
    children = {d.name for d in entry_devices(hass) if d.via_device_id == controller.id}
    assert children == {"Garage", "Study"}
    rows = trackers(hass, entry)
    assert set(rows) == {client.mac for client in ac_snapshot.clients}
    assert all(row.disabled_by is DISABLED for row in rows.values())


@pytest.mark.usefixtures("entity_registry_enabled_by_default")
async def test_enabled_trackers_share_the_same_tree(hass, api, ac_snapshot):
    entry = make_entry(hass)
    await setup(hass, entry)
    assert tree(hass) == TREE
    for client in ac_snapshot.clients:
        row = trackers(hass, entry)[client.mac]
        assert row.device_id == client_device(hass, client.mac).id
        assert hass.states.get(row.entity_id).state == STATE_HOME


async def test_the_tree_needs_no_entity_and_is_built_parents_first(hass, api, ac_snapshot):
    events = async_capture_events(hass, dr.EVENT_DEVICE_REGISTRY_UPDATED)
    with patch("custom_components.tplink_ac100.PLATFORMS", []):
        await setup(hass, make_entry(hass))
    assert tree(hass) == TREE
    created = [
        dr.async_get(hass).async_get(event.data["device_id"])
        for event in events
        if event.data["action"] == "create"
    ]
    assert [device.name for device in created] == [
        "TL-AC100",
        "Garage",
        "Study",
        "Example phone",
        client_mac(2),
        client_mac(3),
    ]
    registered: set[str] = set()
    for device in created:
        assert device.via_device_id is None or device.via_device_id in registered
        registered.add(device.id)


async def test_clients_follow_their_access_point_without_a_reload(hass, api, ac_snapshot):
    entry = make_entry(hass)
    coordinator = await setup(hass, entry)
    phone, *others = ac_snapshot.clients
    original = client_device(hass, phone.mac)
    for ap_name in ("Study", "Garage", "Study"):
        moved = replace(phone, ap_name=ap_name)
        await publish(hass, api, coordinator, replace(ac_snapshot, clients=(moved, *others)))
        assert tree(hass)["Example phone"] == ap_name
        assert client_device(hass, phone.mac).id == original.id
        assert trackers(hass, entry)[phone.mac].device_id == original.id


@pytest.mark.parametrize(
    ("aps", "reported", "parent"),
    [
        pytest.param([(1, "Garage")], "Garage", 1, id="unique name"),
        pytest.param([(1, "Garage"), (2, "Garage")], "Garage", None, id="namesake"),
        pytest.param([(1, "Garage"), (2, "Garage", "0")], "Garage", None, id="offline namesake"),
        pytest.param([(1, "Hall"), (2, "hall")], "Hall", 1, id="names differing by case differ"),
        pytest.param([(1, "Hall"), (2, "hall")], "HALL", None, id="case is kept"),
        pytest.param([(1, "Hall"), (2, "Hall")], "Hall AABB01", None, id="display name"),
        pytest.param([(1, "Garage")], "Garage ", None, id="padding is kept"),
        pytest.param([(1, "Garage")], "Study", None, id="unknown AP"),
        pytest.param([(1, "Garage")], "", None, id="no AP name"),
        pytest.param([(1, "")], "", None, id="blank AP name"),
    ],
)
async def test_the_ap_name_must_belong_to_exactly_one_ap(
    hass, api, ac_snapshot, aps, reported, parent
):
    api.async_get_snapshot.return_value = replace(
        ac_snapshot,
        aps=tuple(access_point(*item) for item in aps),
        clients=(wifi_client(1, reported, "Example phone"),),
    )
    await setup(hass, make_entry(hass))
    expected = ap_device(hass, parent).id if parent else None
    assert client_device(hass, client_mac(1)).via_device_id == expected


async def test_the_name_a_user_gave_an_ap_is_never_its_ap_name(
    hass, api, ac_snapshot, device_registry
):
    coordinator = await setup(hass, make_entry(hass))
    garage = ap_device(hass, 1)
    device_registry.async_update_device(garage.id, name_by_user="Back door AP")
    phone, tablet, watch = ac_snapshot.clients
    clients = (replace(phone, ap_name="Back door AP"), tablet, watch)
    await publish(hass, api, coordinator, replace(ac_snapshot, clients=clients))
    assert client_device(hass, phone.mac).via_device_id is None
    assert client_device(hass, watch.mac).via_device_id == garage.id


async def test_a_client_that_leaves_loses_its_parent_but_keeps_its_device(hass, api, ac_snapshot):
    entry = make_entry(hass)
    coordinator = await setup(hass, entry)
    phone, *others = ac_snapshot.clients
    original = client_device(hass, phone.mac)
    await publish(hass, api, coordinator, replace(ac_snapshot, clients=tuple(others)))
    away = client_device(hass, phone.mac)
    assert (away.id, away.name, away.via_device_id) == (original.id, "Example phone", None)
    assert trackers(hass, entry)[phone.mac].device_id == original.id
    await publish(hass, api, coordinator, ac_snapshot)
    assert tree(hass) == TREE
    assert client_device(hass, phone.mac).id == original.id


async def test_uncertain_parent_is_cleared_and_restored_when_the_name_is_unique_again(
    hass, api, ac_snapshot
):
    entry = make_entry(hass)
    coordinator = await setup(hass, entry)
    phone, *others = ac_snapshot.clients
    original = client_device(hass, phone.mac)
    uncertain = replace(ac_snapshot, aps=(*ac_snapshot.aps, access_point(3, "Garage", "0")))
    await publish(hass, api, coordinator, uncertain)
    assert client_device(hass, phone.mac).via_device_id is None
    await publish(hass, api, coordinator, ac_snapshot)
    assert client_device(hass, phone.mac).via_device_id == ap_device(hass, 1).id
    await publish(
        hass,
        api,
        coordinator,
        replace(ac_snapshot, clients=(replace(phone, ap_name="Unknown"), *others)),
    )
    assert client_device(hass, phone.mac).via_device_id is None
    await publish(hass, api, coordinator, ac_snapshot)
    assert client_device(hass, phone.mac).via_device_id == ap_device(hass, 1).id
    assert client_device(hass, phone.mac).id == original.id
    assert trackers(hass, entry)[phone.mac].device_id == original.id


async def test_later_snapshots_update_names_and_leave_other_changes_alone(hass, api, ac_snapshot):
    coordinator = await setup(hass, make_entry(hass))
    phone, tablet, watch = ac_snapshot.clients
    renamed = (replace(phone, name="Example tablet"), replace(tablet, name="Kitchen tablet"), watch)
    await publish(hass, api, coordinator, replace(ac_snapshot, clients=renamed))
    assert client_device(hass, phone.mac).name == "Example tablet"
    assert client_device(hass, tablet.mac).name == "Kitchen tablet"
    assert client_device(hass, watch.mac).name == watch.mac

    events = async_capture_events(hass, dr.EVENT_DEVICE_REGISTRY_UPDATED)
    changed = tuple(replace(client, ip="192.0.2.200", ssid="Other wireless") for client in renamed)
    await publish(hass, api, coordinator, replace(ac_snapshot, clients=changed))
    assert not events


async def test_user_settings_and_ids_survive_every_move(
    hass, api, ac_snapshot, device_registry, area_registry, label_registry
):
    entry = make_entry(hass)
    coordinator = await setup(hass, entry)
    phone, *others = ac_snapshot.clients
    area = area_registry.async_create("Garage")
    label = label_registry.async_create("Family")
    device_registry.async_update_device(
        client_device(hass, phone.mac).id,
        name_by_user="Alice phone",
        area_id=area.id,
        labels={label.label_id},
    )

    def kept() -> tuple:
        device = client_device(hass, phone.mac)
        row = trackers(hass, entry)[phone.mac]
        return (
            (device.id, device.connections, device.identifiers),
            (device.name_by_user, device.area_id, device.labels),
            (row.entity_id, row.unique_id, row.device_id, row.disabled_by),
        )

    expected = kept()
    assert expected[1] == ("Alice phone", area.id, {label.label_id})
    assert expected[2][2:] == (expected[0][0], DISABLED)
    for clients in ((replace(phone, ap_name="Study"), *others), tuple(others), ac_snapshot.clients):
        await publish(hass, api, coordinator, replace(ac_snapshot, clients=clients))
        assert kept() == expected
    assert await hass.config_entries.async_unload(entry.entry_id)
    await setup(hass, entry)
    assert kept() == expected
    assert client_device(hass, phone.mac).via_device_id == ap_device(hass, 1).id


async def test_version_0_2_1_client_devices_leave_the_controller(
    hass, api, ac_snapshot, device_registry, entity_registry
):
    entry = make_entry(hass)
    controller = device_registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, CONTROLLER_MAC)},
        connections={(NETWORK_MAC, CONTROLLER_MAC)},
        manufacturer="TP-Link",
        model="TL-AC100",
        name="TL-AC100",
    )
    old = {}
    old_names = ((client_mac(1), "Example phone"), (client_mac(2), client_mac(2)))
    for address, name in (
        *old_names,
        (client_mac(3), client_mac(3)),
        (client_mac(9), "Old laptop"),
    ):
        device = device_registry.async_get_or_create(
            config_entry_id=entry.entry_id,
            connections={(NETWORK_MAC, address)},
            name=name,
            via_device_id=controller.id,
        )
        entity_registry.async_get_or_create(
            "device_tracker",
            DOMAIN,
            address,
            config_entry=entry,
            device_id=device.id,
            disabled_by=DISABLED,
        )
        old[address] = device.id
    await setup(hass, entry)
    assert tree(hass) == TREE | {"Old laptop": None}
    assert {address: client_device(hass, address).id for address in old} == old
    assert {address: row.device_id for address, row in trackers(hass, entry).items()} == old


@pytest.mark.parametrize(
    ("device_name", "tracker_name", "reported", "present", "expected"),
    [
        pytest.param("Example phone", None, None, True, "Example phone", id="unnamed now"),
        pytest.param("Example phone", None, None, False, "Example phone", id="away"),
        pytest.param("Example phone", "Old phone", None, False, "Example phone", id="device first"),
        pytest.param(client_mac(1), "Old phone", None, False, "Old phone", id="MAC to 0.2.0 name"),
        pytest.param(client_mac(1), None, None, False, client_mac(1), id="MAC stays"),
        pytest.param(None, "Old phone", None, False, "Old phone", id="new device, 0.2.0 name"),
        pytest.param(None, "Old phone", None, True, "Old phone", id="new unnamed present device"),
        pytest.param(None, None, None, False, client_mac(1), id="new device, MAC"),
        pytest.param(None, None, "  ", True, client_mac(1), id="blank name is no name"),
        pytest.param(client_mac(1), "Old phone", "Phone", True, "Phone", id="live name wins"),
        pytest.param("Example phone", None, "Phone", True, "Phone", id="live rename"),
    ],
)
async def test_a_mac_never_replaces_a_better_device_name(
    hass, api, ac_snapshot, device_registry, entity_registry,
    device_name, tracker_name, reported, present, expected,
):  # fmt: skip
    entry = make_entry(hass)
    device = None
    if device_name is not None:
        device = device_registry.async_get_or_create(
            config_entry_id=entry.entry_id,
            connections={(NETWORK_MAC, client_mac(1))},
            name=device_name,
        )
        device_registry.async_update_device(device.id, name_by_user="Alice phone")
    entity_registry.async_get_or_create(
        "device_tracker",
        DOMAIN,
        client_mac(1),
        config_entry=entry,
        device_id=device.id if device else None,
        original_name=tracker_name,
        disabled_by=DISABLED,
    )
    clients = (wifi_client(1, "Garage", reported),) if present else ()
    api.async_get_snapshot.return_value = replace(ac_snapshot, clients=clients)
    await setup(hass, entry)
    assert client_device(hass, client_mac(1)).name == expected
    if device is not None:
        assert client_device(hass, client_mac(1)).name_by_user == "Alice phone"


async def test_another_entries_client_metadata_is_left_alone(
    hass, api, ac_snapshot, device_registry, entity_registry
):
    other = make_entry(hass, "02:00:00:00:00:77")
    foreign = device_registry.async_get_or_create(
        config_entry_id=other.entry_id,
        connections={(NETWORK_MAC, client_mac(2))},
        name="Other controller's tablet",
    )
    entity_registry.async_get_or_create(
        "device_tracker",
        DOMAIN,
        client_mac(2),
        config_entry=other,
        device_id=foreign.id,
        original_name="Other controller's tablet",
    )
    entry = make_entry(hass)
    with patch("custom_components.tplink_ac100.PLATFORMS", []):
        coordinator = await setup(hass, entry)
        await publish(hass, api, coordinator, ac_snapshot)
    own = device_registry.async_get_device_by_connection(
        (NETWORK_MAC, client_mac(2)), entry.entry_id
    )
    assert own.id != foreign.id
    assert own.name == client_mac(2)
    assert (
        own.via_device_id
        == device_registry.async_get_device_by_identifier((DOMAIN, ap_mac(2)), entry.entry_id).id
    )
    assert device_registry.async_get(foreign.id) == foreign


async def test_the_registry_is_left_alone_after_a_failed_poll(
    hass, api, ac_snapshot, device_registry
):
    coordinator = await setup(hass, make_entry(hass))
    phone = ac_snapshot.clients[0]
    controller = named_device(hass, "TL-AC100")
    device_registry.async_update_device(
        client_device(hass, phone.mac).id, via_device_id=controller.id
    )
    events = async_capture_events(hass, dr.EVENT_DEVICE_REGISTRY_UPDATED)
    api.async_get_snapshot.side_effect = Ac100ConnectionError("Unavailable")
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert not coordinator.last_update_success
    assert not events
    assert client_device(hass, phone.mac).via_device_id == controller.id
    api.async_get_snapshot.side_effect = None
    await coordinator.async_refresh()
    assert client_device(hass, phone.mac).via_device_id == ap_device(hass, 1).id


async def test_a_new_access_point_and_its_first_client_arrive_in_one_poll(hass, api, ac_snapshot):
    coordinator = await setup(hass, make_entry(hass))
    snapshot = replace(
        ac_snapshot,
        aps=(*ac_snapshot.aps, access_point(3, "Annex")),
        clients=(*ac_snapshot.clients, wifi_client(4, "Annex", "Guest phone")),
    )
    await publish(hass, api, coordinator, snapshot)
    assert tree(hass) == TREE | {"Annex": "TL-AC100", "Guest phone": "Annex"}


async def test_a_client_row_with_an_ap_mac_never_moves_the_ap(hass, api, ac_snapshot):
    clients = (*ac_snapshot.clients, replace(wifi_client(4, "Study"), mac=ap_mac(1)))
    api.async_get_snapshot.return_value = replace(ac_snapshot, clients=clients)
    entry = make_entry(hass)
    await setup(hass, entry)
    assert tree(hass) == TREE
    assert ap_mac(1) not in trackers(hass, entry)


async def test_an_absent_clients_device_can_be_deleted_and_stays_deleted(
    hass, api, ac_snapshot, device_registry
):
    entry = make_entry(hass)
    coordinator = await setup(hass, entry)
    phone, tablet, watch = ac_snapshot.clients
    await publish(hass, api, coordinator, replace(ac_snapshot, clients=(tablet, watch)))
    device = client_device(hass, phone.mac)
    assert await async_remove_config_entry_device(hass, entry, device)
    device_registry.async_remove_device(device.id)
    await publish(
        hass,
        api,
        coordinator,
        replace(ac_snapshot, clients=(replace(tablet, name="Tablet"), watch)),
    )
    assert client_device(hass, phone.mac) is None
    assert phone.mac not in trackers(hass, entry)


async def test_known_client_macs_are_the_current_clients_and_the_restored_trackers(
    hass, api, ac_snapshot, entity_registry
):
    entry = make_entry(hass)
    coordinator = await setup(hass, entry)
    other = make_entry(hass, "02:00:00:00:00:77")
    for domain, unique_id, owner in (
        ("device_tracker", client_mac(9), entry),
        ("device_tracker", client_mac(10), other),
        ("sensor", client_mac(11), entry),
        ("device_tracker", f"{CONTROLLER_MAC}_client_{client_mac(12)}_signal", entry),
        ("device_tracker", ap_mac(1), entry),
        ("device_tracker", CONTROLLER_MAC, entry),
    ):
        entity_registry.async_get_or_create(domain, DOMAIN, unique_id, config_entry=owner)
    assert known_client_macs(coordinator) == {*(c.mac for c in ac_snapshot.clients), client_mac(9)}


async def test_client_entities_share_the_clients_device_and_need_a_current_client(
    hass, api, ac_snapshot
):
    entry = make_entry(hass)
    coordinator = await setup(hass, entry)
    phone, tablet, watch = ac_snapshot.clients
    entity = Ac100ClientEntity(coordinator, phone.mac, "probe")
    assert entity.unique_id == f"{CONTROLLER_MAC}_client_{phone.mac}_probe"
    assert entity.has_entity_name
    assert (entity.mac, entity.client, entity.available) == (phone.mac, phone, True)
    device = dr.async_get(hass).async_get_or_create_from_info(
        config_entry_id=entry.entry_id, device_info=entity.device_info
    )
    assert device.id == client_device(hass, phone.mac).id
    assert device.via_device_id == ap_device(hass, 1).id

    await publish(hass, api, coordinator, replace(ac_snapshot, clients=(tablet, watch)))
    assert (entity.client, entity.available) == (None, False)
    assert entity.device_info.via_device_id is None
    other = Ac100ClientEntity(coordinator, tablet.mac, "probe")
    assert other.available
    api.async_get_snapshot.side_effect = Ac100ConnectionError("Unavailable")
    await coordinator.async_refresh()
    assert not other.available


async def test_saved_offline_client_keeps_its_identity_without_becoming_present(
    hass, api, ac_snapshot, device_registry
):
    address = client_mac(7)
    saved = TerminalConfiguration(mac=address, name="Retained phone")
    offline = replace(ac_snapshot, clients=(), terminals=TerminalState((saved,)))
    api.async_get_snapshot.return_value = offline
    entry = make_entry(hass)
    coordinator = await setup(hass, entry)
    original = client_device(hass, address)
    assert (original.name, original.via_device_id) == ("Retained phone", None)
    tracker = trackers(hass, entry)[address]
    assert (tracker.device_id, tracker.disabled_by) == (original.id, DISABLED)
    assert known_client_macs(coordinator) == {address}
    entity = Ac100ClientEntity(coordinator, address, "probe")
    assert (entity.client, entity.available) == (None, False)
    device_registry.async_update_device(original.id, name_by_user="User phone")

    online = replace(offline, clients=(wifi_client(7, "Study", "Current phone"),))
    await publish(hass, api, coordinator, online)
    current = client_device(hass, address)
    assert (current.id, current.name, current.name_by_user, current.via_device_id) == (
        original.id,
        "Current phone",
        "User phone",
        ap_device(hass, 2).id,
    )
    assert entity.client == online.clients[0]
    assert entity.available
    assert trackers(hass, entry)[address] == tracker

    renamed = replace(saved, name="Saved phone")
    await publish(hass, api, coordinator, replace(offline, terminals=TerminalState((renamed,))))
    away = client_device(hass, address)
    assert (away.id, away.name, away.name_by_user, away.via_device_id) == (
        original.id,
        "Saved phone",
        "User phone",
        None,
    )
    assert (entity.client, entity.available) == (None, False)


async def test_retained_configuration_is_not_home_assistant_device_removal(
    hass, api, ac_snapshot, device_registry
):
    address = client_mac(7)
    saved = TerminalConfiguration(mac=address, name="Retained phone")
    snapshot = replace(ac_snapshot, clients=(), terminals=TerminalState((saved,)))
    api.async_get_snapshot.return_value = snapshot
    entry = make_entry(hass)
    coordinator = await setup(hass, entry)
    device = client_device(hass, address)
    assert not await async_remove_config_entry_device(hass, entry, device)

    await publish(hass, api, coordinator, replace(snapshot, terminals=None))
    assert client_device(hass, address).id == device.id
    assert client_device(hass, address).name == "Retained phone"
    assert not await async_remove_config_entry_device(hass, entry, device)

    empty = replace(snapshot, terminals=TerminalState())
    await publish(hass, api, coordinator, empty)
    assert await async_remove_config_entry_device(hass, entry, device)
    device_registry.async_remove_device(device.id)
    await publish(hass, api, coordinator, empty)
    assert client_device(hass, address) is None
    assert address not in known_client_macs(coordinator)


async def test_configured_infrastructure_never_becomes_a_terminal(hass, api, ac_snapshot):
    saved = TerminalState(
        tuple(
            TerminalConfiguration(mac=address, name="Not a client")
            for address in (CONTROLLER_MAC, ap_mac(1), client_mac(7))
        )
    )
    api.async_get_snapshot.return_value = replace(ac_snapshot, terminals=saved)
    entry = make_entry(hass)
    coordinator = await setup(hass, entry)
    assert known_client_macs(coordinator) == {
        *(client.mac for client in ac_snapshot.clients),
        client_mac(7),
    }
    assert named_device(hass, "TL-AC100").via_device_id is None
    assert ap_device(hass, 1).name == "Garage"
    assert ap_device(hass, 1).via_device_id == named_device(hass, "TL-AC100").id
    assert set(trackers(hass, entry)) == known_client_macs(coordinator)


@pytest.mark.parametrize(
    ("present", "current_name", "saved_name", "expected"),
    [
        (True, "Current phone", "Saved phone", "Current phone"),
        (True, "", "Saved phone", "Saved phone"),
        (False, None, "Saved phone", "Saved phone"),
        (True, client_mac(1), None, "Known phone"),
        (False, None, client_mac(1).upper().replace(":", "-"), "Known phone"),
        (False, None, None, "Known phone"),
    ],
)
async def test_saved_names_do_not_replace_user_metadata_or_promote_presence(
    hass, api, ac_snapshot, device_registry, present, current_name, saved_name, expected
):
    entry = make_entry(hass)
    device = device_registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        connections={(NETWORK_MAC, client_mac(1))},
        name="Known phone",
    )
    device_registry.async_update_device(device.id, name_by_user="User phone")
    saved = TerminalConfiguration(mac=client_mac(1), name=saved_name)
    clients = (wifi_client(1, "Garage", current_name),) if present else ()
    api.async_get_snapshot.return_value = replace(
        ac_snapshot, clients=clients, terminals=TerminalState((saved,))
    )
    await setup(hass, entry)
    current = client_device(hass, client_mac(1))
    assert (current.id, current.name, current.name_by_user) == (device.id, expected, "User phone")
    assert current.via_device_id == (ap_device(hass, 1).id if present else None)
