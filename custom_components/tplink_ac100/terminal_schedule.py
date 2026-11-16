"""Allowed-time schedule of a wireless terminal, as the controller's terminal page edits it.

This module is pure data and rules: it has no network or Home Assistant code. Times are the
controller's own local clock and are used exactly as the controller shows them; nothing is
converted to or from the Home Assistant time zone.

A terminal without a time object may use the network at any time. That "any" belongs to the
terminal, not to a time object, so it is the absence of a schedule and has no value here.
"""

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from typing import Any

# The manual weekday mask has Monday as bit 0 and Sunday as bit 6.
DAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
ANY = "any"
MANUAL = "manual"
CALENDAR = "calendar"
MODES = (MANUAL, CALENDAR)
MAX_WINDOWS = 12
# The terminal page flags its time objects with this, which hides them from the time management
# page, and names each one by the terminal's MAC address without separators.
OWNED_FLAG = "inner"

_TIME_TYPE = "section"
_WIRE_DAYS = tuple(day[:3] for day in DAYS)
_TIME = r"([0-9]{1,2}):([0-9]{2})(?::([0-9]{2}))?"
_RANGE = re.compile(rf"\s*{_TIME}\s*-\s*{_TIME}\s*")
_MAC_DIGITS = re.compile(r"[0-9A-Fa-f]{12}")


@dataclass(frozen=True, slots=True)
class TerminalSchedule:
    """The schedule of a time object: manual weekdays and windows, or a weekly hour calendar.

    The fields of the other mode are always empty, never invented. Windows are ``HH:MM-HH:MM``
    and calendar ranges ``HH:00-HH:00``; an end of 24:00 is the end of the day. Both are sorted
    and joined where they touch, which is how the terminal page stores them.
    """

    mode: str
    weekdays: tuple[str, ...] = ()
    windows: tuple[str, ...] = ()
    calendar: tuple[tuple[str, ...], ...] = ()

    def __post_init__(self) -> None:
        if self.mode == MANUAL:
            if self.calendar:
                raise ValueError("A manual schedule has no calendar")
            order = [DAYS.index(day) if day in DAYS else -1 for day in self.weekdays]
            if -1 in order or order != sorted(set(order)):
                raise ValueError("Weekdays are distinct day names in week order")
            if _canonical_windows(_ranges(self.windows)) != self.windows:
                raise ValueError("Windows are valid, sorted and joined where they touch")
        elif self.mode == CALENDAR:
            if self.weekdays or self.windows:
                raise ValueError("A calendar schedule has no weekdays or windows")
            if len(self.calendar) != len(DAYS):
                raise ValueError("A calendar has one entry for each day of the week")
            for day in self.calendar:
                if _canonical_day(_hour_ranges(day)) != day:
                    raise ValueError("Calendar ranges are valid, sorted and joined if they touch")
        else:
            raise ValueError("A schedule is manual or calendar")


def default_terminal_schedule(mode: str) -> TerminalSchedule:
    """The schedule that a terminal gets when it is first given a manual or calendar mode.

    Both allow every hour of every day, so choosing a mode alone blocks nothing. For manual this
    is the terminal page's own start: all weekdays and 00:00-24:00 (T L672-L679, L1923-L1931). For
    calendar it is this integration's choice, not a verified picker default: the cached source
    does not include the picker implementation. The page refuses to save an empty calendar.
    """
    if mode == MANUAL:
        return TerminalSchedule(MANUAL, weekdays=DAYS, windows=("00:00-24:00",))
    if mode == CALENDAR:
        return TerminalSchedule(CALENDAR, calendar=(("00:00-24:00",),) * len(DAYS))
    raise ValueError("A new schedule must use manual or calendar mode")


