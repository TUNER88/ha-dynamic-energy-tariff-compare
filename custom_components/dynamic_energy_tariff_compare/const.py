"""Constants for Dynamic Energy Tariff Compare."""

from __future__ import annotations

DOMAIN = "dynamic_energy_tariff_compare"
STORAGE_VERSION = 1

CONF_PROVIDER_NAME = "provider_name"
CONF_ENERGY_ENTITY = "energy_entity"
CONF_PRICE_ENTITY = "price_entity"
CONF_FIXED_PRICE = "fixed_price_ct"
CONF_FIXED_BASE_FEE = "fixed_base_fee_eur_month"
CONF_PROVIDER_MONTHLY_FEE = "provider_monthly_fee_eur"
CONF_PRICE_SOURCE = "price_source"  # PRICE_SOURCE_TIBBER_DE or PRICE_SOURCE_SENSOR
CONF_POSTCODE = "postcode"
PRICE_SOURCE_TIBBER_DE = "tibber_de"
PRICE_SOURCE_SENSOR = "sensor"
CONF_PRICE_UNIT = "price_unit"  # "auto" or a manual unit (EUR/kWh, ct/kWh, EUR/MWh)

# Options removed in config entry version 2 (0.3.0). The price sensor is now
# always treated as the final all-in price per kWh.
LEGACY_KEYS = (
    "price_is_final",
    "use_energy_charts",
    "provider_markup_ct",
    "network_fee_ct",
    "levies_ct",
    "vat_percent",
    "grid_base_fee_eur_month",
)

# Example defaults (gross). Only a starting point: users must enter the values
# of their own tariffs.
DEFAULTS: dict[str, float | str] = {
    CONF_PROVIDER_NAME: "",
    CONF_PRICE_SOURCE: "sensor",
    CONF_PRICE_UNIT: "auto",
    CONF_FIXED_PRICE: 32.0,
    CONF_FIXED_BASE_FEE: 12.0,
    CONF_PROVIDER_MONTHLY_FEE: 5.99,
}

NUMBER_KEYS = (
    CONF_FIXED_PRICE,
    CONF_FIXED_BASE_FEE,
    CONF_PROVIDER_MONTHLY_FEE,
)

# Public endpoint behind tibber.com/de's price calculator (no account needed).
# Returns yesterday/today/tomorrow per quarter hour and hour for a postcode,
# all-in incl. VAT with components (power, taxes, grid) and monthly fees.
TIBBER_DE_URL = "https://tibber.com/de/api/lookup/price-overview"

SIGNAL_UPDATE = f"{DOMAIN}_update_{{}}"

SERVICE_BACKFILL = "backfill"
SERVICE_RESET = "reset"
ATTR_DAYS = "days"
ATTR_ENTRY_ID = "entry_id"
DEFAULT_BACKFILL_DAYS = 30
