"""Offline protocol tests using synthetic credentials and anonymized real response shapes."""

import asyncio
import copy
import hashlib
import json
import logging
from dataclasses import asdict, replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import aiohttp
import pytest

from custom_components.tplink_ac100.api import (
    Ac100AccountLockedError,
    Ac100Api,
    Ac100AuthError,
    Ac100ConnectionError,
    Ac100Error,
    Ac100ProtocolError,
    Ac100SessionBusyError,
    Ac100SessionExpiredError,
    Ac100UnsafeLedError,
    normalize_host,
)
from custom_components.tplink_ac100.const import UPDATE_INTERVAL
from custom_components.tplink_ac100.diagnostics import async_get_config_entry_diagnostics
from custom_components.tplink_ac100.switch import Ac100LedSwitch
from custom_components.tplink_ac100.terminal import TerminalState

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
    """One poll's answers: the core reads, the host table, and the radios of its one network."""
    return [
        Response(load(name))
        for name in (
            "controller.json",
            "identity.json",
            "aps.json",
            "clients.json",
            "clients_page2.json",
            "hosts.json",
            "hosts_page2.json",
            "radio_bind_list.json",
        )
    ]


def host_rows():
    """The fixture's host table as wire rows, in table order."""
    return [
        next(iter(wrapper.values()))
        for name in ("hosts.json", "hosts_page2.json")
        for wrapper in load(name)["host_management"]["host_info"]
    ]


