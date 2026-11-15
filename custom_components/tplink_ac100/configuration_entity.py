"""Disabled-by-default read-only views of explicitly allow-listed settings."""

from homeassistant.components.binary_sensor import (
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorEntityDescription
from homeassistant.const import EntityCategory
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import CONFIGURATION_CONTEXT, Ac100ConfigEntry, Ac100Coordinator
from .entity import Ac100Entity
from .monitoring import CONFIGURATION_FIELDS, ConfigField, Scalar


def add_configuration_entities(
    entry: Ac100ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
    *,
    binary: bool,
) -> None:
    """Add read-only settings entities; boolean settings are binary sensors, not switches."""
    coordinator = entry.runtime_data
    entity = Ac100ConfigurationBinarySensor if binary else Ac100ConfigurationSensor
    async_add_entities(
        entity(coordinator, field)
        for field in CONFIGURATION_FIELDS
        if (field.kind == "bool") == binary
    )


class Ac100ConfigurationEntity(Ac100Entity):
    """Only a projected scalar reaches an entity; the raw response is discarded."""

    def __init__(self, coordinator: Ac100Coordinator, field: ConfigField) -> None:
        super().__init__(coordinator, field.key)
        self.field = field
        # Only an enabled settings entity makes the coordinator read the settings.
        self.coordinator_context = CONFIGURATION_CONTEXT

    @property
    def available(self) -> bool:
        return (
            super().available
            and (configuration := self.coordinator.data.configuration) is not None
            and self.field.source in configuration.available_sections
        )

    @property
    def value(self) -> Scalar:
        if (configuration := self.coordinator.data.configuration) is None:
            return None
        return configuration.values.get(self.field.key)


class Ac100ConfigurationBinarySensor(Ac100ConfigurationEntity, BinarySensorEntity):
    """A read-only on/off setting with unknown represented by None."""

    def __init__(self, coordinator: Ac100Coordinator, field: ConfigField) -> None:
        super().__init__(coordinator, field)
        self.entity_description = BinarySensorEntityDescription(
            key=field.key,
            translation_key=field.key,
            entity_category=EntityCategory.DIAGNOSTIC,
            entity_registry_enabled_default=False,
        )

    @property
    def is_on(self) -> bool | None:
        value = self.value
        return value if isinstance(value, bool) else None


class Ac100ConfigurationSensor(Ac100ConfigurationEntity, SensorEntity):
    """A numeric, short text or enumerated setting, without a write method."""

    def __init__(self, coordinator: Ac100Coordinator, field: ConfigField) -> None:
        super().__init__(coordinator, field)
        self.entity_description = SensorEntityDescription(
            key=field.key,
            translation_key=field.key,
            device_class=SensorDeviceClass.ENUM if field.kind == "enum" else None,
            options=list(field.options) if field.kind == "enum" else None,
            native_unit_of_measurement=field.unit,
            entity_category=EntityCategory.DIAGNOSTIC,
            entity_registry_enabled_default=False,
        )

    @property
    def native_value(self) -> str | int | float | None:
        value = self.value
        return None if isinstance(value, bool) else value
