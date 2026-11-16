"""The saved configuration of terminals and the terminal actions, against a fake SLP controller.

The fake answers the reads of the terminal pages from a small model of one network and changes
that model when a write arrives, so every test sees the same fresh reads that the API sees. The
wire requests that the API sends are compared with the ones that the controller's own page
sends (userrpm__wireless_terminal_mngr.htm and userrpm__wireless_terminal_identify.htm).
"""

import asyncio
import copy
import json
import logging
from dataclasses import asdict, replace
from pathlib import Path
from unittest.mock import MagicMock
from urllib.parse import quote, unquote

import aiohttp
import pytest

from custom_components.tplink_ac100 import api as api_module
from custom_components.tplink_ac100.api import (
    Ac100AccountLockedError,
    Ac100Api,
    Ac100AuthError,
    Ac100ConnectionError,
    Ac100Error,
    Ac100ProtocolError,
    Ac100SessionBusyError,
    Ac100SessionExpiredError,
    Ac100TerminalError,
    Ac100TerminalPartialError,
)
from custom_components.tplink_ac100.terminal import (
    TerminalBindingTarget,
    TerminalConfiguration,
    TerminalField,
    TerminalState,
    validate_terminal_value,
)
from custom_components.tplink_ac100.terminal_schedule import DAYS, TerminalSchedule

FIXTURES = Path(__file__).parent / "fixtures"
EXPIRED = {"error_code": -40401, "data": {"code": -40401}}
SESSION_URL = "http://controller.example/stok=synthetic-session/ds"


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


def plain(value):
    if isinstance(value, dict):
        return {key: plain(item) for key, item in value.items()}
    if isinstance(value, list):
        return [plain(item) for item in value]
    return unquote(value) if isinstance(value, str) else value


# One network: the fixture's six APs (two of them are both named "Example AP 3") and a few
# terminals, each with every field that the controller's host table has.


def mac_text(mac):
    """A MAC as the controller spells it: upper case with dashes."""
    return mac.upper().replace(":", "-")


def host(mac, **fields):
    """A host row. Defaults describe a terminal that has no saved settings and is connected."""
    row = {
        "mac": mac_text(mac),
        "entry_id": "7",
        "ap_name": "Example AP 1",
        "ssid": "Example WiFi 1",
        "serv_id": "3",
        "ip": "192.0.2.50",
        "ipv6": "::",
        "encode": "1",
        "vlan_id": "0",
        "brand": "---",
        "operating_system": "---",
        "up_speed": "1",
        "down_speed": "2",
        "connect_date": "2026/11/15",
        "connect_time": "02:59:32",
        "rf_entry": [{"freq_name": "2.4GHz", "rssi": "-50", "freq_unit": "1", "nego_rate": "276"}],
        "limit": "0",
        "up_limit": "0",
        "down_limit": "0",
        "bind_enable": "off",
        "force_bind_enable": "off",
        "bind_radio_id": [],
        "time_obj_array": [],
    }
    return row | fields


PHONE = "02:00:00:00:01:01"
LAPTOP = "02:00:00:00:01:02"
TABLET = "02:00:00:00:01:03"
NAMELESS = "02:00:00:00:01:04"
# The fixture's APs by command address. Entries 2 and 4 are both named "Example AP 3".
AP_E0, AP_E2, AP_E3, AP_E4, AP_E5 = (f"02:00:00:00:00:0{n}" for n in (1, 3, 4, 5, 6))
CALENDAR_ROW = {
    "name": "020000000103",
    "flag": "inner",
    "comment": "",
    "time_type": "section",
    "mode": "calendar",
    "mon_section": ["08:00:00-12:00:00"],
    "tue_section": [],
    "wed_section": [],
    "thu_section": [],
    "fri_section": [],
    "sat_section": ["00:00:00-24:00:00"],
    "sun_section": [],
}
MANUAL_ROW = {
    "name": "020000000102",
    "flag": "inner",
    "comment": "",
    "time_type": "section",
    "mode": "manual",
    "weekday": "31",
    "time_section": ["08:00:00-12:30:00", "13:00:00-24:00:00"],
}
MANUAL = TerminalSchedule(
    "manual",
    weekdays=DAYS[:5],
    windows=("08:00-12:30", "13:00-24:00"),
)
CALENDAR = TerminalSchedule(
    "calendar",
    calendar=(("08:00-12:00",), (), (), (), (), ("00:00-24:00",), ()),
)


def radio_row(radio_id, unit, band, ap_name, status="1"):
    return {
        "bind_status": status,
        "ap_type": "ap",
        "freq_name": band,
        "bind_vlan_type": "single_vlan",
        "freq_unit": str(unit),
        "vlan_pool_id": "",
        "radio_id": str(radio_id),
        "freq_mode": "6",
        "vlan_id": "0",
        "ap_name": ap_name,
    }


class FakeController:
    """An SLP double for the terminal tables, whose rows change when a write arrives.

    Reads are answered from the model, ten rows to a page like the integration asks, and a write
    updates the fields that it names, as the controller does. ``inject`` answers the request
    with that index in place of the model.
    """

    def __init__(self):
        self.aps = [
            plain(next(iter(wrapper.values())))
            for wrapper in load("aps.json")["apmng_status"]["ap_list"]
        ]
        self._radios = [
            (int(ap["entry_id"]) * 16 + int(radio["freq_unit"]), radio, copy.deepcopy(ap))
            for ap in self.aps
            for radio in ap["rf_entry"]
        ]
        self.clients = []
        self.hosts = [
            host(PHONE, hostname="Example phone", serv_id="3"),
            host(
                LAPTOP,
                hostname="Example laptop",
                limit="1",
                up_limit="100",
                down_limit="2000",
                bind_enable="on",
                force_bind_enable="off",
                bind_radio_id=["33", "34"],
                time_obj_array=["5"],
                ap_name="Example AP 3",
            ),
            host(
                TABLET,
                hostname="Old tablet",
                serv_id="-1",
                ap_name="",
                limit="1",
                up_limit="50",
                down_limit="0",
                bind_radio_id=["82"],
                time_obj_array=["6"],
            ),
            host(NAMELESS, serv_id="0"),
        ]
        # Service 3 is broadcast by AP 1, both APs named "Example AP 3" and AP 4's 2.4 GHz radio.
        self.bound = {"3": {1, 2, 33, 34, 49, 65, 66}, "0": {1, 33}}
        self.time_objects = {"5": copy.deepcopy(MANUAL_ROW), "6": copy.deepcopy(CALENDAR_ROW)}
        self.next_time_object = 9
        self.rules = []
        self.inject = {}
        self.gate_at = None
        self.gate = None
        self.gate_created = asyncio.Event()
        self.bodies = []
        self.urls = []

    def on(self, predicate, response, times=None):
        """Answer the requests that ``predicate`` accepts with ``response``, ``times`` at most."""
        self.rules.append([predicate, response, times])

    def radios(self):
        """Every radio as (address, radio, AP), fixed when the controller was built."""
        return self._radios

    def __call__(self, url, **kwargs):
        body = plain(kwargs["json"])
        self.urls.append(url)
        self.bodies.append(kwargs["json"])
        if (index := len(self.bodies) - 1) in self.inject:
            return self.inject[index]
        for rule in self.rules:
            if rule[2] != 0 and rule[0](body):
                rule[2] = None if rule[2] is None else rule[2] - 1
                answer = rule[1](body) if callable(rule[1]) else rule[1]
                return answer if isinstance(answer, Response) else Response(answer)
        if url.endswith("/"):
            if "user_management" in body:
                return Response({"error_code": 0, "encrypt_type": ["-3"]})
            return Response({"error_code": 0, "stok": "renewed-session"})
        response = self.answer(body)
        if self.gate_at == index:
            self.gate = GatedResponse(response)
            self.gate_created.set()
            return self.gate
        return response if isinstance(response, Response) else Response(response)

    def answer(self, body):
        if "firmware" in body:
            return load("controller.json")
        if "interface" in body:
            return load("identity.json")
        (module,) = (key for key in body if key != "method")
        query = body[module]
        if body["method"] == "get":
            return self.read(module, query)
        return self.write(body["method"], module, query)

    def table(self, module, name, rows, query):
        start, end = query["para"]["start"], query["para"]["end"]
        return {
            "error_code": 0,
            module: {
                name: [
                    {f"{name}_{start + i}": wire(row)}
                    for i, row in enumerate(rows[start : end + 1])
                ],
                "count": {name: len(rows)},
            },
        }

    def read(self, module, query):
        if module == "apmng_status":
            return self.table(module, "ap_list", self.aps, query)
        if module == "apmng_client":
            return self.table(module, "client_list", self.clients, query)
        if module == "host_management":
            return self.table(module, "host_info", self.hosts, query)
        if module == "apmng_wserv":
            (flt,) = query["filter"]
            bound = self.bound.get(flt["serv_id"], set())
            rows = [
                radio_row(
                    radio_id,
                    radio["freq_unit"],
                    radio["freq_name"],
                    ap["entry_name"],
                    "1" if radio_id in bound else "0",
                )
                for radio_id, radio, ap in self.radios()
                if "bind_status" not in flt or radio_id in bound
            ]
            assert "para" not in query
            return {
                "error_code": 0,
                module: {
                    "radio_bind_list": [
                        {f"radio_bind_list_{i}": wire(row)} for i, row in enumerate(rows)
                    ],
                    "count": {"radio_bind_list": len(rows)},
                },
            }
        (flt,) = query["filter"]
        rows = [
            {"id": flt["id"], **row} for key, row in self.time_objects.items() if key == flt["id"]
        ]
        # The time table is read without paging, like the terminal page does.
        return {
            "error_code": 0,
            module: {
                "time_obj": [{f"time_obj_{i}": wire(row)} for i, row in enumerate(rows)],
                "count": {"time_obj": len(rows)},
            },
        }

    def write(self, method, module, query):
        if module == "host_management":
            if method == "do":
                gone = {mac for mac in query["disconnect"]["mac_list"]}
                self.clients = [row for row in self.clients if row["mac"] not in gone]
                return {"error_code": 0}
            (flt,) = query["filter"]
            if method == "delete":
                self.hosts = [row for row in self.hosts if row["mac"] != flt["mac"]]
                return {"error_code": 0}
            for row in self.hosts:
                if row["mac"] == flt["mac"]:
                    row.update(copy.deepcopy(query["para"]))
            return {"error_code": 0}
        if method == "add":
            key = str(self.next_time_object)
            self.next_time_object += 1
            self.time_objects[key] = copy.deepcopy(query["para"])
            return {"error_code": 0, "time_mngt": {"name": [f"time_obj_{key}"]}}
        (flt,) = query["filter"]
        if method == "delete":
            self.time_objects.pop(flt["id"], None)
        else:
            self.time_objects[flt["id"]].update(copy.deepcopy(query["para"]))
        return {"error_code": 0}

    def renumber(self, old, new):
        """The controller gives an AP another command address; its radios and networks follow."""
        for ap in self.aps:
            if ap["entry_id"] == old:
                ap["entry_id"] = new
        self._radios = [
            (int(ap["entry_id"]) * 16 + int(radio["freq_unit"]), radio, copy.deepcopy(ap))
            for ap in self.aps
            for radio in ap["rf_entry"]
        ]
        moved = {int(old) * 16 + unit: int(new) * 16 + unit for unit in (1, 2)}
        self.bound = {
            service: {moved.get(radio, radio) for radio in radios}
            for service, radios in self.bound.items()
        }

    def present(self, mac, entry_id="40"):
        self.clients.append(
            {
                "entry_id": entry_id,
                "mac": mac_text(mac),
                "ip": "192.0.2.50",
                "ipv6": "::",
                "ssid": "Example WiFi 1",
                "ap_name": "Example AP 1",
                "rf_entry": [],
            }
        )

    def record(self, mac):
        return [row for row in self.hosts if row["mac"] == mac_text(mac)]

    def writes(self):
        return [plain(body) for body in self.bodies if body.get("method") != "get"]

    def requests(self):
        """What each request read or did, by module and table, in order."""
        return [plain(body) for body in self.bodies if "user_management" not in body]