def host_responses(rows, count=None):
    """Answer the host table's page requests from wire rows, ten rows to a page.

    The controller's own count can be made to disagree with the rows.
    """
    count = len(rows) if count is None else count
    return [
        Response(
            {
                "error_code": 0,
                "host_management": {
                    "count": {"host_info": count},
                    "host_info": [
                        {f"host_info_{start + index}": row}
                        for index, row in enumerate(rows[start : start + 10])
                    ],
                },
            }
        )
        for start in range(0, max(len(rows), 1), 10)
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
    pages = [request["apmng_client"]["para"] for request in requests if "apmng_client" in request]
    assert pages == [{"start": 0, "end": 9}, {"start": 10, "end": 19}]
    session.close.assert_not_called()


def speeds_of(snapshot):
    """Each client's (upload, download) speeds by the last byte of its MAC."""
    return {
        client.mac[-2:]: (client.upload_kilobytes_per_second, client.download_kilobytes_per_second)
        for client in snapshot.clients
    }


def host(*, ssid="Example WiFi 1", ap="Example AP 1", up="1", down="1", mac="02-00-00-00-00-07"):
    """A host row; by default the one the fixtures list for the first client."""
    row = host_rows()[0]
    return {**row, "mac": mac, "ssid": ssid, "ap_name": ap, "up_speed": up, "down_speed": down}


async def snapshot_with_hosts(*responses):
    """Poll with the given answers to the host table's page requests.

    The answer of the radio list follows them: it is only asked for after a readable table.
    """
    api, session = make_api(*snapshot_responses()[:5], *responses, snapshot_responses()[7])
    api._stok = "synthetic-session"
    return await api.async_get_snapshot(), session


def host_requests(session):
    return [
        call for call in session.post.call_args_list if "host_management" in call.kwargs["json"]
    ]


async def test_clients_get_their_speeds_from_the_host_table_only_if_they_are_listed():
    snapshot, _ = await snapshot_with_hosts(*snapshot_responses()[5:])
    assert speeds_of(snapshot) == {
        "07": (1.0, 1.0),
        "08": (6.0, 67.0),
        "09": (1.0, 1.0),
        "0a": (12.0, 40.0),
        "0b": (1.0, 1.0),
        "0c": (3.0, 9.0),
        "0d": (1.0, 1.0),
        "0e": (None, None),  # listed, but with the placeholders of a host that has left
        "0f": (2.0, 5.0),
        "10": (1.0, 1.0),
        "11": (4.0, 20.0),
        "12": (1.0, 1.0),
        "13": (1.0, 1.0),  # two rows: a stale one for another AP, then the current one
        "14": (5.0, 3.0),
    }
    # The table also lists a host that has left; only the client list says who is connected.
    assert "02:00:00:00:00:30" not in {client.mac for client in snapshot.clients}


async def test_the_poll_adds_exactly_the_host_table_pages():
    api, session = make_api(*snapshot_responses())
    api._stok = "synthetic-session"
    await api.async_get_snapshot()
    bodies = [call.kwargs["json"] for call in session.post.call_args_list]
    assert [sorted(set(body) - {"method"}) for body in bodies] == [
        ["firmware", "system"],
        ["interface"],
        ["apmng_status"],
        ["apmng_client"],
        ["apmng_client"],
        ["host_management"],
        ["host_management"],
        ["apmng_wserv"],
    ]
    assert all(body["method"] == "get" for body in bodies)
    assert bodies[-3:-1] == [
        {
            "method": "get",
            "host_management": {
                "table": "host_info",
                "filter": [{"group_id": ["-1"]}],
                "para": {"start": start, "end": start + 9},
            },
        }
        for start in (0, 10)
    ]
    # Every fixture host is connected to wireless network 0, so its radios are read once.
    assert bodies[-1] == {
        "method": "get",
        "apmng_wserv": {
            "table": "radio_bind_list",
            "filter": [{"group_id": ["-1"], "bind_status": "1", "serv_id": "0"}],
        },
    }


ELSEWHERE = {"ssid": "Example WiFi 2", "ap": "Example AP 2"}
SAME_SSID = {"ssid": "Example WiFi 1", "ap": "Example AP 2"}
SAME_AP = {"ssid": "Example WiFi 2", "ap": "Example AP 1"}


@pytest.mark.parametrize(
    ("rows", "expected"),
    [
        pytest.param([host(up="5", down="6")], (5.0, 6.0), id="one row"),
        pytest.param([host(up="0", down="0")], (0.0, 0.0), id="zero is a speed"),
        pytest.param(
            [host(**ELSEWHERE, up="5", down="6"), host(up="7", down="8")],
            (7.0, 8.0),
            id="the row for the client's SSID and AP wins",
        ),
        pytest.param(
            [host(up="5", down="6"), host(up="7", down="8")],
            (5.0, 6.0),
            id="the first of two matching rows",
        ),
        pytest.param(
            [host(**ELSEWHERE, up="5", down="6"), host(**ELSEWHERE, up="7", down="8")],
            (5.0, 6.0),
            id="no row matches: the first",
        ),
        pytest.param(
            [host(**SAME_SSID, up="5", down="6"), host(**SAME_AP, up="7", down="8")],
            (5.0, 6.0),
            id="a match needs the SSID and the AP",
        ),
        pytest.param(
            [host(up="---", down="---"), host(**ELSEWHERE, up="7", down="8")],
            (7.0, 8.0),
            id="a matching row without speeds is passed over",
        ),
        pytest.param(
            [host(**ELSEWHERE, up="---", down="---"), host(up="7", down="8")],
            (7.0, 8.0),
            id="a stale row first",
        ),
        pytest.param(
            [host(up="---", down="---"), host(**ELSEWHERE, up="---", down="---")],
            (None, None),
            id="no row has a speed",
        ),
        pytest.param([host(up="5", down="---")], (5.0, None), id="no download speed"),
        pytest.param([host(up="---", down="6")], (None, 6.0), id="no upload speed"),
    ],
)
async def test_a_client_uses_the_host_row_that_fits_its_ssid_and_ap(rows, expected):
    snapshot, _ = await snapshot_with_hosts(*host_responses(rows))
    assert speeds_of(snapshot)["07"] == expected
    assert all(speeds == (None, None) for mac, speeds in speeds_of(snapshot).items() if mac != "07")


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("152", 152.0),
        ("2.5", 2.5),
        *((raw, None) for raw in ("---", "", "abc", "-3", "nan", None, [], True)),
    ],
)
async def test_a_speed_is_a_non_negative_number_and_anything_else_is_unknown(raw, expected):
    snapshot, _ = await snapshot_with_hosts(*host_responses([host(up=raw, down=raw)]))
    assert speeds_of(snapshot)["07"] == (expected, expected)


BROKEN_HOST_TABLES = {
    "rejected": lambda: [Response({"error_code": -40210})],
    "not JSON": lambda: [Response(text="not json")],
    "no table": lambda: [Response({"error_code": 0})],
    "table that is not a list": lambda: [
        Response({"error_code": 0, "host_management": {"count": {"host_info": 1}, "host_info": 1}})
    ],
    "no count": lambda: [
        Response({"error_code": 0, "host_management": {"count": {}, "host_info": []}})
    ],
    "count that is not a number": lambda: [
        Response(
            {"error_code": 0, "host_management": {"count": {"host_info": "x"}, "host_info": []}}
        )
    ],
    "huge count": lambda: host_responses(host_rows(), count=10**9),
    "short first page": lambda: host_responses(host_rows()[:3], count=16),
    "short last page": lambda: host_responses(host_rows()[:12], count=16),
    "row that is not an object": lambda: host_responses([*host_rows()[:4], "unexpected"]),
    "row without a MAC": lambda: host_responses(
        [*host_rows()[:4], {key: value for key, value in host_rows()[4].items() if key != "mac"}]
    ),
    "row with an invalid MAC": lambda: host_responses([*host_rows()[:4], host(mac="not-a-mac")]),
}


