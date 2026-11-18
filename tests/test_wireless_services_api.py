"""The opt-in SSID table: reading, the field allow-list and one-field writes, on synthetic data.

The table's rows hold Wi-Fi and RADIUS secrets next to the settings. These tests plant canaries in
every secret field and check that none of them survives the API. Setting tokens and values follow
the controller's wireless service page (apmng_wserv.htm): ``enable`` and the IGMP switches are
on/off, ``ssidbrd`` and ``isolate`` are 1/0, and ``encryption`` is one of 0, 1, 2 or 4.
"""

import asyncio
import json
import logging
import re
from dataclasses import asdict, fields
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from urllib.parse import quote

import aiohttp
import pytest
from homeassistant.const import Platform
from pytest_homeassistant.common import MockConfigEntry

from custom_components.tplink_ac100.api import (
    Ac100AccountLockedError,
    Ac100Api,
    Ac100ConnectionError,
    Ac100Error,
    Ac100ProtocolError,
    Ac100SessionBusyError,
    Ac100SessionExpiredError,
)
from custom_components.tplink_ac100.const import (
    CONF_ENABLE_CONTROL,
    CONF_MONITOR_WIRELESS_SERVICES,
    DOMAIN,
    UPDATE_INTERVAL,
)
from custom_components.tplink_ac100.diagnostics import async_get_config_entry_diagnostics
from custom_components.tplink_ac100.models import Controller, Snapshot
from custom_components.tplink_ac100.monitoring import (
    WIRELESS_SERVICE_FLAGS,
    WIRELESS_SERVICE_SECURITY,
    ConfigurationState,
    MonitoringState,
    WirelessService,
)

SECRETS = {
    "key": "CANARY_WIFI_KEY",
    "priv_key": "CANARY_PRIVATE_KEY",
    "ppsk_group": "CANARY_PPSK_GROUP",
    "radius_pwd": "CANARY_RADIUS_PWD",
    "radius_ip": "198.51.100.77",
    "radius_port": "CANARY_RADIUS_PORT",
    "radius_acct_port": "CANARY_RADIUS_ACCOUNTING",
}
CANARIES = (*SECRETS.values(), "CANARY_DESCRIPTION", "CANARY_FREQUENCY_BINDING")
NAMES = ("Home", "Guest", "Cameras")
TABLE = "apmng_wserv"
SSID_READ = {"method": "get", TABLE: {"table": "wlan_serv", "para": {"start": 0, "end": 9}}}
ALLOWED_FIELDS = {
    "serv_id",
    "ssid",
    "enable",
    "ssidbrd",
    "isolate",
    "encryption",
    "igmp_snooping_switch",
    "igmp_query_switch",
    "default_bind_vlan",
    "bind_vlan_type",
}
SWITCHES = [
    # (WirelessService field, controller field, token for True, token for False)
    ("enabled", "enable", "on", "off"),
    ("broadcast", "ssidbrd", "1", "0"),
    ("isolation", "isolate", "1", "0"),
    ("igmp_snooping", "igmp_snooping_switch", "on", "off"),
    ("igmp_querier", "igmp_query_switch", "on", "off"),
]
EXPIRED = {"error_code": -40401, "data": {"code": -40401}}


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


def wire(value):
    """URI-encode string values like the controller does; object keys stay as they are."""
    if isinstance(value, dict):
        return {key: wire(item) for key, item in value.items()}
    if isinstance(value, list):
        return [wire(item) for item in value]
    if isinstance(value, str):
        return quote(value, safe="~()*!.'-")
    return value


def service_row(number, ssid=None, **extra):
    """A row as the controller sends it: the settings, and the secrets that must never leave."""
    return {
        "serv_id": str(number),
        "ssid": ssid or f"Network {number}",
        "ssid_code_type": "1",
        "desc": "CANARY_DESCRIPTION",
        "enable": "on",
        "ssidbrd": "1",
        "isolate": "0",
        "encryption": "1",
        "auth": "3",
        "cipher": "2",
        "key_update_intv": "86400",
        "igmp_snooping_switch": "on",
        "igmp_query_switch": "off",
        "igmp_snooping_enable": "on",
        "igmp_drop_unknown": "0",
        "igmp_2g_rate": "1",
        "igmp_5g_rate": "6",
        "igmp_query_interval": "60",
        "igmp_max_resp_time": "10",
        "bw_ctrl_enable": "0",
        "bw_ctrl_mode": "1",
        "up_limit": "0",
        "down_limit": "0",
        "auto_bind": "on",
        "bind_vlan_type": "single_vlan",
        "default_bind_vlan": "0",
        "default_bind_freq": "CANARY_FREQUENCY_BINDING",
        "vlan_pool_id": "",
        "mlo_switch": "0",
        "central_forward_switch": "off",
        **SECRETS,
        **extra,
    }


