"""Controller-wide writes, radio switching and saving, against a fake SLP controller."""

import asyncio
import json
import logging
from collections import Counter
from types import SimpleNamespace
from unittest.mock import MagicMock
from urllib.parse import quote, unquote

import aiohttp
import pytest

from custom_components.tplink_ac100 import api as api_module
from custom_components.tplink_ac100.api import (
    Ac100Api,
    Ac100ConnectionError,
    Ac100Error,
    Ac100ProtocolError,
    Ac100SaveTimeoutError,
    Ac100SessionBusyError,
    Ac100SessionExpiredError,
)
from custom_components.tplink_ac100.management import (
    ALIAS_FIELD,
    FORMS,
    FormField,
    WriteForm,
    encode_setting,
    form_request,
    select_options,
    writable_fields,
)
from custom_components.tplink_ac100.monitoring import CONFIGURATION_FIELDS, ConfigField

FIELDS = {item.key: item for item in CONFIGURATION_FIELDS}
EXPIRED = {"error_code": -40401, "data": {"code": -40401}}

# The forms that the live write test sent and restored without changing any other field
# on the hardware: every field of the page's form, and nothing else.
LIVE_FORMS = {
    ("apmng_band_navigation", "settings"): (
        "enable",
        "5g_threshold",
        "dif_threshold",
        "max_fail_num",
    ),
    ("apmng_balance", "settings"): (
        "enable",
        "group_id_str",
        "mode",
        "usr_threshold",
        "dif_threshold",
        "max_fail_num",
        "rssi_threshold",
    ),
    ("apmng_bcast_storm_ctrl", "settings"): ("enable", "storm_rate", "type"),
    ("apmng_wserv", "igmp_settings"): (
        "igmp_snooping_enable",
        "igmp_drop_unknown",
        "igmp_2g_rate",
        "igmp_5g_rate",
    ),
}


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


def radio_row(rf_id, unit, **extra):
    return {
        "rf_id": str(rf_id),
        "freq_unit": str(unit),
        "ap_type": "ap",
        "rf_state": "on",
        "mode": "6",
        "ap_name": "Example AP",
        **extra,
    }


class FakeController:
    """An SLP double whose sections and radio table change when a ``set`` request arrives.

    Reads are answered from plain dictionaries and writes update them like the controller does:
    a section update touches the fields it names and a row update the fields of its ``para``.
    """

    def __init__(self):
        self.sections = {
            ("apmng_band_navigation", "settings"): {
                ".name": "settings",
                ".type": "global",
                "enable": "on",
                "5g_threshold": "20",
                "dif_threshold": "4",
                "max_fail_num": "10",
            },
            ("apmng_balance", "settings"): {
                ".name": "settings",
                ".type": "global",
                "enable": "off",
                "group_id_str": "-1,3",
                "group_id": "display only",
                "mode": "session",
                "usr_threshold": "20",
                "dif_threshold": "4",
                "max_fail_num": "3",
                "rssi_threshold": "-75",
            },
            ("apmng_bcast_storm_ctrl", "settings"): {
                ".name": "settings",
                "enable": "off",
                "storm_rate": "1024",
                "type": "pre_define",
            },
            ("apmng_wserv", "igmp_settings"): {
                "igmp_snooping_enable": "on",
                "igmp_drop_unknown": "0",
                "igmp_2g_rate": "1",
                "igmp_5g_rate": "6",
                "igmp_query_interval": "60",
                "igmp_max_resp_time": "10",
            },
            ("system", "sys"): {".name": "sys", "dev_alias": "TL-AC100 5A1", "timezone": "GMT"},
        }
        self.radios = [radio_row(entry * 16 + unit, unit) for entry in (0, 1) for unit in (1, 2)]
        self.save_replies = ["0", "1"]
        self.saves = 0
        self.inject = {}
        self.hold = False
        self.gate = None
        self.gate_created = asyncio.Event()
        self.urls = []
        self.bodies = []

    def __call__(self, url, **kwargs):
        body = kwargs["json"]
        self.urls.append(url)
        self.bodies.append(body)
        if (index := len(self.bodies) - 1) in self.inject:
            return self.inject[index]
        if url.endswith("/"):
            if "user_management" in body:
                return Response({"error_code": 0, "encrypt_type": ["-3"]})
            return Response({"error_code": 0, "stok": "renewed-session"})
        response = self.answer(body)
        if self.hold and self.gate is None:
            self.gate = GatedResponse(response)
            self.gate_created.set()
            return self.gate
        return response if isinstance(response, Response) else Response(response)

    def answer(self, body):
        modules = [key for key in body if key != "method"]
        (module,) = modules
        query = body[module]
        if body["method"] == "get":
            return self.read(module, query)
        if body["method"] == "do":
            self.saves += "save_cfg" in query
            return {"error_code": 0, "status": "Saved"}
        if "table" in query:
            targets = {item["rf_id"] for item in query["filter"]}
            for row in self.radios:
                if row["rf_id"] in targets:
                    row.update(unquote_all(query["para"]))
        else:
            ((section, form),) = query.items()
            self.sections[module, section].update(unquote_all(form))
        return {"error_code": 0}

    def read(self, module, query):
        if "name" in query:
            if (module, query["name"]) == ("system", "save_cfg_status"):
                reply = (
                    self.save_replies.pop(0) if len(self.save_replies) > 1 else self.save_replies[0]
                )
                if isinstance(reply, Response):
                    return reply
                progress = "100" if reply == "1" else "0"
                return {
                    "error_code": 0,
                    "system": {"save_cfg_status": {"status": reply, "progress": progress}},
                }
            found = self.sections.get((module, query["name"]))
            return {"error_code": 0, module: {query["name"]: wire(found)} if found else {}}
        start, end = query["para"]["start"], query["para"]["end"]
        rows = self.radios
        return {
            "error_code": 0,
            module: {
                query["table"]: [
                    {f"{query['table']}_{start + i}": wire(row)}
                    for i, row in enumerate(rows[start : end + 1])
                ],
                "count": {query["table"]: len(rows)},
            },
        }

    def writes(self):
        return [body for body in self.bodies if body["method"] != "get"]