def parse_terminal_schedule(row: Mapping[str, Any]) -> TerminalSchedule:
    """Read a terminal's time object row; ValueError if it is not a readable schedule.

    The row is the time object itself, without the numbered wrapper that the controller puts
    around it. Only the fields of the row's mode count. An empty weekday mask or window list is
    read as it is, although the terminal page cannot save one.
    """
    if (time_type := row.get("time_type")) is not None and time_type != _TIME_TYPE:
        raise ValueError("The time object is not a time-section object")
    mode = row.get("mode")
    if mode == MANUAL:
        return TerminalSchedule(
            MANUAL,
            weekdays=_weekdays(row.get("weekday")),
            windows=_canonical_windows(_ranges(_texts(row, "time_section"))),
        )
    if mode == CALENDAR:
        sections = [f"{day}_section" for day in _WIRE_DAYS]
        if not any(section in row for section in sections):
            raise ValueError("The calendar time object has no day sections")
        return TerminalSchedule(
            CALENDAR,
            calendar=tuple(
                _canonical_day(_hour_ranges(_texts(row, section) if section in row else ()))
                for section in sections
            ),
        )
    raise ValueError("The time object mode is not manual or calendar")


def change_terminal_schedule(
    schedule: TerminalSchedule | None, field: str, value: bool | int | str
) -> TerminalSchedule | None:
    """Apply one native field edit and return the schedule that must then be stored.

    ``None`` is a terminal without a time object, which may use the network at any time, both as
    the current schedule and as the result. Changing the mode never changes the hours that are
    allowed: from any time it starts with ``default_terminal_schedule``, which allows everything,
    and between manual and calendar it converts exactly or refuses. A schedule that is changed
    has only the fields of its own mode, so nothing of the old mode is carried over.
    The result is always one that the terminal page could save. Raises ValueError, with a message
    fit for a user, for anything that the page would refuse.
    """
    if field == "allowed_time_mode":
        return _change_mode(schedule, value)
    if field == "manual_windows":
        manual = _expect(schedule, MANUAL)
        return _writable(replace(manual, windows=parse_windows_text(_text(value))))
    if field.startswith("manual_") and (day := field.removeprefix("manual_")) in DAYS:
        manual = _expect(schedule, MANUAL)
        if not isinstance(value, bool):
            raise ValueError("A weekday is switched on or off")
        weekdays = {*manual.weekdays, day} if value else set(manual.weekdays) - {day}
        return _writable(replace(manual, weekdays=tuple(name for name in DAYS if name in weekdays)))
    if field.startswith("calendar_") and (day := field.removeprefix("calendar_")) in DAYS:
        calendar = _expect(schedule, CALENDAR)
        days = list(calendar.calendar)
        days[DAYS.index(day)] = parse_calendar_day_text(_text(value))
        return _writable(replace(calendar, calendar=tuple(days)))
    raise ValueError("The field is not a schedule field")


def terminal_schedule_payload(schedule: TerminalSchedule) -> dict[str, Any]:
    """The wire fields of the schedule's mode, as the terminal page sends them.

    Manual sends the weekday mask and the windows as ``HH:MM:00-HH:MM:00``. Calendar sends all
    seven day lists as ``HH:00:00-HH:00:00``, a day without allowed hours as an empty list.
    Raises ValueError for a schedule that the terminal page could not save.
    """
    _writable(schedule)
    if schedule.mode == MANUAL:
        mask = sum(1 << DAYS.index(day) for day in schedule.weekdays)
        return {
            "mode": MANUAL,
            "weekday": str(mask),
            "time_section": [_wire_range(window) for window in schedule.windows],
        }
    return {"mode": CALENDAR} | {
        f"{day}_section": [_wire_range(item) for item in ranges]
        for day, ranges in zip(_WIRE_DAYS, schedule.calendar, strict=True)
    }


def terminal_time_object_name(mac: str) -> str:
    """The name that the terminal page gives a terminal's time object: its bare MAC digits."""
    digits = mac.replace(":", "").replace("-", "")
    if not _MAC_DIGITS.fullmatch(digits):
        raise ValueError("Not a MAC address")
    return digits.upper()


