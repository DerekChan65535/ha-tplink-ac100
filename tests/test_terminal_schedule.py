"""The allowed-time schedule codec: the wire shapes of the controller's terminal page and its rules.

Every expectation comes from the page's own code (userrpm__wireless_terminal_mngr.htm): Monday is
bit 0 of the weekday mask, a manual schedule has at most 12 windows that end by 24:00, a calendar
is seven hour lists, and the page refuses an empty weekday set, an empty window list and an empty
calendar. The controller itself was not asked.
"""

import dataclasses

import pytest

from custom_components.tplink_ac100.terminal import TerminalField
from custom_components.tplink_ac100.terminal_schedule import (
    DAYS,
    MAX_WINDOWS,
    TerminalSchedule,
    change_terminal_schedule,
    default_terminal_schedule,
    format_ranges,
    is_terminal_time_object,
    parse_calendar_day_text,
    parse_terminal_schedule,
    parse_windows_text,
    terminal_schedule_payload,
    terminal_time_object_name,
    terminal_time_object_para,
)

WIRE_DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
MAC = "02:00:00:aa:bb:01"
WEEK = DAYS
WORKDAYS = DAYS[:5]


def manual(weekdays=WORKDAYS, windows=("08:00-12:00", "13:00-17:30")) -> TerminalSchedule:
    return TerminalSchedule("manual", weekdays=tuple(weekdays), windows=tuple(windows))


def calendar(**days: tuple[str, ...]) -> TerminalSchedule:
    return TerminalSchedule("calendar", calendar=tuple(tuple(days.get(day, ())) for day in DAYS))


def cells(schedule: TerminalSchedule) -> set[tuple[str, int]]:
    """The (day, hour) cells that a schedule allows, whatever its mode."""
    if schedule.mode == "calendar":
        return {
            (day, hour)
            for day, ranges in zip(DAYS, schedule.calendar, strict=True)
            for hour in parse_hours(ranges)
        }
    return {(day, hour) for day in schedule.weekdays for hour in parse_hours(schedule.windows)}


def parse_hours(ranges: tuple[str, ...]) -> set[int]:
    hours: set[int] = set()
    for item in ranges:
        start, end = item.split("-")
        hours.update(range(int(start[:2]), int(end[:2])))
    return hours


# The wire shapes


def test_the_weekday_mask_has_monday_as_bit_zero_and_sunday_as_bit_six():
    assert DAYS == ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
    for bit, day in enumerate(DAYS):
        schedule = manual(weekdays=(day,))
        payload = terminal_schedule_payload(schedule)
        assert payload["weekday"] == str(1 << bit)
        for form in (1 << bit, str(1 << bit)):
            assert parse_terminal_schedule({**payload, "weekday": form}) == schedule
    assert terminal_schedule_payload(manual(weekdays=WORKDAYS))["weekday"] == "31"
    assert terminal_schedule_payload(manual(weekdays=DAYS[5:]))["weekday"] == "96"
    assert terminal_schedule_payload(manual(weekdays=WEEK))["weekday"] == "127"


def test_a_manual_schedule_is_sent_as_the_page_sends_it():
    assert terminal_schedule_payload(manual()) == {
        "mode": "manual",
        "weekday": "31",
        "time_section": ["08:00:00-12:00:00", "13:00:00-17:30:00"],
    }
    assert terminal_time_object_para(MAC, manual(windows=("00:00-24:00",))) == {
        "name": "020000AABB01",
        "mode": "manual",
        "flag": "inner",
        "comment": "",
        "time_type": "section",
        "weekday": "31",
        "time_section": ["00:00:00-24:00:00"],
    }


