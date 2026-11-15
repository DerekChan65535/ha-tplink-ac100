"""Offline protocol tests using synthetic credentials and anonymized real response shapes."""

import asyncio
import copy
import hashlib
import json
import logging
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import aiohttp
import pytest

from custom_components.tplink_ac100.api import (
    Ac100Api,
    Ac100AuthError,
    Ac100ConnectionError,
    Ac100Error,
    Ac100ProtocolError,
    Ac100SessionBusyError,
    Ac100UnsafeLedError,
    normalize_host,
)
from custom_components.tplink_ac100.diagnostics import async_get_config_entry_diagnostics
from custom_components.tplink_ac100.switch import Ac100LedSwitch

FIXTURES = Path(__file__).parent / "fixtures"


def load(name):
    return json.loads((FIXTURES / name).read_text())


class Response:
    """Small aiohttp context-manager double; it never opens a network socket."""

    def __init__(self, data=None, *, status=200, text=None, error=None):
        self.status = status
        self.body = json.dumps(data) if text is None else text
        self.error = error

    async def __aenter__(self):
        if self.error:
            raise self.error
        return self

    async def __aexit__(self, *args):
        return False

    async def text(self):
        return self.body


def make_api(*responses):
    session = MagicMock(spec=aiohttp.ClientSession)
    session.post.side_effect = responses
    api = Ac100Api(session, "controller.example", "admin", "example-password")
    return api, session


def metadata():
    return Response({"error_code": 0, "encrypt_type": ["-3"], "username_type": "one_username"})


def logged_in():
    return Response({"error_code": 0, "stok": "synthetic-session", "role": "sys_admin"})


def led_config(entry_id="0", sync="off", **extra):
    row = {"entry_id": entry_id, "led_wifi_sync": sync, **extra}
    return Response(
        {
            "error_code": 0,
            "apmng_set": {"count": {"ap_list": 1}, "ap_list": [{"ap_list_0": row}]},
        }
    )


def snapshot_responses():
    return [
        Response(load(name))
        for name in (
            "controller.json",
            "identity.json",
            "aps.json",
            "clients.json",
            "clients_page2.json",
        )
    ]


@pytest.mark.parametrize(
    ("host", "expected"),
    [
        ("controller.example", "http://controller.example"),
        (" 192.0.2.1 ", "http://192.0.2.1"),
        ("https://controller.example:8443/", "https://controller.example:8443"),
        ("[2001:db8::1]", "http://[2001:db8::1]"),
    ],
)
def test_normalize_host(host, expected):
    assert normalize_host(host) == expected


@pytest.mark.parametrize(
    "host",
    [
        "",
        "ftp://controller.example",
        "http://user:secret@controller.example",
        "http://host/path",
        "http://host?token=secret",
        "http://host:bad",
    ],
)
def test_reject_unsafe_host(host):
    with pytest.raises(Ac100ProtocolError) as exc:
        normalize_host(host)
    assert "secret" not in str(exc.value)


async def test_legacy_login_never_forces_or_logs_out():
    api, session = make_api(metadata(), logged_in())
    await api.async_login()
    assert session.post.call_args_list[0].kwargs["json"] == {
        "method": "do",
        "user_management": {"get_encrypt_info": None},
    }
    # Golden vector independently evaluated using the device JavaScript algorithm.
    assert session.post.call_args_list[1].kwargs["json"] == {
        "method": "do",
        "login": {"username": "admin", "password": "3iQPis7rV4UDQzly", "force": 0},
    }
    assert all(call.args[0] == "http://controller.example/" for call in session.post.call_args_list)
    session.close.assert_not_called()


async def test_modern_login_uses_nonce_without_force():
    api, session = make_api(
        Response({"error_code": 0, "encrypt_type": ["-3", "3"], "nonce": "challenge"}),
        logged_in(),
    )
    await api.async_login()
    login = session.post.call_args_list[1].kwargs["json"]["login"]
    assert login == {
        "username": "admin",
        "password": hashlib.md5(b"example-password:challenge", usedforsecurity=False).hexdigest(),
        "encrypt_type": "3",
    }


async def test_logout_only_releases_the_clients_own_token():
    api, session = make_api(metadata(), logged_in(), Response({"error_code": 0}))
    await api.async_login()
    await api.async_logout()
    assert session.post.call_count == 3
    assert session.post.call_args.args == ("http://controller.example/stok=synthetic-session/ds",)
    assert session.post.call_args.kwargs["json"] == {"method": "do", "system": {"logout": None}}
    assert api._stok is None
    session.close.assert_not_called()
    await api.async_logout()
    assert session.post.call_count == 3