def make_api(controller=None):
    controller = controller or FakeController()
    session = MagicMock(spec=aiohttp.ClientSession)
    session.post.side_effect = controller
    api = Ac100Api(session, "controller.example", "admin", "example-password")
    api._stok = "synthetic-session"
    return api, session, controller


def shape(body):
    """A short name for a request: its method, module and table (or section)."""
    module = next(key for key in body if key != "method")
    query = body[module]
    return body["method"], module, query.get("table") if isinstance(query, dict) else None


def configuration(snapshot, mac):
    return next(item for item in snapshot.terminals.configurations if item.mac == mac)


def kind(body):
    """The module and table of a read, for choosing the requests that a test changes."""
    return shape(body)[1:]


def radio_list_request(service=None):
    """The manager's exact direct, unpaged radio query for one service or every radio."""
    flt = {"group_id": ["-1"], "serv_id": "0"}
    if service is not None:
        flt = {"group_id": ["-1"], "bind_status": "1", "serv_id": service}
    return {
        "method": "get",
        "apmng_wserv": {"table": "radio_bind_list", "filter": [flt]},
    }


def time_object_request(object_id):
    """The terminal page's read of a time object: by address and without paging."""
    return {
        "method": "get",
        "time_mngt": {"table": "time_obj", "filter": [{"id": object_id}]},
    }


def targets_of(ap_mac, ap_name, units):
    """The binding targets of an AP with these radios, as the binding editor offers them."""
    names = {"1": "2.4GHz", "2": "5GHz"}
    entry = {mac: key for key, mac in AP_ENTRIES.items()}[ap_mac]
    ids = tuple(str(int(entry) * 16 + int(unit)) for unit in units)
    every = (
        [TerminalBindingTarget(f"{ap_mac}/all", ap_mac, ap_name, None, "", ids)]
        if len(units) > 1
        else []
    )
    return every + [
        TerminalBindingTarget(f"{ap_mac}/{unit}", ap_mac, ap_name, unit, names[unit], (radio,))
        for unit, radio in zip(units, ids, strict=True)
    ]


AP_ENTRIES = {"0": AP_E0, "2": AP_E2, "3": AP_E3, "4": AP_E4, "5": AP_E5}
SERVICE_3_TARGETS = tuple(
    targets_of(AP_E0, "Example AP 1", "12")
    + targets_of(AP_E2, "Example AP 3", "12")
    + targets_of(AP_E3, "Example AP 4", "1")
    + targets_of(AP_E4, "Example AP 3", "12")
)
SERVICE_0_TARGETS = tuple(
    targets_of(AP_E0, "Example AP 1", "1") + targets_of(AP_E2, "Example AP 3", "1")
)


async def poll(controller=None):
    api, session, controller = make_api(controller)
    return await api.async_get_snapshot(), controller


# Reading: the saved configuration, apart from the connection


async def test_the_poll_reads_each_dependent_source_once_in_a_fixed_order():
    snapshot, controller = await poll()
    requests = controller.requests()
    assert [shape(body)[1:] for body in requests[:5]] == [
        ("firmware", None),
        ("interface", "interface"),
        ("apmng_status", "ap_list"),
        ("apmng_client", "client_list"),
        ("host_management", "host_info"),
    ]
    # Each service once, though two terminals share service 3; no offline service query.
    # One saved radio needs the all-radio fallback: eleven radios arrive in one direct query.
    assert requests[5:] == [
        radio_list_request("3"),
        radio_list_request("0"),
        radio_list_request(),
        time_object_request("5"),
        time_object_request("6"),
    ]
    assert len(snapshot.terminals.configurations) == 4
    assert all(body["method"] == "get" for body in requests)


async def test_the_saved_configuration_of_each_terminal():
    snapshot, _ = await poll()
    assert [item.mac for item in snapshot.terminals.configurations] == [
        PHONE,
        LAPTOP,
        TABLET,
        NAMELESS,
    ]
    assert configuration(snapshot, PHONE) == TerminalConfiguration(
        mac=PHONE,
        name="Example phone",
        rate_limit_enabled=False,
        upload_limit=0,
        download_limit=0,
        binding_enabled=False,
        force_binding=False,
        bound_radio_ids=(),
        binding_targets=SERVICE_3_TARGETS,
        binding_available=True,
        allowed_time_mode="any",
        schedule_available=True,
    )
    assert configuration(snapshot, LAPTOP) == TerminalConfiguration(
        mac=LAPTOP,
        name="Example laptop",
        rate_limit_enabled=True,
        upload_limit=100,
        download_limit=2000,
        binding_enabled=True,
        force_binding=False,
        bound_radio_ids=("33", "34"),
        binding_targets=SERVICE_3_TARGETS,
        binding_available=True,
        bound_ap="Example AP 3",
        bound_radio="2.4GHz, 5GHz",
        bound_target=f"{AP_E2}/all",
        allowed_time_mode="manual",
        schedule=MANUAL,
        schedule_available=True,
    )
    # A retained record of a terminal that is away: no binding edit, the saved radio is named
    # from the list of every radio, and nothing says that the terminal is connected.
    assert configuration(snapshot, TABLET) == TerminalConfiguration(
        mac=TABLET,
        name="Old tablet",
        rate_limit_enabled=True,
        upload_limit=50,
        download_limit=0,
        binding_enabled=False,
        force_binding=False,
        bound_radio_ids=("82",),
        binding_available=False,
        bound_ap="Example AP 5",
        bound_radio="5GHz",
        allowed_time_mode="calendar",
        schedule=CALENDAR,
        schedule_available=True,
    )
    assert configuration(snapshot, NAMELESS).name is None
    assert configuration(snapshot, NAMELESS).binding_targets == SERVICE_0_TARGETS
    assert snapshot.clients == ()


async def test_two_aps_with_the_same_name_are_told_apart_by_mac():
    snapshot, _ = await poll()
    both = [
        t for t in configuration(snapshot, PHONE).binding_targets if t.ap_name == "Example AP 3"
    ]
    assert {t.ap_mac for t in both} == {AP_E2, AP_E4}
    assert len({t.token for t in both}) == len(both) == 6
    assert all(t.token.startswith(t.ap_mac) for t in both)


async def test_an_ap_with_one_eligible_radio_offers_only_that_radio():
    snapshot, _ = await poll()
    tokens = [t.token for t in configuration(snapshot, PHONE).binding_targets]
    assert f"{AP_E3}/1" in tokens
    assert f"{AP_E3}/all" not in tokens
    assert f"{AP_E0}/all" in tokens


