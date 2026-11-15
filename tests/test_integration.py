"""Native HA lifecycle, presence and opt-in management tests."""

import json
from dataclasses import replace
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import STATE_HOME, STATE_NOT_HOME, STATE_UNAVAILABLE
from homeassistant.exceptions import ConfigEntryAuthFailed, HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.update_coordinator import UpdateFailed
from pytest_homeassistant.common import MockConfigEntry

from custom_components.tplink_ac100.api import Ac100Api, Ac100AuthError, Ac100ConnectionError
from custom_components.tplink_ac100.button import Ac100RebootButton
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


@pytest.fixture
def ac_snapshot():
    return Snapshot(
        Controller("02:00:00:00:00:01", "TL-AC100", "5.0", "1.0.5", {"core1": 12.0}, 31.0, 3600),
        (
            AccessPoint(
                "0",
                "Study AP",
                "TL-XAP1502GI-PoE",
                "02:00:00:00:00:02",
                "192.0.2.2",
                "1",
                True,
                1800,
                (ApRadio("0", "2.4GHz", 1, 6, 15.0),),
            ),
        ),
        (
            Client(
                "4",
                "02:00:00:00:00:03",
                "Example phone",
                "192.0.2.3",
                "",
                "Example wireless",
                "Study AP",
                (ClientRadio("0", "2.4GHz", -55, 72.0),),
            ),
        ),
    )


@pytest.fixture
def ac_entry(hass):
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Test AC100",
        unique_id="02:00:00:00:00:01",
        data={"host": "http://192.0.2.1", "username": "example", "password": "example-only"},
    )
    entry.add_to_hass(hass)
    return entry


@pytest.fixture
def ac_api(ac_snapshot):
    api = AsyncMock(spec=Ac100Api)
    api.async_get_snapshot.return_value = ac_snapshot
    with patch("custom_components.tplink_ac100.Ac100Api", return_value=api):
        yield api


async def setup(hass, entry):
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    return entry.runtime_data


async def test_default_setup_is_read_only_and_unloads(hass, ac_entry, ac_api, entity_registry):
    coordinator = await setup(hass, ac_entry)
    entities = er.async_entries_for_config_entry(entity_registry, ac_entry.entry_id)
    assert {entity.domain for entity in entities} >= {"sensor", "binary_sensor", "device_tracker"}
    assert not any(entity.domain == "button" for entity in entities)
    assert sum(entity.domain == "switch" for entity in entities) == 1
    tracker = next(entity for entity in entities if entity.domain == "device_tracker")
    assert tracker.disabled_by is er.RegistryEntryDisabler.INTEGRATION
    ac_api.async_set_ap_led.assert_not_called()
    ac_api.async_reboot_ap.assert_not_called()
    ac_api.async_reboot_controller.assert_not_called()
    assert await hass.config_entries.async_unload(ac_entry.entry_id)
    assert not coordinator._listeners


@pytest.mark.usefixtures("entity_registry_enabled_by_default")
async def test_presence_disappearance_and_failed_poll(
    hass, ac_entry, ac_api, ac_snapshot, entity_registry
):
    coordinator = await setup(hass, ac_entry)
    entity_id = entity_registry.async_get_entity_id(
        "device_tracker", DOMAIN, ac_snapshot.clients[0].mac
    )
    assert hass.states.get(entity_id).state == STATE_HOME
    coordinator.async_set_updated_data(replace(ac_snapshot, clients=()))
    await hass.async_block_till_done()
    state = hass.states.get(entity_id)
    assert state.state == STATE_NOT_HOME
    assert "ssid" not in state.attributes
    coordinator.async_set_update_error(UpdateFailed("Controller unreachable"))
    await hass.async_block_till_done()
    assert hass.states.get(entity_id).state == STATE_UNAVAILABLE


@pytest.mark.usefixtures("entity_registry_enabled_by_default")
async def test_restored_tracker_is_away_after_reload(
    hass, ac_entry, ac_api, ac_snapshot, entity_registry
):
    await setup(hass, ac_entry)
    entity_id = entity_registry.async_get_entity_id(
        "device_tracker", DOMAIN, ac_snapshot.clients[0].mac
    )
    assert await hass.config_entries.async_unload(ac_entry.entry_id)
    ac_api.async_get_snapshot.return_value = replace(ac_snapshot, clients=())
    await setup(hass, ac_entry)
    assert hass.states.get(entity_id).state == STATE_NOT_HOME


async def test_management_requires_opt_in(hass, ac_entry, ac_api):
    coordinator = await setup(hass, ac_entry)
    with pytest.raises(HomeAssistantError):
        await coordinator.async_control(ac_api.async_reboot_controller)
    ac_api.async_reboot_controller.assert_not_called()


