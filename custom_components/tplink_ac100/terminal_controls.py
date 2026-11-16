"""Controls of one terminal's saved configuration: name, rate limit, AP binding and removal.

They sit on the terminal's own device page and are off until the user enables them. They are
registered whether or not device control is on, so that they can be found; until that option is
on they are unavailable. Each one needs the saved record or the connection it acts on, and the
API checks the terminal again before the action writes. No write is repeated automatically.
Nothing here saves the controller's configuration.
"""

from collections import Counter
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.button import ButtonEntity, ButtonEntityDescription
from homeassistant.components.number import NumberEntity, NumberEntityDescription, NumberMode
from homeassistant.components.select import SelectEntity
from homeassistant.components.switch import SwitchEntity, SwitchEntityDescription
from homeassistant.components.text import TextEntity, TextMode
from homeassistant.const import UnitOfDataRate
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .api import Ac100Api
from .control_entity import invalid_value
from .coordinator import Ac100ConfigEntry, Ac100Coordinator
from .device_info import ap_device_names
from .terminal import (
    TERMINAL_LIMIT_MAX,
    TERMINAL_NAME_MAX_BYTES,
    TerminalBindingTarget,
    TerminalConfiguration,
    TerminalField,
    terminal_name_is_valid,
    validate_terminal_value,
)
from .terminal_entity import Ac100TerminalControl, add_for_each_terminal

CHOOSE_TARGET = "Choose an AP binding target first"
ALL_RADIOS = "All radios"


def _checked(field: TerminalField, value: object) -> bool | int | str:
    """The value as the terminal page would accept it, or the English reason it would not."""
    try:
        return validate_terminal_value(field, value)
    except ValueError as err:
        raise invalid_value(str(err)) from err


def binding_target_labels(
    coordinator: Ac100Coordinator, targets: tuple[TerminalBindingTarget, ...]
) -> dict[str, str]:
    """A readable and unique option for each binding target, by its token.

    The AP is named as its device is, which tells apart APs that the controller gave the same
    name. A radio is named by its band, and by its unit as well while the AP has two of one band.
    """
    ap_names = ap_device_names(coordinator.data.aps)

    def radio(target: TerminalBindingTarget) -> str:
        if target.radio_unit is None:
            return ALL_RADIOS
        band = target.radio_name.strip()
        return band if band and band != "---" else f"Radio {target.radio_unit}"

    labels = {
        target.token: f"{ap_names.get(target.ap_mac) or target.ap_name} - {radio(target)}"
        for target in targets
    }
    counts = Counter(labels.values())
    return {
        target.token: (
            f"{labels[target.token]} (radio {target.radio_unit})"
            if counts[labels[target.token]] > 1 and target.radio_unit is not None
            else labels[target.token]
        )
        for target in targets
    }


@dataclass(frozen=True, kw_only=True)
class Ac100TerminalButtonDescription(ButtonEntityDescription):
    """Describe an action on a terminal that needs it connected or away, and maybe its record."""

    present: bool
    needs_record: bool
    press_fn: Callable[[Ac100Api, str], Awaitable[None]]


@dataclass(frozen=True, kw_only=True)
class Ac100TerminalSwitchDescription(SwitchEntityDescription):
    """Describe an on/off field of a terminal's saved record and when it can be changed."""

    field: TerminalField
    state_fn: Callable[[TerminalConfiguration], bool | None]
    editable_fn: Callable[[TerminalConfiguration], bool]
    turn_on_blocker: Callable[[TerminalConfiguration], str | None] | None = None


@dataclass(frozen=True, kw_only=True)
class Ac100TerminalNumberDescription(NumberEntityDescription):
    """Describe a rate cap of a terminal's saved record."""

    field: TerminalField
    value_fn: Callable[[TerminalConfiguration], int | None]


# Disconnecting is not a block: the terminal can reconnect at once. Clearing deletes the saved
# record: its name, caps, binding and schedule. It is not the deletion of the Home Assistant device.
TERMINAL_BUTTONS = (
    Ac100TerminalButtonDescription(
        key="disconnect_control",
        translation_key="terminal_disconnect",
        present=True,
        needs_record=False,
        press_fn=lambda api, mac: api.async_disconnect_terminal(mac),
    ),
    Ac100TerminalButtonDescription(
        key="disconnect_and_clear_control",
        translation_key="terminal_disconnect_and_clear",
        present=True,
        needs_record=True,
        press_fn=lambda api, mac: api.async_clear_terminal(mac, disconnect=True),
    ),
    Ac100TerminalButtonDescription(
        key="clear_offline_control",
        translation_key="terminal_clear_offline",
        present=False,
        needs_record=True,
        press_fn=lambda api, mac: api.async_clear_terminal(mac, disconnect=False),
    ),
)

