"""Integration tests running inside a real Home Assistant test instance."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed

from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from custom_components.tibber_compare.const import (
    CONF_ENERGY_ENTITY,
    CONF_PRICE_ENTITY,
    CONF_PRICE_IS_FINAL,
    DEFAULTS,
    DOMAIN,
)

NOW = datetime(2026, 10, 10, 10, 5, tzinfo=timezone.utc)
SLOT0 = datetime(2026, 10, 10, 10, 0, tzinfo=timezone.utc)
ENERGY = "sensor.edl21_total_active_energy"
PRICE = "sensor.epex_spot_data_net_price"
ENERGY_ATTRS = {"device_class": "energy", "unit_of_measurement": "kWh", "state_class": "total_increasing"}


def _epex_attrs() -> dict:
    data = []
    for i in range(8):
        start = SLOT0 + timedelta(minutes=15 * i)
        data.append(
            {
                "start_time": start.isoformat(),
                "end_time": (start + timedelta(minutes=15)).isoformat(),
                "price_per_kwh": 0.10 + 0.01 * i,  # 10, 11, 12 ... ct/kWh
            }
        )
    return {"unit_of_measurement": "EUR/kWh", "data": data}


def _options(**kw):
    return {
        **DEFAULTS,
        CONF_ENERGY_ENTITY: ENERGY,
        CONF_PRICE_ENTITY: PRICE,
        CONF_PRICE_IS_FINAL: False,
        "fixed_price_ct": 30.0,
        "tibber_markup_ct": 2.0,
        "network_fee_ct": 9.0,
        "levies_ct": 7.0,
        "vat_percent": 19.0,
        **kw,
    }


async def test_config_flow(hass: HomeAssistant) -> None:
    hass.states.async_set(ENERGY, "1000", ENERGY_ATTRS)
    hass.states.async_set(PRICE, "0.1", _epex_attrs())
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.flow.async_configure(result["flow_id"], _options())
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_ENERGY_ENTITY] == ENERGY


async def test_config_flow_bad_price_unit(hass: HomeAssistant) -> None:
    hass.states.async_set(ENERGY, "1000", ENERGY_ATTRS)
    hass.states.async_set("sensor.weird", "5", {"unit_of_measurement": "SEK/kWh"})
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], _options(price_entity="sensor.weird")
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {CONF_PRICE_ENTITY: "unknown_price_unit"}


async def _setup(hass: HomeAssistant, **kw) -> MockConfigEntry:
    entry = MockConfigEntry(domain=DOMAIN, data=_options(**kw), title="Tibber Compare")
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def test_tracking_with_epex_sensor(hass: HomeAssistant, freezer) -> None:
    freezer.move_to(NOW)
    hass.states.async_set(PRICE, "0.1", _epex_attrs())
    hass.states.async_set(ENERGY, "1000", ENERGY_ATTRS)
    entry = await _setup(hass)
    mgr = entry.runtime_data

    # 10:05 -> 10:20: 2 kWh; 2/3 in slot 10:00 (10 ct), 1/3 in slot 10:15 (11 ct)
    freezer.move_to(NOW + timedelta(minutes=15))
    hass.states.async_set(ENERGY, "1002", ENERGY_ATTRS)
    await hass.async_block_till_done()
    t = mgr.acc.totals
    assert t.energy_kwh == pytest.approx(2.0)
    eff0 = 10 * 1.19 + 18
    eff1 = 11 * 1.19 + 18
    expected = (4 / 3 * eff0 + 2 / 3 * eff1) / 100
    assert t.tibber_energy_cost_eur == pytest.approx(expected)
    assert t.fixed_energy_cost_eur == pytest.approx(0.60)

    # debounce -> entities update
    async_fire_time_changed(hass, NOW + timedelta(minutes=16))
    await hass.async_block_till_done()
    state = hass.states.get("sensor.tibber_compare_tibber_energy_cost")
    assert state is not None
    assert float(state.state) == pytest.approx(expected, abs=1e-4)
    assert state.attributes["state_class"] == "total"
    assert state.attributes["device_class"] == "monetary"
    cons = hass.states.get("sensor.tibber_compare_tracked_consumption")
    assert float(cons.state) == pytest.approx(2.0)
    cur = hass.states.get("sensor.tibber_compare_current_tibber_price_effective")
    assert float(cur.state) == pytest.approx(eff1, abs=1e-3)
    src = hass.states.get("sensor.tibber_compare_current_price_source")
    assert float(src.state) == pytest.approx(11.0)
    assert len(src.attributes["today"]) == 8

    # unavailable then negative jump (meter swap) is ignored
    hass.states.async_set(ENERGY, "unavailable", ENERGY_ATTRS)
    freezer.move_to(NOW + timedelta(minutes=20))
    hass.states.async_set(ENERGY, "5", ENERGY_ATTRS)
    await hass.async_block_till_done()
    assert mgr.acc.totals.energy_kwh == pytest.approx(2.0)

    # persistence across reload
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.runtime_data.acc.totals.energy_kwh == pytest.approx(2.0)
    assert entry.runtime_data.acc.totals.last_value == pytest.approx(5.0)


async def test_final_price_state_only_sensor(hass: HomeAssistant, freezer) -> None:
    """Tibber core sensor: state only, EUR/kWh, already all-in."""
    freezer.move_to(NOW)
    hass.states.async_set("sensor.tibber_price", "0.30", {"unit_of_measurement": "EUR/kWh"})
    hass.states.async_set(ENERGY, "1000", ENERGY_ATTRS)
    entry = await _setup(hass, price_entity="sensor.tibber_price", price_is_final=True)
    freezer.move_to(NOW + timedelta(minutes=5))
    hass.states.async_set(ENERGY, "1001", ENERGY_ATTRS)
    await hass.async_block_till_done()
    assert entry.runtime_data.acc.totals.tibber_energy_cost_eur == pytest.approx(0.30)


async def test_energy_charts_fallback(hass: HomeAssistant, freezer, aioclient_mock) -> None:
    freezer.move_to(NOW)
    ts = int(SLOT0.timestamp())
    aioclient_mock.get(
        "https://api.energy-charts.info/price",
        json={"unix_seconds": [ts, ts + 900], "price": [100.0, 200.0], "unit": "EUR / MWh"},
    )
    hass.states.async_set(ENERGY, "1000", ENERGY_ATTRS)
    entry = await _setup(hass, price_entity=None)
    await hass.async_block_till_done()
    mgr = entry.runtime_data
    assert mgr.price_lookup(ts) == (pytest.approx(10.0), False)
    freezer.move_to(NOW + timedelta(minutes=5))
    hass.states.async_set(ENERGY, "1001", ENERGY_ATTRS)
    await hass.async_block_till_done()
    assert mgr.acc.totals.tibber_energy_cost_eur == pytest.approx((10 * 1.19 + 18) / 100)


async def test_options_flow_changes_sensor(hass: HomeAssistant) -> None:
    hass.states.async_set(PRICE, "0.1", _epex_attrs())
    hass.states.async_set(ENERGY, "1000", ENERGY_ATTRS)
    hass.states.async_set("sensor.other_energy", "50", ENERGY_ATTRS)
    entry = await _setup(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], _options(energy_entity="sensor.other_energy", fixed_price_ct=35.5)
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    mgr = entry.runtime_data
    assert mgr.energy_entity == "sensor.other_energy"
    assert mgr.params.fixed_price_ct == 35.5
