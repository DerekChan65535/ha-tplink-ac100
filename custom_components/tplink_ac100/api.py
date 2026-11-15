"""Asynchronous client for the AC100's local SLP API.

Management calls are explicit. Polling never changes controller configuration.
"""

import asyncio
import hashlib
import json
import logging
import math
import re
from typing import Any
from urllib.parse import quote, unquote, urlsplit

import aiohttp

from .models import AccessPoint, ApRadio, Client, ClientRadio, Controller, Snapshot

_LOGGER = logging.getLogger(__name__)

_PAGE_SIZE = 10
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


class _SessionExpiredError(Ac100AuthError):
    """A request was definitively rejected because its session is invalid."""


class Ac100ConnectionError(Ac100Error):
    """The controller could not be reached."""


class Ac100ProtocolError(Ac100Error):
    """The controller returned an unsupported or malformed response."""


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
        led_enabled={"1": True, "0": False}.get(str(row.get("led_status"))),
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
    )


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
        if code == -40401:
            if path != "/" and status != 403 and (subcode is None or subcode == -40401):
                raise _SessionExpiredError("Controller session has expired")
            raise Ac100AuthError("Controller authentication was rejected")
        if status in {401, 403} or code in {-40100, -40404, -57703, -57704, -57705}:
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
            except _SessionExpiredError:
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
        except _SessionExpiredError:
            _LOGGER.debug("Controller session expired; reauthenticating once")
            self._stok = None
            await self._login()
            self._ensure_open()
            return await self._post(f"/stok={quote(self._stok, safe='')}/ds", data)

    async def _table(
        self, module: str, table: str, filters: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        start = 0
        target: int | None = None
        while target is None or start < target:
            result = await self._request(
                {
                    "method": "get",
                    module: {
                        "table": table,
                        "filter": filters,
                        "para": {"start": start, "end": start + _PAGE_SIZE - 1},
                    },
                }
            )
            count = _integer(_section(result, module, "count").get(table))
            if count is None or count < 0:
                raise Ac100ProtocolError("Controller table count is invalid")
            target = count if target is None else min(target, count)
            page = _rows(result, module, table)
            if len(page) < min(_PAGE_SIZE, max(0, target - start)):
                raise Ac100ProtocolError("Controller returned an incomplete table")
            rows.extend(page)
            start += _PAGE_SIZE
        return rows

    async def async_get_snapshot(self) -> Snapshot:
        """Poll controller, APs and every client page before publishing a snapshot."""
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
            ap_rows = await self._table(
                "apmng_status", "ap_list", [{"group_id": ["-1"], "ap_status": ["0"]}]
            )
            client_rows = await self._table("apmng_client", "client_list", [{"group_id": ["-1"]}])
            try:
                aps = {_mac(row.get("mac")): _parse_ap(row) for row in ap_rows}
                clients = {_mac(row.get("mac")): _parse_client(row) for row in client_rows}
            except KeyError, TypeError, AttributeError:
                raise Ac100ProtocolError("Controller returned malformed device telemetry") from None
            _LOGGER.debug(
                "Controller snapshot received (aps=%s, clients=%s)", len(aps), len(clients)
            )
            return Snapshot(
                controller=controller, aps=tuple(aps.values()), clients=tuple(clients.values())
            )

    async def async_set_ap_led(self, entry_id: str, enabled: bool) -> None:
        """Set one AP LED; do not retry on ambiguous network failures."""
        async with self._lock:
            self._ensure_open()
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
