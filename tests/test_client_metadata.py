"""Optional client details already present in the association list."""

from dataclasses import asdict

import pytest

from custom_components.tplink_ac100.api import _parse_client


def client_row(**fields):
    return {"entry_id": "1", "mac": "02-00-00-00-00-11", **fields}


def test_client_details_are_projected_without_extra_fields():
    client = _parse_client(
        client_row(
            brand="Example vendor",
            operating_system="Example OS",
            vlan_id="27",
            connect_date="2026/11/16",
            connect_time="10:25:33",
            password="secret-canary",
            radius_pwd="radius-canary",
            unrelated="extra-canary",
        )
    )
    assert client.brand == "Example vendor"
    assert client.operating_system == "Example OS"
    assert client.vlan_id == 27
    assert client.connected_at == "2026/11/16 10:25:33"
    assert not any(
        value in repr(asdict(client))
        for value in [
            "secret-canary",
            "radius-canary",
            "extra-canary",
        ]
    )


@pytest.mark.parametrize("value", [None, "", "---", "others", "UNKNOWN", 4, "x" * 65])
def test_unknown_client_identification_is_not_a_vendor_or_os(value):
    client = _parse_client(client_row(brand=value, operating_system=value))
    assert client.brand is None
    assert client.operating_system is None


@pytest.mark.parametrize("value", [None, "0", "---", "4095", "-1", "1.5", True])
def test_unknown_or_invalid_vlan_is_not_a_real_vlan(value):
    assert _parse_client(client_row(vlan_id=value)).vlan_id is None


@pytest.mark.parametrize("value", [1, "1", "4094"])
def test_valid_client_vlan(value):
    assert _parse_client(client_row(vlan_id=value)).vlan_id == int(value)


@pytest.mark.parametrize(
    ("date", "time"),
    [
        (None, "10:25:33"),
        ("2026/11/16", None),
        ("---", "---"),
        ("2026/02/30", "10:25:33"),
        ("2026/11/16", "26:25:33"),
    ],
)
def test_unknown_or_invalid_controller_local_connection_time(date, time):
    assert _parse_client(client_row(connect_date=date, connect_time=time)).connected_at is None


def test_missing_new_fields_do_not_change_presence_or_existing_identity():
    client = _parse_client(client_row(name="Example phone", ip="192.0.2.21"))
    assert client.mac == "02:00:00:00:00:11"
    assert client.name == "Example phone"
    assert client.ip == "192.0.2.21"
    assert (client.brand, client.operating_system, client.vlan_id, client.connected_at) == (
        None,
        None,
        None,
        None,
    )
