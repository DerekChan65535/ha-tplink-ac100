"""Wireless networks (SSIDs) as devices with sensors, binary sensors and switches.

The Home Assistant test runtime and a mocked API stand in for the controller. Entities are found
by unique ID, never by translated name.
"""

import json
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import (
    CONF_SCAN_INTERVAL,
    STATE_OFF,
    STATE_ON,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
    EntityCategory,
    Platform,
)
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_platform
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.update_coordinator import UpdateFailed
from pytest_homeassistant.common import MockConfigEntry, async_capture_events

from custom_components.tplink_ac100 import async_remove_config_entry_device
from custom_components.tplink_ac100.api import (
    Ac100Api,
    Ac100ConnectionError,
    Ac100Error,
    Ac100SessionExpiredError,
)
from custom_components.tplink_ac100.const import (
    CONF_ENABLE_CONTROL,
    CONF_MONITOR_WIRELESS_SERVICES,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
)
from custom_components.tplink_ac100.diagnostics import async_get_config_entry_diagnostics
from custom_components.tplink_ac100.models import AccessPoint, Client, Controller, Snapshot
from custom_components.tplink_ac100.monitoring import (
    WIRELESS_SERVICE_SECURITY,
    ConfigurationState,
    MonitoringState,
    WirelessService,
)

pytestmark = pytest.mark.usefixtures("enable_custom_integrations")

CONTROLLER_MAC = "02:00:00:00:00:10"
AP_MAC = "02:00:00:00:00:41"
HOME = WirelessService(
    "1",
    "Home",
    enabled=True,
    broadcast=True,
    isolation=False,
    security="wpa_psk_wpa2_psk",
    igmp_snooping=True,
    igmp_querier=False,
    vlan=None,
)
GUEST = WirelessService(
    "2",
    "Guest",
    enabled=True,
    broadcast=False,
    isolation=True,
    security="open",
    igmp_snooping=False,
    igmp_querier=True,
    vlan=20,
)
CAMERAS = WirelessService(
    "3",
    "Cameras",
    enabled=False,
    broadcast=True,
    isolation=False,
    security="wpa2_psk_wpa3_sae",
    igmp_snooping=None,
    igmp_querier=None,
    vlan=None,
)
SERVICES = (HOME, GUEST, CAMERAS)
SWITCH_KEYS = ("enabled", "broadcast", "isolation", "igmp_snooping", "igmp_querier")
DIAGNOSTIC, CONFIG = EntityCategory.DIAGNOSTIC, EntityCategory.CONFIG
SENSORS = {
    # key: (translation key, entity category, disabled by default)
    "connected_clients": ("connected_clients", None, False),
    "security": ("wireless_service_security", DIAGNOSTIC, False),
    "vlan": ("wireless_service_vlan", DIAGNOSTIC, True),
}
BINARY_SENSORS = {
    "enabled": ("wireless_service_enabled", None, False),
    "hidden": ("wireless_service_hidden", DIAGNOSTIC, False),
    "isolation": ("wireless_service_isolation", DIAGNOSTIC, True),
}
SWITCHES = {key: (f"wireless_service_{key}", CONFIG, True) for key in SWITCH_KEYS}
EXPECTED = {
    **{("sensor", key): value for key, value in SENSORS.items()},
    **{("binary_sensor", key): value for key, value in BINARY_SENSORS.items()},
    **{("switch", key): value for key, value in SWITCHES.items()},
}
READ_ONLY = {pair for pair in EXPECTED if pair[0] != "switch"}


def client(number, ssid):
    return Client(
        str(number), f"02:00:00:cc:dd:{number:02x}", f"Phone {number}", f"192.0.2.{number + 50}",
        "", ssid, "Garage", (),
    )  # fmt: skip


@pytest.fixture
def snapshot():
    """Two clients on Home, one on Guest, one on a network the table does not list."""
    controller = Controller(CONTROLLER_MAC, "TL-AC100", "5.0", "1.0.5", {"core1": 1.0}, 40.0, 7200)
    aps = (
        AccessPoint("0", "Garage", "TL-XAP1502GI-PoE", AP_MAC, "192.0.2.41", "1", True, 600, ()),
    )
    clients = (client(1, "Home"), client(2, "Home"), client(3, "Guest"), client(4, "Elsewhere"))
    return Snapshot(controller, aps, clients)