async def test_opt_in_controls_and_safe_defaults(hass, ac_entry, ac_api, entity_registry):
    hass.config_entries.async_update_entry(ac_entry, options={CONF_ENABLE_CONTROL: True})
    await setup(hass, ac_entry)
    entities = er.async_entries_for_config_entry(entity_registry, ac_entry.entry_id)
    buttons = [entity for entity in entities if entity.domain == "button"]
    assert len(buttons) == 2
    assert all(entity.disabled_by is er.RegistryEntryDisabler.INTEGRATION for entity in buttons)
    switch = next(
        entity
        for entity in entities
        if entity.domain == "switch" and entity.unique_id.endswith("_led")
    )
    await hass.services.async_call(
        "switch", "turn_off", {"entity_id": switch.entity_id}, blocking=True
    )
    ac_api.async_set_ap_led.assert_awaited_once_with("0", False)
    ac_api.async_reboot_ap.assert_not_called()
    ac_api.async_reboot_controller.assert_not_called()


async def test_reboot_target_uses_fresh_ap_address(hass, ac_entry, ac_api, ac_snapshot):
    hass.config_entries.async_update_entry(ac_entry, options={CONF_ENABLE_CONTROL: True})
    coordinator = await setup(hass, ac_entry)
    changed = replace(ac_snapshot, aps=(replace(ac_snapshot.aps[0], entry_id="9"),))
    ac_api.async_get_snapshot.return_value = changed
    button = Ac100RebootButton(coordinator, ac_snapshot.aps[0].mac)
    button.hass = hass
    await button.async_press()
    ac_api.async_reboot_ap.assert_awaited_once_with("9")


async def test_connecting_ap_cannot_be_managed(hass, ac_entry, ac_api, ac_snapshot):
    hass.config_entries.async_update_entry(ac_entry, options={CONF_ENABLE_CONTROL: True})
    ac_api.async_get_snapshot.return_value = replace(
        ac_snapshot, aps=(replace(ac_snapshot.aps[0], link_status="0"),)
    )
    coordinator = await setup(hass, ac_entry)
    button = Ac100RebootButton(coordinator, ac_snapshot.aps[0].mac)
    button.hass = hass
    assert not button.available
    with pytest.raises(HomeAssistantError):
        await button.async_press()
    ac_api.async_reboot_ap.assert_not_called()


async def test_update_errors_map_to_ha_exceptions(hass, ac_entry, ac_api):
    coordinator = await setup(hass, ac_entry)
    ac_api.async_get_snapshot.side_effect = Ac100AuthError("Rejected credentials")
    with pytest.raises(ConfigEntryAuthFailed):
        await coordinator._async_update_data()
    ac_api.async_get_snapshot.side_effect = Ac100ConnectionError("Unavailable")
    with pytest.raises(UpdateFailed):
        await coordinator._async_update_data()


async def test_diagnostics_omit_credentials_and_network_identities(
    hass, ac_entry, ac_api, ac_snapshot
):
    await setup(hass, ac_entry)
    diagnostics = await async_get_config_entry_diagnostics(hass, ac_entry)
    encoded = json.dumps(diagnostics)
    private = [
        *ac_entry.data.values(),
        ac_snapshot.controller.mac,
        ac_snapshot.aps[0].mac,
        ac_snapshot.aps[0].ip,
        ac_snapshot.aps[0].name,
        ac_snapshot.clients[0].mac,
        ac_snapshot.clients[0].ip,
        ac_snapshot.clients[0].name,
        ac_snapshot.clients[0].ssid,
    ]
    assert all(value not in encoded for value in private)
    assert diagnostics["access_points"] == {"total": 1, "online": 1}


async def test_changed_controller_is_rejected_before_setup(hass, ac_entry, ac_api, ac_snapshot):
    ac_api.async_get_snapshot.return_value = replace(
        ac_snapshot, controller=replace(ac_snapshot.controller, mac="02:00:00:00:ff:01")
    )
    assert not await hass.config_entries.async_setup(ac_entry.entry_id)
    assert ac_entry.state is ConfigEntryState.SETUP_ERROR
    ac_api.async_set_ap_led.assert_not_called()
    ac_api.async_reboot_ap.assert_not_called()
    ac_api.async_reboot_controller.assert_not_called()