def test_a_calendar_is_sent_as_seven_day_lists_empty_days_included():
    schedule = calendar(monday=("00:00-06:00", "08:00-24:00"), sunday=("23:00-24:00",))
    payload = terminal_schedule_payload(schedule)
    assert list(payload) == ["mode", *(f"{day}_section" for day in WIRE_DAYS)]
    assert payload == {
        "mode": "calendar",
        "mon_section": ["00:00:00-06:00:00", "08:00:00-24:00:00"],
        "tue_section": [],
        "wed_section": [],
        "thu_section": [],
        "fri_section": [],
        "sat_section": [],
        "sun_section": ["23:00:00-24:00:00"],
    }
    para = terminal_time_object_para(MAC, schedule)
    assert list(para)[:5] == ["name", "mode", "flag", "comment", "time_type"]
    assert para["time_type"] == "section" and para["flag"] == "inner"


@pytest.mark.parametrize(
    "schedule",
    [
        manual(),
        manual(weekdays=("sunday",), windows=("00:00-00:01",)),
        manual(weekdays=WEEK, windows=("00:00-24:00",)),
        manual(windows=tuple(f"{hour:02d}:00-{hour:02d}:30" for hour in range(0, 24, 2))),
        calendar(monday=("00:00-24:00",)),
        calendar(wednesday=("00:00-01:00", "02:00-03:00", "22:00-24:00"), friday=("12:00-13:00",)),
        default_terminal_schedule("manual"),
        default_terminal_schedule("calendar"),
    ],
)
def test_a_schedule_survives_its_own_wire_form(schedule):
    row = terminal_time_object_para(MAC, schedule)
    assert parse_terminal_schedule(row) == schedule
    assert parse_terminal_schedule(terminal_schedule_payload(schedule)) == schedule


def test_every_cell_of_the_week_can_be_the_only_allowed_one():
    for day in DAYS:
        for hour in range(24):
            text = f"{hour:02d}:00-{hour + 1:02d}:00"
            schedule = change_terminal_schedule(
                default_terminal_schedule("calendar"), f"calendar_{day}", text
            )
            others = DAYS[: DAYS.index(day)] + DAYS[DAYS.index(day) + 1 :]
            for other in others:
                schedule = change_terminal_schedule(schedule, f"calendar_{other}", text)
            assert cells(schedule) == {(name, hour) for name in DAYS}
            assert parse_terminal_schedule(terminal_schedule_payload(schedule)) == schedule


def test_the_hours_of_one_day_are_read_and_written_cell_by_cell():
    start = default_terminal_schedule("calendar")
    for day in DAYS:
        index = DAYS.index(day)
        for hours in (
            {0},
            {23},
            {5, 6, 7},
            {0, 2, 4, 6, 8, 10, 12, 14, 16, 18, 20, 22},
            set(range(24)),
        ):
            text = ", ".join(f"{hour:02d}:00-{hour + 1:02d}:00" for hour in sorted(hours))
            edited = change_terminal_schedule(start, f"calendar_{day}", text)
            assert parse_hours(edited.calendar[index]) == hours
            assert edited.calendar[:index] + edited.calendar[index + 1 :] == (
                start.calendar[:index] + start.calendar[index + 1 :]
            )
            wire = terminal_schedule_payload(edited)[f"{WIRE_DAYS[index]}_section"]
            assert sum(int(item[9:11]) - int(item[:2]) for item in wire) == len(hours)
            assert parse_terminal_schedule(terminal_schedule_payload(edited)) == edited


# Reading what the controller reports


def test_a_row_is_read_with_the_fields_of_its_mode_only():
    row = {
        "id": "7",
        "name": "020000AABB01",
        "mode": "manual",
        "flag": "inner",
        "comment": "",
        "time_type": "section",
        "weekday": "65",
        "time_section": ["20:00:00-24:00:00", "06:00:00-07:30:00"],
        "mon_section": ["01:00:00-02:00:00"],
    }
    assert parse_terminal_schedule(row) == manual(
        weekdays=("monday", "sunday"), windows=("06:00-07:30", "20:00-24:00")
    )
    row = {"mode": "calendar", "weekday": "1", "time_section": ["01:00:00-02:00:00"]} | {
        f"{day}_section": [] for day in WIRE_DAYS
    }
    assert parse_terminal_schedule(row) == calendar()


