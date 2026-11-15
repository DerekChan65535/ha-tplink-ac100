"""Credential-free monitoring and configuration reads, using synthetic controller data."""

import asyncio
import json
import logging
from dataclasses import FrozenInstanceError, asdict, replace
from unittest.mock import MagicMock
from urllib.parse import quote

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
)
from custom_components.tplink_ac100.models import Controller, Snapshot
from custom_components.tplink_ac100.monitoring import (
    CONFIGURATION_FIELDS,
    CONFIGURATION_SOURCES,
    ApFirmware,
    ConfigurationState,
    FirmwareAvailability,
    InterfaceTraffic,
    MonitoringState,
    PortStatus,
)

FIELDS = {item.key: item for item in CONFIGURATION_FIELDS}
CANARIES = (
    "CANARY_KEY",
    "CANARY_PASSWORD",
    "CANARY_TOKEN",
    "CANARY_RADIUS_PWD",
    "CANARY_BACKHAUL_KEY",
    "CANARY_SITE_ID",
    "CANARY_SITE_NAME",
    "CANARY_DOWNLOAD_URL",
    "CANARY_RELEASE_LOG",
    "CANARY_AP_NAME",
    "CANARY_PROXY_URI",
    "CANARY_TP_LINK_ID",
    "198.51.100.77",
)
SECRET_FIELDS = {
    "key": "CANARY_KEY",
    "password": "CANARY_PASSWORD",
    "token": "CANARY_TOKEN",
    "radius_pwd": "CANARY_RADIUS_PWD",
    "backhaul_key": "CANARY_BACKHAUL_KEY",
    "sub_proxy_uri": "CANARY_PROXY_URI",
    "username": "CANARY_TP_LINK_ID",
    "remote_ip": "198.51.100.77",
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


def ap_row(number, **extra):
    return {
        "mac": f"02-00-00-00-00-{number:02X}",
        "hw_version": "2.0",
        "model_id": "1000000",
        "sw_version": f"1.0.{number} Build 20250101 Rel.00001",
        "model": "Example AP model",
        "status": "0",
        "name": "CANARY_AP_NAME",
        "upgrade_type": "0",
        "new_sw_version": "---",
        **SECRET_FIELDS,
        **extra,
    }


def port_row(number, switch="switch0", state="connected", **extra):
    up = state == "connected"
    return {
        ".name": f"port_{number}",
        "switch_dev": switch,
        "phy_index": str(number),
        "port_name": str(number),
        "port_state": state,
        "link_speed": "1000M" if up else "---",
        "port_speed": "1000",
        "link_duplex": "Full" if up else "---",
        "enable": "on",
        "cfg_speed": "auto",
        "key": "CANARY_KEY",
        **extra,
    }


def interface_row(name="lan", **extra):
    return {
        "interface": name,
        "tx_bps": "5",
        "rx_bps": "4",
        "tx_pps": "17",
        "rx_pps": "29",
        "tx_bytes": "1000",
        "rx_bytes": "2000",
        "tx_pkts": "54830",
        "rx_pkts": "202236",
        "token": "CANARY_TOKEN",
        **extra,
    }


class FakeController:
    """A GET-only SLP double answering section and table queries from plain dictionaries."""

    def __init__(self):
        self.sections = {
            ("system", "cfg_status"): {"status": "Not Saved", "dft_pwd_status": "Modify"},
            ("apmng_upgrade", "upgrade_status"): {"status": "0"},
            ("apmng_set_status", "status"): {"ap_full_flag": "0", "unknown_ap_flag": "1"},
            ("apmng_rrm", "rrm_status"): {"status": "3"},
            ("cloud_config", "conf_mngt"): {"mngt_switch": "0", "read_only": "1"},
            (
                "cloud_config",
                "bind_info",
            ): {"bind_status": "1", "site_id": "CANARY_SITE_ID", "site_name": "CANARY_SITE_NAME"},
            ("administrator", "auth_status"): {"status": "1", "change": "0"},
            ("cloud_config", "new_firmware"): {"fw_new_notify": "1", "not_show": "0"},
            ("cloud_config", "upgrade_info"): {
                "version": "1.0.7 Build 260208 Rel.50128n",
                "release_date": "2026-04-29",
                "download_url": "http://CANARY_DOWNLOAD_URL.example/fw.bin",
                "release_log": "CANARY_RELEASE_LOG",
                "release_log_url": "undefined yet",
            },
        }
        self.tables = {
            ("apmng_upgrade", "ap_list"): [
                ap_row(1),
                ap_row(2, new_sw_version="1.0.9 Build 20260101 Rel.00002"),
            ],
            ("port", "port"): [
                port_row(1, "switch1"),
                port_row(2),
                port_row(3, state="disconnected"),
            ],
            ("system", "ifstat_list"): [interface_row()],
        }
        self.sections.update(configuration_sections())
        self.counts = {}
        self.rejected = set()
        self.inject = {}
        self.gate_first = False
        self.gate = None
        self.gate_ready = asyncio.Event()
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
        if self.gate_first and self.gate is None:
            self.gate = response = GatedResponse(response)
            self.gate_ready.set()
            return response
        return Response(response)

    def answer(self, body):
        modules = [key for key in body if key != "method"]
        if body["method"] != "get":
            return {"error_code": 0}
        if self.rejected & set(modules):
            return {"error_code": -40210}
        result = {"error_code": 0}
        for module in modules:
            query = body[module]
            if "name" in query:
                names = [query["name"]] if isinstance(query["name"], str) else query["name"]
                result[module] = {
                    name: wire(self.sections[module, name])
                    for name in names
                    if (module, name) in self.sections
                }
            else:
                table = query["table"]
                rows = self.tables[module, table]
                start = query.get("para", {}).get("start", 0)
                end = query.get("para", {}).get("end", len(rows) - 1)
                page = rows[start : end + 1]
                result[module] = {
                    table: [{f"{table}_{start + i}": wire(row)} for i, row in enumerate(page)],
                    "count": {table: self.counts.get((module, table), len(rows))},
                }
        return result


def make_api(controller=None):
    controller = controller or FakeController()
    session = MagicMock(spec=aiohttp.ClientSession)
    session.post.side_effect = controller
    api = Ac100Api(session, "controller.example", "admin", "example-password")
    api._stok = "synthetic-session"
    return api, session, controller


def sections_of(controller):
    """The (module, section) names the controller was asked for, in request order."""
    return [
        (module, name)
        for body in controller.bodies
        for module, query in body.items()
        if module != "method" and "name" in query
        for name in query["name"]
    ]


async def test_monitoring_reads_every_source_get_only_over_the_one_session():
    api, session, controller = make_api()
    state = await api.async_get_monitoring()
    assert state.values == {
        "configuration_not_saved": True,
        "unknown_access_points": True,
        "access_point_limit_reached": False,
        "ap_upgrade_in_progress": False,
        "radio_optimization_in_progress": True,
        "controller_writable": True,
        "cloud_bound": True,
        "cloud_management_enabled": False,
    }
    assert state.available_sections == {
        "system.cfg_status",
        "apmng_set_status.status",
        "apmng_upgrade.upgrade_status",
        "apmng_rrm.rrm_status",
        "administrator.auth_status",
        "cloud_config.bind_info",
        "cloud_config.conf_mngt",
        "cloud_config.upgrade_info",
        "apmng_upgrade.ap_list",
        "port.port",
        "system.ifstat_list",
    }
    assert state.firmware == FirmwareAvailability("1.0.7 Build 260208 Rel.50128n")
    assert state.ap_firmware == (
        ApFirmware("02:00:00:00:00:01", "1.0.1 Build 20250101 Rel.00001", "2.0", None),
        ApFirmware(
            "02:00:00:00:00:02",
            "1.0.2 Build 20250101 Rel.00001",
            "2.0",
            "1.0.9 Build 20260101 Rel.00002",
        ),
    )
    assert state.ports == (
        PortStatus("switch1_port_1", "1", True, 1000),
        PortStatus("switch0_port_2", "2", True, 1000),
        PortStatus("switch0_port_3", "3", False),
    )
    assert state.interfaces == (InterfaceTraffic("lan", 5.0, 4.0, 17.0, 29.0, 1000, 2000),)
    assert all(body["method"] == "get" for body in controller.bodies)
    assert set(controller.urls) == {"http://controller.example/stok=synthetic-session/ds"}
    assert not {"apmng_mesh", "ha_mngt", "apmng_set"} & {
        module for body in controller.bodies for module in body
    }
    assert controller.bodies[-3:] == [
        {
            "method": "get",
            "apmng_upgrade": {
                "table": "ap_list",
                "filter": [{"group_id": ["-1"], "ap_role": ["cap", "normal"]}],
                "para": {"start": 0, "end": 9},
            },
        },
        {"method": "get", "port": {"table": "port"}},
        {"method": "get", "system": {"table": "ifstat_list", "para": {"start": 0, "end": 9}}},
    ]
    session.close.assert_not_called()


@pytest.mark.parametrize(
    ("section", "field", "raw", "key", "expected"),
    [
        *(
            ("system.cfg_status", "status", raw, "configuration_not_saved", expected)
            for raw, expected in (
                ("Saved", False),
                ("Saving", False),
                ("Not Saved", True),
                ("Not_Saved", None),
                ("saved", None),
                ("", None),
                ([], None),
            )
        ),
        *(
            ("apmng_set_status.status", field, raw, key, expected)
            for field, key in (
                ("unknown_ap_flag", "unknown_access_points"),
                ("ap_full_flag", "access_point_limit_reached"),
            )
            for raw, expected in (("0", False), ("1", True), ("2", None), (1, None), (None, None))
        ),
        *(
            ("apmng_upgrade.upgrade_status", "status", raw, "ap_upgrade_in_progress", expected)
            for raw, expected in (("0", False), ("3", True), ("5", False), ("6", None), ("x", None))
        ),
        *(
            ("apmng_rrm.rrm_status", "status", raw, "radio_optimization_in_progress", expected)
            for raw, expected in (
                ("0", False),
                ("7", False),
                *((str(code), True) for code in range(1, 7)),
                ("8", None),
                ("", None),
                ({}, None),
            )
        ),
        *(
            ("administrator.auth_status", "status", raw, "controller_writable", expected)
            for raw, expected in (("1", True), ("0", False), ("2", None), ("rw", None))
        ),
        *(
            ("cloud_config.bind_info", "bind_status", raw, "cloud_bound", expected)
            for raw, expected in (("1", True), ("0", False), ("2", None))
        ),
        *(
            ("cloud_config.conf_mngt", "mngt_switch", raw, "cloud_management_enabled", expected)
            for raw, expected in (("1", True), ("0", False), ("on", None))
        ),
    ],
)
async def test_status_enums_are_strict_and_unknown_is_none_not_false(
    section, field, raw, key, expected
):
    api, _, controller = make_api()
    module, name = section.split(".")
    controller.sections[module, name] = {field: raw}
    state = await api.async_get_monitoring()
    assert section in state.available_sections
    assert state.values[key] is expected


async def test_missing_or_non_object_sections_are_unavailable_not_healthy():
    api, _, controller = make_api()
    del controller.sections["system", "cfg_status"]
    controller.sections["apmng_rrm", "rrm_status"] = []
    state = await api.async_get_monitoring()
    assert {"system.cfg_status", "apmng_rrm.rrm_status"}.isdisjoint(state.available_sections)
    assert "configuration_not_saved" not in state.values
    assert "radio_optimization_in_progress" not in state.values
    assert state.values["controller_writable"] is True


@pytest.mark.parametrize("reader", ["async_get_monitoring", "async_get_configuration"])
async def test_rejected_module_is_retried_alone_and_does_not_hide_the_others(reader):
    api, _, controller = make_api()
    controller.sections["apmng_audit", "settings"] = {"enable": "on"}
    controller.sections["apmng_band_navigation", "settings"] = {"enable": "on"}
    controller.rejected = {"apmng_rrm", "apmng_audit"}
    state = await getattr(api, reader)()
    first = controller.bodies[0]
    modules = set(first) - {"method"}
    assert len(modules) > 2
    retries = controller.bodies[1 : len(modules) + 1]
    assert all(len(body) == 2 for body in retries)
    assert {module for body in retries for module in body} - {"method"} == modules
    if reader == "async_get_monitoring":
        assert "apmng_rrm.rrm_status" not in state.available_sections
        assert "radio_optimization_in_progress" not in state.values
        assert state.values["controller_writable"] is True
        assert "port.port" in state.available_sections
    else:
        assert "apmng_audit.settings" not in state.available_sections
        assert "security_audit_enabled" not in state.values
        assert state.values["band_navigation_enabled"] is True


@pytest.mark.parametrize(
    ("notify", "version", "expected"),
    [
        ("1", "1.0.7 Build 260208 Rel.50128n", "1.0.7 Build 260208 Rel.50128n"),
        ("0", "1.0.7 Build 260208 Rel.50128n", None),
        *(
            ("1", placeholder, None)
            for placeholder in ("", "---", "undefined", "Undefined yet", "  ")
        ),
        ("1", "x" * 65, None),
    ],
)
async def test_firmware_availability_follows_the_cloud_announcement(notify, version, expected):
    api, _, controller = make_api()
    controller.sections["cloud_config", "new_firmware"] = {"fw_new_notify": notify}
    controller.sections["cloud_config", "upgrade_info"]["version"] = version
    state = await api.async_get_monitoring()
    assert "cloud_config.upgrade_info" in state.available_sections
    assert state.firmware == FirmwareAvailability(expected)


@pytest.mark.parametrize("broken", ["missing_notice", "missing_info", "unknown_flag"])
async def test_firmware_is_unavailable_without_a_usable_announcement(broken):
    api, _, controller = make_api()
    if broken == "missing_notice":
        del controller.sections["cloud_config", "new_firmware"]
    elif broken == "missing_info":
        del controller.sections["cloud_config", "upgrade_info"]
    else:
        controller.sections["cloud_config", "new_firmware"] = {"fw_new_notify": "2"}
    state = await api.async_get_monitoring()
    assert state.firmware is None
    assert "cloud_config.upgrade_info" not in state.available_sections
    assert "cloud_config.conf_mngt" in state.available_sections


async def test_ap_firmware_reads_every_page_and_maps_only_versions():
    api, _, controller = make_api()
    controller.tables["apmng_upgrade", "ap_list"] = [
        ap_row(number, sw_version="---", hw_version="") for number in range(1, 13)
    ]
    state = await api.async_get_monitoring()
    assert [item.mac for item in state.ap_firmware] == [
        f"02:00:00:00:00:{number:02x}" for number in range(1, 13)
    ]
    assert all(item.firmware_version is item.hardware_version is None for item in state.ap_firmware)
    pages = [
        body["apmng_upgrade"]["para"]
        for body in controller.bodies
        if "table" in body.get("apmng_upgrade", {})
    ]
    assert pages == [{"start": 0, "end": 9}, {"start": 10, "end": 19}]


@pytest.mark.parametrize(
    "change",
    ["huge_count", "short_page", "bad_mac", "nested_row", "duplicate_ports", "port_without_id"],
)
async def test_malformed_optional_tables_become_unavailable_without_hiding_the_rest(change):
    api, _, controller = make_api()
    if change == "huge_count":
        controller.counts["apmng_upgrade", "ap_list"] = 10**9
    elif change == "short_page":
        controller.counts["apmng_upgrade", "ap_list"] = 12
    elif change == "bad_mac":
        controller.tables["apmng_upgrade", "ap_list"] = [ap_row(1, mac="not-a-mac")]
    elif change == "nested_row":
        controller.tables["apmng_upgrade", "ap_list"] = [ap_row(1), "unexpected"]
    elif change == "duplicate_ports":
        controller.tables["port", "port"] = [port_row(1), port_row(1)]
    else:
        controller.tables["port", "port"] = [{**port_row(1), ".name": None}]
    state = await api.async_get_monitoring()
    broken = "port.port" if "port" in change else "apmng_upgrade.ap_list"
    assert broken not in state.available_sections
    assert not (state.ports if broken == "port.port" else state.ap_firmware)
    assert len(state.available_sections) == 10
    assert state.interfaces
    table_requests = [body for body in controller.bodies if "apmng_upgrade" in body]
    assert len(table_requests) <= 2


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({}, PortStatus("switch0_port_1", "1", True, 1000)),
        (
            {"port_state": "disconnected", "link_speed": "---"},
            PortStatus("switch0_port_1", "1", False),
        ),
        (
            {"port_state": "disconnected", "link_speed": "1000M"},
            PortStatus("switch0_port_1", "1", False),
        ),
        ({"port_state": "disable"}, PortStatus("switch0_port_1", "1", False)),
        ({"port_state": "unexpected"}, PortStatus("switch0_port_1", "1", None)),
        ({"link_speed": "fast"}, PortStatus("switch0_port_1", "1", True)),
        ({"link_speed": "100M"}, PortStatus("switch0_port_1", "1", True, 100)),
    ],
)
async def test_port_link_speed_is_the_active_link_not_the_configured_speed(overrides, expected):
    api, _, controller = make_api()
    controller.tables["port", "port"] = [port_row(1, **overrides)]
    state = await api.async_get_monitoring()
    assert state.ports == (expected,)