def unquote_all(value):
    if isinstance(value, dict):
        return {key: unquote_all(item) for key, item in value.items()}
    return unquote(value) if isinstance(value, str) else value


def make_api(controller=None):
    controller = controller or FakeController()
    session = MagicMock(spec=aiohttp.ClientSession)
    session.post.side_effect = controller
    api = Ac100Api(session, "controller.example", "admin", "example-password")
    api._stok = "synthetic-session"
    return api, session, controller


@pytest.fixture
def clock(monkeypatch):
    """Replace the API's sleep and clock: waiting advances a fake time and costs nothing."""
    state = SimpleNamespace(now=1000.0, sleeps=[], on_sleep=None)

    async def sleep(seconds):
        state.sleeps.append(seconds)
        if state.on_sleep is not None:
            await state.on_sleep(len(state.sleeps))
        state.now += seconds
        await asyncio.sleep(0)

    monkeypatch.setattr(api_module, "sleep", sleep)
    monkeypatch.setattr(api_module, "monotonic", lambda: state.now)
    return state


# (setting, new value, the fields of its page's form that change)
CHANGES = [
    ("band_navigation_enabled", False, {"enable": "off"}),
    ("band_navigation_5g_threshold", 40, {"5g_threshold": "40"}),
    ("band_navigation_difference_threshold", 8, {"dif_threshold": "8"}),
    ("band_navigation_max_failures", 0, {"max_fail_num": "0"}),
    ("load_balance_enabled", True, {"enable": "on"}),
    ("load_balance_threshold", 2, {"usr_threshold": "2"}),
    ("load_balance_difference_threshold", 1, {"dif_threshold": "1"}),
    ("load_balance_max_failures", 100, {"max_fail_num": "100"}),
    ("load_balance_rssi_threshold", -95, {"rssi_threshold": "-95"}),
    ("load_balance_rssi_threshold", 0, {"rssi_threshold": "0"}),
    ("broadcast_storm_control_enabled", True, {"enable": "on"}),
    # A rate is always entered as a user-defined one, also when it equals a preset step.
    ("broadcast_storm_rate", 300, {"storm_rate": "300", "type": "user_define"}),
    ("broadcast_storm_rate", 1, {"storm_rate": "1", "type": "user_define"}),
    ("broadcast_storm_rate", 64, {"storm_rate": "64", "type": "user_define"}),
    ("broadcast_storm_rate", 1024, {"storm_rate": "1024", "type": "user_define"}),
    ("igmp_snooping_enabled", False, {"igmp_snooping_enable": "off"}),
    ("igmp_unknown_multicast", "forward", {"igmp_drop_unknown": "1"}),
    ("igmp_2g_multicast_rate", 11, {"igmp_2g_rate": "11"}),
    ("igmp_5g_multicast_rate", 24, {"igmp_5g_rate": "24"}),
]


