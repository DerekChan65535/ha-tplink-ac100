"""Asynchronous client for the AC100's local SLP API.

Management calls are explicit. Polling never changes controller configuration.
"""

import asyncio
import hashlib
import json
import logging
import math
import re
import unicodedata
from asyncio import sleep
from collections.abc import Awaitable, Iterable, Mapping
from dataclasses import replace
from datetime import datetime
from time import monotonic
from typing import Any
from urllib.parse import quote, unquote, urlsplit

import aiohttp

from .management import (
    ALIAS_RULE,
    SAVE_COMPLETE,
    SAVE_POLL_INTERVAL,
    SAVE_TIMEOUT,
    alias_is_valid,
    encode_setting,
    form_request,
)
from .models import AccessPoint, ApRadio, Client, ClientRadio, Controller, Snapshot
from .monitoring import (
    CONFIGURATION_FIELDS,
    CONFIGURATION_SOURCES,
    RADIO_FIELDS,
    RADIO_MODULE,
    RADIO_SOURCE,
    RADIO_TABLE,
    WIRELESS_SERVICE_FLAGS,
    WIRELESS_SERVICE_SECURITY,
    ApFirmware,
    ConfigField,
    ConfigurationState,
    FirmwareAvailability,
    InterfaceTraffic,
    MonitoringState,
    PortStatus,
    RadioField,
    RadioSettings,
    Scalar,
    WirelessService,
)
from .terminal import (
    SCHEDULE_FIELDS,
    TerminalBindingTarget,
    TerminalField,
    TerminalState,
    validate_terminal_value,
)
from .terminal_api import (
    BINDING_IS_OFF,
    BINDING_NOT_EDITABLE,
    BINDING_TARGET_GONE,
    BINDING_TARGET_NEEDED,
    NO_RECORD,
    NOT_CONNECTED,
    PARTIAL_CLEAR,
    PARTIAL_LINK,
    PARTIAL_UNLINK,
    PARTIAL_UPDATE,
    RADIO_BIND_MODULE,
    RADIO_BIND_TABLE,
    RATE_LIMIT_IS_OFF,
    SCHEDULE_NOT_OWNED,
    SCHEDULE_UNREADABLE,
    STILL_CONNECTED,
    HostRecord,
    RadioBinding,
    TerminalRefusal,
    ap_directory,
    binding_targets,
    build_configuration,
    host_delete_request,
    host_disconnect_request,
    host_name_request,
    host_update_para,
    host_update_request,
    merge_host_rows,
    new_time_object_id,
    parse_radio_bind_rows,
    radio_bind_filter,
    saved_radio_ids,
    target_for_radios,
    time_object_add_request,
    time_object_delete_request,
    time_object_ids,
    time_object_is_exclusive,
    time_object_read_request,
    time_object_set_request,
)
from .terminal_schedule import (
    TerminalSchedule,
    change_terminal_schedule,
    is_terminal_time_object,
    parse_terminal_schedule,
    terminal_time_object_para,
)

_LOGGER = logging.getLogger(__name__)

_PAGE_SIZE = 10
# Optional tables are bounded by the AC100's limits (100 APs, 60 interfaces, 5 ports): a larger
# count is treated as invalid instead of paged through.
_MAX_OPTIONAL_ROWS = 256
# One row per AP radio: at most 100 APs with at most 6 radios each (max_radio_per_ap).
# Source capability max_radio_count includes physical radios and templates.
_MAX_RADIO_ROWS = 1368
_REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=15)
_LEGACY_SALT = "RDpbLfCPsJZ7fiv"
_LEGACY_ALPHABET = (
    "yLwVl0zKqws7LgKPRQ84Mdt708T1qQ3Ha7xv3H7NyU84p21BriUWBU43odz3iP4rBL3cD02KZciXTysVXiV8ngg6"
    "vL48rPJyAUw0HurW20xqxv9aYb4M9wK1Ae0wlro510qXeU07kV57fQMc8L6aLgMLwygtc0F10a0Dg70TOoouyFhd"
    "ysuRMO51yY5ZlOZZLEal1h0t9YQW0Ko7oBwmCAHoic4HYbUyVeU3sfQ1xtXcPcf1aT303wAQhv66qzW"
)


class Ac100Error(Exception):
    """Base AC100 error; messages never contain response bodies or credentials."""


class Ac100AuthError(Ac100Error):
    """Authentication or access to the controller was rejected."""


class Ac100SessionBusyError(Ac100AuthError):
    """Another administrator holds the controller's read-write session."""


class Ac100SessionExpiredError(Ac100Error):
    """The controller rejected the session token as expired or invalid.

    This is not a credentials failure: a fresh login may succeed. It does not prove that a
    rejected management command had no side effect, so such commands are never replayed.
    """


class Ac100AccountLockedError(Ac100Error):
    """The controller temporarily locked the account after too many failed sign-ins."""


class Ac100ConnectionError(Ac100Error):
    """The controller could not be reached."""


class Ac100ProtocolError(Ac100Error):
    """The controller returned an unsupported or malformed response."""


class Ac100UnsafeLedError(Ac100Error):
    """LED control could not be confirmed independent of the AP's WiFi."""


class Ac100SaveTimeoutError(Ac100Error):
    """The controller did not report in time that a configuration save had finished.

    The save request was accepted, so the outcome is unknown. It is never sent again.
    """


class Ac100TerminalError(Ac100Error):
    """A terminal action was refused before anything was sent to the controller.

    The message is a fixed sentence without any name, address or identifier, so it can be shown.
    """


class Ac100TerminalPartialError(Ac100Error):
    """A terminal action stopped after an earlier step had been acknowledged.

    A later step failed or its outcome is unknown. The steps already taken stay, nothing is
    retried or undone, and the state must be read again. ``stage`` and the message are fixed
    text without any identifier.
    """

    def __init__(self, message: str, stage: str) -> None:
        super().__init__(message)
        self.stage = stage


def normalize_host(host: str) -> str:
    """Accept an HTTP(S) origin or a bare hostname/address, never a URL path."""
    value = host.strip()
    if "://" not in value:
        value = "http://" + value
    try:
        parts = urlsplit(value)
        valid = (
            parts.scheme in {"http", "https"}
            and parts.hostname
            and parts.port != 0
            and parts.path in {"", "/"}
            and not parts.query
            and not parts.fragment
            and parts.username is None
            and parts.password is None
        )
    except ValueError:
        valid = False
    if not valid:
        raise Ac100ProtocolError("Enter a controller hostname or HTTP(S) origin without a path")
    return f"{parts.scheme}://{parts.netloc}".rstrip("/")


def _transform_strings(value: Any, *, encode: bool = False) -> Any:
    if isinstance(value, dict):
        return {key: _transform_strings(item, encode=encode) for key, item in value.items()}
    if isinstance(value, list):
        return [_transform_strings(item, encode=encode) for item in value]
    if isinstance(value, str):
        if encode:
            return quote(value, safe="~()*!.'-")
        if re.search(r"%(?![0-9a-fA-F]{2})", value):
            return value
        try:
            return unquote(value, errors="strict")
        except UnicodeDecodeError:
            return value
    return value


def _legacy_password(password: str) -> str:
    """Match su.js securityEncode, including JavaScript's UTF-16 code units."""
    raw = password.encode("utf-16-le", errors="surrogatepass")
    units = [int.from_bytes(raw[index : index + 2], "little") for index in range(0, len(raw), 2)]
    salt = [ord(char) for char in _LEGACY_SALT]
    return "".join(
        _LEGACY_ALPHABET[
            (
                (units[index] if index < len(units) else 187)
                ^ (salt[index] if index < len(salt) else 187)
            )
            % len(_LEGACY_ALPHABET)
        ]
        for index in range(max(len(units), len(salt)))
    )


def _section(data: dict[str, Any], module: str, name: str) -> dict[str, Any]:
    try:
        value = data[module][name]
    except KeyError, TypeError:
        raise Ac100ProtocolError(f"Missing {module}.{name} data") from None
    if not isinstance(value, dict):
        raise Ac100ProtocolError(f"Invalid {module}.{name} data")
    return value


