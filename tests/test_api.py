"""Offline protocol tests using synthetic credentials and anonymized real response shapes."""

import copy
import hashlib
import json
from pathlib import Path
from unittest.mock import MagicMock

import aiohttp
import pytest

from custom_components.tplink_ac100.api import (
    Ac100Api,
    Ac100AuthError,
    Ac100ConnectionError,
    Ac100ProtocolError,
    normalize_host,
)

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
    assert snapshot.aps[0].is_online
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
    api, session = make_api(expired, metadata(), logged_in(), Response({"error_code": 0}))
    api._stok = "old-session"
    await api.async_set_ap_led("0", True)
    assert session.post.call_count == 4
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


async def test_conflicting_session_never_attempts_force_or_reauthentication():
    api, session = make_api(Response({"error_code": -40100, "data": {"code": -57704}}))
    api._stok = "synthetic-session"
    with pytest.raises(Ac100AuthError):
        await api.async_reboot_controller()
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
    api, session = make_api(Response({"error_code": 0}))
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
