"""Constants for the TP-Link AC100 integration."""

from datetime import timedelta

from homeassistant.const import Platform

DOMAIN = "tplink_ac100"
NAME = "TP-Link AC100"
CONF_ENABLE_CONTROL = "enable_control"
CONF_MONITOR_WIRELESS_SERVICES = "monitor_wireless_services"
DEFAULT_ENABLE_CONTROL = False
DEFAULT_SCAN_INTERVAL = 10
MIN_SCAN_INTERVAL = 10
MAX_SCAN_INTERVAL = 3600
UPDATE_INTERVAL = timedelta(seconds=DEFAULT_SCAN_INTERVAL)
MONITORING_INTERVAL = timedelta(minutes=5)
CONFIGURATION_INTERVAL = timedelta(minutes=15)
PLATFORMS = [
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.DEVICE_TRACKER,
    Platform.NUMBER,
    Platform.SELECT,
    Platform.SENSOR,
    Platform.SWITCH,
    Platform.TEXT,
    Platform.UPDATE,
]
