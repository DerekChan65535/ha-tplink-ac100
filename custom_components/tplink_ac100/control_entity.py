"""Shared base of the opt-in entities that write controller-wide settings."""

from homeassistant.const import EntityCategory
from homeassistant.exceptions import ServiceValidationError

from .const import DOMAIN
from .coordinator import CONFIGURATION_CONTEXT, Ac100Coordinator
from .entity import Ac100Entity
from .management import encode_setting
from .monitoring import ConfigField, Scalar


def invalid_value(reason: str) -> ServiceValidationError:
    """The error for a value that the setting's web page would not accept."""
    return ServiceValidationError(
        translation_domain=DOMAIN,
        translation_key="invalid_value",
        translation_placeholders={"reason": reason},
    )


class Ac100SettingControl(Ac100Entity):
    """A writable view of one allow-listed controller setting, off until the user enables it.

    The state comes from the settings read, like the read-only entity of the same setting, and
    the entity is unavailable while that value is unread or unknown. A change is checked against
    the rules of the setting's web page, sent once through the coordinator, and followed by a
    refresh of the reads.
    """

    _attr_entity_category = EntityCategory.CONFIG
    _attr_entity_registry_enabled_default = False

    def __init__(self, coordinator: Ac100Coordinator, field: ConfigField) -> None:
        super().__init__(coordinator, f"{field.key}_control")
        self.field = field
        self._attr_translation_key = field.key
        # Only an enabled settings entity makes the coordinator read the settings.
        self.coordinator_context = CONFIGURATION_CONTEXT

    @property
    def current(self) -> Scalar:
        """The setting as last read; None while it is unread or unknown."""
        configuration = self.coordinator.data.configuration
        if configuration is None or self.field.source not in configuration.available_sections:
            return None
        return configuration.values.get(self.field.key)

    @property
    def available(self) -> bool:
        return super().available and self.current is not None

    async def _async_write(self, value: Scalar) -> None:
        """Change the setting once; a value that its web page would refuse is not sent."""
        try:
            encode_setting(self.field, value)
        except ValueError as err:
            raise invalid_value(str(err)) from err

        async def action() -> None:
            await self.coordinator.api.async_set_setting(self.field, value)

        await self.coordinator.async_control(action)
