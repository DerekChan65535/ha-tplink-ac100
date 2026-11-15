"""Read-only monitoring and configuration data shared by the API client and entities.

Nothing here holds a raw controller response: every field is an explicit, bounded scalar.
The wire parsing lives in api.py; this module is the schema, including the allow-lists of
configuration fields. A setting that is not listed in CONFIGURATION_FIELDS (controller settings)
or RADIO_FIELDS (per-radio settings) is never read out.
"""

from dataclasses import dataclass, field
from typing import Literal

type Scalar = str | int | float | bool | None
type ConfigKind = Literal["bool", "number", "text", "enum"]


@dataclass(frozen=True, slots=True)
class FirmwareAvailability:
    """Controller firmware announced by the AC's own cloud check; never a download location.

    ``latest_version`` is None when no newer firmware is announced, or when the announcement has
    no usable version.
    """

    latest_version: str | None = None


@dataclass(frozen=True, slots=True)
class ApFirmware:
    """One AP's installed and advertised firmware."""

    mac: str
    firmware_version: str | None = None
    hardware_version: str | None = None
    latest_version: str | None = None


@dataclass(frozen=True, slots=True)
class PortStatus:
    """A physical controller port; the speed is only set while the link is up.

    ``port_id`` is stable (switch device plus port section name); ``name`` is the label.
    """

    port_id: str
    name: str
    connected: bool | None = None
    speed_mbps: int | None = None


@dataclass(frozen=True, slots=True)
class InterfaceTraffic:
    """Interface rates in kilobytes (not bits) and packets per second, plus byte counters."""

    name: str
    tx_kilobytes_per_second: float | None = None
    rx_kilobytes_per_second: float | None = None
    tx_packets_per_second: float | None = None
    rx_packets_per_second: float | None = None
    tx_bytes: int | None = None
    rx_bytes: int | None = None


@dataclass(frozen=True, slots=True)
class WirelessService:
    """One wireless network (SSID), reduced to the fields that entities use.

    ``service_id`` is the controller's ``serv_id``. It is the identity, because the name can be
    edited. ``name`` is the SSID as the controller sends it. ``security`` is a state of
    WIRELESS_SERVICE_SECURITY. ``vlan`` is the default VLAN (1-4094) of the network's automatic
    radio binding; it is None when no single VLAN is set (the controller sends 0) or a VLAN pool
    is used. A setting reported in an unknown form is None. The controller's SSID table also
    holds Wi-Fi and RADIUS secrets; none of them is kept here.
    """

    service_id: str
    name: str
    enabled: bool | None = None
    broadcast: bool | None = None
    isolation: bool | None = None
    security: str | None = None
    igmp_snooping: bool | None = None
    igmp_querier: bool | None = None
    vlan: int | None = None


@dataclass(frozen=True, slots=True)
class WirelessServiceFlag:
    """An on/off setting of a wireless network, in the controller's own terms.

    ``key`` is the ``WirelessService`` field. ``field`` is the controller's field in its SSID
    table; ``on`` and ``off`` are the two tokens that field takes.
    """

    key: str
    field: str
    on: str
    off: str

    @property
    def states(self) -> dict[str, bool]:
        """The token of each state."""
        return {self.on: True, self.off: False}


# ``enable`` and the IGMP switches take on/off; ``ssidbrd`` (SSID broadcast, the inverse of the
# page's "hide") and ``isolate`` take 1/0. The web UI's enable/disable buttons update ``enable``
# and the IGMP switches alone, and a live test showed that a one-field update of the others is
# applied too.
WIRELESS_SERVICE_FLAGS: tuple[WirelessServiceFlag, ...] = (
    WirelessServiceFlag("enabled", "enable", "on", "off"),
    WirelessServiceFlag("broadcast", "ssidbrd", "1", "0"),
    WirelessServiceFlag("isolation", "isolate", "1", "0"),
    WirelessServiceFlag("igmp_snooping", "igmp_snooping_switch", "on", "off"),
    WirelessServiceFlag("igmp_querier", "igmp_query_switch", "on", "off"),
)

