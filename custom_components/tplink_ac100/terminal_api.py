"""What the controller's terminal pages mean, and the requests that change them.

This is the wire logic of the saved terminal configuration, kept apart from api.py's network
code: it does no I/O, has no Home Assistant code and holds no state. Each rule names the page
that it comes from; T is userrpm__wireless_terminal_mngr.htm and I is
userrpm__wireless_terminal_identify.htm, and L is a line of their source.

A terminal's saved record is a row of the host table. The same MAC can have more than one row;
they are one record only while they agree on every setting that a write sends back.
"""

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from .models import AccessPoint
from .terminal import (
    TERMINAL_LIMIT_MAX,
    TERMINAL_LIMIT_MIN,
    TerminalBindingTarget,
    TerminalConfiguration,
    terminal_name_is_valid,
)
from .terminal_schedule import TerminalSchedule

HOST_MODULE = "host_management"
HOST_TABLE = "host_info"
RADIO_BIND_MODULE = "apmng_wserv"
RADIO_BIND_TABLE = "radio_bind_list"
TIME_MODULE = "time_mngt"
TIME_TABLE = "time_obj"

# The settings that the terminal editor sends back (T L1195-L1216), in its order. The hostname
# is the editor's name field, so a record without a usable one has no name to send back.
_FORM_KEYS = (
    "hostname",
    "limit",
    "up_limit",
    "down_limit",
    "bind_enable",
    "force_bind_enable",
    "bind_radio_id",
    "time_obj_array",
)
# A record also needs its service: a negative one is an offline record (T L1904-L1909).
_RECORD_KEYS = (*_FORM_KEYS, "serv_id", "mac")
_MISSING = object()

# The binding editor offers the 2.4 GHz and 5 GHz radios of an AP, 2.4 first (T L718-L731).
_BANDS = ("2.4", "5")
_DIGITS = re.compile(r"[0-9]{1,9}")
_CAP = re.compile(r"0|[1-9][0-9]{0,5}")
_SERVICE = re.compile(r"-?[0-9]{1,9}")
_TIME_OBJECT_NAME = re.compile(r"time_obj_([0-9]{1,9})")

# Fixed reasons for a refusal. They hold no name, address or identifier, so they can be shown.
NO_RECORD = "The controller has no saved record for this terminal"
CONFLICTING_RECORDS = "The controller lists this terminal more than once with different settings"
UNSENDABLE_RECORD = "The saved settings of this terminal cannot be sent back unchanged"
NOT_CONNECTED = "The terminal is not connected"
STILL_CONNECTED = "The terminal is connected"
BINDING_NOT_EDITABLE = "Binding can only be changed for a connected terminal"
BINDING_TARGET_GONE = "That binding target is not available for this terminal now"
BINDING_TARGET_NEEDED = "Choose a binding target first"
BINDING_IS_OFF = "Turn binding on first"
RATE_LIMIT_IS_OFF = "Turn rate limiting on first"
SCHEDULE_UNREADABLE = "The saved schedule of this terminal could not be read"
SCHEDULE_NOT_OWNED = "The saved schedule is not used by this terminal alone"
PARTIAL_LINK = "The schedule was created but could not be linked to the terminal"
PARTIAL_UNLINK = "The terminal no longer uses its schedule, but the schedule was not deleted"
PARTIAL_CLEAR = "The terminal's saved record was cleared, but its schedule was not deleted"
PARTIAL_UPDATE = "The schedule was changed, but the terminal's saved record could not be updated"


class TerminalRefusal(Exception):
    """A change that must not be sent; the message is one of the fixed reasons above."""