def _rows(data: dict[str, Any], module: str, table: str) -> list[dict[str, Any]]:
    try:
        wrappers = data[module][table]
    except KeyError, TypeError:
        raise Ac100ProtocolError(f"Missing {module}.{table} table") from None
    if not isinstance(wrappers, list):
        raise Ac100ProtocolError(f"Invalid {module}.{table} table")
    rows = []
    for wrapper in wrappers:
        if not isinstance(wrapper, dict) or len(wrapper) != 1:
            raise Ac100ProtocolError(f"Invalid {module}.{table} row")
        row = next(iter(wrapper.values()))
        if not isinstance(row, dict):
            raise Ac100ProtocolError(f"Invalid {module}.{table} row")
        rows.append(row)
    return rows


def _number(value: Any) -> float | None:
    try:
        number = float(value)
    except TypeError, ValueError:
        return None
    return number if math.isfinite(number) else None


def _integer(value: Any) -> int | None:
    number = _number(value)
    return int(number) if number is not None else None


def _mac(value: Any) -> str:
    if not isinstance(value, str) or not re.fullmatch(
        r"(?:[0-9a-fA-F]{2}[:-]){5}[0-9a-fA-F]{2}", value
    ):
        raise Ac100ProtocolError("Missing or invalid device MAC address")
    return value.replace("-", ":").lower()


def _terminal_mac(value: str) -> str:
    """The normal form of a MAC that a caller names; ValueError if it is not a MAC address."""
    try:
        return _mac(value)
    except Ac100ProtocolError:
        raise ValueError("Not a MAC address") from None


def _parse_ap(row: dict[str, Any]) -> AccessPoint:
    radios = tuple(
        ApRadio(
            unit=str(radio.get("freq_unit", "")),
            frequency=str(radio.get("freq_name", "")),
            client_count=_integer(radio.get("rf_client_num")) or 0,
            channel=(
                channel if (channel := _integer(radio.get("channel"))) and channel > 0 else None
            ),
            channel_load=_number(str(radio.get("channel_load", "")).removesuffix("%")),
        )
        for radio in row.get("rf_entry", [])
    )
    return AccessPoint(
        entry_id=str(row["entry_id"]),
        name=str(row.get("entry_name", "")),
        model=str(row.get("model_name", "")),
        mac=_mac(row.get("mac")),
        ip=str(row.get("ip", "")),
        link_status=str(row.get("link_status", "")),
        led_enabled={"1": True, "2": False}.get(str(row.get("led_status"))),
        uptime=_integer(row.get("online_time")),
        radios=radios,
    )


def _parse_client(row: dict[str, Any]) -> Client:
    return Client(
        entry_id=str(row["entry_id"]),
        mac=_mac(row.get("mac")),
        name=row.get("name"),
        ip=str(row.get("ip", "")),
        ipv6=str(row.get("ipv6", "")),
        ssid=str(row.get("ssid", "")),
        ap_name=str(row.get("ap_name", "")),
        radios=tuple(
            ClientRadio(
                unit=str(radio.get("freq_unit", "")),
                frequency=str(radio.get("freq_name", "")),
                rssi=_integer(radio.get("rssi")),
                negotiated_rate=_number(radio.get("nego_rate")),
            )
            for radio in row.get("rf_entry", [])
        ),
        brand=_client_identification(row.get("brand")),
        operating_system=_client_identification(row.get("operating_system")),
        vlan_id=_client_vlan(row.get("vlan_id")),
        connected_at=_client_connected_at(row),
        ssid_encoding=_text(row.get("encode"), 8),
    )


def _client_identification(value: Any) -> str | None:
    """The controller's vendor/OS identification, excluding its unknown placeholders.

    ``others`` is a category of the controller's identification list, not an unknown.
    """
    text = _version(value)
    return None if text is None or text.casefold() == "unknown" else text


def _client_vlan(value: Any) -> int | None:
    """The web UI displays VLAN 0 as unknown, not as VLAN 1 or an untagged network."""
    vlan = _count(value)
    return vlan if vlan is not None and 1 <= vlan <= 4094 else None


def _client_connected_at(row: dict[str, Any]) -> str | None:
    """The controller-local date and time shown in the UI; no timezone is supplied."""
    date = _text(row.get("connect_date"), 10)
    time = _text(row.get("connect_time"), 8)
    if date is None or time is None:
        return None
    value = f"{date} {time}"
    try:
        datetime.strptime(value, "%Y/%m/%d %H:%M:%S")
    except ValueError:
        return None
    return value


def _host_speeds(row: dict[str, Any]) -> tuple[float | None, float | None]:
    """The upload and download speed of a host row in KB/s; hosts that left show ---."""
    return _rate(row.get("up_speed")), _rate(row.get("down_speed"))


def _with_speeds(client: Client, rows: list[dict[str, Any]]) -> Client:
    """Add the speeds from the client's rows of the host table.

    A MAC can be listed more than once. The row used is one that has a speed: the one for the
    client's SSID and AP if there is one, else the first.
    """
    numeric = [row for row in rows if _host_speeds(row) != (None, None)]
    here = [
        row
        for row in numeric
        if (row.get("ssid"), row.get("ap_name")) == (client.ssid, client.ap_name)
    ]
    if not (chosen := here or numeric):
        return client
    upload, download = _host_speeds(chosen[0])
    return replace(
        client, upload_kilobytes_per_second=upload, download_kilobytes_per_second=download
    )


# Top-bar status enums (the controller UI's root.html refreshConfigStatus). The UI itself only
# distinguishes "Not Saved", upgrade status "3" and RRM status outside {"0", "7"}; a value in any
# other form stays None here instead of being guessed as idle.
_CONFIG_NOT_SAVED = {"Saved": False, "Saving": False, "Not Saved": True}
_FLAG = {"0": False, "1": True}
_UPGRADE_RUNNING = {"0": False, "1": False, "2": False, "3": True, "4": False, "5": False}
_RRM_RUNNING = {"0": False, "7": False} | {str(code): True for code in range(1, 7)}
_STATUS_FIELDS = (
    ("system.cfg_status", "status", "configuration_not_saved", _CONFIG_NOT_SAVED),
    ("apmng_set_status.status", "unknown_ap_flag", "unknown_access_points", _FLAG),
    ("apmng_set_status.status", "ap_full_flag", "access_point_limit_reached", _FLAG),
    ("apmng_upgrade.upgrade_status", "status", "ap_upgrade_in_progress", _UPGRADE_RUNNING),
    ("apmng_rrm.rrm_status", "status", "radio_optimization_in_progress", _RRM_RUNNING),
    ("administrator.auth_status", "status", "controller_writable", _FLAG),
    ("cloud_config.bind_info", "bind_status", "cloud_bound", _FLAG),
    ("cloud_config.conf_mngt", "mngt_switch", "cloud_management_enabled", _FLAG),
)
_PORT_STATES = {"connected": True, "disconnected": False, "disable": False}
_NAME = re.compile(r"[A-Za-z0-9_.:-]{1,32}")
_MBPS = re.compile(r"([0-9]{1,5})M")
_PLACEHOLDER = re.compile(r"-*|undefined.*", re.IGNORECASE)

# The UI's top-bar poll without apmng_mesh (it holds the backhaul key) and ha_mngt. This exact
# subset of modules has not been tested on a controller; a rejected request is retried per module.
_STATUS_SECTIONS = {
    "system": ("cfg_status",),
    "apmng_upgrade": ("upgrade_status",),
    "apmng_set_status": ("status",),
    "apmng_rrm": ("rrm_status",),
    "cloud_config": ("conf_mngt", "bind_info"),
    "administrator": ("auth_status",),
}
_FIRMWARE_SECTIONS = {"cloud_config": ("new_firmware", "upgrade_info")}
_INVISIBLE = frozenset({"Cc", "Cs", "Cn", "Zl", "Zp"})


def _choice[T](states: Mapping[str, T], value: Any) -> T | None:
    return states.get(value) if isinstance(value, str) else None


def _visible(text: str) -> bool:
    """Text without control or unassigned characters or line breaks.

    Names may contain ideographic spaces, emoji joiners and other format characters.
    """
    return all(unicodedata.category(char) not in _INVISIBLE for char in text)