async def test_port_identity_does_not_depend_on_row_order_or_display_name():
    api, _, controller = make_api()
    first = await api.async_get_monitoring()
    controller.tables["port", "port"].reverse()
    controller.tables["port", "port"][0]["port_name"] = "Renamed"
    second = await api.async_get_monitoring()
    by_id = {port.port_id: port for port in first.ports}
    assert {port.port_id for port in second.ports} == set(by_id)
    assert next(p for p in second.ports if p.port_id == "switch0_port_3").name == "Renamed"
    assert by_id["switch1_port_1"].connected is True


@pytest.mark.parametrize(
    ("row", "expected"),
    [
        (
            interface_row(),
            InterfaceTraffic("lan", 5.0, 4.0, 17.0, 29.0, 1000, 2000),
        ),
        (
            interface_row("vlan10", tx_bps="2.5", rx_bps="-1", tx_pps="x", rx_pps=None),
            InterfaceTraffic("vlan10", 2.5, None, None, None, 1000, 2000),
        ),
        (
            interface_row(tx_bytes=str(2**62), rx_bytes="-5"),
            InterfaceTraffic("lan", 5.0, 4.0, 17.0, 29.0, 2**62, None),
        ),
        (
            interface_row(tx_bytes=[], rx_bytes="12.5"),
            InterfaceTraffic("lan", 5.0, 4.0, 17.0, 29.0, None, None),
        ),
    ],
)
async def test_interface_rates_stay_in_kilobytes_per_second_and_unknown_is_none(row, expected):
    api, _, controller = make_api()
    controller.tables["system", "ifstat_list"] = [row]
    state = await api.async_get_monitoring()
    assert state.interfaces == (expected,)