@dataclass(frozen=True, slots=True)
class HostRecord:
    """The saved record of one MAC, as its rows of the host table report it.

    ``wire_mac`` is the MAC as the controller spells it, which is what a request must name.
    ``values`` holds the form settings and the service exactly as read; one that the rows do
    not have is missing. ``conflicts`` names the settings on which duplicate rows differ.
    ``online`` is whether a row names the AP that the terminal is on, which is how the terminal
    page tells a connected terminal from a retained record (T L1523).
    """

    mac: str
    wire_mac: str
    values: Mapping[str, Any]
    conflicts: frozenset[str]
    online: bool

    def get(self, key: str) -> Any:
        """A setting as read, or None if it is missing or the duplicate rows disagree."""
        return None if key in self.conflicts else self.values.get(key)

    @property
    def service(self) -> int | None:
        """The service of the connection; negative for an offline record, None if unknown."""
        return _service(self.get("serv_id"))

    @property
    def offline(self) -> bool:
        return (service := self.service) is not None and service < 0

    def require_consistent(self) -> None:
        if self.conflicts:
            raise TerminalRefusal(CONFLICTING_RECORDS)


@dataclass(frozen=True, slots=True)
class RadioBinding:
    """One radio of an AP in the controller's radio binding list.

    ``radio_id`` is the radio's command address, valid only as read. The AP is its MAC, found
    from that address and the AP list of the same read. ``eligible`` is the list's own
    statement that the service is bound to the radio.
    """

    radio_id: str
    unit: str
    band: str
    ap_mac: str
    eligible: bool


def _digits(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, str) and _DIGITS.fullmatch(value):
        return int(value)
    return None


def _service(value: Any) -> int | None:
    if isinstance(value, str) and _SERVICE.fullmatch(value):
        return int(value)
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _ids(value: Any) -> tuple[str, ...] | None:
    """Radio or time object addresses as the controller lists them; None if not readable."""
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list | tuple):
        return None
    ids = [_digits(item) for item in value]
    return None if None in ids else tuple(str(item) for item in ids)


def _switch(value: Any) -> bool | None:
    return {"on": True, "off": False}.get(value) if isinstance(value, str) else None


def _limit(value: Any) -> bool | None:
    return {"0": False, "1": True}.get(value) if isinstance(value, str) else None


def ap_directory(aps: Iterable[AccessPoint]) -> tuple[dict[str, str], dict[str, str]]:
    """The MAC of each AP by command address, and the name of each AP by MAC.

    An address that two APs claim names none of them.
    """
    entry_macs: dict[str, str] = {}
    ap_names: dict[str, str] = {}
    claimed_twice: set[str] = set()
    for ap in aps:
        if entry_macs.setdefault(ap.entry_id, ap.mac) != ap.mac:
            claimed_twice.add(ap.entry_id)
        ap_names[ap.mac] = ap.name
    return (
        {entry: mac for entry, mac in entry_macs.items() if entry not in claimed_twice},
        ap_names,
    )


def merge_host_rows(mac: str, rows: Sequence[Mapping[str, Any]]) -> HostRecord:
    """The record of ``mac`` from all its rows; rows that differ in a setting conflict on it."""
    first = rows[0]
    conflicts = {
        key
        for key in _RECORD_KEYS
        if any(row.get(key, _MISSING) != first.get(key, _MISSING) for row in rows[1:])
    }
    return HostRecord(
        mac=mac,
        wire_mac=str(first.get("mac")),
        values={key: first[key] for key in _RECORD_KEYS if key in first},
        conflicts=frozenset(conflicts),
        online=any(isinstance(row.get("ap_name"), str) and row["ap_name"].strip() for row in rows),
    )


def time_object_ids(record: HostRecord) -> tuple[str, ...] | None:
    """The time objects that the record links to; None if that is not readable."""
    return _ids(record.get("time_obj_array"))


def time_object_is_exclusive(
    object_id: str, owner: HostRecord, records: Iterable[HostRecord]
) -> bool:
    """Whether every other record is known not to link to this object."""
    return all(
        (ids := time_object_ids(other)) is not None and object_id not in ids
        for other in records
        if other.mac != owner.mac
    )


