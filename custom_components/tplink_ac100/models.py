"""Typed snapshots from the local AC100 protocol, independent of Home Assistant."""

from dataclasses import dataclass

from .monitoring import ConfigurationState, MonitoringState
from .terminal import TerminalState


@dataclass(frozen=True, slots=True)
class Controller:
    """Controller identity and resource use; uptime is seconds."""

    mac: str
    model: str
    hardware_version: str
    firmware_version: str
    cpu_usage: dict[str, float]
    memory_usage: float | None
    uptime: int | None


@dataclass(frozen=True, slots=True)
class ApRadio:
    """One AP radio, with unknown telemetry represented by None."""

    unit: str
    frequency: str
    client_count: int
    channel: int | None
    channel_load: float | None


@dataclass(frozen=True, slots=True)
class AccessPoint:
    """An AP; entry_id is a current command address, not its stable identity."""

    entry_id: str
    name: str
    model: str
    mac: str
    ip: str
    link_status: str
    led_enabled: bool | None
    uptime: int | None
    radios: tuple[ApRadio, ...]

    @property
    def is_online(self) -> bool:
        """Only status 1 is online; status 0 is still connecting."""
        return self.link_status == "1"

    @property
    def is_manageable(self) -> bool:
        """Only offer commands to the current master, not a standby controller."""
        return self.link_status == "1"

    @property
    def client_count(self) -> int:
        """Count associations across the AP's radios."""
        return sum(radio.client_count for radio in self.radios)


@dataclass(frozen=True, slots=True)
class ClientRadio:
    """One client radio or link; signal is dBm and negotiated rate is Mbps."""

    unit: str
    frequency: str
    rssi: int | None
    negotiated_rate: float | None


@dataclass(frozen=True, slots=True)
class Client:
    """A currently observed client; AP name is not a reliable AP identifier.

    The speeds are the controller's current rates in KB/s; None when it reports none.
    ``brand`` and ``operating_system`` are the controller's identification as sent, including
    ``others`` (a category of its own); only blank and placeholder values are None.
    ``ssid_encoding`` is the ``encode`` token as sent ("1" is UTF-8, "2" is GBK), None if absent.
    """

    entry_id: str
    mac: str
    name: str | None
    ip: str
    ipv6: str
    ssid: str
    ap_name: str
    radios: tuple[ClientRadio, ...]
    upload_kilobytes_per_second: float | None = None
    download_kilobytes_per_second: float | None = None
    brand: str | None = None
    operating_system: str | None = None
    vlan_id: int | None = None
    connected_at: str | None = None
    ssid_encoding: str | None = None


@dataclass(frozen=True, slots=True)
class Snapshot:
    """A complete successful poll; absent clients were not observed in this poll.

    ``monitoring`` and ``configuration`` come from separate, slower optional reads and stay
    None until one has succeeded. ``terminals`` is the saved record of every terminal, also of
    those that are away, from the same poll's host table: None if that table could not be read,
    which differs from an empty state. It never says who is connected.
    """

    controller: Controller
    aps: tuple[AccessPoint, ...]
    clients: tuple[Client, ...]
    monitoring: MonitoringState | None = None
    configuration: ConfigurationState | None = None
    terminals: TerminalState | None = None
