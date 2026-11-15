"""Local-only debug logging controls with a simulated Home Assistant runtime."""

import logging
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import STATE_OFF, STATE_ON, Platform
from homeassistant.helpers.update_coordinator import UpdateFailed
from pytest_homeassistant.common import MockConfigEntry

from custom_components.tplink_ac100.api import Ac100Api
from custom_components.tplink_ac100.const import CONF_ENABLE_CONTROL, DOMAIN
from custom_components.tplink_ac100.debug_logging import (
    LOGGER_NAME,
    Ac100DebugLogging,
    async_get_debug_logging,
)
from custom_components.tplink_ac100.models import AccessPoint, Controller, Snapshot
from custom_components.tplink_ac100.monitoring import ConfigurationState, MonitoringState
from custom_components.tplink_ac100.switch import Ac100DebugLoggingSwitch

pytestmark = pytest.mark.usefixtures("enable_custom_integrations")


@pytest.fixture
def local_logger():
    """Restore only levels deliberately changed by each test."""
    logger = logging.getLogger(LOGGER_NAME)
    root_logger = logging.getLogger()
    unrelated = logging.getLogger("custom_components.example_unrelated")
    owned = [logger]
    for parent in owned:
        owned.extend(parent.getChildren())
    previous = {item: item.level for item in [*owned, root_logger, unrelated]}
    logger.setLevel(logging.INFO)
    try:
        yield logger
    finally:
        current_owned = [logger]
        for parent in current_owned:
            current_owned.extend(parent.getChildren())
        for item in current_owned:
            item.setLevel(previous.get(item, logging.NOTSET))
        root_logger.setLevel(previous[root_logger])
        unrelated.setLevel(previous[unrelated])


@pytest.fixture
async def debug_controller_factory(hass, local_logger):
    """Set up switch entities without reaching or changing a physical device."""
    entries = []

    async def create(*, enable_control=False):
        index = len(entries) + 1
        mac = f"02:00:00:00:00:{index:02x}"
        entry = MockConfigEntry(
            domain=DOMAIN,
            unique_id=mac,
            title=f"Controller {index}",
            data={
                "host": f"http://192.0.2.{index}",
                "username": "private-username",
                "password": "private-password",
            },
            options={CONF_ENABLE_CONTROL: enable_control},
        )
        entry.add_to_hass(hass)
        snapshot = Snapshot(
            Controller(mac, "TL-AC100", "5.0", "1.0", {"0": 12.0}, 30.0, 3600),
            (
                AccessPoint(
                    "0",
                    "Private AP",
                    "TL-XAP1502GI-PoE",
                    f"02:00:00:00:01:{index:02x}",
                    "192.0.2.100",
                    "1",
                    True,
                    1800,
                    (),
                ),
            ),
            (),
        )
        api = AsyncMock(spec=Ac100Api)
        api.async_get_snapshot.return_value = snapshot
        api.async_get_monitoring.return_value = MonitoringState()
        api.async_get_configuration.return_value = ConfigurationState()
        with patch("custom_components.tplink_ac100.Ac100Api", return_value=api):
            assert await hass.config_entries.async_setup(entry.entry_id)
            await hass.async_block_till_done()
        entries.append(entry)
        return entry, entry.runtime_data, api

    with patch("custom_components.tplink_ac100.PLATFORMS", [Platform.SWITCH]):
        yield create
        for entry in reversed(entries):
            if entry.state is ConfigEntryState.LOADED:
                assert await hass.config_entries.async_unload(entry.entry_id)


def debug_entity_id(registry, entry):
    """Find the switch by stable controller identity, not its translated name."""
    result = registry.async_get_entity_id(
        "switch", DOMAIN, f"{entry.unique_id}_controller_debug_logging"
    )
    assert result is not None
    return result


async def set_debug(hass, entity_id, enabled):
    """Call the normal native HA switch service."""
    await hass.services.async_call(
        "switch", "turn_on" if enabled else "turn_off", {"entity_id": entity_id}, blocking=True
    )
    await hass.async_block_till_done()


