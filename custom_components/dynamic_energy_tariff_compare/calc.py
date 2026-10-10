"""Pure calculation logic for Dynamic Energy Tariff Compare (no Home Assistant imports).

All timestamps handled here are timezone-aware datetimes or POSIX seconds (UTC).
Slots are 15-minute market time units (MTU) aligned to UTC quarter hours, which
coincide with Europe/Berlin quarter hours.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone, tzinfo
from bisect import bisect_left, bisect_right
import math
from typing import Any, Iterable

SLOT_SECONDS = 900
SECONDS_PER_MONTH = 365.25 / 12 * 86400

# ---------------------------------------------------------------------------
# Time / slot helpers
# ---------------------------------------------------------------------------


def to_ts(value: Any, tz: tzinfo | None = None) -> float | None:
    """Convert datetime / ISO string / number to POSIX seconds.

    Naive datetimes/strings are interpreted in `tz` (default UTC); price lists
    without an offset (e.g. "2026-10-10 14:00:00") are usually local time.
    """
    if value is None or isinstance(value, bool):
        return None
    naive_tz = tz or timezone.utc
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=naive_tz)
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
            dt = dt.replace(tzinfo=naive_tz)
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


_CENT_TOKENS = ("ct", "c", "¢", "cent", "cents", "eurct", "ceur", "c€", "€ct", "eurocent", "eurocents")
_EUR_TOKENS = ("eur", "€", "euro", "euros", "e")
_ENERGY_FACTORS = {"kwh": 1.0, "mwh": 1 / 1000.0, "wh": 1000.0}


def price_factor_to_ct_kwh(unit: Any) -> float | None:
    """Return the factor that converts a price in `unit` to ct/kWh, or None.

    Accepts e.g. "EUR/kWh", "€/kWh", "EUR / kWh", "ct/kWh", "Cent/kWh",
    "c/kWh", "€ct/kWh", "EUR/MWh", "€/MWh" and Wh variants. Never raises.
    """
    if not isinstance(unit, str):
        return None
    u = "".join(unit.split()).lower()
    if u.count("/") != 1:
        return None
    money, energy = u.split("/")
    energy_factor = _ENERGY_FACTORS.get(energy)
    if energy_factor is None:
        return None
    if money in _CENT_TOKENS:
        m = 1.0
    elif money in _EUR_TOKENS:
        m = 100.0
    else:
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

_START_KEYS = (
    "start_time", "start", "startsAt", "starts_at", "startTime", "start_at", "valid_from",
    "from", "time", "timestamp", "datetime", "date_time", "begin", "period_start",
)
_END_KEYS = ("end_time", "end", "endsAt", "ends_at", "endTime", "end_at", "valid_to", "to", "till", "until")
# key -> fixed unit (None = use the sensor's unit / override / heuristic).
# Gross / all-in values first: the price sensor is the final price.
_VALUE_KEYS: tuple[tuple[str, str | None], ...] = (
    ("price_ct_per_kwh", "ct/kWh"),
    ("price_eur_per_mwh", "EUR/MWh"),
    ("priceIncludingVat", None),
    ("price_including_vat", None),
    ("price_incl_vat", None),
    ("value_inc_vat", None),
    ("total_price", None),
    ("totalPrice", None),
    ("price_per_kwh", None),
    ("total", None),
    ("price", None),
    ("value", None),
    ("energy", None),
    ("cost", None),
    ("rate", None),
)
# Attributes that are scanned first; any other attribute holding a list of
# dicts with a time-like and a price-like key is scanned afterwards.
_LIST_ATTRS = (
    "data",  # EPEX Spot (mampfes)
    "raw_today",  # custom nordpool
    "raw_tomorrow",
    "prices",  # Tibber Pricing DE (cyberjunky) and others
    "today",
    "tomorrow",
    "prices_today",
    "prices_tomorrow",
    "forecast",
)
MAX_ITEM_SECONDS = 3600  # longest interval a single list entry may cover


PRICE_UNIT_AUTO = "auto"
# option key -> unit (keys must be translation-key slugs)
PRICE_UNIT_OPTIONS: dict[str, str | None] = {
    PRICE_UNIT_AUTO: None,
    "eur_kwh": "EUR/kWh",
    "ct_kwh": "ct/kWh",
    "eur_mwh": "EUR/MWh",
}
PRICE_UNIT_CHOICES = tuple(PRICE_UNIT_OPTIONS)


def override_factor(choice: Any) -> float | None:
    """Factor for a manually selected price unit; None for auto-detect."""
    if not isinstance(choice, str) or choice == PRICE_UNIT_AUTO:
        return None
    return price_factor_to_ct_kwh(PRICE_UNIT_OPTIONS.get(choice, choice))


def state_price_factor(unit: Any, value: float | None, override: float | None = None) -> tuple[float | None, bool]:
    """Factor for a sensor state: manual unit > detected unit > heuristic.

    Returns (factor, guessed).
    """
    if override is not None:
        return override, False
    factor = price_factor_to_ct_kwh(unit)
    if factor is not None:
        return factor, False
    if value is None:
        return None, False
    return guess_price_factor([value]), True


def guess_price_factor(values: Iterable[float]) -> float | None:
    """Plausibility heuristic for prices without a usable unit.

    Typical all-in prices are 0.05-1 EUR/kWh, 5-100 ct/kWh or 50-1000 EUR/MWh.
    Median |value| < 2 -> EUR/kWh, 2..200 -> ct/kWh, > 200 -> EUR/MWh.
    Returns the factor to ct/kWh, or None if there is no value.
    """
    vals = sorted(abs(float(v)) for v in values if v is not None and math.isfinite(float(v)))
    if not vals:
        return None
    median = vals[len(vals) // 2]
    if median < 2:
        return 100.0
    if median <= 200:
        return 1.0
    return 0.1


def _item_start(item: dict[str, Any], tz: tzinfo | None) -> float | None:
    for key in _START_KEYS:
        if key in item:
            ts = to_ts(item[key], tz)
            if ts is not None:
                return ts
    # Tibber API style: {"date": "2026-10-10", "hour": 14, "minute": 15}
    if "date" in item and "hour" in item:
        try:
            day = datetime.fromisoformat(str(item["date"]))
            dt = day.replace(tzinfo=None) + timedelta(
                hours=int(item["hour"]), minutes=int(item.get("minute") or 0)
            )
        except (TypeError, ValueError):
            return None
        return to_ts(dt, tz)
    if "date" in item:
        return to_ts(item["date"], tz)
    return None


def _item_value(item: dict[str, Any]) -> tuple[float, str | None] | None:
    for vkey, vunit in _VALUE_KEYS:
        if vkey in item and item[vkey] is not None and not isinstance(item[vkey], bool):
            try:
                val = float(item[vkey])
            except (TypeError, ValueError):
                continue
            if math.isfinite(val):
                return val, vunit
    return None


def _looks_like_price_list(items: Any) -> bool:
    if not isinstance(items, list) or not items:
        return False
    first = next((i for i in items if isinstance(i, dict)), None)
    if first is None:
        return False
    has_time = any(k in first for k in _START_KEYS) or ("date" in first and "hour" in first)
    return has_time and _item_value(first) is not None


def _expand(start: float, end: float, ct: float, out: dict[int, float]) -> None:
    s = slot_start(start)
    if end - start <= SLOT_SECONDS:
        out[s] = ct
        return
    t = s
    while t < end:
        out[t] = ct
        t += SLOT_SECONDS


def parse_price_attributes(
    attrs: dict[str, Any] | None,
    sensor_unit: str | None,
    tz: tzinfo | None = None,
    factor_override: float | None = None,
) -> dict[int, float]:
    """Extract a {slot_start: ct/kWh} map from a price sensor's attributes.

    Supported layouts (and anything similar):
      * EPEX Spot: data=[{start_time, end_time, price_per_kwh|price_ct_per_kwh|price_eur_per_mwh}]
      * Nord Pool custom: raw_today/raw_tomorrow=[{start, end, value}]
      * Tibber-like: today/tomorrow=[{startsAt, total}]
      * Tibber Pricing DE: prices=[{timestamp: "2026-10-10 14:00:00" (local), price}]
      * Tibber API style: [{date, hour, minute, priceIncludingVat}]
    Entries without an end last until the next entry (max. 1 h), so hourly
    lists are expanded to four 15-minute slots. Naive times are read in `tz`.
    Unit: factor_override > sensor/attribute unit > plausibility heuristic.
    Never raises on odd data.
    """
    out: dict[int, float] = {}
    if not isinstance(attrs, dict) or not attrs:
        return out
    factor = factor_override
    if factor is None:
        factor = price_factor_to_ct_kwh(sensor_unit)
    if factor is None:
        attr_unit = attrs.get("unit_of_measurement") or attrs.get("unit")
        factor = price_factor_to_ct_kwh(attr_unit)
    # custom nordpool: price_in_cents flag + currency + unit 'kWh'
    if factor is None and "price_in_cents" in attrs:
        energy_unit = str(attrs.get("unit", "kWh"))
        base = "ct" if attrs.get("price_in_cents") else "EUR"
        factor = price_factor_to_ct_kwh(f"{base}/{energy_unit}")

    keys = [k for k in _LIST_ATTRS if k in attrs]
    keys += [k for k in attrs if k not in _LIST_ATTRS and _looks_like_price_list(attrs[k])]

    # (start, end|None, value, fixed unit)
    entries: list[tuple[float, float | None, float, str | None]] = []
    for key in keys:
        items = attrs.get(key)
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            try:
                start = _item_start(item, tz)
                if start is None:
                    continue
                end = next((to_ts(item[k], tz) for k in _END_KEYS if k in item), None)
                value = _item_value(item)
            except Exception:  # noqa: BLE001 - tolerate odd entries
                continue
            if value is None:
                continue
            entries.append((start, end, value[0], value[1]))
    if not entries:
        return out

    if factor is None:
        factor = guess_price_factor(v for _, _, v, u in entries if u is None)

    entries.sort(key=lambda e: e[0])
    starts = [e[0] for e in entries]
    gaps = sorted(b - a for a, b in zip(starts, starts[1:], strict=False) if b > a)
    typical = min(gaps[len(gaps) // 2], MAX_ITEM_SECONDS) if gaps else MAX_ITEM_SECONDS
    for i, (start, end, val, vunit) in enumerate(entries):
        f = price_factor_to_ct_kwh(vunit) if vunit else factor
        if f is None:
            continue
        if end is None or end <= start:
            nxt = next((s for s in starts[i + 1 :] if s > start), None)
            end = start + typical
            if nxt is not None and nxt - start <= MAX_ITEM_SECONDS:
                end = nxt
        end = min(end, start + MAX_ITEM_SECONDS)
        _expand(start, end, val * f, out)
    return out


# ---------------------------------------------------------------------------
# Tibber Germany price overview (tibber.com/de/api/lookup/price-overview)
# ---------------------------------------------------------------------------


@dataclass
class TibberOverview:
    """Parsed Tibber price overview for one postcode."""

    prices: dict[int, float] = field(default_factory=dict)  # slot -> ct/kWh all-in incl. VAT
    components: dict[int, dict[str, float]] = field(default_factory=dict)  # slot -> type -> ct/kWh
    monthly_fee_eur: float | None = None  # total monthly fees incl. VAT
    monthly_components: dict[str, float] = field(default_factory=dict)  # type -> EUR/month
    tomorrow_available: bool = False


def _tibber_entries(energy: dict[str, Any], day: str) -> list[dict[str, Any]]:
    quarter = energy.get(f"{day}QuarterHours")
    if isinstance(quarter, list) and quarter:
        return [dict(e, _len=SLOT_SECONDS) for e in quarter if isinstance(e, dict)]
    hours = energy.get(f"{day}Hours")
    if isinstance(hours, list):
        return [dict(e, _len=3600) for e in hours if isinstance(e, dict)]
    return []


def parse_tibber_overview(payload: Any, tz: tzinfo) -> TibberOverview:
    """Parse the price-overview payload. Never raises on odd data.

    Entries carry local `date`, `hour`, `minute`; `priceIncludingVat` (EUR/kWh)
    is power (spot incl. VAT, no markup) + taxes/levies + grid fee. Entries
    without a `power` component are placeholders (spot not published yet) and
    are skipped. Repeated local times (DST end) get fold=1.
    """
    out = TibberOverview()
    if not isinstance(payload, dict):
        return out
    energy = payload.get("energy") if isinstance(payload.get("energy"), dict) else {}
    for day in ("yesterday", "today", "tomorrow"):
        seen: set[tuple[str, int, int]] = set()
        for e in _tibber_entries(energy, day):
            try:
                comps = {
                    str(c["type"]): float(c["priceIncludingVat"]) * 100.0
                    for c in e.get("priceComponents") or []
                    if isinstance(c, dict) and c.get("priceIncludingVat") is not None
                }
                if "power" not in comps:
                    continue  # placeholder: spot price not known yet
                key = (str(e["date"]), int(e["hour"]), int(e.get("minute") or 0))
                d = datetime.fromisoformat(key[0])
                local = datetime(
                    d.year, d.month, d.day, key[1], key[2], tzinfo=tz, fold=1 if key in seen else 0
                )
                seen.add(key)
                ct = float(e["priceIncludingVat"]) * 100.0
            except (KeyError, TypeError, ValueError):
                continue
            start = local.timestamp()
            t = slot_start(start)
            while t < start + e["_len"]:
                out.prices[t] = ct
                out.components[t] = comps
                t += SLOT_SECONDS
            if day == "tomorrow":
                out.tomorrow_available = True
    fees = payload.get("monthlyFees")
    if isinstance(fees, dict):
        try:
            out.monthly_fee_eur = float(fees["priceIncludingVat"])
        except (KeyError, TypeError, ValueError):
            out.monthly_fee_eur = None
        for c in fees.get("priceComponents") or []:
            try:
                out.monthly_components[str(c["type"])] = float(c["priceIncludingVat"])
            except (KeyError, TypeError, ValueError):
                continue
    return out


def valid_postcode(value: Any) -> str | None:
    """Return a normalised German postcode (5 digits) or None."""
    txt = "".join(str(value or "").split())
    return txt if len(txt) == 5 and txt.isdigit() else None


# ---------------------------------------------------------------------------
# Tariff
# ---------------------------------------------------------------------------


@dataclass
class TariffParams:
    """Tariff parameters (gross).

    The dynamic price comes from the selected price sensor and is always taken
    as the final all-in price per kWh (incl. grid fees, levies, VAT, markup).
    """

    fixed_price_ct: float = 32.0  # fixed tariff energy price
    fixed_base_fee_eur_month: float = 12.0  # fixed tariff base fee
    provider_monthly_fee_eur: float = 5.99  # dynamic tariff monthly fee


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

        price_lookup(slot_start) -> ct/kWh (final all-in price) | None
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
        ct = price_lookup(slot)
        if ct is None:
            return False
        t = self.totals
        t.priced_energy_kwh += kwh
        t.dynamic_energy_cost_eur += kwh * float(ct) / 100.0
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
        return self.totals.dynamic_energy_cost_eur + months * p.provider_monthly_fee_eur

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
    live.backfilled_kwh += back.energy_kwh
    if live.tracking_start is None or backfill_start < live.tracking_start:
        live.tracking_start = backfill_start
