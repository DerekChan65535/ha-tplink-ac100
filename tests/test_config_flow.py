"""Test safe setup, credential renewal, reconfiguration and options."""

import asyncio
from dataclasses import replace
from unittest.mock import MagicMock, patch

import pytest
from homeassistant.config_entries import (
    SOURCE_REAUTH,
    SOURCE_RECONFIGURE,
    SOURCE_USER,
    ConfigEntryState,
)
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant.common import MockConfigEntry

from custom_components.tplink_ac100.api import (
    Ac100AccountLockedError,
    Ac100AuthError,
    Ac100ConnectionError,
    Ac100Error,
    Ac100ProtocolError,
    Ac100SessionBusyError,
    Ac100SessionExpiredError,
)
from custom_components.tplink_ac100.config_flow import async_validate_input
from custom_components.tplink_ac100.const import (
    CONF_ENABLE_CONTROL,
    CONF_MONITOR_WIRELESS_SERVICES,
    DOMAIN,
)
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
    flow_api.async_logout.assert_awaited_once()
    flow_api.async_set_ap_led.assert_not_awaited()
    flow_api.async_reboot_ap.assert_not_awaited()
    flow_api.async_reboot_controller.assert_not_awaited()


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (Ac100SessionBusyError("Another administrator session"), "session_busy"),
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
    flow_api.async_logout.assert_awaited_once()


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
    flow_api.async_logout.assert_awaited_once()


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
    flow_api.async_logout.assert_awaited_once()
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
    flow_api.async_logout.assert_awaited_once()
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
    assert flow_entry.options == {
        CONF_ENABLE_CONTROL: enabled,
        CONF_MONITOR_WIRELESS_SERVICES: False,
    }
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


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (Ac100ConnectionError("Unavailable"), "cannot_connect"),
        (Ac100ProtocolError("Incomplete snapshot"), "unknown"),
    ],
)
async def test_snapshot_failure_releases_session(
    hass: HomeAssistant, flow_api: MagicMock, error: Exception, expected: str
) -> None:
    """Release a temporary login even if the controller cannot be read."""
    flow_api.async_get_snapshot.side_effect = error
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}, data=DATA
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": expected}
    flow_api.async_logout.assert_awaited_once()
    assert not hass.config_entries.async_entries(DOMAIN)


async def test_cancelled_validation_releases_session(
    hass: HomeAssistant, flow_api: MagicMock
) -> None:
    """Cancellation must not strand the flow's authenticated controller session."""
    flow_api.async_get_snapshot.side_effect = asyncio.CancelledError
    with pytest.raises(asyncio.CancelledError):
        await async_validate_input(hass, DATA)
    flow_api.async_logout.assert_awaited_once()


async def test_failed_logout_does_not_create_entry(
    hass: HomeAssistant, flow_api: MagicMock
) -> None:
    """Do not start a second login if releasing the temporary session failed."""
    flow_api.async_logout.side_effect = Ac100ConnectionError("Unable to release session")
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}, data=DATA
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "cannot_connect"}
    assert not hass.config_entries.async_entries(DOMAIN)


@pytest.mark.parametrize("source", [SOURCE_RECONFIGURE, SOURCE_REAUTH])
async def test_busy_update_preserves_entry(
    hass: HomeAssistant, flow_api: MagicMock, flow_entry: MockConfigEntry, source: str
) -> None:
    """An existing session is actionable contention, not a wrong-password error."""
    flow_api.async_login.side_effect = Ac100SessionBusyError("Session already in use")
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
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "session_busy"}
    assert flow_entry.data == DATA
    flow_api.async_logout.assert_awaited_once()