async def test_consecutive_actions_bypass_poll_debounce(hass, ac_entry, ac_api, ac_snapshot):
    hass.config_entries.async_update_entry(ac_entry, options={CONF_ENABLE_CONTROL: True})
    coordinator = await setup(hass, ac_entry)
    button = Ac100RebootButton(coordinator, ac_snapshot.aps[0].mac)
    button.hass = hass
    ac_api.async_get_snapshot.return_value = replace(
        ac_snapshot, aps=(replace(ac_snapshot.aps[0], entry_id="9"),)
    )
    await button.async_press()
    ac_api.async_get_snapshot.return_value = replace(
        ac_snapshot, aps=(replace(ac_snapshot.aps[0], entry_id="11"),)
    )
    await button.async_press()
    assert [call.args for call in ac_api.async_reboot_ap.await_args_list] == [("9",), ("11",)]


async def test_failed_freshness_check_blocks_controller_reboot(hass, ac_entry, ac_api):
    hass.config_entries.async_update_entry(ac_entry, options={CONF_ENABLE_CONTROL: True})
    coordinator = await setup(hass, ac_entry)
    button = Ac100RebootButton(coordinator)
    button.hass = hass
    ac_api.async_get_snapshot.side_effect = Ac100ConnectionError("Unavailable")
    with pytest.raises(HomeAssistantError):
        await button.async_press()
    ac_api.async_reboot_controller.assert_not_called()


async def test_busy_session_is_retryable_not_reauthentication(hass, ac_entry, ac_api):
    from custom_components.tplink_ac100.api import Ac100SessionBusyError

    ac_api.async_get_snapshot.side_effect = Ac100SessionBusyError("Session occupied")
    assert not await hass.config_entries.async_setup(ac_entry.entry_id)
    assert ac_entry.state is ConfigEntryState.SETUP_RETRY
    ac_api.async_logout.assert_awaited()


async def test_failed_setup_releases_own_session(hass, ac_entry, ac_api, ac_snapshot):
    ac_api.async_get_snapshot.return_value = replace(
        ac_snapshot, controller=replace(ac_snapshot.controller, mac="02:00:00:00:ff:02")
    )
    assert not await hass.config_entries.async_setup(ac_entry.entry_id)
    ac_api.async_logout.assert_awaited()


async def test_unload_releases_own_session_after_polling_stops(hass, ac_entry, ac_api):
    coordinator = await setup(hass, ac_entry)
    ac_api.async_logout.assert_not_called()

    async def check_shutdown():
        assert coordinator._shutdown_requested

    ac_api.async_logout.side_effect = check_shutdown
    assert await hass.config_entries.async_unload(ac_entry.entry_id)
    ac_api.async_logout.assert_awaited()


async def test_unload_network_failure_does_not_keep_entry_loaded(hass, ac_entry, ac_api):
    await setup(hass, ac_entry)
    ac_api.async_logout.side_effect = Ac100ConnectionError("Unavailable")
    assert await hass.config_entries.async_unload(ac_entry.entry_id)
    assert ac_entry.state is ConfigEntryState.NOT_LOADED


async def test_stopped_coordinator_does_not_run_control(hass, ac_entry, ac_api):
    hass.config_entries.async_update_entry(ac_entry, options={CONF_ENABLE_CONTROL: True})
    coordinator = await setup(hass, ac_entry)
    await coordinator.async_shutdown()
    action = AsyncMock()
    with pytest.raises(HomeAssistantError):
        await coordinator.async_control(action)
    action.assert_not_called()
    ac_api.async_reboot_controller.assert_not_called()


async def test_home_assistant_stop_releases_session(hass, ac_entry, ac_api):
    from homeassistant.const import EVENT_HOMEASSISTANT_STOP

    coordinator = await setup(hass, ac_entry)
    hass.bus.async_fire(EVENT_HOMEASSISTANT_STOP)
    await hass.async_block_till_done()
    assert coordinator._shutdown_requested
    ac_api.async_logout.assert_awaited()


async def test_cancelled_setup_releases_session(hass, ac_entry, ac_api):
    import asyncio

    entered = asyncio.Event()
    blocked = asyncio.Event()

    async def snapshot():
        entered.set()
        await blocked.wait()

    ac_api.async_get_snapshot.side_effect = snapshot
    task = asyncio.create_task(hass.config_entries.async_setup(ac_entry.entry_id))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    ac_api.async_logout.assert_awaited()


async def test_unsafe_led_refusal_is_explained_without_reauth(hass, ac_entry, ac_api):
    from custom_components.tplink_ac100.api import Ac100UnsafeLedError

    hass.config_entries.async_update_entry(ac_entry, options={CONF_ENABLE_CONTROL: True})
    coordinator = await setup(hass, ac_entry)
    action = AsyncMock(side_effect=Ac100UnsafeLedError("Safety check rejected"))
    with patch.object(type(ac_entry), "async_start_reauth") as reauth:
        with pytest.raises(HomeAssistantError) as error:
            await coordinator.async_control(action)
        assert error.value.translation_key == "led_wifi_sync_unsafe"
        reauth.assert_not_called()
