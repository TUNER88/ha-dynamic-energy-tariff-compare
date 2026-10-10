"""Rolling windows (last 24 h / 30 days / 365 days)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed

from homeassistant.core import HomeAssistant

from custom_components.dynamic_energy_tariff_compare import calc
from custom_components.dynamic_energy_tariff_compare.const import DOMAIN

DAY = 86400
NOW_TS = int(datetime(2026, 10, 10, 12, 0, tzinfo=timezone.utc).timestamp())
PARAMS = calc.TariffParams(fixed_price_ct=30.0, fixed_base_fee_eur_month=12.0, provider_monthly_fee_eur=6.0)


def _acc(tracking_start: float) -> calc.Accumulator:
    acc = calc.Accumulator(PARAMS, calc.Totals(tracking_start=tracking_start))
    return acc


# ----------------------------------------------------------------------- math
def test_window_math_and_fee_proration() -> None:
    acc = _acc(NOW_TS - 10 * DAY)  # tracked for 10 days
    # 1 kWh at 20 ct two hours ago, 2 kWh at 10 ct 5 days ago, 3 kWh unpriced 3 h ago
    acc.add_energy([(NOW_TS - 2 * 3600, 1.0)], lambda s: 20.0)
    acc.add_energy([(NOW_TS - 5 * DAY, 2.0)], lambda s: 10.0)
    acc.add_energy([(NOW_TS - 3 * 3600, 3.0)], lambda s: None)

    w = acc.window_stats(calc.WINDOWS["24h"], NOW_TS)
    assert w.energy_kwh == pytest.approx(4.0)
    assert w.priced_kwh == pytest.approx(1.0)  # unpriced kWh excluded from the comparison
    assert w.dynamic_energy_cost_eur == pytest.approx(0.20)
    assert w.fixed_energy_cost_eur == pytest.approx(0.30)
    assert w.covered_from == NOW_TS - DAY
    months = DAY / calc.SECONDS_PER_MONTH
    assert w.dynamic_fees_eur == pytest.approx(6.0 * months)
    assert w.fixed_fees_eur == pytest.approx(12.0 * months)
    assert w.avg_dynamic_price_ct == pytest.approx(20.0)
    assert w.avg_fixed_price_ct == pytest.approx(30.0)
    assert w.avg_dynamic_price_incl_fees_ct == pytest.approx((0.20 + 6.0 * months) * 100)
    assert w.avg_fixed_price_incl_fees_ct == pytest.approx((0.30 + 12.0 * months) * 100)
    assert w.savings_eur == pytest.approx((0.30 + 12.0 * months) - (0.20 + 6.0 * months))

    w30 = acc.window_stats(calc.WINDOWS["30d"], NOW_TS)
    assert w30.priced_kwh == pytest.approx(3.0)
    assert w30.dynamic_energy_cost_eur == pytest.approx(0.40)
    assert w30.avg_dynamic_price_ct == pytest.approx(0.40 / 3 * 100)
    # fees only for the tracked 10 days, not the whole 30-day window
    assert w30.covered_from == NOW_TS - 10 * DAY
    assert w30.dynamic_fees_eur == pytest.approx(6.0 * 10 * DAY / calc.SECONDS_PER_MONTH)

    # pending energy priced later is attributed to its original slot
    acc.resolve_pending(lambda s: 40.0, NOW_TS)
    w = acc.window_stats(calc.WINDOWS["24h"], NOW_TS)
    assert w.priced_kwh == pytest.approx(4.0)
    assert w.dynamic_energy_cost_eur == pytest.approx(0.20 + 1.20)


def test_empty_window() -> None:
    acc = calc.Accumulator(PARAMS)
    w = acc.window_stats(DAY, NOW_TS)
    assert w.priced_kwh == 0
    assert w.avg_dynamic_price_ct is None
    assert w.savings_eur == 0
    assert w.covered_from is None


def test_window_boundary_is_proportional() -> None:
    hist = calc.SlotHistory()
    hist.rows[calc.DAY][NOW_TS - NOW_TS % DAY - 2 * DAY] = [24.0, 24.0, 2.4, 7.2]
    start = NOW_TS - NOW_TS % DAY - 2 * DAY + 6 * 3600  # 18 of 24 h inside
    assert hist.window(start, NOW_TS) == pytest.approx([18.0, 18.0, 1.8, 5.4])


# ------------------------------------------------------- compaction / pruning
def test_compaction_and_pruning_keep_window_sums() -> None:
    acc = _acc(NOW_TS - 400 * DAY)
    lookup = lambda s: 25.0  # noqa: E731
    for age_h in range(0, 400 * 24, 5):  # a reading every 5 hours for 400 days
        acc.add_energy([(NOW_TS - age_h * 3600 - 600, 0.5)], lookup)
    hist = acc.history
    before = {k: acc.window_stats(v, NOW_TS) for k, v in calc.WINDOWS.items()}
    n_before = len(hist)
    hist.compact(NOW_TS)
    assert len(hist) < n_before
    # recent rows stay at 15-min resolution, then hourly, then daily
    assert all(k >= NOW_TS - 3 * DAY for k in hist.rows[calc.SLOT_SECONDS])
    assert all(NOW_TS - 36 * DAY <= k < NOW_TS - DAY for k in hist.rows[calc.HOUR])
    assert all(k % DAY == 0 for k in hist.rows[calc.DAY])
    # nothing older than 366 days survives
    assert min(min(r) for r in hist.rows.values() if r) >= NOW_TS - calc.HISTORY_KEEP_SECONDS - DAY
    after = {k: acc.window_stats(v, NOW_TS) for k, v in calc.WINDOWS.items()}
    # 24 h / 30 d boundaries fall in 15-min / hourly rows: exact
    for key in ("24h", "30d"):
        assert after[key].priced_kwh == pytest.approx(before[key].priced_kwh)
        assert after[key].dynamic_energy_cost_eur == pytest.approx(before[key].dynamic_energy_cost_eur)
    # 365 d boundary falls into a daily row: off by at most one day's consumption
    assert after["365d"].priced_kwh == pytest.approx(before["365d"].priced_kwh, abs=2.5)
    assert after["365d"].avg_dynamic_price_ct == pytest.approx(25.0)
    # compaction is idempotent
    snapshot = hist.to_dict()
    hist.compact(NOW_TS)
    assert hist.to_dict() == snapshot
    # small store: ~2 days of 15-min rows + ~33 days of hours + ~331 days
    assert len(hist) < 2 * 96 + 34 * 24 + 335


def test_history_serialization_roundtrip_and_garbage() -> None:
    acc = _acc(NOW_TS - DAY)
    acc.add_energy([(NOW_TS - 3600, 1.25)], lambda s: 12.5)
    acc.history.rows[calc.DAY][NOW_TS - NOW_TS % DAY - 100 * DAY] = [1.0, 1.0, 0.1, 0.3]
    data = acc.history.to_dict()
    again = calc.SlotHistory.from_dict(data)
    assert again.to_dict() == data
    bad = calc.SlotHistory.from_dict(
        {"900": [[1, 2], ["x", 1, 1, 1, 1], [901, 1, 1, 1, 1], [900, 1, 1, 1, 1]], "3600": "nope"}
    )
    assert len(bad) == 1
    assert len(calc.SlotHistory.from_dict(None)) == 0


# --------------------------------------------------------------- integration
ENERGY = "sensor.edl21_total_active_energy"
PRICE = "sensor.epex_spot_data_total_price"
ENERGY_ATTRS = {"device_class": "energy", "unit_of_measurement": "kWh", "state_class": "total_increasing"}
NOW = datetime(2026, 10, 10, 10, 5, tzinfo=timezone.utc)
SLOT0 = datetime(2026, 10, 10, 10, 0, tzinfo=timezone.utc)
AVG = "sensor.dynamic_energy_tariff_compare_avg_dynamic_price_{}"
SAV = "sensor.dynamic_energy_tariff_compare_savings_{}"


def _price_attrs() -> dict:
    data = [
        {
            "start_time": (SLOT0 + timedelta(minutes=15 * i)).isoformat(),
            "end_time": (SLOT0 + timedelta(minutes=15 * (i + 1))).isoformat(),
            "price_per_kwh": 0.20,
        }
        for i in range(8)
    ]
    return {"unit_of_measurement": "EUR/kWh", "data": data}


async def test_window_sensors_live_restart_tick_and_reset(hass: HomeAssistant, freezer) -> None:
    await hass.config.async_set_time_zone("Europe/Berlin")
    freezer.move_to(NOW)
    hass.states.async_set(PRICE, "0.2", _price_attrs())
    hass.states.async_set(ENERGY, "1000", ENERGY_ATTRS)
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=3,
        data={
            "price_source": "sensor", "provider_name": "", "energy_entity": ENERGY, "price_entity": PRICE,
            "price_unit": "auto", "fixed_price_ct": 30.0, "fixed_base_fee_eur_month": 12.0,
            "provider_monthly_fee_eur": 6.0,
        },
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    freezer.move_to(NOW + timedelta(minutes=10))
    hass.states.async_set(ENERGY, "1002", ENERGY_ATTRS)
    await hass.async_block_till_done()
    tick = datetime(2026, 10, 10, 10, 15, 2, tzinfo=timezone.utc)
    freezer.move_to(tick)
    async_fire_time_changed(hass, tick)
    await hass.async_block_till_done()

    for key in ("24h", "30d", "365d"):
        avg = hass.states.get(AVG.format(key))
        assert float(avg.state) == pytest.approx(20.0)
        assert avg.attributes["unit_of_measurement"] == "ct/kWh"
        assert avg.attributes["consumption_kwh"] == pytest.approx(2.0)
        assert avg.attributes["dynamic_energy_cost_eur"] == pytest.approx(0.40)
        assert avg.attributes["fixed_energy_cost_eur"] == pytest.approx(0.60)
        assert avg.attributes["window"] == key
        sav = hass.states.get(SAV.format(key))
        assert sav.attributes["unit_of_measurement"] == "EUR"
        # fees prorated over the 10 min 2 s tracked so far (same for every window)
        assert float(sav.state) == pytest.approx(0.20 + 6.0 * 602 / calc.SECONDS_PER_MONTH, abs=1e-4)
    start_24h = hass.states.get(AVG.format("24h")).attributes["window_start"]
    assert start_24h == "2026-10-09T12:15:02+02:00"  # local time, labelled with the window start

    # restart: rolling-window data is persisted in the Store
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert hass.states.get(AVG.format("30d")).state == "20.0"

    # time passes without any meter update: the 24 h window moves on (15-min tick)
    later = datetime(2026, 10, 11, 10, 30, 2, tzinfo=timezone.utc)
    freezer.move_to(later)
    async_fire_time_changed(hass, later)
    await hass.async_block_till_done()
    assert hass.states.get(AVG.format("24h")).state == "unknown"
    assert hass.states.get(AVG.format("24h")).attributes["consumption_kwh"] == 0
    assert float(hass.states.get(AVG.format("30d")).state) == pytest.approx(20.0)

    # reset clears the windows
    await hass.services.async_call(DOMAIN, "reset", {}, blocking=True)
    await hass.async_block_till_done()
    assert len(entry.runtime_data.acc.history) == 0
    assert hass.states.get(AVG.format("365d")).state == "unknown"
    assert float(hass.states.get(SAV.format("365d")).state) == 0


async def test_dashboard_window_table_renders(hass: HomeAssistant) -> None:
    """The markdown card in examples/dashboard.yaml renders with HA's template engine."""
    from pathlib import Path  # noqa: PLC0415

    import yaml  # noqa: PLC0415

    from homeassistant.helpers.template import Template  # noqa: PLC0415

    dash = yaml.safe_load((Path(__file__).parents[1] / "examples" / "dashboard.yaml").read_text())
    card = next(c for c in dash["views"][0]["cards"] if c["type"] == "markdown")
    attrs = {
        "consumption_kwh": 2.0, "avg_dynamic_price_ct": 20.0, "avg_dynamic_price_incl_fees_ct": None,
        "avg_fixed_price_incl_fees_ct": 31.2, "dynamic_total_cost_eur": 0.4,
        "fixed_total_cost_eur": 0.6, "savings_eur": 0.0,
    }
    for key in ("24h", "30d", "365d"):
        hass.states.async_set(AVG.format(key), "20.0", attrs)
    out = Template(card["content"], hass).async_render(parse_result=False)
    assert "| Consumption (kWh) | 2.0 | 2.0 | 2.0 |" in out
    assert "| Ø dynamic incl. fees (ct/kWh) | – | – | – |" in out
    assert "| **Savings (€)** | **0.0** | **0.0** | **0.0** |" in out
