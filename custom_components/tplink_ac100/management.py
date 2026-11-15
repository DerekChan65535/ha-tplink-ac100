"""What the controller's web UI lets an administrator change, and the form each page sends.

This is the write catalog. A setting is writable here only if one of the UI's pages edits it,
and a write sends exactly that page's own form: the fields listed below, each one validated as
the page validates it. The form is built from a fresh read of its section, so a write changes
no field except the one it was asked to change (and the field that page ties to it).

The module does no I/O and does not depend on Home Assistant. api.py sends the requests. Each
rule names the controller web page it comes from (L is a line in that page's source).
"""

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

from .monitoring import CONFIGURATION_FIELDS, ConfigField, Scalar

type FieldKind = Literal["switch", "integer", "choice", "text"]

_WHOLE_NUMBER = re.compile(r"-?(?:0|[1-9][0-9]*)")


@dataclass(frozen=True, slots=True)
class FormField:
    """One field of a web page's form, with the rule that page validates it by.

    ``kind`` is switch (on or off), integer (``minimum`` to ``maximum``, as a decimal string),
    choice (one of ``choices``) or text (any non-empty text, only passed through). A field that
    is not ``changeable`` is part of the form but is only sent back as it was read. ``also`` is a
    second field that the page sets together with this one, and the wire value it gets.
    """

    name: str
    kind: FieldKind
    minimum: int = 0
    maximum: int = 0
    choices: tuple[str, ...] = ()
    changeable: bool = True
    also: tuple[str, str] | None = None

    def accepts(self, raw: object) -> bool:
        """Whether a wire value is one that this field's page could have sent."""
        if not isinstance(raw, str):
            return False
        if self.kind == "switch":
            return raw in {"on", "off"}
        if self.kind == "choice":
            return raw in self.choices
        if self.kind == "integer":
            return (
                _WHOLE_NUMBER.fullmatch(raw) is not None
                and self.minimum <= int(raw) <= self.maximum
            )
        return self.kind == "text" and raw != ""


@dataclass(frozen=True, slots=True)
class WriteForm:
    """The form of one settings section, in the page's field order."""

    module: str
    section: str
    fields: tuple[FormField, ...]


def _switch(name: str) -> FormField:
    return FormField(name, "switch")


def _integer(
    name: str, minimum: int, maximum: int, also: tuple[str, str] | None = None
) -> FormField:
    return FormField(name, "integer", minimum, maximum, also=also)


def _choice(name: str, *choices: str) -> FormField:
    return FormField(name, "choice", choices=choices)


def _sent_back(name: str, kind: FieldKind, *choices: str) -> FormField:
    return FormField(name, kind, choices=choices, changeable=False)


# Each form lists every field that its page submits: the page's form sends all of them and
# nothing else, so no other field of the section is part of a write.
FORMS: tuple[WriteForm, ...] = (
    # userrpm__apmng_band_navigation.htm: form L34-L48; enable on/off L57-L71; 5g_threshold 2-40
    # L72-L82; dif_threshold 1-8 L83-L93; max_fail_num 0-100 L94-L104.
    WriteForm(
        "apmng_band_navigation",
        "settings",
        (
            _switch("enable"),
            _integer("5g_threshold", 2, 40),
            _integer("dif_threshold", 1, 8),
            _integer("max_fail_num", 0, 100),
        ),
    ),
    # userrpm__apmng_balance.htm: form L39-L62 (group_id only displays the groups and is removed
    # before the set, L56-L59); enable L75-L81; group_id_str keeps the page's AP group list,
    # L91-L102; mode "session" L109-L115; usr_threshold 2-40 L117-L130; dif_threshold 1-8
    # L133-L146; max_fail_num 1-100 L149-L162; rssi_threshold -95 to 0 L165-L178.
    WriteForm(
        "apmng_balance",
        "settings",
        (
            _switch("enable"),
            _sent_back("group_id_str", "text"),
            _sent_back("mode", "choice", "session"),
            _integer("usr_threshold", 2, 40),
            _integer("dif_threshold", 1, 8),
            _integer("max_fail_num", 1, 100),
            _integer("rssi_threshold", -95, 0),
        ),
    ),
    # userrpm__apmng_bcast_storm_ctrl.htm: enable L69-L76; the submit handler sends enable,
    # storm_rate and type together, L133-L164. The page offers the preset rates 1, 2, 4 ... 1024
    # KBps (L82-L94) and a user-defined rate of 1 to 1024 (L120-L129), and sends the type that
    # goes with each choice (L144-L155): "user_define" for the user-defined rate, "pre_define" for
    # a preset step. Setting a rate here is entering a user-defined one, which the page allows
    # for every value of 1 to 1024, so the type is always "user_define". "pre_define" is never
    # sent: the page shows such a rate that is no preset step as an empty choice (L172-L183).
    WriteForm(
        "apmng_bcast_storm_ctrl",
        "settings",
        (
            _switch("enable"),
            _integer("storm_rate", 1, 1024, also=("type", "user_define")),
            _sent_back("type", "choice", "pre_define", "user_define"),
        ),
    ),
    # userrpm__apmng_igmp_snp.htm: snooping on/off L69-L77; igmp_drop_unknown "1" is the page's
    # Forward and "0" its Drop, L78-L86; igmp_2g_rate 1 or 11 Mbps L87-L96; igmp_5g_rate 6 or 24
    # Mbps L97-L106; form and section L107-L122. Before it submits, the page asks for a
    # confirmation while an online AP model lacks multicast optimization (L133-L145); that is a
    # warning and no rule of the form.
    WriteForm(
        "apmng_wserv",
        "igmp_settings",
        (
            _switch("igmp_snooping_enable"),
            _choice("igmp_drop_unknown", "1", "0"),
            _choice("igmp_2g_rate", "1", "11"),
            _choice("igmp_5g_rate", "6", "24"),
        ),
    ),
)