@pytest.mark.parametrize("change", ["count_too_large", "no_name"])
async def test_interface_table_that_disagrees_with_its_count_is_unavailable(change):
    api, _, controller = make_api()
    if change == "count_too_large":
        controller.counts["system", "ifstat_list"] = 2
    else:
        controller.tables["system", "ifstat_list"] = [interface_row(None)]
    state = await api.async_get_monitoring()
    assert "system.ifstat_list" not in state.available_sections
    assert state.interfaces == ()
    assert "port.port" in state.available_sections


async def test_garbage_everywhere_yields_an_empty_state_instead_of_an_error():
    api, _, controller = make_api()
    garbage = Response(text="not json")
    controller.inject = {index: garbage for index in range(20)}
    assert await api.async_get_monitoring() == MonitoringState()
    controller.bodies.clear()
    controller.inject = {index: garbage for index in range(20)}
    assert await api.async_get_configuration() == ConfigurationState()


@pytest.mark.parametrize(
    ("reader", "position"),
    [("async_get_monitoring", 0), ("async_get_monitoring", 3), ("async_get_configuration", 0)],
)
@pytest.mark.parametrize(
    ("response", "error"),
    [
        (Response(error=aiohttp.ClientConnectionError("private-url")), Ac100ConnectionError),
        (Response({"error_code": -40100}, status=401), Ac100AuthError),
        (Response({"error_code": -57704}, status=401), Ac100SessionBusyError),
        (Response({"error_code": -40404}, status=401), Ac100AccountLockedError),
    ],
)
async def test_connection_and_authentication_failures_propagate_without_more_requests(
    reader, position, response, error
):
    api, _, controller = make_api()
    controller.inject = {position: response}
    with pytest.raises(error) as exc:
        await getattr(api, reader)()
    assert "private-url" not in str(exc.value)
    assert len(controller.bodies) == position + 1


