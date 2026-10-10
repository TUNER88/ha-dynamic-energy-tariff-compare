"""Runtime manager: tracks the meter, collects prices, persists totals."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
import logging
from typing import Any

import aiohttp

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import ATTR_UNIT_OF_MEASUREMENT, STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import CALLBACK_TYPE, Event, EventStateChangedData, HomeAssistant, State, callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import (
    async_call_later,
    async_track_state_change_event,
    async_track_time_change,
    async_track_time_interval,
)
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from . import calc
from .const import (
    BIDDING_ZONE,
    CONF_ENERGY_ENTITY,
    CONF_FIXED_BASE_FEE,
    CONF_FIXED_PRICE,
    CONF_GRID_BASE_FEE,
    CONF_LEVIES,
    CONF_NETWORK_FEE,
    CONF_PRICE_ENTITY,
    CONF_PRICE_IS_FINAL,
    CONF_TIBBER_MARKUP,
    CONF_TIBBER_MONTHLY_FEE,
    CONF_USE_ENERGY_CHARTS,
    CONF_VAT,
    DEFAULTS,
    DOMAIN,
    ENERGY_CHARTS_URL,
    SIGNAL_UPDATE,
    STORAGE_VERSION,
)

_LOGGER = logging.getLogger(__name__)

PRICE_KEEP_SECONDS = 4 * 86400
STATE_ONLY_LOOKBACK_SLOTS = 4  # state-only sources: reuse price up to 1 h back
EC_REFRESH = timedelta(minutes=30)
WRITE_DEBOUNCE = 5.0


def entry_config(entry: ConfigEntry) -> dict[str, Any]:
    """Merged data + options with defaults."""
    cfg: dict[str, Any] = dict(DEFAULTS)
    cfg.update(entry.data)
    cfg.update(entry.options)
    return cfg


def params_from_config(cfg: dict[str, Any]) -> calc.TariffParams:
    return calc.TariffParams(
        fixed_price_ct=float(cfg[CONF_FIXED_PRICE]),
        fixed_base_fee_eur_month=float(cfg[CONF_FIXED_BASE_FEE]),
        tibber_markup_ct=float(cfg[CONF_TIBBER_MARKUP]),
        network_fee_ct=float(cfg[CONF_NETWORK_FEE]),
        levies_ct=float(cfg[CONF_LEVIES]),
        vat_percent=float(cfg[CONF_VAT]),
        tibber_monthly_fee_eur=float(cfg[CONF_TIBBER_MONTHLY_FEE]),
        grid_base_fee_eur_month=float(cfg[CONF_GRID_BASE_FEE]),
        price_is_final=bool(cfg.get(CONF_PRICE_IS_FINAL, False)) and bool(cfg.get(CONF_PRICE_ENTITY)),
    )


async def async_fetch_energy_charts(
    hass: HomeAssistant, start: datetime, end: datetime
) -> dict[int, float]:
    """Fetch DE-LU day-ahead prices (ct/kWh net) from energy-charts.info."""
    session = async_get_clientsession(hass)
    params = {
        "bzn": BIDDING_ZONE,
        "start": start.isoformat(timespec="minutes"),
        "end": end.isoformat(timespec="minutes"),
    }
    async with asyncio.timeout(30):
        resp = await session.get(ENERGY_CHARTS_URL, params=params)
        if resp.status == 404:
            # energy-charts answers 404 when no data exists for the range yet
            return {}
        resp.raise_for_status()
        payload = await resp.json(content_type=None)
    return calc.parse_energy_charts(payload)


class TibberCompareManager:
    """Owns state for one config entry."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        self.cfg = entry_config(entry)
        self.params = params_from_config(self.cfg)
        self.energy_entity: str = self.cfg[CONF_ENERGY_ENTITY]
        self.price_entity: str | None = self.cfg.get(CONF_PRICE_ENTITY) or None
        self.use_energy_charts = self.price_entity is None and bool(
            self.cfg.get(CONF_USE_ENERGY_CHARTS, True)
        )
        self.store: Store[dict[str, Any]] = Store(
            hass, STORAGE_VERSION, f"{DOMAIN}.{entry.entry_id}"
        )
        self.acc = calc.Accumulator(self.params)
        # slot -> (ct/kWh, is_final)
        self.prices: dict[int, tuple[float, bool]] = {}
        self._attr_slots: set[int] = set()  # slots known from a price list
        self.price_source = "none"
        self.last_price_error: str | None = None
        self.last_price_fetch: datetime | None = None
        self.backfill_lock = asyncio.Lock()
        self.last_backfill: dict[str, Any] | None = None
        self._unsubs: list[CALLBACK_TYPE] = []
        self._write_pending: CALLBACK_TYPE | None = None

    # ------------------------------------------------------------------ setup
    async def async_start(self) -> None:
        data = await self.store.async_load() or {}
        self.acc.totals = calc.Totals.from_dict(data.get("totals"))
        for slot, ct, final in data.get("prices", []):
            self.prices[int(slot)] = (float(ct), bool(final))
        self.last_backfill = data.get("last_backfill")
        if data.get("energy_entity") not in (None, self.energy_entity):
            _LOGGER.info("Energy sensor changed; re-baselining meter reading")
            self.acc.totals.last_value = None
            self.acc.totals.last_ts = None

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
        if self.use_energy_charts:
            self._unsubs.append(
                async_track_time_interval(self.hass, self._handle_ec_refresh, EC_REFRESH)
            )
            self.hass.async_create_task(self.async_refresh_energy_charts())
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
                [slot, ct, final]
                for slot, (ct, final) in sorted(self.prices.items())
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
    def price_lookup(self, slot: int) -> tuple[float, bool] | None:
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
        final = self.params.price_is_final
        attr_prices = calc.parse_price_attributes(dict(state.attributes), unit)
        if attr_prices:
            for slot, ct in attr_prices.items():
                self.prices[slot] = (ct, final)
                self._attr_slots.add(slot)
            self.price_source = f"{self.price_entity} (15-min list)"
        if state.state not in (STATE_UNKNOWN, STATE_UNAVAILABLE, None, ""):
            try:
                value = float(state.state)
            except ValueError:
                value = None
            factor = calc.price_factor_to_ct_kwh(unit)
            if value is not None and factor is not None:
                slot = calc.slot_start(dt_util.utcnow().timestamp())
                if slot not in self._attr_slots:
                    self.prices[slot] = (value * factor, final)
                    if not attr_prices:
                        self.price_source = f"{self.price_entity} (state)"
            elif value is not None and factor is None and not attr_prices:
                self.last_price_error = f"Unknown price unit '{unit}' on {self.price_entity}"
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

    async def async_refresh_energy_charts(self) -> None:
        now = dt_util.now()
        start = dt_util.start_of_local_day(now) - timedelta(days=1)
        end = start + timedelta(days=3) - timedelta(minutes=1)
        try:
            fetched = await async_fetch_energy_charts(self.hass, start, end)
        except (aiohttp.ClientError, TimeoutError, ValueError) as err:
            self.last_price_error = f"energy-charts.info: {err}"
            _LOGGER.warning("Could not fetch prices from energy-charts.info: %s", err)
            return
        self.last_price_error = None
        self.last_price_fetch = dt_util.utcnow()
        for slot, ct in fetched.items():
            self.prices[slot] = (ct, False)
        self.price_source = "energy-charts.info (DE-LU day-ahead)"
        self._prune_prices()
        self.acc.resolve_pending(self.price_lookup, dt_util.utcnow().timestamp())
        self._schedule_save()
        self._notify(immediate=True)

    async def _handle_ec_refresh(self, now: datetime) -> None:
        # Every 30 min; after 13:00 local refresh until tomorrow is complete
        await self.async_refresh_energy_charts()

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
        kwh = value * calc.energy_factor_to_kwh(state.attributes.get(ATTR_UNIT_OF_MEASUREMENT))
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

    def current_slot_price(self) -> tuple[float, bool] | None:
        return self.price_lookup(calc.slot_start(self.now_ts()))

    def price_list(self, effective: bool) -> dict[str, list[dict[str, Any]]]:
        """Today/tomorrow 15-min price lists in local time."""
        today = dt_util.start_of_local_day()
        result: dict[str, list[dict[str, Any]]] = {}
        for key, offset in (("today", 0), ("tomorrow", 1)):
            day_start = today + timedelta(days=offset)
            day_end = day_start + timedelta(days=1)
            items = []
            for slot in range(int(day_start.timestamp()), int(day_end.timestamp()), calc.SLOT_SECONDS):
                entry = self.prices.get(slot)
                if entry is None:
                    continue
                ct, final = entry
                value = calc.effective_price_ct(ct, self.params, final) if effective else ct
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

