"""Independent scheduling and failures of credential-free optional read tiers."""

from dataclasses import replace
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.exceptions import ConfigEntryError, ConfigEntryNotReady
from homeassistant.helpers.update_coordinator import UpdateFailed
from pytest_homeassistant.common import MockConfigEntry

from custom_components.tplink_ac100.api import (
    Ac100Api,
    Ac100AuthError,
    Ac100ConnectionError,
    Ac100SessionBusyError,
    Ac100SessionExpiredError,
)
from custom_components.tplink_ac100.const import DOMAIN
from custom_components.tplink_ac100.coordinator import CONFIGURATION_CONTEXT
from custom_components.tplink_ac100.models import Controller, Snapshot
from custom_components.tplink_ac100.monitoring import ConfigurationState, MonitoringState

pytestmark = pytest.mark.usefixtures("enable_custom_integrations")


@pytest.fixture
async def tier_setup(hass):
    """Load the real coordinator but no entity platforms or physical device."""
    clock = [100.0]
    snapshot = Snapshot(
        Controller("02:00:00:00:00:01", "TL-AC100", "5.0", "1.0.5", {}, 30.0, 3600),
        (),
        (),
    )
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=snapshot.controller.mac,
        data={"host": "http://192.0.2.1", "username": "example", "password": "example-only"},
    )
    entry.add_to_hass(hass)
    api = AsyncMock(spec=Ac100Api)
    api.async_get_snapshot.return_value = snapshot
    api.async_get_monitoring.return_value = MonitoringState(
        values={"configuration_not_saved": True},
        available_sections=frozenset({"system.cfg_status"}),
    )
    api.async_get_configuration.return_value = ConfigurationState()
    with (
        patch("custom_components.tplink_ac100.Ac100Api", return_value=api),
        patch("custom_components.tplink_ac100.PLATFORMS", []),
        patch("custom_components.tplink_ac100.coordinator.monotonic", side_effect=lambda: clock[0]),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        coordinator = entry.runtime_data
        yield coordinator, api, clock, snapshot
        assert await hass.config_entries.async_unload(entry.entry_id)


def use_settings(coordinator):
    """Stand in for an enabled settings entity."""
    return coordinator.async_add_listener(lambda: None, CONFIGURATION_CONTEXT)


async def test_settings_are_not_read_without_an_enabled_settings_entity(tier_setup):
    coordinator, api, clock, _ = tier_setup
    for now in (160, 400, 2000, 2001):
        clock[0] = now
        await coordinator.async_refresh()
    api.async_get_configuration.assert_not_awaited()
    assert coordinator.data.configuration is None


async def test_staggered_tiers_no_catch_up_burst(tier_setup):
    coordinator, api, clock, _ = tier_setup
    use_settings(coordinator)
    api.async_get_monitoring.assert_awaited_once_with()
    api.async_get_configuration.assert_not_awaited()
    clock[0] = 159
    await coordinator.async_refresh()
    api.async_get_monitoring.assert_awaited_once_with()
    api.async_get_configuration.assert_not_awaited()
    clock[0] = 160
    await coordinator.async_refresh()
    api.async_get_configuration.assert_awaited_once_with()
    clock[0] = 400
    await coordinator.async_refresh()
    assert api.async_get_monitoring.await_count == 2
    api.async_get_configuration.assert_awaited_once_with()
    clock[0] = 2000
    await coordinator.async_refresh()
    assert api.async_get_monitoring.await_count == 3
    api.async_get_configuration.assert_awaited_once_with()
    clock[0] = 2001
    await coordinator.async_refresh()
    assert api.async_get_configuration.await_count == 2


@pytest.mark.parametrize(
    "error", [Ac100ConnectionError("offline"), Ac100AuthError("not permitted for this source")]
)
async def test_optional_failure_keeps_core_and_drops_cached_readings(tier_setup, error, caplog):
    coordinator, api, clock, _ = tier_setup
    original = coordinator.data.monitoring
    api.async_get_monitoring.side_effect = error
    for now in (400, 700):
        clock[0] = now
        await coordinator.async_refresh()
        assert coordinator.last_update_success
        assert coordinator.data.monitoring is None
    assert caplog.text.count("Controller monitoring data is unavailable") == 1
    api.async_get_monitoring.side_effect = None
    clock[0] = 1000
    await coordinator.async_refresh()
    assert coordinator.data.monitoring == original
    assert "Controller monitoring data is available again" in caplog.text


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (Ac100SessionBusyError("busy"), ConfigEntryNotReady),
        (Ac100SessionExpiredError("expired"), UpdateFailed),
    ],
)
async def test_session_errors_propagate_and_retry_on_the_next_poll(tier_setup, error, expected):
    coordinator, api, clock, _ = tier_setup
    api.async_get_monitoring.side_effect = error
    clock[0] = 400
    with pytest.raises(expected):
        await coordinator._async_update_data()
    api.async_get_monitoring.side_effect = None
    clock[0] = 460
    await coordinator.async_refresh()
    assert api.async_get_monitoring.await_count == 3


async def test_identity_checked_before_any_optional_reads(tier_setup):
    coordinator, api, clock, snapshot = tier_setup
    api.async_get_snapshot.return_value = replace(
        snapshot, controller=replace(snapshot.controller, mac="02:00:00:00:00:99")
    )
    clock[0] = 400
    with pytest.raises(ConfigEntryError):
        await coordinator._async_update_data()
    api.async_get_monitoring.assert_awaited_once_with()