@pytest.mark.parametrize("reader", ["async_get_monitoring", "async_get_configuration"])
async def test_failed_sign_in_is_not_mistaken_for_an_unavailable_source(reader):
    expired = Response({"error_code": -40401, "data": {"code": -40401}}, status=401)
    api, _, controller = make_api()
    controller.inject = {0: expired, 1: Response(text="not json")}
    with pytest.raises(Ac100ProtocolError):
        await getattr(api, reader)()
    assert len(controller.bodies) == 2


@pytest.mark.parametrize("reader", ["async_get_monitoring", "async_get_configuration"])
async def test_session_that_stays_expired_after_one_renewal_propagates(reader):
    expired = Response({"error_code": -40401, "data": {"code": -40401}}, status=401)
    api, _, controller = make_api()
    controller.inject = {0: expired, 3: expired}
    with pytest.raises(Ac100SessionExpiredError):
        await getattr(api, reader)()
    assert len(controller.bodies) == 4


@pytest.mark.parametrize("reader", ["async_get_monitoring", "async_get_configuration"])
async def test_expired_session_is_renewed_once_and_the_read_continues(reader):
    api, _, controller = make_api()
    controller.inject = {0: Response({"error_code": -40401, "data": {"code": -40401}}, status=401)}
    state = await getattr(api, reader)()
    assert state.available_sections
    methods = [body["method"] for body in controller.bodies]
    assert methods.count("do") == 2
    assert controller.bodies[0] == controller.bodies[3]
    assert controller.urls[4:] == ["http://controller.example/stok=renewed-session/ds"] * (
        len(controller.urls) - 4
    )