@pytest.fixture
def loaded_flow_entry(hass: HomeAssistant, flow_entry: MockConfigEntry, flow_api: MagicMock):
    """Model only this entry's active session and lifecycle transitions."""
    events = []
    flow_entry.mock_state(hass, ConfigEntryState.LOADED)

    async def unload(entry_id):
        assert entry_id == flow_entry.entry_id
        events.append("unload")
        flow_entry.mock_state(hass, ConfigEntryState.NOT_LOADED)
        return True

    async def setup(entry_id):
        assert entry_id == flow_entry.entry_id
        events.append("restore")
        flow_entry.mock_state(hass, ConfigEntryState.LOADED)
        return True

    async def login():
        assert flow_entry.state is ConfigEntryState.NOT_LOADED
        events.append("validate")

    flow_api.async_login.side_effect = login
    flow_api.async_logout.side_effect = lambda: events.append("logout")
    with (
        patch.object(hass.config_entries, "async_unload", side_effect=unload) as unload_entry,
        patch.object(hass.config_entries, "async_setup", side_effect=setup) as setup_entry,
    ):
        yield flow_entry, events, unload_entry, setup_entry


@pytest.mark.parametrize("source", [SOURCE_RECONFIGURE, SOURCE_REAUTH])
async def test_loaded_update_success(
    hass: HomeAssistant, flow_api: MagicMock, loaded_flow_entry, source: str
) -> None:
    """Release the running session before validation, then load only the new data."""
    entry, events, unload_entry, setup_entry = loaded_flow_entry
    changes = (
        {CONF_HOST: "192.0.2.2"}
        if source == SOURCE_RECONFIGURE
        else {CONF_USERNAME: "new-admin", CONF_PASSWORD: "new-fake-password"}
    )
    with patch.object(
        hass.config_entries,
        "async_schedule_reload",
        side_effect=lambda entry_id: events.append("reload"),
    ) as reload_entry:
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": source, "entry_id": entry.entry_id},
            data=entry.data if source == SOURCE_REAUTH else None,
        )
        unload_entry.assert_not_awaited()
        result = await hass.config_entries.flow.async_configure(result["flow_id"], changes)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] in {"reconfigure_successful", "reauth_successful"}
    assert events == ["unload", "validate", "logout", "reload"]
    unload_entry.assert_awaited_once_with(entry.entry_id)
    setup_entry.assert_not_awaited()
    reload_entry.assert_called_once_with(entry.entry_id)
    assert entry.unique_id == MAC
    assert entry.data == {
        **DATA,
        **changes,
        **({CONF_HOST: "http://192.0.2.2"} if source == SOURCE_RECONFIGURE else {}),
    }


@pytest.mark.parametrize("source", [SOURCE_RECONFIGURE, SOURCE_REAUTH])
@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (Ac100ConnectionError("Unavailable"), "cannot_connect"),
        (Ac100SessionBusyError("Other session"), "session_busy"),
        (ValueError("Unexpected response"), "unknown"),
    ],
)
async def test_loaded_validation_failure_restores_original(
    hass: HomeAssistant,
    flow_api: MagicMock,
    loaded_flow_entry,
    source: str,
    error: Exception,
    expected: str,
) -> None:
    """A failed reconfigure restores polling; a failed reauth never retries rejected credentials."""
    entry, events, unload_entry, setup_entry = loaded_flow_entry
    flow_api.async_get_snapshot.side_effect = error
    changes = (
        {CONF_HOST: "192.0.2.2"}
        if source == SOURCE_RECONFIGURE
        else {CONF_USERNAME: "new-admin", CONF_PASSWORD: "new-fake-password"}
    )
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": source, "entry_id": entry.entry_id},
        data=entry.data if source == SOURCE_REAUTH else None,
    )
    result = await hass.config_entries.flow.async_configure(result["flow_id"], changes)
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": expected}
    assert entry.data == DATA
    if source == SOURCE_REAUTH:
        assert events == ["unload", "validate", "logout"]
        setup_entry.assert_not_awaited()
    else:
        assert events == ["unload", "validate", "logout", "restore"]
        assert entry.state is ConfigEntryState.LOADED
        setup_entry.assert_awaited_once_with(entry.entry_id)