def test_touching_windows_are_joined_as_the_page_joins_them_and_the_hours_of_a_day_are_a_set():
    row = {
        "mode": "manual",
        "weekday": 1,
        "time_section": ["08:00:00-12:00:00", "12:00:00-13:00:00"],
    }
    assert parse_terminal_schedule(row).windows == ("08:00-13:00",)
    row = {
        "mode": "calendar",
        "mon_section": ["01:00:00-03:00:00", "02:00:00-04:00:00", "05:00:00-06:00:00"],
    }
    assert parse_terminal_schedule(row).calendar[0] == ("01:00-04:00", "05:00-06:00")


def test_a_calendar_day_that_the_controller_leaves_out_is_a_day_without_allowed_hours():
    assert parse_terminal_schedule({"mode": "calendar", "tue_section": ["00:00:00-24:00:00"]}) == (
        calendar(tuesday=("00:00-24:00",))
    )


def test_a_manual_row_that_the_page_could_not_save_is_read_as_it_is():
    assert parse_terminal_schedule({"mode": "manual", "weekday": "0", "time_section": []}) == (
        TerminalSchedule("manual")
    )
    assert parse_terminal_schedule({"mode": "calendar", "mon_section": []}) == calendar()


@pytest.mark.parametrize(
    "row",
    [
        {},
        {"mode": "any"},
        {"mode": "weekly", "weekday": "1", "time_section": []},
        {"mode": None},
        {
            "mode": "manual",
            "time_type": "period",
            "weekday": "1",
            "time_section": ["00:00:00-01:00:00"],
        },
        {"mode": "manual", "time_section": ["00:00:00-01:00:00"]},
        {"mode": "manual", "weekday": "1"},
        {"mode": "manual", "weekday": "128", "time_section": ["00:00:00-01:00:00"]},
        {"mode": "manual", "weekday": "-1", "time_section": ["00:00:00-01:00:00"]},
        {"mode": "manual", "weekday": "mon", "time_section": ["00:00:00-01:00:00"]},
        {"mode": "manual", "weekday": 1.0, "time_section": ["00:00:00-01:00:00"]},
        {"mode": "manual", "weekday": True, "time_section": ["00:00:00-01:00:00"]},
        {"mode": "manual", "weekday": None, "time_section": ["00:00:00-01:00:00"]},
        {"mode": "manual", "weekday": "1", "time_section": "00:00:00-01:00:00"},
        {"mode": "manual", "weekday": "1", "time_section": [None]},
        {"mode": "manual", "weekday": "1", "time_section": ["0000,0100"]},
        {"mode": "manual", "weekday": "1", "time_section": ["00:00:00-00:00:00"]},
        {"mode": "manual", "weekday": "1", "time_section": ["02:00:00-01:00:00"]},
        {"mode": "manual", "weekday": "1", "time_section": ["00:00:30-01:00:00"]},
        {"mode": "manual", "weekday": "1", "time_section": ["00:00:00-24:00:01"]},
        {"mode": "manual", "weekday": "1", "time_section": ["00:00:00-24:30:00"]},
        {"mode": "manual", "weekday": "1", "time_section": ["24:00:00-24:00:00"]},
        {"mode": "manual", "weekday": "1", "time_section": ["00:60:00-01:00:00"]},
        {"mode": "manual", "weekday": "1", "time_section": ["00:00:00-25:00:00"]},
        {
            "mode": "manual",
            "weekday": "1",
            "time_section": ["01:00:00-03:00:00", "02:00:00-04:00:00"],
        },
        {
            "mode": "manual",
            "weekday": "1",
            "time_section": [f"{h:02d}:00:00-{h:02d}:30:00" for h in range(13)],
        },
        {"mode": "calendar"},
        {"mode": "calendar", "mon_section": "01:00:00-02:00:00"},
        {"mode": "calendar", "mon_section": ["01:30:00-02:00:00"]},
        {"mode": "calendar", "mon_section": ["01:00:00-02:30:00"]},
        {"mode": "calendar", "mon_section": ["01:00:00-02:00:30"]},
        {"mode": "calendar", "mon_section": ["02:00:00-01:00:00"]},
        {"mode": "calendar", "mon_section": ["24:00:00-24:00:00"]},
        {"mode": "calendar", "mon_section": ["00:00:00-25:00:00"]},
    ],
)
def test_a_row_that_is_not_a_readable_schedule_is_refused(row):
    with pytest.raises(ValueError):
        parse_terminal_schedule(row)