@pytest.fixture
def api(snapshot):
    """The protocol boundary: nothing here reaches a physical controller."""
    api = AsyncMock(spec=Ac100Api)
    api.async_get_snapshot.return_value = snapshot
    api.async_get_monitoring.return_value = MonitoringState(wireless_services=SERVICES)
    api.async_get_configuration.return_value = ConfigurationState()
    platforms = [Platform.SENSOR, Platform.BINARY_SENSOR, Platform.SWITCH]
    with (
        patch("custom_components.tplink_ac100.Ac100Api", return_value=api),
        patch("custom_components.tplink_ac100.PLATFORMS", platforms),
    ):
        yield api


@pytest.fixture
async def start(hass, api):
    """Load one entry with the given opt-ins and return it with its coordinator."""
    entries = []

    async def create(*, wireless=True, control=False):
        entry = MockConfigEntry(
            domain=DOMAIN,
            unique_id=CONTROLLER_MAC,
            data={"host": "http://192.0.2.1", "username": "example", "password": "example-only"},
            options={CONF_ENABLE_CONTROL: control, CONF_MONITOR_WIRELESS_SERVICES: wireless},
        )
        entry.add_to_hass(hass)
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        entries.append(entry)
        return entry, entry.runtime_data

    yield create
    for entry in entries:
        if entry.state is ConfigEntryState.LOADED:
            assert await hass.config_entries.async_unload(entry.entry_id)


def unique_id(service_id, key):
    return f"{CONTROLLER_MAC}_wlan_{service_id}_{key}"


def entity_id(registry, domain, service_id, key):
    result = registry.async_get_entity_id(domain, DOMAIN, unique_id(service_id, key))
    assert result is not None, (domain, service_id, key)
    return result


def state_of(hass, registry, domain, service_id, key):
    state = hass.states.get(entity_id(registry, domain, service_id, key))
    assert state is not None, (domain, service_id, key)
    return state


def entity_object(hass, entity_id):
    for platform in entity_platform.async_get_platforms(hass, DOMAIN):
        if entity := platform.entities.get(entity_id):
            return entity
    raise AssertionError(f"{entity_id} is not loaded")


def service_rows(registry, entry):
    """The entry's wireless network entities by domain and unique ID.

    A binary sensor and a switch of one network share a unique ID in different domains.
    """
    return {
        (row.domain, row.unique_id): row
        for row in er.async_entries_for_config_entry(registry, entry.entry_id)
        if "_wlan_" in row.unique_id
    }


def service_device(registry, entry, service):
    return registry.async_get_device_by_identifier(
        (DOMAIN, f"{CONTROLLER_MAC}_wlan_{service.service_id}"), entry.entry_id
    )


async def publish(hass, coordinator, services=SERVICES, **changes):
    """Publish a poll result whose SSID read gave ``services`` (None: the read failed)."""
    monitoring = MonitoringState(wireless_services=services)
    coordinator.async_set_updated_data(replace(coordinator.data, monitoring=monitoring, **changes))
    await hass.async_block_till_done()


def on_off(value):
    return STATE_UNKNOWN if value is None else STATE_ON if value else STATE_OFF


# Options


async def test_the_options_form_offers_the_wireless_opt_in_defaulting_to_off(hass):
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=CONTROLLER_MAC,
        data={"host": "http://192.0.2.1", "username": "example", "password": "example-only"},
        options={CONF_ENABLE_CONTROL: True},
    )
    entry.add_to_hass(hass)
    with patch.object(hass.config_entries, "async_schedule_reload") as reload_entry:
        result = await hass.config_entries.options.async_init(entry.entry_id)
        assert result["type"] is FlowResultType.FORM
        assert {str(key) for key in result["data_schema"].schema} == {
            CONF_ENABLE_CONTROL,
            CONF_MONITOR_WIRELESS_SERVICES,
            CONF_SCAN_INTERVAL,
        }
        saved = await hass.config_entries.options.async_configure(result["flow_id"], {})
        assert saved["type"] is FlowResultType.CREATE_ENTRY
        assert entry.options == {
            CONF_ENABLE_CONTROL: True,
            CONF_MONITOR_WIRELESS_SERVICES: False,
            CONF_SCAN_INTERVAL: DEFAULT_SCAN_INTERVAL,
        }
        result = await hass.config_entries.options.async_init(entry.entry_id)
        await hass.config_entries.options.async_configure(
            result["flow_id"], {CONF_MONITOR_WIRELESS_SERVICES: True}
        )
    assert entry.options == {
        CONF_ENABLE_CONTROL: True,
        CONF_MONITOR_WIRELESS_SERVICES: True,
        CONF_SCAN_INTERVAL: DEFAULT_SCAN_INTERVAL,
    }
    assert reload_entry.call_count == 2


