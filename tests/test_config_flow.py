"""Regression tests for the config/options flow ("Invalid flow specified").

The flows are driven through the same HTTP API the frontend uses, with sensors
mimicking core Tibber (price, EUR/kWh, state only) and EDL21 (energy, Wh).
"""

from __future__ import annotations

import asyncio
from http import HTTPStatus
from unittest.mock import patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component

from custom_components.dynamic_energy_tariff_compare.const import DOMAIN, LEGACY_KEYS

ENERGY = "sensor.smart_meter_positive_active_energy_total"
PRICE = "sensor.tibber_pricing_de_current_price"
EDL21_ATTRS = {
    "device_class": "energy",
    "state_class": "total_increasing",
    "unit_of_measurement": "Wh",
    "friendly_name": "Smart Meter Positive active energy total",
}
TIBBER_ATTRS = {
    "app_nickname": "Home",
    "grid_company": "Stadtwerke Jena-Pößneck",
    "estimated_annual_consumption": 3200,
    "max_price": 0.3512,
    "avg_price": 0.3011,
    "min_price": 0.2655,
    "off_peak_1": 0.2833,
    "peak": 0.3189,
    "off_peak_2": 0.2901,
    "intraday_price_ranking": 0.42,
    "unit_of_measurement": "EUR/kWh",
    "state_class": "measurement",
    "icon": "mdi:currency-eur",
    "friendly_name": "Tibber Pricing DE Current Price",
}
USER_INPUT = {
    "provider_name": "Tibber",
    "energy_entity": ENERGY,
    "price_entity": PRICE,
    "fixed_price_ct": 32.0,
    "fixed_base_fee_eur_month": 14.01,
    "provider_monthly_fee_eur": 5.99,
}
# What a 0.2.x entry looked like (user's partly created entry)
LEGACY_DATA = {
    **USER_INPUT,
    "price_is_final": True,
    "use_energy_charts": False,
    "provider_markup_ct": 0,
    "network_fee_ct": 0,
    "levies_ct": 0,
    "vat_percent": 0,
    "grid_base_fee_eur_month": 0,
}


@pytest.fixture
async def client(hass: HomeAssistant, hass_client):
    assert await async_setup_component(hass, "config", {})
    hass.states.async_set(ENERGY, "12345678", EDL21_ATTRS)
    hass.states.async_set(PRICE, "0.2874", TIBBER_ATTRS)
    return await hass_client()


async def _start(client, path="/api/config/config_entries/flow", handler=DOMAIN):
    resp = await client.post(path, json={"handler": handler})
    assert resp.status == HTTPStatus.OK, await resp.text()
    data = await resp.json()
    assert data["type"] == "form", data
    return data


async def _submit(client, flow_id, body, path="/api/config/config_entries/flow"):
    resp = await client.post(f"{path}/{flow_id}", json=body)
    return resp.status, await resp.json()


async def test_user_flow_tibber_edl21_wh(hass: HomeAssistant, client) -> None:
    form = await _start(client)
    keys = [f["name"] for f in form["data_schema"]]
    assert keys == list(USER_INPUT)  # minimal form
    status, res = await _submit(client, form["flow_id"], USER_INPUT)
    assert status == HTTPStatus.OK, res
    assert res["type"] == "create_entry", res
    assert res["title"] == "Dynamic Energy Tariff Compare (Tibber)"
    entry = hass.config_entries.async_entries(DOMAIN)[0]
    assert entry.version == 2
    assert dict(entry.data) == USER_INPUT
    await hass.async_block_till_done()
    assert entry.runtime_data.acc.totals.last_value == pytest.approx(12345.678)  # Wh -> kWh


@pytest.mark.parametrize(
    ("unit", "ok"),
    [
        ("EUR/kWh", True), ("€/kWh", True), ("ct/kWh", True), ("Cent/kWh", True),
        ("EUR/MWh", True), ("EUR / kWh", True), ("c/kWh", True), ("SEK/kWh", False),
        ("kWh", False), (None, False),
    ],
)
async def test_units_never_break_flow(hass: HomeAssistant, client, unit, ok) -> None:
    attrs = {k: v for k, v in TIBBER_ATTRS.items() if k != "unit_of_measurement"}
    if unit is not None:
        attrs["unit_of_measurement"] = unit
    hass.states.async_set(PRICE, "0.2874", attrs)
    form = await _start(client)
    status, res = await _submit(client, form["flow_id"], USER_INPUT)
    assert status == HTTPStatus.OK, res
    if ok:
        assert res["type"] == "create_entry", res
        return
    assert res["type"] == "form"
    assert res["errors"] == {"price_entity": "unknown_price_unit"}
    assert res["description_placeholders"]["unit"] == (unit or "—")
    # the same flow is still alive and accepts a corrected sensor
    hass.states.async_set("sensor.good_price", "29.1", {"unit_of_measurement": "ct/kWh"})
    status, res = await _submit(client, form["flow_id"], {**USER_INPUT, "price_entity": "sensor.good_price"})
    assert status == HTTPStatus.OK, res
    assert res["type"] == "create_entry", res


@pytest.mark.parametrize("state", ["unavailable", "unknown"])
async def test_unavailable_price_sensor_accepted(hass: HomeAssistant, client, state) -> None:
    hass.states.async_set(PRICE, state, {"friendly_name": "Tibber Pricing DE Current Price"})
    form = await _start(client)
    status, res = await _submit(client, form["flow_id"], USER_INPUT)
    assert status == HTTPStatus.OK, res
    assert res["type"] == "create_entry", res


async def test_unavailable_energy_sensor_accepted(hass: HomeAssistant, client) -> None:
    hass.states.async_set(ENERGY, "unavailable", EDL21_ATTRS)
    form = await _start(client)
    status, res = await _submit(client, form["flow_id"], USER_INPUT)
    assert res["type"] == "create_entry", res


