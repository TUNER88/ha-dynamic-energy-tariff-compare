"""Runtime manager: tracks the meter, collects prices, persists totals."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import ATTR_UNIT_OF_MEASUREMENT, STATE_UNAVAILABLE, STATE_UNKNOWN, UnitOfEnergy
from homeassistant.core import CALLBACK_TYPE, Event, EventStateChangedData, HomeAssistant, State, callback
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import (
    async_call_later,
    async_track_state_change_event,
    async_track_time_change,
)
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util
from homeassistant.util.unit_conversion import EnergyConverter

from . import calc
from .const import (
    CONF_ENERGY_ENTITY,
    CONF_FIXED_BASE_FEE,
    CONF_FIXED_PRICE,
    CONF_PRICE_ENTITY,
    CONF_PRICE_UNIT,
    CONF_PROVIDER_MONTHLY_FEE,
    DEFAULTS,
    DOMAIN,
    LEGACY_KEYS,
    SIGNAL_UPDATE,
    STORAGE_VERSION,
)

_LOGGER = logging.getLogger(__name__)

PRICE_KEEP_SECONDS = 4 * 86400
STATE_ONLY_LOOKBACK_SLOTS = 4  # state-only sources: reuse price up to 1 h back
WRITE_DEBOUNCE = 5.0


def energy_to_kwh(value: float, unit: Any) -> float:
    """Convert a meter reading to kWh using HA's EnergyConverter (Wh, kWh, MWh, GWh, ...)."""
    if isinstance(unit, str) and unit in EnergyConverter.VALID_UNITS:
        return EnergyConverter.convert(value, unit, UnitOfEnergy.KILO_WATT_HOUR)
    return value * calc.energy_factor_to_kwh(unit if isinstance(unit, str) else None)


def _as_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def entry_config(entry: ConfigEntry) -> dict[str, Any]:
    """Merged data + options with defaults (removed legacy keys dropped)."""
    cfg: dict[str, Any] = dict(DEFAULTS)
    cfg.update(entry.data)
    cfg.update(entry.options)
    for key in LEGACY_KEYS:
        cfg.pop(key, None)
    return cfg


def params_from_config(cfg: dict[str, Any]) -> calc.TariffParams:
    return calc.TariffParams(
        fixed_price_ct=_as_float(cfg.get(CONF_FIXED_PRICE), DEFAULTS[CONF_FIXED_PRICE]),
        fixed_base_fee_eur_month=_as_float(cfg.get(CONF_FIXED_BASE_FEE), DEFAULTS[CONF_FIXED_BASE_FEE]),
        provider_monthly_fee_eur=_as_float(
            cfg.get(CONF_PROVIDER_MONTHLY_FEE), DEFAULTS[CONF_PROVIDER_MONTHLY_FEE]
        ),
    )


