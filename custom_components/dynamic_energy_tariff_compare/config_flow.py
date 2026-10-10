"""Config and options flow for Dynamic Energy Tariff Compare.

The step handlers never raise: every problem is reported as a form error so
the frontend flow stays alive (an exception would answer the submit with an
error and leave the dialog in a broken state).
"""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant.components.sensor import SensorDeviceClass
from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.const import ATTR_UNIT_OF_MEASUREMENT, STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er, selector
from homeassistant.util import dt as dt_util

from . import calc
from .const import (
    CONF_ENERGY_ENTITY,
    CONF_FIXED_BASE_FEE,
    CONF_FIXED_PRICE,
    CONF_PRICE_ENTITY,
    CONF_PRICE_UNIT,
    CONF_PROVIDER_MONTHLY_FEE,
    CONF_PROVIDER_NAME,
    DEFAULTS,
    DOMAIN,
    NUMBER_KEYS,
)

_LOGGER = logging.getLogger(__name__)

CONFIG_VERSION = 2

_NUM_SPECS: dict[str, tuple[float, float, float, str]] = {
    CONF_FIXED_PRICE: (0, 200, 0.01, "ct/kWh"),
    CONF_FIXED_BASE_FEE: (0, 200, 0.01, "€/month"),
    CONF_PROVIDER_MONTHLY_FEE: (0, 100, 0.01, "€/month"),
}
CONFIG_KEYS = (CONF_PROVIDER_NAME, CONF_ENERGY_ENTITY, CONF_PRICE_ENTITY, CONF_PRICE_UNIT, *NUMBER_KEYS)


def _num(key: str) -> selector.NumberSelector:
    lo, hi, step, unit = _NUM_SPECS[key]
    return selector.NumberSelector(
        selector.NumberSelectorConfig(
            min=lo, max=hi, step=step, unit_of_measurement=unit,
            mode=selector.NumberSelectorMode.BOX,
        )
    )


