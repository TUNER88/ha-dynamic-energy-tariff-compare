"""Dynamic Energy Tariff Compare: estimate dynamic (spot-price) electricity tariff costs vs. a fixed tariff."""

from __future__ import annotations

import logging

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse, SupportsResponse
from homeassistant.exceptions import ServiceValidationError
import homeassistant.helpers.config_validation as cv
from homeassistant.helpers import entity_registry as er

from .const import (
    ATTR_DAYS,
    ATTR_ENTRY_ID,
    DEFAULT_BACKFILL_DAYS,
    DOMAIN,
    LEGACY_KEYS,
    SERVICE_BACKFILL,
    SERVICE_RESET,
)
from .manager import DynamicEnergyTariffCompareManager

_LOGGER = logging.getLogger(__name__)

# Sensors removed in 0.3.0 (prices are always final all-in prices now)
REMOVED_SENSOR_KEYS = ("current_spot_price",)

PLATFORMS = [Platform.SENSOR, Platform.BUTTON]

type DynamicEnergyTariffCompareConfigEntry = ConfigEntry[DynamicEnergyTariffCompareManager]

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

BACKFILL_SCHEMA = vol.Schema(
    {
        vol.Optional(ATTR_ENTRY_ID): cv.string,
        vol.Optional(ATTR_DAYS, default=DEFAULT_BACKFILL_DAYS): vol.All(
            vol.Coerce(int), vol.Range(min=1, max=730)
        ),
    }
)
RESET_SCHEMA = vol.Schema({vol.Optional(ATTR_ENTRY_ID): cv.string})


def _managers(hass: HomeAssistant, entry_id: str | None) -> list[DynamicEnergyTariffCompareManager]:
    entries = [
        e
        for e in hass.config_entries.async_loaded_entries(DOMAIN)
        if entry_id is None or e.entry_id == entry_id
    ]
    if not entries:
        raise ServiceValidationError("No matching Dynamic Energy Tariff Compare entry is loaded")
    return [e.runtime_data for e in entries]


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    """Register services."""

    async def _backfill(call: ServiceCall) -> ServiceResponse:
        results = {}
        for mgr in _managers(hass, call.data.get(ATTR_ENTRY_ID)):
            results[mgr.entry.entry_id] = await mgr.async_backfill(call.data[ATTR_DAYS])
        return results

    async def _reset(call: ServiceCall) -> None:
        for mgr in _managers(hass, call.data.get(ATTR_ENTRY_ID)):
            await mgr.async_reset()

    hass.services.async_register(
        DOMAIN,
        SERVICE_BACKFILL,
        _backfill,
        schema=BACKFILL_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(DOMAIN, SERVICE_RESET, _reset, schema=RESET_SCHEMA)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: DynamicEnergyTariffCompareConfigEntry) -> bool:
    """Set up from a config entry."""
    manager = DynamicEnergyTariffCompareManager(hass, entry)
    await manager.async_start()
    entry.runtime_data = manager
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    return True


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Migrate old config entries.

    v2 -> v3 (0.4.0): price source (Tibber Germany by postcode or price sensor);
    existing entries keep using their price sensor.
    v1 -> v2 (0.3.0): energy-charts fallback, the "final price" toggle and the
    surcharge fields (markup, grid fee, levies, VAT, grid base fee) were removed.
    The price sensor is now always the final all-in price per kWh.
    """
    if entry.version > 3:
        return False  # downgraded from a future version
    if entry.version == 1:
        data = {k: v for k, v in entry.data.items() if k not in LEGACY_KEYS}
        options = {k: v for k, v in entry.options.items() if k not in LEGACY_KEYS}
        if options.get("price_entity") is None:
            options.pop("price_entity", None)  # cleared in old options -> fall back to data
        registry = er.async_get(hass)
        for key in REMOVED_SENSOR_KEYS:
            if entity_id := registry.async_get_entity_id("sensor", DOMAIN, f"{entry.entry_id}_{key}"):
                registry.async_remove(entity_id)
        hass.config_entries.async_update_entry(entry, data=data, options=options, version=2)
        _LOGGER.info("Migrated %s config entry %s to version 2", DOMAIN, entry.entry_id)
    if entry.version == 2:
        # v3 (0.4.0): price source choice; existing entries keep their price sensor
        data = {**entry.data, "price_source": entry.data.get("price_source", "sensor")}
        hass.config_entries.async_update_entry(entry, data=data, version=3)
        _LOGGER.info("Migrated %s config entry %s to version 3", DOMAIN, entry.entry_id)
    return True


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: DynamicEnergyTariffCompareConfigEntry) -> bool:
    """Unload a config entry."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        await entry.runtime_data.async_stop()
    return unloaded


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Delete stored totals when the entry is removed."""
    from homeassistant.helpers.storage import Store  # noqa: PLC0415

    from .const import STORAGE_VERSION  # noqa: PLC0415

    await Store(hass, STORAGE_VERSION, f"{DOMAIN}.{entry.entry_id}").async_remove()
