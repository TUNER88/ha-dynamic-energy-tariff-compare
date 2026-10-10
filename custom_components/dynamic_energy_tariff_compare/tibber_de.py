"""Fetch Tibber Germany prices for a postcode (no Tibber account needed)."""

from __future__ import annotations

import asyncio
from http import HTTPStatus
from typing import Any

import aiohttp

from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.util import dt as dt_util

from . import calc
from .const import TIBBER_DE_URL

TIMEOUT = 20


class TibberLookupError(Exception):
    """Lookup failed. `reason` is a config-flow error key."""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(detail or reason)
        self.reason = reason


async def async_fetch_overview(hass: HomeAssistant, postcode: str) -> dict[str, Any]:
    """Return the raw price-overview payload for a German postcode."""
    code = calc.valid_postcode(postcode)
    if code is None:
        raise TibberLookupError("invalid_postcode")
    session = async_get_clientsession(hass)
    try:
        async with asyncio.timeout(TIMEOUT):
            resp = await session.get(
                TIBBER_DE_URL,
                params={"postalCode": code},
                headers={"Accept": "application/json", "Accept-Language": "de-DE"},
            )
            # unknown postcodes answer 404, malformed ones 204 (no content)
            if resp.status in (HTTPStatus.NOT_FOUND, HTTPStatus.NO_CONTENT):
                raise TibberLookupError("invalid_postcode", f"HTTP {resp.status}")
            if resp.status != HTTPStatus.OK:
                raise TibberLookupError("cannot_connect", f"HTTP {resp.status}")
            payload = await resp.json(content_type=None)
    except TibberLookupError:
        raise
    except (aiohttp.ClientError, TimeoutError, ValueError) as err:
        raise TibberLookupError("cannot_connect", str(err)) from err
    if not isinstance(payload, dict) or not isinstance(payload.get("energy"), dict):
        raise TibberLookupError("invalid_postcode", "unexpected response")
    return payload


async def async_fetch_parsed(hass: HomeAssistant, postcode: str) -> calc.TibberOverview:
    payload = await async_fetch_overview(hass, postcode)
    return calc.parse_tibber_overview(payload, dt_util.get_default_time_zone())