def _text(value: Any, limit: int = 64) -> str | None:
    """A short visible string, or None."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text if 0 < len(text) <= limit and _visible(text) else None


def _name(value: Any) -> str | None:
    return value if isinstance(value, str) and _NAME.fullmatch(value) else None


def _version(value: Any) -> str | None:
    """A firmware or hardware version; the controller's blank placeholders are not versions."""
    text = _text(value)
    return None if text is None or _PLACEHOLDER.fullmatch(text) else text


def _finite(value: Any) -> float | None:
    return None if isinstance(value, bool) else _number(value)


def _rate(value: Any) -> float | None:
    number = _finite(value)
    return number if number is not None and number >= 0 else None


def _count(value: Any) -> int | None:
    """A non-negative counter; digit strings keep exact precision."""
    if isinstance(value, int) and not isinstance(value, bool):
        return value if value >= 0 else None
    if isinstance(value, str) and value.isascii() and value.isdigit() and len(value) <= 20:
        return int(value)
    return None


def _sections_request(wanted: Mapping[str, tuple[str, ...]]) -> dict[str, Any]:
    return {"method": "get", **{module: {"name": list(names)} for module, names in wanted.items()}}


def _present_sections(
    result: dict[str, Any], wanted: Mapping[str, tuple[str, ...]]
) -> dict[str, dict[str, Any]]:
    """Keep only the requested sections that came back as objects, keyed ``module.section``."""
    found: dict[str, dict[str, Any]] = {}
    for module, names in wanted.items():
        data = result.get(module)
        if not isinstance(data, dict):
            continue
        for name in names:
            if isinstance(section := data.get(name), dict):
                found[f"{module}.{name}"] = section
    return found


def _parse_status(sections: dict[str, dict[str, Any]]) -> dict[str, Scalar]:
    """Project the known status sections; a value in an unrecognised form is None."""
    values: dict[str, Scalar] = {}
    for source, wire_field, key, states in _STATUS_FIELDS:
        if (section := sections.get(source)) is not None:
            values[key] = _choice(states, section.get(wire_field))
    return values


def _parse_firmware(sections: dict[str, dict[str, Any]]) -> FirmwareAvailability | None:
    """Only a set cloud announcement (new_firmware.fw_new_notify) makes upgrade_info current."""
    info = sections.get("cloud_config.upgrade_info")
    notice = sections.get("cloud_config.new_firmware")
    if info is None or notice is None:
        return None
    announced = _choice(_FLAG, notice.get("fw_new_notify"))
    if announced is None:
        return None
    return FirmwareAvailability(_version(info.get("version")) if announced else None)


def _parse_ap_firmware(rows: list[dict[str, Any]]) -> tuple[ApFirmware, ...]:
    """Map only the identity and versions; the row's name, model and upgrade status are dropped."""
    items: dict[str, ApFirmware] = {}
    for row in rows:
        mac = _mac(row.get("mac"))
        items[mac] = ApFirmware(
            mac,
            _version(row.get("sw_version")),
            _version(row.get("hw_version")),
            _version(row.get("new_sw_version")),
        )
    return tuple(items.values())


def _parse_port(row: dict[str, Any]) -> PortStatus:
    switch = _name(row.get("switch_dev"))
    section = _name(row.get(".name"))
    label = _text(row.get("port_name"), 32)
    if switch is None or section is None or label is None:
        raise Ac100ProtocolError("Invalid port data")
    connected = _choice(_PORT_STATES, row.get("port_state"))
    speed = row.get("link_speed")
    link_speed = _MBPS.fullmatch(speed) if connected and isinstance(speed, str) else None
    return PortStatus(
        port_id=f"{switch}_{section}",
        name=label,
        connected=connected,
        speed_mbps=int(link_speed[1]) if link_speed else None,
    )


def _parse_ports(rows: list[dict[str, Any]]) -> tuple[PortStatus, ...]:
    ports = {port.port_id: port for port in map(_parse_port, rows)}
    if len(ports) != len(rows):
        raise Ac100ProtocolError("Duplicate port identity")
    return tuple(ports.values())


def _parse_interface(row: dict[str, Any]) -> InterfaceTraffic:
    """The rates are KB/s and packets/s (the UI's column units), counters are totals."""
    name = _name(row.get("interface"))
    if name is None:
        raise Ac100ProtocolError("Invalid interface statistics")
    return InterfaceTraffic(
        name,
        _rate(row.get("tx_bps")),
        _rate(row.get("rx_bps")),
        _rate(row.get("tx_pps")),
        _rate(row.get("rx_pps")),
        _count(row.get("tx_bytes")),
        _count(row.get("rx_bytes")),
    )


def _config_value(item: ConfigField, raw: Any) -> Scalar:
    if item.kind in {"bool", "enum"}:
        return _choice(dict(item.mapping), raw)
    if item.kind == "text":
        return _text(raw)
    number = _finite(raw)
    if number is None:
        return None
    number += item.offset
    return int(number) if number.is_integer() else number


def _radio_value(item: RadioField, row: dict[str, Any]) -> Scalar:
    """Project one catalogued field of a radio row; an unknown or inapplicable one is None."""
    if item.applies_when is not None and row.get(item.applies_when[0]) != item.applies_when[1]:
        return None
    raw = row.get(item.field)
    if (state := _choice(dict(item.mapping), raw)) is not None:
        return state
    if item.kind != "number":
        return None
    number = _finite(raw)
    if number is None:
        return None
    if item.minimum is not None and number < item.minimum:
        return None
    if item.maximum is not None and number > item.maximum:
        return None
    return int(number) if number.is_integer() else number