def test_twelve_windows_are_the_most_and_the_thirteenth_is_refused_before_joining():
    twelve = ", ".join(f"{hour:02d}:00-{hour:02d}:30" for hour in range(0, 24, 2))
    assert len(parse_windows_text(twelve)) == MAX_WINDOWS
    with pytest.raises(ValueError, match="At most 12"):
        parse_windows_text(twelve + ", 23:00-23:30")
    touching = ", ".join(f"{hour:02d}:00-{hour + 1:02d}:00" for hour in range(13))
    with pytest.raises(ValueError, match="At most 12"):
        parse_windows_text(touching)


# The text of the windows and of a calendar day


@pytest.mark.parametrize(
    ("text", "windows"),
    [
        ("08:00-12:30", ("08:00-12:30",)),
        ("13:00-24:00, 08:00-12:30", ("08:00-12:30", "13:00-24:00")),
        ("8:00-9:05,10:00 - 11:00", ("08:00-09:05", "10:00-11:00")),
        ("00:00-00:01", ("00:00-00:01",)),
        ("23:59-24:00", ("23:59-24:00",)),
        ("08:00-12:00, 12:00-13:00", ("08:00-13:00",)),
        ("08:00:00-12:00:00", ("08:00-12:00",)),
        ("", ()),
        (" , ", ()),
    ],
)
def test_manual_window_text_is_read_and_written_in_one_form(text, windows):
    assert parse_windows_text(text) == windows
    assert parse_windows_text(format_ranges(windows)) == windows


@pytest.mark.parametrize(
    "text",
    [
        "08:00",
        "08:00-",
        "08:00-12:00-13:00",
        "noon-13:00",
        "12:00-12:00",
        "13:00-12:00",
        "24:00-24:00",
        "00:00-24:01",
        "00:00-25:00",
        "00:60-01:00",
        "00:00:30-01:00",
        "08:00-12:00, 11:00-13:00",
        "08:00-12:00, 08:00-09:00",
        "-1:00-2:00",
        "٠٨:٠٠-١٢:٠٠",
    ],
)
def test_manual_window_text_that_the_page_would_refuse_is_refused(text):
    with pytest.raises(ValueError):
        parse_windows_text(text)


@pytest.mark.parametrize(
    ("text", "ranges"),
    [
        ("", ()),
        ("00:00-24:00", ("00:00-24:00",)),
        ("06:00-07:00", ("06:00-07:00",)),
        ("23:00-24:00", ("23:00-24:00",)),
        ("08:00-12:00, 00:00-06:00", ("00:00-06:00", "08:00-12:00")),
        ("01:00-03:00, 02:00-05:00", ("01:00-05:00",)),
        ("01:00-02:00, 02:00-03:00", ("01:00-03:00",)),
        ("1:00-2:00", ("01:00-02:00",)),
        ("00:00:00-02:00:00", ("00:00-02:00",)),
    ],
)
def test_calendar_day_text_is_the_set_of_its_whole_hours(text, ranges):
    assert parse_calendar_day_text(text) == ranges
    assert parse_calendar_day_text(format_ranges(ranges)) == ranges


@pytest.mark.parametrize(
    "text",
    [
        "08:30-10:00",
        "08:00-10:30",
        "08:00-08:00",
        "10:00-08:00",
        "24:00-24:00",
        "00:00-25:00",
        "all",
    ],
)
def test_calendar_day_text_with_minutes_or_a_bad_range_is_refused(text):
    with pytest.raises(ValueError):
        parse_calendar_day_text(text)