def radio_bind_filter(service: int | None) -> list[dict[str, Any]]:
    """The filter of the radio list: the radios that carry a service, or all radios.

    An offline record has no service, so the page asks for every radio with service 0 and
    without the bind status (T L786-L806).
    """
    if service is None:
        return [{"group_id": ["-1"], "serv_id": "0"}]
    return [{"group_id": ["-1"], "bind_status": "1", "serv_id": str(service)}]


def parse_radio_bind_rows(
    rows: Iterable[Mapping[str, Any]], entry_macs: Mapping[str, str]
) -> tuple[RadioBinding, ...]:
    """The radios of the list that belong to a known AP; ValueError if the list is unusable.

    A radio's address is its AP's command address times 16 plus the radio's index (T L815,
    I L186-L190), so the AP is the one that the AP list gives that address. A radio of an AP
    that the list does not give is dropped. A row or pair of rows that cannot be told apart
    makes the whole list unusable.
    """
    radios: dict[str, RadioBinding] = {}
    units: set[tuple[str, str]] = set()
    for row in rows:
        if row.get("ap_type") == "template":
            continue
        radio_id, unit = _digits(row.get("radio_id")), _digits(row.get("freq_unit"))
        if radio_id is None or unit is None or radio_id & 15 != unit or str(radio_id) in radios:
            raise ValueError("Invalid radio binding data")
        band = row.get("freq_name")
        ap_mac = entry_macs.get(str(radio_id >> 4), "")
        radios[str(radio_id)] = RadioBinding(
            str(radio_id),
            str(unit),
            band.strip() if isinstance(band, str) else "",
            ap_mac,
            row.get("bind_status") == "1",
        )
        if ap_mac:
            if (ap_mac, str(unit)) in units:
                raise ValueError("Duplicate radio binding identity")
            units.add((ap_mac, str(unit)))
    return tuple(radio for radio in radios.values() if radio.ap_mac)


def binding_targets(
    radios: Iterable[RadioBinding], ap_names: Mapping[str, str]
) -> tuple[TerminalBindingTarget, ...]:
    """The targets that the binding editor offers: an AP's eligible 2.4 and 5 GHz radios.

    An AP offers each such radio, and all of them together when it has more than one
    (T L718-L731). The identity is the AP's MAC and the radio's unit, never an address or a name.
    """
    by_ap: dict[str, list[RadioBinding]] = {}
    for radio in radios:
        if radio.eligible and any(band in radio.band for band in _BANDS):
            by_ap.setdefault(radio.ap_mac, []).append(radio)
    targets: list[TerminalBindingTarget] = []
    for ap_mac, items in by_ap.items():
        ordered = [radio for band in _BANDS for radio in items if band in radio.band]
        name = ap_names.get(ap_mac, "")
        if len(ordered) > 1:
            ids = tuple(radio.radio_id for radio in ordered)
            targets.append(TerminalBindingTarget(f"{ap_mac}/all", ap_mac, name, None, "", ids))
        targets.extend(
            TerminalBindingTarget(
                f"{ap_mac}/{radio.unit}", ap_mac, name, radio.unit, radio.band, (radio.radio_id,)
            )
            for radio in ordered
        )
    return tuple(targets)


def target_for_radios(
    radio_ids: Iterable[str] | None, targets: Iterable[TerminalBindingTarget]
) -> TerminalBindingTarget | None:
    """The one target whose radios are exactly ``radio_ids``."""
    if not radio_ids:
        return None
    wanted = frozenset(radio_ids)
    found = [target for target in targets if frozenset(target.radio_ids) == wanted]
    return found[0] if len(found) == 1 else None


def saved_radio_ids(record: HostRecord) -> tuple[str, ...] | None:
    """The radio addresses that the record has saved; empty if none, None if unreadable."""
    return _ids(record.get("bind_radio_id"))