class DynamicEnergyTariffCompareManager:
    """Owns state for one config entry."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        self.cfg = entry_config(entry)
        self.params = params_from_config(self.cfg)
        self.energy_entity: str = self.cfg[CONF_ENERGY_ENTITY]
        self.price_entity: str | None = self.cfg.get(CONF_PRICE_ENTITY) or None
        self.unit_override: float | None = calc.override_factor(self.cfg.get(CONF_PRICE_UNIT))
        self._warned_guess = False
        self.store: Store[dict[str, Any]] = Store(
            hass, STORAGE_VERSION, f"{DOMAIN}.{entry.entry_id}"
        )
        self.acc = calc.Accumulator(self.params)
        # slot -> final all-in price in ct/kWh
        self.prices: dict[int, float] = {}
        self._attr_slots: set[int] = set()  # slots known from a price list
        self.price_source = "none"
        self.last_price_error: str | None = None
        self.backfill_lock = asyncio.Lock()
        self.last_backfill: dict[str, Any] | None = None
        self._unsubs: list[CALLBACK_TYPE] = []
        self._write_pending: CALLBACK_TYPE | None = None

    # ------------------------------------------------------------------ setup
    async def async_start(self) -> None:
        data = await self.store.async_load() or {}
        self.acc.totals = calc.Totals.from_dict(data.get("totals"))
        for item in data.get("prices", []):
            # stored as [slot, ct] (<=0.2 also had an is_final flag)
            try:
                self.prices[int(item[0])] = float(item[1])
            except (TypeError, ValueError, IndexError):
                continue
        self.last_backfill = data.get("last_backfill")
        if data.get("energy_entity") not in (None, self.energy_entity):
            _LOGGER.info("Energy sensor changed; re-baselining meter reading")
            self.acc.totals.last_value = None
            self.acc.totals.last_ts = None

        if not self.price_entity:
            self.last_price_error = (
                "No price sensor selected. Open the integration's options and select one."
            )
            _LOGGER.warning("%s: %s", self.entry.title, self.last_price_error)
        if self.price_entity:
            self._unsubs.append(
                async_track_state_change_event(
                    self.hass, [self.price_entity], self._handle_price_event
                )
            )
            self._ingest_price_state(self.hass.states.get(self.price_entity))
        self._unsubs.append(
            async_track_state_change_event(
                self.hass, [self.energy_entity], self._handle_energy_event
            )
        )
        self._unsubs.append(
            async_track_time_change(
                self.hass, self._handle_slot_tick, minute=[0, 15, 30, 45], second=2
            )
        )
        # Process current meter value (counts consumption during downtime)
        self._ingest_energy_state(self.hass.states.get(self.energy_entity))

    async def async_stop(self) -> None:
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()
        if self._write_pending:
            self._write_pending()
            self._write_pending = None
        await self.store.async_save(self._data_to_save())

    # ---------------------------------------------------------------- storage
    @callback
    def _data_to_save(self) -> dict[str, Any]:
        cutoff = dt_util.utcnow().timestamp() - PRICE_KEEP_SECONDS
        return {
            "totals": self.acc.totals.to_dict(),
            "energy_entity": self.energy_entity,
            "prices": [
                [slot, ct]
                for slot, ct in sorted(self.prices.items())
                if slot >= cutoff
            ],
            "last_backfill": self.last_backfill,
        }

    @callback
    def _schedule_save(self) -> None:
        self.store.async_delay_save(self._data_to_save, 30)

    @callback
    def _notify(self, immediate: bool = False) -> None:
        if immediate:
            async_dispatcher_send(self.hass, SIGNAL_UPDATE.format(self.entry.entry_id))
            return
        if self._write_pending is not None:
            return

        @callback
        def _fire(_now: datetime) -> None:
            self._write_pending = None
            async_dispatcher_send(self.hass, SIGNAL_UPDATE.format(self.entry.entry_id))

        self._write_pending = async_call_later(self.hass, WRITE_DEBOUNCE, _fire)

    # ----------------------------------------------------------------- prices
    def price_lookup(self, slot: int) -> float | None:
        if slot in self.prices:
            return self.prices[slot]
        if self.price_entity:
            # state-only sources (core Nord Pool / Tibber): reuse recent value
            for i in range(1, STATE_ONLY_LOOKBACK_SLOTS + 1):
                prev = self.prices.get(slot - i * calc.SLOT_SECONDS)
                if prev is not None and (slot - i * calc.SLOT_SECONDS) not in self._attr_slots:
                    return prev
        return None

    @callback
    def _ingest_price_state(self, state: State | None) -> None:
        if state is None:
            return
        unit = state.attributes.get(ATTR_UNIT_OF_MEASUREMENT)
        try:
            attr_prices = calc.parse_price_attributes(
                dict(state.attributes), unit, dt_util.get_default_time_zone(), self.unit_override
            )
        except Exception:  # noqa: BLE001 - never break on odd attributes
            _LOGGER.debug("Could not parse price attributes of %s", self.price_entity, exc_info=True)
            attr_prices = {}
        if attr_prices:
            for slot, ct in attr_prices.items():
                self.prices[slot] = ct
                self._attr_slots.add(slot)
            self.price_source = f"{self.price_entity} (15-min list)"
        if state.state not in (STATE_UNKNOWN, STATE_UNAVAILABLE, None, ""):
            try:
                value = float(state.state)
            except (TypeError, ValueError):
                value = None
            factor, guessed = calc.state_price_factor(unit, value, self.unit_override)
            if guessed and not self._warned_guess:
                self._warned_guess = True
                _LOGGER.warning(
                    "%s has no usable unit (%r); assuming %s from the value %s. "
                    "Select the price unit in the integration options if this is wrong",
                    self.price_entity,
                    unit,
                    {100.0: "EUR/kWh", 1.0: "ct/kWh", 0.1: "EUR/MWh"}.get(factor, factor),
                    value,
                )
            if value is not None and factor is not None:
                slot = calc.slot_start(dt_util.utcnow().timestamp())
                if slot not in self._attr_slots:
                    self.prices[slot] = value * factor
                    if not attr_prices:
                        self.price_source = f"{self.price_entity} (state)"
            elif value is None and not attr_prices:
                self.last_price_error = f"{self.price_entity} state {state.state!r} is not a number"
        self._prune_prices()
        if self.acc.resolve_pending(self.price_lookup, dt_util.utcnow().timestamp()):
            self._schedule_save()
        self._notify()

    @callback
    def _handle_price_event(self, event: Event[EventStateChangedData]) -> None:
        self._ingest_price_state(event.data["new_state"])

    def _prune_prices(self) -> None:
        cutoff = dt_util.utcnow().timestamp() - PRICE_KEEP_SECONDS
        for slot in [s for s in self.prices if s < cutoff]:
            del self.prices[slot]
            self._attr_slots.discard(slot)

    @callback
    def _handle_slot_tick(self, now: datetime) -> None:
        if self.price_entity:
            self._ingest_price_state(self.hass.states.get(self.price_entity))
        self._notify(immediate=True)

    # ----------------------------------------------------------------- energy
    @callback
    def _ingest_energy_state(self, state: State | None) -> None:
        if state is None or state.state in (STATE_UNKNOWN, STATE_UNAVAILABLE, None, ""):
            return
        try:
            value = float(state.state)
        except ValueError:
            return
        kwh = energy_to_kwh(value, state.attributes.get(ATTR_UNIT_OF_MEASUREMENT))
        ts = state.last_updated.timestamp()
        t = self.acc.totals
        if t.last_ts is not None and ts <= t.last_ts:
            # Same reading as before restart (or older): just keep the baseline
            if t.last_value is None:
                t.last_value = kwh
            return
        self.acc.process_reading(kwh, ts, self.price_lookup)
        self._schedule_save()
        self._notify()

    @callback
    def _handle_energy_event(self, event: Event[EventStateChangedData]) -> None:
        self._ingest_energy_state(event.data["new_state"])

    # ----------------------------------------------------------------- public
    def now_ts(self) -> float:
        return dt_util.utcnow().timestamp()

    def current_slot_price(self) -> float | None:
        return self.price_lookup(calc.slot_start(self.now_ts()))

    def price_list(self) -> dict[str, list[dict[str, Any]]]:
        """Today/tomorrow 15-min price lists in local time."""
        today = dt_util.start_of_local_day()
        result: dict[str, list[dict[str, Any]]] = {}
        for key, offset in (("today", 0), ("tomorrow", 1)):
            day_start = today + timedelta(days=offset)
            day_end = day_start + timedelta(days=1)
            items = []
            for slot in range(int(day_start.timestamp()), int(day_end.timestamp()), calc.SLOT_SECONDS):
                value = self.prices.get(slot)
                if value is None:
                    continue
                items.append(
                    {
                        "start": dt_util.as_local(dt_util.utc_from_timestamp(slot)).isoformat(),
                        "price": round(value, 3),
                    }
                )
            result[key] = items
        return result

    async def async_reset(self) -> None:
        last_value, last_ts = self.acc.totals.last_value, self.acc.totals.last_ts
        self.acc.totals = calc.Totals(
            last_value=last_value,
            last_ts=last_ts,
            tracking_start=self.now_ts(),
            live_start=self.now_ts(),
        )
        self.last_backfill = None
        await self.store.async_save(self._data_to_save())
        self._notify(immediate=True)

    async def async_backfill(self, days: int) -> dict[str, Any]:
        from .backfill import async_run_backfill  # noqa: PLC0415

        async with self.backfill_lock:
            result = await async_run_backfill(self, days)
        self.last_backfill = result
        await self.store.async_save(self._data_to_save())
        self._notify(immediate=True)
        return result