async def test_logout_without_a_token_does_not_login_or_send_a_request():
    api, session = make_api()
    await api.async_logout()
    assert api._stok is None
    session.post.assert_not_called()


@pytest.mark.parametrize(
    "expired",
    [
        Response({"error_code": -40401, "data": {"code": -40401}}, status=401),
        Response({"error_code": -40401}),
    ],
)
async def test_logout_of_expired_session_succeeds_without_reauthentication(expired):
    api, session = make_api(expired)
    api._stok = "expired-synthetic-session"
    await api.async_logout()
    assert api._stok is None
    assert session.post.call_count == 1
    assert session.post.call_args.kwargs["json"] == {"method": "do", "system": {"logout": None}}


@pytest.mark.parametrize(
    ("response", "error"),
    [
        (Response(error=aiohttp.ClientConnectionError("secret-url")), Ac100ConnectionError),
        (Response(status=401), Ac100AuthError),
        (Response({"error_code": -12345}), Ac100ProtocolError),
    ],
)
async def test_logout_failure_clears_token_without_replay_or_reauthentication(response, error):
    api, session = make_api(response)
    api._stok = "synthetic-session"
    with pytest.raises(error) as exc:
        await api.async_logout()
    assert "secret-url" not in str(exc.value)
    assert api._stok is None
    assert session.post.call_count == 1
    await api.async_logout()
    assert session.post.call_count == 1


@pytest.mark.parametrize(
    ("method", "arguments"),
    [
        ("async_login", ()),
        ("async_get_snapshot", ()),
        ("async_get_ap_led_wifi_sync", ("0",)),
        ("async_set_ap_led", ("0", True)),
        ("async_reboot_ap", ("0",)),
        ("async_reboot_controller", ()),
    ],
)
async def test_retired_client_cannot_login_poll_or_manage(method, arguments):
    api, session = make_api()
    await api.async_logout()
    with pytest.raises(Ac100Error, match="client has been closed"):
        await getattr(api, method)(*arguments)
    session.post.assert_not_called()
    session.close.assert_not_called()


@pytest.mark.parametrize("method", ["async_login", "async_get_snapshot", "async_reboot_controller"])
async def test_logout_retires_client_before_earlier_queued_actions_get_lock(method):
    api, session = make_api(Response({"error_code": 0}))
    api._stok = "synthetic-session"
    await api._lock.acquire()
    queued = asyncio.create_task(getattr(api, method)())
    await asyncio.sleep(0)
    closing = asyncio.create_task(api.async_logout())
    await asyncio.sleep(0)
    assert api._closed
    api._lock.release()
    with pytest.raises(Ac100Error, match="client has been closed"):
        await queued
    await closing
    assert api._stok is None
    assert session.post.call_count == 1
    assert session.post.call_args.kwargs["json"] == {"method": "do", "system": {"logout": None}}


class GatedResponse(Response):
    """Keep an HTTP response in flight until a test explicitly releases it."""

    def __init__(self, data):
        super().__init__(data)
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def text(self):
        self.started.set()
        await self.release.wait()
        return self.body


async def test_close_during_poll_prevents_later_queries_but_releases_owned_session():
    in_flight = GatedResponse(load("controller.json"))
    api, session = make_api(in_flight, Response({"error_code": 0}))
    api._stok = "synthetic-session"
    poll = asyncio.create_task(api.async_get_snapshot())
    await in_flight.started.wait()
    closing = asyncio.create_task(api.async_logout())
    await asyncio.sleep(0)
    assert api._closed
    in_flight.release.set()
    with pytest.raises(Ac100Error, match="client has been closed"):
        await poll
    await closing
    bodies = [call.kwargs["json"] for call in session.post.call_args_list]
    assert len(bodies) == 2
    assert bodies[0]["method"] == "get"
    assert bodies[1] == {"method": "do", "system": {"logout": None}}
    assert api._stok is None


