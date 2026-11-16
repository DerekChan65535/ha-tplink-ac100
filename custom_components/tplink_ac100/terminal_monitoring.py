"""Read-only saved configuration of each terminal, on its own device page, enabled by default.

The values come from the shared snapshot's host table read, so they need no further request. They
are the controller's saved record for the terminal, not its current telemetry: the record can be
read while the terminal is away, and a missing or unreadable record makes them unavailable.
"""

from collections.abc import Callable
from dataclasses import dataclass

from homeassistant.components.binary_sensor import BinarySensorEntity, BinarySensorEntityDescription
from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
)
from homeassistant.const import UnitOfDataRate
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import Ac100ConfigEntry, Ac100Coordinator
from .terminal import TerminalConfiguration
from .terminal_entity import Ac100TerminalEntity, add_for_each_terminal


@dataclass(frozen=True, kw_only=True)
class Ac100TerminalSensorDescription(SensorEntityDescription):
    """Describe a number or text of a terminal's saved record."""

    value_fn: Callable[[TerminalConfiguration], str | int | None]


@dataclass(frozen=True, kw_only=True)
class Ac100TerminalBinarySensorDescription(BinarySensorEntityDescription):
    """Describe an on/off value of a terminal's saved record; None is unknown."""

    value_fn: Callable[[TerminalConfiguration], bool | None]


def _saved_name(configuration: TerminalConfiguration) -> str | None:
    """The name saved on the controller, unknown when it is blank; it is not the device's name."""
    name = configuration.name
    return name if name is not None and name.strip() else None


TERMINAL_SENSORS = (
    Ac100TerminalSensorDescription(
        key="name",
        translation_key="terminal_name",
        value_fn=_saved_name,
    ),
    Ac100TerminalSensorDescription(
        key="upload_limit",
        translation_key="terminal_upload_limit",
        device_class=SensorDeviceClass.DATA_RATE,
        native_unit_of_measurement=UnitOfDataRate.KILOBYTES_PER_SECOND,
        value_fn=lambda configuration: configuration.upload_limit,
    ),
    Ac100TerminalSensorDescription(
        key="download_limit",
        translation_key="terminal_download_limit",
        device_class=SensorDeviceClass.DATA_RATE,
        native_unit_of_measurement=UnitOfDataRate.KILOBYTES_PER_SECOND,
        value_fn=lambda configuration: configuration.download_limit,
    ),
    Ac100TerminalSensorDescription(
        key="bound_access_point",
        translation_key="terminal_bound_access_point",
        value_fn=lambda configuration: configuration.bound_ap,
    ),
    Ac100TerminalSensorDescription(
        key="bound_radio",
        translation_key="terminal_bound_radio",
        value_fn=lambda configuration: configuration.bound_radio,
    ),
)

TERMINAL_BINARY_SENSORS = (
    Ac100TerminalBinarySensorDescription(
        key="rate_limit_enabled",
        translation_key="terminal_rate_limit_enabled",
        value_fn=lambda configuration: configuration.rate_limit_enabled,
    ),
    Ac100TerminalBinarySensorDescription(
        key="binding_enabled",
        translation_key="terminal_binding_enabled",
        value_fn=lambda configuration: configuration.binding_enabled,
    ),
    Ac100TerminalBinarySensorDescription(
        key="force_binding",
        translation_key="terminal_force_binding",
        value_fn=lambda configuration: configuration.force_binding,
    ),
)


def add_terminal_sensors(
    entry: Ac100ConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    """Add the saved-record numbers and texts for every terminal that has a device."""
    add_for_each_terminal(
        entry,
        async_add_entities,
        lambda coordinator, mac: [
            Ac100TerminalSensor(coordinator, mac, description) for description in TERMINAL_SENSORS
        ],
    )


def add_terminal_binary_sensors(
    entry: Ac100ConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    """Add the saved-record on/off values for every terminal that has a device."""
    add_for_each_terminal(
        entry,
        async_add_entities,
        lambda coordinator, mac: [
            Ac100TerminalBinarySensor(coordinator, mac, description)
            for description in TERMINAL_BINARY_SENSORS
        ],
    )


class Ac100TerminalSensor(Ac100TerminalEntity, SensorEntity):
    """A saved value of one terminal: unknown if the controller reports none."""

    entity_description: Ac100TerminalSensorDescription

    def __init__(
        self,
        coordinator: Ac100Coordinator,
        mac: str,
        description: Ac100TerminalSensorDescription,
    ) -> None:
        super().__init__(coordinator, mac, description.key)
        self.entity_description = description

    @property
    def native_value(self) -> str | int | None:
        if (configuration := self.configuration) is None:
            return None
        return self.entity_description.value_fn(configuration)


class Ac100TerminalBinarySensor(Ac100TerminalEntity, BinarySensorEntity):
    """A saved on/off value of one terminal: unknown if the controller reports none."""

    entity_description: Ac100TerminalBinarySensorDescription

    def __init__(
        self,
        coordinator: Ac100Coordinator,
        mac: str,
        description: Ac100TerminalBinarySensorDescription,
    ) -> None:
        super().__init__(coordinator, mac, description.key)
        self.entity_description = description

    @property
    def is_on(self) -> bool | None:
        if (configuration := self.configuration) is None:
            return None
        return self.entity_description.value_fn(configuration)