async def test_the_configuration_holds_nothing_but_its_own_fields():
    controller = FakeController()
    controller.hosts[0] |= {"entry_id": "entry-canary", "secret": "secret-canary"}
    snapshot, _ = await poll(controller)
    text = repr(asdict(snapshot.terminals))
    assert "entry-canary" not in text
    assert "secret-canary" not in text
    assert "192.0.2.50" not in text


async def test_an_empty_readable_host_table_is_no_record_and_an_unreadable_one_is_unknown():
    controller = FakeController()
    controller.hosts = []
    snapshot, controller = await poll(controller)
    assert snapshot.terminals == TerminalState(())
    assert [shape(body)[1] for body in controller.requests()][-1] == "host_management"
    broken = FakeController()
    broken.on(lambda body: kind(body) == ("host_management", "host_info"), {"error_code": -40210})
    snapshot, broken = await poll(broken)
    assert snapshot.terminals is None
    assert not [body for body in broken.requests() if "apmng_wserv" in body or "time_mngt" in body]


def radio_list_of(service):
    """Match the read of the radios of one wireless network."""

    def match(body):
        flt = body.get("apmng_wserv", {}).get("filter", [{}])[0]
        return kind(body) == ("apmng_wserv", "radio_bind_list") and flt.get("serv_id") == service

    return match


def time_object_of(object_id):
    def match(body):
        return kind(body) == ("time_mngt", "time_obj") and (
            body["time_mngt"]["filter"][0]["id"] == object_id
        )

    return match


def one_row_answer(module, table, rows):
    """An answer that holds these rows, ten to a page when it is asked for pages."""

    def answer(body):
        para = body[module].get("para", {"start": 0, "end": len(rows)})
        return {
            "error_code": 0,
            module: {
                table: [
                    {f"{table}_{para['start'] + i}": wire(row)}
                    for i, row in enumerate(rows[para["start"] : para["end"] + 1])
                ],
                "count": {table: len(rows)},
            },
        }

    return answer


UNREADABLE = {
    "rejected": lambda: {"error_code": -40210},
    "not JSON": lambda: Response(text="not json"),
    "no table": lambda: {"error_code": 0},
    "account may not read it": lambda: Response({"error_code": -40100}, status=401),
    "forbidden": lambda: Response(status=403),
}


@pytest.mark.parametrize("failure", UNREADABLE)
async def test_an_unreadable_radio_list_leaves_only_the_binding_unknown(failure):
    baseline, _ = await poll()
    controller = FakeController()
    controller.on(radio_list_of("3"), UNREADABLE[failure]())
    snapshot, controller = await poll(controller)
    for mac in (PHONE, LAPTOP):
        item, healthy = configuration(snapshot, mac), configuration(baseline, mac)
        assert (item.binding_targets, item.binding_available, item.bound_target) == (
            (),
            False,
            None,
        )
        assert item == replace(
            healthy, binding_targets=(), binding_available=False, bound_target=None
        )
    for mac in (TABLET, NAMELESS):
        assert configuration(snapshot, mac) == configuration(baseline, mac)
    # No second sign-in, and the rest of the poll is the healthy one.
    assert not [url for url in controller.urls if url.endswith("/")]
    assert snapshot.controller == baseline.controller
    assert snapshot.aps == baseline.aps


@pytest.mark.parametrize("failure", UNREADABLE)
async def test_an_unreadable_time_object_leaves_only_its_schedule_unknown(failure):
    baseline, _ = await poll()
    controller = FakeController()
    controller.on(time_object_of("5"), UNREADABLE[failure]())
    snapshot, controller = await poll(controller)
    laptop = configuration(snapshot, LAPTOP)
    assert (laptop.allowed_time_mode, laptop.schedule, laptop.schedule_available) == (
        None,
        None,
        False,
    )
    assert laptop == replace(
        configuration(baseline, LAPTOP),
        allowed_time_mode=None,
        schedule=None,
        schedule_available=False,
    )
    assert configuration(snapshot, TABLET) == configuration(baseline, TABLET)
    assert not [url for url in controller.urls if url.endswith("/")]


@pytest.mark.parametrize(
    ("answer", "error"),
    [
        (Response({"error_code": -57704}, status=401), Ac100SessionBusyError),
        (Response({"error_code": -40404}, status=401), Ac100AccountLockedError),
    ],
)
@pytest.mark.parametrize("source", ["radios", "time objects"])
async def test_busy_sessions_and_account_locks_still_fail_the_poll(source, answer, error):
    controller = FakeController()
    controller.on(
        radio_list_of("3") if source == "radios" else time_object_of("5"),
        answer,
    )
    with pytest.raises(error) as exc:
        await poll(controller)
    assert "private-url" not in str(exc.value)


async def test_duplicate_rows_of_one_mac_are_one_record_while_they_agree():
    baseline, _ = await poll()
    controller = FakeController()
    stale = copy.deepcopy(controller.hosts[1]) | {"ap_name": "Example AP 5", "up_speed": "---"}
    controller.hosts.insert(2, stale)
    snapshot, _ = await poll(controller)
    assert snapshot.terminals == baseline.terminals


async def test_duplicate_rows_that_disagree_leave_only_the_disputed_settings_unknown():
    baseline, _ = await poll()
    controller = FakeController()
    controller.hosts.append(copy.deepcopy(controller.hosts[1]) | {"up_limit": "300"})
    snapshot, _ = await poll(controller)
    item = configuration(snapshot, LAPTOP)
    assert item.upload_limit is None
    assert asdict(item) | {"upload_limit": 100} == asdict(configuration(baseline, LAPTOP))
    other = copy.deepcopy(controller.hosts[1]) | {"serv_id": "0"}
    controller.hosts[-1] = other
    snapshot, _ = await poll(controller)
    item = configuration(snapshot, LAPTOP)
    assert (item.binding_available, item.binding_targets, item.bound_target) == (False, (), None)
    assert item.upload_limit == 100


@pytest.mark.parametrize(
    ("fields", "expected"),
    [
        ({"limit": "2"}, {"rate_limit_enabled": None}),
        ({"limit": "1", "up_limit": "abc", "down_limit": "-5"}, {"upload_limit": None}),
        ({"up_limit": "---", "down_limit": 7}, {"upload_limit": None, "download_limit": 7}),
        ({"bind_enable": "yes", "force_bind_enable": "---"}, {"binding_enabled": None}),
        ({"bind_radio_id": "17"}, {"bound_radio_ids": ("17",)}),
        ({"bind_radio_id": ["17", "x"]}, {"bound_radio_ids": None}),
        ({"bind_radio_id": 5}, {"bound_radio_ids": None}),
        ({"time_obj_array": ["5", "6"]}, {"allowed_time_mode": None}),
        ({"time_obj_array": "x"}, {"allowed_time_mode": None}),
        ({"hostname": "   "}, {"name": None}),
        ({"hostname": 5}, {"name": None}),
    ],
)
async def test_a_value_in_a_form_the_page_cannot_send_is_unknown_not_a_default(fields, expected):
    controller = FakeController()
    controller.hosts[0] |= fields
    snapshot, _ = await poll(controller)
    item = configuration(snapshot, PHONE)
    assert all(getattr(item, key) == value for key, value in expected.items())


async def test_a_record_without_a_saved_field_has_it_unknown():
    controller = FakeController()
    for key in ("limit", "up_limit", "down_limit", "bind_enable", "force_bind_enable"):
        del controller.hosts[0][key]
    del controller.hosts[0]["bind_radio_id"]
    del controller.hosts[0]["time_obj_array"]
    snapshot, _ = await poll(controller)
    item = configuration(snapshot, PHONE)
    assert (item.rate_limit_enabled, item.upload_limit, item.download_limit) == (None, None, None)
    assert (item.binding_enabled, item.force_binding, item.bound_radio_ids) == (None, None, None)
    assert (item.allowed_time_mode, item.schedule, item.schedule_available) == (None, None, False)


async def test_a_linked_time_object_is_read_once_however_many_records_link_to_it():
    controller = FakeController()
    controller.hosts[3] |= {"time_obj_array": ["5"]}
    snapshot, controller = await poll(controller)
    reads = [body for body in controller.requests() if "time_mngt" in body]
    assert reads == [time_object_request("5"), time_object_request("6")]
    assert configuration(snapshot, NAMELESS).schedule == MANUAL


@pytest.mark.parametrize(
    "answer",
    [
        one_row_answer("time_mngt", "time_obj", []),
        one_row_answer("time_mngt", "time_obj", [MANUAL_ROW, CALENDAR_ROW]),
        one_row_answer("time_mngt", "time_obj", [{"mode": "any", "name": "x", "flag": "inner"}]),
        one_row_answer("time_mngt", "time_obj", [MANUAL_ROW | {"time_type": "interval"}]),
        one_row_answer("time_mngt", "time_obj", [MANUAL_ROW | {"weekday": "512"}]),
        one_row_answer(
            "time_mngt", "time_obj", [MANUAL_ROW | {"time_section": ["25:00:00-26:00:00"]}]
        ),
    ],
)
async def test_a_time_object_that_is_not_exactly_one_readable_schedule_is_unknown(answer):
    controller = FakeController()
    controller.on(time_object_of("5"), answer)
    snapshot, _ = await poll(controller)
    laptop = configuration(snapshot, LAPTOP)
    assert (laptop.allowed_time_mode, laptop.schedule, laptop.schedule_available) == (
        None,
        None,
        False,
    )