async def test_reads_hold_the_shared_lock_and_never_interleave():
    expected = []
    for reader in ("async_get_monitoring", "async_get_configuration"):
        api, _, controller = make_api()
        await getattr(api, reader)()
        expected += controller.bodies
    api, _, controller = make_api()
    controller.gate_first = True
    first = asyncio.create_task(api.async_get_monitoring())
    await controller.gate_ready.wait()
    await controller.gate.started.wait()
    second = asyncio.create_task(api.async_get_configuration())
    for _ in range(10):
        await asyncio.sleep(0)
    assert len(controller.bodies) == 1
    controller.gate.release.set()
    await asyncio.gather(first, second)
    assert controller.bodies == expected


@pytest.mark.parametrize("reader", ["async_get_monitoring", "async_get_configuration"])
async def test_retired_client_does_not_read(reader):
    api, session, controller = make_api()
    await api.async_logout()
    controller.bodies.clear()
    with pytest.raises(Ac100Error, match="client has been closed"):
        await getattr(api, reader)()
    assert controller.bodies == []
    session.close.assert_not_called()


def configuration_sections(**raw):
    """Sections holding every catalogued field with the given raw values, plus secret fields."""
    sections = {}
    for item in CONFIGURATION_FIELDS:
        section = sections.setdefault((item.module, item.section), dict(SECRET_FIELDS))
        section[item.field] = raw.get(item.key, "1")
    return sections


