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


async def test_backfill_hourly_with_energy_charts(
    recorder_mock, hass: HomeAssistant, freezer, aioclient_mock
) -> None:
    freezer.move_to(NOW)
    base = int(START.timestamp())
    # 15-min spot prices: 0..15 * 10 EUR/MWh -> 0..15 ct/kWh net
    aioclient_mock.get(
        "https://api.energy-charts.info/price",
        json={"unix_seconds": [base + 900 * i for i in range(16)],
              "price": [10.0 * i for i in range(16)], "unit": "EUR / MWh"},
    )
    meta = StatisticMetaData(
        has_sum=True, mean_type=StatisticMeanType.NONE, name=None, source="recorder",
        statistic_id=ENERGY, unit_of_measurement="kWh", unit_class="energy",
    )
    stats = [
        StatisticData(start=START + timedelta(hours=h), state=100 + h + 1, sum=float(h + 1))
        for h in range(4)
    ]
    # a row before the window so "change" for the first hour is defined
    stats.insert(0, StatisticData(start=START - timedelta(hours=1), state=100, sum=0.0))
    async_import_statistics(hass, meta, stats)
    await async_wait_recording_done(hass)

    hass.states.async_set(ENERGY, "104", ENERGY_ATTRS)
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={**DEFAULTS, "energy_entity": ENERGY, "price_entity": None, "fixed_price_ct": 30.0,
              "provider_markup_ct": 0.0, "network_fee_ct": 0.0, "levies_ct": 0.0, "vat_percent": 0.0},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    mgr = entry.runtime_data

    result = await hass.services.async_call(
        DOMAIN, "backfill", {"days": 1}, blocking=True, return_response=True
    )
    res = result[entry.entry_id]
    assert res["status"] == "ok", res
    assert res["hourly_buckets"] >= 4
    # 1 kWh per hour, spread 0.25 kWh per quarter -> 0.25 * sum(0..15) ct
    assert mgr.acc.totals.energy_kwh == pytest.approx(4.0)
    assert mgr.acc.totals.dynamic_energy_cost_eur == pytest.approx(0.25 * sum(range(16)) / 100)
    assert mgr.acc.totals.fixed_energy_cost_eur == pytest.approx(1.20)
    assert mgr.acc.totals.tracking_start <= START.timestamp()
