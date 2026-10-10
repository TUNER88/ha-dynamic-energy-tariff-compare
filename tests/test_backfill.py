"""Backfill test using the real recorder (in-memory SQLite)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)

from homeassistant.components.recorder.models import StatisticData, StatisticMeanType, StatisticMetaData
from homeassistant.components.recorder.statistics import async_import_statistics
from homeassistant.core import HomeAssistant

from custom_components.dynamic_energy_tariff_compare.const import DEFAULTS, DOMAIN

ENERGY = "sensor.edl21_total_active_energy"
ENERGY_ATTRS = {"device_class": "energy", "unit_of_measurement": "kWh", "state_class": "total_increasing"}


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(recorder_mock, enable_custom_integrations):
    """Recorder must be set up before hass; override the conftest fixture."""
    return


START = datetime(2026, 10, 9, 0, 0, tzinfo=timezone.utc)
NOW = datetime(2026, 10, 9, 4, 0, tzinfo=timezone.utc)


PRICE = "sensor.tibber_pricing_de_current_price"


def _import_energy(hass: HomeAssistant, unit: str, per_hour: float) -> None:
    meta = StatisticMetaData(
        has_sum=True, mean_type=StatisticMeanType.NONE, name=None, source="recorder",
        statistic_id=ENERGY, unit_of_measurement=unit, unit_class="energy",
    )
    stats = [
        StatisticData(start=START + timedelta(hours=h), state=100 + (h + 1) * per_hour, sum=(h + 1) * per_hour)
        for h in range(4)
    ]
    # a row before the window so "change" for the first hour is defined
    stats.insert(0, StatisticData(start=START - timedelta(hours=1), state=100, sum=0.0))
    async_import_statistics(hass, meta, stats)


def _import_price_means(hass: HomeAssistant, unit: str, values: list[float]) -> None:
    meta = StatisticMetaData(
        has_sum=False, mean_type=StatisticMeanType.ARITHMETIC, name=None, source="recorder",
        statistic_id=PRICE, unit_of_measurement=unit, unit_class=None,
    )
    stats = [
        StatisticData(start=START + timedelta(hours=h), mean=v, min=v, max=v)
        for h, v in enumerate(values)
    ]
    async_import_statistics(hass, meta, stats)


async def _setup(hass: HomeAssistant) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=2,
        data={**DEFAULTS, "energy_entity": ENERGY, "price_entity": PRICE, "fixed_price_ct": 30.0},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def _backfill(hass: HomeAssistant, entry: MockConfigEntry) -> dict:
    result = await hass.services.async_call(
        DOMAIN, "backfill", {"days": 1}, blocking=True, return_response=True
    )
    return result[entry.entry_id]


@pytest.mark.parametrize(("energy_unit", "per_hour"), [("kWh", 1.0), ("Wh", 1000.0)])
async def test_backfill_prices_from_sensor_statistics(
    recorder_mock, hass: HomeAssistant, freezer, energy_unit, per_hour
) -> None:
    """State history purged: hourly price means of the selected sensor are used.

    EDL21 meters often report Wh; statistics are converted to kWh.
    """
    freezer.move_to(NOW)
    _import_energy(hass, energy_unit, per_hour)
    _import_price_means(hass, "EUR/kWh", [0.20, 0.30, 0.40, 0.50])
    await async_wait_recording_done(hass)

    hass.states.async_set(ENERGY, str(100 + 4 * per_hour), {**ENERGY_ATTRS, "unit_of_measurement": energy_unit})
    hass.states.async_set(PRICE, "0.5", {"unit_of_measurement": "EUR/kWh", "state_class": "measurement"})
    entry = await _setup(hass)
    mgr = entry.runtime_data

    res = await _backfill(hass, entry)
    assert res["status"] == "ok", res
    assert res["hourly_buckets"] >= 4
    assert res["price_slots_from_statistics"] >= 16
    assert mgr.acc.totals.energy_kwh == pytest.approx(4.0)
    assert mgr.acc.totals.dynamic_energy_cost_eur == pytest.approx(0.20 + 0.30 + 0.40 + 0.50)
    assert mgr.acc.totals.fixed_energy_cost_eur == pytest.approx(1.20)
    assert mgr.acc.totals.tracking_start <= START.timestamp()


async def test_backfill_reports_missing_price_history(recorder_mock, hass: HomeAssistant, freezer) -> None:
    freezer.move_to(NOW)
    _import_energy(hass, "kWh", 1.0)
    await async_wait_recording_done(hass)
    hass.states.async_set(ENERGY, "104", ENERGY_ATTRS)
    entry = await _setup(hass)  # price sensor has neither states nor statistics
    mgr = entry.runtime_data
    res = await _backfill(hass, entry)
    assert res["status"] == "no_price_history", res
    assert PRICE in res["message"]
    assert mgr.acc.totals.energy_kwh == 0
    assert mgr.acc.totals.dynamic_energy_cost_eur == 0


# ------------------------------------------------------------ Tibber Germany
TIBBER_NOW = datetime(2026, 10, 10, 11, 5, tzinfo=timezone.utc)


async def _setup_tibber(hass: HomeAssistant, aioclient_mock, payload) -> MockConfigEntry:
    import json  # noqa: PLC0415
    from pathlib import Path  # noqa: PLC0415

    from custom_components.dynamic_energy_tariff_compare.const import TIBBER_DE_URL  # noqa: PLC0415

    if payload is None:
        payload = json.loads((Path(__file__).parent / "fixtures" / "tibber_price_overview_07646.json").read_text())
    if payload == 404:
        aioclient_mock.get(TIBBER_DE_URL, status=404)
    else:
        aioclient_mock.get(TIBBER_DE_URL, json=payload)
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=3,
        data={
            "price_source": "tibber_de", "provider_name": "Tibber", "postcode": "07646",
            "energy_entity": ENERGY, "fixed_price_ct": 30.0,
            "fixed_base_fee_eur_month": 12.0, "provider_monthly_fee_eur": 15.64,
        },
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def test_backfill_tibber_from_archive(recorder_mock, hass: HomeAssistant, freezer, aioclient_mock) -> None:
    """Yesterday's quarter-hour prices (and older archived ones) price the meter statistics."""
    import json  # noqa: PLC0415
    from pathlib import Path  # noqa: PLC0415

    await hass.config.async_set_time_zone("Europe/Berlin")
    freezer.move_to(TIBBER_NOW)
    _import_energy(hass, "Wh", 1000.0)  # 2026-10-09 00:00-04:00 UTC = 02:00-06:00 local
    await async_wait_recording_done(hass)
    hass.states.async_set(ENERGY, "4100", {**ENERGY_ATTRS, "unit_of_measurement": "Wh"})
    entry = await _setup_tibber(hass, aioclient_mock, None)
    mgr = entry.runtime_data

    result = await hass.services.async_call(DOMAIN, "backfill", {"days": 2}, blocking=True, return_response=True)
    res = result[entry.entry_id]
    assert res["status"] == "ok", res
    assert res["price_slots_from_tibber_archive"] >= 16
    assert res["tibber_archive_from"].startswith("2026-10-09T00:00:00+02:00")

    payload = json.loads((Path(__file__).parent / "fixtures" / "tibber_price_overview_07646.json").read_text())
    yday = [e["priceIncludingVat"] for e in payload["energy"]["yesterdayQuarterHours"]]
    expected = sum(yday[8:24]) / 4  # local 02:00-06:00, 1 kWh per hour
    assert mgr.acc.totals.energy_kwh == pytest.approx(4.0)
    assert mgr.acc.totals.dynamic_energy_cost_eur == pytest.approx(expected)
    assert mgr.acc.totals.fixed_energy_cost_eur == pytest.approx(1.20)


async def test_backfill_tibber_without_archive(recorder_mock, hass: HomeAssistant, freezer, aioclient_mock) -> None:
    freezer.move_to(TIBBER_NOW)
    _import_energy(hass, "kWh", 1.0)
    await async_wait_recording_done(hass)
    hass.states.async_set(ENERGY, "104", ENERGY_ATTRS)
    entry = await _setup_tibber(hass, aioclient_mock, 404)
    result = await hass.services.async_call(DOMAIN, "backfill", {"days": 2}, blocking=True, return_response=True)
    res = result[entry.entry_id]
    assert res["status"] == "no_price_history", res
    assert "only provides yesterday, today and tomorrow" in res["message"]
    assert entry.runtime_data.acc.totals.dynamic_energy_cost_eur == 0
