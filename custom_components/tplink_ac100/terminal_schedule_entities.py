"""The allowed-time schedule of each terminal: readings, and opt-in controls to change it.

The schedule is the terminal's saved record on the controller, so its entities exist for every
known terminal, also while it is away. Readings are on by default. The controls are off until the
user enables them and unavailable until device control is on; each one changes one field of the
schedule through the API, which reads the schedule again before it writes. Times are the
controller's own local clock; nothing is converted to the Home Assistant time zone.
"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.select import SelectEntity
from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
)
from homeassistant.components.switch import SwitchEntity
from homeassistant.components.text import TextEntity, TextMode
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .control_entity import invalid_value
from .coordinator import Ac100ConfigEntry, Ac100Coordinator
from .terminal import TIME_MODES, TerminalConfiguration, TerminalField
from .terminal_entity import Ac100TerminalControl, Ac100TerminalEntity, add_for_each_terminal
from .terminal_schedule import (
    CALENDAR,
    DAYS,
    MANUAL,
    TerminalSchedule,
    change_terminal_schedule,
    format_ranges,
)

# A reading with nothing to list, such as a day without allowed hours, says so.
NONE = "none"


def _day(name: str) -> dict[str, str]:
    return {"day": name.capitalize()}


def _saved_schedule(
    configuration: TerminalConfiguration | None, mode: str
) -> TerminalSchedule | None:
    """The saved schedule while it could be read and is of this mode.

    The fields of the other mode are unknown, not empty: nothing is invented for them.
    """
    if configuration is None or not configuration.schedule_available:
        return None
    schedule = configuration.schedule
    return schedule if schedule is not None and schedule.mode == mode else None


def _readable(configuration: TerminalConfiguration | None) -> bool:
    return (
        configuration is not None
        and configuration.schedule_available
        and configuration.allowed_time_mode in TIME_MODES
    )


def _checked(
    configuration: TerminalConfiguration | None, field: TerminalField, value: bool | str
) -> None:
    """Refuse a value that the terminal page would refuse for the schedule as last read.

    The API checks again against the schedule as it is when it writes.
    """
    try:
        change_terminal_schedule(
            None if configuration is None else configuration.schedule, field, value
        )
    except ValueError as err:
        raise invalid_value(str(err)) from err


@dataclass(frozen=True, kw_only=True)
class Ac100ScheduleSensorDescription(SensorEntityDescription):
    """Describe a reading of the saved schedule: None is unknown, as for another mode's field."""

    value_fn: Callable[[TerminalConfiguration], str | None]


def _mode(configuration: TerminalConfiguration) -> str | None:
    return configuration.allowed_time_mode if _readable(configuration) else None


def _weekdays(configuration: TerminalConfiguration) -> str | None:
    schedule = _saved_schedule(configuration, MANUAL)
    return None if schedule is None else ", ".join(schedule.weekdays) or NONE


def _windows(configuration: TerminalConfiguration) -> str | None:
    schedule = _saved_schedule(configuration, MANUAL)
    return None if schedule is None else format_ranges(schedule.windows) or NONE


def _calendar_day(index: int) -> Callable[[TerminalConfiguration], str | None]:
    def read(configuration: TerminalConfiguration) -> str | None:
        schedule = _saved_schedule(configuration, CALENDAR)
        return None if schedule is None else format_ranges(schedule.calendar[index]) or NONE

    return read


SCHEDULE_SENSORS = (
    Ac100ScheduleSensorDescription(
        key="allowed_time_mode",
        translation_key="terminal_schedule_mode",
        device_class=SensorDeviceClass.ENUM,
        options=list(TIME_MODES),
        value_fn=_mode,
    ),
    Ac100ScheduleSensorDescription(
        key="manual_weekdays",
        translation_key="terminal_schedule_manual_weekdays",
        value_fn=_weekdays,
    ),
    Ac100ScheduleSensorDescription(
        key="manual_windows",
        translation_key="terminal_schedule_manual_windows",
        value_fn=_windows,
    ),
    *(
        Ac100ScheduleSensorDescription(
            key=f"calendar_{day}",
            translation_key="terminal_schedule_calendar_day",
            translation_placeholders=_day(day),
            value_fn=_calendar_day(index),
        )
        for index, day in enumerate(DAYS)
    ),
)