def _bound_names(
    radio_ids: tuple[str, ...] | None,
    radios: Mapping[str, RadioBinding],
    ap_names: Mapping[str, str],
) -> tuple[str | None, str | None]:
    """The AP and the bands that saved radio addresses are now; None if unknown or mixed."""
    found = [radios.get(radio_id) for radio_id in radio_ids or ()]
    known = [radio for radio in found if radio is not None]
    if not found or len(known) != len(found) or len({radio.ap_mac for radio in known}) != 1:
        return None, None
    bands = dict.fromkeys(radio.band for radio in known if radio.band)
    return ap_names.get(known[0].ap_mac) or None, ", ".join(bands) or None


def build_configuration(
    record: HostRecord,
    *,
    targets: tuple[TerminalBindingTarget, ...] | None,
    radios: Mapping[str, RadioBinding],
    ap_names: Mapping[str, str],
    schedules: Mapping[str, TerminalSchedule | None],
) -> TerminalConfiguration:
    """The saved configuration of a record; every setting that is not readable is unknown.

    ``targets`` are the radio targets of the record's service, None if they could not be read.
    ``radios`` are the radios read so far by address, which name the saved binding, and
    ``schedules`` the schedules read so far by time object address, None for one that could
    not be read.
    """
    name = record.get("hostname")
    saved = saved_radio_ids(record)
    bound_ap, bound_radio = _bound_names(saved, radios, ap_names)
    service = record.service
    available = targets is not None and service is not None and service >= 0
    match = target_for_radios(saved, targets or ()) if available else None
    mode, schedule, schedule_available = _schedule_of(record, schedules)
    return TerminalConfiguration(
        mac=record.mac,
        name=name if isinstance(name, str) and name.strip() else None,
        rate_limit_enabled=_limit(record.get("limit")),
        upload_limit=_digits(record.get("up_limit")),
        download_limit=_digits(record.get("down_limit")),
        binding_enabled=_switch(record.get("bind_enable")),
        force_binding=_switch(record.get("force_bind_enable")),
        bound_radio_ids=saved,
        binding_targets=(targets or ()) if available else (),
        binding_available=available,
        bound_ap=bound_ap,
        bound_radio=bound_radio,
        bound_target=match.token if match else None,
        allowed_time_mode=mode,
        schedule=schedule,
        schedule_available=schedule_available,
    )


def _schedule_of(
    record: HostRecord, schedules: Mapping[str, TerminalSchedule | None]
) -> tuple[str | None, TerminalSchedule | None, bool]:
    """The allowed-time mode, schedule and whether they are known: no object is any time."""
    ids = time_object_ids(record)
    if ids is None or len(ids) > 1:
        return None, None, False
    if not ids:
        return "any", None, True
    if (schedule := schedules.get(ids[0])) is None:
        return None, None, False
    return schedule.mode, schedule, True


def _echo(record: HostRecord, key: str) -> Any:
    """A setting of the record as the form sends it back, or _MISSING if there is none.

    A setting that the page could not have sent refuses the form. The name is the exception:
    one that the page would not accept is left out, which leaves the saved name as it is.
    """
    if key not in record.values:
        return _MISSING
    raw = record.values[key]
    if key == "hostname":
        return raw if terminal_name_is_valid(raw) else _MISSING
    if key in {"bind_radio_id", "time_obj_array"}:
        ids = _ids(raw)
        if ids is None or (key == "time_obj_array" and len(ids) > 1):
            raise TerminalRefusal(UNSENDABLE_RECORD)
        return list(ids)
    if key == "limit":
        valid = _limit(raw) is not None
    elif key in {"up_limit", "down_limit"}:
        valid = isinstance(raw, str) and _CAP.fullmatch(raw) is not None and cap_is_valid(int(raw))
    else:
        valid = _switch(raw) is not None
    if not valid:
        raise TerminalRefusal(UNSENDABLE_RECORD)
    return raw