async def test_a_linked_time_object_of_another_owner_is_still_shown():
    controller = FakeController()
    controller.time_objects["5"] |= {"name": "SOMETHING-ELSE", "flag": "user"}
    snapshot, _ = await poll(controller)
    assert configuration(snapshot, LAPTOP).schedule == MANUAL


async def test_every_distinct_service_and_object_is_read_without_the_old_skip_limits():
    controller = FakeController()
    controller.hosts = [
        host(
            f"02:00:00:02:00:{i:02x}",
            hostname=f"Terminal {i}",
            serv_id=str(i),
            time_obj_array=[str(i)],
        )
        for i in range(100)
    ]
    controller.time_objects = {
        str(i): MANUAL_ROW | {"name": f"0200000200{i:02X}"} for i in range(100)
    }
    snapshot, controller = await poll(controller)
    radio_reads = [body for body in controller.requests() if "apmng_wserv" in body]
    time_reads = [body for body in controller.requests() if "time_mngt" in body]
    assert radio_reads == [radio_list_request(str(i)) for i in range(100)]
    assert time_reads == [time_object_request(str(i)) for i in range(100)]
    assert len(snapshot.terminals.configurations) == 100
    assert all(item.binding_available for item in snapshot.terminals.configurations)
    assert all(item.schedule == MANUAL for item in snapshot.terminals.configurations)
    assert all("para" not in body["apmng_wserv"] for body in radio_reads)


def eligible_only(*radio_ids, extra=()):
    """An answer to the radio list of a network with these radios and any other rows."""
    controller = FakeController()
    rows = [
        radio_row(radio_id, radio["freq_unit"], radio["freq_name"], ap["entry_name"], status)
        for radio_id, radio, ap in controller.radios()
        for status in ["1" if radio_id in radio_ids else "0"]
        if radio_id in radio_ids or status == "0"
    ]
    return one_row_answer("apmng_wserv", "radio_bind_list", rows + list(extra))


async def test_only_a_radio_the_list_calls_bound_is_offered_even_if_the_filter_is_ignored():
    controller = FakeController()
    controller.on(radio_list_of("3"), eligible_only(1, 33, 34))
    snapshot, _ = await poll(controller)
    tokens = [target.token for target in configuration(snapshot, PHONE).binding_targets]
    assert tokens == [f"{AP_E0}/1", f"{AP_E2}/all", f"{AP_E2}/1", f"{AP_E2}/2"]


async def test_a_radio_of_an_ap_the_ap_list_lacks_is_dropped_and_templates_are_no_radios():
    controller = FakeController()
    extra = [
        radio_row(161, 1, "2.4GHz", "Gone AP"),
        radio_row(2, 2, "5GHz", "x") | {"ap_type": "template"},
    ]
    controller.on(radio_list_of("3"), eligible_only(1, extra=extra))
    snapshot, _ = await poll(controller)
    tokens = [target.token for target in configuration(snapshot, PHONE).binding_targets]
    assert tokens == [f"{AP_E0}/1"]


@pytest.mark.parametrize(
    "rows",
    [
        [radio_row(1, 1, "2.4GHz", "a"), radio_row(1, 1, "2.4GHz", "a")],
        [radio_row(1, 1, "2.4GHz", "a"), radio_row(2, 1, "5GHz", "a")],
        [radio_row("x", 1, "2.4GHz", "a")],
        [radio_row(1, "", "2.4GHz", "a")],
    ],
)
async def test_a_radio_list_with_rows_that_cannot_be_told_apart_is_unusable(rows):
    controller = FakeController()
    controller.on(radio_list_of("3"), one_row_answer("apmng_wserv", "radio_bind_list", rows))
    snapshot, _ = await poll(controller)
    item = configuration(snapshot, PHONE)
    assert (item.binding_targets, item.binding_available) == ((), False)
    assert configuration(snapshot, NAMELESS).binding_available


async def test_an_address_that_two_aps_claim_names_no_ap():
    controller = FakeController()
    controller.aps[3]["entry_id"] = "2"
    snapshot, _ = await poll(controller)
    owners = {target.ap_mac for target in configuration(snapshot, PHONE).binding_targets}
    assert owners == {AP_E0, AP_E4}
    assert configuration(snapshot, LAPTOP).bound_ap is None


async def test_a_saved_radio_that_matches_no_single_target_has_no_bound_target():
    controller = FakeController()
    controller.hosts[1] |= {"bind_radio_id": ["33", "66"]}
    snapshot, _ = await poll(controller)
    item = configuration(snapshot, LAPTOP)
    assert item.bound_target is None
    assert item.bound_ap is None
    assert item.bound_radio_ids == ("33", "66")
    controller.hosts[1] |= {"bind_radio_id": ["34"]}
    snapshot, _ = await poll(controller)
    assert configuration(snapshot, LAPTOP).bound_target == f"{AP_E2}/2"
    assert configuration(snapshot, LAPTOP).bound_radio == "5GHz"


async def test_an_offline_record_never_makes_a_client():
    snapshot, controller = await poll()
    assert snapshot.clients == ()
    controller.present(PHONE)
    snapshot, _ = await poll(controller)
    assert [client.mac for client in snapshot.clients] == [PHONE]
    assert len(snapshot.terminals.configurations) == 4


# Writing: exact page requests and the state that they must be planned from


def table_read(module, table):
    flt = {"group_id": ["-1"]}
    if module == "apmng_status":
        flt["ap_status"] = ["0"]
    return {
        "method": "get",
        module: {"table": table, "filter": [flt], "para": {"start": 0, "end": 9}},
    }


HOSTS_READ = table_read("host_management", "host_info")
CLIENTS_READ = table_read("apmng_client", "client_list")
APS_READ = table_read("apmng_status", "ap_list")
PHONE_FORM = {
    "hostname": "Example phone",
    "limit": "0",
    "up_limit": "0",
    "down_limit": "0",
    "bind_enable": "off",
    "force_bind_enable": "off",
    "time_obj_array": [],
    "mac": mac_text(PHONE),
}
LAPTOP_FORM = {
    "hostname": "Example laptop",
    "limit": "1",
    "up_limit": "100",
    "down_limit": "2000",
    "bind_enable": "on",
    "force_bind_enable": "off",
    "bind_radio_id": ["33", "34"],
    "time_obj_array": ["5"],
    "mac": mac_text(LAPTOP),
}
TABLET_FORM = {
    "hostname": "Old tablet",
    "limit": "1",
    "up_limit": "50",
    "down_limit": "0",
    "time_obj_array": ["6"],
    "mac": mac_text(TABLET),
}


def host_set(mac, para):
    return {
        "method": "set",
        "host_management": {
            "table": "host_info",
            "filter": [{"mac": mac_text(mac)}],
            "para": para,
        },
    }


def host_delete(mac):
    return {
        "method": "delete",
        "host_management": {
            "table": "host_info",
            "filter": [{"mac": mac_text(mac)}],
        },
    }


def time_write(method, object_id=None, para=None):
    section = {"table": "time_obj"}
    if object_id is not None:
        section["filter"] = [{"id": object_id}]
    if para is not None:
        section["para"] = para
    return {"method": method, "time_mngt": section}


def is_write(body):
    return body["method"] != "get"


async def test_rename_is_a_sparse_mac_filtered_name_only_write():
    api, _, controller = make_api()
    before = copy.deepcopy(controller.hosts)
    await api.async_set_terminal_field(PHONE, TerminalField.NAME, "Desk & study")
    assert controller.requests() == [HOSTS_READ, host_set(PHONE, {"hostname": "Desk & study"})]
    assert controller.hosts == [before[0] | {"hostname": "Desk & study"}, *before[1:]]
    # The value is URI encoded once on the wire, then decoded in the fake.
    assert controller.bodies[-1]["host_management"]["para"]["hostname"] == "Desk%20%26%20study"


async def test_disconnect_requires_fresh_presence_and_sends_the_present_mac_only():
    api, _, controller = make_api()
    controller.present(PHONE, "a-recyclable-row-address")
    before = copy.deepcopy(controller.hosts)
    await api.async_disconnect_terminal(PHONE)
    assert controller.requests() == [
        CLIENTS_READ,
        {"method": "do", "host_management": {"disconnect": {"mac_list": [mac_text(PHONE)]}}},
    ]
    assert controller.hosts == before
    assert controller.clients == []
    with pytest.raises(Ac100TerminalError, match="not connected"):
        await api.async_disconnect_terminal(PHONE)
    assert len(controller.writes()) == 1


@pytest.mark.parametrize(
    ("mac", "disconnect", "object_id"), [(LAPTOP, True, "5"), (TABLET, False, "6")]
)
async def test_clear_freshly_checks_presence_deletes_saved_host_then_its_owned_schedule(
    mac, disconnect, object_id
):
    api, _, controller = make_api()
    if disconnect:
        controller.present(mac)
    await api.async_clear_terminal(mac, disconnect=disconnect)
    assert controller.requests() == [
        HOSTS_READ,
        CLIENTS_READ,
        time_object_request(object_id),
        host_delete(mac),
        time_write("delete", object_id),
    ]
    assert controller.record(mac) == []
    assert object_id not in controller.time_objects
    assert len(controller.hosts) == 3
    assert len(controller.time_objects) == 1