def test_the_longest_text_fits_a_state():
    windows = parse_windows_text(
        ", ".join(f"{hour:02d}:00-{hour:02d}:30" for hour in range(0, 24, 2))
    )
    day = parse_calendar_day_text(
        ", ".join(f"{hour:02d}:00-{hour + 1:02d}:00" for hour in range(0, 24, 2))
    )
    assert len(day) == 12
    assert len(format_ranges(windows)) < 255 and len(format_ranges(day)) < 255


# Edits


def test_a_weekday_switch_changes_that_day_only():
    schedule = manual()
    for field in (TerminalField.MANUAL_SATURDAY, "manual_saturday"):
        edited = change_terminal_schedule(schedule, field, True)
        assert edited == manual(weekdays=(*WORKDAYS, "saturday"))
    edited = change_terminal_schedule(schedule, TerminalField.MANUAL_WEDNESDAY, False)
    assert edited == manual(weekdays=("monday", "tuesday", "thursday", "friday"))
    assert change_terminal_schedule(schedule, "manual_monday", True) == schedule
    assert change_terminal_schedule(schedule, "manual_sunday", False) == schedule


def test_the_last_weekday_cannot_be_switched_off_and_a_day_needs_a_switch_value():
    schedule = manual(weekdays=("friday",))
    with pytest.raises(ValueError, match="At least one weekday"):
        change_terminal_schedule(schedule, "manual_friday", False)
    with pytest.raises(ValueError, match="switched on or off"):
        change_terminal_schedule(schedule, "manual_friday", "on")
    with pytest.raises(ValueError, match="switched on or off"):
        change_terminal_schedule(schedule, "manual_friday", 1)


def test_manual_windows_change_without_touching_the_weekdays():
    edited = change_terminal_schedule(
        manual(), TerminalField.MANUAL_WINDOWS, "18:00-22:00, 06:00-07:00"
    )
    assert edited == manual(windows=("06:00-07:00", "18:00-22:00"))
    for text in ("", "  ", ","):
        with pytest.raises(ValueError, match="At least one time window"):
            change_terminal_schedule(manual(), "manual_windows", text)
    with pytest.raises(ValueError, match="text"):
        change_terminal_schedule(manual(), "manual_windows", True)


def test_one_calendar_day_changes_without_touching_the_others():
    schedule = calendar(monday=("08:00-12:00",), tuesday=("09:00-10:00",))
    edited = change_terminal_schedule(schedule, TerminalField.CALENDAR_SATURDAY, "10:00-11:00")
    assert edited == calendar(
        monday=("08:00-12:00",), tuesday=("09:00-10:00",), saturday=("10:00-11:00",)
    )
    emptied = change_terminal_schedule(schedule, "calendar_monday", "")
    assert emptied == calendar(tuesday=("09:00-10:00",))
    assert change_terminal_schedule(schedule, "calendar_tuesday", "09:00-10:00") == schedule


def test_the_calendar_cannot_be_emptied():
    schedule = calendar(thursday=("00:00-24:00",))
    with pytest.raises(ValueError, match="At least one hour"):
        change_terminal_schedule(schedule, "calendar_thursday", "")
    with pytest.raises(ValueError, match="text"):
        change_terminal_schedule(schedule, "calendar_thursday", False)


@pytest.mark.parametrize(
    ("schedule", "field", "value"),
    [
        (None, "manual_monday", True),
        (None, "manual_windows", "08:00-09:00"),
        (None, "calendar_monday", "08:00-09:00"),
        (calendar(monday=("08:00-09:00",)), "manual_monday", True),
        (calendar(monday=("08:00-09:00",)), "manual_windows", "08:00-09:00"),
        (manual(), "calendar_monday", "08:00-09:00"),
    ],
)
def test_a_field_of_another_mode_needs_that_mode_selected_first(schedule, field, value):
    with pytest.raises(ValueError, match="Select the (manual|calendar) allowed time first"):
        change_terminal_schedule(schedule, field, value)


