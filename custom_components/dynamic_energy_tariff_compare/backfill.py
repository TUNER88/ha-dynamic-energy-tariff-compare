"""Backfill historic consumption from recorder statistics."""

from __future__ import annotations

from datetime import datetime, timedelta
import logging
from typing import TYPE_CHECKING, Any

import aiohttp

from homeassistant.const import ATTR_UNIT_OF_MEASUREMENT, STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.exceptions import HomeAssistantError
from homeassistant.util import dt as dt_util

from . import calc

if TYPE_CHECKING:
    from .manager import DynamicEnergyTariffCompareManager

_LOGGER = logging.getLogger(__name__)
EC_CHUNK = timedelta(days=31)


def _row_ts(value: Any) -> float:
    if isinstance(value, datetime):
        return value.timestamp()
    return float(value)


async def _energy_buckets(
    mgr: DynamicEnergyTariffCompareManager, start: datetime, end: datetime
) -> tuple[list[tuple[float, float, float]], dict[str, Any]]:
    """Return [(start, end, kWh)] using 5-min stats where available, else hourly."""
    from homeassistant.components.recorder import get_instance  # noqa: PLC0415
    from homeassistant.components.recorder.statistics import (  # noqa: PLC0415
        statistics_during_period,
    )

    hass = mgr.hass
    instance = get_instance(hass)
    ids = {mgr.energy_entity}
    units = {"energy": "kWh"}
    short = await instance.async_add_executor_job(
        statistics_during_period, hass, start, end, ids, "5minute", units, {"change"}
    )
    short_rows = short.get(mgr.energy_entity, [])
    if short_rows:
        first_short = _row_ts(short_rows[0]["start"])
        # first full hour covered by short-term stats
        boundary = (first_short // 3600 + (1 if first_short % 3600 else 0)) * 3600
    else:
        boundary = end.timestamp()
    end_ts = end.timestamp()

    buckets: list[tuple[float, float, float]] = []
    hourly_used = 0
    if boundary > start.timestamp():
        hourly = await instance.async_add_executor_job(
            statistics_during_period,
            hass,
            start,
            dt_util.utc_from_timestamp(min(boundary, end_ts)),
            ids,
            "hour",
            units,
            {"change"},
        )
        for row in hourly.get(mgr.energy_entity, []):
            r_start, r_end = _row_ts(row["start"]), _row_ts(row["end"])
            change = row.get("change")
            if change is None or r_end > boundary:
                continue
            kwh = float(change)
            if r_end > end_ts:  # partial hour before live tracking began
                kwh *= max(0.0, end_ts - r_start) / (r_end - r_start)
                r_end = end_ts
            buckets.append((r_start, r_end, kwh))
            hourly_used += 1
    short_used = 0
    for row in short_rows:
        r_start, r_end = _row_ts(row["start"]), _row_ts(row["end"])
        change = row.get("change")
        if change is None or r_start < boundary or r_start >= end_ts:
            continue
        kwh = float(change)
        if r_end > end_ts:
            kwh *= (end_ts - r_start) / (r_end - r_start)
            r_end = end_ts
        buckets.append((r_start, r_end, kwh))
        short_used += 1
    info = {
        "hourly_buckets": hourly_used,
        "five_minute_buckets": short_used,
        "five_minute_from": dt_util.as_local(dt_util.utc_from_timestamp(boundary)).isoformat()
        if short_rows
        else None,
    }
    return buckets, info


async def _price_history(
    mgr: DynamicEnergyTariffCompareManager, start: datetime, end: datetime
) -> dict[int, float]:
    """Prices of the selected price sensor from recorder history (ct/kWh)."""
    from homeassistant.components.recorder import get_instance  # noqa: PLC0415
    from homeassistant.components.recorder.history import (  # noqa: PLC0415
        get_significant_states,
    )

    if not mgr.price_entity:
        return {}
    hass = mgr.hass
    states = await get_instance(hass).async_add_executor_job(
        get_significant_states,
        hass,
        start - timedelta(hours=1),
        end,
        [mgr.price_entity],
        None,
        True,  # include_start_time_state
        False,  # significant_changes_only
        False,  # minimal_response
        False,  # no_attributes
    )
    result: dict[int, float] = {}
    timeline: list[tuple[float, float]] = []
    for state in states.get(mgr.price_entity, []):
        if isinstance(state, dict):
            continue
        unit = state.attributes.get(ATTR_UNIT_OF_MEASUREMENT)
        result.update(calc.parse_price_attributes(dict(state.attributes), unit))
        if state.state in (STATE_UNKNOWN, STATE_UNAVAILABLE):
            continue
        factor = calc.price_factor_to_ct_kwh(unit)
        try:
            value = float(state.state)
        except ValueError:
            continue
        if factor is not None:
            timeline.append((state.last_changed.timestamp(), value * factor))
    from_states = calc.price_map_from_state_timeline(timeline, start.timestamp(), end.timestamp())
    # explicit 15-min lists win over the sampled state timeline
    from_states.update(result)
    return from_states


async def async_run_backfill(mgr: DynamicEnergyTariffCompareManager, days: int) -> dict[str, Any]:
    """Backfill `days` days before the currently covered period."""
    from .manager import async_fetch_energy_charts  # noqa: PLC0415

    if "recorder" not in mgr.hass.config.components:
        raise HomeAssistantError("Recorder is not loaded; backfill needs recorder statistics")
    totals = mgr.acc.totals
    end_ts = totals.tracking_start or mgr.now_ts()
    end = dt_util.utc_from_timestamp(end_ts)
    start = end - timedelta(days=days)

    buckets, info = await _energy_buckets(mgr, start, end)
    if not buckets:
        return {"status": "no_statistics", **info}
    first = min(b[0] for b in buckets)
    start = dt_util.utc_from_timestamp(first)

    final = mgr.params.price_is_final
    prices: dict[int, tuple[float, bool]] = {}
    sensor_slots = 0
    for slot, ct in (await _price_history(mgr, start, end)).items():
        prices[slot] = (ct, final)
        sensor_slots += 1

    needed = {s for b in buckets for s, _ in calc.split_interval(b[0], b[1], b[2] or 1.0)}
    missing = needed - set(prices)
    ec_slots = 0
    ec_error = None
    if missing:
        lo = dt_util.utc_from_timestamp(min(missing))
        hi = dt_util.utc_from_timestamp(max(missing) + calc.SLOT_SECONDS)
        cur = lo
        while cur < hi:
            nxt = min(cur + EC_CHUNK, hi)
            try:
                fetched = await async_fetch_energy_charts(mgr.hass, cur, nxt)
            except (aiohttp.ClientError, TimeoutError, ValueError) as err:
                ec_error = str(err)
                _LOGGER.warning("energy-charts backfill fetch failed: %s", err)
                break
            for slot, ct in fetched.items():
                if slot in missing:
                    # energy-charts is always the raw spot price -> surcharges apply
                    prices[slot] = (ct, False)
                    ec_slots += 1
            cur = nxt

    def lookup(slot: int) -> tuple[float, bool] | None:
        return prices.get(slot)

    back = calc.backfill_energy(buckets, lookup, mgr.params)
    calc.merge_backfill(totals, back, first)
    unpriced = sum(k for _, k in back.pending)
    result = {
        "status": "ok",
        "from": dt_util.as_local(start).isoformat(),
        "to": dt_util.as_local(end).isoformat(),
        "energy_kwh": round(back.energy_kwh, 3),
        "unpriced_kwh_skipped": round(unpriced, 3),
        "dynamic_energy_cost_eur": round(back.dynamic_energy_cost_eur, 2),
        "fixed_energy_cost_eur": round(back.fixed_energy_cost_eur, 2),
        "price_slots_from_sensor_history": sensor_slots,
        "price_slots_from_energy_charts": ec_slots,
        "energy_charts_error": ec_error,
        **info,
    }
    _LOGGER.info("Dynamic Energy Tariff Compare backfill result: %s", result)
    return result
