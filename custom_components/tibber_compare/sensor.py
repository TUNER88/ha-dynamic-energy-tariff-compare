"""Sensors for Tibber Compare."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import EntityCategory, UnitOfEnergy
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import dt as dt_util

from . import TibberCompareConfigEntry
from . import calc
from .entity import TibberCompareEntity
from .manager import TibberCompareManager

CT_KWH = "ct/kWh"
EUR = "EUR"


@dataclass(frozen=True, kw_only=True)
class TCSensorDescription(SensorEntityDescription):
    """Describes a Tibber Compare sensor."""

    value_fn: Callable[[TibberCompareManager], Any]
    attr_fn: Callable[[TibberCompareManager], dict[str, Any] | None] = lambda m: None


def _r(value: float | None, nd: int = 4) -> float | None:
    return None if value is None else round(value, nd)


def _current_source(m: TibberCompareManager) -> float | None:
    p = m.current_slot_price()
    return None if p is None else round(p[0], 3)


def _current_effective(m: TibberCompareManager) -> float | None:
    p = m.current_slot_price()
    return None if p is None else round(calc.effective_price_ct(p[0], m.params, p[1]), 3)


def _source_attrs(m: TibberCompareManager) -> dict[str, Any]:
    p = m.current_slot_price()
    return {
        "source": m.price_source,
        "price_is_final": m.params.price_is_final,
        "price_entity": m.price_entity,
        "last_error": m.last_price_error,
        "last_energy_charts_fetch": m.last_price_fetch.isoformat() if m.last_price_fetch else None,
        "current_slot_is_final": None if p is None else p[1],
        **m.price_list(effective=False),
    }


def _effective_attrs(m: TibberCompareManager) -> dict[str, Any]:
    p = m.params
    return {
        "formula": "final price as delivered"
        if p.price_is_final
        else "spot * (1 + VAT) + markup + network fee + levies",
        "vat_percent": p.vat_percent,
        "tibber_markup_ct": p.tibber_markup_ct,
        "network_fee_ct": p.network_fee_ct,
        "levies_ct": p.levies_ct,
        "fixed_price_ct": p.fixed_price_ct,
        **m.price_list(effective=True),
    }


def _period_attrs(m: TibberCompareManager) -> dict[str, Any]:
    t = m.acc.totals
    now = m.now_ts()
    months = m.acc.elapsed_months(now)
    savings = m.acc.savings(now)
    return {
        "tracking_since": dt_util.as_local(dt_util.utc_from_timestamp(t.tracking_start)).isoformat()
        if t.tracking_start
        else None,
        "months_tracked": round(months, 3),
        "savings_per_month_eur": round(savings / months, 2) if months > 0.03 else None,
        "projected_savings_per_year_eur": round(savings / months * 12, 2) if months > 0.03 else None,
        "tibber_total_cost_eur": round(m.acc.tibber_total_cost(now), 2),
        "fixed_total_cost_eur": round(m.acc.fixed_total_cost(now), 2),
        "pending_unpriced_kwh": round(m.acc.pending_kwh, 3),
        "backfilled_kwh": round(t.backfilled_kwh, 3),
        "last_backfill": m.last_backfill,
    }


def _avg_spot(m: TibberCompareManager) -> float | None:
    t = m.acc.totals
    if t.priced_energy_kwh <= 0 or m.params.price_is_final:
        return None
    return round(t.spot_weighted_ct_kwh_sum / t.priced_energy_kwh, 3)


SENSORS: tuple[TCSensorDescription, ...] = (
    TCSensorDescription(
        key="current_spot_price",
        native_unit_of_measurement=CT_KWH,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
        value_fn=_current_source,
        attr_fn=_source_attrs,
    ),
    TCSensorDescription(
        key="current_tibber_price",
        native_unit_of_measurement=CT_KWH,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
        value_fn=_current_effective,
        attr_fn=_effective_attrs,
    ),
    TCSensorDescription(
        key="tibber_energy_cost",
        native_unit_of_measurement=EUR,
        device_class=SensorDeviceClass.MONETARY,
        state_class=SensorStateClass.TOTAL,
        suggested_display_precision=2,
        value_fn=lambda m: _r(m.acc.totals.tibber_energy_cost_eur),
    ),
    TCSensorDescription(
        key="fixed_energy_cost",
        native_unit_of_measurement=EUR,
        device_class=SensorDeviceClass.MONETARY,
        state_class=SensorStateClass.TOTAL,
        suggested_display_precision=2,
        value_fn=lambda m: _r(m.acc.totals.fixed_energy_cost_eur),
    ),
    TCSensorDescription(
        key="tibber_total_cost",
        native_unit_of_measurement=EUR,
        device_class=SensorDeviceClass.MONETARY,
        state_class=SensorStateClass.TOTAL,
        suggested_display_precision=2,
        value_fn=lambda m: _r(m.acc.tibber_total_cost(m.now_ts())),
    ),
    TCSensorDescription(
        key="fixed_total_cost",
        native_unit_of_measurement=EUR,
        device_class=SensorDeviceClass.MONETARY,
        state_class=SensorStateClass.TOTAL,
        suggested_display_precision=2,
        value_fn=lambda m: _r(m.acc.fixed_total_cost(m.now_ts())),
    ),
    TCSensorDescription(
        key="savings",
        native_unit_of_measurement=EUR,
        device_class=SensorDeviceClass.MONETARY,
        state_class=SensorStateClass.TOTAL,
        suggested_display_precision=2,
        value_fn=lambda m: _r(m.acc.savings(m.now_ts())),
        attr_fn=_period_attrs,
    ),
    TCSensorDescription(
        key="consumption",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        suggested_display_precision=3,
        value_fn=lambda m: _r(m.acc.totals.energy_kwh),
        attr_fn=lambda m: {"priced_kwh": round(m.acc.totals.priced_energy_kwh, 3)},
    ),
    TCSensorDescription(
        key="avg_tibber_price",
        native_unit_of_measurement=CT_KWH,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
        value_fn=lambda m: _r(m.acc.avg_tibber_price_ct(), 3),
        attr_fn=lambda m: {"consumption_weighted_spot_ct": _avg_spot(m)},
    ),
    TCSensorDescription(
        key="avg_tibber_price_incl_fees",
        native_unit_of_measurement=CT_KWH,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
        value_fn=lambda m: _r(m.acc.avg_tibber_price_incl_fees_ct(m.now_ts()), 3),
    ),
    TCSensorDescription(
        key="avg_fixed_price_incl_fees",
        native_unit_of_measurement=CT_KWH,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
        value_fn=lambda m: _r(m.acc.avg_fixed_price_incl_fees_ct(m.now_ts()), 3),
    ),
    TCSensorDescription(
        key="pending_energy",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        entity_category=EntityCategory.DIAGNOSTIC,
        suggested_display_precision=3,
        value_fn=lambda m: _r(m.acc.pending_kwh),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: TibberCompareConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up sensors."""
    manager = entry.runtime_data
    async_add_entities(TCSensor(manager, d) for d in SENSORS)


class TCSensor(TibberCompareEntity, SensorEntity):
    """Tibber Compare sensor."""

    entity_description: TCSensorDescription
    # Price lists are large; keep them out of the recorder database
    _unrecorded_attributes = frozenset({"today", "tomorrow", "last_backfill"})

    def __init__(self, manager: TibberCompareManager, description: TCSensorDescription) -> None:
        super().__init__(manager, description.key)
        self.entity_description = description

    @property
    def native_value(self) -> Any:
        return self.entity_description.value_fn(self.manager)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        return self.entity_description.attr_fn(self.manager)