@pytest.mark.parametrize(
    "field", ["", "name", "manual_", "manual_someday", "calendar_monday2", "upload_limit"]
)
def test_a_field_that_is_not_a_schedule_field_is_refused(field):
    with pytest.raises(ValueError, match="not a schedule field"):
        change_terminal_schedule(manual(), field, True)


# The mode


@pytest.mark.parametrize("mode", ["manual", "calendar"])
def test_choosing_a_mode_for_a_terminal_without_a_schedule_allows_every_day_all_day(mode):
    schedule = change_terminal_schedule(None, TerminalField.ALLOWED_TIME_MODE, mode)
    assert schedule == default_terminal_schedule(mode)
    assert cells(schedule) == {(day, hour) for day in DAYS for hour in range(24)}
    assert terminal_schedule_payload(schedule)  # the page could save it


def test_choosing_any_removes_the_schedule_and_choosing_the_same_mode_changes_nothing():
    for schedule in (None, manual(), calendar(monday=("00:00-24:00",))):
        assert change_terminal_schedule(schedule, "allowed_time_mode", "any") is None
    for schedule in (manual(), calendar(monday=("00:00-24:00",))):
        assert change_terminal_schedule(schedule, "allowed_time_mode", schedule.mode) is schedule
    for value in ("weekly", "", True, None):
        with pytest.raises(ValueError, match="any, manual or calendar"):
            change_terminal_schedule(manual(), "allowed_time_mode", value)


def test_manual_becomes_the_same_hours_as_a_calendar_and_back():
    schedule = manual(
        weekdays=("monday", "wednesday", "sunday"), windows=("06:00-08:00", "13:00-24:00")
    )
    converted = change_terminal_schedule(schedule, "allowed_time_mode", "calendar")
    assert converted == calendar(
        monday=("06:00-08:00", "13:00-24:00"),
        wednesday=("06:00-08:00", "13:00-24:00"),
        sunday=("06:00-08:00", "13:00-24:00"),
    )
    assert cells(converted) == cells(schedule)
    assert change_terminal_schedule(converted, "allowed_time_mode", "manual") == schedule
    everything = change_terminal_schedule(
        default_terminal_schedule("calendar"), "allowed_time_mode", "manual"
    )
    assert everything == default_terminal_schedule("manual")


def test_a_mode_change_that_cannot_keep_the_allowed_hours_is_refused():
    with pytest.raises(ValueError, match="whole hour"):
        change_terminal_schedule(manual(), "allowed_time_mode", "calendar")
    different = calendar(monday=("08:00-12:00",), tuesday=("09:00-12:00",))
    with pytest.raises(ValueError, match="same hours on every allowed day"):
        change_terminal_schedule(different, "allowed_time_mode", "manual")


def test_a_schedule_that_allows_nothing_has_no_mode_to_change_to():
    nothing = TerminalSchedule("manual", windows=("08:00-09:00",))
    with pytest.raises(ValueError, match="At least one weekday"):
        change_terminal_schedule(nothing, "allowed_time_mode", "calendar")
    with pytest.raises(ValueError, match="At least one hour"):
        change_terminal_schedule(calendar(), "allowed_time_mode", "manual")


@pytest.mark.parametrize("start", [None, "manual", "calendar"])
@pytest.mark.parametrize("mode", ["manual", "calendar"])
def test_a_schedule_of_one_mode_sends_none_of_the_fields_of_the_other(start, mode):
    schedule = change_terminal_schedule(
        None if start is None else default_terminal_schedule(start), "allowed_time_mode", mode
    )
    assert schedule.mode == mode
    wire = {*terminal_schedule_payload(schedule), *terminal_time_object_para(MAC, schedule)}
    sections = {f"{day}_section" for day in WIRE_DAYS}
    if mode == "manual":
        assert wire == {"name", "mode", "flag", "comment", "time_type", "weekday", "time_section"}
        assert schedule.calendar == ()
    else:
        assert wire == {"name", "mode", "flag", "comment", "time_type", *sections}
        assert (schedule.weekdays, schedule.windows) == ((), ())