@pytest.mark.parametrize(
    ("key", "raw", "expected"),
    [
        *(
            ("band_navigation_enabled", raw, value)
            for raw, value in (
                ("on", True),
                ("off", False),
                ("1", None),
                ("ON", None),
                ("", None),
                ([], None),
            )
        ),
        *(
            ("band_navigation_5g_threshold", raw, value)
            for raw, value in (
                ("20", 20),
                ("2.5", 2.5),
                ("abc", None),
                ("", None),
                ("nan", None),
                ("inf", None),
                (True, None),
                (None, None),
                ({}, None),
            )
        ),
        ("load_balance_rssi_threshold", "-75", -75),
        ("load_balance_mode", "session", "session"),
        *(
            ("rrm_band", raw, value)
            for raw, value in (
                ("0", "both"),
                ("1", "2_4ghz"),
                ("2", "5ghz"),
                ("3", None),
            )
        ),
        ("rrm_5g_bandwidth", "160M", "160mhz"),
        ("rrm_5g_bandwidth", "160", None),
        *(
            ("rrm_schedule_day", raw, value)
            for raw, value in (
                ("eve", "every_day"),
                ("sat", "saturday"),
                ("Sat", None),
            )
        ),
        ("rrm_schedule_time", "00:00:00", "00:00:00"),
        ("rrm_schedule_time", "x" * 65, None),
        ("roaming_2g_rate_threshold", "20", 20),
        *(
            ("igmp_unknown_multicast", raw, value)
            for raw, value in (
                ("1", "forward"),
                ("0", "drop"),
                ("2", None),
            )
        ),
        ("broadcast_storm_rate", "1024", 1024),
        ("broadcast_storm_rate_type", "user_define", "user_defined"),
        *(
            ("time_zone_offset", raw, value)
            for raw, value in (
                ("720", 0),
                ("1200", 480),
                ("1065", 345),
                ("450", -270),
                ("bad", None),
            )
        ),
        *(
            ("time_source", raw, value)
            for raw, value in (
                ("auto", "network"),
                ("manual", "manual"),
                ("cloud", "cloud"),
                ("ntp", None),
            )
        ),
        ("ntp_primary_server", "192.0.2.123", "192.0.2.123"),
        ("ntp_secondary_server", "", None),
        ("device_alias", "Example Controller", "Example Controller"),
        ("device_alias", "bad\nalias", None),
        ("web_session_timeout", "6", 6),
        ("login_max_attempts", "0", 0),
        ("ap_telnet_enabled", "off", False),
        ("wireless_report_protocol", "UDP", "udp"),
        ("wireless_report_protocol", "udp", None),
    ],
)
async def test_configuration_values_follow_the_catalog_kinds(key, raw, expected):
    api, _, controller = make_api()
    controller.sections = {
        **controller.sections,
        **configuration_sections(**{key: raw}),
    }
    state = await api.async_get_configuration()
    item = FIELDS[key]
    assert item.source in state.available_sections
    assert state.values[key] == expected
    assert type(state.values[key]) is type(expected)