def port_row():
    return {
        ".name": "port_1",
        "switch_dev": "switch0",
        "port_name": "1",
        "port_state": "connected",
        "link_speed": "1000M",
    }


def interface_row():
    return {"interface": "lan", "tx_bps": "5", "rx_bps": "4", "tx_pps": "17", "rx_pps": "29"}


class FakeController:
    """A SLP double answering GETs from dictionaries; it records every request it receives.

    ``script`` queues responses that replace the next answer to a kind of request: ``ssid``
    (the SSID table), ``set`` (any write) or ``login`` (the sign-in requests).
    """

    def __init__(self, rows):
        self.sections = {("system", "cfg_status"): {"status": "Not Saved"}}
        self.tables = {
            (TABLE, "wlan_serv"): rows,
            ("port", "port"): [port_row()],
            ("system", "ifstat_list"): [interface_row()],
        }
        self.counts = {}
        self.script = {}
        self.bodies = []
        self.urls = []
        self.gate_ssid = False
        self.gate = None
        self.gate_ready = asyncio.Event()

    @property
    def rows(self):
        return self.tables[TABLE, "wlan_serv"]

    def __call__(self, url, **kwargs):
        body = kwargs["json"]
        self.urls.append(url)
        self.bodies.append(body)
        if url.endswith("/"):
            if self.script.get("login"):
                return self.script["login"].pop(0)
            if "user_management" in body:
                return Response({"error_code": 0, "encrypt_type": ["-3"]})
            return Response({"error_code": 0, "stok": "renewed-session"})
        kind = "set" if body["method"] == "set" else "ssid" if self.reads_ssids(body) else None
        if kind and self.script.get(kind):
            return self.script[kind].pop(0)
        response = self.answer(body)
        if self.gate_ssid and kind == "ssid" and self.gate is None:
            self.gate = response = GatedResponse(response)
            self.gate_ready.set()
            return response
        return Response(response)

    @staticmethod
    def reads_ssids(body):
        return body["method"] == "get" and body.get(TABLE, {}).get("table") == "wlan_serv"

    def apply(self, body):
        """Change only the given fields of the rows that the update names."""
        query = body.get(TABLE, {})
        wanted = {item["serv_id"] for item in query.get("filter", [])}
        for row in self.rows:
            if row["serv_id"] in wanted:
                row.update(query.get("para", {}))

    def answer(self, body):
        if body["method"] == "set":
            self.apply(body)
        if body["method"] != "get":
            return {"error_code": 0}
        result = {"error_code": 0}
        for module, query in body.items():
            if module == "method":
                continue
            if "name" in query:
                result[module] = {
                    name: wire(self.sections[module, name])
                    for name in query["name"]
                    if (module, name) in self.sections
                }
                continue
            table = query["table"]
            if (module, table) not in self.tables:
                return {"error_code": -40210}
            rows = self.tables[module, table]
            start = query.get("para", {}).get("start", 0)
            end = query.get("para", {}).get("end", len(rows) - 1)
            result[module] = {
                table: [
                    {f"{table}_{start + i}": wire(row)}
                    for i, row in enumerate(rows[start : end + 1])
                ],
                "count": {table: self.counts.get((module, table), len(rows))},
            }
        return result


def make_api(rows=None):
    if rows is None:
        rows = [service_row(number, name) for number, name in enumerate(NAMES, start=1)]
    controller = FakeController(rows)
    session = MagicMock(spec=aiohttp.ClientSession)
    session.post.side_effect = controller
    api = Ac100Api(session, "controller.example", "admin", "example-password")
    api._stok = "synthetic-session"
    return api, session, controller