@pytest.mark.parametrize("source", [SOURCE_RECONFIGURE, SOURCE_REAUTH])
async def test_loaded_wrong_device_restores_original(
    hass: HomeAssistant, flow_api: MagicMock, loaded_flow_entry, source: str
) -> None:
    """Reject a different MAC; only a reconfigure recovers the original loaded entry."""
    entry, events, unload_entry, setup_entry = loaded_flow_entry
    flow_api.async_get_snapshot.return_value = replace(
        SNAPSHOT, controller=replace(SNAPSHOT.controller, mac="02:00:00:00:00:02")
    )
    changes = (
        {CONF_HOST: "192.0.2.2"}
        if source == SOURCE_RECONFIGURE
        else {CONF_USERNAME: "new-admin", CONF_PASSWORD: "new-fake-password"}
    )
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": source, "entry_id": entry.entry_id},
        data=entry.data if source == SOURCE_REAUTH else None,
    )
    result = await hass.config_entries.flow.async_configure(result["flow_id"], changes)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "wrong_device"
    assert entry.data == DATA
    if source == SOURCE_REAUTH:
        assert events == ["unload", "validate", "logout"]
        setup_entry.assert_not_awaited()
    else:
        assert events == ["unload", "validate", "logout", "restore"]
        assert entry.state is ConfigEntryState.LOADED
        setup_entry.assert_awaited_once_with(entry.entry_id)


@pytest.mark.parametrize("source", [SOURCE_RECONFIGURE, SOURCE_REAUTH])
async def test_loaded_cancelled_validation_restores_original(
    hass: HomeAssistant, flow_api: MagicMock, loaded_flow_entry, source: str
) -> None:
    """Propagate cancellation after the temporary logout; restore only for a reconfigure."""
    entry, events, unload_entry, setup_entry = loaded_flow_entry
    flow_api.async_get_snapshot.side_effect = asyncio.CancelledError
    changes = (
        {CONF_HOST: "192.0.2.2"}
        if source == SOURCE_RECONFIGURE
        else {CONF_USERNAME: "new-admin", CONF_PASSWORD: "new-fake-password"}
    )
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": source, "entry_id": entry.entry_id},
        data=entry.data if source == SOURCE_REAUTH else None,
    )
    with pytest.raises(asyncio.CancelledError):
        await hass.config_entries.flow.async_configure(result["flow_id"], changes)
    assert entry.data == DATA
    if source == SOURCE_REAUTH:
        assert events == ["unload", "validate", "logout"]
        setup_entry.assert_not_awaited()
    else:
        assert events == ["unload", "validate", "logout", "restore"]
        assert entry.state is ConfigEntryState.LOADED
        setup_entry.assert_awaited_once_with(entry.entry_id)


async def test_loaded_unload_failure_does_not_validate(
    hass: HomeAssistant, flow_api: MagicMock, loaded_flow_entry
) -> None:
    """Leave a running entry alone if HA cannot release its session safely."""
    entry, events, unload_entry, setup_entry = loaded_flow_entry
    unload_entry.side_effect = None
    unload_entry.return_value = False
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_RECONFIGURE, "entry_id": entry.entry_id}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_HOST: "192.0.2.2"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "unknown"}
    assert entry.state is ConfigEntryState.LOADED
    assert entry.data == DATA
    flow_api.async_login.assert_not_awaited()
    setup_entry.assert_not_awaited()


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (Ac100AccountLockedError("Locked"), "account_locked"),
        (Ac100SessionExpiredError("Rejected"), "cannot_connect"),
    ],
)
async def test_user_flow_reports_lock_and_expiry_without_calling_it_a_bad_password(
    hass: HomeAssistant, flow_api: MagicMock, error: Exception, expected: str
) -> None:
    flow_api.async_login.side_effect = error
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], DATA)
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": expected}
    flow_api.async_logout.assert_awaited()