async def test_close_during_authentication_metadata_does_not_open_new_session():
    in_flight = GatedResponse({"error_code": 0, "encrypt_type": ["-3"]})
    api, session = make_api(in_flight)
    login = asyncio.create_task(api.async_login())
    await in_flight.started.wait()
    closing = asyncio.create_task(api.async_logout())
    await asyncio.sleep(0)
    assert api._closed
    in_flight.release.set()
    with pytest.raises(Ac100Error, match="client has been closed"):
        await login
    await closing
    assert api._stok is None
    assert session.post.call_count == 1
    assert session.post.call_args.kwargs["json"] == {
        "method": "do",
        "user_management": {"get_encrypt_info": None},
    }


async def test_unknown_authentication_is_not_guessed():
    api, session = make_api(Response({"error_code": 0, "encrypt_type": ["unsupported"]}))
    with pytest.raises(Ac100ProtocolError):
        await api.async_login()
    assert session.post.call_count == 1


async def test_snapshot_reads_every_page_and_decodes_once():
    api, session = make_api(metadata(), logged_in(), *snapshot_responses())
    snapshot = await api.async_get_snapshot()
    controller = snapshot.controller
    assert controller.model == "TL-AC100"
    assert controller.mac.startswith("02:00:00:00:")
    assert controller.firmware_version == "1.0.5 Build 250814 Rel.82234n"
    assert controller.memory_usage == 17
    assert controller.uptime == 9296
    assert controller.cpu_usage == {"core1": 0, "core2": 0, "core3": 1, "core4": 0}
    assert len(snapshot.aps) == 6
    assert sum(ap.is_online for ap in snapshot.aps) == 4
    assert snapshot.aps[0].client_count == 8
    assert snapshot.aps[0].radios[0].channel_load == 31
    assert snapshot.aps[1].ip == "0.0.0.0"
    assert not snapshot.aps[1].is_manageable
    assert len(snapshot.clients) == 14
    assert snapshot.clients[0].radios[0].rssi == -50
    assert snapshot.clients[0].ipv6 == "::"
    assert snapshot.clients[0].name is None
    assert snapshot.clients[0].ssid.startswith("Example WiFi ")
    requests = [call.kwargs["json"] for call in session.post.call_args_list[2:]]
    assert all(request["method"] == "get" for request in requests)
    assert all("apmng_set" not in request for request in requests)
    assert requests[-2]["apmng_client"]["para"] == {"start": 0, "end": 9}
    assert requests[-1]["apmng_client"]["para"] == {"start": 10, "end": 19}
    session.close.assert_not_called()


async def test_identity_is_rechecked_even_without_a_new_login():
    responses = snapshot_responses()
    new_identity = load("identity.json")
    next(iter(new_identity["interface"]["interface"][0].values()))["macaddr"] = "02-00-00-FF-FF-FF"
    api, session = make_api(*responses, responses[0], Response(new_identity), *responses[2:])
    api._stok = "synthetic-session"
    first = await api.async_get_snapshot()
    second = await api.async_get_snapshot()
    assert first.controller.mac != second.controller.mac
    assert second.controller.mac == "02:00:00:ff:ff:ff"
    bodies = [call.kwargs["json"] for call in session.post.call_args_list]
    assert sum("interface" in body for body in bodies) == 2


@pytest.mark.parametrize(
    ("wire_status", "expected_state", "expected_available"),
    [("1", True, True), ("2", False, True), ("0", None, False), ("unknown", None, False)],
)
async def test_led_read_status_enumeration(wire_status, expected_state, expected_available):
    aps = load("aps.json")
    row = next(iter(aps["apmng_status"]["ap_list"][0].values()))
    row["led_status"] = wire_status
    row["link_status"] = "1"
    responses = snapshot_responses()
    responses[2] = Response(aps)
    api, _ = make_api(*responses)
    api._stok = "synthetic-session"
    snapshot = await api.async_get_snapshot()
    ap = snapshot.aps[0]
    assert ap.led_enabled is expected_state
    coordinator = SimpleNamespace(data=snapshot, last_update_success=True)
    switch = Ac100LedSwitch(coordinator, ap.mac)
    assert switch.available is expected_available
    assert switch.is_on is expected_state