def writes(controller):
    return [body for body in controller.bodies if body["method"] == "set"]


async def read(api):
    return await api.async_get_monitoring(wireless_services=True)


# Reading


async def test_the_ssid_table_is_not_requested_unless_asked():
    api, _, controller = make_api()
    assert (await api.async_get_monitoring()).wireless_services is None
    assert (await api.async_get_monitoring(wireless_services=False)).wireless_services is None
    assert not [body for body in controller.bodies if TABLE in body]


async def test_every_page_is_read_with_the_ui_request_and_each_row_is_projected():
    rows = [service_row(number, f"Network {number}") for number in range(23)]
    api, session, controller = make_api(rows)
    state = await read(api)
    assert [body for body in controller.bodies if TABLE in body] == [
        {"method": "get", TABLE: {"table": "wlan_serv", "para": {"start": s, "end": s + 9}}}
        for s in (0, 10, 20)
    ]
    assert state.wireless_services == tuple(
        WirelessService(
            str(number),
            f"Network {number}",
            enabled=True,
            broadcast=True,
            isolation=False,
            security="wpa_psk_wpa2_psk",
            igmp_snooping=True,
            igmp_querier=False,
            vlan=None,
        )
        for number in range(23)
    )
    assert all(body["method"] == "get" for body in controller.bodies)
    assert set(controller.urls) == {"http://controller.example/stok=synthetic-session/ds"}
    assert state.values == {"configuration_not_saved": True}
    assert {"system.cfg_status", "port.port", "system.ifstat_list"} <= state.available_sections
    assert state.ports and state.interfaces
    session.close.assert_not_called()


@pytest.mark.parametrize(("key", "field", "on", "off"), SWITCHES)
async def test_on_off_tokens_follow_the_controller_page(key, field, on, off):
    other_family = "1" if on == "on" else "on"
    rows = [
        service_row(1, "On", **{field: on}),
        service_row(2, "Off", **{field: off}),
        service_row(3, "Other", **{field: "maybe"}),
        service_row(4, "Spelling", **{field: {"on": "ON", "1": "01"}[on]}),
        service_row(5, "Other family", **{field: other_family}),
    ]
    api, _, _ = make_api(rows)
    state = await read(api)
    assert [getattr(item, key) for item in state.wireless_services] == [
        True,
        False,
        None,
        None,
        None,
    ]


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("0", "open"),
        ("1", "wpa_psk_wpa2_psk"),
        ("2", "wpa_wpa2_enterprise"),
        ("4", "wpa2_psk_wpa3_sae"),
        ("3", None),
        ("5", None),
        ("01", None),
        ("", None),
        (1, None),
        (None, None),
    ],
)
async def test_security_follows_the_options_of_the_controller_page(raw, expected):
    api, _, _ = make_api([service_row(1, "Home", encryption=raw)])
    assert (await read(api)).wireless_services[0].security == expected


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({}, None),  # the page shows 0 as "---": no VLAN is set
        ({"default_bind_vlan": "1"}, 1),
        ({"default_bind_vlan": "4094"}, 4094),
        ({"default_bind_vlan": "4095"}, None),
        ({"default_bind_vlan": "-5"}, None),
        ({"default_bind_vlan": "ten"}, None),
        ({"default_bind_vlan": ""}, None),
        ({"default_bind_vlan": 20}, 20),
        ({"default_bind_vlan": True}, None),
        ({"default_bind_vlan": "20", "bind_vlan_type": "single_vlan"}, 20),
        ({"default_bind_vlan": "20", "bind_vlan_type": "vlan_pool"}, None),
        ({"default_bind_vlan": "20", "bind_vlan_type": "unexpected"}, None),
        ({"default_bind_vlan": "20", "bind_vlan_type": None}, None),
    ],
)
async def test_vlan_is_the_default_bind_vlan_of_a_single_vlan_network(overrides, expected):
    api, _, _ = make_api([service_row(1, "Home", **overrides)])
    assert (await read(api)).wireless_services[0].vlan == expected