# The ``encryption`` options of the controller's wireless service page: 0 is shown as "---", 2 is
# its WPA/WPA2-Enterprise (the only kind that the 802.1X page lists). The list has no 3; any
# other token is unknown.
WIRELESS_SERVICE_SECURITY: dict[str, str] = {
    "0": "open",
    "1": "wpa_psk_wpa2_psk",
    "2": "wpa_wpa2_enterprise",
    "4": "wpa2_psk_wpa3_sae",
}


@dataclass(frozen=True, slots=True)
class MonitoringState:
    """Slow, credential-free status; a source missing from available_sections is not healthy.

    ``available_sections`` holds the ``module.section`` (or ``module.table``) names that were
    read and recognised in this poll. ``values`` only has keys for available sources, and a
    value the controller reported in an unknown form is None, never False.

    ``wireless_services`` is None unless the opt-in SSID table was read and recognised in this
    poll; () means it was read and the controller has no wireless network.
    """

    values: dict[str, Scalar] = field(default_factory=dict)
    available_sections: frozenset[str] = frozenset()
    firmware: FirmwareAvailability | None = None
    ap_firmware: tuple[ApFirmware, ...] = ()
    ports: tuple[PortStatus, ...] = ()
    interfaces: tuple[InterfaceTraffic, ...] = ()
    wireless_services: tuple[WirelessService, ...] | None = None


@dataclass(frozen=True, slots=True)
class RadioSettings:
    """One AP radio's allow-listed settings, keyed by the RADIO_FIELDS keys.

    ``ap_entry_id`` is the AP's command address at read time (the controller's ``rf_id // 16``),
    not a stable identity; the coordinator sets ``ap_mac`` from the same poll. ``unit`` is the
    radio's ``freq_unit``, as in ``ApRadio.unit``. A value the controller reported in an unknown
    form, or one that does not apply to the radio's current configuration, is None.
    """

    ap_entry_id: str
    unit: str
    values: dict[str, Scalar] = field(default_factory=dict)
    ap_mac: str | None = None


@dataclass(frozen=True, slots=True)
class ConfigurationState:
    """Allow-listed controller settings as scalars, keyed by the CONFIGURATION_FIELDS keys.

    ``radios`` holds each AP radio's settings; it is empty when the radio table was not read,
    which ``available_sections`` records by lacking RADIO_SOURCE.
    """

    values: dict[str, Scalar] = field(default_factory=dict)
    available_sections: frozenset[str] = frozenset()
    radios: tuple[RadioSettings, ...] = ()


@dataclass(frozen=True, slots=True)
class ConfigField:
    """One allow-listed scalar setting; the catalog drives parsing and read-only entities.

    ``key`` is also the entity's translation key; display names live only in strings.json.
    ``kind`` is bool (mapping: raw token -> True/False), enum (mapping: raw token -> short
    snake_case state), number (unit as printed by the controller UI) or text (bounded printable
    string). A raw value that is not in the mapping, or a malformed number or text, becomes
    None. For numbers, ``offset`` is added to the raw value.
    """

    key: str
    module: str
    section: str
    field: str
    kind: ConfigKind
    unit: str | None = None
    mapping: tuple[tuple[str, bool | str], ...] = ()
    offset: int = 0

    @property
    def source(self) -> str:
        """The ConfigurationState.available_sections entry that makes this value current."""
        return f"{self.module}.{self.section}"

    @property
    def options(self) -> tuple[str, ...]:
        """The distinct states an enum can take, in declaration order."""
        return tuple(dict.fromkeys(state for _, state in self.mapping if isinstance(state, str)))


type _Source = tuple[str, str]
type _Mapping = tuple[tuple[str, bool | str], ...]