@pytest.mark.parametrize("wireless", [True, False])
async def test_the_poll_requests_the_ssid_table_only_with_the_opt_in(api, start, wireless):
    await start(wireless=wireless)
    api.async_get_monitoring.assert_awaited_once_with(wireless_services=wireless)


@pytest.mark.parametrize("control", [True, False])
async def test_nothing_is_added_while_the_opt_in_is_off(
    hass, start, entity_registry, device_registry, control
):
    entry, coordinator = await start(wireless=False, control=control)
    assert coordinator.data.monitoring.wireless_services == SERVICES  # the mock answers anyway
    assert not service_rows(entity_registry, entry)
    devices = dr.async_entries_for_config_entry(device_registry, entry.entry_id)
    assert not [d for d in devices if any("_wlan_" in value for _, value in d.identifiers)]
    await publish(hass, coordinator)
    assert not service_rows(entity_registry, entry)


# Devices and entities


async def test_every_network_is_a_device_behind_the_controller(
    hass, start, device_registry, entity_registry
):
    entry, _ = await start()
    controller = device_registry.async_get_device_by_identifier(
        (DOMAIN, CONTROLLER_MAC), entry.entry_id
    )
    rows = service_rows(entity_registry, entry)
    for service in SERVICES:
        device = service_device(device_registry, entry, service)
        assert device is not None
        assert device.identifiers == {(DOMAIN, f"{CONTROLLER_MAC}_wlan_{service.service_id}")}
        assert device.connections == set()
        assert device.name == service.name
        assert (device.manufacturer, device.model) == ("TP-Link", "Wireless network")
        assert device.via_device_id == controller.id
        assert device.configuration_url == "http://192.0.2.1"
        assert device.entry_type is None
        attached = {row.unique_id for row in rows.values() if row.device_id == device.id}
        assert attached == {unique_id(service.service_id, key) for _, key in READ_ONLY}
    names = {d.name for d in dr.async_entries_for_config_entry(device_registry, entry.entry_id)}
    clients = {f"Phone {number}" for number in range(1, 5)}
    assert names == {"TL-AC100", "Garage", "Home", "Guest", "Cameras"} | clients


async def test_entities_have_stable_ids_categories_and_defaults(hass, start, entity_registry):
    entry, _ = await start()
    rows = service_rows(entity_registry, entry)
    assert set(rows) == {
        (domain, unique_id(s.service_id, key)) for s in SERVICES for domain, key in READ_ONLY
    }
    for service in SERVICES:
        for domain, key in READ_ONLY:
            row = rows[domain, unique_id(service.service_id, key)]
            translation_key, category, disabled = EXPECTED[domain, key]
            assert (row.domain, row.platform) == (domain, DOMAIN)
            assert row.translation_key == translation_key
            assert row.entity_category is category
            assert (row.disabled_by is er.RegistryEntryDisabler.INTEGRATION) is disabled
            assert (hass.states.get(row.entity_id) is None) is disabled


async def test_values_of_the_default_entities(hass, start, entity_registry):
    await start()
    counts = {"1": "2", "2": "1", "3": "0"}
    for service in SERVICES:
        sid = service.service_id
        clients = state_of(hass, entity_registry, "sensor", sid, "connected_clients")
        assert clients.state == counts[sid]
        assert clients.attributes["state_class"] == "measurement"
        security = state_of(hass, entity_registry, "sensor", sid, "security")
        assert security.state == service.security
        assert security.attributes["device_class"] == "enum"
        assert security.attributes["options"] == list(WIRELESS_SERVICE_SECURITY.values())
        enabled = state_of(hass, entity_registry, "binary_sensor", sid, "enabled")
        assert enabled.state == on_off(service.enabled)
        hidden = state_of(hass, entity_registry, "binary_sensor", sid, "hidden")
        assert hidden.state == on_off(not service.broadcast)


