"""Tests for the pure calculation module (no Home Assistant needed)."""

from __future__ import annotations

from datetime import datetime, timezone
import importlib.util
from pathlib import Path
import sys

import pytest

_spec = importlib.util.spec_from_file_location(
    "tc_calc",
    Path(__file__).parents[1] / "custom_components" / "tibber_compare" / "calc.py",
)
calc = importlib.util.module_from_spec(_spec)
sys.modules["tc_calc"] = calc
_spec.loader.exec_module(calc)

T0 = int(datetime(2026, 10, 10, 10, 0, tzinfo=timezone.utc).timestamp())


def test_slot_start():
    assert calc.slot_start(T0) == T0
    assert calc.slot_start(T0 + 899) == T0
    assert calc.slot_start(T0 + 900) == T0 + 900


def test_split_within_slot():
    assert calc.split_interval(T0 + 60, T0 + 120, 1.0) == [(T0, 1.0)]


def test_split_across_slots_proportional():
    # 10:10 -> 10:40 = 5 min in slot 0, 15 in slot 1, 10 in slot 2
    parts = calc.split_interval(T0 + 600, T0 + 2400, 3.0)
    assert [p[0] for p in parts] == [T0, T0 + 900, T0 + 1800]
    assert [round(p[1], 6) for p in parts] == [0.5, 1.5, 1.0]
    assert sum(p[1] for p in parts) == pytest.approx(3.0)


def test_split_zero_duration():
    assert calc.split_interval(T0 + 5, T0 + 5, 2.0) == [(T0, 2.0)]


@pytest.mark.parametrize(
    ("unit", "factor"),
    [
        ("EUR/kWh", 100), ("€/kWh", 100), ("ct/kWh", 1), ("Cent/kWh", 1),
        ("EUR/MWh", 0.1), ("€/MWh", 0.1), ("EUR / MWh", 0.1), ("SEK/kWh", None), (None, None),
    ],
)
def test_price_units(unit, factor):
    got = calc.price_factor_to_ct_kwh(unit)
    if factor is None:
        assert got is None
    else:
        assert got == pytest.approx(factor)


def test_energy_units():
    assert calc.energy_factor_to_kwh("Wh") == 0.001
    assert calc.energy_factor_to_kwh("kWh") == 1
    assert calc.energy_factor_to_kwh(None) == 1


def test_effective_price_raw_and_final():
    p = calc.TariffParams(tibber_markup_ct=2.0, network_fee_ct=9.0, levies_ct=7.0, vat_percent=19)
    assert calc.effective_price_ct(10.0, p) == pytest.approx(11.9 + 18.0)
    assert calc.effective_price_ct(-5.0, p) == pytest.approx(-5.95 + 18.0)
    assert calc.effective_price_ct(30.0, p, is_final=True) == 30.0


def test_parse_epex_spot_attributes():
    attrs = {
        "data": [
            {"start_time": "2026-10-10T10:00:00+00:00", "end_time": "2026-10-10T10:15:00+00:00", "price_per_kwh": 0.1},
            {"start_time": "2026-10-10T10:15:00+00:00", "end_time": "2026-10-10T10:30:00+00:00", "price_ct_per_kwh": 12.5},
            {"start_time": "2026-10-10T10:30:00+00:00", "end_time": "2026-10-10T10:45:00+00:00", "price_eur_per_mwh": 80.0},
        ]
    }
    m = calc.parse_price_attributes(attrs, "EUR/kWh")
    assert m == {T0: pytest.approx(10.0), T0 + 900: pytest.approx(12.5), T0 + 1800: pytest.approx(8.0)}


def test_parse_nordpool_custom_hourly_expands():
    attrs = {
        "unit": "kWh", "price_in_cents": False,
        "raw_today": [{"start": "2026-10-10T12:00:00+02:00", "end": "2026-10-10T13:00:00+02:00", "value": 0.2}],
    }
    m = calc.parse_price_attributes(attrs, "EUR/kWh")
    assert sorted(m) == [T0, T0 + 900, T0 + 1800, T0 + 2700]
    assert all(v == pytest.approx(20.0) for v in m.values())


def test_parse_tibber_like_and_energy_charts():
    m = calc.parse_price_attributes({"today": [{"startsAt": "2026-10-10T10:00:00Z", "total": 0.3}]}, "EUR/kWh")
    assert m[T0] == pytest.approx(30.0)
    m2 = calc.parse_price_attributes({"unix_seconds": [T0, T0 + 900], "price": [100.0, None], "unit": "EUR / MWh"}, None)
    assert m2 == {T0: pytest.approx(10.0)}


def test_parse_energy_charts_payload_15min_and_hourly():
    m = calc.parse_energy_charts({"unix_seconds": [T0, T0 + 900], "price": [50.0, 60.0], "unit": "EUR / MWh"})
    assert m == {T0: pytest.approx(5.0), T0 + 900: pytest.approx(6.0)}
    h = calc.parse_energy_charts({"unix_seconds": [T0, T0 + 3600], "price": [50.0, 60.0], "unit": "EUR / MWh"})
    assert len(h) == 8 and h[T0 + 2700] == pytest.approx(5.0) and h[T0 + 3600 + 2700] == pytest.approx(6.0)


