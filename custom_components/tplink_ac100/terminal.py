"""Saved per-terminal configuration and the fields a terminal write may change.

The controller's Wireless Terminal Management table keeps one saved record per terminal MAC. It
outlives the terminal's visit, so it says nothing about who is connected: presence comes only
from the client list. Nothing here holds a raw controller row. This module does no I/O and does
not depend on Home Assistant; api.py reads and writes, and the entities only use these types.
"""

import unicodedata
from dataclasses import dataclass
from enum import StrEnum

from .terminal_schedule import (
    TerminalSchedule,
    parse_calendar_day_text,
    parse_windows_text,
)

# The terminal page's own limits (userrpm__wireless_terminal_mngr.htm L909-L947): a cap is 0, which
# is unlimited, or a whole number of KB/s from 15 to 125000. The name is 1 to 64 bytes (L96-L122).
TERMINAL_LIMIT_MIN = 15
TERMINAL_LIMIT_MAX = 125000
TERMINAL_NAME_MAX_BYTES = 64

LIMIT_RULE = (
    f"Enter 0 for no limit, or a whole number from {TERMINAL_LIMIT_MIN} to {TERMINAL_LIMIT_MAX}"
)
NAME_RULE = f"The name must be 1 to {TERMINAL_NAME_MAX_BYTES} visible bytes"
TIME_MODES = ("any", "manual", "calendar")

# The page's string_visible_allow_blank type (js__su__su.js): non-whitespace characters, the plain
# space and the ideographic space. JavaScript's \s covers these whitespace characters.
_WHITESPACE = frozenset("\t\n\v\f\r\u00a0\u1680\u2028\u2029\u202f\u205f\ufeff") | frozenset(
    chr(code) for code in range(0x2000, 0x200B)
)
_INVISIBLE = frozenset({"Cc", "Cs", "Cn", "Zl", "Zp"})


class TerminalField(StrEnum):
    """One changeable setting of one terminal."""

    NAME = "name"
    RATE_LIMIT_ENABLED = "rate_limit_enabled"
    UPLOAD_LIMIT = "upload_limit"
    DOWNLOAD_LIMIT = "download_limit"
    BINDING_ENABLED = "binding_enabled"
    FORCE_BINDING = "force_binding"
    BINDING_TARGET = "binding_target"
    ALLOWED_TIME_MODE = "allowed_time_mode"
    MANUAL_WINDOWS = "manual_windows"
    MANUAL_MONDAY = "manual_monday"
    MANUAL_TUESDAY = "manual_tuesday"
    MANUAL_WEDNESDAY = "manual_wednesday"
    MANUAL_THURSDAY = "manual_thursday"
    MANUAL_FRIDAY = "manual_friday"
    MANUAL_SATURDAY = "manual_saturday"
    MANUAL_SUNDAY = "manual_sunday"
    CALENDAR_MONDAY = "calendar_monday"
    CALENDAR_TUESDAY = "calendar_tuesday"
    CALENDAR_WEDNESDAY = "calendar_wednesday"
    CALENDAR_THURSDAY = "calendar_thursday"
    CALENDAR_FRIDAY = "calendar_friday"
    CALENDAR_SATURDAY = "calendar_saturday"
    CALENDAR_SUNDAY = "calendar_sunday"


# The controller keeps the allowed-time schedule in a time object of its own, apart from the
# terminal record. terminal_schedule.py holds the rules for its content.
SCHEDULE_FIELDS = frozenset(
    field
    for field in TerminalField
    if field is TerminalField.ALLOWED_TIME_MODE or field.startswith(("manual_", "calendar_"))
)
_SWITCH_FIELDS = frozenset(
    {TerminalField.RATE_LIMIT_ENABLED, TerminalField.BINDING_ENABLED, TerminalField.FORCE_BINDING}
)
_LIMIT_FIELDS = frozenset({TerminalField.UPLOAD_LIMIT, TerminalField.DOWNLOAD_LIMIT})


