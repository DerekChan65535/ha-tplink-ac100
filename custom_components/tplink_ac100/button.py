"""Opt-in reboot buttons; never called as part of polling or setup."""

from homeassistant.components.button import ButtonDeviceClass, ButtonEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import CONF_ENABLE_CONTROL, DOMAIN
from .coordinator import Ac100ConfigEntry, Ac100Coordinator
from .entity import Ac100Entity


async def async_setup_entry(
    hass: HomeAssistant, entry: Ac100ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    if not entry.options.get(CONF_ENABLE_CONTROL, False):
        return
    coordinator = entry.runtime_data
    async_add_entities([Ac100RebootButton(coordinator)])
    known: set[str] = set()

    @callback
    def add_buttons() -> None:
        aps = [ap for ap in coordinator.data.aps if ap.mac not in known]
        known.update(ap.mac for ap in aps)
        async_add_entities(Ac100RebootButton(coordinator, ap.mac) for ap in aps)

    add_buttons()
    entry.async_on_unload(coordinator.async_add_listener(add_buttons))


class Ac100RebootButton(Ac100Entity, ButtonEntity):
    """A disruptive operation requiring both management and entity opt-in."""

    _attr_translation_key = "reboot"
    _attr_device_class = ButtonDeviceClass.RESTART
    _attr_entity_category = EntityCategory.CONFIG
    _attr_entity_registry_enabled_default = False

    def __init__(self, coordinator: Ac100Coordinator, ap_mac: str | None = None) -> None:
        super().__init__(coordinator, "reboot", ap_mac)

    @property
    def available(self) -> bool:
        return super().available and (
            self.ap_mac is None or ((ap := self.access_point) is not None and ap.is_manageable)
        )

    async def async_press(self) -> None:
        async def action() -> None:
            await self.coordinator.async_refresh()
            if not self.available:
                raise HomeAssistantError(
                    translation_domain=DOMAIN, translation_key="control_unavailable"
                )
            if self.ap_mac is None:
                await self.coordinator.api.async_reboot_controller()
            elif (ap := self.access_point) is not None:
                await self.coordinator.api.async_reboot_ap(ap.entry_id)

        await self.coordinator.async_control(action)
