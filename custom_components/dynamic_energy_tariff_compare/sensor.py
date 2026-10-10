"""Sensors for Dynamic Energy Tariff Compare."""

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
from homeassistant.const import PERCENTAGE, EntityCategory, UnitOfEnergy
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import dt as dt_util

from . import DynamicEnergyTariffCompareConfigEntry, calc
from .entity import DynamicEnergyTariffCompareEntity, reserve_entity_ids
from .manager import DynamicEnergyTariffCompareManager

CT_KWH = "ct/kWh"
EUR = "EUR"


@dataclass(frozen=True, kw_only=True)
class TCSensorDescription(SensorEntityDescription):
    """Describes a Dynamic Energy Tariff Compare sensor."""

    value_fn: Callable[[DynamicEnergyTariffCompareManager], Any]
    attr_fn: Callable[[DynamicEnergyTariffCompareManager], dict[str, Any] | None] = lambda m: None


def _r(value: float | None, nd: int = 4) -> float | None:
    return None if value is None else round(value, nd)


def _current_price(m: DynamicEnergyTariffCompareManager) -> float | None:
    p = m.current_slot_price()
    return None if p is None else round(p, 3)


def _price_attrs(m: DynamicEnergyTariffCompareManager) -> dict[str, Any]:
    attrs: dict[str, Any] = {
        "source": m.price_source,
        "price_entity": m.price_entity,
        "last_error": m.last_price_error,
        "fixed_price_ct": m.params.fixed_price_ct,
    }
    if m.tibber:
        comps = m.current_components()
        attrs["postcode"] = m.postcode
        attrs["components_ct"] = {k: round(v, 3) for k, v in comps.items()} if comps else None
        attrs["tibber_monthly_fees_eur"] = m.monthly_fees
        attrs["last_price_fetch"] = m.last_price_fetch.isoformat() if m.last_price_fetch else None
    attrs.update(m.price_list())
    return attrs


def _period_attrs(m: DynamicEnergyTariffCompareManager) -> dict[str, Any]:
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
        "dynamic_total_cost_eur": round(m.acc.dynamic_total_cost(now), 2),
        "fixed_total_cost_eur": round(m.acc.fixed_total_cost(now), 2),
        "pending_unpriced_kwh": round(m.acc.pending_kwh, 3),
        "backfilled_kwh": round(t.backfilled_kwh, 3),
        "last_backfill": m.last_backfill,
    }


def _ts_local(ts: float | None) -> str | None:
    return dt_util.as_local(dt_util.utc_from_timestamp(ts)).isoformat() if ts is not None else None


def _window_attrs(m: DynamicEnergyTariffCompareManager, key: str) -> dict[str, Any]:
    w = m.window_stats(key)
    return {
        "window": key,
        "window_start": _ts_local(w.start_ts),
        "covered_from": _ts_local(w.covered_from),
        "consumption_kwh": round(w.priced_kwh, 3),
        "unpriced_kwh": round(max(0.0, w.energy_kwh - w.priced_kwh), 3),
        "avg_dynamic_price_ct": _r(w.avg_dynamic_price_ct, 3),
        "avg_dynamic_price_incl_fees_ct": _r(w.avg_dynamic_price_incl_fees_ct, 3),
        "avg_fixed_price_ct": _r(w.avg_fixed_price_ct, 3),
        "avg_fixed_price_incl_fees_ct": _r(w.avg_fixed_price_incl_fees_ct, 3),
        "dynamic_energy_cost_eur": round(w.dynamic_energy_cost_eur, 2),
        "fixed_energy_cost_eur": round(w.fixed_energy_cost_eur, 2),
        "dynamic_fees_eur": round(w.dynamic_fees_eur, 2),
        "fixed_fees_eur": round(w.fixed_fees_eur, 2),
        "dynamic_total_cost_eur": round(w.dynamic_total_cost_eur, 2),
        "fixed_total_cost_eur": round(w.fixed_total_cost_eur, 2),
        "savings_eur": round(w.savings_eur, 2),
        "savings_percent": _r(w.savings_percent, 2),
        "energy_savings_percent": _r(w.energy_savings_percent, 2),
    }