def cap_is_valid(value: int) -> bool:
    """The terminal page's rule for a cap: 0, or 15 to 125000 KB/s (T L913-L918)."""
    return value == 0 or TERMINAL_LIMIT_MIN <= value <= TERMINAL_LIMIT_MAX


def host_update_para(record: HostRecord, **changes: Any) -> dict[str, Any]:
    """The terminal editor's complete form for ``record``, with ``changes`` applied.

    It is the editor's own set of fields in its own shape (T L1195-L1216, L2099-L2119): each
    setting as the record has it, except what ``changes`` sets, and the MAC. The form never
    invents a setting: one that the record lacks stays out. An offline record has no binding
    fields, as the editor disables them (T L1904-L1909), and the radios are only sent while
    binding is on. Duplicate rows that disagree, or a setting that the page could not have
    sent, refuse the form.
    """
    record.require_consistent()
    para: dict[str, Any] = {}
    for key in _FORM_KEYS:
        if record.offline and key in {"bind_enable", "force_bind_enable", "bind_radio_id"}:
            continue
        if key == "bind_radio_id" and para.get("bind_enable") != "on":
            continue
        value = changes[key] if key in changes else _echo(record, key)
        if value is not _MISSING:
            para[key] = value
    para["mac"] = record.wire_mac
    if not para.get("bind_radio_id"):
        para.pop("bind_radio_id", None)
    return para


def host_update_request(record: HostRecord, para: Mapping[str, Any]) -> dict[str, Any]:
    """A ``set`` of the host table row of the record's MAC (T L1021-L1025)."""
    return {
        "method": "set",
        HOST_MODULE: {
            "table": HOST_TABLE,
            "filter": [{"mac": record.wire_mac}],
            "para": dict(para),
        },
    }


def host_name_request(record: HostRecord, name: str) -> dict[str, Any]:
    """The rename of the terminal identification page, which sends the name alone (I L334-L341)."""
    return host_update_request(record, {"hostname": name})


def host_disconnect_request(wire_mac: str) -> dict[str, Any]:
    """The terminal page's disconnect of a connected terminal, by MAC (T L753-L760)."""
    return {"method": "do", HOST_MODULE: {"disconnect": {"mac_list": [wire_mac]}}}


def host_delete_request(record: HostRecord) -> dict[str, Any]:
    """The terminal page's deletion of a saved record, by MAC (T L1786-L1797)."""
    return {
        "method": "delete",
        HOST_MODULE: {"table": HOST_TABLE, "filter": [{"mac": record.wire_mac}]},
    }


def time_object_read_request(object_id: str) -> dict[str, Any]:
    """The terminal page's read of the time object that a record links to (T L1933-L1941)."""
    return {"method": "get", TIME_MODULE: {"table": TIME_TABLE, "filter": [{"id": object_id}]}}


def time_object_add_request(para: Mapping[str, Any]) -> dict[str, Any]:
    return {"method": "add", TIME_MODULE: {"table": TIME_TABLE, "para": dict(para)}}


def time_object_set_request(object_id: str, para: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "method": "set",
        TIME_MODULE: {"table": TIME_TABLE, "filter": [{"id": object_id}], "para": dict(para)},
    }


def time_object_delete_request(object_id: str) -> dict[str, Any]:
    return {"method": "delete", TIME_MODULE: {"table": TIME_TABLE, "filter": [{"id": object_id}]}}


def new_time_object_id(response: Mapping[str, Any]) -> str | None:
    """The address of the time object that an ``add`` made, or None if the answer has none.

    The page takes it from the first name of the answer, which reads ``time_obj_`` and the
    address (T L2075). Nothing else is accepted as one.
    """
    section = response.get(TIME_MODULE)
    names = section.get("name") if isinstance(section, Mapping) else None
    if not isinstance(names, list) or not names or not isinstance(names[0], str):
        return None
    found = _TIME_OBJECT_NAME.fullmatch(names[0])
    return found[1] if found else None