@pytest.mark.parametrize(
    ("mac", "disconnect", "present"),
    [(PHONE, False, True), (PHONE, True, False), (TABLET, True, True)],
)
async def test_clear_refuses_a_presence_or_saved_record_state_that_changed(
    mac, disconnect, present
):
    api, _, controller = make_api()
    if present:
        controller.present(mac)
    with pytest.raises(Ac100TerminalError):
        await api.async_clear_terminal(mac, disconnect=disconnect)
    assert controller.requests() == [HOSTS_READ, CLIENTS_READ]
    assert not controller.writes()


@pytest.mark.parametrize("ownership", ["foreign name", "foreign flag", "shared"])
async def test_clear_deletes_no_foreign_or_shared_schedule(ownership):
    api, _, controller = make_api()
    if ownership == "foreign name":
        controller.time_objects["6"]["name"] = "other-owner"
    elif ownership == "foreign flag":
        controller.time_objects["6"]["flag"] = "user"
    else:
        controller.hosts[0]["time_obj_array"] = ["6"]
    await api.async_clear_terminal(TABLET, disconnect=False)
    assert controller.writes() == [host_delete(TABLET)]
    assert "6" in controller.time_objects


@pytest.mark.parametrize(
    ("mac", "field", "value", "form"),
    [
        (PHONE, TerminalField.RATE_LIMIT_ENABLED, True, PHONE_FORM | {"limit": "1"}),
        (
            LAPTOP,
            TerminalField.RATE_LIMIT_ENABLED,
            False,
            LAPTOP_FORM | {"limit": "0", "up_limit": "0", "down_limit": "0"},
        ),
        (LAPTOP, TerminalField.UPLOAD_LIMIT, 0, LAPTOP_FORM | {"up_limit": "0"}),
        (LAPTOP, TerminalField.DOWNLOAD_LIMIT, 125000, LAPTOP_FORM | {"down_limit": "125000"}),
        (TABLET, TerminalField.UPLOAD_LIMIT, 15, TABLET_FORM | {"up_limit": "15"}),
    ],
)
async def test_limit_changes_send_the_whole_form_and_preserve_every_other_setting(
    mac, field, value, form
):
    api, _, controller = make_api()
    await api.async_set_terminal_field(mac, field, value)
    assert controller.requests() == [HOSTS_READ, host_set(mac, form)]
    assert len(controller.writes()) == 1
    assert all(key not in form for key in ("entry_id", "ip", "ssid", "rf_entry", "serv_id"))


async def test_each_action_reads_the_host_again_instead_of_using_the_poll_or_the_last_write():
    api, _, controller = make_api()
    await api.async_get_snapshot()
    controller.bodies.clear()
    controller.hosts[1]["down_limit"] = "500"
    await api.async_set_terminal_field(LAPTOP, TerminalField.UPLOAD_LIMIT, 900)
    controller.hosts[1]["down_limit"] = "800"
    await api.async_set_terminal_field(LAPTOP, TerminalField.UPLOAD_LIMIT, 901)
    assert controller.requests() == [
        HOSTS_READ,
        host_set(LAPTOP, LAPTOP_FORM | {"up_limit": "900", "down_limit": "500"}),
        HOSTS_READ,
        host_set(LAPTOP, LAPTOP_FORM | {"up_limit": "901", "down_limit": "800"}),
    ]


async def test_missing_and_invalid_names_are_not_invented_in_a_whole_form():
    api, _, controller = make_api()
    await api.async_set_terminal_field(NAMELESS, TerminalField.RATE_LIMIT_ENABLED, True)
    para = controller.writes()[-1]["host_management"]["para"]
    assert "hostname" not in para
    controller.hosts[3]["hostname"] = "---\ninvalid"
    controller.hosts[3].pop("down_limit")
    await api.async_set_terminal_field(NAMELESS, TerminalField.UPLOAD_LIMIT, 100)
    para = controller.writes()[-1]["host_management"]["para"]
    assert "hostname" not in para
    assert "down_limit" not in para


@pytest.mark.parametrize(
    ("mac", "field", "value"),
    [
        (PHONE, TerminalField.NAME, "Example phone"),
        (PHONE, TerminalField.RATE_LIMIT_ENABLED, False),
        (LAPTOP, TerminalField.RATE_LIMIT_ENABLED, True),
        (LAPTOP, TerminalField.UPLOAD_LIMIT, 100),
        (LAPTOP, TerminalField.FORCE_BINDING, False),
        (PHONE, TerminalField.BINDING_ENABLED, False),
        (LAPTOP, TerminalField.BINDING_ENABLED, True),
        (LAPTOP, TerminalField.ALLOWED_TIME_MODE, "manual"),
    ],
)
async def test_an_unchanged_setting_does_not_write(mac, field, value):
    api, _, controller = make_api()
    controller.present(mac)
    await api.async_set_terminal_field(mac, field, value)
    assert not controller.writes()
    assert controller.requests()[0] == HOSTS_READ


@pytest.mark.parametrize(
    ("field", "value"),
    [
        (TerminalField.UPLOAD_LIMIT, 500),
        (TerminalField.DOWNLOAD_LIMIT, 0),
        (TerminalField.FORCE_BINDING, True),
        (TerminalField.BINDING_ENABLED, True),
    ],
)
async def test_state_dependent_refusals_read_then_do_not_write(field, value):
    api, _, controller = make_api()
    controller.present(PHONE)
    with pytest.raises(Ac100TerminalError):
        await api.async_set_terminal_field(PHONE, field, value)
    assert not controller.writes()


@pytest.mark.parametrize(
    ("field", "value", "changes", "fresh"),
    [
        (TerminalField.BINDING_ENABLED, False, {"bind_enable": "off"}, False),
        (TerminalField.FORCE_BINDING, True, {"force_bind_enable": "on"}, True),
        (
            TerminalField.BINDING_TARGET,
            f"{AP_E4}/2",
            {"bind_enable": "on", "bind_radio_id": ["66"]},
            True,
        ),
    ],
)
async def test_binding_and_force_preserve_the_form_and_targets_use_fresh_service_eligibility(
    field, value, changes, fresh
):
    api, _, controller = make_api()
    controller.present(LAPTOP)
    await api.async_set_terminal_field(LAPTOP, field, value)
    expected = LAPTOP_FORM | changes
    if field is TerminalField.BINDING_ENABLED:
        expected.pop("bind_radio_id")
    reads = (
        [HOSTS_READ, CLIENTS_READ, APS_READ, radio_list_request("3")]
        if fresh
        else [HOSTS_READ, CLIENTS_READ]
    )
    assert controller.requests() == reads + [host_set(LAPTOP, expected)]


async def test_selecting_an_eligible_ap_and_radio_also_enables_binding_atomically():
    api, _, controller = make_api()
    controller.present(PHONE)
    await api.async_set_terminal_field(PHONE, TerminalField.BINDING_TARGET, f"{AP_E2}/all")
    assert controller.requests() == [
        HOSTS_READ,
        CLIENTS_READ,
        APS_READ,
        radio_list_request("3"),
        host_set(PHONE, PHONE_FORM | {"bind_enable": "on", "bind_radio_id": ["33", "34"]}),
    ]
    assert controller.record(PHONE)[0]["bind_radio_id"] == ["33", "34"]


async def test_binding_can_be_reenabled_only_with_a_fresh_eligible_saved_target():
    api, _, controller = make_api()
    controller.present(PHONE)
    controller.hosts[0]["bind_radio_id"] = ["33", "34"]
    await api.async_set_terminal_field(PHONE, TerminalField.BINDING_ENABLED, True)
    assert controller.requests() == [
        HOSTS_READ,
        CLIENTS_READ,
        APS_READ,
        radio_list_request("3"),
        host_set(PHONE, PHONE_FORM | {"bind_enable": "on", "bind_radio_id": ["33", "34"]}),
    ]


@pytest.mark.parametrize(
    "field",
    [TerminalField.BINDING_ENABLED, TerminalField.FORCE_BINDING, TerminalField.BINDING_TARGET],
)
async def test_binding_is_not_editable_on_the_retained_offline_record(field):
    api, _, controller = make_api()
    with pytest.raises(Ac100TerminalError, match="connected terminal"):
        await api.async_set_terminal_field(
            TABLET, field, f"{AP_E0}/1" if field is TerminalField.BINDING_TARGET else True
        )
    assert controller.requests() == [HOSTS_READ]


@pytest.mark.parametrize("token", ["bad-token", "Example AP 3", f"{AP_E5}/2", f"{AP_E3}/2"])
async def test_binding_never_guesses_a_name_or_a_target_that_does_not_carry_the_service(token):
    api, _, controller = make_api()
    controller.present(PHONE)
    with pytest.raises(Ac100TerminalError, match="not available"):
        await api.async_set_terminal_field(PHONE, TerminalField.BINDING_TARGET, token)
    assert controller.requests() == [HOSTS_READ, CLIENTS_READ, APS_READ, radio_list_request("3")]
    assert not controller.writes()