def _safe_float(value: Any, default: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def _entity_default(value: Any) -> Any:
    return value if isinstance(value, str) and value else vol.UNDEFINED


def _unit_choice(value: Any) -> str:
    return value if value in calc.PRICE_UNIT_CHOICES else calc.PRICE_UNIT_AUTO


def build_schema(current: dict[str, Any] | None) -> vol.Schema:
    """Schema shared by config and options flow. Tolerates any stored values."""
    cfg = {**DEFAULTS, **(current or {})}
    provider = cfg.get(CONF_PROVIDER_NAME)
    schema: dict[Any, Any] = {
        vol.Optional(
            CONF_PROVIDER_NAME,
            description={"suggested_value": provider if isinstance(provider, str) and provider else None},
        ): selector.TextSelector(),
        vol.Required(
            CONF_ENERGY_ENTITY, default=_entity_default(cfg.get(CONF_ENERGY_ENTITY))
        ): selector.EntitySelector(
            selector.EntitySelectorConfig(domain="sensor", device_class=SensorDeviceClass.ENERGY)
        ),
        vol.Required(
            CONF_PRICE_ENTITY, default=_entity_default(cfg.get(CONF_PRICE_ENTITY))
        ): selector.EntitySelector(selector.EntitySelectorConfig(domain="sensor")),
        vol.Required(CONF_PRICE_UNIT, default=_unit_choice(cfg.get(CONF_PRICE_UNIT))): selector.SelectSelector(
            selector.SelectSelectorConfig(
                options=list(calc.PRICE_UNIT_CHOICES),
                translation_key=CONF_PRICE_UNIT,
                mode=selector.SelectSelectorMode.DROPDOWN,
            )
        ),
    }
    for key in NUMBER_KEYS:
        schema[vol.Required(key, default=_safe_float(cfg.get(key), DEFAULTS[key]))] = _num(key)
    return vol.Schema(schema)


def clean_input(user_input: dict[str, Any]) -> dict[str, Any]:
    """Keep only current keys, normalise types."""
    data: dict[str, Any] = {k: user_input[k] for k in CONFIG_KEYS if k in user_input}
    data[CONF_PROVIDER_NAME] = str(user_input.get(CONF_PROVIDER_NAME) or "").strip()
    data[CONF_PRICE_UNIT] = _unit_choice(user_input.get(CONF_PRICE_UNIT))
    for key in NUMBER_KEYS:
        data[key] = _safe_float(user_input.get(key), DEFAULTS[key])
    return data


def validate(hass: HomeAssistant, user_input: dict[str, Any]) -> tuple[dict[str, str], dict[str, str]]:
    """Return (errors, description_placeholders). Never raises."""
    errors: dict[str, str] = {}
    placeholders: dict[str, str] = {"state": ""}
    try:
        energy = user_input.get(CONF_ENERGY_ENTITY)
        if not energy:
            errors[CONF_ENERGY_ENTITY] = "entity_not_found"
        price_entity = user_input.get(CONF_PRICE_ENTITY)
        if not price_entity:
            errors[CONF_PRICE_ENTITY] = "price_entity_required"
            return errors, placeholders
        state = hass.states.get(price_entity)
        if state is None:
            if er.async_get(hass).async_get(price_entity) is None:
                errors[CONF_PRICE_ENTITY] = "entity_not_found"
            # registered but not (yet) providing a state: accept, it may come up later
            return errors, placeholders
        # Never block on a missing/odd unit: a manual unit, a recognised unit,
        # a parsable price list or a numeric state (plausibility heuristic) is enough.
        if calc.override_factor(user_input.get(CONF_PRICE_UNIT)) is not None:
            return errors, placeholders
        if state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            return errors, placeholders  # resolved at runtime
        try:
            float(state.state)
            return errors, placeholders
        except (TypeError, ValueError):
            pass
        try:
            has_list = bool(
                calc.parse_price_attributes(
                    dict(state.attributes),
                    state.attributes.get(ATTR_UNIT_OF_MEASUREMENT),
                    dt_util.get_default_time_zone(),
                )
            )
        except Exception:  # noqa: BLE001
            has_list = False
        if has_list:
            return errors, placeholders
        placeholders["state"] = str(state.state)[:40]
        errors[CONF_PRICE_UNIT] = "price_not_numeric"
    except Exception:  # noqa: BLE001 - never let the flow die
        _LOGGER.exception("Unexpected error while validating the configuration")
        errors["base"] = "unknown"
    return errors, placeholders


def _title(provider: str) -> str:
    return f"Dynamic Energy Tariff Compare ({provider})" if provider else "Dynamic Energy Tariff Compare"


class DynamicEnergyTariffCompareConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow."""

    VERSION = CONFIG_VERSION

    def __init__(self) -> None:
        self._finishing = False

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        placeholders: dict[str, str] = {"state": ""}
        if user_input is not None and self._finishing:
            # A second submit (double click / Enter) arrived while the first one is
            # still creating and setting up the entry. Answering it with an abort
            # ("already_configured") would remove the flow under the first request,
            # which then fails with "Invalid flow specified" although the entry was
            # created. Keep the flow alive instead; the first request finishes it.
            _LOGGER.debug("Ignoring duplicate submit of flow %s", self.flow_id)
            return self.async_show_form(
                step_id="user",
                data_schema=_schema_or_default(user_input),
                errors=errors,
                description_placeholders=placeholders,
            )
        if user_input is not None:
            errors, placeholders = validate(self.hass, user_input)
            if not errors:
                data = clean_input(user_input)
                await self.async_set_unique_id(data[CONF_ENERGY_ENTITY])
                self._abort_if_unique_id_configured()
                self._finishing = True
                return self.async_create_entry(title=_title(data[CONF_PROVIDER_NAME]), data=data)
        return self.async_show_form(
            step_id="user",
            data_schema=_schema_or_default(user_input),
            errors=errors,
            description_placeholders=placeholders,
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return DynamicEnergyTariffCompareOptionsFlow()


def _schema_or_default(current: dict[str, Any] | None) -> vol.Schema:
    try:
        return build_schema(current)
    except Exception:  # noqa: BLE001
        _LOGGER.exception("Could not build form from %s; using defaults", current)
        return build_schema({})


class DynamicEnergyTariffCompareOptionsFlow(OptionsFlow):
    """Options flow: everything (incl. sensors) can be changed."""

    def __init__(self) -> None:
        self._finishing = False

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        placeholders: dict[str, str] = {"state": ""}
        if user_input is not None and not self._finishing:
            errors, placeholders = validate(self.hass, user_input)
            if not errors:
                self._finishing = True
                return self.async_create_entry(data=clean_input(user_input))
        current = {**self.config_entry.data, **self.config_entry.options, **(user_input or {})}
        return self.async_show_form(
            step_id="init",
            data_schema=_schema_or_default(current),
            errors=errors,
            description_placeholders=placeholders,
        )
