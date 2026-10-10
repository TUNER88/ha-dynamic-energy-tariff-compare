"""Config and options flow for Tibber Compare."""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.components.sensor import SensorDeviceClass
from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.const import ATTR_UNIT_OF_MEASUREMENT
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import selector

from . import calc
from .const import (
    CONF_ENERGY_ENTITY,
    CONF_FIXED_BASE_FEE,
    CONF_FIXED_PRICE,
    CONF_GRID_BASE_FEE,
    CONF_LEVIES,
    CONF_NETWORK_FEE,
    CONF_PRICE_ENTITY,
    CONF_PRICE_IS_FINAL,
    CONF_TIBBER_MARKUP,
    CONF_TIBBER_MONTHLY_FEE,
    CONF_USE_ENERGY_CHARTS,
    CONF_VAT,
    DEFAULTS,
    DOMAIN,
)

_NUM_SPECS: dict[str, tuple[float, float, float, str]] = {
    CONF_FIXED_PRICE: (0, 200, 0.01, "ct/kWh"),
    CONF_FIXED_BASE_FEE: (0, 200, 0.01, "€/month"),
    CONF_TIBBER_MARKUP: (-10, 50, 0.01, "ct/kWh"),
    CONF_NETWORK_FEE: (0, 50, 0.01, "ct/kWh"),
    CONF_LEVIES: (0, 50, 0.01, "ct/kWh"),
    CONF_VAT: (0, 50, 0.1, "%"),
    CONF_TIBBER_MONTHLY_FEE: (0, 50, 0.01, "€/month"),
    CONF_GRID_BASE_FEE: (0, 100, 0.01, "€/month"),
}


def _num(key: str) -> selector.NumberSelector:
    lo, hi, step, unit = _NUM_SPECS[key]
    return selector.NumberSelector(
        selector.NumberSelectorConfig(
            min=lo, max=hi, step=step, unit_of_measurement=unit,
            mode=selector.NumberSelectorMode.BOX,
        )
    )


def build_schema(current: dict[str, Any]) -> vol.Schema:
    """Schema shared by config and options flow."""
    cfg = {**DEFAULTS, **current}
    schema: dict[Any, Any] = {
        vol.Required(
            CONF_ENERGY_ENTITY, default=cfg.get(CONF_ENERGY_ENTITY, vol.UNDEFINED)
        ): selector.EntitySelector(
            selector.EntitySelectorConfig(domain="sensor", device_class=SensorDeviceClass.ENERGY)
        ),
        vol.Optional(
            CONF_PRICE_ENTITY,
            description={"suggested_value": cfg.get(CONF_PRICE_ENTITY)},
        ): selector.EntitySelector(selector.EntitySelectorConfig(domain="sensor")),
        vol.Required(CONF_PRICE_IS_FINAL, default=bool(cfg[CONF_PRICE_IS_FINAL])): selector.BooleanSelector(),
        vol.Required(CONF_USE_ENERGY_CHARTS, default=bool(cfg[CONF_USE_ENERGY_CHARTS])): selector.BooleanSelector(),
    }
    for key in _NUM_SPECS:
        schema[vol.Required(key, default=float(cfg[key]))] = _num(key)
    return vol.Schema(schema)


def validate(hass: HomeAssistant, user_input: dict[str, Any]) -> dict[str, str]:
    """Return errors dict."""
    errors: dict[str, str] = {}
    price_entity = user_input.get(CONF_PRICE_ENTITY)
    if price_entity:
        state = hass.states.get(price_entity)
        if state is not None:
            unit = state.attributes.get(ATTR_UNIT_OF_MEASUREMENT)
            has_list = bool(calc.parse_price_attributes(dict(state.attributes), unit))
            if calc.price_factor_to_ct_kwh(unit) is None and not has_list:
                errors[CONF_PRICE_ENTITY] = "unknown_price_unit"
    elif not user_input.get(CONF_USE_ENERGY_CHARTS):
        errors["base"] = "no_price_source"
    return errors


class TibberCompareConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow."""

    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            errors = validate(self.hass, user_input)
            if not errors:
                await self.async_set_unique_id(user_input[CONF_ENERGY_ENTITY])
                self._abort_if_unique_id_configured()
                return self.async_create_entry(title="Tibber Compare", data=user_input)
        return self.async_show_form(
            step_id="user",
            data_schema=build_schema(user_input or {}),
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return TibberCompareOptionsFlow()


class TibberCompareOptionsFlow(OptionsFlow):
    """Options flow: everything (incl. sensors) can be changed."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            errors = validate(self.hass, user_input)
            if not errors:
                if CONF_PRICE_ENTITY not in user_input:
                    user_input[CONF_PRICE_ENTITY] = None  # cleared in UI
                return self.async_create_entry(data=user_input)
        current = {**self.config_entry.data, **self.config_entry.options, **(user_input or {})}
        return self.async_show_form(
            step_id="init", data_schema=build_schema(current), errors=errors
        )