_ON_OFF: _Mapping = (("on", True), ("off", False))
_WEEKDAYS: _Mapping = (
    ("eve", "every_day"),
    ("sun", "sunday"),
    ("mon", "monday"),
    ("tue", "tuesday"),
    ("wed", "wednesday"),
    ("thu", "thursday"),
    ("fri", "friday"),
    ("sat", "saturday"),
)


def _switch(key: str, source: _Source, field_name: str) -> ConfigField:
    return ConfigField(key, *source, field_name, "bool", mapping=_ON_OFF)


def _number(
    key: str, source: _Source, field_name: str, unit: str | None = None, offset: int = 0
) -> ConfigField:
    return ConfigField(key, *source, field_name, "number", unit, offset=offset)


def _text(key: str, source: _Source, field_name: str) -> ConfigField:
    return ConfigField(key, *source, field_name, "text")


def _enum(key: str, source: _Source, field_name: str, mapping: _Mapping) -> ConfigField:
    return ConfigField(key, *source, field_name, "enum", mapping=mapping)


_BAND_NAVIGATION = ("apmng_band_navigation", "settings")
_LOAD_BALANCE = ("apmng_balance", "settings")
_RADIO_OPTIMIZATION = ("apmng_rrm", "settings")
_ROAMING = ("apmng_roam", "roam")
_STORM_CONTROL = ("apmng_bcast_storm_ctrl", "settings")
_IGMP = ("apmng_wserv", "igmp_settings")
_SYSTEM = ("system", "sys")
_NTP = ("system", "ntp")
_TIME_TYPE = ("system", "time_type")
_SERVICE_PORT = ("system", "service_port")
_AP_TELNET = ("diagnose", "settings")
_AP_MANAGEMENT = ("apmng_set", "settings")
_WIRELESS_REPORT = ("apmng_report", "settings")
_SECURITY_AUDIT = ("apmng_audit", "settings")

