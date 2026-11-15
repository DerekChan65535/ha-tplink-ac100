"""In-memory logging controls shared by every AC100 config entry."""

import logging
from collections.abc import Iterator

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.util.hass_dict import HassKey

from .const import DOMAIN

LOGGER_NAME = f"custom_components.{DOMAIN}"
SIGNAL_DEBUG_LOGGING_CHANGED = f"{DOMAIN}_debug_logging_changed"
_LOGGER = logging.getLogger(LOGGER_NAME)


def _owned_loggers(logger: logging.Logger = _LOGGER) -> Iterator[logging.Logger]:
    """Walk the integration namespace using the public logger hierarchy."""
    yield logger
    for child in logger.getChildren():
        yield from _owned_loggers(child)


class Ac100DebugLogging:
    """Manage only the integration namespace, without persisted options."""

    def __init__(self, hass: HomeAssistant) -> None:
        """Start each HA runtime with this integration's debug logging off."""
        self.hass = hass
        self._previous_levels: dict[logging.Logger, int] = {}
        self._disable_debug()

    def _disable_debug(self) -> None:
        """Disable inherited and explicit DEBUG levels, including new children."""
        for logger in _owned_loggers():
            if logger.getEffectiveLevel() <= logging.DEBUG:
                logger.setLevel(logging.INFO)

    @property
    def is_on(self) -> bool:
        """Reflect DEBUG enabled anywhere inside this integration namespace."""
        return any(logger.getEffectiveLevel() <= logging.DEBUG for logger in _owned_loggers())

    @callback
    def async_set_enabled(self, enabled: bool) -> None:
        """Change local logging and update every controller's diagnostic switch."""
        if enabled:
            for logger in _owned_loggers():
                self._previous_levels.setdefault(logger, logger.level)
                logger.setLevel(logging.DEBUG)
        else:
            for logger, level in self._previous_levels.items():
                logger.setLevel(level)
            self._previous_levels.clear()
            self._disable_debug()
        async_dispatcher_send(self.hass, SIGNAL_DEBUG_LOGGING_CHANGED)


DATA_DEBUG_LOGGING: HassKey[Ac100DebugLogging] = HassKey(f"{DOMAIN}_debug_logging")


@callback
def async_get_debug_logging(hass: HomeAssistant) -> Ac100DebugLogging:
    """Return the shared, non-persistent logging controls."""
    if DATA_DEBUG_LOGGING not in hass.data:
        hass.data[DATA_DEBUG_LOGGING] = Ac100DebugLogging(hass)
    return hass.data[DATA_DEBUG_LOGGING]
