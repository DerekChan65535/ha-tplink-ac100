"""Per-radio and IP address sensors of access points on the Home Assistant test runtime."""

from dataclasses import replace
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN, EntityCategory, Platform
from homeassistant.helpers import entity_platform
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant.common import MockConfigEntry

from custom_components.tplink_ac100.api import Ac100Api
from custom_components.tplink_ac100.const import DOMAIN
from custom_components.tplink_ac100.models import AccessPoint, ApRadio, Controller, Snapshot
from custom_components.tplink_ac100.monitoring import ConfigurationState, MonitoringState

pytestmark = pytest.mark.usefixtures("enable_custom_integrations")

METRICS = ("connected_clients", "channel", "channel_load")


def make_ap(index, status="1", ip=None, radios=None):
    """Build an AP that keeps non-zero readings even while it is not online."""
    return AccessPoint(
        str(index),
        f"AP {index}",
        "TL-XAP1502GI-PoE",
        f"02:00:00:00:00:{index + 30:02x}",
        f"192.0.2.{index + 30}" if ip is None else ip,
        status,
        True,
        3600,
        radios or (ApRadio("1", "2.4GHz", 2, 6, 18.0), ApRadio("2", "5GHz", 3, 36, 25.0)),
    )


@pytest.fixture
def radio_snapshot():
    """One online AP and one connecting AP, both with dual-band readings."""
    return Snapshot(
        Controller("02:00:00:00:00:10", "TL-AC100", "5.0", "1.0", {"core1": 1.0}, 40.0, 7200),
        (make_ap(0), make_ap(1, status="0")),
        (),
    )