def add_terminal_schedule_sensors(
    entry: Ac100ConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    """Add the schedule readings for every terminal, on by default."""
    add_for_each_terminal(
        entry,
        async_add_entities,
        lambda coordinator, mac: [
            Ac100ScheduleSensor(coordinator, mac, description) for description in SCHEDULE_SENSORS
        ],
    )


def add_terminal_schedule_selects(
    entry: Ac100ConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    """Add the allowed-time mode choice for every terminal, disabled until enabled."""
    add_for_each_terminal(
        entry,
        async_add_entities,
        lambda coordinator, mac: [Ac100ScheduleModeSelect(coordinator, mac)],
    )


def add_terminal_schedule_switches(
    entry: Ac100ConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    """Add the seven manual weekday switches for every terminal, disabled until enabled."""
    add_for_each_terminal(
        entry,
        async_add_entities,
        lambda coordinator, mac: [Ac100ScheduleWeekdaySwitch(coordinator, mac, d) for d in DAYS],
    )


def add_terminal_schedule_texts(
    entry: Ac100ConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    """Add the manual windows and the seven calendar days, disabled until enabled."""
    add_for_each_terminal(
        entry,
        async_add_entities,
        lambda coordinator, mac: [
            Ac100ScheduleWindowsText(coordinator, mac),
            *(Ac100ScheduleCalendarText(coordinator, mac, day) for day in DAYS),
        ],
    )


class Ac100ScheduleSensor(Ac100TerminalEntity, SensorEntity):
    """A reading of the saved schedule: the mode, or a field of the manual or calendar mode.

    Unavailable while the controller's schedule could not be read. A field that the saved mode does
    not use is unknown, as is every field of a terminal that may use the network at any time.
    """

    entity_description: Ac100ScheduleSensorDescription

    def __init__(
        self,
        coordinator: Ac100Coordinator,
        mac: str,
        description: Ac100ScheduleSensorDescription,
    ) -> None:
        super().__init__(coordinator, mac, description.key)
        self.entity_description = description

    @property
    def source_available(self) -> bool:
        configuration = self.configuration
        return configuration is not None and configuration.schedule_available

    @property
    def native_value(self) -> str | None:
        if (configuration := self.configuration) is None:
            return None
        return self.entity_description.value_fn(configuration)


class Ac100ScheduleModeSelect(Ac100TerminalControl, SelectEntity):
    """Whether a terminal may use the network at any time, or only at the times of a schedule.

    Choosing manual or calendar for a terminal that has no schedule starts with every hour of
    every day allowed, so the choice alone blocks nothing. That is the terminal page's own start for
    manual and this integration's choice for calendar. Between manual and calendar the allowed
    hours stay the same, or the change is refused. Choosing any removes the terminal's schedule.
    """

    _attr_translation_key = "terminal_schedule_mode"
    _attr_options = list(TIME_MODES)

    def __init__(self, coordinator: Ac100Coordinator, mac: str) -> None:
        super().__init__(coordinator, mac, "allowed_time_mode_control")

    @property
    def source_available(self) -> bool:
        return _readable(self.configuration)

    @property
    def current_option(self) -> str | None:
        return None if (configuration := self.configuration) is None else _mode(configuration)

    async def async_select_option(self, option: str) -> None:
        _checked(self.configuration, TerminalField.ALLOWED_TIME_MODE, option)
        await self.async_control_terminal(
            lambda api: api.async_set_terminal_field(
                self.mac, TerminalField.ALLOWED_TIME_MODE, option
            )
        )


class Ac100ScheduleWeekdaySwitch(Ac100TerminalControl, SwitchEntity):
    """One weekday of the manual schedule: whether its windows apply on that day."""

    _attr_translation_key = "terminal_schedule_manual_day"

    def __init__(self, coordinator: Ac100Coordinator, mac: str, day: str) -> None:
        super().__init__(coordinator, mac, f"manual_{day}_control")
        self._day = day
        self._field = TerminalField(f"manual_{day}")
        self._attr_translation_placeholders = _day(day)

    @property
    def source_available(self) -> bool:
        return _saved_schedule(self.configuration, MANUAL) is not None

    @property
    def is_on(self) -> bool | None:
        schedule = _saved_schedule(self.configuration, MANUAL)
        return None if schedule is None else self._day in schedule.weekdays

    async def _async_set(self, value: bool) -> None:
        _checked(self.configuration, self._field, value)
        await self.async_control_terminal(
            lambda api: api.async_set_terminal_field(self.mac, self._field, value)
        )

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._async_set(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._async_set(False)


class _Ac100ScheduleText(Ac100TerminalControl, TextEntity):
    """A text field of the saved schedule, which exists only in one of its modes."""

    _attr_mode = TextMode.TEXT
    # An empty day is a valid reading, so the state may be shorter than any minimum.
    _attr_native_min = 0
    _mode: str

    def __init__(self, coordinator: Ac100Coordinator, mac: str, field: TerminalField) -> None:
        super().__init__(coordinator, mac, f"{field}_control")
        self._field = field

    @property
    def schedule(self) -> TerminalSchedule | None:
        return _saved_schedule(self.configuration, self._mode)

    @property
    def source_available(self) -> bool:
        return self.schedule is not None

    async def async_set_value(self, value: str) -> None:
        _checked(self.configuration, self._field, value)
        await self.async_control_terminal(
            lambda api: api.async_set_terminal_field(self.mac, self._field, value)
        )


class Ac100ScheduleWindowsText(_Ac100ScheduleText):
    """The time windows of the manual schedule, such as 08:00-12:30, 13:00-24:00.

    There are at most 12 windows. Each starts and ends within the day, at most at 24:00, and no two
    overlap; windows that touch are joined. They apply on every selected weekday.
    """

    _attr_translation_key = "terminal_schedule_manual_windows"
    _mode = MANUAL

    def __init__(self, coordinator: Ac100Coordinator, mac: str) -> None:
        super().__init__(coordinator, mac, TerminalField.MANUAL_WINDOWS)

    @property
    def native_value(self) -> str | None:
        return None if (schedule := self.schedule) is None else format_ranges(schedule.windows)


class Ac100ScheduleCalendarText(_Ac100ScheduleText):
    """The allowed hours of one calendar day, such as 00:00-06:00, 08:00-24:00.

    Each range is whole hours, so that every hour of the day is allowed or not. A day without
    allowed hours is empty, but the calendar must keep at least one allowed hour in the week.
    """

    _attr_translation_key = "terminal_schedule_calendar_day"
    _mode = CALENDAR

    def __init__(self, coordinator: Ac100Coordinator, mac: str, day: str) -> None:
        super().__init__(coordinator, mac, TerminalField(f"calendar_{day}"))
        self._index = DAYS.index(day)
        self._attr_translation_placeholders = _day(day)

    @property
    def native_value(self) -> str | None:
        schedule = self.schedule
        return None if schedule is None else format_ranges(schedule.calendar[self._index])
