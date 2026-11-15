"""Disabled-by-default read-only views of each AP radio's configured settings."""

from homeassistant.components.binary_sensor import (
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorEntityDescription
from homeassistant.const import EntityCategory
from homeassistant.core import callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import CONFIGURATION_CONTEXT, Ac100ConfigEntry, Ac100Coordinator
from .entity import Ac100Entity, band_label
from .models import ApRadio
from .monitoring import RADIO_FIELDS, RadioField, RadioSettings, Scalar


def add_radio_settings(
    entry: Ac100ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
    *,
    binary: bool,
) -> None:
    """Add a setting entity per radio and field once the status poll lists the radio.

    The status poll already names every AP's radio units, so these entities exist, disabled,
    before any settings read. Enabling one makes the coordinator start that read.
    """
    coordinator = entry.runtime_data
    fields = [field for field in RADIO_FIELDS if (field.kind == "bool") == binary]
    entity = Ac100RadioBinarySensor if binary else Ac100RadioSettingSensor
    known: set[tuple[str, str]] = set()

    @callback
    def add_radios() -> None:
        entities = []
        for ap in coordinator.data.aps:
            for radio in ap.radios:
                if radio.unit and (ap.mac, radio.unit) not in known:
                    known.add((ap.mac, radio.unit))
                    entities.extend(entity(coordinator, ap.mac, radio, field) for field in fields)
        if entities:
            async_add_entities(entities)

    add_radios()
    entry.async_on_unload(coordinator.async_add_listener(add_radios))


class Ac100RadioSettingEntity(Ac100Entity):
    """One setting of one AP radio, identified by the AP's MAC and the radio's unit."""

    def __init__(
        self, coordinator: Ac100Coordinator, ap_mac: str, radio: ApRadio, field: RadioField
    ) -> None:
        super().__init__(coordinator, f"radio_{radio.unit}_{field.key}", ap_mac)
        self.field = field
        self._radio_unit = radio.unit
        self._attr_translation_placeholders = {"band": band_label(radio)}
        # Only an enabled settings entity makes the coordinator read the settings.
        self.coordinator_context = CONFIGURATION_CONTEXT

    @property
    def settings(self) -> RadioSettings | None:
        """The radio's settings; none while the AP is not online or the row was not read."""
        ap = self.access_point
        configuration = self.coordinator.data.configuration
        if ap is None or not ap.is_online or configuration is None:
            return None
        return next(
            (
                radio
                for radio in configuration.radios
                if radio.ap_mac == self.ap_mac and radio.unit == self._radio_unit
            ),
            None,
        )

    @property
    def available(self) -> bool:
        return super().available and self.settings is not None

    @property
    def value(self) -> Scalar:
        return None if (settings := self.settings) is None else settings.values.get(self.field.key)


class Ac100RadioBinarySensor(Ac100RadioSettingEntity, BinarySensorEntity):
    """A read-only on/off radio setting with unknown represented by None."""

    def __init__(
        self, coordinator: Ac100Coordinator, ap_mac: str, radio: ApRadio, field: RadioField
    ) -> None:
        super().__init__(coordinator, ap_mac, radio, field)
        self.entity_description = BinarySensorEntityDescription(
            key=field.key,
            translation_key=f"radio_{field.key}",
            entity_category=EntityCategory.DIAGNOSTIC,
            entity_registry_enabled_default=False,
        )

    @property
    def is_on(self) -> bool | None:
        value = self.value
        return value if isinstance(value, bool) else None


class Ac100RadioSettingSensor(Ac100RadioSettingEntity, SensorEntity):
    """A numeric or enumerated radio setting, without a write method."""

    def __init__(
        self, coordinator: Ac100Coordinator, ap_mac: str, radio: ApRadio, field: RadioField
    ) -> None:
        super().__init__(coordinator, ap_mac, radio, field)
        self.entity_description = SensorEntityDescription(
            key=field.key,
            translation_key=f"radio_{field.key}",
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