def _window_sensors(key: str) -> tuple[TCSensorDescription, ...]:
    return (
        TCSensorDescription(
            key=f"avg_dynamic_price_{key}",
            native_unit_of_measurement=CT_KWH,
            state_class=SensorStateClass.MEASUREMENT,
            suggested_display_precision=2,
            value_fn=lambda m: _r(m.window_stats(key).avg_dynamic_price_ct, 3),
            attr_fn=lambda m: _window_attrs(m, key),
        ),
        TCSensorDescription(
            key=f"savings_{key}",
            native_unit_of_measurement=EUR,
            device_class=SensorDeviceClass.MONETARY,
            suggested_display_precision=2,
            value_fn=lambda m: _r(m.window_stats(key).savings_eur),
            attr_fn=lambda m: _window_attrs(m, key),
        ),
        TCSensorDescription(
            key=f"savings_percent_{key}",
            native_unit_of_measurement=PERCENTAGE,
            state_class=SensorStateClass.MEASUREMENT,
            suggested_display_precision=1,
            value_fn=lambda m: _r(m.window_stats(key).savings_percent, 2),
            attr_fn=lambda m: _window_attrs(m, key),
        ),
    )


def _lifetime_percent_attrs(m: DynamicEnergyTariffCompareManager) -> dict[str, Any]:
    now = m.now_ts()
    acc = m.acc
    pending_fixed = acc.pending_kwh * acc.params.fixed_price_ct / 100.0
    return {
        "energy_savings_percent": _r(acc.energy_savings_percent(), 2),
        "savings_eur": round(acc.savings(now), 2),
        "dynamic_total_cost_eur": round(acc.dynamic_total_cost(now), 2),
        "fixed_total_cost_eur": round(acc.fixed_total_cost(now) - pending_fixed, 2),
        "consumption_kwh": round(acc.totals.priced_energy_kwh, 3),
        "tracking_since": _ts_local(acc.totals.tracking_start),
    }


SENSORS: tuple[TCSensorDescription, ...] = (
    TCSensorDescription(
        key="current_dynamic_price",
        native_unit_of_measurement=CT_KWH,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
        value_fn=_current_price,
        attr_fn=_price_attrs,
    ),
    TCSensorDescription(
        key="dynamic_energy_cost",
        native_unit_of_measurement=EUR,
        device_class=SensorDeviceClass.MONETARY,
        state_class=SensorStateClass.TOTAL,
        suggested_display_precision=2,
        value_fn=lambda m: _r(m.acc.totals.dynamic_energy_cost_eur),
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
        key="dynamic_total_cost",
        native_unit_of_measurement=EUR,
        device_class=SensorDeviceClass.MONETARY,
        state_class=SensorStateClass.TOTAL,
        suggested_display_precision=2,
        value_fn=lambda m: _r(m.acc.dynamic_total_cost(m.now_ts())),
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
        key="avg_dynamic_price",
        native_unit_of_measurement=CT_KWH,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
        value_fn=lambda m: _r(m.acc.avg_dynamic_price_ct(), 3),
    ),
    TCSensorDescription(
        key="avg_dynamic_price_incl_fees",
        native_unit_of_measurement=CT_KWH,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
        value_fn=lambda m: _r(m.acc.avg_dynamic_price_incl_fees_ct(m.now_ts()), 3),
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
    TCSensorDescription(
        key="savings_percent",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        value_fn=lambda m: _r(m.acc.savings_percent(m.now_ts()), 2),
        attr_fn=_lifetime_percent_attrs,
    ),
    *(d for key in calc.WINDOWS for d in _window_sensors(key)),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: DynamicEnergyTariffCompareConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up sensors."""
    manager = entry.runtime_data
    reserve_entity_ids(hass, entry, "sensor", [d.key for d in SENSORS])
    async_add_entities(TCSensor(manager, d) for d in SENSORS)


class TCSensor(DynamicEnergyTariffCompareEntity, SensorEntity):
    """Dynamic Energy Tariff Compare sensor."""

    entity_description: TCSensorDescription
    # Price lists are large; keep them out of the recorder database
    _unrecorded_attributes = frozenset({"today", "tomorrow", "last_backfill"})

    def __init__(self, manager: DynamicEnergyTariffCompareManager, description: TCSensorDescription) -> None:
        super().__init__(manager, description.key)
        self.entity_description = description

    @property
    def native_value(self) -> Any:
        return self.entity_description.value_fn(self.manager)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        return self.entity_description.attr_fn(self.manager)
