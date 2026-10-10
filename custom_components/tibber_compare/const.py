"""Constants for Tibber Compare."""

from __future__ import annotations

DOMAIN = "tibber_compare"
STORAGE_VERSION = 1

CONF_ENERGY_ENTITY = "energy_entity"
CONF_PRICE_ENTITY = "price_entity"
CONF_PRICE_IS_FINAL = "price_is_final"
CONF_USE_ENERGY_CHARTS = "use_energy_charts"
CONF_FIXED_PRICE = "fixed_price_ct"
CONF_FIXED_BASE_FEE = "fixed_base_fee_eur_month"
CONF_TIBBER_MARKUP = "tibber_markup_ct"
CONF_NETWORK_FEE = "network_fee_ct"
CONF_LEVIES = "levies_ct"
CONF_VAT = "vat_percent"
CONF_TIBBER_MONTHLY_FEE = "tibber_monthly_fee_eur"
CONF_GRID_BASE_FEE = "grid_base_fee_eur_month"

# Defaults (Germany, 2026, gross incl. 19 % VAT). The user MUST verify these
# for their postcode on tibber.com / their grid operator's price sheet.
DEFAULTS: dict[str, float | bool] = {
    CONF_PRICE_IS_FINAL: False,
    CONF_USE_ENERGY_CHARTS: True,
    CONF_FIXED_PRICE: 32.0,
    CONF_FIXED_BASE_FEE: 12.0,
    CONF_TIBBER_MARKUP: 2.15,
    CONF_NETWORK_FEE: 9.0,
    CONF_LEVIES: 7.52,
    CONF_VAT: 19.0,
    CONF_TIBBER_MONTHLY_FEE: 5.99,
    CONF_GRID_BASE_FEE: 8.28,
}

NUMBER_KEYS = (
    CONF_FIXED_PRICE,
    CONF_FIXED_BASE_FEE,
    CONF_TIBBER_MARKUP,
    CONF_NETWORK_FEE,
    CONF_LEVIES,
    CONF_VAT,
    CONF_TIBBER_MONTHLY_FEE,
    CONF_GRID_BASE_FEE,
)

ENERGY_CHARTS_URL = "https://api.energy-charts.info/price"
BIDDING_ZONE = "DE-LU"

SIGNAL_UPDATE = f"{DOMAIN}_update_{{}}"

SERVICE_BACKFILL = "backfill"
SERVICE_RESET = "reset"
ATTR_DAYS = "days"
ATTR_ENTRY_ID = "entry_id"
DEFAULT_BACKFILL_DAYS = 30