def terminal_time_object_para(mac: str, schedule: TerminalSchedule) -> dict[str, Any]:
    """All the fields that the terminal page sends to add its time object or to set it."""
    return {
        "name": terminal_time_object_name(mac),
        "mode": schedule.mode,
        "flag": OWNED_FLAG,
        "comment": "",
        "time_type": _TIME_TYPE,
    } | terminal_schedule_payload(schedule)


def is_terminal_time_object(row: Mapping[str, Any], mac: str) -> bool:
    """Whether the terminal page made this time object for this terminal.

    Other pages, such as routing policies, use time objects by name. Only an object that has the
    terminal page's flag and this terminal's name is the terminal's own to change or delete.
    """
    expected = terminal_time_object_name(mac)
    name = row.get("name")
    return row.get("flag") == OWNED_FLAG and isinstance(name, str) and name.upper() == expected


def format_ranges(ranges: tuple[str, ...]) -> str:
    """Windows or calendar ranges as one line of text; no ranges give the empty string."""
    return ", ".join(ranges)


def parse_windows_text(text: str) -> tuple[str, ...]:
    """Read comma separated manual windows such as ``08:00-12:30, 13:00-24:00``."""
    return _canonical_windows(_ranges(_tokens(text)))


def parse_calendar_day_text(text: str) -> tuple[str, ...]:
    """Read the allowed whole hours of one day such as ``00:00-06:00, 08:00-24:00``."""
    return _canonical_day(_hour_ranges(_tokens(text)))


def _change_mode(
    schedule: TerminalSchedule | None, value: bool | int | str
) -> TerminalSchedule | None:
    if value == ANY:
        return None
    if value not in MODES:
        raise ValueError(f"The allowed time is {ANY}, {MANUAL} or {CALENDAR}")
    if schedule is None:
        return default_terminal_schedule(value)
    if schedule.mode == value:
        return schedule
    _writable(schedule)
    return _manual_as_calendar(schedule) if value == CALENDAR else _calendar_as_manual(schedule)


