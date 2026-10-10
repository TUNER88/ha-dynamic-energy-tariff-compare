"""Backfill historic consumption from recorder statistics."""

from __future__ import annotations

from datetime import datetime, timedelta
from functools import partial
import logging
from typing import TYPE_CHECKING, Any

from homeassistant.const import ATTR_UNIT_OF_MEASUREMENT, STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.exceptions import HomeAssistantError
from homeassistant.util import dt as dt_util

from . import calc

if TYPE_CHECKING:
    from .manager import DynamicEnergyTariffCompareManager

_LOGGER = logging.getLogger(__name__)


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
) -> tuple[dict[int, float], dict[str, Any]]:
    """Prices of the selected price sensor from recorder history (ct/kWh).

    Sources, best first: 15-min lists in recorded attributes, the recorded
    state timeline, and finally the sensor's statistics (5-min / hourly mean),
    which outlive the purged state history when the sensor has a state class.
    """
    from homeassistant.components.recorder import get_instance  # noqa: PLC0415
    from homeassistant.components.recorder.history import (  # noqa: PLC0415
        get_significant_states,
    )
    from homeassistant.components.recorder.statistics import (  # noqa: PLC0415
        get_metadata,
        statistics_during_period,
    )

    hass = mgr.hass
    entity = mgr.price_entity
    info: dict[str, Any] = {
        "price_slots_from_attributes": 0,
        "price_slots_from_states": 0,
        "price_slots_from_statistics": 0,
    }
    if not entity:
        return {}, info
    instance = get_instance(hass)
    states = await instance.async_add_executor_job(
        get_significant_states,
        hass,
        start - timedelta(hours=1),
        end,
        [entity],
        None,
        True,  # include_start_time_state
        False,  # significant_changes_only
        False,  # minimal_response
        False,  # no_attributes
    )
    from_attrs: dict[int, float] = {}
    timeline: list[tuple[float, float]] = []
    seen_unit: str | None = None
    for state in states.get(entity, []):
        if isinstance(state, dict):
            continue
        unit = state.attributes.get(ATTR_UNIT_OF_MEASUREMENT)
        try:
            from_attrs.update(
                calc.parse_price_attributes(
                    dict(state.attributes), unit, dt_util.get_default_time_zone(), mgr.unit_override
                )
            )
        except Exception:  # noqa: BLE001 - odd attributes must not break backfill
            _LOGGER.debug("Unparsable price attributes in history of %s", entity, exc_info=True)
        if state.state in (STATE_UNKNOWN, STATE_UNAVAILABLE):
            continue
        try:
            value = float(state.state)
        except (TypeError, ValueError):
            continue
        factor, _guessed = calc.state_price_factor(unit, value, mgr.unit_override)
        if factor is not None:
            seen_unit = unit
            timeline.append((state.last_changed.timestamp(), value * factor))
    prices = calc.price_map_from_state_timeline(timeline, start.timestamp(), end.timestamp())
    info["price_slots_from_states"] = len(prices)
    # explicit 15-min lists win over the sampled state timeline
    info["price_slots_from_attributes"] = len(set(from_attrs) - set(prices))
    prices.update(from_attrs)

    # Statistics fallback for slots without state history (e.g. purged)
    meta = await instance.async_add_executor_job(
        partial(get_metadata, hass, statistic_ids={entity})
    )
    stat_unit = None
    if entity in meta:
        stat_unit = meta[entity][1].get("unit_of_measurement")
    if stat_unit is None:
        current = hass.states.get(entity)
        stat_unit = (current.attributes.get(ATTR_UNIT_OF_MEASUREMENT) if current else None) or seen_unit
    factor = mgr.unit_override or calc.price_factor_to_ct_kwh(stat_unit)
    if entity in meta and factor is None:
        # no unit at all: guess from the recorded means
        probe = await instance.async_add_executor_job(
            statistics_during_period, hass, start, end, {entity}, "hour", None, {"mean"}
        )
        factor = calc.guess_price_factor(
            r["mean"] for r in probe.get(entity, []) if r.get("mean") is not None
        )
    if entity in meta and factor is not None:
        added = 0
        for period, length in (("5minute", 300), ("hour", 3600)):
            rows = await instance.async_add_executor_job(
                statistics_during_period, hass, start, end, {entity}, period, None, {"mean"}
            )
            for row in rows.get(entity, []):
                mean = row.get("mean")
                if mean is None:
                    continue
                r_start = _row_ts(row["start"])
                for slot in range(calc.slot_start(r_start), int(r_start) + length, calc.SLOT_SECONDS):
                    if slot not in prices:
                        prices[slot] = float(mean) * factor
                        added += 1
        info["price_slots_from_statistics"] = added
    elif entity in meta:
        info["statistics_unit_unknown"] = stat_unit
    return prices, info


