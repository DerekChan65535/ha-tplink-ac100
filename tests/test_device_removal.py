"""Users may delete stale devices, never ones the controller still reports."""

from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.helpers import device_registry as dr
from pytest_homeassistant.common import MockConfigEntry

from custom_components.tplink_ac100 import async_remove_config_entry_device
from custom_components.tplink_ac100.api import Ac100Api
from custom_components.tplink_ac100.const import DOMAIN
from custom_components.tplink_ac100.models import AccessPoint, Controller, Snapshot
from custom_components.tplink_ac100.monitoring import ConfigurationState, MonitoringState

pytestmark = pytest.mark.usefixtures("enable_custom_integrations")

AP = AccessPoint(
    "0", "Example AP", "Example model", "02:00:00:00:00:02", "192.0.2.2", "1", True, 1, ()
)


async def test_only_unreported_devices_can_be_removed(hass, device_registry):
    snapshot = Snapshot(
        Controller("02:00:00:00:00:01", "TL-AC100", "5.0", "1", {}, 1.0, 1), (AP,), ()
    )
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=snapshot.controller.mac,
        data={"host": "http://192.0.2.1", "username": "example", "password": "example-only"},
    )
    entry.add_to_hass(hass)
    api = AsyncMock(spec=Ac100Api)
    api.async_get_snapshot.return_value = snapshot
    api.async_get_monitoring.return_value = MonitoringState()
    api.async_get_configuration.return_value = ConfigurationState()
    with (
        patch("custom_components.tplink_ac100.Ac100Api", return_value=api),
        patch("custom_components.tplink_ac100.PLATFORMS", []),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
    stale = device_registry.async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={(DOMAIN, "02:00:00:00:00:09")}
    )
    controller = device_registry.async_get_device_by_identifier(
        (DOMAIN, snapshot.controller.mac), entry.entry_id
    )
    access_point = device_registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, AP.mac)},
        connections={(dr.CONNECTION_NETWORK_MAC, AP.mac)},
    )
    assert await async_remove_config_entry_device(hass, entry, stale)
    assert not await async_remove_config_entry_device(hass, entry, controller)
    assert not await async_remove_config_entry_device(hass, entry, access_point)
    assert await hass.config_entries.async_unload(entry.entry_id)