@pytest.mark.parametrize("weird", [None, [], {}, True, "x" * 1000, "\x00", "\u2028"])
async def test_unexpected_value_forms_are_unknown_and_never_raise(weird):
    changed = {field: weird for _, field, _, _ in SWITCHES}
    changed |= {"encryption": weird, "default_bind_vlan": weird}
    api, _, _ = make_api([service_row(1, "Home", **changed)])
    service = (await read(api)).wireless_services[0]
    assert service == WirelessService("1", "Home")


@pytest.mark.parametrize(
    "name",
    [
        "Home",
        "My Wi-Fi ",
        " padded",
        "Caf\u00e9 \u65e5\u672c\u8a9e",
        "a" * 96,
        "50% off",
        "a/b?c=d&e",
    ],
)
async def test_the_name_is_the_ssid_exactly_as_sent(name):
    api, _, _ = make_api([service_row(1, name)])
    assert (await read(api)).wireless_services[0].name == name


async def test_the_identity_is_the_service_id_not_the_name_or_the_row_order():
    api, _, controller = make_api()
    first = await read(api)
    controller.rows.reverse()
    controller.rows[0]["ssid"] = "Renamed"
    second = await read(api)
    assert [item.service_id for item in second.wireless_services] == ["3", "2", "1"]
    by_id = {item.service_id: item for item in first.wireless_services}
    assert next(i for i in second.wireless_services if i.service_id == "3").name == "Renamed"
    assert by_id["3"].name == "Cameras"


async def test_a_table_over_the_row_limit_is_refused_after_its_first_page():
    api, _, controller = make_api([service_row(number) for number in range(257)])
    assert (await read(api)).wireless_services is None
    assert len([body for body in controller.bodies if TABLE in body]) == 1
    del controller.rows[256:]
    assert len((await read(api)).wireless_services) == 256


async def test_an_empty_table_is_read_and_has_no_networks():
    api, _, _ = make_api([])
    state = await read(api)
    assert state.wireless_services == ()
    assert state.wireless_services is not None


def drop_id(controller):
    del controller.rows[1]["serv_id"]


def blank_id(controller):
    controller.rows[1]["serv_id"] = ""


def text_id(controller):
    controller.rows[1]["serv_id"] = "second"


def negative_id(controller):
    controller.rows[1]["serv_id"] = "-1"


def repeated_id(controller):
    controller.rows[1]["serv_id"] = controller.rows[0]["serv_id"]


def huge_count(controller):
    controller.counts[TABLE, "wlan_serv"] = 10**9


def count_over_the_limit(controller):
    controller.counts[TABLE, "wlan_serv"] = 257


def short_page(controller):
    controller.counts[TABLE, "wlan_serv"] = 12


def nested_row(controller):
    controller.rows.append("unexpected")


def rejected(controller):
    controller.script["ssid"] = [Response({"error_code": -40210})]


def not_json(controller):
    controller.script["ssid"] = [Response(text="not json")]


def not_a_table(controller):
    controller.script["ssid"] = [
        Response({"error_code": 0, TABLE: {"wlan_serv": "x", "count": {"wlan_serv": 1}}})
    ]


def without_a_count(controller):
    controller.script["ssid"] = [Response({"error_code": 0, TABLE: {"wlan_serv": []}})]


def blank_name(controller):
    controller.rows[1]["ssid"] = "  "


def long_name(controller):
    controller.rows[1]["ssid"] = "a" * 97


def unprintable_name(controller):
    controller.rows[1]["ssid"] = "line\nbreak"


def number_as_name(controller):
    controller.rows[1]["ssid"] = 5


UNUSABLE = [
    drop_id,
    blank_id,
    text_id,
    negative_id,
    repeated_id,
    huge_count,
    count_over_the_limit,
    short_page,
    nested_row,
    rejected,
    not_json,
    not_a_table,
    without_a_count,
    blank_name,
    long_name,
    unprintable_name,
    number_as_name,
]


@pytest.mark.parametrize(
    "name",
    ["Family \U0001f468\u200d\U0001f469\u200d\U0001f467", "Caf\u00e9\u00a0Guest", "A\u3000B"],
)
async def test_names_with_emoji_joiners_and_special_spaces_are_kept(name):
    api, _, controller = make_api()
    controller.rows[1]["ssid"] = name
    state = await read(api)
    assert [item.name for item in state.wireless_services][1] == name


