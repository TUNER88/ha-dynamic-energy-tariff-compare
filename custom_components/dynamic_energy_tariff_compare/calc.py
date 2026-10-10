"""Pure calculation logic for Dynamic Energy Tariff Compare (no Home Assistant imports).

All timestamps handled here are timezone-aware datetimes or POSIX seconds (UTC).
Slots are 15-minute market time units (MTU) aligned to UTC quarter hours, which
coincide with Europe/Berlin quarter hours.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from bisect import bisect_left, bisect_right
import math
from typing import Any, Iterable

SLOT_SECONDS = 900
SECONDS_PER_MONTH = 365.25 / 12 * 86400

# ---------------------------------------------------------------------------
# Time / slot helpers
# ---------------------------------------------------------------------------


def to_ts(value: Any) -> float | None:
    """Convert datetime / ISO string / number to POSIX seconds."""
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.timestamp()
    if isinstance(value, (int, float)):
        # Heuristic: milliseconds
        return float(value) / 1000 if value > 1e11 else float(value)
    if isinstance(value, str):
        txt = value.strip()
        if not txt:
            return None
        if txt.endswith("Z"):
            txt = txt[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(txt)
        except ValueError:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    return None


def slot_start(ts: float) -> int:
    """Return the start (POSIX seconds) of the 15-min slot containing ts."""
    return int(math.floor(ts / SLOT_SECONDS) * SLOT_SECONDS)


def split_interval(start_ts: float, end_ts: float, amount: float) -> list[tuple[int, float]]:
    """Split `amount` proportionally over the 15-min slots of [start_ts, end_ts).

    Returns list of (slot_start, share). If start == end, the whole amount goes
    into the slot of end_ts.
    """
    if amount == 0:
        return []
    if end_ts <= start_ts:
        return [(slot_start(end_ts), amount)]
    total = end_ts - start_ts
    result: list[tuple[int, float]] = []
    cur = start_ts
    while cur < end_ts:
        s = slot_start(cur)
        nxt = min(s + SLOT_SECONDS, end_ts)
        share = amount * (nxt - cur) / total
        if share:
            result.append((s, share))
        cur = nxt
    return result


# ---------------------------------------------------------------------------
# Units
# ---------------------------------------------------------------------------


def price_factor_to_ct_kwh(unit: str | None) -> float | None:
    """Return factor that converts a price in `unit` to ct/kWh.

    Supports EUR/kWh, €/kWh, ct/kWh, Cent/kWh, EUR/MWh, €/MWh, ct/Wh, EUR/Wh.
    Returns None if the unit is unknown/empty.
    """
    if not unit:
        return None
    u = unit.strip().lower().replace(" ", "").replace("€", "eur")
    u = u.replace("cent", "ct").replace("cts", "ct").replace("euro", "eur")
    if "/" not in u:
        return None
    money, energy = u.split("/", 1)
    if money in ("ct", "c", "¢"):
        m = 1.0
    elif money in ("eur", "e"):
        m = 100.0
    else:
        return None
    energy_factor = {"kwh": 1.0, "mwh": 1 / 1000.0, "wh": 1000.0}.get(energy)
    if energy_factor is None:
        return None
    return m * energy_factor


def energy_factor_to_kwh(unit: str | None) -> float:
    """Factor converting an energy reading to kWh (default kWh)."""
    if not unit:
        return 1.0
    u = unit.strip().lower()
    return {"wh": 0.001, "kwh": 1.0, "mwh": 1000.0, "gwh": 1e6}.get(u, 1.0)


# ---------------------------------------------------------------------------
# Price list parsing from sensor attributes
# ---------------------------------------------------------------------------

_START_KEYS = ("start_time", "start", "startsAt", "starts_at", "time", "from", "datetime")
_END_KEYS = ("end_time", "end", "endsAt", "ends_at", "to", "till")
# key -> fixed unit (None = use sensor unit)
_VALUE_KEYS: tuple[tuple[str, str | None], ...] = (
    ("price_ct_per_kwh", "ct/kWh"),
    ("price_eur_per_mwh", "EUR/MWh"),
    ("price_per_kwh", None),
    ("total", None),
    ("price", None),
    ("value", None),
    ("energy", None),
)
_LIST_ATTRS = (
    "data",  # EPEX Spot (mampfes)
    "raw_today",  # custom nordpool
    "raw_tomorrow",
    "today",  # Tibber-style lists / others
    "tomorrow",
    "prices",
    "prices_today",
    "prices_tomorrow",
    "forecast",
)


def _expand(start: float, end: float | None, ct: float, out: dict[int, float]) -> None:
    s = slot_start(start)
    if end is None or end - start <= SLOT_SECONDS:
        out[s] = ct
        return
    t = s
    while t < end:
        out[t] = ct
        t += SLOT_SECONDS


def parse_price_attributes(attrs: dict[str, Any] | None, sensor_unit: str | None) -> dict[int, float]:
    """Extract a {slot_start: ct/kWh} map from a price sensor's attributes.

    Supported layouts:
      * EPEX Spot: data=[{start_time, end_time, price_per_kwh|price_ct_per_kwh|price_eur_per_mwh}]
      * Nord Pool custom: raw_today/raw_tomorrow=[{start, end, value}]
      * Tibber-like: today/tomorrow=[{startsAt, total}]
      * energy-charts style: unix_seconds=[...], price=[...] (unit attr or EUR/MWh)
    Hourly entries are expanded to four 15-minute slots.
    """
    out: dict[int, float] = {}
    if not attrs:
        return out
    sensor_factor = price_factor_to_ct_kwh(sensor_unit)
    attr_unit = attrs.get("unit_of_measurement") or attrs.get("unit")
    if isinstance(attr_unit, str):
        f = price_factor_to_ct_kwh(attr_unit)
        if f is not None and sensor_factor is None:
            sensor_factor = f
    # custom nordpool: price_in_cents flag + currency + unit 'kWh'
    if sensor_factor is None and "price_in_cents" in attrs:
        energy_unit = str(attrs.get("unit", "kWh"))
        base = "ct" if attrs.get("price_in_cents") else "EUR"
        sensor_factor = price_factor_to_ct_kwh(f"{base}/{energy_unit}")

    # energy-charts style parallel arrays
    if isinstance(attrs.get("unix_seconds"), list) and isinstance(attrs.get("price"), list):
        factor = price_factor_to_ct_kwh(str(attrs.get("unit") or "EUR/MWh")) or 0.1
        for ts, p in zip(attrs["unix_seconds"], attrs["price"], strict=False):
            if p is None:
                continue
            out[slot_start(float(ts))] = float(p) * factor
        return out

    for key in _LIST_ATTRS:
        items = attrs.get(key)
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            start = next((to_ts(item[k]) for k in _START_KEYS if k in item), None)
            if start is None:
                continue
            end = next((to_ts(item[k]) for k in _END_KEYS if k in item), None)
            ct = None
            for vkey, vunit in _VALUE_KEYS:
                if vkey in item and item[vkey] is not None:
                    try:
                        val = float(item[vkey])
                    except (TypeError, ValueError):
                        continue
                    factor = price_factor_to_ct_kwh(vunit) if vunit else sensor_factor
                    if factor is None:
                        factor = 100.0  # assume EUR/kWh
                    ct = val * factor
                    break
            if ct is not None:
                _expand(start, end, ct, out)
    return out


def parse_energy_charts(payload: dict[str, Any]) -> dict[int, float]:
    """Parse api.energy-charts.info /price payload into {slot: ct/kWh}."""
    out: dict[int, float] = {}
    secs = payload.get("unix_seconds") or []
    prices = payload.get("price") or []
    factor = price_factor_to_ct_kwh(str(payload.get("unit") or "EUR/MWh").replace(" ", "")) or 0.1
    # Resolution is derived per point (15 min since 2025-10-01, 60 min before)
    for i, (ts, p) in enumerate(zip(secs, prices, strict=False)):
        if p is None:
            continue
        if i + 1 < len(secs):
            step = secs[i + 1] - ts
        elif i > 0:
            step = ts - secs[i - 1]
        else:
            step = SLOT_SECONDS
        step = min(max(step, SLOT_SECONDS), 3600)
        _expand(float(ts), float(ts) + step, float(p) * factor, out)
    return out


# ---------------------------------------------------------------------------
# Tariff
# ---------------------------------------------------------------------------


@dataclass
class TariffParams:
    """Tariff parameters. All ct/kWh values are GROSS (incl. 19 % VAT) unless noted."""

    fixed_price_ct: float = 32.0  # current tariff Arbeitspreis, gross
    fixed_base_fee_eur_month: float = 12.0  # current tariff Grundpreis, gross
    provider_markup_ct: float = 2.15  # provider markup on the spot price, gross (example value)
    network_fee_ct: float = 9.0  # Netzentgelt Arbeitspreis (+ Messstelle var.), gross
    levies_ct: float = 7.5  # Stromsteuer + Konzessionsabgabe + Umlagen, gross
    vat_percent: float = 19.0  # applied to the raw spot price
    provider_monthly_fee_eur: float = 5.99  # gross
    grid_base_fee_eur_month: float = 8.0  # Netz-Grundpreis + Messstellenbetrieb, gross
    price_is_final: bool = False  # price source already all-in (no surcharges)


def effective_price_ct(spot_ct: float, params: TariffParams, is_final: bool | None = None) -> float:
    """Effective dynamic-tariff energy price in ct/kWh gross for one slot.

    raw:   spot_net * (1 + VAT) + markup + network fee + levies   (all add-ons gross)
    final: price as delivered by the source
    """
    final = params.price_is_final if is_final is None else is_final
    if final:
        return spot_ct
    return (
        spot_ct * (1 + params.vat_percent / 100.0)
        + params.provider_markup_ct
        + params.network_fee_ct
        + params.levies_ct
    )


# ---------------------------------------------------------------------------
# Accumulator
# ---------------------------------------------------------------------------


@dataclass
class Totals:
    """Persisted running totals."""

    energy_kwh: float = 0.0
    priced_energy_kwh: float = 0.0
    dynamic_energy_cost_eur: float = 0.0
    fixed_energy_cost_eur: float = 0.0
    spot_weighted_ct_kwh_sum: float = 0.0  # sum(kWh * spot ct) for priced energy
    tracking_start: float | None = None  # POSIX seconds; earliest covered time
    live_start: float | None = None  # when live tracking began (backfill end)
    last_value: float | None = None  # last meter reading (kWh)
    last_ts: float | None = None
    pending: list[list[float]] = field(default_factory=list)  # [[slot, kwh], ...]
    backfilled_kwh: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "Totals":
        if not data:
            return cls()
        known = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        return cls(**known)


MAX_PENDING_AGE = 3 * 86400


class Accumulator:
    """Attributes meter deltas to slots and accumulates costs."""

    def __init__(self, params: TariffParams, totals: Totals | None = None) -> None:
        self.params = params
        self.totals = totals or Totals()

    # -- meter handling ---------------------------------------------------
    def process_reading(
        self,
        value_kwh: float,
        ts: float,
        price_lookup,
    ) -> float:
        """Process a new meter reading. Returns the kWh attributed (0 if none).

        price_lookup(slot_start) -> (ct_kwh, is_final) | None
        """
        t = self.totals
        if t.tracking_start is None:
            t.tracking_start = ts
        if t.live_start is None:
            t.live_start = ts
        if t.last_value is None or t.last_ts is None:
            t.last_value, t.last_ts = value_kwh, ts
            return 0.0
        delta = value_kwh - t.last_value
        prev_ts = t.last_ts
        if delta < 0:
            # Meter reset / replaced / glitch: re-baseline, ignore negative delta
            t.last_value, t.last_ts = value_kwh, ts
            return 0.0
        t.last_value, t.last_ts = value_kwh, ts
        if delta == 0:
            return 0.0
        self.add_energy(split_interval(prev_ts, ts, delta), price_lookup)
        return delta

    def add_energy(self, parts: Iterable[tuple[int, float]], price_lookup) -> None:
        """Add (slot, kWh) parts; unpriced parts are queued as pending."""
        t = self.totals
        for slot, kwh in parts:
            if kwh <= 0:
                continue
            t.energy_kwh += kwh
            t.fixed_energy_cost_eur += kwh * self.params.fixed_price_ct / 100.0
            if not self._price(slot, kwh, price_lookup):
                t.pending.append([slot, kwh])

    def _price(self, slot: int, kwh: float, price_lookup) -> bool:
        res = price_lookup(slot)
        if res is None:
            return False
        ct, is_final = res
        eff = effective_price_ct(ct, self.params, is_final)
        t = self.totals
        t.priced_energy_kwh += kwh
        t.dynamic_energy_cost_eur += kwh * eff / 100.0
        if not is_final:
            t.spot_weighted_ct_kwh_sum += kwh * ct
        return True

    def resolve_pending(self, price_lookup, now_ts: float | None = None) -> int:
        """Try to price pending energy. Returns number of resolved entries.

        Entries older than MAX_PENDING_AGE are priced with the nearest known
        price via price_lookup fallback or dropped from dynamic-tariff costing.
        """
        t = self.totals
        still: list[list[float]] = []
        resolved = 0
        for slot, kwh in t.pending:
            if self._price(int(slot), kwh, price_lookup):
                resolved += 1
            elif now_ts is not None and now_ts - slot > MAX_PENDING_AGE:
                continue  # give up
            else:
                still.append([slot, kwh])
        t.pending = still
        return resolved

    # -- derived values -----------------------------------------------------
    @property
    def pending_kwh(self) -> float:
        return sum(k for _, k in self.totals.pending)

    def elapsed_months(self, now_ts: float) -> float:
        start = self.totals.tracking_start
        if start is None:
            return 0.0
        return max(0.0, now_ts - start) / SECONDS_PER_MONTH

    def dynamic_total_cost(self, now_ts: float) -> float:
        p = self.params
        months = self.elapsed_months(now_ts)
        return self.totals.dynamic_energy_cost_eur + months * (
            p.provider_monthly_fee_eur + p.grid_base_fee_eur_month
        )

    def fixed_total_cost(self, now_ts: float) -> float:
        months = self.elapsed_months(now_ts)
        return self.totals.fixed_energy_cost_eur + months * self.params.fixed_base_fee_eur_month

    def savings(self, now_ts: float) -> float:
        """Positive = the dynamic tariff would have been cheaper.

        Fixed-tariff energy cost of still-pending (unpriced) energy is excluded
        so both sides compare the same kWh.
        """
        pending_fixed = self.pending_kwh * self.params.fixed_price_ct / 100.0
        return (self.fixed_total_cost(now_ts) - pending_fixed) - self.dynamic_total_cost(now_ts)

    def avg_dynamic_price_ct(self) -> float | None:
        t = self.totals
        if t.priced_energy_kwh <= 0:
            return None
        return t.dynamic_energy_cost_eur / t.priced_energy_kwh * 100.0

    def avg_dynamic_price_incl_fees_ct(self, now_ts: float) -> float | None:
        t = self.totals
        if t.priced_energy_kwh <= 0:
            return None
        return self.dynamic_total_cost(now_ts) / t.priced_energy_kwh * 100.0

    def avg_fixed_price_incl_fees_ct(self, now_ts: float) -> float | None:
        t = self.totals
        if t.energy_kwh <= 0:
            return None
        return self.fixed_total_cost(now_ts) / t.energy_kwh * 100.0


# ---------------------------------------------------------------------------
# Backfill helpers
# ---------------------------------------------------------------------------


def price_map_from_state_timeline(
    timeline: list[tuple[float, float]], start_ts: float, end_ts: float
) -> dict[int, float]:
    """Build {slot: ct} from a step function [(ts, ct), ...].

    Each slot gets the time-weighted average of the step function over the
    part of the slot where a value is known.
    """
    out: dict[int, float] = {}
    if not timeline:
        return out
    timeline = sorted(timeline)
    times = [t for t, _ in timeline]
    s = slot_start(start_ts)
    while s < end_ts:
        e = s + SLOT_SECONDS
        lo = bisect_right(times, s)
        hi = bisect_left(times, e)
        bounds = [s, *times[lo:hi], e]
        acc = covered = 0.0
        for a, b in zip(bounds, bounds[1:], strict=False):
            if b <= a:
                continue
            i = bisect_right(times, a) - 1
            if i < 0:
                continue
            acc += timeline[i][1] * (b - a)
            covered += b - a
        if covered > 0:
            out[s] = acc / covered
        s = e
    return out


def backfill_energy(
    buckets: list[tuple[float, float, float]],
    price_lookup,
    params: TariffParams,
) -> Totals:
    """Compute totals for historic energy buckets [(start_ts, end_ts, kWh)].

    Buckets are split proportionally into 15-min slots. Energy without price
    goes to pending (and is reported, not priced).
    """
    acc = Accumulator(params, Totals())
    for start, end, kwh in buckets:
        if kwh is None or kwh <= 0:
            continue
        acc.add_energy(split_interval(start, end, kwh), price_lookup)
    # Energy without any known price is excluded from BOTH sides so the
    # comparison stays apples-to-apples; it is kept in `pending` for reporting.
    t = acc.totals
    unpriced = sum(k for _, k in t.pending)
    t.energy_kwh -= unpriced
    t.fixed_energy_cost_eur -= unpriced * params.fixed_price_ct / 100.0
    return t


def merge_backfill(live: Totals, back: Totals, backfill_start: float) -> None:
    """Merge backfill totals into live totals (in place)."""
    live.energy_kwh += back.energy_kwh
    live.priced_energy_kwh += back.priced_energy_kwh
    live.dynamic_energy_cost_eur += back.dynamic_energy_cost_eur
    live.fixed_energy_cost_eur += back.fixed_energy_cost_eur
    live.spot_weighted_ct_kwh_sum += back.spot_weighted_ct_kwh_sum
    live.backfilled_kwh += back.energy_kwh
    if live.tracking_start is None or backfill_start < live.tracking_start:
        live.tracking_start = backfill_start