def _parse_radios(rows: list[dict[str, Any]]) -> tuple[RadioSettings, ...]:
    """Keep each radio's AP address, unit and catalogued settings; the AP name is dropped.

    ``rf_id`` is the AP's ``entry_id`` times 16 plus the radio's local index (the web UI splits
    it the same way). Rows of AP templates are not radios of any AP. A row or a pair of rows
    that cannot be told apart makes the whole table unusable.
    """
    radios: dict[tuple[str, str], RadioSettings] = {}
    for row in rows:
        if row.get("ap_type") == "template":
            continue
        radio_id, unit = _count(row.get("rf_id")), _count(row.get("freq_unit"))
        if radio_id is None or unit is None:
            raise Ac100ProtocolError("Invalid radio data")
        identity = (str(radio_id // 16), str(unit))
        if identity in radios:
            raise Ac100ProtocolError("Duplicate radio identity")
        values = {item.key: _radio_value(item, row) for item in RADIO_FIELDS}
        radios[identity] = RadioSettings(*identity, values)
    return tuple(radios.values())


def _parse_configuration(
    sections: dict[str, dict[str, Any]], radios: tuple[RadioSettings, ...] | None = None
) -> ConfigurationState:
    """Read exactly the catalogued fields; every other field of a section is ignored."""
    values = {
        item.key: _config_value(item, sections[item.source].get(item.field))
        for item in CONFIGURATION_FIELDS
        if item.source in sections
    }
    available = set(sections)
    if radios is not None:
        available.add(RADIO_SOURCE)
    return ConfigurationState(values, frozenset(available), radios or ())


class Ac100Api:
    """An AC100 client that shares, but never owns, an aiohttp session."""

    def __init__(
        self, session: aiohttp.ClientSession, host: str, username: str, password: str
    ) -> None:
        self.host = normalize_host(host)
        self._session = session
        self._username = username
        self._password = password
        self._stok: str | None = None
        self._closed = False
        self._lock = asyncio.Lock()

    def _ensure_open(self) -> None:
        if self._closed:
            raise Ac100Error("Controller client has been closed")

    async def _post(self, path: str, data: dict[str, Any]) -> dict[str, Any]:
        try:
            async with self._session.post(
                self.host + path,
                json=_transform_strings(data, encode=True),
                timeout=_REQUEST_TIMEOUT,
                allow_redirects=False,
                raise_for_status=False,
            ) as response:
                status = response.status
                if status >= 400 and status not in {401, 403}:
                    raise Ac100ConnectionError(f"Controller returned HTTP {status}")
                try:
                    result = json.loads(await response.text())
                except ValueError, UnicodeError:
                    if status in {401, 403}:
                        raise Ac100AuthError("Controller authentication was rejected") from None
                    raise Ac100ProtocolError("Controller did not return JSON") from None
        except aiohttp.ClientError, TimeoutError:
            raise Ac100ConnectionError("Unable to communicate with the controller") from None
        if status in {401, 403} and (not isinstance(result, dict) or "error_code" not in result):
            raise Ac100AuthError("Controller authentication was rejected")
        if not isinstance(result, dict) or "error_code" not in result:
            raise Ac100ProtocolError("Controller response has no error code")
        result = _transform_strings(result)
        code = result["error_code"]
        if not isinstance(code, int):
            raise Ac100ProtocolError("Controller response has an invalid error code")
        details = result.get("data")
        subcode = details.get("code") if isinstance(details, dict) else None
        if code == -57704 or subcode == -57704:
            _LOGGER.debug("Controller authentication blocked by another active session")
            raise Ac100SessionBusyError("Another administrator session is active")
        if code == -40404 or subcode == -40404:
            _LOGGER.debug("Controller account is temporarily locked")
            raise Ac100AccountLockedError("The controller account is temporarily locked")
        if code == -40401:
            if path != "/" and status != 403 and (subcode is None or subcode == -40401):
                raise Ac100SessionExpiredError("Controller session has expired")
            raise Ac100AuthError("Controller authentication was rejected")
        if status in {401, 403} or code in {-40100, -57703, -57704, -57705}:
            raise Ac100AuthError("Controller authentication or access was rejected")
        if code != 0:
            raise Ac100ProtocolError("Controller rejected the request")
        return result

    async def _login(self) -> None:
        self._ensure_open()
        _LOGGER.debug("Controller authentication started")
        self._stok = None
        info = await self._post(
            "/", {"method": "do", "user_management": {"get_encrypt_info": None}}
        )
        types = info.get("encrypt_type", [])
        if not isinstance(types, list):
            raise Ac100ProtocolError("Controller returned invalid authentication metadata")
        if "3" in types:
            nonce = info.get("nonce")
            if not isinstance(nonce, str) or not nonce:
                raise Ac100ProtocolError("Controller authentication nonce is missing")
            encoded = hashlib.md5(
                f"{self._password}:{nonce}".encode(), usedforsecurity=False
            ).hexdigest()
            login = {"username": self._username, "password": encoded, "encrypt_type": "3"}
        elif "-3" in types:
            login = {
                "username": self._username,
                "password": _legacy_password(self._password),
                "force": 0,
            }
        else:
            raise Ac100ProtocolError("Controller authentication method is unsupported")
        self._ensure_open()
        result = await self._post("/", {"method": "do", "login": login})
        token = result.get("stok")
        if not isinstance(token, str) or not token:
            raise Ac100ProtocolError("Controller did not return a valid session token")
        self._stok = token
        _LOGGER.debug("Controller authentication succeeded")

    async def async_login(self) -> None:
        """Authenticate without taking over or logging out another administrator."""
        async with self._lock:
            self._ensure_open()
            if self._stok is None:
                await self._login()

    async def async_logout(self) -> None:
        """Retire this logical client and release only its token, without replaying a request."""
        self._closed = True
        async with self._lock:
            if self._stok is None:
                _LOGGER.debug("Controller own-session logout skipped; no active session")
                return
            _LOGGER.debug("Controller own-session logout started")
            try:
                await self._post(
                    f"/stok={quote(self._stok, safe='')}/ds",
                    {"method": "do", "system": {"logout": None}},
                )
            except Ac100SessionExpiredError:
                return
            finally:
                self._stok = None
                _LOGGER.debug("Controller own-session cleanup complete")

    async def _request(self, data: dict[str, Any]) -> dict[str, Any]:
        self._ensure_open()
        if self._stok is None:
            await self._login()
        self._ensure_open()
        try:
            return await self._post(f"/stok={quote(self._stok, safe='')}/ds", data)
        except Ac100SessionExpiredError:
            self._stok = None
            if data.get("method") != "get":
                _LOGGER.debug("Controller session expired during a write; not replayed")
                raise
            _LOGGER.debug("Controller session expired; reauthenticating once")
            await self._login()
            self._ensure_open()
            return await self._post(f"/stok={quote(self._stok, safe='')}/ds", data)

    async def _table(
        self,
        module: str,
        table: str,
        filters: list[dict[str, Any]] | None,
        *,
        max_rows: int | None = None,
    ) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        start = 0
        target: int | None = None
        while target is None or start < target:
            query: dict[str, Any] = {"table": table}
            if filters is not None:
                query["filter"] = filters
            query["para"] = {"start": start, "end": start + _PAGE_SIZE - 1}
            result = await self._request({"method": "get", module: query})
            count = _integer(_section(result, module, "count").get(table))
            if count is None or count < 0 or (max_rows is not None and count > max_rows):
                raise Ac100ProtocolError("Controller table count is invalid")
            target = count if target is None else min(target, count)
            page = _rows(result, module, table)
            if len(page) < min(_PAGE_SIZE, max(0, target - start)):
                raise Ac100ProtocolError("Controller returned an incomplete table")
            rows.extend(page)
            start += _PAGE_SIZE
        return rows

    async def async_get_snapshot(self) -> Snapshot:
        """Poll controller, APs and every client page before publishing a snapshot.

        The client list says who is connected. The clients' speeds come from the host table, an
        optional read: if it is rejected, malformed or not readable with this account, the speeds
        are unknown and the rest is kept.
        """
        async with self._lock:
            result = await self._request(
                {
                    "method": "get",
                    "firmware": {"name": "config"},
                    "system": {"name": ["cpu_usage", "mem_usage", "system_time"]},
                }
            )
            identity = await self._request({"method": "get", "interface": {"table": "interface"}})
            interfaces = _rows(identity, "interface", "interface")
            lan = next((row for row in interfaces if row.get("t_name") == "lan"), None)
            if lan is None:
                raise Ac100ProtocolError("Controller LAN identity is missing")
            controller_mac = _mac(lan.get("macaddr"))
            firmware = _section(result, "firmware", "config")
            cpu = _section(result, "system", "cpu_usage")
            controller = Controller(
                mac=controller_mac,
                model=str(firmware.get("model", "")),
                hardware_version=str(firmware.get("hardware_version", "")),
                firmware_version=str(firmware.get("firmware_version", "")),
                cpu_usage={
                    key: number
                    for key, value in cpu.items()
                    if not key.startswith(".") and (number := _number(value)) is not None
                },
                memory_usage=_number(_section(result, "system", "mem_usage").get("mem")),
                uptime=_integer(_section(result, "system", "system_time").get("run")),
            )
            ap_rows = await self._read_ap_rows()
            client_rows = await self._read_client_rows()
            try:
                aps = {_mac(row.get("mac")): _parse_ap(row) for row in ap_rows}
                clients = {_mac(row.get("mac")): _parse_client(row) for row in client_rows}
            except KeyError, TypeError, AttributeError:
                raise Ac100ProtocolError("Controller returned malformed device telemetry") from None
            hosts = await self._read_optional_hosts()
            terminals = None if hosts is None else await self._read_terminals(hosts, aps.values())
            hosts = hosts or {}
            clients = {
                mac: _with_speeds(client, hosts.get(mac, [])) for mac, client in clients.items()
            }
            _LOGGER.debug(
                "Controller snapshot received (aps=%s, clients=%s)", len(aps), len(clients)
            )
            return Snapshot(
                controller=controller,
                aps=tuple(aps.values()),
                clients=tuple(clients.values()),
                terminals=terminals,
            )

    async def _read_ap_rows(self) -> list[dict[str, Any]]:
        return await self._table(
            "apmng_status", "ap_list", [{"group_id": ["-1"], "ap_status": ["0"]}]
        )

    async def _read_client_rows(self) -> list[dict[str, Any]]:
        return await self._table("apmng_client", "client_list", [{"group_id": ["-1"]}])

    async def _optional[T](self, read: Awaitable[T]) -> T | None:
        """Await one optional read: a rejected or malformed answer is None, nothing else is."""
        try:
            return await read
        except Ac100ProtocolError:
            if self._stok is None:
                raise  # a failed sign-in, not a problem with this source
            return None

    async def _read_hosts(self) -> dict[str, list[dict[str, Any]]]:
        """Read the Wireless Terminal Management table and group its rows by MAC.

        It also lists hosts that have left, so it says nothing about who is connected. A row
        without a valid MAC makes the whole table unusable.
        """
        rows = await self._table(
            "host_management",
            "host_info",
            [{"group_id": ["-1"]}],
            max_rows=_MAX_OPTIONAL_ROWS,
        )
        hosts: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            hosts.setdefault(_mac(row.get("mac")), []).append(row)
        return hosts

    async def _read_optional_hosts(self) -> dict[str, list[dict[str, Any]]] | None:
        """The host table's rows by MAC, or None if it is garbled or this account may not read it.

        The client list was just read, so an authorization error here is about this table, not
        about the credentials. Connection and session errors propagate.
        """
        hosts = await self._read_dependent(self._read_hosts())
        if hosts is None:
            _LOGGER.debug("Controller client speeds are unavailable")
        return hosts

    async def _read_dependent[T](self, read: Awaitable[T]) -> T | None:
        """Await a read that only adds to a healthy poll; None if it is garbled or not allowed.

        The core reads of the poll just succeeded, so an authorization error here is about this
        source, not about the credentials. Connection and session errors propagate.
        """
        try:
            return await self._optional(read)
        except Ac100SessionBusyError:
            raise
        except Ac100AuthError:
            if self._stok is None:
                raise  # a failed sign-in, not a permission error on this source
            return None

    async def _read_radio_bindings(
        self, service: int | None, entry_macs: Mapping[str, str]
    ) -> tuple[RadioBinding, ...]:
        """Read the manager's direct, unpaged radio list, rejecting incomplete answers."""
        result = await self._request(
            {
                "method": "get",
                RADIO_BIND_MODULE: {
                    "table": RADIO_BIND_TABLE,
                    "filter": radio_bind_filter(service),
                },
            }
        )
        count = _count(_section(result, RADIO_BIND_MODULE, "count").get(RADIO_BIND_TABLE))
        rows = _rows(result, RADIO_BIND_MODULE, RADIO_BIND_TABLE)
        if count is None or count > _MAX_RADIO_ROWS or len(rows) != count:
            raise Ac100ProtocolError("Controller radio binding list is incomplete or invalid")
        try:
            return parse_radio_bind_rows(rows, entry_macs)
        except ValueError:
            raise Ac100ProtocolError("Invalid radio binding data") from None

    async def _read_time_object(self, object_id: str) -> dict[str, Any]:
        """Read one time object as the terminal page does, with no paging.

        A filter on the address that gives no row or several is not the object that was asked
        for, so only exactly one row is accepted.
        """
        result = await self._request(time_object_read_request(object_id))
        rows = _rows(result, "time_mngt", "time_obj")
        if len(rows) != 1:
            raise Ac100ProtocolError("Controller returned an unexpected time object")
        return rows[0]

    async def _read_schedule(self, object_id: str) -> TerminalSchedule:
        try:
            return parse_terminal_schedule(await self._read_time_object(object_id))
        except ValueError:
            raise Ac100ProtocolError("Invalid time object data") from None

    async def _read_terminals(
        self, hosts: dict[str, list[dict[str, Any]]], aps: Iterable[AccessPoint]
    ) -> TerminalState:
        """The saved configuration of every record of the host table, which is already read.

        Two sources fill in what a record only points to. The radio binding list names the AP
        and radio of a saved binding and offers the targets of each wireless network: it is read
        once for each network that a record is connected to, and once more for every radio if a
        saved binding is still unnamed after that. A time object holds a schedule and is read
        once for each address that a record links to. The host table bounds both sets. A source
        rejection leaves only its values unknown. The first transport failure discards all R/T
        results and stops this batch, while the fresh core, clients and scalar host data survive.
        Actual sign-in failures, busy sessions and account locks still propagate.
        """
        entry_macs, ap_names = ap_directory(aps)
        records = [merge_host_rows(mac, rows) for mac, rows in hosts.items()]
        services = dict.fromkeys(
            record.service
            for record in records
            if record.service is not None and record.service >= 0
        )
        radios: dict[str, RadioBinding] = {}
        targets: dict[int, tuple[TerminalBindingTarget, ...] | None] = {}
        schedules: dict[str, TerminalSchedule | None] = {}
        try:
            for service in services:
                found = await self._read_dependent(self._read_radio_bindings(service, entry_macs))
                targets[service] = None if found is None else binding_targets(found, ap_names)
                radios.update({radio.radio_id: radio for radio in found or ()})
            if any(
                radio_id not in radios
                for record in records
                for radio_id in saved_radio_ids(record) or ()
            ):
                found = await self._read_dependent(self._read_radio_bindings(None, entry_macs))
                radios.update({radio.radio_id: radio for radio in found or ()})
            object_ids = dict.fromkeys(
                ids[0] for record in records if len(ids := time_object_ids(record) or ()) == 1
            )
            for object_id in object_ids:
                schedules[object_id] = await self._read_dependent(self._read_schedule(object_id))
        except Ac100ConnectionError:
            if self._stok is None:
                raise  # authentication failed, not a transport failure on a dependent source
            # One failed dependency stops this batch; do not keep partial or earlier R/T data.
            radios.clear()
            targets.clear()
            schedules.clear()
            _LOGGER.debug("Controller terminal dependent sources are unavailable")
        _LOGGER.debug(
            "Controller terminal records received (records=%s, services=%s, schedules=%s)",
            len(records),
            len(targets),
            len(schedules),
        )
        return TerminalState(
            tuple(
                build_configuration(
                    record,
                    targets=targets.get(record.service) if record.service is not None else None,
                    radios=radios,
                    ap_names=ap_names,
                    schedules=schedules,
                )
                for record in records
            )
        )

    async def _get_sections(
        self, wanted: Mapping[str, tuple[str, ...]]
    ) -> dict[str, dict[str, Any]]:
        """GET named sections, keyed ``module.section``; one that cannot be read is absent.

        A rejected multi-module request is retried one module at a time, so one unreadable
        module does not hide the others.
        """
        result = await self._optional(self._request(_sections_request(wanted)))
        if result is not None:
            return _present_sections(result, wanted)
        found: dict[str, dict[str, Any]] = {}
        if len(wanted) > 1:
            for module, names in wanted.items():
                single = {module: names}
                result = await self._optional(self._request(_sections_request(single)))
                if result is not None:
                    found |= _present_sections(result, single)
        return found

    async def _read_ap_firmware(self) -> tuple[ApFirmware, ...]:
        rows = await self._table(
            "apmng_upgrade",
            "ap_list",
            [{"group_id": ["-1"], "ap_role": ["cap", "normal"]}],
            max_rows=_MAX_OPTIONAL_ROWS,
        )
        return _parse_ap_firmware(rows)

    async def _read_ports(self) -> tuple[PortStatus, ...]:
        # Like the UI, one unpaged request: the table is the controller's few physical ports.
        result = await self._request({"method": "get", "port": {"table": "port"}})
        rows = _rows(result, "port", "port")
        if len(rows) > _MAX_OPTIONAL_ROWS:
            raise Ac100ProtocolError("Controller returned too many ports")
        return _parse_ports(rows)

    async def _read_interfaces(self) -> tuple[InterfaceTraffic, ...]:
        rows = await self._table("system", "ifstat_list", None, max_rows=_MAX_OPTIONAL_ROWS)
        return tuple(map(_parse_interface, rows))

    async def _read_radios(self) -> tuple[RadioSettings, ...]:
        rows = await self._table(
            RADIO_MODULE,
            RADIO_TABLE,
            [{"group_id": ["-1"], "ap_status": ["0"]}],
            max_rows=_MAX_RADIO_ROWS,
        )
        return _parse_radios(rows)

    async def async_get_monitoring(self, *, wireless_services: bool = False) -> MonitoringState:
        """Read slow, credential-free status; a source that is rejected or garbled is left out.

        Every request is a GET under the shared lock and session. Connection, authentication and
        session errors propagate; the core snapshot is a separate call and is never affected.
        The SSID table, which also holds Wi-Fi and RADIUS secrets, is only requested with
        ``wireless_services``.
        """
        async with self._lock:
            self._ensure_open()
            status = await self._get_sections(_STATUS_SECTIONS)
            values = _parse_status(status)
            available = set(status)
            firmware = _parse_firmware(await self._get_sections(_FIRMWARE_SECTIONS))
            if firmware is not None:
                available.add("cloud_config.upgrade_info")
            tables: dict[str, Any] = {}
            for source, read in (
                ("apmng_upgrade.ap_list", self._read_ap_firmware),
                ("port.port", self._read_ports),
                ("system.ifstat_list", self._read_interfaces),
            ):
                if (table := await self._optional(read())) is not None:
                    tables[source] = table
            available.update(tables)
            services = await self._read_optional_wireless_services() if wireless_services else None
            state = MonitoringState(
                values=values,
                available_sections=frozenset(available),
                firmware=firmware,
                ap_firmware=tables.get("apmng_upgrade.ap_list", ()),
                ports=tables.get("port.port", ()),
                interfaces=tables.get("system.ifstat_list", ()),
                wireless_services=services,
            )
            _LOGGER.debug(
                "Controller monitoring received (sources=%s, aps=%s, ports=%s, interfaces=%s)",
                len(state.available_sections),
                len(state.ap_firmware),
                len(state.ports),
                len(state.interfaces),
            )
            return state

    async def async_get_configuration(self) -> ConfigurationState:
        """Read the allow-listed CONFIGURATION_FIELDS and RADIO_FIELDS settings.

        A section or the radio table that cannot be read is left out. Every request is a GET
        under the shared lock and session. Connection, authentication and session errors
        propagate. Only catalogued fields leave this method.
        """
        async with self._lock:
            self._ensure_open()
            sections = await self._get_sections(CONFIGURATION_SOURCES)
            radios = await self._optional(self._read_radios())
            state = _parse_configuration(sections, radios)
            _LOGGER.debug(
                "Controller configuration received (sources=%s, values=%s, radios=%s)",
                len(state.available_sections),
                len(state.values),
                len(state.radios),
            )
            return state

    async def _read_optional_wireless_services(self) -> tuple[WirelessService, ...] | None:
        """The SSID table, or None if it is garbled or this account may not read it.

        The other monitoring reads in the same session succeeded, so an authorization error here
        is about this table, not about the credentials.
        """
        try:
            return await self._optional(self._read_wireless_services())
        except Ac100SessionBusyError:
            raise
        except Ac100AuthError:
            _LOGGER.debug("Controller wireless services are not readable with this account")
            return None

    async def _read_wireless_services(self) -> tuple[WirelessService, ...]:
        """Read the SSID table and keep only the allow-listed fields of each row.

        The same rows hold Wi-Fi and RADIUS secrets and the request cannot leave them out, so
        every row is projected here and none of them leaves this method.
        """
        try:
            rows = await self._table("apmng_wserv", "wlan_serv", None, max_rows=_MAX_OPTIONAL_ROWS)
            services = self._parse_wireless_services(rows)
        except Ac100ProtocolError:
            _LOGGER.debug("Controller wireless services are unavailable")
            raise
        _LOGGER.debug("Controller wireless services received (count=%s)", len(services))
        return services

    @staticmethod
    def _parse_wireless_services(rows: list[dict[str, Any]]) -> tuple[WirelessService, ...]:
        """Map each row to a WirelessService; a row that cannot be told apart voids the table.

        The identity is ``serv_id``, never the name or the row position.
        """
        services: dict[str, WirelessService] = {}
        for row in rows:
            number, name = _count(row.get("serv_id")), row.get("ssid")
            # An SSID has at most 32 bytes, so at most 96 characters while still percent-encoded.
            if (
                number is None
                or not isinstance(name, str)
                or not name.strip()
                or len(name) > 96
                or not _visible(name)
            ):
                raise Ac100ProtocolError("Invalid wireless service data")
            if (service_id := str(number)) in services:
                raise Ac100ProtocolError("Duplicate wireless service identity")
            vlan = _count(row.get("default_bind_vlan"))
            if row.get("bind_vlan_type") != "single_vlan" or vlan not in range(1, 4095):
                vlan = None  # VLAN IDs are 1-4094; the controller sends 0 for none
            services[service_id] = WirelessService(
                service_id,
                name,
                security=_choice(WIRELESS_SERVICE_SECURITY, row.get("encryption")),
                vlan=vlan,
                **{
                    flag.key: _choice(flag.states, row.get(flag.field))
                    for flag in WIRELESS_SERVICE_FLAGS
                },
            )
        return tuple(services.values())

    async def async_set_wireless_service(
        self, service_id: str, field: str, value: bool, *, name: str
    ) -> None:
        """Set one on/off setting of one wireless network, once and without a replay.

        ``field`` is a key of WIRELESS_SERVICE_FLAGS. ``name`` is the SSID that the caller
        believes the identity belongs to, as a service ID can be reused after its network is
        deleted. Under the shared lock the SSID table is read again, and nothing is sent unless
        the network still has that identity and name. The write holds only the one setting, and
        is left out when the controller already has the requested value.
        """
        flag = {item.key: item for item in WIRELESS_SERVICE_FLAGS}[field]
        async with self._lock:
            self._ensure_open()
            services = await self._read_wireless_services()
            service = next((item for item in services if item.service_id == service_id), None)
            if service is None or service.name != name:
                raise Ac100Error("The wireless network changed or no longer exists")
            if getattr(service, field) is value:
                _LOGGER.debug("Controller wireless network setting already applied (%s)", field)
                return
            _LOGGER.debug(
                "Controller management request: wireless network (%s, enabled=%s)", field, value
            )
            await self._request(
                {
                    "method": "set",
                    "apmng_wserv": {
                        "table": "wlan_serv",
                        "filter": [{"serv_id": service_id}],
                        "para": {flag.field: flag.on if value else flag.off},
                    },
                }
            )

    async def _get_ap_led_wifi_sync(self, entry_id: str) -> bool | None:
        """Read one AP configuration transiently; never cache its accompanying fields."""
        _LOGGER.debug("AP LED safety check started")
        result = await self._request(
            {
                "method": "get",
                "apmng_set": {
                    "table": "ap_list",
                    "filter": [{"entry_id": entry_id}],
                    "para": {"start": 0, "end": 0},
                },
            }
        )
        try:
            rows = _rows(result, "apmng_set", "ap_list")
        except Ac100ProtocolError:
            rows = []
        state = None
        if len(rows) == 1 and str(rows[0].get("entry_id")) == entry_id:
            value = rows[0].get("led_wifi_sync")
            if value == "on":
                state = True
            elif value == "off":
                state = False
        _LOGGER.debug("AP LED safety check complete (verified_off=%s)", state is False)
        return state

    async def _read_form_section(self, module: str, section: str) -> dict[str, Any]:
        """Read one settings section now: a write builds its form from this, never from a cache."""
        result = await self._request({"method": "get", module: {"name": section}})
        return _section(result, module, section)

    async def async_set_setting(self, field: ConfigField, value: Scalar) -> None:
        """Change one catalogued setting by sending its web page's complete form once.

        The value is checked against the page's own rules first (ValueError, and nothing is
        read or sent). Then, under the shared lock, the section is read again and the form is
        built from that fresh read, with only this field (and the field its page ties to it)
        changed. The write is never sent again, not even after a session expiry.
        """
        raw = encode_setting(field, value)
        async with self._lock:
            self._ensure_open()
            section = await self._read_form_section(field.module, field.section)
            try:
                request = form_request(field, raw, section)
            except ValueError as err:
                raise Ac100ProtocolError(str(err)) from None
            _LOGGER.debug("Controller management request: setting")
            await self._request(request)

    async def async_set_controller_alias(self, name: str) -> None:
        """Rename the controller with the System page's request, which sends the name alone."""
        if not alias_is_valid(name):
            raise ValueError(ALIAS_RULE)
        async with self._lock:
            self._ensure_open()
            _LOGGER.debug("Controller management request: controller name")
            await self._request({"method": "set", "system": {"sys": {"dev_alias": name}}})

    async def _find_radio_address(self, ap_entry_id: str, unit: str) -> str:
        """Look up one radio's ``rf_id`` in a fresh read of the radio table.

        The radio is the one row whose ``rf_id // 16`` is the AP's ``entry_id`` and whose
        ``freq_unit`` is the unit. As in _parse_radios, AP templates are no radios and a row
        that cannot be read makes the whole table unusable.
        """
        rows = await self._table(
            RADIO_MODULE,
            RADIO_TABLE,
            [{"group_id": ["-1"], "ap_status": ["0"]}],
            max_rows=_MAX_RADIO_ROWS,
        )
        matches = []
        for row in rows:
            if row.get("ap_type") == "template":
                continue
            radio_id, radio_unit = _count(row.get("rf_id")), _count(row.get("freq_unit"))
            if radio_id is None or radio_unit is None:
                raise Ac100ProtocolError("Invalid radio data")
            if (str(radio_id // 16), str(radio_unit)) == (ap_entry_id, unit):
                matches.append(str(radio_id))
        if not matches:
            raise Ac100Error("The radio changed or no longer exists")
        if len(matches) > 1:
            raise Ac100ProtocolError("Duplicate radio identity")
        return matches[0]

    async def async_set_radio_enabled(self, ap_entry_id: str, unit: str, enabled: bool) -> None:
        """Switch one AP radio on or off with the radio page's one-field update, once.

        ``ap_entry_id`` is the AP's current command address. Under the shared lock the radio
        table is read again, and nothing is sent unless exactly one radio matches.
        """
        if not isinstance(enabled, bool):
            raise ValueError("A radio is switched on or off")
        async with self._lock:
            self._ensure_open()
            radio_id = await self._find_radio_address(ap_entry_id, unit)
            _LOGGER.debug("Controller management request: radio (enabled=%s)", enabled)
            await self._request(
                {
                    "method": "set",
                    RADIO_MODULE: {
                        "table": RADIO_TABLE,
                        "filter": [{"rf_id": radio_id}],
                        "para": {"rf_state": "on" if enabled else "off"},
                    },
                }
            )

    async def async_save_configuration(self) -> None:
        """Save the configuration once, then wait until the controller reports it finished.

        The save request is sent once and never repeated. The wait polls the save status every
        SAVE_POLL_INTERVAL seconds and does not hold the shared lock in between. A poll without
        an answer is only a poll that has no answer yet: the controller can be slow while it
        writes. If it has not reported completion within SAVE_TIMEOUT seconds, the outcome is
        unknown (Ac100SaveTimeoutError).
        """
        async with self._lock:
            self._ensure_open()
            _LOGGER.debug("Controller management request: save configuration")
            await self._request({"method": "do", "system": {"save_cfg": None}})
        deadline = monotonic() + SAVE_TIMEOUT
        polls = 0
        while True:
            await sleep(SAVE_POLL_INTERVAL)
            polls += 1
            if await self._save_is_complete():
                _LOGGER.debug("Controller configuration save complete (polls=%s)", polls)
                return
            if monotonic() >= deadline:
                _LOGGER.debug("Controller configuration save not confirmed (polls=%s)", polls)
                raise Ac100SaveTimeoutError("The controller did not report that saving finished")

    async def _save_is_complete(self) -> bool:
        async with self._lock:
            self._ensure_open()
            try:
                result = await self._request(
                    {"method": "get", "system": {"name": "save_cfg_status"}}
                )
                status = _section(result, "system", "save_cfg_status").get("status")
            except Ac100ConnectionError, Ac100ProtocolError:
                return False
        return status == SAVE_COMPLETE

    async def async_get_ap_led_wifi_sync(self, entry_id: str) -> bool | None:
        """Read one AP's sync setting: True=on, False=off, None=unverified."""
        async with self._lock:
            self._ensure_open()
            return await self._get_ap_led_wifi_sync(entry_id)

    async def async_set_ap_led(self, entry_id: str, enabled: bool) -> None:
        """Set one AP LED only after its current WiFi synchronization is confirmed off."""
        async with self._lock:
            self._ensure_open()
            if await self._get_ap_led_wifi_sync(entry_id) is not False:
                raise Ac100UnsafeLedError("Cannot confirm LED and WiFi synchronization is disabled")
            _LOGGER.debug("Controller management request: AP LED (enabled=%s)", bool(enabled))
            await self._request(
                {
                    "method": "set",
                    "apmng_status": {
                        "table": "ap_list",
                        "filter": [{"entry_id": entry_id}],
                        "para": {"led_status": "1" if enabled else "0"},
                    },
                }
            )

    async def async_reboot_ap(self, entry_id: str) -> None:
        """Request one AP reboot, with no network-error replay."""
        async with self._lock:
            self._ensure_open()
            _LOGGER.debug("Controller management request: AP reboot")
            await self._request(
                {"method": "do", "apmng_status": {"ap_reboot": {"entry_id": [entry_id]}}}
            )

    async def async_reboot_controller(self) -> None:
        """Request a controller reboot, with no network-error replay."""
        async with self._lock:
            self._ensure_open()
            _LOGGER.debug("Controller management request: controller reboot")
            await self._request({"method": "do", "system": {"reboot": None}})

    async def _terminal_records(self) -> dict[str, HostRecord]:
        """Read the host table now: a write is planned from this, never from the last poll."""
        hosts = await self._read_hosts()
        return {mac: merge_host_rows(mac, rows) for mac, rows in hosts.items()}

    async def _present_terminals(self) -> dict[str, str]:
        """The MAC of every client that the client list shows now, as the controller spells it."""
        rows = await self._read_client_rows()
        return {_mac(row.get("mac")): str(row["mac"]) for row in rows}

    async def _fresh_targets(self, record: HostRecord) -> tuple[TerminalBindingTarget, ...]:
        """The binding targets of the record's wireless network, read now.

        An AP is named by its MAC, found from the radio's address in the AP list that is read
        here too, so no name and no address from an earlier poll is trusted.
        """
        if record.service is None or record.offline:
            raise TerminalRefusal(BINDING_NOT_EDITABLE)
        try:
            aps = [_parse_ap(row) for row in await self._read_ap_rows()]
        except KeyError, TypeError, AttributeError:
            raise Ac100ProtocolError("Controller returned malformed device telemetry") from None
        entry_macs, ap_names = ap_directory(aps)
        radios = await self._read_radio_bindings(record.service, entry_macs)
        return binding_targets(radios, ap_names)

    async def async_set_terminal_field(
        self, mac: str, field: TerminalField, value: bool | int | str
    ) -> None:
        """Change one saved setting of one terminal, once and without a replay.

        The value is checked first (ValueError, and nothing is read or sent). Then, under the
        shared lock, the terminal's record is read again from the host table, and the change is
        planned from that read: the terminal page's whole form is sent with every other setting
        as it was read, apart from the rename, which sends the name alone as the identification
        page does. A binding target is looked up again from the AP list and the radio list, and
        enables binding in the same request. A schedule edit is a series of requests whose
        steps stop at the first failure; after an acknowledged step that is a
        Ac100TerminalPartialError. A refusal of the current state is an Ac100TerminalError.
        """
        field = TerminalField(field)
        value = validate_terminal_value(field, value)
        mac = _terminal_mac(mac)
        async with self._lock:
            self._ensure_open()
            try:
                await self._set_terminal_field(mac, field, value)
            except TerminalRefusal as err:
                raise Ac100TerminalError(str(err)) from None

    async def _set_terminal_field(
        self, mac: str, field: TerminalField, value: bool | int | str
    ) -> None:
        records = await self._terminal_records()
        if (record := records.get(mac)) is None:
            raise TerminalRefusal(NO_RECORD)
        if field in SCHEDULE_FIELDS:
            await self._set_terminal_schedule(record, records, field, value)
            return
        if field is TerminalField.NAME:
            if record.get("hostname") == value:
                return
            request = host_name_request(record, str(value))
        else:
            if (changes := await self._terminal_changes(record, field, value)) is None:
                return
            request = host_update_request(record, host_update_para(record, **changes))
        _LOGGER.debug("Controller management request: terminal setting (%s)", field.value)
        await self._request(request)

    async def _terminal_changes(
        self, record: HostRecord, field: TerminalField, value: bool | int | str
    ) -> dict[str, Any] | None:
        """The form settings that a change sets, or None if the record already has them."""
        if field is TerminalField.RATE_LIMIT_ENABLED:
            if value:
                return None if record.get("limit") == "1" else {"limit": "1"}
            # Switching the limit off sets both caps to 0, as the page does (T L2115-L2119).
            off = {"limit": "0", "up_limit": "0", "down_limit": "0"}
            return None if all(record.get(key) == token for key, token in off.items()) else off
        if field in {TerminalField.UPLOAD_LIMIT, TerminalField.DOWNLOAD_LIMIT}:
            if record.get("limit") != "1":
                raise TerminalRefusal(RATE_LIMIT_IS_OFF)
            key = "up_limit" if field is TerminalField.UPLOAD_LIMIT else "down_limit"
            return None if record.get(key) == str(value) else {key: str(value)}
        if record.service is None or record.offline:
            raise TerminalRefusal(BINDING_NOT_EDITABLE)
        if record.mac not in await self._present_terminals():
            raise TerminalRefusal(BINDING_NOT_EDITABLE)
        if field is TerminalField.FORCE_BINDING:
            if record.get("bind_enable") != "on":
                raise TerminalRefusal(BINDING_IS_OFF)
            token = "on" if value else "off"
            if record.get("force_bind_enable") == token:
                return None
            if (
                value
                and target_for_radios(saved_radio_ids(record), await self._fresh_targets(record))
                is None
            ):
                raise TerminalRefusal(BINDING_TARGET_GONE)
            return {"force_bind_enable": token}
        saved = saved_radio_ids(record)
        if field is TerminalField.BINDING_ENABLED:
            if not value:
                return None if record.get("bind_enable") == "off" else {"bind_enable": "off"}
            if record.get("bind_enable") == "on":
                return None
            target = target_for_radios(saved, await self._fresh_targets(record))
            if target is None:
                raise TerminalRefusal(BINDING_TARGET_NEEDED)
        else:
            target = next(
                (item for item in await self._fresh_targets(record) if item.token == value), None
            )
            if target is None:
                raise TerminalRefusal(BINDING_TARGET_GONE)
            if record.get("bind_enable") == "on" and frozenset(saved or ()) == frozenset(
                target.radio_ids
            ):
                return None
        return {"bind_enable": "on", "bind_radio_id": list(target.radio_ids)}

    async def _set_terminal_schedule(
        self,
        record: HostRecord,
        records: dict[str, HostRecord],
        field: TerminalField,
        value: bool | int | str,
    ) -> None:
        """Edit the allowed-time schedule of a record, through the time object that holds it.

        The page makes a time object when a terminal first gets a schedule and links it to the
        record, changes it in place, and unlinks and then deletes it for any time (T L2043-L2120,
        L1217-L1236). Only an object that the terminal page made for this MAC and that no other
        record links to is changed or deleted. Every step is sent once, and the sequence stops
        at a step that fails.
        """
        ids = time_object_ids(record)
        if ids is None or len(ids) > 1:
            raise TerminalRefusal(SCHEDULE_UNREADABLE)
        current_id = ids[0] if ids else None
        current = None
        if current_id is not None:
            row = await self._read_time_object(current_id)
            try:
                current = parse_terminal_schedule(row)
            except ValueError:
                raise TerminalRefusal(SCHEDULE_UNREADABLE) from None
            if not is_terminal_time_object(row, record.mac) or not time_object_is_exclusive(
                current_id, record, records.values()
            ):
                raise TerminalRefusal(SCHEDULE_NOT_OWNED)
        try:
            target = change_terminal_schedule(current, field.value, value)
        except ValueError as err:
            # The text was checked before any read; this is what the schedule as read now forbids.
            raise TerminalRefusal(str(err)) from None
        if target == current:
            return
        _LOGGER.debug("Controller management request: terminal schedule (%s)", field.value)
        if target is None:
            link = host_update_request(record, host_update_para(record, time_obj_array=[]))
            await self._request(link)
            await self._follow_up(time_object_delete_request(current_id), PARTIAL_UNLINK, "unlink")
        elif current_id is None:
            link = host_update_para(record, time_obj_array=[])
            created = await self._request(
                time_object_add_request(terminal_time_object_para(record.mac, target))
            )
            new_id = new_time_object_id(created)
            if new_id is None or any(
                new_id in (time_object_ids(other) or ()) for other in records.values()
            ):
                raise Ac100TerminalPartialError(PARTIAL_LINK, "add")
            link["time_obj_array"] = [new_id]
            await self._follow_up(host_update_request(record, link), PARTIAL_LINK, "link")
        else:
            # The editor updates the object, then submits the host form (T L2043-L2101).
            host = host_update_request(record, host_update_para(record))
            await self._request(
                time_object_set_request(current_id, terminal_time_object_para(record.mac, target))
            )
            await self._follow_up(host, PARTIAL_UPDATE, "update")

    async def _follow_up(self, request: dict[str, Any], reason: str, stage: str) -> None:
        """Send the next step of a series whose earlier step was acknowledged.

        Whatever goes wrong with it, the series has partly happened: it is reported as such,
        and neither this step nor an earlier one is sent again or undone.
        """
        try:
            await self._request(request)
        except Ac100Error as err:
            raise Ac100TerminalPartialError(reason, stage) from err

    async def async_disconnect_terminal(self, mac: str) -> None:
        """Disconnect one connected terminal, once, without deleting its saved record.

        The terminal page sends the MAC (T L753-L760). The client list is read again under the
        shared lock, and nothing is sent unless the terminal is in it now. It may reconnect.
        """
        mac = _terminal_mac(mac)
        async with self._lock:
            self._ensure_open()
            if (wire_mac := (await self._present_terminals()).get(mac)) is None:
                raise Ac100TerminalError(NOT_CONNECTED)
            _LOGGER.debug("Controller management request: terminal disconnect")
            await self._request(host_disconnect_request(wire_mac))

    async def async_clear_terminal(self, mac: str, *, disconnect: bool) -> None:
        """Delete the saved record of one terminal and the schedule that only it uses.

        ``disconnect`` says that the terminal is connected, which disconnects it too, and
        otherwise that it is away; the host table and the client list are read again under
        the shared lock, and both must agree. The page deletes the record first and then its
        time object (T L1780-L1807, L2306-L2356). If the second step fails after the first
        was acknowledged, that is an Ac100TerminalPartialError and nothing is retried. The
        record cannot be restored, and neither can the schedule.
        """
        mac = _terminal_mac(mac)
        async with self._lock:
            self._ensure_open()
            try:
                await self._clear_terminal(mac, disconnect)
            except TerminalRefusal as err:
                raise Ac100TerminalError(str(err)) from None

    async def _clear_terminal(self, mac: str, disconnect: bool) -> None:
        records = await self._terminal_records()
        if (record := records.get(mac)) is None:
            raise TerminalRefusal(NO_RECORD)
        if disconnect != (mac in await self._present_terminals()) or disconnect != record.online:
            raise TerminalRefusal(NOT_CONNECTED if disconnect else STILL_CONNECTED)
        ids = time_object_ids(record)
        if ids is None or len(ids) > 1:
            raise TerminalRefusal(SCHEDULE_UNREADABLE)
        # A time object that the terminal does not own alone is not this action's to delete.
        object_id = None
        if ids:
            row = await self._read_time_object(ids[0])
            if is_terminal_time_object(row, mac) and time_object_is_exclusive(
                ids[0], record, records.values()
            ):
                object_id = ids[0]
        _LOGGER.debug("Controller management request: terminal record clear")
        await self._request(host_delete_request(record))
        if object_id is not None:
            await self._follow_up(time_object_delete_request(object_id), PARTIAL_CLEAR, "clear")