async def test_a_stable_ap_mac_target_is_resolved_to_its_new_command_address_before_the_write():
    api, _, controller = make_api()
    controller.present(PHONE)
    await api.async_get_snapshot()
    controller.bodies.clear()
    controller.renumber("2", "6")
    await api.async_set_terminal_field(PHONE, TerminalField.BINDING_TARGET, f"{AP_E2}/all")
    assert controller.requests() == [
        HOSTS_READ,
        CLIENTS_READ,
        APS_READ,
        radio_list_request("3"),
        host_set(PHONE, PHONE_FORM | {"bind_enable": "on", "bind_radio_id": ["97", "98"]}),
    ]
    # No address from the old poll was sent.
    assert controller.writes()[0]["host_management"]["para"]["bind_radio_id"] == ["97", "98"]


async def test_a_recycled_ap_address_cannot_redirect_the_target_to_another_ap():
    api, _, controller = make_api()
    controller.present(PHONE)
    await api.async_get_snapshot()
    controller.bodies.clear()
    controller.aps[2]["mac"] = "02-00-00-00-09-09"
    with pytest.raises(Ac100TerminalError):
        await api.async_set_terminal_field(PHONE, TerminalField.BINDING_TARGET, f"{AP_E2}/all")
    assert not controller.writes()


async def test_a_roamed_terminal_uses_its_fresh_service_not_the_polled_one():
    api, _, controller = make_api()
    controller.present(PHONE)
    await api.async_get_snapshot()
    controller.bodies.clear()
    controller.hosts[0]["serv_id"] = "0"
    await api.async_set_terminal_field(PHONE, TerminalField.BINDING_TARGET, f"{AP_E2}/1")
    assert controller.requests() == [
        HOSTS_READ,
        CLIENTS_READ,
        APS_READ,
        radio_list_request("0"),
        host_set(PHONE, PHONE_FORM | {"bind_enable": "on", "bind_radio_id": ["33"]}),
    ]


@pytest.mark.parametrize("mode", ["manual", "calendar"])
async def test_first_schedule_is_created_and_linked_as_all_time(
    mode,
):
    api, _, controller = make_api()
    await api.async_set_terminal_field(PHONE, TerminalField.ALLOWED_TIME_MODE, mode)
    schedule = {
        "name": "020000000101",
        "flag": "inner",
        "comment": "",
        "time_type": "section",
        "mode": mode,
    }
    if mode == "manual":
        schedule |= {"weekday": "127", "time_section": ["00:00:00-24:00:00"]}
    else:
        schedule |= {
            f"{day}_section": ["00:00:00-24:00:00"]
            for day in ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
        }
    assert controller.requests() == [
        HOSTS_READ,
        time_write("add", para=schedule),
        host_set(PHONE, PHONE_FORM | {"time_obj_array": ["9"]}),
    ]
    assert controller.record(PHONE)[0]["time_obj_array"] == ["9"]
    assert controller.time_objects["9"] == schedule


@pytest.mark.parametrize(
    ("mac", "field", "value", "object_id", "expected"),
    [
        (LAPTOP, TerminalField.MANUAL_SATURDAY, True, "5", MANUAL_ROW | {"weekday": "63"}),
        (
            LAPTOP,
            TerminalField.MANUAL_WINDOWS,
            "09:00-10:00, 13:30-24:00",
            "5",
            MANUAL_ROW | {"time_section": ["09:00:00-10:00:00", "13:30:00-24:00:00"]},
        ),
        (
            TABLET,
            TerminalField.CALENDAR_TUESDAY,
            "06:00-07:00, 12:00-14:00",
            "6",
            CALENDAR_ROW | {"tue_section": ["06:00:00-07:00:00", "12:00:00-14:00:00"]},
        ),
    ],
)
async def test_schedule_edit_changes_the_owned_object_in_place_preserving_every_other_day(
    mac, field, value, object_id, expected
):
    api, _, controller = make_api()
    await api.async_set_terminal_field(mac, field, value)
    assert controller.requests() == [
        HOSTS_READ,
        time_object_request(object_id),
        time_write("set", object_id, expected),
        host_set(mac, LAPTOP_FORM if mac == LAPTOP else TABLET_FORM),
    ]
    assert controller.time_objects[object_id] == expected


async def test_any_time_unlinks_the_host_before_deleting_the_owned_schedule():
    api, _, controller = make_api()
    await api.async_set_terminal_field(LAPTOP, TerminalField.ALLOWED_TIME_MODE, "any")
    assert controller.requests() == [
        HOSTS_READ,
        time_object_request("5"),
        host_set(LAPTOP, LAPTOP_FORM | {"time_obj_array": []}),
        time_write("delete", "5"),
    ]
    assert controller.record(LAPTOP)[0]["time_obj_array"] == []
    assert "5" not in controller.time_objects


@pytest.mark.parametrize("ownership", ["foreign name", "foreign flag", "shared"])
async def test_an_existing_schedule_edit_refuses_foreign_or_shared_objects(ownership):
    api, _, controller = make_api()
    if ownership == "foreign name":
        controller.time_objects["5"]["name"] = "SOMEONE-ELSE"
    elif ownership == "foreign flag":
        controller.time_objects["5"]["flag"] = "user"
    else:
        controller.hosts[0]["time_obj_array"] = ["5"]
    with pytest.raises(Ac100TerminalError, match="not used by this terminal alone"):
        await api.async_set_terminal_field(LAPTOP, TerminalField.MANUAL_SATURDAY, True)
    assert controller.requests() == [HOSTS_READ, time_object_request("5")]
    assert not controller.writes()


@pytest.mark.parametrize(
    ("mac", "field", "value"),
    [
        (PHONE, TerminalField.MANUAL_MONDAY, True),
        (LAPTOP, TerminalField.CALENDAR_MONDAY, "09:00-10:00"),
        (LAPTOP, TerminalField.ALLOWED_TIME_MODE, "calendar"),
        (TABLET, TerminalField.MANUAL_WINDOWS, "09:00-10:00"),
    ],
)
async def test_fresh_schedule_state_refusals_are_terminal_errors_not_raw_value_errors(
    mac, field, value
):
    api, _, controller = make_api()
    with pytest.raises(Ac100TerminalError):
        await api.async_set_terminal_field(mac, field, value)
    assert not controller.writes()


async def test_unknown_or_multiple_time_object_links_are_refused_without_writing():
    for ids in (None, ["5", "6"], ["x"]):
        api, _, controller = make_api()
        controller.hosts[0]["time_obj_array"] = ids
        with pytest.raises(Ac100TerminalError, match="could not be read"):
            await api.async_set_terminal_field(PHONE, TerminalField.ALLOWED_TIME_MODE, "manual")
        assert controller.requests() == [HOSTS_READ]


# Failed writes never repeat, and an acknowledged earlier step is never hidden or undone.

WRITE_FAILURES = {
    "expired": (lambda: Response(EXPIRED, status=401), Ac100SessionExpiredError),
    "transport": (
        lambda: Response(error=aiohttp.ClientConnectionError("private-url-canary")),
        Ac100ConnectionError,
    ),
    "permission": (lambda: Response({"error_code": -40100}, status=401), Ac100AuthError),
    "rejected": (
        lambda: Response({"error_code": -40210, "detail": "private-body-canary"}),
        Ac100ProtocolError,
    ),
    "unreadable": (lambda: Response(text="private-html-canary"), Ac100ProtocolError),
}


@pytest.mark.parametrize("failure", WRITE_FAILURES)
@pytest.mark.parametrize(
    ("action", "stage"), [("create", "link"), ("any", "unlink"), ("clear", "clear")]
)
async def test_a_failed_follow_up_is_partial_stops_and_is_not_compensated_or_replayed(
    failure, action, stage
):
    api, _, controller = make_api()
    factory, cause = WRITE_FAILURES[failure]
    if action == "create":
        controller.on(lambda body: body["method"] == "set" and "host_management" in body, factory())
    else:
        controller.on(lambda body: body["method"] == "delete" and "time_mngt" in body, factory())
    with pytest.raises(Ac100TerminalPartialError) as exc:
        if action == "clear":
            await api.async_clear_terminal(TABLET, disconnect=False)
        else:
            await api.async_set_terminal_field(
                PHONE if action == "create" else LAPTOP,
                TerminalField.ALLOWED_TIME_MODE,
                "manual" if action == "create" else "any",
            )
    assert exc.value.stage == stage
    assert isinstance(exc.value.__cause__, cause)
    assert len(controller.writes()) == 2
    assert not [url for url in controller.urls if url.endswith("/")]
    assert all(
        word not in str(exc.value)
        for word in ("private-", mac_text(PHONE), "Example", "5", "6", "9")
    )
    if action == "create":
        assert controller.record(PHONE)[0]["time_obj_array"] == []
        assert "9" in controller.time_objects
    elif action == "any":
        assert controller.record(LAPTOP)[0]["time_obj_array"] == []
        assert "5" in controller.time_objects
    else:
        assert controller.record(TABLET) == []
        assert "6" in controller.time_objects


@pytest.mark.parametrize("failure", WRITE_FAILURES)
@pytest.mark.parametrize("action", ["name", "create", "any", "clear", "disconnect"])
async def test_a_first_write_failure_is_not_reported_as_acknowledged_and_is_never_replayed(
    failure, action
):
    api, _, controller = make_api()
    factory, error = WRITE_FAILURES[failure]
    controller.on(is_write, factory())
    controller.present(PHONE)
    with pytest.raises(error):
        if action == "disconnect":
            await api.async_disconnect_terminal(PHONE)
        elif action == "clear":
            await api.async_clear_terminal(TABLET, disconnect=False)
        else:
            await api.async_set_terminal_field(
                LAPTOP if action == "any" else PHONE,
                TerminalField.NAME if action == "name" else TerminalField.ALLOWED_TIME_MODE,
                {"name": "Desk", "create": "manual", "any": "any"}[action],
            )
    assert len(controller.writes()) == 1
    assert not [url for url in controller.urls if url.endswith("/")]
    assert api._stok == (None if failure == "expired" else "synthetic-session")