_FORMS = {(form.module, form.section): form for form in FORMS}


def form_field(item: ConfigField) -> FormField | None:
    """The page field that edits ``item``, or None when no page form holds it."""
    form = _FORMS.get((item.module, item.section))
    return next((part for part in form.fields if part.name == item.field), None) if form else None


def writable_fields(kind: FieldKind) -> tuple[ConfigField, ...]:
    """The catalogued settings that can be changed, whose page field is of ``kind``."""
    return tuple(
        item
        for item in CONFIGURATION_FIELDS
        if (target := form_field(item)) is not None and target.changeable and target.kind == kind
    )


def select_options(item: ConfigField) -> tuple[str, ...]:
    """What a select offers for ``item``: the page's own choices, named as the read names them.

    A setting that is read as a state name offers the states of the choices; one that is read as
    a number offers the numbers, as digit strings.
    """
    target = form_field(item)
    if target is None:
        return ()
    if item.kind == "enum":
        return tuple(
            state for raw, state in item.mapping if isinstance(state, str) and raw in target.choices
        )
    return target.choices


def native_range(item: ConfigField) -> tuple[int, int]:
    """The least and the greatest value of a whole-number setting, in the units of ``item``."""
    target = form_field(item)
    assert target is not None and target.kind == "integer"
    return target.minimum + item.offset, target.maximum + item.offset


def _whole_number(value: Scalar) -> int:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError("Enter a whole number")
    if isinstance(value, float) and not value.is_integer():
        raise ValueError("Enter a whole number")
    return int(value)


def _token(item: ConfigField, value: Scalar) -> str:
    """The wire token of a state: the inverse of the catalog's mapping."""
    for raw, state in item.mapping:
        if state == value and type(state) is type(value):
            return raw
    raise ValueError("Use on or off" if item.kind == "bool" else _choose(item))


def _choose(item: ConfigField) -> str:
    return f"Choose one of: {', '.join(select_options(item))}"


def encode_setting(item: ConfigField, value: Scalar) -> str:
    """The wire value for changing ``item`` to ``value``.

    Raises ValueError, with a message that is fit to show, if no page changes the setting or the
    page would not accept the value. A number is converted with the inverse of ``item.offset``.
    """
    target = form_field(item)
    if target is None or not target.changeable:
        raise ValueError("This setting cannot be changed")
    if item.kind in {"bool", "enum"}:
        raw = _token(item, value)
    else:
        raw = str(_whole_number(value) - item.offset)
    if not target.accepts(raw):
        if target.kind == "integer":
            low, high = native_range(item)
            raise ValueError(f"Enter a whole number from {low} to {high}")
        raise ValueError(_choose(item))
    return raw


def form_request(item: ConfigField, raw: str, section: Mapping[str, Any]) -> dict[str, Any]:
    """The page's complete form with ``item`` set to ``raw``, as one ``set`` request.

    ``section`` is the section as the controller reports it now. Every other field of the form
    is sent back as it was read. If one of them holds a value that the page could not have sent,
    the form is not built (ValueError): such a section is left as it is.
    """
    form = _FORMS[(item.module, item.section)]
    changes = {item.field: raw}
    if (target := form_field(item)) is not None and target.also is not None:
        name, value = target.also
        changes[name] = value
    values: dict[str, str] = {}
    for part in form.fields:
        if part.name in changes:
            values[part.name] = changes[part.name]
        elif part.accepts(current := section.get(part.name)):
            values[part.name] = current
        else:
            raise ValueError(f"{form.module} field {part.name} cannot be sent back unchanged")
    return {"method": "set", form.module: {form.section: values}}


# The controller's name, from the System page: userrpm__system_mgt.htm L69-L76 (not blank, vtype
# string_visible_allow_blank, at most 20 characters), and its set request L48-L67, which sends
# dev_alias alone. The vtype is the expression /^(\S|\x20|\u3000)+$/ in js__su__su.js (line 27):
# visible characters, spaces and the ideographic space. The page counts UTF-16 code units.
ALIAS_FIELD = next(
    item
    for item in CONFIGURATION_FIELDS
    if (item.module, item.section, item.field) == ("system", "sys", "dev_alias")
)
ALIAS_MAX_LENGTH = 20
ALIAS_RULE = f"The name must be 1 to {ALIAS_MAX_LENGTH} visible characters"


def alias_is_valid(value: object) -> bool:
    """Whether the System page would accept ``value`` as the controller's name."""
    if not isinstance(value, str) or not value.strip(" \u3000"):
        return False
    if not all(char.isprintable() or char == "\u3000" for char in value):
        return False
    return len(value) + sum(char > "\uffff" for char in value) <= ALIAS_MAX_LENGTH


# Saving: the top bar sends "do system.save_cfg" once (root.html L844), then polls "get
# system.save_cfg_status" every second until its status is "1" (L818-L835, L852). On the
# controller that was tested, the save took 6.7 s. Here the poll is every 2 s, for 60 s at most.
SAVE_COMPLETE = "1"
SAVE_POLL_INTERVAL = 2.0
SAVE_TIMEOUT = 60.0
