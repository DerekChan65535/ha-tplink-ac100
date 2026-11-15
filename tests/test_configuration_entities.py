"""Configuration catalog drives read-only diagnostic entities, disabled by default."""

from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.const import STATE_ON, STATE_UNAVAILABLE, STATE_UNKNOWN, Platform
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant.common import MockConfigEntry

from custom_components.tplink_ac100.api import Ac100Api
from custom_components.tplink_ac100.const import DOMAIN
from custom_components.tplink_ac100.coordinator import CONFIGURATION_CONTEXT
from custom_components.tplink_ac100.models import Controller, Snapshot
from custom_components.tplink_ac100.monitoring import (
    ConfigField,
    ConfigurationState,
    MonitoringState,
)

pytestmark = pytest.mark.usefixtures("enable_custom_integrations")

FIELDS = (
    ConfigField("test_switch", "example", "settings", "enable", "bool"),
    ConfigField(
        "test_mode",
        "example",
        "settings",
        "mode",
        "enum",
        mapping=(("1", "automatic"), ("2", "manual")),
    ),
    ConfigField("test_limit", "example", "limits", "limit", "number", "min"),
)


@pytest.fixture
async def configuration_setup(hass):
    snapshot = Snapshot(
        Controller("02:00:00:00:00:01", "TL-AC100", "5.0", "1.0", {}, 1.0, 1), (), ()
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
        patch("custom_components.tplink_ac100.configuration_entity.CONFIGURATION_FIELDS", FIELDS),
        patch(
            "custom_components.tplink_ac100.PLATFORMS", [Platform.SENSOR, Platform.BINARY_SENSOR]
        ),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        yield entry, entry.runtime_data, snapshot
        assert await hass.config_entries.async_unload(entry.entry_id)


def registry_id(registry, entry, domain, key):
    return registry.async_get_entity_id(domain, DOMAIN, f"{entry.unique_id}_controller_{key}")


async def test_settings_entities_start_disabled_and_settings_are_not_read(
    hass, configuration_setup, entity_registry
):
    entry, coordinator, _ = configuration_setup
    rows = er.async_entries_for_config_entry(entity_registry, entry.entry_id)
    configured = [
        row for row in rows if row.unique_id.endswith(("test_switch", "test_mode", "test_limit"))
    ]
    assert len(configured) == 3
    assert all(row.disabled_by is er.RegistryEntryDisabler.INTEGRATION for row in configured)
    assert all(row.entity_category == "diagnostic" for row in configured)
    assert {row.domain for row in configured} == {"binary_sensor", "sensor"}
    assert CONFIGURATION_CONTEXT not in set(coordinator.async_contexts())


@pytest.mark.usefixtures("entity_registry_enabled_by_default")
async def test_configuration_values_unknowns_and_failed_source(
    hass, configuration_setup, entity_registry
):
    entry, coordinator, snapshot = configuration_setup
    assert CONFIGURATION_CONTEXT in set(coordinator.async_contexts())
    state = ConfigurationState(
        values={"test_switch": True, "test_mode": None, "test_limit": 6},
        available_sections=frozenset({"example.settings", "example.limits"}),
    )
    coordinator.async_set_updated_data(Snapshot(snapshot.controller, (), (), configuration=state))
    await hass.async_block_till_done()
    switch = hass.states.get(registry_id(entity_registry, entry, "binary_sensor", "test_switch"))
    mode = hass.states.get(registry_id(entity_registry, entry, "sensor", "test_mode"))
    limit = hass.states.get(registry_id(entity_registry, entry, "sensor", "test_limit"))
    assert switch.state == STATE_ON
    assert mode.state == STATE_UNKNOWN
    assert mode.attributes["options"] == ["automatic", "manual"]
    assert limit.state == "6"
    assert limit.attributes["unit_of_measurement"] == "min"
    coordinator.async_set_updated_data(
        Snapshot(
            snapshot.controller,
            (),
            (),
            configuration=ConfigurationState(values=state.values, available_sections=frozenset()),
        )
    )
    await hass.async_block_till_done()
    assert hass.states.get(switch.entity_id).state == STATE_UNAVAILABLE
    assert hass.states.get(limit.entity_id).state == STATE_UNAVAILABLE


def test_every_configuration_field_has_english_translations():
    """Catalog keys are translation keys; enum states must have readable names."""
    import json
    from pathlib import Path

    from custom_components.tplink_ac100.monitoring import CONFIGURATION_FIELDS

    component = Path(__file__).parents[1] / "custom_components" / "tplink_ac100"
    strings = json.loads((component / "strings.json").read_text())
    assert json.loads((component / "translations" / "en.json").read_text()) == strings
    for field in CONFIGURATION_FIELDS:
        platform = "binary_sensor" if field.kind == "bool" else "sensor"
        translation = strings["entity"][platform][field.key]
        assert translation["name"]
        if field.kind == "enum":
            assert set(translation["state"]) == set(field.options)