async def test_configuration_reads_only_catalogued_sections_by_get_and_nothing_else_leaves():
    api, _, controller = make_api()
    state = await api.async_get_configuration()
    assert set(state.values) == set(FIELDS)
    assert state.available_sections == {item.source for item in CONFIGURATION_FIELDS}
    assert all(body["method"] == "get" for body in controller.bodies)
    assert all(
        "table" not in query
        for body in controller.bodies
        for query in body.values()
        if isinstance(query, dict)
    )
    requested = {(module, name) for module, name in sections_of(controller)}
    assert requested == {
        (module, name) for module, names in CONFIGURATION_SOURCES.items() for name in names
    }
    assert {module for module, _ in requested}.isdisjoint(
        {
            "apmng_mesh",
            "authentication",
            "user_management",
            "ha_mngt",
            "ap_capture_packet",
            "network",
        }
    )
    assert ("apmng_wserv", "wlan_serv") not in requested
    text = repr(state) + str(asdict(state))
    assert not [canary for canary in CANARIES if canary in text]


async def test_monitoring_state_never_carries_names_urls_or_secret_fields():
    api, _, controller = make_api()
    state = await api.async_get_monitoring()
    text = repr(state) + str(asdict(state)) + repr(state.values)
    assert not [canary for canary in CANARIES if canary in text]
    assert "download" not in text.lower()
    assert "Example AP" not in text