@pytest.mark.parametrize("broken", BROKEN_HOST_TABLES)
async def test_an_unreadable_host_table_leaves_the_speeds_unknown_and_nothing_else(broken, caplog):
    caplog.set_level(logging.DEBUG, logger="custom_components.tplink_ac100.api")
    baseline, _ = await snapshot_with_hosts(*host_responses([]))
    caplog.clear()
    snapshot, session = await snapshot_with_hosts(*BROKEN_HOST_TABLES[broken]())
    # An empty table is a readable one with no record; an unreadable table is no information.
    assert baseline.terminals == TerminalState(())
    assert snapshot.terminals is None
    assert snapshot == replace(baseline, terminals=None)
    assert not [
        call for call in session.post.call_args_list if "apmng_wserv" in call.kwargs["json"]
    ]
    assert set(speeds_of(snapshot).values()) == {(None, None)}
    assert [record.getMessage() for record in caplog.records] == [
        "Controller client speeds are unavailable",
        "Controller snapshot received (aps=6, clients=14)",
    ]


async def test_a_bad_later_page_discards_the_rows_of_the_earlier_pages():
    snapshot, session = await snapshot_with_hosts(*host_responses(host_rows()[:12], count=16))
    assert set(speeds_of(snapshot).values()) == {(None, None)}
    assert len(host_requests(session)) == 2


async def test_a_host_table_over_the_row_limit_is_refused_after_its_first_page():
    def table(size):
        rows = [host(up="5", down="6")]
        rows += [host(mac=f"02-00-00-01-{n // 256:02X}-{n % 256:02X}") for n in range(size - 1)]
        return host_responses(rows)

    snapshot, session = await snapshot_with_hosts(*table(257))
    assert speeds_of(snapshot)["07"] == (None, None)
    assert len(host_requests(session)) == 1
    snapshot, _ = await snapshot_with_hosts(*table(256))
    assert speeds_of(snapshot)["07"] == (5.0, 6.0)


@pytest.mark.parametrize(
    "answer",
    [
        Response({"error_code": -40100}, status=401),
        Response({"error_code": -57705}, status=401),
        Response(status=403),
    ],
)
async def test_a_host_table_this_account_may_not_read_is_not_a_credentials_failure(answer, caplog):
    """The client list was just read, so a refusal here is about this table only."""
    caplog.set_level(logging.DEBUG, logger="custom_components.tplink_ac100.api")
    baseline, _ = await snapshot_with_hosts(*host_responses([]))
    snapshot, session = await snapshot_with_hosts(answer)
    assert snapshot == replace(baseline, terminals=None)
    assert len(host_requests(session)) == 1
    assert "Controller client speeds are unavailable" in caplog.text


@pytest.mark.parametrize(
    ("answers", "error"),
    [
        ([Response(error=aiohttp.ClientConnectionError("private-url"))], Ac100ConnectionError),
        ([Response({"error_code": -57704}, status=401)], Ac100SessionBusyError),
        ([Response({"error_code": -40404}, status=401)], Ac100AccountLockedError),
        (
            [
                Response({"error_code": -40401, "data": {"code": -40401}}, status=401),
                metadata(),
                logged_in(),
                Response({"error_code": -40401, "data": {"code": -40401}}, status=401),
            ],
            Ac100SessionExpiredError,
        ),
    ],
)
async def test_connection_and_session_failures_in_the_host_table_still_fail_the_poll(
    answers, error
):
    with pytest.raises(error) as exc:
        await snapshot_with_hosts(*answers)
    assert "private-url" not in str(exc.value)


async def test_a_failed_sign_in_during_the_host_table_is_not_an_unreadable_table():
    expired = Response({"error_code": -40401, "data": {"code": -40401}}, status=401)
    with pytest.raises(Ac100ProtocolError):
        await snapshot_with_hosts(expired, metadata(), Response(text="not json"))


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