def _lookup(prices):
    return lambda slot: (prices[slot], False) if slot in prices else None


def test_accumulator_basic_and_split():
    p = calc.TariffParams(fixed_price_ct=30, tibber_markup_ct=0, network_fee_ct=0, levies_ct=0, vat_percent=0)
    acc = calc.Accumulator(p)
    prices = {T0: 10.0, T0 + 900: 20.0}
    look = _lookup(prices)
    assert acc.process_reading(1000.0, T0 + 450, look) == 0  # baseline
    acc.process_reading(1002.0, T0 + 1350, look)  # 1 kWh in each slot
    t = acc.totals
    assert t.energy_kwh == pytest.approx(2.0)
    assert t.tibber_energy_cost_eur == pytest.approx(0.30)
    assert t.fixed_energy_cost_eur == pytest.approx(0.60)
    assert acc.avg_tibber_price_ct() == pytest.approx(15.0)


def test_accumulator_reset_and_negative_delta_ignored():
    acc = calc.Accumulator(calc.TariffParams())
    look = _lookup({T0: 10.0})
    acc.process_reading(500.0, T0, look)
    acc.process_reading(0.5, T0 + 60, look)  # meter replaced / reset
    assert acc.totals.energy_kwh == 0
    acc.process_reading(1.5, T0 + 120, look)
    assert acc.totals.energy_kwh == pytest.approx(1.0)


def test_pending_resolution_and_savings_exclude_pending():
    p = calc.TariffParams(fixed_price_ct=30, fixed_base_fee_eur_month=0, tibber_monthly_fee_eur=0,
                          grid_base_fee_eur_month=0, tibber_markup_ct=0, network_fee_ct=0, levies_ct=0, vat_percent=0)
    acc = calc.Accumulator(p)
    prices: dict[int, float] = {}
    look = _lookup(prices)
    acc.process_reading(0, T0, look)
    acc.process_reading(1, T0 + 600, look)
    assert acc.pending_kwh == pytest.approx(1)
    assert acc.savings(T0 + 600) == pytest.approx(0)
    prices[T0] = 10.0
    assert acc.resolve_pending(look, T0 + 700) == 1
    assert acc.pending_kwh == 0
    assert acc.savings(T0 + 700) == pytest.approx(0.20)


def test_monthly_fees_prorated():
    p = calc.TariffParams(fixed_base_fee_eur_month=12, tibber_monthly_fee_eur=6, grid_base_fee_eur_month=8)
    acc = calc.Accumulator(p, calc.Totals(tracking_start=T0))
    now = T0 + calc.SECONDS_PER_MONTH
    assert acc.fixed_total_cost(now) == pytest.approx(12)
    assert acc.tibber_total_cost(now) == pytest.approx(14)
    assert acc.savings(now) == pytest.approx(-2)


def test_totals_roundtrip():
    t = calc.Totals(energy_kwh=1.5, pending=[[T0, 0.2]], tracking_start=T0)
    assert calc.Totals.from_dict(t.to_dict()) == t
    assert calc.Totals.from_dict({"energy_kwh": 1, "unknown": 5}).energy_kwh == 1


def test_state_timeline_time_weighted():
    tl = [(T0 - 100, 10.0), (T0 + 450, 20.0)]
    m = calc.price_map_from_state_timeline(tl, T0, T0 + 1800)
    assert m[T0] == pytest.approx(15.0)
    assert m[T0 + 900] == pytest.approx(20.0)
    # nothing known before the first point
    assert calc.price_map_from_state_timeline([(T0 + 1000, 5.0)], T0, T0 + 1800) == {
        T0 + 900: pytest.approx(5.0)
    }


def test_backfill_hourly_with_15min_prices_and_merge():
    p = calc.TariffParams(fixed_price_ct=30, tibber_markup_ct=0, network_fee_ct=0, levies_ct=0, vat_percent=0)
    prices = {T0: 0.0, T0 + 900: 10.0, T0 + 1800: 20.0}  # last quarter missing
    back = calc.backfill_energy([(T0, T0 + 3600, 4.0)], _lookup(prices), p)
    assert back.priced_energy_kwh == pytest.approx(3.0)
    assert back.energy_kwh == pytest.approx(3.0)  # unpriced excluded from both sides
    assert back.tibber_energy_cost_eur == pytest.approx(0.30)
    assert back.fixed_energy_cost_eur == pytest.approx(0.90)
    live = calc.Totals(energy_kwh=1, tracking_start=T0 + 3600)
    calc.merge_backfill(live, back, T0)
    assert live.tracking_start == T0 and live.energy_kwh == pytest.approx(4.0)
    assert live.backfilled_kwh == pytest.approx(3.0)