# Every entry is a setting whose meaning, range and unit the controller's own web UI shows.
# Sections also carry other fields (proxy, report server, backhaul and account data); those are
# deliberately absent here and therefore never copied.
CONFIGURATION_FIELDS: tuple[ConfigField, ...] = (
    _switch("band_navigation_enabled", _BAND_NAVIGATION, "enable"),
    _number("band_navigation_5g_threshold", _BAND_NAVIGATION, "5g_threshold"),
    _number("band_navigation_difference_threshold", _BAND_NAVIGATION, "dif_threshold"),
    _number("band_navigation_max_failures", _BAND_NAVIGATION, "max_fail_num"),
    _switch("load_balance_enabled", _LOAD_BALANCE, "enable"),
    _enum("load_balance_mode", _LOAD_BALANCE, "mode", (("session", "session"),)),
    _number("load_balance_threshold", _LOAD_BALANCE, "usr_threshold"),
    _number("load_balance_difference_threshold", _LOAD_BALANCE, "dif_threshold"),
    _number("load_balance_max_failures", _LOAD_BALANCE, "max_fail_num"),
    _number("load_balance_rssi_threshold", _LOAD_BALANCE, "rssi_threshold", "dBm"),
    _switch("rrm_channel_optimization", _RADIO_OPTIMIZATION, "chnl_enable"),
    _switch("rrm_power_optimization", _RADIO_OPTIMIZATION, "pwr_enable"),
    _switch("rrm_scheduled_optimization", _RADIO_OPTIMIZATION, "autorrm_enable"),
    _enum(
        "rrm_band",
        _RADIO_OPTIMIZATION,
        "freq_enable",
        (("0", "both"), ("1", "2_4ghz"), ("2", "5ghz")),
    ),
    _enum("rrm_2g_bandwidth", _RADIO_OPTIMIZATION, "bw_2g", (("20M", "20mhz"),)),
    _enum(
        "rrm_5g_bandwidth",
        _RADIO_OPTIMIZATION,
        "bw_5g",
        (("20M", "20mhz"), ("40M", "40mhz"), ("80M", "80mhz"), ("160M", "160mhz")),
    ),
    _number("rrm_coverage_threshold", _RADIO_OPTIMIZATION, "pwr_threshold", "dBm"),
    _number("rrm_max_power", _RADIO_OPTIMIZATION, "pwr_max_pwr", "dBm"),
    _number("rrm_min_power", _RADIO_OPTIMIZATION, "pwr_min_pwr", "dBm"),
    _enum("rrm_schedule_day", _RADIO_OPTIMIZATION, "autorrm_date", _WEEKDAYS),
    _text("rrm_schedule_time", _RADIO_OPTIMIZATION, "autorrm_time"),
    _switch("roaming_11k", _ROAMING, "11k_switch"),
    _switch("roaming_11v", _ROAMING, "11v_switch"),
    _switch("roaming_11r", _ROAMING, "11r_switch"),
    _number("roaming_2g_rssi_threshold", _ROAMING, "2g_rssi_trigger_threshold", "dBm"),
    _number("roaming_5g_rssi_threshold", _ROAMING, "5g_rssi_trigger_threshold", "dBm"),
    _number(
        "roaming_high_performance_rssi_threshold",
        _ROAMING,
        "rssi_trigger_threshold_high_performance",
        "dBm",
    ),
    _number("roaming_2g_rate_threshold", _ROAMING, "2g_rate_trigger_threshold", "%"),
    _number("roaming_5g_rate_threshold", _ROAMING, "5g_rate_trigger_threshold", "%"),
    _switch("roaming_2g_low_rssi_kick", _ROAMING, "2g_low_rssi_kick"),
    _switch("roaming_5g_low_rssi_kick", _ROAMING, "5g_low_rssi_kick"),
    _number("roaming_2g_low_rssi_kick_threshold", _ROAMING, "2g_low_rssi_threshold", "dBm"),
    _number("roaming_5g_low_rssi_kick_threshold", _ROAMING, "5g_low_rssi_threshold", "dBm"),
    _switch("roaming_2g_low_rate_kick", _ROAMING, "2g_low_rate_kick"),
    _switch("roaming_5g_low_rate_kick", _ROAMING, "5g_low_rate_kick"),
    _number("roaming_2g_low_rate_kick_threshold", _ROAMING, "2g_low_rate_threshold", "%"),
    _number("roaming_5g_low_rate_kick_threshold", _ROAMING, "5g_low_rate_threshold", "%"),
    _number("roaming_check_interval", _ROAMING, "threshold_check_interval", "s"),
    _number("roaming_difference", _ROAMING, "roaming_dif", "dB"),
    _number("roaming_blacklist_time", _ROAMING, "blacklist_restrict_time", "s"),
    _switch("roaming_probe_report", _ROAMING, "stats_switch"),
    _number("roaming_probe_report_interval", _ROAMING, "stats_interval", "s"),
    _switch("broadcast_storm_control_enabled", _STORM_CONTROL, "enable"),
    _number("broadcast_storm_rate", _STORM_CONTROL, "storm_rate", "kB/s"),
    _enum(
        "broadcast_storm_rate_type",
        _STORM_CONTROL,
        "type",
        (("pre_define", "predefined"), ("user_define", "user_defined")),
    ),
    _switch("igmp_snooping_enabled", _IGMP, "igmp_snooping_enable"),
    _enum("igmp_unknown_multicast", _IGMP, "igmp_drop_unknown", (("1", "forward"), ("0", "drop"))),
    _number("igmp_2g_multicast_rate", _IGMP, "igmp_2g_rate", "Mbit/s"),
    _number("igmp_5g_multicast_rate", _IGMP, "igmp_5g_rate", "Mbit/s"),
    _text("device_alias", _SYSTEM, "dev_alias"),
    _switch("ap_detection_enabled", _SYSTEM, "apmng_detection"),
    _text("ntp_primary_server", _NTP, "pri_ntp"),
    _text("ntp_secondary_server", _NTP, "snd_ntp"),
    # The UI's time-zone list counts minutes from UTC-12:00 (720 is GMT, 1200 is GMT+8).
    _number("time_zone_offset", _NTP, "timezone", "min", offset=-720),
    _enum(
        "time_source",
        _TIME_TYPE,
        "type",
        (("auto", "network"), ("manual", "manual"), ("cloud", "cloud")),
    ),
    _number("web_session_timeout", _SERVICE_PORT, "web_timeout", "min"),
    _number("login_max_attempts", _SERVICE_PORT, "max_trytimes"),
    _number("login_lock_duration", _SERVICE_PORT, "lock_time", "min"),
    _switch("ap_telnet_enabled", _AP_TELNET, "enable"),
    _switch("ap_management_enabled", _AP_MANAGEMENT, "apmngr_status"),
    _switch("ap_auto_adopt_enabled", _AP_MANAGEMENT, "auto_firm"),
    _switch("ap_offline_self_management", _AP_MANAGEMENT, "dft_offline_selfmanage"),
    _switch("ap_auto_reboot_enabled", _AP_MANAGEMENT, "auto_reboot"),
    _enum("ap_reboot_day", _AP_MANAGEMENT, "reboot_day", _WEEKDAYS),
    _text("ap_reboot_time", _AP_MANAGEMENT, "reboot_time"),
    _switch("wireless_report_enabled", _WIRELESS_REPORT, "state"),
    _enum(
        "wireless_report_protocol",
        _WIRELESS_REPORT,
        "protocol",
        (("TCP", "tcp"), ("UDP", "udp"), ("HTTP", "http")),
    ),
    _number("wireless_report_interval", _WIRELESS_REPORT, "interval", "s"),
    _number("wireless_report_max_payload", _WIRELESS_REPORT, "max_load"),
    _switch("security_audit_enabled", _SECURITY_AUDIT, "enable"),
)