@dataclass(frozen=True, slots=True)
class TerminalBindingTarget:
    """An AP, or one radio of an AP, that a terminal can be bound to.

    ``token`` is the stable identity that a write names: the AP's MAC and the radio's unit, or
    ``all``. It is never a radio's command address, which the controller can reuse.
    ``radio_unit`` is None for the target that covers every radio of the AP. ``radio_name`` is
    the controller's band name (such as 5GHz), and empty for that all-radios target.
    ``radio_ids`` are the controller's radio addresses at read time. They are evidence only: a
    write looks the target up again from its token.
    """

    token: str
    ap_mac: str
    ap_name: str
    radio_unit: str | None
    radio_name: str
    radio_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class TerminalConfiguration:
    """The controller's saved configuration of one terminal, also while the terminal is away.

    None is an unknown value: the controller did not report the field, reported it in a form
    that its page cannot send, or the duplicate records of the MAC disagree. A limit of 0 is a
    real value (unlimited).

    The saved binding is not the connection. ``bound_radio_ids`` are the radio addresses as
    saved (evidence; the controller can reuse them). ``bound_ap`` and ``bound_radio`` name what
    they are now, and are None when that is unknown or spans more than one AP. ``bound_target``
    is the token of the one entry of ``binding_targets`` with exactly the saved radios.
    ``binding_targets`` are the targets offered by the source as of this read.
    ``binding_available`` describes source eligibility/readability only: the saved service is
    not offline and its targets were read. It does not declare presence or overall permission.
    Native controls also require current client-list presence; writes check that presence again.

    ``allowed_time_mode`` is any (no time object, all time), manual or calendar, and ``schedule``
    is the linked time object, None for any. ``schedule_available`` is False when the mode and
    schedule could not be read, so that they are never shown as all time.
    """

    mac: str
    name: str | None = None
    rate_limit_enabled: bool | None = None
    upload_limit: int | None = None
    download_limit: int | None = None
    binding_enabled: bool | None = None
    force_binding: bool | None = None
    bound_radio_ids: tuple[str, ...] | None = None
    binding_targets: tuple[TerminalBindingTarget, ...] = ()
    binding_available: bool = False
    bound_ap: str | None = None
    bound_radio: str | None = None
    bound_target: str | None = None
    allowed_time_mode: str | None = None
    schedule: TerminalSchedule | None = None
    schedule_available: bool = False


@dataclass(frozen=True, slots=True)
class TerminalState:
    """The saved record of every terminal, including terminals that are away.

    There is one configuration per MAC. An empty tuple is a table that was read and holds no
    record, which differs from ``Snapshot.terminals`` being None: the table could not be read.
    """

    configurations: tuple[TerminalConfiguration, ...] = ()


def terminal_name_is_valid(value: object) -> bool:
    """Whether the terminal page would accept ``value`` as a name.

    The page counts bytes with its own rule; the check here counts the UTF-8 bytes, which is
    never fewer, so a name that passes is also one that the page accepts.
    """
    if not isinstance(value, str) or not value.strip(" \u3000"):
        return False
    if len(value.encode("utf-8", errors="surrogatepass")) > TERMINAL_NAME_MAX_BYTES:
        return False
    return all(
        (char in {" ", "\u3000"} or char not in _WHITESPACE)
        and unicodedata.category(char) not in _INVISIBLE
        for char in value
    )


def validate_terminal_value(field: TerminalField, value: object) -> bool | int | str:
    """Check ``value`` against the page's rule for ``field`` and return it as the API takes it.

    Raises ValueError with a message that is fit to show and holds no identity. The text of a
    schedule field is parsed here; what it does to the schedule that it edits is checked
    against that schedule when the API has read it. binding_target is a token that the API
    looks up in the targets that it reads.
    """
    field = TerminalField(field)
    if field in _SWITCH_FIELDS or field.startswith("manual_") and field != field.MANUAL_WINDOWS:
        if not isinstance(value, bool):
            raise ValueError("Use on or off")
        return value
    if field in _LIMIT_FIELDS:
        if (
            isinstance(value, bool)
            or not isinstance(value, int | float)
            or (isinstance(value, float) and not value.is_integer())
            or (int(value) != 0 and not TERMINAL_LIMIT_MIN <= int(value) <= TERMINAL_LIMIT_MAX)
        ):
            raise ValueError(LIMIT_RULE)
        return int(value)
    if field is TerminalField.NAME:
        if not terminal_name_is_valid(value):
            raise ValueError(NAME_RULE)
        return str(value)
    if field is TerminalField.ALLOWED_TIME_MODE:
        if value not in TIME_MODES:
            raise ValueError(f"Choose one of: {', '.join(TIME_MODES)}")
        return str(value)
    if not isinstance(value, str):
        raise ValueError("Enter text")
    if field is TerminalField.BINDING_TARGET:
        if not value:
            raise ValueError("Choose a binding target")
    elif field is TerminalField.MANUAL_WINDOWS:
        parse_windows_text(value)
    else:
        parse_calendar_day_text(value)
    return value
