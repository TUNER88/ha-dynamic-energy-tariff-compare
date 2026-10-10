"""Brand images shipped with the integration (HA >= 2026.3 serves brand/ locally)."""

from __future__ import annotations

from pathlib import Path
import struct

import pytest

BRAND = Path(__file__).parents[1] / "custom_components" / "dynamic_energy_tariff_compare" / "brand"


@pytest.mark.parametrize(
    ("name", "size"),
    [("icon.png", 256), ("icon@2x.png", 512), ("logo.png", 256), ("logo@2x.png", 512)],
)
def test_brand_image(name: str, size: int) -> None:
    data = (BRAND / name).read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    width, height, _bit_depth, color_type = struct.unpack(">IIBB", data[16:26])
    assert (width, height) == (size, size)
    assert color_type == 6  # RGBA -> transparent background


async def test_home_assistant_detects_brand(hass) -> None:
    """Home Assistant's loader sees the local brand/ folder (served via /api/brands)."""
    from homeassistant.loader import async_get_integration  # noqa: PLC0415

    integration = await async_get_integration(hass, "dynamic_energy_tariff_compare")
    assert integration.has_branding