# The page hides the caps while rate limiting is off, and the binding choices and forced binding
# while binding is off. A record that is offline cannot have its binding edited.
TERMINAL_SWITCHES = (
    Ac100TerminalSwitchDescription(
        key="rate_limit_control",
        translation_key="terminal_rate_limit",
        field=TerminalField.RATE_LIMIT_ENABLED,
        state_fn=lambda configuration: configuration.rate_limit_enabled,
        editable_fn=lambda configuration: True,
    ),
    Ac100TerminalSwitchDescription(
        key="binding_control",
        translation_key="terminal_binding",
        field=TerminalField.BINDING_ENABLED,
        state_fn=lambda configuration: configuration.binding_enabled,
        editable_fn=lambda configuration: configuration.binding_available,
        turn_on_blocker=lambda configuration: (
            None if configuration.bound_target is not None else CHOOSE_TARGET
        ),
    ),
    Ac100TerminalSwitchDescription(
        key="force_binding_control",
        translation_key="terminal_force_binding",
        field=TerminalField.FORCE_BINDING,
        state_fn=lambda configuration: configuration.force_binding,
        editable_fn=lambda configuration: (
            configuration.binding_available and configuration.binding_enabled is True
        ),
    ),
)

TERMINAL_NUMBERS = (
    Ac100TerminalNumberDescription(
        key="upload_limit_control",
        translation_key="terminal_upload_limit",
        field=TerminalField.UPLOAD_LIMIT,
        value_fn=lambda configuration: configuration.upload_limit,
    ),
    Ac100TerminalNumberDescription(
        key="download_limit_control",
        translation_key="terminal_download_limit",
        field=TerminalField.DOWNLOAD_LIMIT,
        value_fn=lambda configuration: configuration.download_limit,
    ),
)