@pytest.mark.parametrize(("key", "value", "changes"), CHANGES)
async def test_a_setting_is_written_as_its_pages_complete_form_in_one_request(key, value, changes):
    api, session, controller = make_api()
    item = FIELDS[key]
    section = controller.sections[item.module, item.section]
    before = {name: field for name, field in section.items()}
    await api.async_set_setting(item, value)
    form_fields = LIVE_FORMS[item.module, item.section]
    expected = {name: before[name] for name in form_fields} | changes
    assert controller.bodies == [
        {"method": "get", item.module: {"name": item.section}},
        wire({"method": "set", item.module: {item.section: expected}}),
    ]
    assert set(controller.bodies[1][item.module][item.section]) == set(form_fields)
    assert section == before | changes
    assert set(controller.urls) == {"http://controller.example/stok=synthetic-session/ds"}
    session.close.assert_not_called()


async def test_the_form_is_built_from_a_fresh_read_not_from_an_earlier_one():
    api, _, controller = make_api()
    item = FIELDS["band_navigation_5g_threshold"]
    section = controller.sections[item.module, item.section]
    await api.async_set_setting(item, 30)
    section.update(enable="off", dif_threshold="7", max_fail_num="55")  # edited elsewhere
    await api.async_set_setting(item, 31)
    first, second = (body[item.module][item.section] for body in controller.writes())
    assert first == {
        "enable": "on",
        "5g_threshold": "30",
        "dif_threshold": "4",
        "max_fail_num": "10",
    }
    assert second == {
        "enable": "off",
        "5g_threshold": "31",
        "dif_threshold": "7",
        "max_fail_num": "55",
    }
    assert [body["method"] for body in controller.bodies] == ["get", "set", "get", "set"]


async def test_fields_outside_the_form_are_never_sent_and_stay_as_they_are():
    api, _, controller = make_api()
    igmp = controller.sections["apmng_wserv", "igmp_settings"]
    balance = controller.sections["apmng_balance", "settings"]
    await api.async_set_setting(FIELDS["igmp_snooping_enabled"], False)
    await api.async_set_setting(FIELDS["load_balance_threshold"], 25)
    igmp_form = controller.writes()[0]["apmng_wserv"]["igmp_settings"]
    balance_form = controller.writes()[1]["apmng_balance"]["settings"]
    assert "igmp_query_interval" not in igmp_form and "igmp_max_resp_time" not in igmp_form
    assert not {".name", ".type", "group_id"} & set(balance_form)
    assert unquote(balance_form["group_id_str"]) == "-1,3"
    assert (igmp["igmp_query_interval"], igmp["igmp_max_resp_time"]) == ("60", "10")
    assert balance["group_id"] == "display only"


@pytest.mark.parametrize(
    ("key", "value"),
    [
        *(("band_navigation_5g_threshold", value) for value in (1, 41, -3, 20.5, True, "20", None)),
        *(("band_navigation_difference_threshold", value) for value in (0, 9, 2.0001)),
        ("band_navigation_max_failures", -1),
        ("band_navigation_max_failures", 101),
        *(("load_balance_threshold", value) for value in (1, 41)),
        *(("load_balance_difference_threshold", value) for value in (0, 9)),
        *(("load_balance_max_failures", value) for value in (0, 101)),
        *(("load_balance_rssi_threshold", value) for value in (-96, 1)),
        *(("broadcast_storm_rate", value) for value in (0, 1025, float("nan"), float("inf"))),
        *(("igmp_2g_multicast_rate", value) for value in (2, 6, 0, "11")),
        *(("igmp_5g_multicast_rate", value) for value in (11, 1, 25)),
        *(("igmp_unknown_multicast", value) for value in ("maybe", "1", 1, True, None, "Drop")),
        *(
            (key, value)
            for key in (
                "band_navigation_enabled",
                "load_balance_enabled",
                "broadcast_storm_control_enabled",
                "igmp_snooping_enabled",
            )
            for value in ("on", "off", 1, 0, None)
        ),
        *(
            (key, value)
            for key, value in (
                ("load_balance_mode", "session"),
                ("broadcast_storm_rate_type", "user_defined"),
                ("broadcast_storm_rate_type", "predefined"),
                ("rrm_band", "both"),
                ("roaming_11k", True),
                ("device_alias", "Name"),
                ("ntp_primary_server", "192.0.2.1"),
                ("time_zone_offset", 60),
                ("web_session_timeout", 10),
                ("ap_telnet_enabled", True),
                ("security_audit_enabled", True),
            )
        ),
    ],
)
async def test_a_value_the_page_would_refuse_is_refused_before_anything_is_sent(key, value):
    api, _, controller = make_api()
    with pytest.raises(ValueError):
        await api.async_set_setting(FIELDS[key], value)
    assert controller.bodies == []


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("band_navigation_5g_threshold", 20.0),
        ("load_balance_rssi_threshold", -75.0),
        ("broadcast_storm_rate", 100.0),
    ],
)
async def test_a_whole_number_may_arrive_as_a_float(key, value):
    api, _, controller = make_api()
    await api.async_set_setting(FIELDS[key], value)
    assert len(controller.writes()) == 1


