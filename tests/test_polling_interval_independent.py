"""Independent regression for serialized refreshes at the maximum interval."""

import asyncio
import json
from unittest.mock import AsyncMock, patch

import probatio
import pytest
from homeassistant.const import CONF_SCAN_INTERVAL
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.translation import async_get_translations
from pytest_homeassistant.common import MockConfigEntry

from custom_components.tplink_ac100.api import Ac100Api
from custom_components.tplink_ac100.const import DOMAIN
from custom_components.tplink_ac100.coordinator import CONFIGURATION_CONTEXT
from custom_components.tplink_ac100.models import Controller, Snapshot
from custom_components.tplink_ac100.monitoring import ConfigurationState, MonitoringState

pytestmark = pytest.mark.usefixtures("enable_custom_integrations")


async def test_interval_selector_serializes_for_native_options_form(hass):
    """The same serializer as HA's flow API exposes a seconds box, not a slider."""
    entry = MockConfigEntry(domain=DOMAIN, options={CONF_SCAN_INTERVAL: 120})
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    fields = probatio.to_field_list(result["data_schema"], custom_serializer=cv.custom_serializer)
    fields = json.loads(json.dumps(fields))
    interval = next(field for field in fields if field["name"] == CONF_SCAN_INTERVAL)
    assert interval["selector"]["number"] == {
        "min": 10,
        "max": 3600,
        "step": 1,
        "unit_of_measurement": "s",
        "mode": "box",
    }
    assert interval["default"] == 120
    chinese_options = await async_get_translations(hass, "zh-Hans", "options", [DOMAIN])
    english_options = await async_get_translations(hass, "en", "options", [DOMAIN])
    for field in fields:
        key = f"component.{DOMAIN}.options.step.init.data.{field['name']}"
        assert chinese_options[key] != english_options[key]
    chinese_config = await async_get_translations(hass, "zh-Hans", "config", [DOMAIN])
    english_config = await async_get_translations(hass, "en", "config", [DOMAIN])
    assert chinese_config == english_config
    assert chinese_config


async def test_hourly_concurrent_refreshes_do_not_overlap_or_starve_settings(hass):
    """A queued cycle waits for the current read, then services the other due tier."""
    clock = [100.0]
    snapshot = Snapshot(
        Controller("02:00:00:00:00:01", "TL-AC100", "test", "test", {}, 0.0, 0),
        (),
        (),
    )
    monitoring = MonitoringState(values={}, available_sections=frozenset())
    configuration = ConfigurationState()
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=snapshot.controller.mac,
        data={"host": "http://192.0.2.1", "username": "example", "password": "example-only"},
        options={CONF_SCAN_INTERVAL: 3600.0},
    )
    entry.add_to_hass(hass)
    api = AsyncMock(spec=Ac100Api)
    api.async_get_snapshot.return_value = snapshot
    api.async_get_monitoring.return_value = monitoring
    api.async_get_configuration.return_value = configuration
    with (
        patch("custom_components.tplink_ac100.Ac100Api", return_value=api),
        patch("custom_components.tplink_ac100.PLATFORMS", []),
        patch("custom_components.tplink_ac100.coordinator.monotonic", side_effect=lambda: clock[0]),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        coordinator = entry.runtime_data
        coordinator.async_add_listener(lambda: None, CONFIGURATION_CONTEXT)
        clock[0] = 7300.0
        calls = []
        monitoring_started = asyncio.Event()
        release_monitoring = asyncio.Event()
        second_queued = asyncio.Event()

        async def read_snapshot():
            calls.append("core")
            return snapshot

        async def read_monitoring(**kwargs):
            calls.append("monitoring_start")
            monitoring_started.set()
            await release_monitoring.wait()
            calls.append("monitoring_end")
            return monitoring

        async def read_configuration():
            calls.append("configuration")
            return configuration

        async def second_refresh():
            second_queued.set()
            await coordinator.async_refresh()

        api.async_get_snapshot.side_effect = read_snapshot
        api.async_get_monitoring.side_effect = read_monitoring
        api.async_get_configuration.side_effect = read_configuration
        first = asyncio.create_task(coordinator.async_refresh())
        await monitoring_started.wait()
        second = asyncio.create_task(second_refresh())
        await second_queued.wait()
        try:
            assert calls == ["core", "monitoring_start"]
            assert not second.done()
        finally:
            release_monitoring.set()
            await asyncio.gather(first, second)
        assert calls == ["core", "monitoring_start", "monitoring_end", "core", "configuration"]
        assert api.async_get_snapshot.await_count == 3
        assert api.async_get_monitoring.await_count == 2
        api.async_get_configuration.assert_awaited_once_with()
        assert coordinator.data.configuration == configuration
        assert await hass.config_entries.async_unload(entry.entry_id)