async def test_debug_defaults_off_and_is_available_read_only(
    hass, debug_controller_factory, entity_registry, local_logger
):
    entry, _, api = await debug_controller_factory()
    entity_id = debug_entity_id(entity_registry, entry)
    entity = entity_registry.async_get(entity_id)
    assert entity.entity_category == "diagnostic"
    assert entity.disabled_by is None
    assert not entry.options[CONF_ENABLE_CONTROL]
    assert hass.states.get(entity_id).state == STATE_OFF
    assert local_logger.getEffectiveLevel() != logging.DEBUG
    assert len(hass.states.async_all("switch")) == 1
    api.async_get_snapshot.assert_awaited_once_with()
    api.async_get_monitoring.assert_awaited_once_with()
    api.async_get_configuration.assert_not_awaited()
    assert len(api.mock_calls) == 2


@pytest.mark.parametrize("previous_level", [logging.INFO, logging.WARNING, logging.NOTSET])
async def test_turn_on_off_changes_only_integration_namespace(
    hass, debug_controller_factory, entity_registry, local_logger, previous_level
):
    root_logger = logging.getLogger()
    unrelated = logging.getLogger("custom_components.example_unrelated")
    root_logger.setLevel(logging.WARNING)
    unrelated.setLevel(logging.ERROR)
    local_logger.setLevel(previous_level)
    entry, _, api = await debug_controller_factory()
    entity_id = debug_entity_id(entity_registry, entry)
    api.reset_mock()
    with patch.object(hass.config_entries, "async_reload", new_callable=AsyncMock) as reload:
        await set_debug(hass, entity_id, True)
        assert local_logger.getEffectiveLevel() == logging.DEBUG
        assert hass.states.get(entity_id).state == STATE_ON
        await set_debug(hass, entity_id, True)
        await set_debug(hass, entity_id, False)
        assert local_logger.level == previous_level
        assert hass.states.get(entity_id).state == STATE_OFF
        assert root_logger.level == logging.WARNING
        assert unrelated.level == logging.ERROR
        assert api.mock_calls == []
        reload.assert_not_called()


async def test_off_falls_back_to_info_if_parent_would_enable_debug(
    hass, debug_controller_factory, entity_registry, local_logger
):
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)
    local_logger.setLevel(logging.NOTSET)
    entry, _, _ = await debug_controller_factory()
    entity_id = debug_entity_id(entity_registry, entry)
    await set_debug(hass, entity_id, True)
    root_logger.setLevel(logging.DEBUG)
    await set_debug(hass, entity_id, False)
    assert local_logger.level == logging.INFO
    assert hass.states.get(entity_id).state == STATE_OFF


async def test_local_switch_works_offline_without_device_calls(
    hass, debug_controller_factory, entity_registry
):
    entry, coordinator, api = await debug_controller_factory()
    entity_id = debug_entity_id(entity_registry, entry)
    coordinator.async_set_update_error(UpdateFailed("Simulated controller outage"))
    await hass.async_block_till_done()
    assert hass.states.get(entity_id).state == STATE_OFF
    api.reset_mock()
    await set_debug(hass, entity_id, True)
    assert hass.states.get(entity_id).state == STATE_ON
    await set_debug(hass, entity_id, False)
    assert hass.states.get(entity_id).state == STATE_OFF
    assert api.mock_calls == []


async def test_multiple_entries_share_logging_state(
    hass, debug_controller_factory, entity_registry
):
    first, _, first_api = await debug_controller_factory()
    first_id = debug_entity_id(entity_registry, first)
    await set_debug(hass, first_id, True)
    second, _, second_api = await debug_controller_factory()
    second_id = debug_entity_id(entity_registry, second)
    assert hass.states.get(second_id).state == STATE_ON
    first_api.reset_mock()
    second_api.reset_mock()
    await set_debug(hass, second_id, False)
    assert hass.states.get(first_id).state == STATE_OFF
    assert hass.states.get(second_id).state == STATE_OFF
    await set_debug(hass, first_id, True)
    assert hass.states.get(first_id).state == STATE_ON
    assert hass.states.get(second_id).state == STATE_ON
    assert first_api.mock_calls == []
    assert second_api.mock_calls == []


async def test_switch_is_independent_of_device_control_option(
    hass, debug_controller_factory, entity_registry
):
    entry, _, api = await debug_controller_factory(enable_control=True)
    entity_id = debug_entity_id(entity_registry, entry)
    assert len(hass.states.async_all("switch")) == 2
    api.reset_mock()
    await set_debug(hass, entity_id, True)
    await set_debug(hass, entity_id, False)
    assert api.mock_calls == []