@pytest.mark.usefixtures("entity_registry_enabled_by_default")
async def test_values_of_the_entities_that_start_disabled(hass, start, entity_registry):
    await start(control=True)
    for service in SERVICES:
        sid = service.service_id
        vlan = state_of(hass, entity_registry, "sensor", sid, "vlan")
        assert vlan.state == (STATE_UNKNOWN if service.vlan is None else str(service.vlan))
        isolation = state_of(hass, entity_registry, "binary_sensor", sid, "isolation")
        assert isolation.state == on_off(service.isolation)
        for key in SWITCH_KEYS:
            assert state_of(hass, entity_registry, "switch", sid, key).state == on_off(
                getattr(service, key)
            )


@pytest.mark.usefixtures("entity_registry_enabled_by_default")
async def test_unknown_values_are_unknown_and_not_off_or_zero(hass, start, entity_registry):
    _, coordinator = await start(control=True)
    unknown = WirelessService("1", "Home")
    await publish(hass, coordinator, (unknown,))
    for domain, key in EXPECTED:
        state = state_of(hass, entity_registry, domain, "1", key)
        if (domain, key) == ("sensor", "connected_clients"):
            assert state.state == "2"
        else:
            assert state.state == STATE_UNKNOWN, (domain, key)


async def test_connected_clients_count_only_the_clients_of_that_exact_ssid(
    hass, start, entity_registry, snapshot
):
    _, coordinator = await start()
    clients = (*snapshot.clients, client(5, "home"), client(6, "Guest "), client(7, "Guest"))
    await publish(hass, coordinator, clients=clients)
    assert state_of(hass, entity_registry, "sensor", "1", "connected_clients").state == "2"
    assert state_of(hass, entity_registry, "sensor", "2", "connected_clients").state == "2"
    await publish(hass, coordinator, clients=())
    assert state_of(hass, entity_registry, "sensor", "2", "connected_clients").state == "0"


@pytest.mark.usefixtures("entity_registry_enabled_by_default")
async def test_entities_are_unavailable_until_the_ssid_table_is_read_and_lists_the_network(
    hass, start, entity_registry
):
    _, coordinator = await start(control=True)

    def states(service):
        return {
            state_of(hass, entity_registry, domain, service.service_id, key).state
            for domain, key in EXPECTED
        }

    assert STATE_UNAVAILABLE not in states(GUEST)
    for lost in (None, MonitoringState(), MonitoringState(wireless_services=None)):
        coordinator.async_set_updated_data(replace(coordinator.data, monitoring=lost))
        await hass.async_block_till_done()
        assert all(states(service) == {STATE_UNAVAILABLE} for service in SERVICES)
    await publish(hass, coordinator, (HOME, CAMERAS))
    assert states(GUEST) == {STATE_UNAVAILABLE}
    assert STATE_UNAVAILABLE not in states(HOME)
    await publish(hass, coordinator, ())
    assert all(states(service) == {STATE_UNAVAILABLE} for service in SERVICES)
    await publish(hass, coordinator)
    assert all(STATE_UNAVAILABLE not in states(service) for service in SERVICES)
    coordinator.async_set_update_error(UpdateFailed("Controller unreachable"))
    await hass.async_block_till_done()
    assert all(states(service) == {STATE_UNAVAILABLE} for service in SERVICES)


async def test_a_network_listed_later_gets_its_entities_once(
    hass, start, entity_registry, device_registry, caplog
):
    entry, coordinator = await start()
    assert len(service_rows(entity_registry, entry)) == 3 * len(READ_ONLY)
    fourth = WirelessService("7", "Garden", enabled=True)
    await publish(hass, coordinator, (*SERVICES, fourth))
    added = {(domain, unique_id("7", key)) for domain, key in READ_ONLY}
    assert added <= set(service_rows(entity_registry, entry))
    assert service_device(device_registry, entry, fourth).name == "Garden"
    count = len(service_rows(entity_registry, entry))
    await publish(hass, coordinator, (*SERVICES, fourth))
    assert len(service_rows(entity_registry, entry)) == count == 4 * len(READ_ONLY)
    assert "does not generate unique IDs" not in caplog.text