def test_the_offset_of_a_number_is_undone_and_its_range_is_in_the_catalogs_terms(monkeypatch):
    zone = ConfigField("zone", "example", "ntp", "timezone", "number", "min", offset=-720)
    form = WriteForm("example", "ntp", (FormField("timezone", "integer", 0, 1440),))
    monkeypatch.setattr(
        "custom_components.tplink_ac100.management._FORMS", {("example", "ntp"): form}
    )
    assert encode_setting(zone, 480) == "1200"
    assert encode_setting(zone, -720) == "0"
    assert encode_setting(zone, 720) == "1440"
    with pytest.raises(ValueError, match="from -720 to 720"):
        encode_setting(zone, 721)
    request = form_request(zone, "1200", {"timezone": "720", "other": "x"})
    assert request == {"method": "set", "example": {"ntp": {"timezone": "1200"}}}


@pytest.mark.parametrize(
    ("key", "broken", "value"),
    [
        ("band_navigation_5g_threshold", {"enable": "1"}, 20),
        ("band_navigation_5g_threshold", {"max_fail_num": "101"}, 20),
        ("band_navigation_5g_threshold", {"dif_threshold": "05"}, 20),
        ("band_navigation_5g_threshold", {"dif_threshold": "four"}, 20),
        ("band_navigation_5g_threshold", {"dif_threshold": None}, 20),
        ("band_navigation_5g_threshold", {"dif_threshold": 4}, 20),
        ("load_balance_enabled", {"group_id_str": ""}, True),
        ("load_balance_enabled", {"group_id_str": None}, True),
        ("load_balance_enabled", {"mode": "round_robin"}, True),
        ("load_balance_enabled", {"rssi_threshold": "-96"}, True),
        ("broadcast_storm_control_enabled", {"type": "custom"}, True),
        ("broadcast_storm_control_enabled", {"storm_rate": "2048"}, True),
        ("igmp_snooping_enabled", {"igmp_2g_rate": "5"}, True),
        ("igmp_snooping_enabled", {"igmp_drop_unknown": "2"}, True),
        ("igmp_2g_multicast_rate", {"igmp_5g_rate": "7"}, 11),
    ],
)
async def test_a_form_that_cannot_be_sent_back_unchanged_is_not_written(key, broken, value):
    api, _, controller = make_api()
    item = FIELDS[key]
    controller.sections[item.module, item.section].update(broken)
    original = dict(controller.sections[item.module, item.section])
    with pytest.raises(Ac100ProtocolError, match="cannot be sent back unchanged"):
        await api.async_set_setting(item, value)
    assert [body["method"] for body in controller.bodies] == ["get"]
    assert controller.sections[item.module, item.section] == original


async def test_a_field_that_is_missing_from_the_fresh_section_blocks_the_write():
    api, _, controller = make_api()
    del controller.sections["apmng_balance", "settings"]["group_id_str"]
    with pytest.raises(Ac100ProtocolError, match="group_id_str"):
        await api.async_set_setting(FIELDS["load_balance_enabled"], True)
    assert controller.writes() == []


@pytest.mark.parametrize(
    ("key", "broken", "value", "changes"),
    [
        ("band_navigation_5g_threshold", {"5g_threshold": "99"}, 20, {"5g_threshold": "20"}),
        ("band_navigation_enabled", {"enable": "maybe"}, True, {"enable": "on"}),
        (
            "broadcast_storm_rate",
            {"type": "bogus", "storm_rate": "7000"},
            100,
            {"type": "user_define"},
        ),
        ("igmp_2g_multicast_rate", {"igmp_2g_rate": "5"}, 11, {"igmp_2g_rate": "11"}),
    ],
)
async def test_a_bad_current_value_of_the_field_being_written_does_not_block_its_repair(
    key, broken, value, changes
):
    api, _, controller = make_api()
    item = FIELDS[key]
    controller.sections[item.module, item.section].update(broken)
    await api.async_set_setting(item, value)
    form = controller.writes()[0][item.module][item.section]
    assert changes.items() <= form.items()