async def test_actual_level_is_read_and_new_runtime_does_not_restore_debug(
    hass, debug_controller_factory, local_logger
):
    _, coordinator, _ = await debug_controller_factory()
    debug_logging = async_get_debug_logging(hass)
    switch = Ac100DebugLoggingSwitch(coordinator, debug_logging)
    local_logger.setLevel(logging.DEBUG)
    assert switch.is_on
    local_logger.setLevel(logging.WARNING)
    assert not switch.is_on
    debug_logging.async_set_enabled(True)
    assert switch.is_on
    fresh_runtime = Ac100DebugLogging(hass)
    assert not fresh_runtime.is_on
    assert local_logger.level == logging.INFO


@pytest.mark.parametrize("suffix", ["api", "api.nested.security_check"])
async def test_default_and_explicit_off_disable_child_debug(
    hass, debug_controller_factory, entity_registry, local_logger, suffix
):
    """An explicit child level cannot keep DEBUG active behind an off switch."""
    if "." in suffix:
        logging.getLogger(f"{LOGGER_NAME}.{suffix.rsplit('.', 1)[0]}")
    child = logging.getLogger(f"{LOGGER_NAME}.{suffix}")
    child.setLevel(logging.DEBUG)
    entry, _, _ = await debug_controller_factory()
    entity_id = debug_entity_id(entity_registry, entry)
    assert hass.states.get(entity_id).state == STATE_OFF
    assert not child.isEnabledFor(logging.DEBUG)
    child.setLevel(logging.DEBUG)
    assert async_get_debug_logging(hass).is_on
    await set_debug(hass, entity_id, False)
    assert hass.states.get(entity_id).state == STATE_OFF
    assert not child.isEnabledFor(logging.DEBUG)
    assert not async_get_debug_logging(hass).is_on


async def test_child_info_is_enabled_then_restored_without_changing_other_namespaces(
    hass, debug_controller_factory, entity_registry, local_logger
):
    child = logging.getLogger(f"{LOGGER_NAME}.api")
    unrelated = logging.getLogger("custom_components.example_unrelated")
    root_logger = logging.getLogger()
    child.setLevel(logging.INFO)
    unrelated.setLevel(logging.ERROR)
    root_before = root_logger.level
    entry, _, _ = await debug_controller_factory()
    entity_id = debug_entity_id(entity_registry, entry)
    await set_debug(hass, entity_id, True)
    await set_debug(hass, entity_id, True)
    assert child.level == logging.DEBUG
    assert child.isEnabledFor(logging.DEBUG)
    await set_debug(hass, entity_id, False)
    assert child.level == logging.INFO
    assert not child.isEnabledFor(logging.DEBUG)
    assert unrelated.level == logging.ERROR
    assert root_logger.level == root_before


async def test_off_disables_child_created_after_enable(
    hass, debug_controller_factory, entity_registry, local_logger
):
    entry, _, _ = await debug_controller_factory()
    entity_id = debug_entity_id(entity_registry, entry)
    await set_debug(hass, entity_id, True)
    child = logging.getLogger(f"{LOGGER_NAME}.api.created_after_enable")
    child.setLevel(logging.DEBUG)
    assert async_get_debug_logging(hass).is_on
    await set_debug(hass, entity_id, False)
    assert not child.isEnabledFor(logging.DEBUG)
    assert not async_get_debug_logging(hass).is_on


async def test_debug_toggle_does_not_log_credentials_or_network_data(
    hass, debug_controller_factory, entity_registry, caplog
):
    entry, coordinator, api = await debug_controller_factory()
    entity_id = debug_entity_id(entity_registry, entry)
    api.reset_mock()
    caplog.clear()
    await set_debug(hass, entity_id, True)
    await set_debug(hass, entity_id, False)
    messages = "\n".join(
        record.getMessage() for record in caplog.records if record.name.startswith(LOGGER_NAME)
    )
    private = [
        *entry.data.values(),
        entry.unique_id,
        coordinator.data.aps[0].name,
        coordinator.data.aps[0].ip,
        coordinator.data.aps[0].mac,
    ]
    assert all(value not in messages for value in private)
    assert api.mock_calls == []