async def test_read_still_expired_after_reauthentication_is_not_a_credentials_failure():
    expired = Response({"error_code": -40401, "data": {"code": -40401}}, status=401)
    api, session = make_api(expired, metadata(), logged_in(), expired)
    api._stok = "old-session"
    with pytest.raises(Ac100SessionExpiredError) as exc:
        await api.async_get_ap_led_wifi_sync("7")
    assert not isinstance(exc.value, Ac100AuthError)
    assert session.post.call_count == 4


@pytest.mark.parametrize(
    "call",
    [
        lambda api: api.async_reboot_controller(),
        lambda api: api.async_reboot_ap("0"),
    ],
)
async def test_expired_session_never_replays_a_management_command(call):
    expired = Response({"error_code": -40401, "data": {"code": -40401}}, status=401)
    api, session = make_api(expired)
    api._stok = "old-session"
    with pytest.raises(Ac100SessionExpiredError):
        await call(api)
    assert session.post.call_count == 1
    assert api._stok is None


async def test_led_write_expiry_after_the_safety_read_is_not_replayed():
    expired = Response({"error_code": -40401, "data": {"code": -40401}}, status=401)
    api, session = make_api(led_config("7", "off"), expired)
    api._stok = "old-session"
    with pytest.raises(Ac100SessionExpiredError):
        await api.async_set_ap_led("7", True)
    methods = [call.kwargs["json"]["method"] for call in session.post.call_args_list]
    assert methods == ["get", "set"]
    assert api._stok is None


async def test_next_request_signs_in_again_after_an_unreplayed_write_expiry():
    expired = Response({"error_code": -40401, "data": {"code": -40401}}, status=401)
    api, session = make_api(
        expired, metadata(), logged_in(), led_config("7", "off"), Response({"error_code": 0})
    )
    api._stok = "old-session"
    with pytest.raises(Ac100SessionExpiredError):
        await api.async_reboot_controller()
    await api.async_set_ap_led("7", True)
    methods = [call.kwargs["json"]["method"] for call in session.post.call_args_list]
    assert methods == ["do", "do", "do", "get", "set"]


@pytest.mark.parametrize(
    "result",
    [
        {"error_code": -40404},
        {"error_code": -40401, "data": {"code": -40404}},
    ],
)
async def test_locked_account_is_reported_without_retry(result):
    api, session = make_api(Response(result, status=401))
    api._stok = "synthetic-session"
    with pytest.raises(Ac100AccountLockedError) as exc:
        await api.async_reboot_controller()
    assert not isinstance(exc.value, Ac100AuthError)
    assert session.post.call_count == 1


async def test_locked_account_on_login_is_not_a_bad_password():
    api, session = make_api(
        metadata(), Response({"error_code": -40401, "data": {"code": -40404}}, status=401)
    )
    with pytest.raises(Ac100AccountLockedError):
        await api.async_login()
    assert session.post.call_count == 2


async def test_login_is_idempotent_while_a_token_is_held():
    api, session = make_api()
    api._stok = "held-session"
    await api.async_login()
    assert api._stok == "held-session"
    assert session.post.call_count == 0


@pytest.mark.parametrize(
    "response",
    [
        Response(status=401),
        Response(text="not JSON", status=401),
        Response({"error_code": -40401, "data": {"code": -40402}}, status=401),
        Response({"error_code": -40401, "data": {"code": -40403}}, status=401),
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
    responses[4] = Response(page2)
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
    hosts = load("hosts.json")
    host = next(iter(hosts["host_management"]["host_info"][0].values()))
    host.update(
        hostname="private-host-name",
        ssid="private-ssid",
        ap_name="private-ap-name",
        mac="02-AB-CD-44-55-66",
        ip="198.51.100.112",
    )
    responses = snapshot_responses()
    responses[1:4] = [Response(identity), Response(aps), Response(clients)]
    responses[5] = Response(hosts)
    _, session = make_api(
        metadata(),
        Response({"error_code": 0, "stok": "private-auth-token"}),
        *responses,
        led_config("private-ap-id"),
        *(Response({"error_code": 0}) for _ in range(4)),
    )
    api = Ac100Api(session, "private-host.example", "private-username", "private-password")
    await api.async_login()
    snapshot = await api.async_get_snapshot()
    assert snapshot.clients[0].download_kilobytes_per_second == 1.0
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
        "private-host-name",
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
        (
            Response({"error_code": -40401, "data": {"code": -40404}}, status=401),
            Ac100AccountLockedError,
        ),
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
    coordinator = SimpleNamespace(
        data=snapshot, last_update_success=True, update_interval=UPDATE_INTERVAL
    )
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