@pytest.mark.parametrize("change", UNUSABLE, ids=lambda change: change.__name__)
async def test_an_unusable_table_leaves_the_services_unknown_and_the_other_sources_intact(change):
    api, _, controller = make_api()
    change(controller)
    state = await read(api)
    assert state.wireless_services is None
    assert state.values == {"configuration_not_saved": True}
    assert state.ports and state.interfaces


@pytest.mark.parametrize(
    ("response", "error"),
    [
        (Response(error=aiohttp.ClientConnectionError("private-url")), Ac100ConnectionError),
        (Response({"error_code": 0}, status=500), Ac100ConnectionError),
        (Response({"error_code": -57704}, status=401), Ac100SessionBusyError),
        (Response({"error_code": -40404}, status=401), Ac100AccountLockedError),
    ],
)
async def test_connection_and_authentication_failures_propagate(response, error):
    api, _, controller = make_api()
    controller.script["ssid"] = [response]
    with pytest.raises(error) as exc:
        await read(api)
    assert "private-url" not in str(exc.value)
    assert controller.bodies[-1] == SSID_READ


async def test_a_table_this_account_may_not_read_only_hides_the_services():
    api, _, controller = make_api()
    controller.script["ssid"] = [Response({"error_code": -40100}, status=401)]
    state = await read(api)
    assert state.wireless_services is None
    assert state.values == {"configuration_not_saved": True}
    assert state.ports and state.interfaces


async def test_an_expired_session_is_renewed_once_for_the_table_read():
    api, _, controller = make_api()
    controller.script["ssid"] = [Response(EXPIRED, status=401)]
    state = await read(api)
    assert [item.name for item in state.wireless_services] == list(NAMES)
    assert [body["method"] for body in controller.bodies].count("do") == 2
    reads = [index for index, body in enumerate(controller.bodies) if body == SSID_READ]
    assert len(reads) == 2
    assert controller.urls[reads[1]] == "http://controller.example/stok=renewed-session/ds"


async def test_a_session_that_stays_expired_propagates():
    api, _, controller = make_api()
    controller.script["ssid"] = [Response(EXPIRED, status=401)] * 2
    with pytest.raises(Ac100SessionExpiredError):
        await read(api)
    assert [body for body in controller.bodies if body == SSID_READ] == [SSID_READ] * 2


async def test_a_failed_sign_in_is_not_mistaken_for_an_unreadable_table():
    api, _, controller = make_api()
    controller.script["ssid"] = [Response(EXPIRED, status=401)]
    controller.script["login"] = [Response(text="not json")]
    with pytest.raises(Ac100ProtocolError):
        await read(api)


async def test_a_retired_client_neither_reads_nor_writes():
    api, session, controller = make_api()
    await api.async_logout()
    controller.bodies.clear()
    with pytest.raises(Ac100Error, match="client has been closed"):
        await read(api)
    with pytest.raises(Ac100Error, match="client has been closed"):
        await api.async_set_wireless_service("1", "enabled", False, name="Home")
    assert controller.bodies == []
    session.close.assert_not_called()


# Secrets