def _manual_as_calendar(schedule: TerminalSchedule) -> TerminalSchedule:
    hours: set[int] = set()
    for start, end in _ranges(schedule.windows):
        if start % 60 or end % 60:
            raise ValueError(
                "The manual windows do not all start and end on a whole hour, so they cannot "
                "become a calendar without changing the allowed time. Select any time first"
            )
        hours.update(range(start // 60, end // 60))
    day = _canonical_day(_hour_runs(hours))
    return TerminalSchedule(
        CALENDAR, calendar=tuple(day if name in schedule.weekdays else () for name in DAYS)
    )


def _calendar_as_manual(schedule: TerminalSchedule) -> TerminalSchedule:
    allowed = {
        day: frozenset(_hours(ranges))
        for day, ranges in zip(DAYS, schedule.calendar, strict=True)
        if ranges
    }
    if len(set(allowed.values())) != 1:
        raise ValueError(
            "The calendar does not allow the same hours on every allowed day, so it cannot "
            "become manual weekdays and windows without changing the allowed time. "
            "Select any time first"
        )
    runs = _hour_runs(next(iter(allowed.values())))
    windows = _canonical_windows([(start * 60, end * 60) for start, end in runs])
    return TerminalSchedule(MANUAL, weekdays=tuple(allowed), windows=windows)


def _expect(schedule: TerminalSchedule | None, mode: str) -> TerminalSchedule:
    if schedule is None or schedule.mode != mode:
        raise ValueError(f"Select the {mode} allowed time first")
    return schedule


def _writable(schedule: TerminalSchedule) -> TerminalSchedule:
    """Return the schedule if the terminal page would save it."""
    if schedule.mode == MANUAL:
        if not schedule.weekdays:
            raise ValueError("At least one weekday must stay selected")
        if not schedule.windows:
            raise ValueError("At least one time window is needed")
    elif not any(schedule.calendar):
        raise ValueError("At least one hour of the week must stay allowed")
    return schedule


def _text(value: bool | int | str) -> str:
    if not isinstance(value, str):
        raise ValueError("A text is needed")
    return value


def _tokens(text: str) -> list[str]:
    return [token for token in text.split(",") if token.strip()]


def _texts(row: Mapping[str, Any], key: str) -> list[str]:
    value = row.get(key)
    if not isinstance(value, list | tuple) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"The time object field {key} is missing or not a list of texts")
    return list(value)


def _weekdays(value: Any) -> tuple[str, ...]:
    if isinstance(value, str) and re.fullmatch("[0-9]{1,3}", value):
        value = int(value)
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value < 1 << len(DAYS):
        raise ValueError("The weekday mask is missing or not a number from 0 to 127")
    return tuple(day for bit, day in enumerate(DAYS) if value >> bit & 1)


def _minutes(hour: str, minute: str, second: str | None, *, end: bool) -> int:
    hours, minutes = int(hour), int(minute)
    if minutes > 59 or (second is not None and int(second) != 0):
        raise ValueError("Times are hours and minutes, such as 08:30")
    if hours > (24 if end else 23) or (hours == 24 and minutes):
        raise ValueError("A window starts between 00:00 and 23:59 and ends by 24:00")
    return hours * 60 + minutes


def _range(text: str) -> tuple[int, int]:
    """A time range as minutes of the day: the end is after the start and at most 24:00."""
    if (match := _RANGE.fullmatch(text)) is None:
        raise ValueError("Use a time range such as 08:00-12:30")
    start = _minutes(*match.group(1, 2, 3), end=False)
    end = _minutes(*match.group(4, 5, 6), end=True)
    if end <= start:
        raise ValueError("The end is not after the start")
    return start, end


def _ranges(texts: Iterable[str]) -> list[tuple[int, int]]:
    return [_range(text) for text in texts]


def _hour_ranges(texts: Iterable[str]) -> list[tuple[int, int]]:
    """Ranges of a calendar day in hours: the controller's hour grid has no minutes."""
    ranges = _ranges(texts)
    if any(start % 60 or end % 60 for start, end in ranges):
        raise ValueError("Calendar ranges start and end on a whole hour, such as 08:00-12:00")
    return [(start // 60, end // 60) for start, end in ranges]


def _hours(texts: Iterable[str]) -> set[int]:
    return {hour for start, end in _hour_ranges(texts) for hour in range(start, end)}


def _hour_runs(hours: Iterable[int]) -> list[tuple[int, int]]:
    """Runs of consecutive hours as the first hour and the hour after the last."""
    return _merge([(hour, hour + 1) for hour in hours], overlap=True)


def _merge(ranges: list[tuple[int, int]], *, overlap: bool) -> list[tuple[int, int]]:
    """Sort ranges and join those that touch; overlapping ones join only if that is allowed."""
    merged: list[tuple[int, int]] = []
    for start, end in sorted(ranges):
        if merged and start <= merged[-1][1]:
            if start < merged[-1][1] and not overlap:
                raise ValueError("Time windows must not overlap")
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        else:
            merged.append((start, end))
    return merged


def _canonical_windows(ranges: list[tuple[int, int]]) -> tuple[str, ...]:
    if len(ranges) > MAX_WINDOWS:
        raise ValueError(f"At most {MAX_WINDOWS} time windows fit in a schedule")
    return tuple(
        f"{start // 60:02d}:{start % 60:02d}-{end // 60:02d}:{end % 60:02d}"
        for start, end in _merge(ranges, overlap=False)
    )


def _canonical_day(ranges: list[tuple[int, int]]) -> tuple[str, ...]:
    return tuple(f"{start:02d}:00-{end:02d}:00" for start, end in _merge(ranges, overlap=True))


def _wire_range(text: str) -> str:
    start, end = text.split("-")
    return f"{start}:00-{end}:00"