async def test_missing_price_entity_is_form_error(hass: HomeAssistant, client) -> None:
    form = await _start(client)
    status, res = await _submit(client, form["flow_id"], {**USER_INPUT, "price_entity": "sensor.does_not_exist"})
    assert status == HTTPStatus.OK
    assert res["errors"] == {"price_entity": "entity_not_found"}
    body = {k: v for k, v in USER_INPUT.items() if k != "price_entity"}
    status, res = await _submit(client, form["flow_id"], body)
    # omitted -> form default (the last entered value) applies -> same error, flow stays alive
    assert status == HTTPStatus.OK, res
    assert res["errors"] == {"price_entity": "entity_not_found"}
    status, res = await _submit(client, form["flow_id"], USER_INPUT)
    assert res["type"] == "create_entry", res


async def test_unexpected_error_becomes_form_error(hass: HomeAssistant, client) -> None:
    form = await _start(client)
    with patch(
        "custom_components.dynamic_energy_tariff_compare.config_flow.calc.price_factor_to_ct_kwh",
        side_effect=RuntimeError("boom"),
    ):
        status, res = await _submit(client, form["flow_id"], USER_INPUT)
    assert status == HTTPStatus.OK, res
    assert res["type"] == "form"
    assert res["errors"] == {"base": "unknown"}
    status, res = await _submit(client, form["flow_id"], USER_INPUT)
    assert res["type"] == "create_entry", res


async def test_legacy_v1_entry_migrates_and_options_flow_works(hass: HomeAssistant, client) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=1,
        unique_id=ENERGY,
        title="Dynamic Energy Tariff Compare (Tibber)",
        data=LEGACY_DATA,
        options={"price_entity": None, "fixed_price_ct": None, "vat_percent": 19},
    )
    entry.add_to_hass(hass)
    registry = er.async_get(hass)
    old = registry.async_get_or_create(
        "sensor", DOMAIN, f"{entry.entry_id}_current_spot_price",
        suggested_object_id=f"{DOMAIN}_current_spot_price", config_entry=entry,
    )
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.version == 2
    assert not set(LEGACY_KEYS) & set(entry.data)
    assert not set(LEGACY_KEYS) & set(entry.options)
    assert "price_entity" not in entry.options  # None from old options dropped
    assert registry.async_get(old.entity_id) is None
    mgr = entry.runtime_data
    assert mgr.price_entity == PRICE
    assert mgr.params.fixed_price_ct == 32.0  # None in options -> default

    path = "/api/config/config_entries/options/flow"
    form = await _start(client, path, entry.entry_id)
    assert [f["name"] for f in form["data_schema"]] == list(USER_INPUT)
    status, res = await _submit(client, form["flow_id"], {**USER_INPUT, "fixed_price_ct": 33.5}, path)
    assert status == HTTPStatus.OK, res
    assert res["type"] == "create_entry", res
    await hass.async_block_till_done()
    assert entry.runtime_data.params.fixed_price_ct == 33.5


async def test_options_flow_unknown_unit_is_form_error(hass: HomeAssistant, client) -> None:
    entry = MockConfigEntry(domain=DOMAIN, version=2, unique_id=ENERGY, data=USER_INPUT)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    hass.states.async_set("sensor.weird", "5", {"unit_of_measurement": "SEK/kWh"})
    path = "/api/config/config_entries/options/flow"
    form = await _start(client, path, entry.entry_id)
    status, res = await _submit(client, form["flow_id"], {**USER_INPUT, "price_entity": "sensor.weird"}, path)
    assert status == HTTPStatus.OK, res
    assert res["errors"] == {"price_entity": "unknown_price_unit"}
    status, res = await _submit(client, form["flow_id"], USER_INPUT, path)
    assert res["type"] == "create_entry", res


async def test_entry_without_price_sensor_still_loads(hass: HomeAssistant) -> None:
    """A 0.2.x entry that used the removed energy-charts fallback keeps loading."""
    hass.states.async_set(ENERGY, "1000", EDL21_ATTRS)
    data = {**LEGACY_DATA, "price_entity": None, "use_energy_charts": True}
    entry = MockConfigEntry(domain=DOMAIN, version=1, unique_id=ENERGY, data=data)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.runtime_data.price_entity is None
    assert "No price sensor selected" in entry.runtime_data.last_price_error


async def test_double_submit_does_not_end_in_invalid_flow(hass: HomeAssistant, client) -> None:
    """Root cause of "Invalid flow specified" in 0.2.0.

    A second submit while the first one is still setting up the new entry ran the
    step again, aborted with "already_configured" and removed the flow, so the
    first request answered 404 "Invalid flow specified" although the entry exists.
    """
    import custom_components.dynamic_energy_tariff_compare as integration  # noqa: PLC0415

    original = integration.async_setup_entry

    async def slow_setup(hass, entry):
        await asyncio.sleep(0.2)  # first-time setup on real hardware takes a moment
        return await original(hass, entry)

    form = await _start(client)

    async def second_submit():
        await asyncio.sleep(0.05)  # arrives while the first one is still setting up
        return await _submit(client, form["flow_id"], USER_INPUT)

    with patch.object(integration, "async_setup_entry", slow_setup):
        results = await asyncio.gather(
            _submit(client, form["flow_id"], USER_INPUT),
            second_submit(),
        )
    statuses = [status for status, _ in results]
    types = [res.get("type") for _, res in results]
    assert HTTPStatus.NOT_FOUND not in statuses, results
    assert "create_entry" in types, results
    assert "abort" not in types, results
    assert len(hass.config_entries.async_entries(DOMAIN)) == 1