def _sources(fields: tuple[ConfigField, ...]) -> dict[str, tuple[str, ...]]:
    modules: dict[str, dict[str, None]] = {}
    for item in fields:
        modules.setdefault(item.module, {})[item.section] = None
    return {module: tuple(sections) for module, sections in modules.items()}


# The sections to request, per module, derived from the catalog so the two cannot disagree.
CONFIGURATION_SOURCES = _sources(CONFIGURATION_FIELDS)

RADIO_MODULE = "apmng_rf"
RADIO_TABLE = "rf_entry"
# The ConfigurationState.available_sections entry that marks the radio table as read.
RADIO_SOURCE = f"{RADIO_MODULE}.{RADIO_TABLE}"


@dataclass(frozen=True, slots=True)
class RadioField:
    """One allow-listed scalar of an AP radio's row in the radio table.

    ``key`` ends the entity's unique ID and follows ``radio_`` in its translation key. ``kind``
    is bool, enum or number. ``unit`` is as the controller UI prints it.

    ``mapping`` turns a raw token into a state: True or False for a bool, a short snake_case name
    for an enum, a named value for a number (channel -1 is "auto"). The value is None for any
    other token of a bool or enum, for a number that is malformed or outside
    ``minimum``..``maximum``, and for a setting that does not apply: ``applies_when`` is a
    (raw field, raw value) pair that the same row must hold.
    """

    key: str
    field: str
    kind: Literal["bool", "number", "enum"]
    unit: str | None = None
    mapping: _Mapping = ()
    minimum: int | None = None
    maximum: int | None = None
    applies_when: tuple[str, str] | None = None

    @property
    def options(self) -> tuple[str, ...]:
        """The distinct states an enum can take, in declaration order."""
        return tuple(dict.fromkeys(state for _, state in self.mapping if isinstance(state, str)))