async def async_run_backfill(mgr: DynamicEnergyTariffCompareManager, days: int) -> dict[str, Any]:
    """Backfill `days` days before the currently covered period."""
    if "recorder" not in mgr.hass.config.components:
        raise HomeAssistantError("Recorder is not loaded; backfill needs recorder statistics")
    totals = mgr.acc.totals
    end_ts = totals.tracking_start or mgr.now_ts()
    end = dt_util.utc_from_timestamp(end_ts)
    start = end - timedelta(days=days)

    buckets, info = await _energy_buckets(mgr, start, end)
    if not buckets:
        return {
            "status": "no_statistics",
            "message": f"No recorder statistics found for {mgr.energy_entity} in the requested period",
            **info,
        }
    first = min(b[0] for b in buckets)
    start = dt_util.utc_from_timestamp(first)

    if mgr.tibber:
        # Tibber's API only serves yesterday/today/tomorrow: use the archive of
        # prices fetched so far (refresh first so yesterday is included).
        await mgr.async_refresh_tibber()
        lo, hi = start.timestamp(), end.timestamp()
        prices = {s: ct for s, ct in mgr.prices.items() if lo - calc.SLOT_SECONDS < s < hi}
        archive_from = min(mgr.prices) if mgr.prices else None
        price_info: dict[str, Any] = {
            "price_slots_from_tibber_archive": len(prices),
            "tibber_archive_from": dt_util.as_local(dt_util.utc_from_timestamp(archive_from)).isoformat()
            if archive_from
            else None,
        }
        source = mgr.price_source
    else:
        prices, price_info = await _price_history(mgr, start, end)
        source = mgr.price_entity
    if not prices:
        return {
            "status": "no_price_history",
            "message": (
                f"No price history found for {source} in the requested period; nothing was "
                "backfilled"
                + (
                    ". Tibber's public API only provides yesterday, today and tomorrow; "
                    "older prices are not available"
                    if mgr.tibber
                    else ""
                )
            ),
            **info,
            **price_info,
        }

    history = calc.SlotHistory()
    back = calc.backfill_energy(buckets, prices.get, mgr.params, history)
    unpriced = sum(k for _, k in back.pending)
    if back.priced_energy_kwh <= 0:
        return {
            "status": "no_price_history",
            "message": (
                f"Price history of {source} does not overlap with the recorded "
                "consumption; nothing was backfilled"
            ),
            "unpriced_kwh_skipped": round(unpriced, 3),
            **info,
            **price_info,
        }
    priced_slots = [s for b in buckets for s, _ in calc.split_interval(b[0], b[1], b[2] or 1.0) if s in prices]
    covered_from = min(priced_slots) if priced_slots else first
    calc.merge_backfill(totals, back, covered_from)
    mgr.acc.history.merge(history)  # rolling windows (24 h / 30 d / 365 d)
    mgr.acc.history.compact(dt_util.utcnow().timestamp())
    result = {
        "status": "ok" if unpriced <= 0 else "partial",
        "from": dt_util.as_local(dt_util.utc_from_timestamp(covered_from)).isoformat(),
        "to": dt_util.as_local(end).isoformat(),
        "energy_kwh": round(back.energy_kwh, 3),
        "unpriced_kwh_skipped": round(unpriced, 3),
        "dynamic_energy_cost_eur": round(back.dynamic_energy_cost_eur, 2),
        "fixed_energy_cost_eur": round(back.fixed_energy_cost_eur, 2),
        **price_info,
        **info,
    }
    if unpriced > 0:
        result["message"] = (
            f"{round(unpriced, 3)} kWh had no recorded price of {source} "
            "and were excluded from both tariffs"
        )
    _LOGGER.info("Dynamic Energy Tariff Compare backfill result: %s", result)
    return result