async def test_led_off_writes_zero_but_reads_two_and_remains_available():
    aps = load("aps.json")
    row = next(iter(aps["apmng_status"]["ap_list"][0].values()))
    row.update(led_status="2", link_status="1")
    responses = snapshot_responses()
    responses[2] = Response(aps)
    api, session = make_api(led_config(row["entry_id"]), Response({"error_code": 0}), *responses)
    api._stok = "synthetic-session"
    await api.async_set_ap_led(row["entry_id"], False)
    assert session.post.call_args_list[1].kwargs["json"] == {
        "method": "set",
        "apmng_status": {
            "table": "ap_list",
            "filter": [{"entry_id": row["entry_id"]}],
            "para": {"led_status": "0"},
        },
    }
    snapshot = await api.async_get_snapshot()
    ap = snapshot.aps[0]
    assert ap.led_enabled is False
    coordinator = SimpleNamespace(data=snapshot, last_update_success=True)
    switch = Ac100LedSwitch(coordinator, ap.mac)
    assert switch.available
    assert switch.is_on is False


async def test_unknown_radio_values_are_not_zero_telemetry():
    responses = snapshot_responses()
    aps = load("aps.json")
    row = next(iter(aps["apmng_status"]["ap_list"][0].values()))
    row["rf_entry"][0].update(channel="-1", channel_load="--")
    row["link_status"] = "0"
    row["led_status"] = "unknown"
    clients = load("clients.json")
    client = next(iter(clients["apmng_client"]["client_list"][0].values()))
    client["rf_entry"][0].update(rssi="---", nego_rate="---")
    responses[2], responses[3] = Response(aps), Response(clients)
    api, _ = make_api(*responses)
    api._stok = "synthetic-session"
    snapshot = await api.async_get_snapshot()
    assert not snapshot.aps[0].is_online
    assert not snapshot.aps[0].is_manageable
    assert snapshot.aps[0].led_enabled is None
    assert snapshot.aps[0].radios[0].channel is None
    assert snapshot.aps[0].radios[0].channel_load is None
    assert snapshot.clients[0].radios[0].rssi is None
    assert snapshot.clients[0].radios[0].negotiated_rate is None


@pytest.mark.parametrize(
    "expired",
    [
        Response({"error_code": -40401, "data": {"code": -40401}}, status=401),
        Response({"error_code": -40401}),
    ],
)
async def test_expired_session_reauthenticates_once(expired):
    api, session = make_api(
        expired, metadata(), logged_in(), led_config(), Response({"error_code": 0})
    )
    api._stok = "old-session"
    await api.async_set_ap_led("0", True)
    assert session.post.call_count == 5
    assert (
        session.post.call_args_list[0].kwargs["json"]
        == session.post.call_args_list[3].kwargs["json"]
    )


async def test_still_expired_after_reauthentication_does_not_loop():
    expired = Response({"error_code": -40401, "data": {"code": -40401}}, status=401)
    api, session = make_api(expired, metadata(), logged_in(), expired)
    api._stok = "old-session"
    with pytest.raises(Ac100AuthError):
        await api.async_reboot_controller()
    assert session.post.call_count == 4


@pytest.mark.parametrize(
    "response",
    [
        Response(status=401),
        Response(text="not JSON", status=401),
        Response({"error_code": -40401, "data": {"code": -40404}}, status=401),
        Response({"error_code": -40401, "data": {"code": -57704}}, status=401),
        Response({"error_code": -40100}, status=401),
        Response({"error_code": -40401}, status=403),
    ],
)
async def test_ambiguous_or_forbidden_401_does_not_trigger_login(response):
    api, session = make_api(response)
    api._stok = "synthetic-session"
    with pytest.raises(Ac100AuthError):
        await api.async_reboot_controller()
    assert session.post.call_count == 1


async def test_bad_password_on_login_has_no_retry():
    api, session = make_api(
        metadata(), Response({"error_code": -40401, "data": {"time": 4}}, status=401)
    )
    with pytest.raises(Ac100AuthError):
        await api.async_login()
    assert session.post.call_count == 2


@pytest.mark.parametrize(
    "result",
    [
        {"error_code": -40100, "data": {"code": -57704}},
        {"error_code": -57704},
        {"error_code": -40401, "data": {"code": -57704}},
    ],
)
async def test_conflicting_session_never_attempts_force_or_reauthentication(result):
    api, session = make_api(Response(result, status=401))
    api._stok = "synthetic-session"
    with pytest.raises(Ac100SessionBusyError):
        await api.async_reboot_controller()
    assert session.post.call_count == 1