def test_catalog_is_unique_typed_and_free_of_secret_bearing_modules():
    assert len(FIELDS) == len(CONFIGURATION_FIELDS)
    assert FIELDS["igmp_unknown_multicast"].source == "apmng_wserv.igmp_settings"
    assert FIELDS["rrm_band"].options == ("both", "2_4ghz", "5ghz")
    for item in CONFIGURATION_FIELDS:
        assert item.key.replace("_", "").isalnum()
        assert item.key == item.key.lower()
        if item.kind == "bool":
            assert {state for _, state in item.mapping} == {True, False}
        elif item.kind == "enum":
            assert item.mapping
            assert all(isinstance(state, str) and state for _, state in item.mapping)
            assert item.options
        else:
            assert not item.mapping
        assert item.unit is None or item.kind == "number"
        assert item.offset == 0 or item.kind == "number"
    forbidden = ("pwd", "pass", "key", "token", "secret", "user", "uri", "url", "ssid")
    assert not [
        item.key for item in CONFIGURATION_FIELDS if any(w in item.field for w in forbidden)
    ]
    assert {item.module for item in CONFIGURATION_FIELDS}.isdisjoint(
        {"apmng_mesh", "authentication", "user_management", "wlan_serv"}
    )
    assert ("apmng_set", "ap_list") not in {(i.module, i.section) for i in CONFIGURATION_FIELDS}


@pytest.mark.parametrize("weird", [None, [], {}, True, "x" * 1000, "\x00", "\u2028"])
async def test_configuration_never_raises_on_unexpected_value_forms(weird):
    api, _, controller = make_api()
    controller.sections.update(
        configuration_sections(**{item.key: weird for item in CONFIGURATION_FIELDS})
    )
    state = await api.async_get_configuration()
    assert set(state.values) == set(FIELDS)
    assert all(value is None for value in state.values.values())


async def test_debug_logs_contain_counts_only(caplog):
    caplog.set_level(logging.DEBUG, logger="custom_components.tplink_ac100.api")
    api, _, _ = make_api()
    await api.async_get_monitoring()
    await api.async_get_configuration()
    records = [r for r in caplog.records if r.name == "custom_components.tplink_ac100.api"]
    assert len(records) == 2
    assert all(isinstance(arg, int) for record in records for arg in record.args)
    text = caplog.text
    assert not [canary for canary in CANARIES if canary in text]
    assert "stok" not in text and "controller.example" not in text


async def test_reads_keep_no_state_on_the_client():
    api, _, _ = make_api()
    before = {key: value for key, value in vars(api).items() if key != "_session"}
    await api.async_get_monitoring()
    await api.async_get_configuration()
    assert {key: value for key, value in vars(api).items() if key != "_session"} == before


def test_states_and_snapshot_defaults_are_empty_immutable_and_independent():
    assert MonitoringState() == MonitoringState({}, frozenset(), None, (), (), ())
    assert MonitoringState().values is not MonitoringState().values
    assert ConfigurationState().values is not ConfigurationState().values
    with pytest.raises(FrozenInstanceError):
        MonitoringState().firmware = FirmwareAvailability()
    snapshot = Snapshot(
        Controller("02:00:00:00:00:01", "TL-AC100", "5.0", "1.0.5", {}, 1.0, 1), (), ()
    )
    assert snapshot.monitoring is None and snapshot.configuration is None
    state = MonitoringState(available_sections=frozenset({"port.port"}))
    refreshed = replace(snapshot, monitoring=state, configuration=ConfigurationState())
    assert refreshed.monitoring is state
    assert replace(state, available_sections=frozenset()).available_sections == frozenset()