@pytest.mark.parametrize("first", [None, ()], ids=["read failed", "no networks"])
async def test_nothing_is_added_before_a_network_is_listed(
    hass, api, start, entity_registry, device_registry, first
):
    api.async_get_monitoring.return_value = MonitoringState(wireless_services=first)
    entry, coordinator = await start()
    assert not service_rows(entity_registry, entry)
    assert service_device(device_registry, entry, HOME) is None
    await publish(hass, coordinator)
    assert len(service_rows(entity_registry, entry)) == 3 * len(READ_ONLY)


async def test_a_renamed_network_keeps_its_identity_and_the_users_choices(
    hass, start, entity_registry, device_registry, snapshot
):
    entry, coordinator = await start()
    before = {key: row.entity_id for key, row in service_rows(entity_registry, entry).items()}
    device_registry.async_update_device(
        service_device(device_registry, entry, GUEST).id, name_by_user="Visitors"
    )
    renamed = replace(GUEST, name="Guest WiFi")
    await publish(hass, coordinator, (HOME, renamed, CAMERAS))
    device = service_device(device_registry, entry, GUEST)
    assert (device.name, device.name_by_user) == ("Guest WiFi", "Visitors")
    after = {key: row.entity_id for key, row in service_rows(entity_registry, entry).items()}
    assert after == before
    # clients name the SSID, so they count again once they report the new name
    assert state_of(hass, entity_registry, "sensor", "2", "connected_clients").state == "0"
    moved = replace(snapshot.clients[2], ssid="Guest WiFi")
    await publish(hass, coordinator, (HOME, renamed, CAMERAS), clients=(moved,))
    assert state_of(hass, entity_registry, "sensor", "2", "connected_clients").state == "1"


async def test_unchanged_data_and_failed_polls_leave_the_device_registry_alone(hass, start):
    _, coordinator = await start()
    events = async_capture_events(hass, dr.EVENT_DEVICE_REGISTRY_UPDATED)
    coordinator.async_update_listeners()
    await hass.async_block_till_done()
    assert not events
    coordinator.data = replace(
        coordinator.data, monitoring=MonitoringState(wireless_services=(replace(HOME, name="X"),))
    )
    coordinator.last_update_success = False
    coordinator.async_update_listeners()
    await hass.async_block_till_done()
    assert not events


async def test_unload_removes_the_listeners(hass, start):
    entry, coordinator = await start(control=True)
    assert await hass.config_entries.async_unload(entry.entry_id)
    assert not coordinator._listeners


# Switches


@pytest.mark.parametrize(
    ("wireless", "control", "expected"),
    [(True, True, 5), (True, False, 0), (False, True, 0), (False, False, 0)],
)
async def test_switches_need_both_opt_ins(
    hass, start, entity_registry, wireless, control, expected
):
    entry, _ = await start(wireless=wireless, control=control)
    switches = [
        row for row in service_rows(entity_registry, entry).values() if row.domain == "switch"
    ]
    assert len(switches) == expected * len(SERVICES)


async def test_switches_start_disabled_in_the_configuration_category(
    hass, start, entity_registry, device_registry
):
    entry, _ = await start(control=True)
    rows = service_rows(entity_registry, entry)
    for service in SERVICES:
        device = service_device(device_registry, entry, service)
        for key, (translation_key, category, _) in SWITCHES.items():
            row = rows["switch", unique_id(service.service_id, key)]
            assert row.domain == "switch"
            assert row.translation_key == translation_key
            assert row.entity_category is category
            assert row.disabled_by is er.RegistryEntryDisabler.INTEGRATION
            assert row.device_id == device.id
            assert hass.states.get(row.entity_id) is None