@pytest.fixture
async def radio_setup(hass, radio_snapshot):
    """Load only the sensor platform; never call the physical device."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=radio_snapshot.controller.mac,
        data={"host": "http://192.0.2.1", "username": "example", "password": "example-only"},
    )
    entry.add_to_hass(hass)
    api = AsyncMock(spec=Ac100Api)
    api.async_get_snapshot.return_value = radio_snapshot
    api.async_get_monitoring.return_value = MonitoringState()
    api.async_get_configuration.return_value = ConfigurationState()
    with (
        patch("custom_components.tplink_ac100.Ac100Api", return_value=api),
        patch("custom_components.tplink_ac100.PLATFORMS", [Platform.SENSOR]),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        yield entry, entry.runtime_data
        assert await hass.config_entries.async_unload(entry.entry_id)


def unique_id(snapshot, ap, key):
    return f"{snapshot.controller.mac}_{ap.mac}_{key}"


def entity_id(registry, snapshot, ap, key):
    """Resolve an AP entity by its stable AP MAC and key."""
    result = registry.async_get_entity_id("sensor", DOMAIN, unique_id(snapshot, ap, key))
    assert result is not None, key
    return result


def radio_id(registry, snapshot, ap, unit, metric):
    return entity_id(registry, snapshot, ap, f"radio_{unit}_{metric}")


def radio_state(hass, registry, snapshot, ap, unit, metric):
    state = hass.states.get(radio_id(registry, snapshot, ap, unit, metric))
    assert state is not None
    return state


def sensor_rows(registry, entry):
    return {
        row.unique_id: row
        for row in er.async_entries_for_config_entry(registry, entry.entry_id)
        if row.domain == "sensor"
    }


def entity_object(hass, entity_id):
    for platform in entity_platform.async_get_platforms(hass, DOMAIN):
        if entity := platform.entities.get(entity_id):
            return entity
    raise AssertionError(f"{entity_id} is not loaded")


async def update(hass, coordinator, snapshot):
    coordinator.async_set_updated_data(snapshot)
    await hass.async_block_till_done()


async def test_each_radio_has_its_own_sensors_keyed_by_unit(
    hass, radio_setup, radio_snapshot, entity_registry
):
    """Connected clients, channel and channel load exist per radio with the right metadata."""
    entry, _ = radio_setup
    ap = radio_snapshot.aps[0]
    expected = {
        ("1", "connected_clients"): "2",
        ("1", "channel"): "6",
        ("1", "channel_load"): "18.0",
        ("2", "connected_clients"): "3",
        ("2", "channel"): "36",
        ("2", "channel_load"): "25.0",
    }
    for (unit, metric), value in expected.items():
        assert radio_state(hass, entity_registry, radio_snapshot, ap, unit, metric).state == value
    rows = sensor_rows(entity_registry, entry)
    for unit in ("1", "2"):
        clients = radio_state(hass, entity_registry, radio_snapshot, ap, unit, "connected_clients")
        assert clients.attributes["state_class"] == "measurement"
        assert "unit_of_measurement" not in clients.attributes
        channel = radio_state(hass, entity_registry, radio_snapshot, ap, unit, "channel")
        assert "state_class" not in channel.attributes
        assert "unit_of_measurement" not in channel.attributes
        load = radio_state(hass, entity_registry, radio_snapshot, ap, unit, "channel_load")
        assert load.attributes["state_class"] == "measurement"
        assert load.attributes["unit_of_measurement"] == "%"
        for metric, category in (
            ("connected_clients", None),
            ("channel", EntityCategory.DIAGNOSTIC),
            ("channel_load", EntityCategory.DIAGNOSTIC),
        ):
            row = rows[unique_id(radio_snapshot, ap, f"radio_{unit}_{metric}")]
            assert row.entity_category is category
            assert row.disabled_by is None
    total = hass.states.get(entity_id(entity_registry, radio_snapshot, ap, "connected_clients"))
    assert total.state == "5"


async def test_radio_identity_follows_unit_not_list_order_or_band_label(
    hass, radio_setup, radio_snapshot, entity_registry
):
    """Reordering radios or relabelling their band moves values, never entities."""
    entry, coordinator = radio_setup
    ap = radio_snapshot.aps[0]
    before = {
        (unit, metric): radio_id(entity_registry, radio_snapshot, ap, unit, metric)
        for unit in ("1", "2")
        for metric in METRICS
    }
    count = len(er.async_entries_for_config_entry(entity_registry, entry.entry_id))
    changed = replace(
        ap,
        radios=(
            ApRadio("2", "5 GHz", 7, 149, 41.0),
            ApRadio("1", "2.4 GHz", 1, 11, 9.0),
        ),
    )
    snapshot = replace(radio_snapshot, aps=(changed, radio_snapshot.aps[1]))
    await update(hass, coordinator, snapshot)
    assert {
        (unit, metric): radio_id(entity_registry, snapshot, ap, unit, metric)
        for unit in ("1", "2")
        for metric in METRICS
    } == before
    for unit, expected in (("1", ("1", "11", "9.0")), ("2", ("7", "149", "41.0"))):
        values = tuple(
            radio_state(hass, entity_registry, snapshot, ap, unit, metric).state
            for metric in METRICS
        )
        assert values == expected
    assert len(er.async_entries_for_config_entry(entity_registry, entry.entry_id)) == count


async def test_radios_and_aps_first_seen_later_are_added_once(
    hass, radio_setup, radio_snapshot, entity_registry, caplog
):
    """A third radio on a known AP and a whole new AP are discovered by a later poll."""
    entry, coordinator = radio_setup
    ap = radio_snapshot.aps[0]
    third = ApRadio("3", "5GHz", 1, 100, 5.0)
    new_ap = make_ap(2, radios=(third,))
    snapshot = replace(
        radio_snapshot,
        aps=(replace(ap, radios=(*ap.radios, third)), radio_snapshot.aps[1], new_ap),
    )
    known = set(sensor_rows(entity_registry, entry))
    await update(hass, coordinator, snapshot)
    added = set(sensor_rows(entity_registry, entry)) - known
    for owner in (ap, new_ap):
        for metric in METRICS:
            assert unique_id(snapshot, owner, f"radio_3_{metric}") in added
            assert radio_state(hass, entity_registry, snapshot, owner, "3", metric).state != (
                STATE_UNAVAILABLE
            )
    assert unique_id(snapshot, new_ap, "ip_address") in added
    assert not any("radio_1_" in identity or "radio_2_" in identity for identity in added)
    known = set(sensor_rows(entity_registry, entry))
    await update(hass, coordinator, snapshot)
    assert set(sensor_rows(entity_registry, entry)) == known
    assert "does not generate unique IDs" not in caplog.text


@pytest.mark.parametrize("status", ["0", "2"])
async def test_radio_sensors_of_a_disconnected_ap_are_unavailable(
    hass, radio_setup, radio_snapshot, entity_registry, status
):
    """Connecting and offline APs keep old readings on the controller; HA must not show them."""
    _, coordinator = radio_setup
    ap = replace(radio_snapshot.aps[1], link_status=status)
    snapshot = replace(radio_snapshot, aps=(ap,))
    await update(hass, coordinator, snapshot)
    for unit in ("1", "2"):
        for metric in METRICS:
            state = radio_state(hass, entity_registry, snapshot, ap, unit, metric)
            assert state.state == STATE_UNAVAILABLE
    await update(hass, coordinator, replace(snapshot, aps=(replace(ap, link_status="1"),)))
    assert radio_state(hass, entity_registry, snapshot, ap, "2", "channel").state == "36"
    await update(hass, coordinator, snapshot)
    assert radio_state(hass, entity_registry, snapshot, ap, "2", "channel").state == (
        STATE_UNAVAILABLE
    )


async def test_removed_radio_is_unavailable_until_the_ap_reports_it_again(
    hass, radio_setup, radio_snapshot, entity_registry
):
    """A radio that vanished from the AP or the controller has no live reading."""
    entry, coordinator = radio_setup
    ap = radio_snapshot.aps[0]
    count = len(er.async_entries_for_config_entry(entity_registry, entry.entry_id))
    single = replace(radio_snapshot, aps=(replace(ap, radios=(ap.radios[0],)),))
    await update(hass, coordinator, single)
    for metric in METRICS:
        assert radio_state(hass, entity_registry, single, ap, "2", metric).state == (
            STATE_UNAVAILABLE
        )
    assert radio_state(hass, entity_registry, single, ap, "1", "channel").state == "6"
    await update(hass, coordinator, replace(radio_snapshot, aps=()))
    assert radio_state(hass, entity_registry, single, ap, "1", "channel").state == (
        STATE_UNAVAILABLE
    )
    await update(hass, coordinator, radio_snapshot)
    assert radio_state(hass, entity_registry, single, ap, "2", "channel").state == "36"
    assert len(er.async_entries_for_config_entry(entity_registry, entry.entry_id)) == count


async def test_unknown_readings_are_unknown_not_zero(
    hass, radio_setup, radio_snapshot, entity_registry
):
    """The API maps an invalid channel and a missing load to None; a real count of 0 stays 0."""
    _, coordinator = radio_setup
    ap = replace(radio_snapshot.aps[0], radios=(ApRadio("1", "2.4GHz", 0, None, None),))
    snapshot = replace(radio_snapshot, aps=(ap,))
    await update(hass, coordinator, snapshot)
    assert radio_state(hass, entity_registry, snapshot, ap, "1", "connected_clients").state == "0"
    assert radio_state(hass, entity_registry, snapshot, ap, "1", "channel").state == STATE_UNKNOWN
    assert (
        radio_state(hass, entity_registry, snapshot, ap, "1", "channel_load").state == STATE_UNKNOWN
    )


async def test_a_radio_without_a_unit_has_no_stable_identity(
    hass, radio_setup, radio_snapshot, entity_registry
):
    """No entity is created for a radio that cannot be identified by its unit."""
    entry, coordinator = radio_setup
    ap = radio_snapshot.aps[0]
    snapshot = replace(
        radio_snapshot,
        aps=(
            replace(ap, radios=(*ap.radios, ApRadio("", "5GHz", 1, 100, 5.0))),
            *radio_snapshot.aps[1:],
        ),
    )
    await update(hass, coordinator, snapshot)
    assert not [
        identity for identity in sensor_rows(entity_registry, entry) if "radio__" in identity
    ]


async def test_band_label_is_the_controller_name_with_a_plain_fallback(
    hass, radio_setup, radio_snapshot, entity_registry
):
    """The band placeholder is runtime data; --- and blank mean the controller sent no name."""
    _, coordinator = radio_setup
    ap = radio_snapshot.aps[0]
    radios = (
        *ap.radios,
        ApRadio("3", "---", 0, None, None),
        ApRadio("4", "", 0, None, None),
    )
    snapshot = replace(radio_snapshot, aps=(replace(ap, radios=radios), radio_snapshot.aps[1]))
    await update(hass, coordinator, snapshot)
    for unit, band in (("1", "2.4GHz"), ("2", "5GHz"), ("3", "Radio 3"), ("4", "Radio 4")):
        for metric in METRICS:
            entity = entity_object(hass, radio_id(entity_registry, snapshot, ap, unit, metric))
            assert entity.translation_placeholders == {"band": band}


async def test_ip_address_is_hidden_unless_the_ap_reports_a_usable_address(
    hass, radio_setup, radio_snapshot, entity_registry
):
    """Blank and 0.0.0.0 are not addresses, and an AP that is not online has no live address."""
    _, coordinator = radio_setup
    online, connecting = radio_snapshot.aps
    address = entity_id(entity_registry, radio_snapshot, online, "ip_address")
    assert hass.states.get(address).state == online.ip
    assert hass.states.get(
        entity_id(entity_registry, radio_snapshot, connecting, "ip_address")
    ).state == (STATE_UNAVAILABLE)
    for unspecified in ("0.0.0.0", "", "---"):
        await update(
            hass, coordinator, replace(radio_snapshot, aps=(replace(online, ip=unspecified),))
        )
        assert hass.states.get(address).state == STATE_UNKNOWN
    changed = replace(online, ip="192.0.2.99")
    await update(hass, coordinator, replace(radio_snapshot, aps=(changed,)))
    assert hass.states.get(address).state == "192.0.2.99"
    legacy = entity_id(entity_registry, radio_snapshot, online, "connected_clients")
    assert hass.states.get(legacy).attributes["ip_address"] == "192.0.2.99"
    await update(
        hass,
        coordinator,
        replace(radio_snapshot, aps=(replace(changed, link_status="2", ip="0.0.0.0"),)),
    )
    assert hass.states.get(address).state == STATE_UNAVAILABLE
