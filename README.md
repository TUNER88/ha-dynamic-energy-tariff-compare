# Dynamic Energy Tariff Compare for Home Assistant

[![HACS Custom](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://hacs.xyz/docs/faq/custom_repositories/)
[![GitHub Release](https://img.shields.io/github/v/release/TUNER88/ha-dynamic-energy-tariff-compare)](https://github.com/TUNER88/ha-dynamic-energy-tariff-compare/releases)
[![CI](https://github.com/TUNER88/ha-dynamic-energy-tariff-compare/actions/workflows/ci.yml/badge.svg)](https://github.com/TUNER88/ha-dynamic-energy-tariff-compare/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

**What would you pay on a dynamic electricity tariff, with your real consumption?**

This integration takes your real consumption from your smart meter (e.g. Home Assistant's
**EDL21** integration) and prices every quarter hour with the day-ahead spot price, plus
your provider's markup, grid fees, taxes and levies. It then compares the result with your
current **fixed-price tariff**. It works for any spot-price-based tariff, for example Tibber,
Rabot Energy, Octopus (dynamicOctopus), tado° Energy (formerly aWATTar) or 1KOMMA5° Dynamic Pulse.
No account with any of them is needed.

* 15-minute day-ahead prices for bidding zone **DE-LU**. The day-ahead market moved to a
  15-minute MTU on 1 Oct 2025.
* Prices come from a **price integration you already use** (EPEX Spot, Nord Pool, your
  provider's integration, …). If you don't select one, it uses the free **energy-charts.info** API.
* Each kWh is assigned to the quarter hour in which it was consumed. Readings that span
  several slots are split proportionally by time.
* Totals survive restarts. A **backfill** action computes the past weeks from the recorder
  statistics, so you get a result right away.

## How the calculation works

For every 15-minute slot *s* with consumption *E(s)* (kWh):

```
dynamic_price(s) [ct/kWh] = spot_net(s) × (1 + VAT) + provider_markup + grid_fee + taxes_and_levies
dynamic_cost             = Σ E(s) × dynamic_price(s)  +  months × (provider_monthly_fee + grid_base_fee)
fixed_cost               = Σ E(s) × fixed_price       +  months × fixed_base_fee
savings                  = fixed_cost − dynamic_cost        (positive = dynamic tariff is cheaper)
```

* Everything except the spot price is entered **gross** (incl. VAT). VAT is applied to the
  net spot price.
* If your price sensor **already is the final price** (e.g. your provider's own integration,
  or the EPEX Spot *total price* with surcharges configured), turn on *Price sensor is final
  price*. The price is then used as is.
* Monthly fees are prorated over the tracked time (1 month = 30.44 days).

## Installation

### HACS (custom repository)

[![Open your Home Assistant instance and open a repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=TUNER88&repository=ha-dynamic-energy-tariff-compare&category=integration)

Or manually: HACS → ⋮ → *Custom repositories* → `https://github.com/TUNER88/ha-dynamic-energy-tariff-compare`,
type *Integration* → install **Dynamic Energy Tariff Compare** → restart Home Assistant.

### Manual

Download `dynamic_energy_tariff_compare.zip` from the
[latest release](https://github.com/TUNER88/ha-dynamic-energy-tariff-compare/releases/latest) and unpack it into
`<config>/custom_components/dynamic_energy_tariff_compare/`. Then restart Home Assistant.

### Set up

*Settings → Devices & services → Add integration → Dynamic Energy Tariff Compare*. All options
can be changed later via *Configure*, including the sensors.

## Configuration

| Option | Meaning | Example default |
|---|---|---|
| Provider name | Optional, for display only (device / entry name). Entity IDs don't change | – |
| Grid import energy sensor | `total_increasing` kWh meter, e.g. EDL21 `sensor.<meter>_total_active_energy` / OBIS 1.8.0. Only `device_class: energy` sensors are offered | – |
| Price sensor | Optional, see [price data](#recommended-integrations-for-price-data). If empty, energy-charts.info is used | – |
| Price sensor is final price | **Off:** raw net spot price, so VAT, markup and fees are added. **On:** all-in price, used as is | off |
| Use energy-charts.info | Fetch DE-LU prices if no price sensor is selected | on |
| Fixed tariff energy price | your current Arbeitspreis, ct/kWh gross | 32.00 |
| Fixed tariff base fee | your current Grundpreis, €/month gross | 12.00 |
| Provider markup | dynamic provider's markup on the spot price, ct/kWh gross | 2.15 |
| Grid fee (Netzentgelt) | grid operator's Arbeitspreis, ct/kWh gross | 9.00 |
| Taxes & levies | Stromsteuer + Konzessionsabgabe + Umlagen, ct/kWh gross | 7.52 |
| VAT | applied to the spot price | 19 % |
| Provider monthly fee | dynamic provider's base fee, €/month gross | 5.99 |
| Grid base fee | grid base price + metering (Messstellenbetrieb), €/month gross | 8.28 |

> ⚠️ **The defaults are only examples.** Grid fees, Konzessionsabgabe and metering costs
> depend on your grid operator and municipality. The provider's markup and monthly fee depend
> on the provider. Copy the real values from the provider's price calculator for your postcode
> (see below) and from your current contract or bill.
>
> The example 2026 levies: Stromsteuer 2.05 + Konzessionsabgabe 1.32 (municipality < 25k
> inhabitants) + §19 StromNEV 1.559 + KWKG 0.446 + Offshore 0.941 = 6.32 ct net
> (7.52 ct gross).
>
> Changed values apply to new consumption only. Press **Reset** and then **Backfill** to
> recalculate.

### Finding the values for your postcode

Most dynamic-tariff providers show a price breakdown after you enter your postcode and annual
consumption. It lists grid fees, taxes/levies, their own markup and monthly fees.

| Provider | Dynamic tariff | What their website says (checked Oct 2026) | Where to look |
|---|---|---|---|
| Tibber | Smart Dynamic | 15-minute wholesale prices; Tibber fee 5.99 €/month | [tibber.com/de](https://tibber.com/de) → price calculator |
| Rabot Energy | dynamic tariff | price follows the spot price set every 15 minutes, plus taxes, levies, grid fees and a monthly base fee | [rabot.energy](https://www.rabot.energy/) → tariff calculator |
| Octopus Energy Germany | dynamicOctopus | price changes every 15 minutes based on EPEX day-ahead prices; smart meter required | [octopusenergy.de/dynamic-octopus](https://octopusenergy.de/dynamic-octopus) |
| tado° Energy (formerly aWATTar) | tado° HOURLY | **hourly** EPEX Spot DE prices; base fee 4.58 €/month plus grid fees and metering | [energy.tado.com/tariffs/hourly](https://energy.tado.com/tariffs/hourly) → Preisrechner |
| 1KOMMA5° | Dynamic Pulse | dynamic tariff combined with their Heartbeat energy management | [1komma5.com/de](https://1komma5.com/de/) |

For tariffs billed **hourly** (e.g. tado° HOURLY), select a price sensor with hourly prices
(e.g. EPEX Spot configured for 60 minutes). Each hourly price is then applied to all four
quarter hours.

## Recommended integrations for price data

| Integration | Install | 15-min support | What this integration reads | Setting |
|---|---|---|---|---|
| [EPEX Spot](https://github.com/mampfes/ha_epex_spot) (`mampfes/ha_epex_spot`) | HACS | yes, selectable 15 or 60 min since v4.0.0. Data sources include ENTSO-E and energy-charts.info | attribute `data` = `[{start_time, end_time, price_per_kwh}]` | *Market price* sensor → final **off**. *Total price* sensor (percentage/absolute surcharge + tax configured in EPEX Spot) → final **on** |
| [Nord Pool](https://www.home-assistant.io/integrations/nordpool/) (core) | built in | yes, the docs say it switches to the 15-min MTU automatically | current state only (spot price, VAT and other costs not included) | final **off** |
| [Nord Pool](https://github.com/custom-components/nordpool) (`custom-components/nordpool`) | HACS | quarter-hour price updates since v0.0.18 | attributes `raw_today` / `raw_tomorrow` | without VAT/additional costs → final **off** |
| [Tibber](https://www.home-assistant.io/integrations/tibber/) (core) | built in | n/a | current state (all-in price of your Tibber contract) | **only if you already have a Tibber account**, final **on** |
| energy-charts.info | built in here | yes (15-min since Oct 2025, hourly before) | fetched directly | used when no price sensor is selected |

Any other sensor also works. Its current state is used for each slot, and the unit is
detected from `unit_of_measurement` (€/kWh, ct/kWh, €/MWh). Hourly lists are expanded to four
quarter hours.

## Entities

Entity IDs are fixed (also when you set a provider name):

| Entity ID | Unit | Class | Notes |
|---|---|---|---|
| `sensor.dynamic_energy_tariff_compare_current_spot_price` | ct/kWh | measurement | price of the source; attributes `today` / `tomorrow` (15-min lists), `source`, `last_error` |
| `sensor.dynamic_energy_tariff_compare_current_dynamic_price` | ct/kWh | measurement | effective dynamic price; attributes `today` / `tomorrow`, `fixed_price_ct`, formula |
| `sensor.dynamic_energy_tariff_compare_dynamic_energy_cost` | EUR | monetary / total | energy only |
| `sensor.dynamic_energy_tariff_compare_fixed_energy_cost` | EUR | monetary / total | energy only |
| `sensor.dynamic_energy_tariff_compare_dynamic_total_cost` | EUR | monetary / total | + prorated provider fee and grid base fee |
| `sensor.dynamic_energy_tariff_compare_fixed_total_cost` | EUR | monetary / total | + prorated base fee |
| `sensor.dynamic_energy_tariff_compare_savings` | EUR | monetary / total | attributes `savings_per_month_eur`, `projected_savings_per_year_eur`, `months_tracked`, `tracking_since`, `last_backfill` |
| `sensor.dynamic_energy_tariff_compare_consumption` | kWh | energy / total_increasing | tracked consumption |
| `sensor.dynamic_energy_tariff_compare_avg_dynamic_price` | ct/kWh | measurement | consumption-weighted; attribute `consumption_weighted_spot_ct` |
| `sensor.dynamic_energy_tariff_compare_avg_dynamic_price_incl_fees` | ct/kWh | measurement | |
| `sensor.dynamic_energy_tariff_compare_avg_fixed_price_incl_fees` | ct/kWh | measurement | |
| `sensor.dynamic_energy_tariff_compare_pending_energy` | kWh | diagnostic | consumption still waiting for a price |
| `button.dynamic_energy_tariff_compare_backfill` | | config | backfill 30 days |
| `button.dynamic_energy_tariff_compare_reset` | | config | reset all totals |

## Backfill

```yaml
action: dynamic_energy_tariff_compare.backfill
data:
  days: 60        # days before the current tracking start
response_variable: result
```

* **Energy:** recorder **5-minute short-term statistics** where they still exist (default
  retention ≈ 10 days), otherwise **hourly long-term statistics**. Hourly energy is spread
  evenly over the four quarter hours, so shifts in usage within an hour are lost.
* **Prices:** the price sensor's recorder history (state + price-list attributes). If it has
  none, energy-charts.info is used instead (raw spot, so surcharges are applied).
* Each run only fills the period **before** the current tracking start, so nothing is
  counted twice. Run it again with more days to go further back.
* Today's tariff values are used for the whole historic period.
* The added amount appears in the cost statistics at the time of the backfill.

`dynamic_energy_tariff_compare.reset` clears all totals.

## Dashboard

A complete, ready-to-paste dashboard is in [`examples/dashboard.yaml`](examples/dashboard.yaml).
Create a new dashboard → ⋮ → *Edit* → ⋮ → *Raw configuration editor* → paste.

Required:

* **View "Overview"**: built-in cards only (tile, entities, statistics-graph, history-graph).
* **View "Charts"**: [ApexCharts Card](https://github.com/RomRider/apexcharts-card) from HACS (Frontend).
* Monthly cards: the `utility_meter` helpers from [`examples/utility_meter.yaml`](examples/utility_meter.yaml).

### 1. Built-in cards: key figures and daily/monthly comparison

```yaml
type: grid
columns: 2
square: false
cards:
  - type: tile
    entity: sensor.dynamic_energy_tariff_compare_savings
    name: Savings with dynamic tariff
  - type: tile
    entity: sensor.dynamic_energy_tariff_compare_consumption
  - type: tile
    entity: sensor.dynamic_energy_tariff_compare_avg_dynamic_price_incl_fees
    name: Ø dynamic incl. fees
  - type: tile
    entity: sensor.dynamic_energy_tariff_compare_avg_fixed_price_incl_fees
    name: Ø fixed incl. fees
```

```yaml
type: statistics-graph
title: Monthly cost – dynamic vs. fixed
period: month          # or: day
days_to_show: 365
chart_type: bar
stat_types: [change]
entities:
  - entity: sensor.dynamic_energy_tariff_compare_dynamic_total_cost
    name: Dynamic
  - entity: sensor.dynamic_energy_tariff_compare_fixed_total_cost
    name: Fixed
```

### 2. ApexCharts: today/tomorrow 15-min price vs. fixed price

Columns show the effective dynamic price per quarter hour. They are green when cheaper than
your fixed price and red when more expensive. The fixed price is drawn as a flat line.

```yaml
type: custom:apexcharts-card
header: {show: true, title: Dynamic price (15 min) vs. fixed price, show_states: true}
graph_span: 2d
span: {start: day}
now: {show: true, label: Now}
series:
  - entity: sensor.dynamic_energy_tariff_compare_current_dynamic_price
    name: Cheaper than fixed
    type: column
    color: "#2e7d32"
    data_generator: |
      const fixed = entity.attributes.fixed_price_ct;
      return [...(entity.attributes.today || []), ...(entity.attributes.tomorrow || [])]
        .map(p => [new Date(p.start).getTime(), p.price <= fixed ? p.price : null]);
  - entity: sensor.dynamic_energy_tariff_compare_current_dynamic_price
    name: More expensive than fixed
    type: column
    color: "#c62828"
    data_generator: |
      const fixed = entity.attributes.fixed_price_ct;
      return [...(entity.attributes.today || []), ...(entity.attributes.tomorrow || [])]
        .map(p => [new Date(p.start).getTime(), p.price > fixed ? p.price : null]);
  - entity: sensor.dynamic_energy_tariff_compare_current_dynamic_price
    name: Fixed price
    type: line
    curve: stepline
    color: "#1565c0"
    data_generator: |
      const fixed = entity.attributes.fixed_price_ct;
      const start = new Date(); start.setHours(0, 0, 0, 0);
      return [[start.getTime(), fixed], [start.getTime() + 2 * 86400000, fixed]];
```

### 3. Cumulative cost comparison

```yaml
type: custom:apexcharts-card
header: {show: true, title: Cumulative cost – last 30 days, show_states: true}
graph_span: 30d
series:
  - entity: sensor.dynamic_energy_tariff_compare_dynamic_total_cost
    name: Dynamic
    statistics: {type: state, period: hour}
  - entity: sensor.dynamic_energy_tariff_compare_fixed_total_cost
    name: Fixed
    statistics: {type: state, period: hour}
  - entity: sensor.dynamic_energy_tariff_compare_savings
    name: Savings
    statistics: {type: state, period: hour}
```

### 4. Monthly bars via `utility_meter`

```yaml
# configuration.yaml – see examples/utility_meter.yaml for the full set (incl. daily)
utility_meter:
  dynamic_energy_tariff_compare_dynamic_monthly:
    name: Dynamic Energy Tariff Compare dynamic monthly
    source: sensor.dynamic_energy_tariff_compare_dynamic_total_cost
    cycle: monthly
  dynamic_energy_tariff_compare_fixed_monthly:
    name: Dynamic Energy Tariff Compare Fixed monthly
    source: sensor.dynamic_energy_tariff_compare_fixed_total_cost
    cycle: monthly
  dynamic_energy_tariff_compare_savings_monthly:
    name: Dynamic Energy Tariff Compare Savings monthly
    source: sensor.dynamic_energy_tariff_compare_savings
    cycle: monthly
    net_consumption: true   # savings can go down
```

```yaml
type: custom:apexcharts-card
header: {show: true, title: Monthly cost – dynamic vs. fixed}
graph_span: 365d
span: {end: month}
series:
  - entity: sensor.dynamic_energy_tariff_compare_dynamic_monthly
    name: Dynamic
    type: column
    statistics: {type: change, period: month, align: start}
  - entity: sensor.dynamic_energy_tariff_compare_fixed_monthly
    name: Fixed
    type: column
    statistics: {type: change, period: month, align: start}
```

The utility meters also give you "this month so far" values for tile cards
(`sensor.dynamic_energy_tariff_compare_savings_monthly`, …).

## Known limitations

* This is an estimate, not a bill. Providers bill from their own meter data and may have
  extra components (bonuses, price caps, minimum terms).
* Only grid **import** is compared. Feed-in compensation is not included. Use the 1.8.0 sensor.
* Reduced grid fees under §14a EnWG (time-variable Netzentgelte, Modul 3) are not modelled.
  Use an average grid fee.
* If a slot's price is missing (API down, price sensor unavailable), its consumption waits up
  to 3 days in *pending energy*. Until then it is excluded from savings on both sides.
* Meter resets or replacements: negative jumps are ignored and the new value becomes the
  baseline. Consumption while the meter sensor is unavailable is spread evenly over the gap.
* Changed tariff values are not applied retroactively (use reset + backfill).
* Version shown for a manual git checkout is the manifest placeholder; releases carry the
  real version (see [CONTRIBUTING.md](CONTRIBUTING.md)).

## Contributing & releases

See [CONTRIBUTING.md](CONTRIBUTING.md). Every merged PR is released automatically.

Price data from energy-charts.info: Bundesnetzagentur | SMARD.de, licensed CC BY 4.0.