def test_changing_the_mode_keeps_the_allowed_hours_and_never_leaves_nothing_allowed():
    everything = {(day, hour) for day in DAYS for hour in range(24)}
    assert cells(default_terminal_schedule("manual")) == everything
    assert cells(default_terminal_schedule("calendar")) == everything
    samples = [
        manual(weekdays=("monday",), windows=("00:00-01:00",)),
        manual(weekdays=("sunday",), windows=("23:00-24:00",)),
        manual(weekdays=WEEK, windows=("00:00-24:00",)),
        manual(weekdays=DAYS[5:], windows=("06:00-09:00", "18:00-22:00")),
        manual(windows=tuple(f"{hour:02d}:00-{hour + 1:02d}:00" for hour in range(0, 24, 2))),
        calendar(monday=("00:00-24:00",)),
        calendar(**{day: ("07:00-09:00", "12:00-13:00") for day in DAYS[:5]}),
        calendar(saturday=("23:00-24:00",), sunday=("23:00-24:00",)),
    ]
    for schedule in samples:
        other = "calendar" if schedule.mode == "manual" else "manual"
        converted = change_terminal_schedule(schedule, "allowed_time_mode", other)
        assert converted.mode == other
        assert cells(converted) == cells(schedule) != set()
        assert terminal_schedule_payload(converted)  # the page could save it
        assert change_terminal_schedule(converted, "allowed_time_mode", schedule.mode) == schedule


def test_the_largest_schedules_survive_their_wire_form_and_their_text():
    twelve = tuple(f"{hour:02d}:00-{hour:02d}:30" for hour in range(0, 24, 2))
    largest_manual = manual(weekdays=WEEK, windows=twelve)
    assert len(terminal_schedule_payload(largest_manual)["time_section"]) == MAX_WINDOWS
    hourly = tuple(f"{hour:02d}:00-{hour + 1:02d}:00" for hour in range(0, 24, 2))
    largest_calendar = calendar(**dict.fromkeys(DAYS, hourly))
    wire = terminal_schedule_payload(largest_calendar)
    assert all(len(wire[f"{day}_section"]) == 12 for day in WIRE_DAYS)
    for schedule in (largest_manual, largest_calendar):
        assert parse_terminal_schedule(terminal_schedule_payload(schedule)) == schedule
    assert len(cells(largest_calendar)) == 7 * 12
    assert parse_windows_text(format_ranges(twelve)) == twelve
    assert parse_calendar_day_text(format_ranges(hourly)) == hourly
    assert max(len(format_ranges(twelve)), len(format_ranges(hourly))) < 255


# What the page would not save


def test_a_schedule_that_the_page_could_not_save_has_no_wire_form():
    with pytest.raises(ValueError, match="weekday"):
        terminal_schedule_payload(TerminalSchedule("manual", windows=("08:00-09:00",)))
    with pytest.raises(ValueError, match="time window"):
        terminal_schedule_payload(TerminalSchedule("manual", weekdays=("monday",)))
    with pytest.raises(ValueError, match="hour"):
        terminal_schedule_payload(calendar())