@pytest.mark.parametrize("missing", ["section", "module"])
async def test_a_section_the_controller_does_not_return_is_never_written(missing):
    api, _, controller = make_api()
    item = FIELDS["igmp_snooping_enabled"]
    controller.sections.pop((item.module, item.section))
    if missing == "module":
        controller.sections.clear()
    with pytest.raises(Ac100ProtocolError, match="Missing"):
        await api.async_set_setting(item, True)
    assert len(controller.bodies) == 1


@pytest.mark.parametrize(
    ("failure", "error"),
    [
        (Response(EXPIRED, status=401), Ac100SessionExpiredError),
        (Response(error=aiohttp.ClientConnectionError("private-detail")), Ac100ConnectionError),
        (Response({"error_code": -40210}), Ac100ProtocolError),
        (
            Response({"error_code": -40100, "data": {"code": -57704}}, status=401),
            Ac100SessionBusyError,
        ),
    ],
)
@pytest.mark.parametrize("write", ["setting", "alias", "radio"])
async def test_a_failed_write_is_never_sent_again(write, failure, error):
    api, _, controller = make_api()
    last = {"setting": 1, "alias": 0, "radio": 1}[write]
    controller.inject = {last: failure}
    attempts = {
        "setting": lambda: api.async_set_setting(FIELDS["band_navigation_enabled"], False),
        "alias": lambda: api.async_set_controller_alias("Renamed"),
        "radio": lambda: api.async_set_radio_enabled("0", "1", False),
    }
    with pytest.raises(error) as exc:
        await attempts[write]()
    assert "private-detail" not in str(exc.value)
    assert len(controller.bodies) == last + 1
    assert [body["method"] for body in controller.bodies].count("set") == 1
    assert controller.writes() == [controller.bodies[-1]]
    assert all("user_management" not in body for body in controller.bodies)


async def test_an_expired_session_is_renewed_for_the_read_but_the_write_follows_only_once():
    api, _, controller = make_api()
    controller.inject = {0: Response(EXPIRED, status=401)}
    await api.async_set_setting(FIELDS["band_navigation_enabled"], False)
    assert [body["method"] for body in controller.bodies] == ["get", "do", "do", "get", "set"]
    assert controller.urls[-1] == "http://controller.example/stok=renewed-session/ds"
    assert [body for body in controller.bodies if body["method"] == "set"] == [
        controller.bodies[-1]
    ]


async def test_a_write_holds_the_lock_from_its_read_to_its_set():
    api, _, controller = make_api()
    controller.hold = True
    first = asyncio.create_task(api.async_set_setting(FIELDS["band_navigation_enabled"], False))
    await controller.gate_created.wait()
    await controller.gate.started.wait()
    second = asyncio.create_task(api.async_set_controller_alias("Other"))
    third = asyncio.create_task(api.async_set_radio_enabled("0", "1", False))
    for _ in range(5):
        await asyncio.sleep(0)
    assert len(controller.bodies) == 1
    controller.gate.release.set()
    await asyncio.gather(first, second, third)
    methods = [
        (body["method"], next(key for key in body if key != "method")) for body in controller.bodies
    ]
    assert methods[:2] == [("get", "apmng_band_navigation"), ("set", "apmng_band_navigation")]
    assert Counter(methods[2:]) == {
        ("set", "system"): 1,
        ("get", "apmng_rf"): 1,
        ("set", "apmng_rf"): 1,
    }


@pytest.mark.parametrize(
    "name",
    ["A", "TL-AC100 5A1", "x" * 20, "Büro", "名称", "a b", "a\u3000b", "\U0001f600" * 10, " a "],
)
async def test_the_controller_name_is_the_system_pages_sparse_update(name):
    api, _, controller = make_api()
    await api.async_set_controller_alias(name)
    assert len(controller.bodies) == 1
    body = controller.bodies[0]
    assert body.keys() == {"method", "system"}
    assert body["method"] == "set"
    assert body["system"].keys() == {"sys"}
    assert body["system"]["sys"].keys() == {"dev_alias"}
    assert body["system"]["sys"]["dev_alias"] == quote(name, safe="~()*!.'-")
    assert controller.sections["system", "sys"] == {
        ".name": "sys",
        "dev_alias": name,
        "timezone": "GMT",
    }