# Radio modes as the controller UI lists them: the 802.11 amendments a radio runs. Mode 8
# ("ac only") is commented out of the UI and has no token here.
_RADIO_MODES: _Mapping = (
    ("0", "802_11a"),
    ("1", "802_11b"),
    ("2", "802_11g"),
    ("3", "802_11n_2_4ghz"),
    ("4", "802_11n_5ghz"),
    ("5", "802_11b_g"),
    ("6", "802_11b_g_n"),
    ("7", "802_11a_n"),
    ("9", "802_11a_n_ac"),
    ("10", "802_11b_g_n_ax"),
    ("11", "802_11a_n_ac_ax"),
    ("12", "802_11b_g_n_ax_be"),
    ("13", "802_11a_n_ac_ax_be"),
)
_RADIO_WIDTHS: _Mapping = (
    ("2", "auto"),
    ("0", "20mhz"),
    ("1", "40mhz"),
    ("3", "80mhz"),
    ("4", "80_plus_80mhz"),
    ("5", "160mhz"),
    ("6", "240mhz"),
)
_RADIO_CHANNEL_SWITCHING: _Mapping = (("0", "auto"), ("1", "manual"), ("2", "off"))

# Every entry is a setting that the controller's radio page shows with its meaning and unit. The
# row's other fields (names, capability limits, EDCA queues, DCS tuning, 802.11ax features and
# the like) are deliberately absent here and therefore never copied.
RADIO_FIELDS: tuple[RadioField, ...] = (
    RadioField("enabled", "rf_state", "bool", mapping=_ON_OFF),
    RadioField("mode", "mode", "enum", mapping=_RADIO_MODES),
    RadioField("channel_width", "bandwidth", "enum", mapping=_RADIO_WIDTHS),
    # The configured channel; -1 is Auto, which is not the channel the AP currently uses.
    RadioField("configured_channel", "channel", "number", mapping=(("-1", "auto"),), minimum=1),
    # The UI offers dynamic channel switching only while the channel is Auto.
    RadioField(
        "channel_switching",
        "rf_chnl_dcs",
        "enum",
        mapping=_RADIO_CHANNEL_SWITCHING,
        applies_when=("channel", "-1"),
    ),
    # A level that the UI shows as Lv1..Lv10, not a power in dBm.
    RadioField("power_level", "power", "number", minimum=1, maximum=10),
    RadioField("client_limit", "max_usr", "number"),
    # Time units (TU, 1.024 ms each), as the UI shows them.
    RadioField("beacon_interval", "bcn_intvl", "number"),
    RadioField("dtim_period", "dtim_prd", "number"),
    RadioField("rts_threshold", "rts_thr", "number", "B"),
    RadioField("fragmentation_threshold", "frag_thr", "number", "B"),
    RadioField("short_guard_interval", "short_gi", "bool", mapping=_ON_OFF),
    RadioField("wmm", "wmm", "bool", mapping=_ON_OFF),
    RadioField("airtime_scheduling", "airtime", "bool", mapping=_ON_OFF),
    RadioField("weak_signal_restriction", "rssi_restrict", "bool", mapping=_ON_OFF),
    # While a restriction or disconnect rule is off, the row holds a placeholder threshold.
    RadioField(
        "weak_signal_restriction_threshold",
        "rssi_restrict_val",
        "number",
        "dBm",
        applies_when=("rssi_restrict", "on"),
    ),
    RadioField("weak_signal_disconnect", "rssi_kick", "bool", mapping=_ON_OFF),
    RadioField(
        "weak_signal_disconnect_threshold",
        "rssi_kick_val",
        "number",
        "dBm",
        applies_when=("rssi_kick", "on"),
    ),
    # The UI labels these "Wireless Client Forward Access" and "Signal Strength Threshold".
    RadioField("client_forward_access", "reject_enable", "bool", mapping=_ON_OFF),
    RadioField("client_forward_access_threshold", "reject_limit", "number", "dBm"),
    RadioField("broadcast_probe_response", "brd_probe", "bool", mapping=_ON_OFF),
)