@pytest.mark.usefixtures("entity_registry_enabled_by_default")
@pytest.mark.parametrize("value", [True, False])
@pytest.mark.parametrize("key", SWITCH_KEYS)
async def test_a_switch_action_changes_only_its_setting_and_shows_the_result(
    hass, api, start, entity_registry, key, value
):
    _, coordinator = await start(control=True)

    async def apply(service_id, field, wanted, *, name):
        services = tuple(
            replace(item, **{field: wanted}) if item.service_id == service_id else item
            for item in SERVICES
        )
        api.async_get_monitoring.return_value = MonitoringState(wireless_services=services)

    api.async_set_wireless_service.side_effect = apply
    switch = entity_id(entity_registry, "switch", "2", key)
    await hass.services.async_call(
        "switch", "turn_on" if value else "turn_off", {"entity_id": switch}, blocking=True
    )
    api.async_set_wireless_service.assert_awaited_once_with("2", key, value, name="Guest")
    assert api.async_get_monitoring.await_count == 2
    api.async_get_monitoring.assert_awaited_with(wireless_services=True)
    assert hass.states.get(switch).state == on_off(value)
    for other in (HOME, CAMERAS):
        for field in SWITCH_KEYS:
            state = state_of(hass, entity_registry, "switch", other.service_id, field)
            assert state.state == on_off(getattr(other, field))
    for unrelated in (api.async_set_ap_led, api.async_reboot_ap, api.async_reboot_controller):
        unrelated.assert_not_awaited()


@pytest.mark.usefixtures("entity_registry_enabled_by_default")
async def test_a_switch_with_an_unknown_setting_can_still_be_used(
    hass, api, start, entity_registry
):
    await start(control=True)
    switch = entity_id(entity_registry, "switch", "3", "igmp_snooping")
    assert hass.states.get(switch).state == STATE_UNKNOWN
    await hass.services.async_call("switch", "turn_on", {"entity_id": switch}, blocking=True)
    api.async_set_wireless_service.assert_awaited_once_with(
        "3", "igmp_snooping", True, name="Cameras"
    )


@pytest.mark.usefixtures("entity_registry_enabled_by_default")
async def test_a_switch_refuses_a_network_the_controller_no_longer_lists(
    hass, api, start, entity_registry
):
    _, coordinator = await start(control=True)
    switch = entity_object(hass, entity_id(entity_registry, "switch", "2", "enabled"))
    await publish(hass, coordinator, (HOME, CAMERAS))
    with pytest.raises(HomeAssistantError) as error:
        await switch.async_turn_on()
    assert error.value.translation_key == "wireless_service_unavailable"
    api.async_set_wireless_service.assert_not_awaited()


@pytest.mark.usefixtures("entity_registry_enabled_by_default")
@pytest.mark.parametrize(
    ("error", "translation_key"),
    [
        (Ac100Error("changed"), "control_failed"),
        (Ac100ConnectionError("down"), "control_failed"),
        (Ac100SessionExpiredError("expired"), "session_expired"),
    ],
)
async def test_a_failed_switch_action_is_explained_and_never_repeated(
    hass, api, start, entity_registry, error, translation_key
):
    await start(control=True)
    api.async_set_wireless_service.side_effect = error
    switch = entity_id(entity_registry, "switch", "1", "broadcast")
    with pytest.raises(HomeAssistantError) as raised:
        await hass.services.async_call("switch", "turn_off", {"entity_id": switch}, blocking=True)
    assert raised.value.translation_key == translation_key
    api.async_set_wireless_service.assert_awaited_once()
    assert api.async_get_monitoring.await_count == 1


@pytest.mark.usefixtures("entity_registry_enabled_by_default")
async def test_a_switch_action_needs_control_to_be_enabled_when_it_runs(
    hass, api, start, entity_registry
):
    entry, _ = await start(control=True)
    hass.config_entries.async_update_entry(
        entry, options={CONF_ENABLE_CONTROL: False, CONF_MONITOR_WIRELESS_SERVICES: True}
    )
    switch = entity_id(entity_registry, "switch", "1", "enabled")
    with pytest.raises(HomeAssistantError) as raised:
        await hass.services.async_call("switch", "turn_off", {"entity_id": switch}, blocking=True)
    assert raised.value.translation_key == "control_disabled"
    api.async_set_wireless_service.assert_not_awaited()