async def test_login_busy_is_reported_without_force_or_retry():
    api, session = make_api(
        metadata(), Response({"error_code": -40100, "data": {"code": -57704}}, status=401)
    )
    with pytest.raises(Ac100SessionBusyError):
        await api.async_login()
    assert api._stok is None
    assert session.post.call_count == 2
    assert session.post.call_args.kwargs["json"]["login"]["force"] == 0


@pytest.mark.parametrize("result", [{"error_code": -40100}, {"error_code": -57703}])
async def test_other_authentication_errors_are_not_classified_as_busy(result):
    api, session = make_api(Response(result, status=401))
    api._stok = "synthetic-session"
    with pytest.raises(Ac100AuthError) as exc:
        await api.async_reboot_controller()
    assert not isinstance(exc.value, Ac100SessionBusyError)
    assert session.post.call_count == 1


@pytest.mark.parametrize(
    ("method", "arguments", "expected"),
    [
        (
            "async_set_ap_led",
            ("2", False),
            {
                "method": "set",
                "apmng_status": {
                    "table": "ap_list",
                    "filter": [{"entry_id": "2"}],
                    "para": {"led_status": "0"},
                },
            },
        ),
        (
            "async_reboot_ap",
            ("2",),
            {"method": "do", "apmng_status": {"ap_reboot": {"entry_id": ["2"]}}},
        ),
        ("async_reboot_controller", (), {"method": "do", "system": {"reboot": None}}),
    ],
)
async def test_explicit_management_payloads(method, arguments, expected):
    responses = [Response({"error_code": 0})]
    if method == "async_set_ap_led":
        responses.insert(0, led_config(arguments[0]))
    api, session = make_api(*responses)
    api._stok = "synthetic-session"
    await getattr(api, method)(*arguments)
    assert session.post.call_args.kwargs["json"] == expected


async def test_network_failure_during_reboot_is_never_replayed_or_exposed():
    api, session = make_api(Response(error=aiohttp.ClientConnectionError("secret-token-and-url")))
    api._stok = "synthetic-session"
    with pytest.raises(Ac100ConnectionError) as exc:
        await api.async_reboot_controller()
    assert "secret-token-and-url" not in str(exc.value)
    assert session.post.call_count == 1


@pytest.mark.parametrize(
    "response",
    [
        Response(text="secret-token-not-json"),
        Response([]),
        Response({}),
        Response({"error_code": []}),
        Response({"error_code": -12345, "error_msg": "secret-token"}),
    ],
)
async def test_invalid_response_errors_do_not_expose_body(response):
    api, session = make_api(response)
    api._stok = "synthetic-session"
    with pytest.raises(Ac100ProtocolError) as exc:
        await api.async_reboot_controller()
    assert "secret-token" not in str(exc.value)
    assert session.post.call_count == 1


async def test_missing_table_page_fails_snapshot_instead_of_disconnect_all():
    responses = snapshot_responses()
    page2 = copy.deepcopy(load("clients_page2.json"))
    page2["apmng_client"]["client_list"] = []
    responses[-1] = Response(page2)
    api, _ = make_api(*responses)
    api._stok = "synthetic-session"
    with pytest.raises(Ac100ProtocolError, match="incomplete table"):
        await api.async_get_snapshot()