@pytest.mark.parametrize(
    "name",
    [
        "",
        " ",
        "\u3000",
        "x" * 21,
        "a\nb",
        "a\tb",
        "a\x00b",
        "a\x7fb",
        "a\xa0b",
        "a\u200ab",
        "a\ufeffb",
        "\U0001f600" * 11,
        "x" * 19 + "\U0001f600",
        None,
        5,
        b"name",
    ],
)
async def test_a_controller_name_the_page_would_refuse_is_refused_before_anything_is_sent(name):
    api, _, controller = make_api()
    with pytest.raises(ValueError, match="1 to 20 visible"):
        await api.async_set_controller_alias(name)
    assert controller.bodies == []


async def test_the_radio_update_uses_the_row_address_of_a_fresh_read():
    api, _, controller = make_api()
    controller.radios = [
        radio_row(1, 1),
        radio_row(2, 2),
        radio_row(35, 1),  # entry 2, local radio 3
        radio_row(36, 2),
        radio_row(48, 1, rf_state="off"),
    ]
    await api.async_set_radio_enabled("2", "1", False)
    assert controller.bodies == [
        {
            "method": "get",
            "apmng_rf": {
                "table": "rf_entry",
                "filter": [{"group_id": ["-1"], "ap_status": ["0"]}],
                "para": {"start": 0, "end": 9},
            },
        },
        {
            "method": "set",
            "apmng_rf": {
                "table": "rf_entry",
                "filter": [{"rf_id": "35"}],
                "para": {"rf_state": "off"},
            },
        },
    ]
    assert [row["rf_state"] for row in controller.radios] == ["on", "on", "off", "on", "off"]
    await api.async_set_radio_enabled("3", "1", True)
    assert controller.writes()[-1]["apmng_rf"]["filter"] == [{"rf_id": "48"}]
    assert controller.radios[-1]["rf_state"] == "on"


async def test_a_radio_on_a_later_page_is_found():
    api, _, controller = make_api()
    controller.radios = [radio_row(entry * 16 + 1, 1) for entry in range(23)]
    await api.async_set_radio_enabled("22", "1", False)
    pages = [body["apmng_rf"]["para"] for body in controller.bodies if body["method"] == "get"]
    assert pages == [{"start": 0, "end": 9}, {"start": 10, "end": 19}, {"start": 20, "end": 29}]
    assert controller.writes()[0]["apmng_rf"]["filter"] == [{"rf_id": "353"}]
    assert sum(row["rf_state"] == "off" for row in controller.radios) == 1


async def test_ap_templates_are_no_radios():
    api, _, controller = make_api()
    controller.radios = [radio_row(1, 1, ap_type="template"), radio_row(17, 1)]
    await api.async_set_radio_enabled("1", "1", False)
    assert controller.writes()[0]["apmng_rf"]["filter"] == [{"rf_id": "17"}]
    controller.radios = [radio_row(1, 1, ap_type="template")]
    with pytest.raises(Ac100Error, match="no longer exists"):
        await api.async_set_radio_enabled("0", "1", False)


@pytest.mark.parametrize(
    ("rows", "error", "message"),
    [
        ([], Ac100Error, "no longer exists"),
        ([radio_row(1, 1)], Ac100Error, "no longer exists"),
        ([radio_row(17, 2), radio_row(18, 3)], Ac100Error, "no longer exists"),
        ([radio_row(32, 2)], Ac100Error, "no longer exists"),
        ([radio_row(17, 1), radio_row(18, 1)], Ac100ProtocolError, "Duplicate radio"),
        ([radio_row(17, 1), radio_row("bad", 2)], Ac100ProtocolError, "Invalid radio"),
        ([radio_row(17, 1), radio_row(18, None)], Ac100ProtocolError, "Invalid radio"),
    ],
)
async def test_no_radio_is_switched_unless_exactly_one_row_matches(rows, error, message):
    api, _, controller = make_api()
    controller.radios = rows
    with pytest.raises(error, match=message):
        await api.async_set_radio_enabled("1", "1", False)
    assert controller.writes() == []
    assert [row["rf_state"] for row in controller.radios] == ["on"] * len(rows)


@pytest.mark.parametrize("enabled", ["off", 0, 1, None])
async def test_a_radio_is_switched_on_or_off_only(enabled):
    api, _, controller = make_api()
    with pytest.raises(ValueError):
        await api.async_set_radio_enabled("0", "1", enabled)
    assert controller.bodies == []


