"""Test safe setup, credential renewal, reconfiguration and options."""

from dataclasses import replace
from unittest.mock import MagicMock, patch

import pytest
from homeassistant.config_entries import SOURCE_REAUTH, SOURCE_RECONFIGURE, SOURCE_USER
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant.common import MockConfigEntry

from custom_components.tplink_ac100.api import (
    Ac100AuthError,
    Ac100ConnectionError,
    Ac100Error,
    Ac100ProtocolError,
)
from custom_components.tplink_ac100.const import CONF_ENABLE_CONTROL, DOMAIN
from custom_components.tplink_ac100.models import Controller, Snapshot

MAC = "02:00:00:00:00:01"
DATA = {CONF_HOST: "192.0.2.1", CONF_USERNAME: "test-admin", CONF_PASSWORD: "fake-password"}
SNAPSHOT = Snapshot(
    controller=Controller(
        mac=MAC,
        model="TL-AC100",
        hardware_version="1.0",
        firmware_version="test",
        cpu_usage={},
        memory_usage=20,
        uptime=120,
    ),
    aps=(),
    clients=(),
)


@pytest.fixture
def flow_api():
    """Mock the protocol boundary, never making network requests."""
    with patch("custom_components.tplink_ac100.config_flow.Ac100Api", autospec=True) as api:
        api.return_value.async_get_snapshot.return_value = SNAPSHOT
        yield api.return_value


