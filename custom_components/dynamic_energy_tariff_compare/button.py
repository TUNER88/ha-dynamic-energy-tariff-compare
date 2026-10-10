"""Buttons for Dynamic Energy Tariff Compare."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import DynamicEnergyTariffCompareConfigEntry
from .const import DEFAULT_BACKFILL_DAYS
from .entity import DynamicEnergyTariffCompareEntity, reserve_entity_ids


async def async_setup_entry(
    hass: HomeAssistant,
    entry: DynamicEnergyTariffCompareConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up buttons."""
    m = entry.runtime_data
    reserve_entity_ids(hass, entry, "button", ["backfill", "reset"])
    async_add_entities([BackfillButton(m, "backfill"), ResetButton(m, "reset")])


class BackfillButton(DynamicEnergyTariffCompareEntity, ButtonEntity):
    """Backfill DEFAULT_BACKFILL_DAYS days from recorder statistics."""

    _attr_entity_category = EntityCategory.CONFIG

    async def async_press(self) -> None:
        await self.manager.async_backfill(DEFAULT_BACKFILL_DAYS)


class ResetButton(DynamicEnergyTariffCompareEntity, ButtonEntity):
    """Reset all accumulated totals."""

    _attr_entity_category = EntityCategory.CONFIG

    async def async_press(self) -> None:
        await self.manager.async_reset()
