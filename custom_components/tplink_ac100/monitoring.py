"""Read-only monitoring and configuration data shared by the API client and entities.

Nothing here holds a raw controller response: every field is an explicit, bounded scalar.
The wire parsing lives in api.py; this module is the schema, including the allow-list of
configuration fields. A setting that is not listed in CONFIGURATION_FIELDS is never read out.
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
class MonitoringState:
    """Slow, credential-free status; a source missing from available_sections is not healthy.

    ``available_sections`` holds the ``module.section`` (or ``module.table``) names that were
    read and recognised in this poll. ``values`` only has keys for available sources, and a
    value the controller reported in an unknown form is None, never False.
    """

    values: dict[str, Scalar] = field(default_factory=dict)
    available_sections: frozenset[str] = frozenset()
    firmware: FirmwareAvailability | None = None
    ap_firmware: tuple[ApFirmware, ...] = ()
    ports: tuple[PortStatus, ...] = ()
    interfaces: tuple[InterfaceTraffic, ...] = ()


@dataclass(frozen=True, slots=True)
class ConfigurationState:
    """Allow-listed controller settings as scalars, keyed by the CONFIGURATION_FIELDS keys."""

    values: dict[str, Scalar] = field(default_factory=dict)
    available_sections: frozenset[str] = frozenset()


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