async def test_saving_sends_once_and_polls_every_two_seconds_without_holding_the_lock(clock):
    api, _, controller = make_api()
    controller.save_replies = ["0", "0", "1"]
    held = []

    async def during_the_wait(count):
        held.append(api._lock.locked())
        if count == 2:
            await api.async_set_controller_alias("Busy elsewhere")

    clock.on_sleep = during_the_wait
    await api.async_save_configuration()
    poll = {"method": "get", "system": {"name": "save_cfg_status"}}
    assert controller.bodies[0] == {"method": "do", "system": {"save_cfg": None}}
    assert controller.bodies[1:] == [
        poll,
        {"method": "set", "system": {"sys": {"dev_alias": "Busy%20elsewhere"}}},
        poll,
        poll,
    ]
    assert controller.saves == 1
    assert clock.sleeps == [2.0, 2.0, 2.0]
    assert held == [False, False, False]


async def test_saving_that_is_already_finished_at_the_first_poll_ends_there(clock):
    api, _, controller = make_api()
    controller.save_replies = ["1"]
    await api.async_save_configuration()
    assert len(controller.bodies) == 2
    assert clock.sleeps == [2.0]


async def test_saving_that_never_finishes_ends_after_sixty_seconds_with_an_unknown_outcome(clock):
    api, _, controller = make_api()
    controller.save_replies = ["0"]
    start = clock.now
    with pytest.raises(Ac100SaveTimeoutError, match="did not report"):
        await api.async_save_configuration()
    assert clock.now - start == 60.0
    assert clock.sleeps == [2.0] * 30
    assert controller.saves == 1
    methods = Counter(body["method"] for body in controller.bodies)
    assert methods == {"do": 1, "get": 30}
    assert controller.bodies[0] == {"method": "do", "system": {"save_cfg": None}}
    assert not api._lock.locked()


async def test_a_poll_without_an_answer_is_not_the_end_of_the_wait(clock):
    api, _, controller = make_api()
    controller.save_replies = [
        Response(error=aiohttp.ClientConnectionError("private-detail")),
        Response({"error_code": -40210}),
        Response({"error_code": 0, "system": {"other": {}}}),
        Response(text="not json"),
        "0",
        "1",
    ]
    await api.async_save_configuration()
    assert clock.sleeps == [2.0] * 6
    assert controller.saves == 1


async def test_polls_that_never_answer_leave_the_outcome_unknown_and_the_save_unrepeated(clock):
    api, _, controller = make_api()
    controller.save_replies = [Response(error=aiohttp.ClientConnectionError("down"))]
    with pytest.raises(Ac100SaveTimeoutError):
        await api.async_save_configuration()
    assert controller.saves == 1
    assert len(clock.sleeps) == 30


async def test_a_busy_session_while_waiting_ends_the_wait_with_its_own_error(clock):
    api, _, controller = make_api()
    controller.save_replies = [
        "0",
        Response({"error_code": -40100, "data": {"code": -57704}}, status=401),
    ]
    with pytest.raises(Ac100SessionBusyError):
        await api.async_save_configuration()
    assert controller.saves == 1


async def test_an_expired_session_is_renewed_for_a_poll_and_the_save_is_not_repeated(clock):
    api, _, controller = make_api()
    controller.save_replies = ["1"]
    controller.inject = {1: Response(EXPIRED, status=401)}
    await api.async_save_configuration()
    assert [body["method"] for body in controller.bodies] == ["do", "get", "do", "do", "get"]
    assert controller.saves == 1


async def test_a_session_that_expires_again_after_renewal_ends_the_wait(clock):
    api, _, controller = make_api()
    controller.inject = {1: Response(EXPIRED, status=401), 4: Response(EXPIRED, status=401)}
    with pytest.raises(Ac100SessionExpiredError):
        await api.async_save_configuration()
    assert controller.saves == 1
    assert len(clock.sleeps) == 1


@pytest.mark.parametrize(
    "failure",
    [
        Response(EXPIRED, status=401),
        Response(error=aiohttp.ClientConnectionError("private-detail")),
        Response({"error_code": -40210}),
    ],
)
async def test_a_failed_save_request_is_neither_repeated_nor_followed_by_polls(clock, failure):
    api, _, controller = make_api()
    controller.inject = {0: failure}
    with pytest.raises(Ac100Error) as exc:
        await api.async_save_configuration()
    assert not isinstance(exc.value, Ac100SaveTimeoutError)
    assert "private-detail" not in str(exc.value)
    assert controller.bodies == [{"method": "do", "system": {"save_cfg": None}}]
    assert clock.sleeps == []