@pytest.mark.parametrize(
    "ack",
    [
        {},
        {"name": "time_obj_9"},
        {"name": [9]},
        {"name": []},
        {"name": ["time_obj_9_extra"]},
        {"name": ["other_9"]},
        {"name": ["time_obj_5"]},
        {"name": ["time_obj_1000000000"]},
    ],
)
async def test_an_acknowledged_add_without_a_new_unambiguous_address_stops_before_linking(ack):
    api, _, controller = make_api()
    controller.on(lambda body: body["method"] == "add", {"error_code": 0, "time_mngt": ack})
    with pytest.raises(Ac100TerminalPartialError) as exc:
        await api.async_set_terminal_field(PHONE, TerminalField.ALLOWED_TIME_MODE, "manual")
    assert exc.value.stage == "add"
    assert len(controller.writes()) == 1
    assert controller.writes()[0]["method"] == "add"


@pytest.mark.parametrize("unknown", [None, ["5", "x"]])
async def test_unknown_other_links_cannot_prove_a_schedule_exclusive(unknown):
    api, _, controller = make_api()
    controller.hosts[0]["time_obj_array"] = unknown
    with pytest.raises(Ac100TerminalError, match="not used by this terminal alone"):
        await api.async_set_terminal_field(LAPTOP, TerminalField.MANUAL_SATURDAY, True)
    assert not controller.writes()
    # Clearing another terminal's saved host is still possible, but it does not delete the
    # object while any other record might share it.
    await api.async_clear_terminal(TABLET, disconnect=False)
    assert controller.writes() == [host_delete(TABLET)]
    assert "6" in controller.time_objects


async def test_conflicting_duplicate_time_links_also_make_exclusivity_unknown():
    api, _, controller = make_api()
    controller.hosts.append(controller.hosts[0] | {"time_obj_array": ["5"]})
    with pytest.raises(Ac100TerminalError):
        await api.async_set_terminal_field(LAPTOP, TerminalField.MANUAL_SATURDAY, True)
    assert not controller.writes()


async def test_duplicate_hosts_that_disagree_cannot_write_back_a_whole_form():
    api, _, controller = make_api()
    controller.hosts.append(controller.hosts[1] | {"down_limit": "1000"})
    with pytest.raises(Ac100TerminalError, match="different settings"):
        await api.async_set_terminal_field(LAPTOP, TerminalField.UPLOAD_LIMIT, 400)
    assert not controller.writes()
    # The identification page's rename only sends the replacement name, not the disputed cap.
    await api.async_set_terminal_field(LAPTOP, TerminalField.NAME, "Study")
    assert controller.writes() == [host_set(LAPTOP, {"hostname": "Study"})]


async def test_a_disabled_or_offline_binding_is_not_echoed_or_required_to_be_valid():
    api, _, controller = make_api()
    controller.hosts[0]["bind_radio_id"] = "not-an-address"
    await api.async_set_terminal_field(PHONE, TerminalField.RATE_LIMIT_ENABLED, True)
    assert controller.writes() == [host_set(PHONE, PHONE_FORM | {"limit": "1"})]
    controller.hosts[2] |= {
        "bind_enable": "bad",
        "force_bind_enable": "bad",
        "bind_radio_id": "bad",
    }
    await api.async_set_terminal_field(TABLET, TerminalField.UPLOAD_LIMIT, 100)
    assert controller.writes()[-1] == host_set(TABLET, TABLET_FORM | {"up_limit": "100"})


@pytest.mark.parametrize("action", ["set", "clear"])
async def test_a_disappeared_record_is_not_created_and_a_recycled_row_id_is_not_trusted(action):
    api, _, controller = make_api()
    controller.hosts[0]["mac"] = "02-00-00-00-01-09"
    with pytest.raises(Ac100TerminalError, match="no saved record"):
        if action == "set":
            await api.async_set_terminal_field(PHONE, TerminalField.NAME, "Study")
        else:
            await api.async_clear_terminal(PHONE, disconnect=True)
    assert controller.requests() == [HOSTS_READ]
    assert not controller.writes()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        (TerminalField.NAME, ""),
        (TerminalField.NAME, "   "),
        (TerminalField.NAME, "\nname"),
        (TerminalField.NAME, "a" * 65),
        (TerminalField.NAME, "é" * 33),
        (TerminalField.UPLOAD_LIMIT, 1),
        (TerminalField.UPLOAD_LIMIT, -15),
        (TerminalField.UPLOAD_LIMIT, 125001),
        (TerminalField.UPLOAD_LIMIT, 15.5),
        (TerminalField.UPLOAD_LIMIT, float("inf")),
        (TerminalField.UPLOAD_LIMIT, True),
        (TerminalField.RATE_LIMIT_ENABLED, 1),
        (TerminalField.ALLOWED_TIME_MODE, "off"),
        (TerminalField.MANUAL_WINDOWS, "25:00-26:00"),
        (TerminalField.CALENDAR_MONDAY, "08:30-09:30"),
    ],
)
async def test_bad_values_are_rejected_before_any_read_or_write(field, value):
    api, _, controller = make_api()
    with pytest.raises(ValueError):
        await api.async_set_terminal_field(PHONE, field, value)
    assert controller.requests() == []


@pytest.mark.parametrize(
    ("field", "value", "expected"),
    [
        (TerminalField.NAME, "é" * 32, "é" * 32),
        (TerminalField.NAME, " Study ", " Study "),
        (TerminalField.UPLOAD_LIMIT, 0, 0),
        (TerminalField.UPLOAD_LIMIT, 15.0, 15),
        (TerminalField.UPLOAD_LIMIT, 125000, 125000),
        (TerminalField.MANUAL_WINDOWS, "00:00-24:00", "00:00-24:00"),
    ],
)
def test_values_at_the_page_boundaries_are_valid(field, value, expected):
    assert validate_terminal_value(field, value) == expected


async def test_all_terminal_actions_share_the_poll_and_management_lock():
    api, _, controller = make_api()
    # Keep the first action's fresh host read in flight, start another terminal action and a
    # normal poll, then prove that neither made a request until the first action finished.
    controller.gate_at = 0
    first = asyncio.create_task(api.async_set_terminal_field(PHONE, TerminalField.NAME, "Study"))
    await asyncio.wait_for(controller.gate_created.wait(), 1)
    await asyncio.wait_for(controller.gate.started.wait(), 1)
    second = asyncio.create_task(
        api.async_set_terminal_field(LAPTOP, TerminalField.UPLOAD_LIMIT, 500)
    )
    poll_task = asyncio.create_task(api.async_get_snapshot())
    await asyncio.sleep(0)
    assert controller.requests() == [HOSTS_READ]
    assert not second.done() and not poll_task.done()
    controller.gate.release.set()
    await asyncio.gather(first, second, poll_task)
    assert controller.requests()[:4] == [
        HOSTS_READ,
        host_set(PHONE, {"hostname": "Study"}),
        HOSTS_READ,
        host_set(LAPTOP, LAPTOP_FORM | {"up_limit": "500"}),
    ]
    assert controller.requests()[4]["method"] == "get"
    assert "firmware" in controller.requests()[4]


async def test_no_terminal_action_can_reopen_a_retired_client():
    api, _, controller = make_api()
    api._stok = None
    await api.async_logout()
    for action in (
        lambda: api.async_set_terminal_field(PHONE, TerminalField.NAME, "Study"),
        lambda: api.async_disconnect_terminal(PHONE),
        lambda: api.async_clear_terminal(TABLET, disconnect=False),
    ):
        with pytest.raises(Ac100Error, match="closed"):
            await action()
    assert not controller.requests()


async def test_debug_logs_and_refusals_do_not_reveal_terminal_identity_or_raw_responses(caplog):
    caplog.set_level(logging.DEBUG, logger=api_module.__name__)
    api, _, controller = make_api()
    controller.hosts[0]["hostname"] = "private-name-canary"
    await api.async_set_terminal_field(PHONE, TerminalField.NAME, "private-replacement-canary")
    controller.on(is_write, WRITE_FAILURES["rejected"][0]())
    with pytest.raises(Ac100Error) as exc:
        await api.async_set_terminal_field(PHONE, TerminalField.NAME, "another-private-name")
    text = caplog.text + str(exc.value)
    assert all(
        token not in text
        for token in (
            "private-",
            "controller.example",
            "example-password",
            "synthetic-session",
            mac_text(PHONE),
            PHONE,
        )
    )


@pytest.mark.parametrize("radios", [[], ["999"], ["82"], ["33", "66"], ["97", "98"]])
async def test_force_on_requires_a_fresh_eligible_saved_binding_target(radios):
    api, _, controller = make_api()
    controller.present(LAPTOP)
    controller.hosts[1]["bind_radio_id"] = radios
    with pytest.raises(Ac100TerminalError, match="not available"):
        await api.async_set_terminal_field(LAPTOP, TerminalField.FORCE_BINDING, True)
    assert controller.requests() == [HOSTS_READ, CLIENTS_READ, APS_READ, radio_list_request("3")]
    assert not controller.writes()