async def test_debug_logs_cover_operations_without_sensitive_data(caplog):
    logger_name = "custom_components.tplink_ac100.api"
    caplog.set_level(logging.DEBUG, logger=logger_name)
    identity = load("identity.json")
    next(iter(identity["interface"]["interface"][0].values()))["macaddr"] = "02-AB-CD-99-88-77"
    aps = load("aps.json")
    ap = next(iter(aps["apmng_status"]["ap_list"][0].values()))
    ap.update(
        entry_id="private-ap-id",
        entry_name="private-ap-name",
        mac="02-AB-CD-11-22-33",
        ip="198.51.100.111",
    )
    clients = load("clients.json")
    client = next(iter(clients["apmng_client"]["client_list"][0].values()))
    client.update(
        name="private-client-name",
        ssid="private-ssid",
        ap_name="private-ap-name",
        mac="02-AB-CD-44-55-66",
        ip="198.51.100.112",
    )
    responses = snapshot_responses()
    responses[1:4] = [Response(identity), Response(aps), Response(clients)]
    _, session = make_api(
        metadata(),
        Response({"error_code": 0, "stok": "private-auth-token"}),
        *responses,
        led_config("private-ap-id"),
        *(Response({"error_code": 0}) for _ in range(4)),
    )
    api = Ac100Api(session, "private-host.example", "private-username", "private-password")
    await api.async_login()
    await api.async_get_snapshot()
    await api.async_set_ap_led("private-ap-id", True)
    await api.async_reboot_ap("private-ap-id")
    await api.async_reboot_controller()
    await api.async_logout()
    records = [record for record in caplog.records if record.name == logger_name]
    messages = "\n".join(record.getMessage() for record in records)
    secrets = (
        "private-host.example",
        "private-username",
        "private-password",
        "private-auth-token",
        "private-ap-id",
        "private-ap-name",
        "private-client-name",
        "private-ssid",
        "02-AB-CD-99-88-77",
        "02-AB-CD-11-22-33",
        "02-AB-CD-44-55-66",
        "02:ab:cd:99:88:77",
        "02:ab:cd:11:22:33",
        "02:ab:cd:44:55:66",
        "198.51.100.111",
        "198.51.100.112",
        "http://",
        "/stok=",
        "error_code",
    )
    assert all(secret not in messages for secret in secrets)
    encoded_password = session.post.call_args_list[1].kwargs["json"]["login"]["password"]
    assert encoded_password not in messages
    assert len(records) >= 8
    assert all(record.levelno == logging.DEBUG for record in records)
    assert all(isinstance(arg, (int, bool)) for record in records for arg in record.args)
    assert any(record.args == (6, 14) for record in records)
    assert any(record.args == (True,) for record in records)


async def test_expiration_and_busy_debug_logs_are_safe_and_not_retried_repeatedly(caplog):
    logger_name = "custom_components.tplink_ac100.api"
    caplog.set_level(logging.DEBUG, logger=logger_name)
    api, session = make_api(
        Response(
            {"error_code": -40401, "data": {"code": -40401}, "error_msg": "private-raw-response"},
            status=401,
        ),
        metadata(),
        logged_in(),
        Response(
            {"error_code": -40100, "data": {"code": -57704}, "error_msg": "private-raw-response"},
            status=401,
        ),
    )
    api._stok = "private-old-token"
    with pytest.raises(Ac100SessionBusyError):
        await api.async_set_ap_led("private-ap-id", False)
    records = [record for record in caplog.records if record.name == logger_name]
    messages = "\n".join(record.getMessage() for record in records)
    assert messages.count("expired") == 1
    assert "another active session" in messages
    assert all(
        secret not in messages
        for secret in (
            "private-old-token",
            "private-ap-id",
            "private-raw-response",
            "synthetic-session",
            "controller.example",
            "example-password",
            "admin",
            "/stok=",
        )
    )
    assert all(isinstance(arg, (int, bool)) for record in records for arg in record.args)
    assert session.post.call_count == 4


async def test_api_debug_logging_respects_level_and_does_not_configure_other_loggers(caplog):
    logger_name = "custom_components.tplink_ac100.api"
    caplog.set_level(logging.WARNING, logger=logger_name)
    other_logger = logging.getLogger("aiohttp")
    other_level = other_logger.level
    root_level = logging.getLogger().level
    api, session = make_api(metadata(), logged_in(), Response({"error_code": 0}))
    await api.async_login()
    await api.async_logout()
    assert not any(record.name == logger_name for record in caplog.records)
    assert other_logger.level == other_level
    assert logging.getLogger().level == root_level
    session.close.assert_not_called()


@pytest.mark.parametrize(
    ("value", "expected"), [("off", False), ("on", True), (None, None), ("unknown", None)]
)
async def test_read_one_ap_led_wifi_sync_only_returns_a_boolean(value, expected):
    api, session = make_api(led_config("7", value))
    api._stok = "synthetic-session"
    assert await api.async_get_ap_led_wifi_sync("7") is expected
    assert session.post.call_args.kwargs["json"] == {
        "method": "get",
        "apmng_set": {
            "table": "ap_list",
            "filter": [{"entry_id": "7"}],
            "para": {"start": 0, "end": 0},
        },
    }
    assert session.post.call_count == 1


@pytest.mark.parametrize("value", ["on", None, "unknown", "OFF", "", False, 0])
async def test_led_control_rejects_sync_not_explicitly_off(value):
    api, session = make_api(led_config("7", value))
    api._stok = "synthetic-session"
    with pytest.raises(Ac100UnsafeLedError):
        await api.async_set_ap_led("7", False)
    assert session.post.call_count == 1
    assert session.post.call_args.kwargs["json"]["method"] == "get"


