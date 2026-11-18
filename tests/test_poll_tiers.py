"""Independent scheduling and failures of credential-free optional read tiers."""

from dataclasses import replace
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.const import CONF_SCAN_INTERVAL
from homeassistant.exceptions import ConfigEntryError, ConfigEntryNotReady, HomeAssistantError
from homeassistant.helpers.update_coordinator import UpdateFailed
from pytest_homeassistant.common import MockConfigEntry

from custom_components.tplink_ac100.api import (
    Ac100Api,
    Ac100AuthError,
    Ac100ConnectionError,
    Ac100SessionBusyError,
    Ac100SessionExpiredError,
    Ac100TerminalError,
    Ac100TerminalPartialError,
)
from custom_components.tplink_ac100.const import DOMAIN
from custom_components.tplink_ac100.coordinator import CONFIGURATION_CONTEXT
from custom_components.tplink_ac100.models import Controller, Snapshot
from custom_components.tplink_ac100.monitoring import ConfigurationState, MonitoringState

pytestmark = pytest.mark.usefixtures("enable_custom_integrations")


@pytest.fixture
async def tier_setup(hass, request):
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
        options={CONF_SCAN_INTERVAL: request.param} if hasattr(request, "param") else {},
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
    api.async_get_monitoring.assert_awaited_once_with(wireless_services=False)
    api.async_get_configuration.assert_not_awaited()
    clock[0] = 109
    await coordinator.async_refresh()
    api.async_get_monitoring.assert_awaited_once_with(wireless_services=False)
    api.async_get_configuration.assert_not_awaited()
    clock[0] = 110
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


@pytest.mark.parametrize("tier_setup", [10, 75, 3600, 10.5], indirect=True)
async def test_selected_interval_sets_polling_and_initial_settings_stagger(tier_setup):
    coordinator, _, _, _ = tier_setup
    interval = coordinator.config_entry.options[CONF_SCAN_INTERVAL]
    assert coordinator.update_interval.total_seconds() == interval
    assert coordinator._next_configuration == 100 + interval


@pytest.mark.parametrize(
    ("tier_setup", "expected"),
    [
        (
            300,
            [
                "monitoring",
                "configuration",
                "monitoring",
                "monitoring",
                "monitoring",
                "configuration",
            ],
        ),
        (900, ["monitoring", "configuration"] * 3),
        (3600, ["monitoring", "configuration"] * 3),
    ],
    indirect=["tier_setup"],
)
async def test_slow_intervals_do_not_starve_settings(tier_setup, expected):
    """Each poll runs the oldest due tier, with monitoring winning deadline ties."""
    coordinator, api, clock, _ = tier_setup
    use_settings(coordinator)
    interval = coordinator.update_interval.total_seconds()
    observed = []
    for step in range(1, 7):
        monitoring_before = api.async_get_monitoring.await_count
        configuration_before = api.async_get_configuration.await_count
        clock[0] = 100 + step * interval
        await coordinator.async_refresh()
        monitoring_reads = api.async_get_monitoring.await_count - monitoring_before
        configuration_reads = api.async_get_configuration.await_count - configuration_before
        assert monitoring_reads + configuration_reads == 1
        observed.append("monitoring" if monitoring_reads else "configuration")
    assert observed == expected


@pytest.mark.parametrize("tier_setup", [3600], indirect=True)
async def test_failed_optional_reads_advance_only_the_attempted_tier(tier_setup):
    """A failed source does not consume the other tier's turn or block core updates."""
    coordinator, api, clock, _ = tier_setup
    use_settings(coordinator)
    api.async_get_monitoring.side_effect = Ac100ConnectionError("Monitoring unavailable")
    api.async_get_configuration.side_effect = Ac100ConnectionError("Settings unavailable")
    clock[0] = 3700
    await coordinator.async_refresh()
    assert coordinator.last_update_success
    assert coordinator.data.monitoring is None
    assert coordinator._next_monitoring == 4000
    assert coordinator._next_configuration == 3700
    api.async_get_configuration.assert_not_awaited()
    clock[0] = 7300
    await coordinator.async_refresh()
    assert coordinator.last_update_success
    assert coordinator.data.configuration is None
    assert coordinator._next_monitoring == 4000
    assert coordinator._next_configuration == 8200
    assert api.async_get_monitoring.await_count == 2
    api.async_get_configuration.assert_awaited_once_with()
    api.async_get_monitoring.side_effect = None
    api.async_get_configuration.side_effect = None
    clock[0] = 10900
    await coordinator.async_refresh()
    assert coordinator.data.monitoring is not None
    assert api.async_get_monitoring.await_count == 3
    api.async_get_configuration.assert_awaited_once_with()
    clock[0] = 14500
    await coordinator.async_refresh()
    assert coordinator.data.configuration is not None
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
    api.async_get_monitoring.assert_awaited_once_with(wireless_services=False)


async def test_management_action_refreshes_both_optional_reads(hass, tier_setup):
    from custom_components.tplink_ac100.const import CONF_ENABLE_CONTROL

    coordinator, api, clock, _ = tier_setup
    use_settings(coordinator)
    hass.config_entries.async_update_entry(
        coordinator.config_entry, options={CONF_ENABLE_CONTROL: True}
    )
    clock[0] = 130
    action = AsyncMock()
    await coordinator.async_control(action)
    action.assert_awaited_once_with()
    assert api.async_get_monitoring.await_count == 2
    api.async_get_configuration.assert_awaited_once_with()


@pytest.mark.parametrize(
    ("error", "translation_key"),
    [
        (Ac100TerminalError("The terminal state changed"), "terminal_action_refused"),
        (
            Ac100TerminalPartialError(
                "The schedule was created but not linked", "schedule_created"
            ),
            "terminal_action_partial",
        ),
    ],
)
async def test_terminal_failure_is_visible_and_forces_next_full_read_without_replay(
    hass, tier_setup, error, translation_key
):
    from custom_components.tplink_ac100.const import CONF_ENABLE_CONTROL

    coordinator, api, clock, _ = tier_setup
    use_settings(coordinator)
    hass.config_entries.async_update_entry(
        coordinator.config_entry, options={CONF_ENABLE_CONTROL: True}
    )
    action = AsyncMock(side_effect=error)
    reads = api.async_get_snapshot.await_count
    with pytest.raises(HomeAssistantError) as raised:
        await coordinator.async_control(action)
    assert raised.value.translation_key == translation_key
    assert raised.value.translation_placeholders == {"reason": str(error)}
    assert coordinator.last_update_success
    assert coordinator._refresh_all
    action.assert_awaited_once_with()
    assert api.async_get_snapshot.await_count == reads
    api.async_get_configuration.assert_not_awaited()
    clock[0] = 130
    await coordinator.async_refresh()
    assert api.async_get_snapshot.await_count == reads + 1
    assert api.async_get_monitoring.await_count == 2
    api.async_get_configuration.assert_awaited_once_with()
    assert not coordinator._refresh_all