@pytest.mark.parametrize(
    "arguments",
    [
        {"mode": "any"},
        {"mode": "weekly"},
        {"mode": "manual", "calendar": (("00:00-01:00",),) * 7},
        {"mode": "manual", "weekdays": ("tuesday", "monday")},
        {"mode": "manual", "weekdays": ("monday", "monday")},
        {"mode": "manual", "weekdays": ("someday",)},
        {"mode": "manual", "windows": ("9:00-10:00",)},
        {"mode": "manual", "windows": ("10:00-11:00", "09:00-10:00")},
        {"mode": "manual", "windows": ("09:00-10:00", "10:00-11:00")},
        {"mode": "manual", "windows": ("09:00-10:00", "09:30-11:00")},
        {"mode": "manual", "windows": ("10:00-09:00",)},
        {"mode": "manual", "windows": tuple(f"{h:02d}:00-{h:02d}:30" for h in range(13))},
        {"mode": "calendar", "weekdays": ("monday",), "calendar": ((),) * 7},
        {"mode": "calendar", "windows": ("09:00-10:00",), "calendar": ((),) * 7},
        {"mode": "calendar"},
        {"mode": "calendar", "calendar": ((),) * 6},
        {"mode": "calendar", "calendar": ((),) * 8},
        {"mode": "calendar", "calendar": (("09:30-10:00",),) + ((),) * 6},
        {"mode": "calendar", "calendar": (("09:00-10:00", "10:00-11:00"),) + ((),) * 6},
        {"mode": "calendar", "calendar": (("10:00-11:00", "09:00-10:00"),) + ((),) * 6},
    ],
)
def test_a_schedule_is_always_in_canonical_form(arguments):
    with pytest.raises(ValueError):
        TerminalSchedule(**arguments)


def test_schedule_errors_never_echo_private_input():
    canary = "private_schedule_input_4e1a7"
    invalid_calls = (
        lambda: parse_windows_text(canary),
        lambda: parse_calendar_day_text(canary),
        lambda: TerminalSchedule(canary),
        lambda: default_terminal_schedule(canary),
        lambda: parse_terminal_schedule({"mode": canary}),
        lambda: change_terminal_schedule(manual(), "allowed_time_mode", canary),
        lambda: change_terminal_schedule(manual(), canary, "any"),
    )
    for invalid_call in invalid_calls:
        with pytest.raises(ValueError) as err:
            invalid_call()
        assert canary not in str(err.value)
    with pytest.raises(ValueError) as err:
        parse_windows_text("09:41-08:37")
    assert "09:41" not in str(err.value) and "08:37" not in str(err.value)


def test_a_schedule_is_a_hashable_value():
    schedule = manual()
    assert hash(schedule) == hash(manual())
    assert schedule != manual(weekdays=DAYS)
    with pytest.raises(dataclasses.FrozenInstanceError):
        schedule.mode = "calendar"  # type: ignore[misc]


# Ownership of the time object


@pytest.mark.parametrize("mac", [MAC, "02-00-00-AA-BB-01", "020000aabb01", "02:00:00:AA:BB:01"])
def test_the_time_object_is_named_by_the_bare_upper_case_mac(mac):
    assert terminal_time_object_name(mac) == "020000AABB01"


@pytest.mark.parametrize(
    "mac", ["", "02:00:00:aa:bb", "02:00:00:aa:bb:0g", "02:00:00:aa:bb:01:02", "any"]
)
def test_a_name_needs_a_real_mac(mac):
    with pytest.raises(ValueError):
        terminal_time_object_name(mac)


def test_only_the_terminal_pages_own_object_of_this_terminal_is_its_own():
    own = {"name": "020000AABB01", "flag": "inner", "mode": "manual"}
    assert is_terminal_time_object(own, MAC)
    assert is_terminal_time_object({**own, "name": "020000aabb01"}, "02-00-00-AA-BB-01")
    assert not is_terminal_time_object({**own, "name": "020000AABB02"}, MAC)
    assert not is_terminal_time_object({**own, "name": "02:00:00:aa:bb:01"}, MAC)
    assert not is_terminal_time_object({**own, "flag": "system"}, MAC)
    assert not is_terminal_time_object({**own, "flag": None}, MAC)
    assert not is_terminal_time_object({"name": "020000AABB01"}, MAC)
    assert not is_terminal_time_object({"flag": "inner"}, MAC)
    assert not is_terminal_time_object({**own, "name": 20000}, MAC)
