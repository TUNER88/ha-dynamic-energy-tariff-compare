"""Integration tests running inside a real Home Assistant test instance."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed

from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from custom_components.dynamic_energy_tariff_compare.const import (
    CONF_ENERGY_ENTITY,
    CONF_PRICE_ENTITY,
    DEFAULTS,
    DOMAIN,
)

NOW = datetime(2026, 10, 10, 10, 5, tzinfo=timezone.utc)
SLOT0 = datetime(2026, 10, 10, 10, 0, tzinfo=timezone.utc)
ENERGY = "sensor.edl21_total_active_energy"
PRICE = "sensor.epex_spot_data_total_price"
ENERGY_ATTRS = {"device_class": "energy", "unit_of_measurement": "kWh", "state_class": "total_increasing"}
ENERGY_WH_ATTRS = {**ENERGY_ATTRS, "unit_of_measurement": "Wh"}


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
        "fixed_price_ct": 30.0,
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


async def test_config_flow_bad_price_unit_legacy(hass: HomeAssistant) -> None:
    hass.states.async_set(ENERGY, "1000", ENERGY_ATTRS)
    hass.states.async_set("sensor.weird", "5", {"unit_of_measurement": "SEK/kWh"})
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], _options(price_entity="sensor.weird")
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {CONF_PRICE_ENTITY: "unknown_price_unit"}


async def _setup(hass: HomeAssistant, **kw) -> MockConfigEntry:
    entry = MockConfigEntry(domain=DOMAIN, data=_options(**kw), title="Dynamic Energy Tariff Compare")
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
    eff0 = 10.0  # the price sensor is the final all-in price
    eff1 = 11.0
    expected = (4 / 3 * eff0 + 2 / 3 * eff1) / 100
    assert t.dynamic_energy_cost_eur == pytest.approx(expected)
    assert t.fixed_energy_cost_eur == pytest.approx(0.60)

    # debounce -> entities update
    async_fire_time_changed(hass, NOW + timedelta(minutes=16))
    await hass.async_block_till_done()
    state = hass.states.get("sensor.dynamic_energy_tariff_compare_dynamic_energy_cost")
    assert state is not None
    assert float(state.state) == pytest.approx(expected, abs=1e-4)
    assert state.attributes["state_class"] == "total"
    assert state.attributes["device_class"] == "monetary"
    cons = hass.states.get("sensor.dynamic_energy_tariff_compare_consumption")
    assert float(cons.state) == pytest.approx(2.0)
    cur = hass.states.get("sensor.dynamic_energy_tariff_compare_current_dynamic_price")
    assert float(cur.state) == pytest.approx(eff1, abs=1e-3)
    assert len(cur.attributes["today"]) == 8
    assert hass.states.get("sensor.dynamic_energy_tariff_compare_current_spot_price") is None

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


async def test_state_only_price_sensor_and_wh_meter(hass: HomeAssistant, freezer) -> None:
    """Core Tibber-like price (state only, EUR/kWh) and an EDL21 meter reporting Wh."""
    freezer.move_to(NOW)
    hass.states.async_set("sensor.provider_price", "0.30", {"unit_of_measurement": "EUR/kWh"})
    hass.states.async_set(ENERGY, "1000000", ENERGY_WH_ATTRS)
    entry = await _setup(hass, price_entity="sensor.provider_price")
    freezer.move_to(NOW + timedelta(minutes=5))
    hass.states.async_set(ENERGY, "1001000", ENERGY_WH_ATTRS)  # +1000 Wh = 1 kWh
    await hass.async_block_till_done()
    t = entry.runtime_data.acc.totals
    assert t.energy_kwh == pytest.approx(1.0)
    assert t.dynamic_energy_cost_eur == pytest.approx(0.30)
    assert t.fixed_energy_cost_eur == pytest.approx(0.30)


@pytest.mark.parametrize("unit", ["MWh", "GWh"])
async def test_large_energy_units_converted(hass: HomeAssistant, freezer, unit) -> None:
    freezer.move_to(NOW)
    hass.states.async_set("sensor.provider_price", "30", {"unit_of_measurement": "ct/kWh"})
    attrs = {**ENERGY_ATTRS, "unit_of_measurement": unit}
    hass.states.async_set(ENERGY, "1", attrs)
    entry = await _setup(hass, price_entity="sensor.provider_price")
    freezer.move_to(NOW + timedelta(minutes=5))
    step = 0.001 if unit == "MWh" else 0.000001
    hass.states.async_set(ENERGY, str(1 + step), attrs)
    await hass.async_block_till_done()
    assert entry.runtime_data.acc.totals.energy_kwh == pytest.approx(1.0)


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


async def test_provider_name_and_stable_entity_ids(hass: HomeAssistant) -> None:
    from homeassistant.helpers import device_registry as dr  # noqa: PLC0415

    hass.states.async_set(PRICE, "0.1", _epex_attrs())
    hass.states.async_set(ENERGY, "1000", ENERGY_ATTRS)
    entry = await _setup(hass, provider_name="Rabot Energy")
    ids = sorted(s.entity_id for s in hass.states.async_all() if "dynamic_energy_tariff_compare" in s.entity_id)
    assert "sensor.dynamic_energy_tariff_compare_savings" in ids
    assert "sensor.dynamic_energy_tariff_compare_dynamic_total_cost" in ids
    assert "button.dynamic_energy_tariff_compare_backfill" in ids
    assert len(ids) == 13
    devices = dr.async_entries_for_config_entry(dr.async_get(hass), entry.entry_id)
    assert [d.name for d in devices] == ["Dynamic Energy Tariff Compare (Rabot Energy)"]
    state = hass.states.get("sensor.dynamic_energy_tariff_compare_savings")
    assert state.attributes["friendly_name"] == "Dynamic Energy Tariff Compare (Rabot Energy) Savings with dynamic tariff"


async def test_example_dashboard_entity_ids_exist(hass: HomeAssistant) -> None:
    """Every integration entity referenced in examples/ must exist with that ID."""
    import re  # noqa: PLC0415
    from pathlib import Path  # noqa: PLC0415

    hass.states.async_set(PRICE, "0.1", _epex_attrs())
    hass.states.async_set(ENERGY, "1000", ENERGY_ATTRS)
    await _setup(hass)
    examples = Path(__file__).parents[1] / "examples"
    utility = set(re.findall(r"^  (\w+):$", (examples / "utility_meter.yaml").read_text(), re.M))
    referenced = set()
    for file in [*examples.glob("*.yaml"), examples.parent / "README.md"]:
        referenced |= set(re.findall(r"(?:sensor|button)\.dynamic_energy_tariff_compare_\w+", file.read_text()))
    assert referenced
    for entity_id in referenced:
        if entity_id.split(".", 1)[1] in utility:
            continue
        assert hass.states.get(entity_id) is not None, entity_id


@pytest.mark.parametrize("expected_lingering_timers", [True])  # utility_meter reset timers
async def test_example_utility_meter_entity_ids(hass: HomeAssistant) -> None:
    """examples/utility_meter.yaml creates the entity IDs used in the dashboard."""
    from pathlib import Path  # noqa: PLC0415

    from homeassistant.setup import async_setup_component  # noqa: PLC0415
    from homeassistant.util.yaml import parse_yaml  # noqa: PLC0415

    hass.states.async_set(PRICE, "0.1", _epex_attrs())
    hass.states.async_set(ENERGY, "1000", ENERGY_ATTRS)
    await _setup(hass)
    config = parse_yaml((Path(__file__).parents[1] / "examples" / "utility_meter.yaml").read_text())
    assert await async_setup_component(hass, "utility_meter", config)
    await hass.async_block_till_done()
    for key in config["utility_meter"]:
        assert hass.states.get(f"sensor.{key}") is not None, key