async def test_closing_the_client_while_a_save_is_pending_stops_the_wait_without_a_new_save(clock):
    api, _, controller = make_api()
    controller.save_replies = ["0"]

    async def close(count):
        await api.async_logout()

    clock.on_sleep = close
    with pytest.raises(Ac100Error, match="client has been closed"):
        await api.async_save_configuration()
    assert controller.saves == 1
    assert [body["method"] for body in controller.bodies] == ["do", "do"]
    assert controller.bodies[-1] == {"method": "do", "system": {"logout": None}}


@pytest.mark.parametrize(
    "call",
    [
        lambda api: api.async_set_setting(FIELDS["band_navigation_enabled"], True),
        lambda api: api.async_set_controller_alias("Name"),
        lambda api: api.async_set_radio_enabled("0", "1", True),
        lambda api: api.async_save_configuration(),
    ],
)
async def test_a_retired_client_cannot_write(call):
    api, session, controller = make_api()
    await api.async_logout()
    controller.bodies.clear()
    with pytest.raises(Ac100Error, match="client has been closed"):
        await call(api)
    assert controller.bodies == []
    session.close.assert_not_called()


async def test_debug_logs_of_writes_hold_fixed_text_and_counts_only(caplog, clock):
    logger_name = "custom_components.tplink_ac100.api"
    caplog.set_level(logging.DEBUG, logger=logger_name)
    api, _, controller = make_api()
    controller.sections["system", "sys"]["dev_alias"] = "private-old-name"
    await api.async_set_setting(FIELDS["load_balance_enabled"], True)
    await api.async_set_controller_alias("private-new-name")
    await api.async_set_radio_enabled("1", "2", False)
    await api.async_save_configuration()
    records = [record for record in caplog.records if record.name == logger_name]
    messages = "\n".join(record.getMessage() for record in records)
    assert len(records) == 5
    assert all(record.levelno == logging.DEBUG for record in records)
    assert all(isinstance(arg, int | bool) for record in records for arg in record.args)
    for secret in ("private-", "-1,3", "controller.example", "synthetic-session", "/stok=", "18"):
        assert secret not in messages


def test_the_catalog_offers_exactly_the_planned_controls():
    names = lambda kind: {item.key for item in writable_fields(kind)}  # noqa: E731
    assert names("switch") == {
        "band_navigation_enabled",
        "load_balance_enabled",
        "broadcast_storm_control_enabled",
        "igmp_snooping_enabled",
    }
    assert names("integer") == {
        "band_navigation_5g_threshold",
        "band_navigation_difference_threshold",
        "band_navigation_max_failures",
        "load_balance_threshold",
        "load_balance_difference_threshold",
        "load_balance_max_failures",
        "load_balance_rssi_threshold",
        "broadcast_storm_rate",
    }
    assert names("choice") == {
        "igmp_unknown_multicast",
        "igmp_2g_multicast_rate",
        "igmp_5g_multicast_rate",
    }
    assert names("text") == set()
    assert (ALIAS_FIELD.module, ALIAS_FIELD.section, ALIAS_FIELD.field) == (
        "system",
        "sys",
        "dev_alias",
    )
    assert {
        (form.module, form.section): tuple(part.name for part in form.fields) for form in FORMS
    } == LIVE_FORMS


def test_selects_offer_only_the_choices_of_their_pages():
    assert select_options(FIELDS["igmp_unknown_multicast"]) == ("forward", "drop")
    assert select_options(FIELDS["igmp_2g_multicast_rate"]) == ("1", "11")
    assert select_options(FIELDS["igmp_5g_multicast_rate"]) == ("6", "24")
    assert select_options(FIELDS["band_navigation_enabled"]) == ()


@pytest.mark.parametrize(
    ("key", "option", "raw"),
    [
        ("igmp_unknown_multicast", "forward", "1"),  # the page's Forward is "1", its Drop "0"
        ("igmp_unknown_multicast", "drop", "0"),
        ("igmp_2g_multicast_rate", 1, "1"),
        ("igmp_2g_multicast_rate", 11, "11"),
        ("igmp_5g_multicast_rate", 6, "6"),
        ("igmp_5g_multicast_rate", 24, "24"),
    ],
)
def test_every_choice_of_a_select_has_its_wire_value_on_the_page(key, option, raw):
    assert encode_setting(FIELDS[key], option) == raw
