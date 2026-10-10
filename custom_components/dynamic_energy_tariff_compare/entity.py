"""Base entity."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import Entity

from .const import CONF_PROVIDER_NAME, DOMAIN, SIGNAL_UPDATE
from .manager import DynamicEnergyTariffCompareManager


def reserve_entity_ids(hass: HomeAssistant, entry: ConfigEntry, domain: str, keys: list[str]) -> None:
    """Pre-register stable entity IDs like sensor.dynamic_energy_tariff_compare_savings.

    With has_entity_name, HA would derive the entity ID from the device name,
    which contains the optional provider name. Reserving the registry entry with
    a suggested object ID keeps IDs stable and dashboard examples copy-pasteable.
    Existing entries (and user renames) are left untouched.
    """
    registry = er.async_get(hass)
    for key in keys:
        registry.async_get_or_create(
            domain,
            DOMAIN,
            f"{entry.entry_id}_{key}",
            suggested_object_id=f"{DOMAIN}_{key}",
            config_entry=entry,
        )


class DynamicEnergyTariffCompareEntity(Entity):
    """Common base."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, manager: DynamicEnergyTariffCompareManager, key: str) -> None:
        self.manager = manager
        self._attr_unique_id = f"{manager.entry.entry_id}_{key}"
        self._attr_translation_key = key
        provider = (manager.cfg.get(CONF_PROVIDER_NAME) or "").strip()
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, manager.entry.entry_id)},
            name=f"Dynamic Energy Tariff Compare ({provider})" if provider else "Dynamic Energy Tariff Compare",
            manufacturer="Dynamic Energy Tariff Compare",
            model=f"{provider} vs. fixed tariff" if provider else "Dynamic vs. fixed tariff estimator",
            entry_type=DeviceEntryType.SERVICE,
        )

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                SIGNAL_UPDATE.format(self.manager.entry.entry_id),
                self._handle_update,
            )
        )

    @callback
    def _handle_update(self) -> None:
        self.async_write_ha_state()