def add_terminal_buttons(
    entry: Ac100ConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    """Add the disconnect and clear buttons for every terminal, disabled until enabled."""
    add_for_each_terminal(
        entry,
        async_add_entities,
        lambda coordinator, mac: [
            Ac100TerminalButton(coordinator, mac, description) for description in TERMINAL_BUTTONS
        ],
    )


def add_terminal_switches(
    entry: Ac100ConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    """Add the rate limit and binding switches for every terminal, disabled until enabled."""
    add_for_each_terminal(
        entry,
        async_add_entities,
        lambda coordinator, mac: [
            Ac100TerminalSwitch(coordinator, mac, description) for description in TERMINAL_SWITCHES
        ],
    )


def add_terminal_numbers(
    entry: Ac100ConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    """Add the upload and download caps for every terminal, disabled until enabled."""
    add_for_each_terminal(
        entry,
        async_add_entities,
        lambda coordinator, mac: [
            Ac100TerminalLimitNumber(coordinator, mac, description)
            for description in TERMINAL_NUMBERS
        ],
    )


def add_terminal_selects(
    entry: Ac100ConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    """Add the AP binding target for every terminal, disabled until enabled."""
    add_for_each_terminal(
        entry,
        async_add_entities,
        lambda coordinator, mac: [Ac100TerminalBindingSelect(coordinator, mac)],
    )


def add_terminal_texts(
    entry: Ac100ConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    """Add the name on the controller for every terminal, disabled until enabled."""
    add_for_each_terminal(
        entry,
        async_add_entities,
        lambda coordinator, mac: [Ac100TerminalNameText(coordinator, mac)],
    )


class Ac100TerminalButton(Ac100TerminalControl, ButtonEntity):
    """Disconnect a terminal that is connected, or clear the saved record of one that is away.

    The state of the terminal that the button needs is read from the latest poll. The API reads
    it again before it acts, so a terminal that has since changed is refused, not acted on.
    """

    entity_description: Ac100TerminalButtonDescription

    def __init__(
        self,
        coordinator: Ac100Coordinator,
        mac: str,
        description: Ac100TerminalButtonDescription,
    ) -> None:
        super().__init__(coordinator, mac, description.key)
        self.entity_description = description

    @property
    def source_available(self) -> bool:
        description = self.entity_description
        return (self.client is not None) is description.present and (
            not description.needs_record or self.configuration is not None
        )

    async def async_press(self) -> None:
        press = self.entity_description.press_fn
        await self.async_control_terminal(lambda api: press(api, self.mac))


class Ac100TerminalSwitch(Ac100TerminalControl, SwitchEntity):
    """An on/off field of a terminal's saved record, unknown while the controller reports none."""

    entity_description: Ac100TerminalSwitchDescription

    def __init__(
        self,
        coordinator: Ac100Coordinator,
        mac: str,
        description: Ac100TerminalSwitchDescription,
    ) -> None:
        super().__init__(coordinator, mac, description.key)
        self.entity_description = description

    @property
    def source_available(self) -> bool:
        description = self.entity_description
        if (
            description.field in (TerminalField.BINDING_ENABLED, TerminalField.FORCE_BINDING)
            and self.client is None
        ):
            return False
        return (
            (configuration := self.configuration) is not None
            and description.state_fn(configuration) is not None
            and description.editable_fn(configuration)
        )

    @property
    def is_on(self) -> bool | None:
        if (configuration := self.configuration) is None:
            return None
        return self.entity_description.state_fn(configuration)

    async def _async_set(self, value: bool) -> None:
        description = self.entity_description
        configuration = self.configuration
        if value and configuration is not None and description.turn_on_blocker is not None:
            if reason := description.turn_on_blocker(configuration):
                raise invalid_value(reason)
        await self.async_control_terminal(
            lambda api: api.async_set_terminal_field(self.mac, description.field, value)
        )

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._async_set(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._async_set(False)


class Ac100TerminalLimitNumber(Ac100TerminalControl, NumberEntity):
    """A rate cap in kB/s: 0 is unlimited, otherwise a whole number the terminal page accepts.

    Like the page, which shows the caps only while rate limiting is on, it is available only then.
    """

    entity_description: Ac100TerminalNumberDescription
    _attr_mode = NumberMode.BOX
    _attr_native_step = 1
    _attr_native_min_value = 0
    _attr_native_max_value = TERMINAL_LIMIT_MAX
    _attr_native_unit_of_measurement = UnitOfDataRate.KILOBYTES_PER_SECOND

    def __init__(
        self,
        coordinator: Ac100Coordinator,
        mac: str,
        description: Ac100TerminalNumberDescription,
    ) -> None:
        super().__init__(coordinator, mac, description.key)
        self.entity_description = description

    @property
    def source_available(self) -> bool:
        return (
            (configuration := self.configuration) is not None
            and configuration.rate_limit_enabled is True
            and self.entity_description.value_fn(configuration) is not None
        )

    @property
    def native_value(self) -> int | None:
        if (configuration := self.configuration) is None:
            return None
        return self.entity_description.value_fn(configuration)

    async def async_set_native_value(self, value: float) -> None:
        field = self.entity_description.field
        limit = _checked(field, value)
        await self.async_control_terminal(
            lambda api: api.async_set_terminal_field(self.mac, field, limit)
        )


class Ac100TerminalNameText(Ac100TerminalControl, TextEntity):
    """The name that the controller keeps for the terminal; it is not the Home Assistant name.

    The device name follows it with the next update. A rename needs the saved record, so the
    control is available while the host table lists the terminal. While that table could not be
    read, the connected terminal's own name is shown and the API decides when it writes. If the
    controller reports a name that the terminal page would not accept, the name is unknown.
    """

    _attr_translation_key = "terminal_name"
    _attr_mode = TextMode.TEXT
    _attr_native_min = 1
    _attr_native_max = TERMINAL_NAME_MAX_BYTES

    def __init__(self, coordinator: Ac100Coordinator, mac: str) -> None:
        super().__init__(coordinator, mac, "name_control")

    @property
    def source_available(self) -> bool:
        if self.configuration is not None:
            return True
        return self.coordinator.data.terminals is None and self.client is not None

    @property
    def native_value(self) -> str | None:
        configuration = self.configuration
        name = None if configuration is None else configuration.name
        if name is None and (client := self.client) is not None:
            name = client.name
        return name if terminal_name_is_valid(name) else None

    async def async_set_value(self, value: str) -> None:
        name = _checked(TerminalField.NAME, value)
        await self.async_control_terminal(
            lambda api: api.async_set_terminal_field(self.mac, TerminalField.NAME, name)
        )


class Ac100TerminalBindingSelect(Ac100TerminalControl, SelectEntity):
    """The AP, or one radio of an AP, that the terminal is bound to.

    Choosing a target also turns binding on, in the same write, because binding cannot be on
    without a target and no target can be chosen while binding is off. The options are the
    targets that the last poll listed, by the AP's device name and the radio's band; the write
    names the target by its stable AP MAC and radio unit, never by a name or a cached address.
    """

    _attr_translation_key = "terminal_binding_target"

    def __init__(self, coordinator: Ac100Coordinator, mac: str) -> None:
        super().__init__(coordinator, mac, "binding_target_control")

    @property
    def source_available(self) -> bool:
        return (
            self.client is not None
            and (configuration := self.configuration) is not None
            and configuration.binding_available
            and bool(configuration.binding_targets)
        )

    @property
    def _labels(self) -> dict[str, str]:
        configuration = self.configuration
        targets = () if configuration is None else configuration.binding_targets
        return binding_target_labels(self.coordinator, targets)

    @property
    def options(self) -> list[str]:
        return list(self._labels.values())

    @property
    def current_option(self) -> str | None:
        configuration = self.configuration
        if configuration is None or configuration.bound_target is None:
            return None
        return self._labels.get(configuration.bound_target)

    async def async_select_option(self, option: str) -> None:
        token = next((token for token, label in self._labels.items() if label == option), None)
        if token is None:
            raise invalid_value("Choose one of the listed access points and radios")
        target = _checked(TerminalField.BINDING_TARGET, token)
        await self.async_control_terminal(
            lambda api: api.async_set_terminal_field(self.mac, TerminalField.BINDING_TARGET, target)
        )