# Device removal and diagnostics


async def test_reported_networks_cannot_be_removed_but_stale_ones_can(hass, start, device_registry):
    entry, coordinator = await start()
    devices = {item.service_id: service_device(device_registry, entry, item) for item in SERVICES}
    controller = device_registry.async_get_device_by_identifier(
        (DOMAIN, CONTROLLER_MAC), entry.entry_id
    )
    for device in devices.values():
        assert not await async_remove_config_entry_device(hass, entry, device)
    await publish(hass, coordinator, (HOME,))
    assert not await async_remove_config_entry_device(hass, entry, devices["1"])
    assert await async_remove_config_entry_device(hass, entry, devices["2"])
    assert await async_remove_config_entry_device(hass, entry, devices["3"])
    await publish(hass, coordinator, (HOME, replace(GUEST, name="Renamed")))
    assert not await async_remove_config_entry_device(hass, entry, devices["2"])
    await publish(hass, coordinator, None)  # the read failed: the network may still exist
    assert not await async_remove_config_entry_device(hass, entry, devices["1"])
    await publish(hass, coordinator, ())
    assert await async_remove_config_entry_device(hass, entry, devices["1"])
    assert not await async_remove_config_entry_device(hass, entry, controller)


async def test_networks_are_not_protected_while_the_opt_in_is_off(
    hass, api, start, device_registry
):
    api.async_get_monitoring.return_value = MonitoringState()  # as the real API answers
    entry, _ = await start(wireless=False)
    old = device_registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, f"{CONTROLLER_MAC}_wlan_1")},
        name="Home",
    )
    assert await async_remove_config_entry_device(hass, entry, old)


async def test_diagnostics_count_networks_without_names_or_identities(hass, start):
    entry, coordinator = await start()
    diagnostics = await async_get_config_entry_diagnostics(hass, entry)
    assert diagnostics["wireless_services_monitored"] is True
    assert diagnostics["monitoring"]["wireless_services"] == 3
    encoded = json.dumps(diagnostics)
    private = [CONTROLLER_MAC, AP_MAC, "192.0.2", *entry.data.values()]
    private += [value for item in SERVICES for value in (item.name, f"_wlan_{item.service_id}")]
    assert not [value for value in private if value in encoded]
    for services, expected in ((None, None), ((), 0), ((HOME,), 1)):
        await publish(hass, coordinator, services)
        diagnostics = await async_get_config_entry_diagnostics(hass, entry)
        assert diagnostics["monitoring"]["wireless_services"] == expected
    coordinator.async_set_updated_data(replace(coordinator.data, monitoring=None))
    diagnostics = await async_get_config_entry_diagnostics(hass, entry)
    assert diagnostics["monitoring"]["wireless_services"] is None


async def test_diagnostics_report_the_opt_in_being_off(hass, start):
    entry, _ = await start(wireless=False)
    diagnostics = await async_get_config_entry_diagnostics(hass, entry)
    assert diagnostics["wireless_services_monitored"] is False


# Translations


def strings_and_icons():
    """The shipped strings and icons."""
    component = Path(__file__).parents[1] / "custom_components" / "tplink_ac100"
    strings = json.loads((component / "strings.json").read_text())
    icons = json.loads((component / "icons.json").read_text())
    return strings, icons


def test_every_wireless_entity_has_an_english_name_states_and_a_known_icon_key():
    strings, icons = strings_and_icons()
    for (domain, key), (translation_key, _, _) in EXPECTED.items():
        translation = strings["entity"][domain][translation_key]
        assert translation["name"], (domain, key)
        if (domain, key) == ("sensor", "security"):
            assert set(translation["state"]) == set(WIRELESS_SERVICE_SECURITY.values())
        else:
            assert "state" not in translation
    assert strings["exceptions"]["wireless_service_unavailable"]["message"]
    init = strings["options"]["step"]["init"]
    assert init["data"][CONF_MONITOR_WIRELESS_SERVICES]
    assert init["data_description"][CONF_MONITOR_WIRELESS_SERVICES]
    for domain, entries in icons["entity"].items():
        wireless = {key for key in entries if key.startswith("wireless_service_")}
        assert wireless <= set(strings["entity"][domain]), domain