class RowSpy(dict):
    """A row that records the fields read from it and cannot be copied or iterated."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.read = set()

    def get(self, key, default=None):
        self.read.add(key)
        return super().get(key, default)

    def __getitem__(self, key):
        self.read.add(key)
        return super().__getitem__(key)

    def __iter__(self):
        raise AssertionError("a row must not be iterated")

    def items(self):
        raise AssertionError("a row must not be copied")

    values = keys = items


def test_only_the_allow_listed_row_fields_are_read():
    row = RowSpy(service_row(1, "Home"))
    (service,) = Ac100Api._parse_wireless_services([row])
    assert row.read == ALLOWED_FIELDS
    assert not ALLOWED_FIELDS & set(SECRETS)
    assert {item.name for item in fields(WirelessService)} == {
        "service_id",
        "name",
        "enabled",
        "broadcast",
        "isolation",
        "security",
        "igmp_snooping",
        "igmp_querier",
        "vlan",
    }
    assert service.name == "Home"


async def test_secrets_never_leave_the_api(caplog):
    caplog.set_level(logging.DEBUG, logger="custom_components.tplink_ac100.api")
    api, _, controller = make_api()
    state = await read(api)
    await api.async_set_wireless_service("2", "isolation", True, name="Guest")
    with pytest.raises(Ac100Error) as mismatch:
        await api.async_set_wireless_service("2", "isolation", True, name="Renamed")
    repeated_id(controller)
    assert (await read(api)).wireless_services is None
    snapshot = Snapshot(
        Controller("02:00:00:00:00:01", "TL-AC100", "5.0", "1.0.5", {}, 1.0, 1),
        (),
        (),
        monitoring=state,
    )
    entry = SimpleNamespace(
        runtime_data=SimpleNamespace(
            data=snapshot, last_update_success=True, update_interval=UPDATE_INTERVAL
        ),
        options={"monitor_wireless_services": True},
    )
    diagnostics = json.dumps(await async_get_config_entry_diagnostics(None, entry))
    retained = {key: value for key, value in vars(api).items() if key != "_session"}
    text = " ".join(
        [
            repr(state),
            str(asdict(state)),
            json.dumps(asdict(state), default=str),
            diagnostics,
            caplog.text,
            str(mismatch.value),
            repr(retained),
        ]
    )
    assert not [canary for canary in CANARIES if canary in text]
    assert not [name for name in NAMES if name in diagnostics + caplog.text + str(mismatch.value)]
    assert "stok" not in caplog.text and "controller.example" not in caplog.text


# Writing


@pytest.mark.parametrize("value", [True, False])
@pytest.mark.parametrize(("key", "field", "on", "off"), SWITCHES)
async def test_a_write_is_one_sparse_update_holding_only_that_field(key, field, on, off, value):
    start = off if value else on
    rows = [service_row(1, "Home", **{field: start}), service_row(2, "Guest", **{field: start})]
    api, _, controller = make_api(rows)
    await api.async_set_wireless_service("2", key, value, name="Guest")
    assert [body["method"] for body in controller.bodies] == ["get", "set"]
    assert controller.bodies[0] == SSID_READ
    assert controller.bodies[1] == {
        "method": "set",
        TABLE: {
            "table": "wlan_serv",
            "filter": [{"serv_id": "2"}],
            "para": {field: on if value else off},
        },
    }
    assert set(controller.urls) == {"http://controller.example/stok=synthetic-session/ds"}


async def test_a_write_changes_that_field_of_that_network_and_nothing_else():
    api, _, controller = make_api()
    before = json.loads(json.dumps(controller.rows))
    await api.async_set_wireless_service("2", "isolation", True, name="Guest")
    changed = [
        (old["serv_id"], field)
        for old, new in zip(before, controller.rows, strict=True)
        for field in new
        if new[field] != old[field]
    ]
    assert changed == [("2", "isolate")]
    assert controller.rows[1]["isolate"] == "1"
    assert all(row[field] == SECRETS[field] for row in controller.rows for field in SECRETS)


@pytest.mark.parametrize("value", [True, False])
@pytest.mark.parametrize(("key", "field", "on", "off"), SWITCHES)
async def test_a_setting_that_already_has_the_value_is_not_written(key, field, on, off, value):
    api, _, controller = make_api([service_row(2, "Guest", **{field: on if value else off})])
    await api.async_set_wireless_service("2", key, value, name="Guest")
    assert [body["method"] for body in controller.bodies] == ["get"]


async def test_a_setting_in_an_unknown_form_is_written():
    api, _, controller = make_api([service_row(2, "Guest", enable="maybe")])
    await api.async_set_wireless_service("2", "enabled", True, name="Guest")
    assert writes(controller)[0][TABLE]["para"] == {"enable": "on"}


@pytest.mark.parametrize(
    ("service_id", "name"),
    [("99", "Guest"), ("2", "Renamed"), ("2", "guest"), ("2", "Guest "), ("1", "Guest")],
)
async def test_a_network_that_changed_or_vanished_is_not_written(service_id, name):
    api, _, controller = make_api()
    with pytest.raises(Ac100Error, match="changed or no longer exists") as exc:
        await api.async_set_wireless_service(service_id, "enabled", False, name=name)
    assert not [text for text in (*NAMES, *CANARIES) if text in str(exc.value)]
    assert writes(controller) == []


@pytest.mark.parametrize("change", UNUSABLE, ids=lambda change: change.__name__)
async def test_an_unusable_table_blocks_the_write(change):
    api, _, controller = make_api()
    change(controller)
    with pytest.raises(Ac100ProtocolError):
        await api.async_set_wireless_service("1", "enabled", False, name="Home")
    assert writes(controller) == []


async def test_an_unreachable_controller_blocks_the_write():
    api, _, controller = make_api()
    controller.script["ssid"] = [Response(error=aiohttp.ClientConnectionError("down"))]
    with pytest.raises(Ac100ConnectionError):
        await api.async_set_wireless_service("1", "enabled", False, name="Home")
    assert writes(controller) == []


@pytest.mark.parametrize(
    ("response", "error"),
    [
        (Response(error=aiohttp.ClientConnectionError("down")), Ac100ConnectionError),
        (Response({"error_code": -40210}), Ac100ProtocolError),
        (Response({"error_code": -57704}, status=401), Ac100SessionBusyError),
    ],
)
async def test_a_failed_write_is_sent_once_and_never_repeated(response, error):
    api, _, controller = make_api()
    controller.script["set"] = [response]
    with pytest.raises(error):
        await api.async_set_wireless_service("1", "enabled", False, name="Home")
    assert len(writes(controller)) == 1
    assert [body["method"] for body in controller.bodies] == ["get", "set"]


async def test_an_expired_session_is_never_replayed_for_a_write():
    api, _, controller = make_api()
    controller.script["set"] = [Response(EXPIRED, status=401)]
    with pytest.raises(Ac100SessionExpiredError):
        await api.async_set_wireless_service("2", "enabled", False, name="Guest")
    assert [body["method"] for body in controller.bodies] == ["get", "set"]
    assert api._stok is None
    await api.async_set_wireless_service("2", "enabled", False, name="Guest")
    assert [body["method"] for body in controller.bodies] == [
        "get",
        "set",
        "do",
        "do",
        "get",
        "set",
    ]
    assert controller.urls[-1] == "http://controller.example/stok=renewed-session/ds"


async def test_an_expired_session_is_renewed_for_the_fresh_read_and_the_write_follows():
    api, _, controller = make_api()
    controller.script["ssid"] = [Response(EXPIRED, status=401)]
    await api.async_set_wireless_service("2", "enabled", False, name="Guest")
    assert [body["method"] for body in controller.bodies] == ["get", "do", "do", "get", "set"]
    assert controller.urls[-1] == "http://controller.example/stok=renewed-session/ds"


async def test_a_write_holds_the_shared_lock_across_its_read_and_its_update():
    api, _, controller = make_api()
    controller.gate_ssid = True
    write = asyncio.create_task(api.async_set_wireless_service("2", "enabled", False, name="Guest"))
    await controller.gate_ready.wait()
    await controller.gate.started.wait()
    monitoring = asyncio.create_task(api.async_get_monitoring())
    for _ in range(10):
        await asyncio.sleep(0)
    assert len(controller.bodies) == 1
    controller.gate.release.set()
    await asyncio.gather(write, monitoring)
    assert [body["method"] for body in controller.bodies[:2]] == ["get", "set"]
    assert TABLE in controller.bodies[0] and TABLE in controller.bodies[1]
    assert "system" in controller.bodies[2]


async def test_only_the_catalogued_settings_can_be_written():
    api, _, controller = make_api()
    for field in ("ssid", "key", "encryption", "default_bind_vlan", "enable", "security", ""):
        with pytest.raises(KeyError):
            await api.async_set_wireless_service("1", field, True, name="Home")
    assert controller.bodies == []


async def test_reads_and_writes_keep_no_state_on_the_client():
    api, _, _ = make_api()
    before = {key: value for key, value in vars(api).items() if key != "_session"}
    await read(api)
    await api.async_set_wireless_service("2", "enabled", False, name="Guest")
    assert {key: value for key, value in vars(api).items() if key != "_session"} == before


async def test_debug_logs_hold_fixed_text_and_counts_only(caplog):
    caplog.set_level(logging.DEBUG, logger="custom_components.tplink_ac100.api")
    api, _, controller = make_api()
    await read(api)
    await api.async_set_wireless_service("2", "enabled", False, name="Guest")
    await api.async_set_wireless_service("2", "enabled", True, name="Guest")
    blank_name(controller)
    await read(api)
    records = [r for r in caplog.records if r.name == "custom_components.tplink_ac100.api"]
    wireless = [r for r in records if "ireless" in r.getMessage()]
    assert any(not r.args for r in wireless), "a failed read is logged as fixed text"
    assert any(r.args for r in wireless), "a successful read is logged as a count"
    assert all(isinstance(a, int | bool) or a == "enabled" for r in wireless for a in r.args)
    assert not [name for name in (*NAMES, *CANARIES) if name in caplog.text]


@pytest.mark.usefixtures("enable_custom_integrations", "entity_registry_enabled_by_default")
async def test_a_switch_reaches_the_controller_as_one_update_and_shows_the_result(
    hass, entity_registry
):
    """The real client, coordinator and entities together; only the HTTP session is a double."""
    mac = "02:00:00:00:00:01"
    api, _, controller = make_api()
    snapshot = Snapshot(Controller(mac, "TL-AC100", "5.0", "1.0.5", {}, 1.0, 1), (), ())
    api.async_get_snapshot = AsyncMock(return_value=snapshot)
    api.async_get_configuration = AsyncMock(return_value=ConfigurationState())
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=mac,
        data={"host": "http://controller.example", "username": "admin", "password": "example"},
        options={CONF_ENABLE_CONTROL: True, CONF_MONITOR_WIRELESS_SERVICES: True},
    )
    entry.add_to_hass(hass)
    platforms = [Platform.BINARY_SENSOR, Platform.SWITCH]
    with (
        patch("custom_components.tplink_ac100.Ac100Api", return_value=api),
        patch("custom_components.tplink_ac100.PLATFORMS", platforms),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        switch = entity_registry.async_get_entity_id("switch", DOMAIN, f"{mac}_wlan_2_isolation")
        hidden = entity_registry.async_get_entity_id(
            "binary_sensor", DOMAIN, f"{mac}_wlan_2_hidden"
        )
        assert hass.states.get(switch).state == "off"
        assert hass.states.get(hidden).state == "off"
        controller.bodies.clear()
        await hass.services.async_call("switch", "turn_on", {"entity_id": switch}, blocking=True)
        await hass.async_block_till_done()
        assert writes(controller) == [
            {
                "method": "set",
                TABLE: {
                    "table": "wlan_serv",
                    "filter": [{"serv_id": "2"}],
                    "para": {"isolate": "1"},
                },
            }
        ]
        assert [body for body in controller.bodies if body == SSID_READ] == [SSID_READ] * 2
        assert hass.states.get(switch).state == "on"
        assert await hass.config_entries.async_unload(entry.entry_id)


# Schema


def test_the_catalogs_follow_the_controller_page():
    assert [
        (item.key, item.field, item.on, item.off) for item in WIRELESS_SERVICE_FLAGS
    ] == SWITCHES
    assert WIRELESS_SERVICE_SECURITY == {
        "0": "open",
        "1": "wpa_psk_wpa2_psk",
        "2": "wpa_wpa2_enterprise",
        "4": "wpa2_psk_wpa3_sae",
    }
    assert all(
        re.fullmatch(r"[a-z0-9]+(_[a-z0-9]+)*", s) for s in WIRELESS_SERVICE_SECURITY.values()
    )
    assert {item.key for item in WIRELESS_SERVICE_FLAGS} <= {
        item.name for item in fields(WirelessService)
    }
    forbidden = ("key", "pwd", "pass", "psk", "radius", "secret", "token")
    assert not [
        i.field for i in WIRELESS_SERVICE_FLAGS if any(word in i.field for word in forbidden)
    ]
    assert all(item.states == {item.on: True, item.off: False} for item in WIRELESS_SERVICE_FLAGS)


def test_the_state_distinguishes_not_read_from_no_networks():
    assert MonitoringState().wireless_services is None
    assert MonitoringState(wireless_services=()) != MonitoringState()
    assert MonitoringState(wireless_services=()).wireless_services == ()