async def test_force_off_can_escape_a_saved_binding_whose_target_is_no_longer_eligible():
    api, _, controller = make_api()
    controller.present(LAPTOP)
    controller.hosts[1] |= {"force_bind_enable": "on", "bind_radio_id": ["999"]}
    await api.async_set_terminal_field(LAPTOP, TerminalField.FORCE_BINDING, False)
    assert controller.requests() == [
        HOSTS_READ,
        CLIENTS_READ,
        host_set(LAPTOP, LAPTOP_FORM | {"bind_radio_id": ["999"]}),
    ]


async def test_unreadable_fresh_service_eligibility_prevents_force_on():
    api, _, controller = make_api()
    controller.present(LAPTOP)
    controller.on(radio_list_of("3"), {"error_code": -40210})
    with pytest.raises(Ac100ProtocolError):
        await api.async_set_terminal_field(LAPTOP, TerminalField.FORCE_BINDING, True)
    assert not controller.writes()


@pytest.mark.parametrize("failure", WRITE_FAILURES)
async def test_existing_schedule_set_is_followed_by_the_host_form_and_reports_partial_failure(
    failure,
):
    api, _, controller = make_api()
    factory, cause = WRITE_FAILURES[failure]
    controller.on(lambda body: body["method"] == "set" and "host_management" in body, factory())
    with pytest.raises(Ac100TerminalPartialError) as exc:
        await api.async_set_terminal_field(LAPTOP, TerminalField.MANUAL_SATURDAY, True)
    assert exc.value.stage == "update"
    assert isinstance(exc.value.__cause__, cause)
    assert controller.requests() == [
        HOSTS_READ,
        time_object_request("5"),
        time_write("set", "5", MANUAL_ROW | {"weekday": "63"}),
        host_set(LAPTOP, LAPTOP_FORM),
    ]
    assert controller.time_objects["5"]["weekday"] == "63"
    assert controller.record(LAPTOP)[0]["time_obj_array"] == ["5"]
    assert len(controller.writes()) == 2
    assert not [url for url in controller.urls if url.endswith("/")]


async def test_schedule_host_form_is_preflighted_before_changing_the_object():
    api, _, controller = make_api()
    controller.hosts.append(controller.hosts[1] | {"down_limit": "1000"})
    with pytest.raises(Ac100TerminalError, match="different settings"):
        await api.async_set_terminal_field(LAPTOP, TerminalField.MANUAL_SATURDAY, True)
    assert controller.requests() == [HOSTS_READ, time_object_request("5")]
    assert not controller.writes()
    assert controller.time_objects["5"] == MANUAL_ROW


@pytest.mark.parametrize(
    ("field", "value"),
    [
        (TerminalField.BINDING_ENABLED, True),
        (TerminalField.BINDING_ENABLED, False),
        (TerminalField.FORCE_BINDING, True),
        (TerminalField.FORCE_BINDING, False),
        (TerminalField.BINDING_TARGET, f"{AP_E2}/all"),
    ],
)
async def test_binding_actions_require_fresh_client_membership_even_with_positive_saved_service(
    field, value
):
    api, _, controller = make_api()
    # The saved row claims an AP and positive service, but only client_list proves presence.
    controller.hosts[1]["force_bind_enable"] = "on" if value is False else "off"
    with pytest.raises(Ac100TerminalError, match="connected terminal"):
        await api.async_set_terminal_field(LAPTOP, field, value)
    assert controller.requests() == [HOSTS_READ, CLIENTS_READ]
    assert not controller.writes()


async def test_a_terminal_that_left_after_poll_cannot_be_bound_from_the_cached_presence():
    api, _, controller = make_api()
    controller.present(PHONE)
    snapshot = await api.async_get_snapshot()
    assert [client.mac for client in snapshot.clients] == [PHONE]
    assert configuration(snapshot, PHONE).binding_available
    controller.clients.clear()
    controller.bodies.clear()
    with pytest.raises(Ac100TerminalError, match="connected terminal"):
        await api.async_set_terminal_field(PHONE, TerminalField.BINDING_TARGET, f"{AP_E2}/all")
    assert controller.requests() == [HOSTS_READ, CLIENTS_READ]
    assert not controller.writes()


async def test_saved_binding_readability_does_not_declare_client_presence():
    snapshot, _ = await poll()
    assert snapshot.clients == ()
    assert configuration(snapshot, LAPTOP).binding_available
    assert configuration(snapshot, LAPTOP).bound_target == f"{AP_E2}/all"
    assert configuration(snapshot, TABLET).bound_ap == "Example AP 5"


@pytest.mark.parametrize(
    ("source", "request_count"),
    [
        ("first service", 1),
        ("second service", 2),
        ("all radios", 3),
        ("first object", 4),
        ("second object", 5),
    ],
)
async def test_first_dependent_transport_failure_stops_batch_and_keeps_only_fresh_core_and_hosts(
    source, request_count
):
    baseline_controller = FakeController()
    baseline_controller.present(PHONE)
    baseline, _ = await poll(baseline_controller)
    controller = FakeController()
    controller.present(PHONE)
    if source == "first service":
        match = radio_list_of("3")
    elif source == "second service":

        def match(body):
            return radio_list_of("0")(body) and "bind_status" in body["apmng_wserv"]["filter"][0]
    elif source == "all radios":

        def match(body):
            return (
                radio_list_of("0")(body) and "bind_status" not in body["apmng_wserv"]["filter"][0]
            )
    else:
        match = time_object_of("5" if source == "first object" else "6")
    controller.on(match, Response(error=aiohttp.ClientConnectionError("private-url")))
    snapshot, controller = await poll(controller)
    assert snapshot.controller == baseline.controller
    assert snapshot.aps == baseline.aps
    assert snapshot.clients == baseline.clients
    assert len(controller.requests()) == 5 + request_count
    assert match(controller.requests()[-1])
    assert not [url for url in controller.urls if url.endswith("/")]
    for item, healthy in zip(
        snapshot.terminals.configurations, baseline.terminals.configurations, strict=True
    ):
        expected = replace(
            healthy,
            binding_targets=(),
            binding_available=False,
            bound_target=None,
            bound_ap=None,
            bound_radio=None,
            schedule=None,
            allowed_time_mode="any" if healthy.allowed_time_mode == "any" else None,
            schedule_available=healthy.allowed_time_mode == "any",
        )
        assert item == expected


@pytest.mark.parametrize("source", ["radios", "time object"])
@pytest.mark.parametrize("login_error", ["credentials", "transport", "busy", "locked", "malformed"])
async def test_failed_dependent_reauthentication_stops_after_one_sign_in(source, login_error):
    api, _, controller = make_api()
    controller.on(
        radio_list_of("3") if source == "radios" else time_object_of("5"),
        Response(EXPIRED, status=401),
        times=1,
    )
    failures = {
        "credentials": (Response({"error_code": -40100}, status=401), Ac100AuthError),
        "transport": (Response(error=aiohttp.ClientConnectionError()), Ac100ConnectionError),
        "busy": (Response({"error_code": -57704}, status=401), Ac100SessionBusyError),
        "locked": (Response({"error_code": -40404}, status=401), Ac100AccountLockedError),
        "malformed": (Response(text="not JSON"), Ac100ProtocolError),
    }
    response, error = failures[login_error]
    controller.on(lambda body: "login" in body, response)
    with pytest.raises(error):
        await api.async_get_snapshot()
    requests = controller.requests()
    assert len([body for body in requests if "login" in body]) == 1
    assert len([body for body in controller.bodies if "user_management" in body]) == 1
    assert "login" in requests[-1]
    assert api._stok is None
    assert not [body for body in requests if body["method"] in {"set", "add", "delete"}]


@pytest.mark.parametrize("count", [None, "invalid", -1, 0, 2, 1.5, True])
async def test_unpaged_radio_answer_requires_an_exact_valid_count(count):
    controller = FakeController()
    answer = one_row_answer("apmng_wserv", "radio_bind_list", [radio_row(1, 1, "2.4GHz", "a")])(
        radio_list_request("3")
    )
    answer["apmng_wserv"]["count"]["radio_bind_list"] = count
    controller.on(radio_list_of("3"), answer)
    snapshot, _ = await poll(controller)
    assert not configuration(snapshot, PHONE).binding_available
    assert configuration(snapshot, PHONE).binding_targets == ()
    assert configuration(snapshot, NAMELESS).binding_available


@pytest.mark.parametrize("count", [601, 1368, 1369])
async def test_raw_radio_guard_includes_templates_up_to_the_source_capacity(count):
    controller = FakeController()
    rows = [radio_row(1, 1, "2.4GHz", "a")] + [{"ap_type": "template"}] * (count - 1)
    controller.on(radio_list_of("3"), one_row_answer("apmng_wserv", "radio_bind_list", rows))
    snapshot, controller = await poll(controller)
    item = configuration(snapshot, PHONE)
    if count <= 1368:
        assert item.binding_available
        assert [target.token for target in item.binding_targets] == [f"{AP_E0}/1"]
    else:
        assert not item.binding_available
        assert item.binding_targets == ()
    assert len([body for body in controller.requests() if radio_list_of("3")(body)]) == 1