@pytest.mark.parametrize(
    "rows",
    [
        [],
        [{"ap_list_8": {"entry_id": "8", "led_wifi_sync": "off"}}],
        [{"ap_list_7": {"entry_id": "7"}}],
        [{"ap_list_7": {"led_wifi_sync": "off"}}],
        [
            {"ap_list_7": {"entry_id": "7", "led_wifi_sync": "off"}},
            {"ap_list_8": {"entry_id": "8", "led_wifi_sync": "off"}},
        ],
        [{"ap_list_7": "malformed"}],
    ],
)
async def test_led_control_requires_exactly_one_matching_ap_config_row(rows):
    api, session = make_api(Response({"error_code": 0, "apmng_set": {"ap_list": rows}}))
    api._stok = "synthetic-session"
    with pytest.raises(Ac100UnsafeLedError):
        await api.async_set_ap_led("7", True)
    assert session.post.call_count == 1
    assert session.post.call_args.kwargs["json"]["method"] == "get"


@pytest.mark.parametrize(
    ("response", "error"),
    [
        (
            Response(error=aiohttp.ClientConnectionError("private-config-error")),
            Ac100ConnectionError,
        ),
        (Response(text="private-config-error"), Ac100ProtocolError),
        (Response({"error_code": -40401, "data": {"code": -40404}}, status=401), Ac100AuthError),
        (
            Response({"error_code": -40100, "data": {"code": -57704}}, status=401),
            Ac100SessionBusyError,
        ),
    ],
)
async def test_led_safety_query_failure_never_sends_a_write(response, error):
    api, session = make_api(response)
    api._stok = "synthetic-session"
    with pytest.raises(error) as exc:
        await api.async_set_ap_led("7", False)
    assert "private-config-error" not in str(exc.value)
    assert session.post.call_count == 1
    assert session.post.call_args.kwargs["json"]["method"] == "get"


async def test_led_safety_setting_is_rechecked_on_every_action_not_cached():
    api, session = make_api(
        led_config("7", "off"), Response({"error_code": 0}), led_config("7", "on")
    )
    api._stok = "synthetic-session"
    await api.async_set_ap_led("7", True)
    with pytest.raises(Ac100UnsafeLedError):
        await api.async_set_ap_led("7", False)
    bodies = [call.kwargs["json"] for call in session.post.call_args_list]
    assert [body["method"] for body in bodies] == ["get", "set", "get"]
    assert bodies[0] == bodies[2]
    assert bodies[1]["apmng_status"]["para"] == {"led_status": "1"}


async def test_transient_ap_configuration_credentials_do_not_escape_to_state_or_logs(caplog):
    caplog.set_level(logging.DEBUG, logger="custom_components.tplink_ac100.api")
    canaries = ("CANARY_AP_USERNAME", "CANARY_AP_PASSWORD", "CANARY_AP_ACCOUNT_PASSWORD")
    config = led_config(
        "0", "off", username=canaries[0], password=canaries[1], account_password=canaries[2]
    )
    api, session = make_api(config, Response({"error_code": 0}), *snapshot_responses())
    api._stok = "synthetic-session"
    attributes_before = set(vars(api))
    await api.async_set_ap_led("0", False)
    snapshot = await api.async_get_snapshot()
    coordinator = SimpleNamespace(data=snapshot, last_update_success=True)
    entry = SimpleNamespace(runtime_data=coordinator, options={})
    diagnostics = await async_get_config_entry_diagnostics(None, entry)
    retained_api_state = {key: value for key, value in vars(api).items() if key != "_session"}
    retained_text = (
        repr(retained_api_state) + json.dumps(asdict(snapshot)) + json.dumps(diagnostics)
    )
    assert set(vars(api)) == attributes_before
    assert all(canary not in retained_text and canary not in caplog.text for canary in canaries)
    bodies = [call.kwargs["json"] for call in session.post.call_args_list]
    assert sum("apmng_set" in body for body in bodies) == 1
    assert bodies[0]["apmng_set"]["para"] == {"start": 0, "end": 0}
    assert all("apmng_set" not in body for body in bodies[2:])
    records = [
        record for record in caplog.records if record.name == "custom_components.tplink_ac100.api"
    ]
    assert all(isinstance(arg, (int, bool)) for record in records for arg in record.args)
