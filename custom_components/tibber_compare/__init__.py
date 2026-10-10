"""Tibber Compare: estimate Tibber dynamic tariff costs vs. a fixed tariff."""

from __future__ import annotations

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse, SupportsResponse
from homeassistant.exceptions import ServiceValidationError
import homeassistant.helpers.config_validation as cv

from .const import ATTR_DAYS, ATTR_ENTRY_ID, DEFAULT_BACKFILL_DAYS, DOMAIN, SERVICE_BACKFILL, SERVICE_RESET
from .manager import TibberCompareManager

PLATFORMS = [Platform.SENSOR, Platform.BUTTON]

type TibberCompareConfigEntry = ConfigEntry[TibberCompareManager]

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


def _managers(hass: HomeAssistant, entry_id: str | None) -> list[TibberCompareManager]:
    entries = [
        e
        for e in hass.config_entries.async_loaded_entries(DOMAIN)
        if entry_id is None or e.entry_id == entry_id
    ]
    if not entries:
        raise ServiceValidationError("No matching Tibber Compare entry is loaded")
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


async def async_setup_entry(hass: HomeAssistant, entry: TibberCompareConfigEntry) -> bool:
    """Set up from a config entry."""
    manager = TibberCompareManager(hass, entry)
    await manager.async_start()
    entry.runtime_data = manager
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    return True


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: TibberCompareConfigEntry) -> bool:
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