@pytest.fixture
def flow_entry(hass: HomeAssistant) -> MockConfigEntry:
    """Create an existing controller without setting up platforms."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data=DATA,
        unique_id=MAC,
        title="Test controller",
        options={CONF_ENABLE_CONTROL: False},
    )
    entry.add_to_hass(hass)
    return entry


async def test_user_success(hass: HomeAssistant, flow_api: MagicMock) -> None:
    """Setup validates the device and explicitly defaults to read-only."""
    with patch("custom_components.tplink_ac100.async_setup_entry", return_value=None) as setup:
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
        assert result["type"] is FlowResultType.FORM
        result = await hass.config_entries.flow.async_configure(result["flow_id"], DATA)
        await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == {**DATA, CONF_HOST: "http://192.0.2.1"}
    assert result["options"] == {CONF_ENABLE_CONTROL: False}
    assert result["result"].unique_id == MAC
    setup.assert_awaited_once()
    flow_api.async_login.assert_awaited_once()
    flow_api.async_get_snapshot.assert_awaited_once()
    flow_api.async_set_ap_led.assert_not_awaited()
    flow_api.async_reboot_ap.assert_not_awaited()
    flow_api.async_reboot_controller.assert_not_awaited()


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (Ac100AuthError("Rejected"), "invalid_auth"),
        (Ac100ConnectionError("Unavailable"), "cannot_connect"),
        (Ac100ProtocolError("Bad response"), "unknown"),
        (Ac100Error("Unexpected device error"), "unknown"),
        (ValueError("secret-token-must-not-be-logged"), "unknown"),
    ],
)
async def test_user_errors(
    hass: HomeAssistant, flow_api: MagicMock, error: Exception, expected: str, caplog
) -> None:
    """Failures stay in the form and never leak exception payloads."""
    flow_api.async_login.side_effect = error
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}, data=DATA
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": expected}
    assert not hass.config_entries.async_entries(DOMAIN)
    assert "secret-token-must-not-be-logged" not in caplog.text


async def test_unsupported_device(hass: HomeAssistant, flow_api: MagicMock) -> None:
    """Do not treat other TP-Link families as compatible controllers."""
    flow_api.async_get_snapshot.return_value = replace(
        SNAPSHOT, controller=replace(SNAPSHOT.controller, model="Other controller")
    )
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}, data=DATA
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "unsupported_device"}


async def test_duplicate_controller(
    hass: HomeAssistant, flow_api: MagicMock, flow_entry: MockConfigEntry
) -> None:
    """A different host spelling cannot add the same hardware twice."""
    flow_api.async_get_snapshot.return_value = replace(
        SNAPSHOT, controller=replace(SNAPSHOT.controller, mac=MAC.upper().replace(":", "-"))
    )
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}, data={**DATA, CONF_HOST: "new-controller.local"}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    assert flow_entry.data == DATA


@pytest.mark.parametrize("source", [SOURCE_RECONFIGURE, SOURCE_REAUTH])
async def test_update_connection_or_credentials(
    hass: HomeAssistant, flow_api: MagicMock, flow_entry: MockConfigEntry, source: str
) -> None:
    """Update the existing entry without changing its stable identity."""
    changes = (
        {CONF_HOST: "192.0.2.2"}
        if source == SOURCE_RECONFIGURE
        else {CONF_USERNAME: "new-admin", CONF_PASSWORD: "new-fake-password"}
    )
    with patch.object(hass.config_entries, "async_schedule_reload") as reload_entry:
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": source, "entry_id": flow_entry.entry_id},
            data=flow_entry.data if source == SOURCE_REAUTH else None,
        )
        assert result["type"] is FlowResultType.FORM
        if source == SOURCE_REAUTH:
            assert DATA[CONF_PASSWORD] not in repr(result["data_schema"])
        result = await hass.config_entries.flow.async_configure(result["flow_id"], changes)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == (
        "reconfigure_successful" if source == SOURCE_RECONFIGURE else "reauth_successful"
    )
    expected = {**DATA, **changes}
    if source == SOURCE_RECONFIGURE:
        expected[CONF_HOST] = "http://192.0.2.2"
    assert flow_entry.data == expected
    assert flow_entry.unique_id == MAC
    assert flow_entry.options == {CONF_ENABLE_CONTROL: False}
    reload_entry.assert_called_once_with(flow_entry.entry_id)


@pytest.mark.parametrize("source", [SOURCE_RECONFIGURE, SOURCE_REAUTH])
async def test_update_wrong_device(
    hass: HomeAssistant, flow_api: MagicMock, flow_entry: MockConfigEntry, source: str
) -> None:
    """Never point existing entities at a replacement controller by accident."""
    flow_api.async_get_snapshot.return_value = replace(
        SNAPSHOT, controller=replace(SNAPSHOT.controller, mac="02:00:00:00:00:02")
    )
    changes = (
        {CONF_HOST: "192.0.2.2"}
        if source == SOURCE_RECONFIGURE
        else {CONF_USERNAME: DATA[CONF_USERNAME], CONF_PASSWORD: DATA[CONF_PASSWORD]}
    )
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": source, "entry_id": flow_entry.entry_id},
        data=flow_entry.data if source == SOURCE_REAUTH else None,
    )
    result = await hass.config_entries.flow.async_configure(result["flow_id"], changes)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "wrong_device"
    assert flow_entry.data == DATA


@pytest.mark.parametrize("source", [SOURCE_RECONFIGURE, SOURCE_REAUTH])
async def test_update_error_recovery(
    hass: HomeAssistant, flow_api: MagicMock, flow_entry: MockConfigEntry, source: str
) -> None:
    """A failed validation preserves the entry and can be corrected in the same flow."""
    flow_api.async_login.side_effect = [Ac100ConnectionError("Unavailable"), None]
    changes = (
        {CONF_HOST: "192.0.2.2"}
        if source == SOURCE_RECONFIGURE
        else {CONF_USERNAME: DATA[CONF_USERNAME], CONF_PASSWORD: DATA[CONF_PASSWORD]}
    )
    with patch.object(hass.config_entries, "async_schedule_reload"):
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": source, "entry_id": flow_entry.entry_id},
            data=flow_entry.data if source == SOURCE_REAUTH else None,
        )
        result = await hass.config_entries.flow.async_configure(result["flow_id"], changes)
        assert result["type"] is FlowResultType.FORM
        assert result["errors"] == {"base": "cannot_connect"}
        assert flow_entry.data == DATA
        result = await hass.config_entries.flow.async_configure(result["flow_id"], changes)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] in {"reauth_successful", "reconfigure_successful"}


@pytest.mark.parametrize("enabled", [True, False])
async def test_options(hass: HomeAssistant, flow_entry: MockConfigEntry, enabled: bool) -> None:
    """Control opt-in reloads once without running protocol actions."""
    hass.config_entries.async_update_entry(flow_entry, options={CONF_ENABLE_CONTROL: not enabled})
    with patch.object(hass.config_entries, "async_schedule_reload") as reload_entry:
        result = await hass.config_entries.options.async_init(flow_entry.entry_id)
        assert result["type"] is FlowResultType.FORM
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {CONF_ENABLE_CONTROL: enabled}
        )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert flow_entry.options == {CONF_ENABLE_CONTROL: enabled}
    reload_entry.assert_called_once_with(flow_entry.entry_id)


async def test_invalid_host_does_not_connect(hass: HomeAssistant, caplog) -> None:
    """Reject URL credentials and paths before sending a request."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": SOURCE_USER},
        data={**DATA, CONF_HOST: "http://user:private-host-secret@192.0.2.1/path"},
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "unknown"}
    assert "private-host-secret" not in caplog.text
    assert not hass.config_entries.async_entries(DOMAIN)
