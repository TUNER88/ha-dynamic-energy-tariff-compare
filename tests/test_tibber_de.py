"""Tibber Germany (postcode) price source, using a real price-overview response for 07646."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import aiohttp
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from custom_components.dynamic_energy_tariff_compare import calc
from custom_components.dynamic_energy_tariff_compare.const import DOMAIN, TIBBER_DE_URL

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "tibber_price_overview_07646.json").read_text())
BERLIN = ZoneInfo("Europe/Berlin")
ENERGY = "sensor.edl21_total_active_energy"
ENERGY_ATTRS = {"device_class": "energy", "unit_of_measurement": "Wh", "state_class": "total_increasing"}
NOW = datetime(2026, 10, 10, 11, 5, tzinfo=timezone.utc)  # 13:05 local
PRICE_SENSOR = "sensor.dynamic_energy_tariff_compare_current_dynamic_price"


def _ts(*args: int) -> int:
    return int(datetime(*args, tzinfo=BERLIN).timestamp())


# ------------------------------------------------------------------ parsing
def test_parse_overview_07646() -> None:
    ov = calc.parse_tibber_overview(FIXTURE, BERLIN)
    # yesterday + today in 15-min resolution; tomorrow is only placeholders (no "power")
    assert len(ov.prices) == 192
    assert not ov.tomorrow_available
    assert min(ov.prices) == _ts(2026, 10, 9, 0, 0)
    assert max(ov.prices) == _ts(2026, 10, 10, 23, 45)
    assert ov.prices[_ts(2026, 10, 9, 0, 0)] == pytest.approx(29.02)
    assert ov.prices[_ts(2026, 10, 10, 13, 0)] == pytest.approx(16.29)
    assert ov.components[_ts(2026, 10, 10, 13, 0)] == pytest.approx(
        {"power": 0.0, "taxes": 9.67, "grid": 6.6164}
    )
    assert ov.monthly_fee_eur == pytest.approx(15.64)
    assert ov.monthly_components == pytest.approx(
        {"tibber service fee": 5.99, "meter operator": 0.96, "grid": 8.69}
    )


def test_parse_hours_fallback_and_garbage() -> None:
    energy = {k: v for k, v in FIXTURE["energy"].items() if "QuarterHours" not in k}
    ov = calc.parse_tibber_overview({"energy": energy}, BERLIN)
    assert len(ov.prices) == 192  # 2 days x 24 h x 4 slots
    hour0 = FIXTURE["energy"]["todayHours"][0]["priceIncludingVat"] * 100
    assert all(ov.prices[_ts(2026, 10, 10, 0, m)] == pytest.approx(hour0) for m in (0, 15, 30, 45))
    assert calc.parse_tibber_overview(None, BERLIN).prices == {}
    assert calc.parse_tibber_overview({"energy": {"todayQuarterHours": [{"x": 1}, 5]}}, BERLIN).prices == {}


def test_parse_dst_end_repeated_hour() -> None:
    comps = [{"type": "power", "priceIncludingVat": 0.1}]
    entries = [
        {"date": "2026-10-25", "hour": h, "minute": 0, "priceIncludingVat": p, "priceComponents": comps}
        for h, p in [(1, 0.1), (2, 0.2), (2, 0.3), (3, 0.4)]
    ]
    ov = calc.parse_tibber_overview({"energy": {"todayHours": entries}}, BERLIN)
    assert len(ov.prices) == 16  # 25-hour day: both 02:00 hours kept
    first = datetime(2026, 10, 25, 2, 0, tzinfo=BERLIN).timestamp()
    assert ov.prices[int(first)] == pytest.approx(20)
    assert ov.prices[int(first) + 3600] == pytest.approx(30)


@pytest.mark.parametrize(
    ("value", "expected"),
    [("07646", "07646"), (" 076 46 ", "07646"), (7646, None), ("1234", None), ("abcde", None), (None, None)],
)
def test_valid_postcode(value, expected) -> None:
    assert calc.valid_postcode(value) == expected


# --------------------------------------------------------------- config flow
async def _menu_tibber(hass: HomeAssistant) -> dict:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    assert result["type"] is FlowResultType.MENU
    assert result["menu_options"] == ["tibber", "sensor"]  # Tibber first = default
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"next_step_id": "tibber"})
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "tibber"
    return result


async def test_tibber_flow_creates_entry_and_fetches(hass: HomeAssistant, aioclient_mock, freezer) -> None:
    await hass.config.async_set_time_zone("Europe/Berlin")
    freezer.move_to(NOW)
    aioclient_mock.get(TIBBER_DE_URL, params={"postalCode": "07646"}, json=FIXTURE)
    hass.states.async_set(ENERGY, "12345678", ENERGY_ATTRS)

    result = await _menu_tibber(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"provider_name": "Tibber", "postcode": " 07646", "energy_entity": ENERGY}
    )
    assert result["type"] is FlowResultType.FORM, result
    assert result["step_id"] == "tibber_fees"
    ph = result["description_placeholders"]
    assert ph["postcode"] == "07646"
    assert ph["price"] == "16,29"
    assert ph["monthly"] == "15,64"
    assert "tibber service fee 5,99 €" in ph["monthly_components"]
    defaults = {str(k): k.default() for k in result["data_schema"].schema}
    assert defaults["provider_monthly_fee_eur"] == pytest.approx(15.64)  # prefilled from Tibber

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {"fixed_price_ct": 32.0, "fixed_base_fee_eur_month": 14.01, "provider_monthly_fee_eur": 15.64},
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY, result
    assert result["data"] == {
        "price_source": "tibber_de",
        "provider_name": "Tibber",
        "postcode": "07646",
        "energy_entity": ENERGY,
        "fixed_price_ct": 32.0,
        "fixed_base_fee_eur_month": 14.01,
        "provider_monthly_fee_eur": 15.64,
    }
    await hass.async_block_till_done()
    mgr = hass.config_entries.async_entries(DOMAIN)[0].runtime_data
    assert mgr.tibber and mgr.price_entity is None
    assert len(mgr.prices) == 192
    assert mgr.current_slot_price() == pytest.approx(16.29)
    assert len(mgr.price_list()["today"]) == 96
    assert mgr.acc.totals.last_value == pytest.approx(12345.678)  # Wh -> kWh

    state = hass.states.get(PRICE_SENSOR)
    assert float(state.state) == pytest.approx(16.29)
    assert state.attributes["source"] == "Tibber Germany (postcode 07646)"
    assert state.attributes["postcode"] == "07646"
    assert state.attributes["components_ct"] == {"power": 0.0, "taxes": 9.67, "grid": 6.616}
    assert state.attributes["tibber_monthly_fees_eur"]["total_eur"] == pytest.approx(15.64)
    assert state.attributes["tibber_monthly_fees_eur"]["tibber_service_fee"] == pytest.approx(5.99)


@pytest.mark.parametrize(
    ("postcode", "mock", "error"),
    [
        ("1234", None, "invalid_postcode"),
        ("00000", {"status": 404, "json": {"message": "Response code 404 (Not Found)"}}, "invalid_postcode"),
        ("00000", {"status": 204, "text": ""}, "invalid_postcode"),
        ("07646", {"status": 503, "text": "busy"}, "cannot_connect"),
        ("07646", {"exc": aiohttp.ClientError("boom")}, "cannot_connect"),
        ("07646", {"exc": TimeoutError()}, "cannot_connect"),
        ("07646", {"text": "<html>not json</html>"}, "cannot_connect"),
        ("07646", {"json": {"energy": {}}}, "no_prices"),
    ],
)
async def test_tibber_flow_errors(hass: HomeAssistant, aioclient_mock, postcode, mock, error) -> None:
    hass.states.async_set(ENERGY, "1", ENERGY_ATTRS)
    if mock is not None:
        aioclient_mock.get(TIBBER_DE_URL, params={"postalCode": postcode}, **mock)
    result = await _menu_tibber(hass)
    user = {"provider_name": "", "postcode": postcode, "energy_entity": ENERGY}
    result = await hass.config_entries.flow.async_configure(result["flow_id"], user)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "tibber"
    assert result["errors"] == {"postcode": error}
    # the user can correct the postcode and continue
    aioclient_mock.clear_requests()
    aioclient_mock.get(TIBBER_DE_URL, params={"postalCode": "07646"}, json=FIXTURE)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {**user, "postcode": "07646"})
    assert result["step_id"] == "tibber_fees"


async def test_tibber_fee_prefill_without_monthly_fees(hass: HomeAssistant, aioclient_mock) -> None:
    payload = {k: v for k, v in FIXTURE.items() if k != "monthlyFees"}
    aioclient_mock.get(TIBBER_DE_URL, json=payload)
    hass.states.async_set(ENERGY, "1", ENERGY_ATTRS)
    result = await _menu_tibber(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"postcode": "07646", "energy_entity": ENERGY}
    )
    defaults = {str(k): k.default() for k in result["data_schema"].schema}
    assert defaults["provider_monthly_fee_eur"] == pytest.approx(5.99)  # integration default
    assert result["description_placeholders"]["monthly"] == "–"


def _tibber_entry(**kw) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        version=3,
        title="Dynamic Energy Tariff Compare (Tibber)",
        data={
            "price_source": "tibber_de",
            "provider_name": "Tibber",
            "postcode": "07646",
            "energy_entity": ENERGY,
            "fixed_price_ct": 32.0,
            "fixed_base_fee_eur_month": 14.01,
            "provider_monthly_fee_eur": 15.64,
            **kw,
        },
    )


async def test_options_flow_tibber(hass: HomeAssistant, aioclient_mock) -> None:
    aioclient_mock.get(TIBBER_DE_URL, params={"postalCode": "07646"}, json=FIXTURE)
    hass.states.async_set(ENERGY, "1", ENERGY_ATTRS)
    entry = _tibber_entry()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    calls = aioclient_mock.call_count

    result = await hass.config_entries.options.async_init(entry.entry_id)
    keys = [str(k) for k in result["data_schema"].schema]
    assert keys == [
        "provider_name", "postcode", "energy_entity",
        "fixed_price_ct", "fixed_base_fee_eur_month", "provider_monthly_fee_eur",
    ]
    base = {**entry.data}
    base.pop("price_source")
    # invalid postcode
    result = await hass.config_entries.options.async_configure(result["flow_id"], {**base, "postcode": "12"})
    assert result["errors"] == {"postcode": "invalid_postcode"}
    # unchanged postcode: no new lookup needed
    result = await hass.config_entries.options.async_configure(result["flow_id"], {**base, "fixed_price_ct": 35.5})
    assert result["type"] is FlowResultType.CREATE_ENTRY, result
    assert result["data"]["fixed_price_ct"] == 35.5
    assert result["data"]["price_source"] == "tibber_de"
    await hass.async_block_till_done()
    assert entry.runtime_data.params.fixed_price_ct == 35.5

    # a changed postcode is validated against Tibber
    aioclient_mock.get(TIBBER_DE_URL, params={"postalCode": "00000"}, status=404)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {**base, "postcode": "00000"})
    assert result["errors"] == {"postcode": "invalid_postcode"}
    assert aioclient_mock.call_count > calls


async def test_tibber_fetch_failure_keeps_archive(hass: HomeAssistant, aioclient_mock, freezer) -> None:
    await hass.config.async_set_time_zone("Europe/Berlin")
    freezer.move_to(NOW)
    aioclient_mock.get(TIBBER_DE_URL, json=FIXTURE)
    hass.states.async_set(ENERGY, "1", ENERGY_ATTRS)
    entry = _tibber_entry()
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    mgr = entry.runtime_data
    assert len(mgr.prices) == 192

    aioclient_mock.clear_requests()
    aioclient_mock.get(TIBBER_DE_URL, exc=aiohttp.ClientError("down"))
    await mgr.async_refresh_tibber()
    assert len(mgr.prices) == 192  # nothing lost
    assert "Tibber price lookup failed" in mgr.last_price_error
    assert mgr.current_slot_price() == pytest.approx(16.29)

    # archive survives a restart (persisted in the Store)
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert len(entry.runtime_data.prices) == 192


async def test_migration_v2_to_v3(hass: HomeAssistant) -> None:
    hass.states.async_set(ENERGY, "1", ENERGY_ATTRS)
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=2,
        data={
            "provider_name": "Tibber", "energy_entity": ENERGY, "price_entity": "sensor.price",
            "price_unit": "auto", "fixed_price_ct": 32.0, "fixed_base_fee_eur_month": 14.01,
            "provider_monthly_fee_eur": 5.99,
        },
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.version == 3
    assert entry.data["price_source"] == "sensor"
    assert entry.runtime_data.price_entity == "sensor.price"
    assert not entry.runtime_data.tibber


async def test_future_version_refused(hass: HomeAssistant) -> None:
    entry = _tibber_entry()
    entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(entry, version=4)
    assert not await hass.config_entries.async_setup(entry.entry_id)
