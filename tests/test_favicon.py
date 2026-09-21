"""The application icon is served, and carries every size it was built with."""

from __future__ import annotations

import io

from fastapi.testclient import TestClient
from PIL import Image

from radshield.web.app import app

client = TestClient(app)

EXPECTED_SIZES = {(16, 16), (20, 20), (24, 24), (32, 32),
                  (48, 48), (64, 64), (128, 128), (256, 256)}


def test_the_icon_is_served_as_an_ico():
    response = client.get("/favicon.ico")
    assert response.status_code == 200
    assert "icon" in response.headers["content-type"]
    assert response.content[:4] == b"\x00\x00\x01\x00"      # ICONDIR, type = icon


def test_it_carries_every_size_it_was_built_with():
    """A single-size .ico would be upscaled by Windows into a blur, which is
    the whole reason the small entries are drawn separately."""
    response = client.get("/favicon.ico")
    image = Image.open(io.BytesIO(response.content))
    assert set(image.ico.sizes()) == EXPECTED_SIZES


def test_the_small_entries_are_legible_rather_than_mush():
    """Below 32 px the mark reduces to a ring, so the 16 px entry should be
    mostly tile with a clear ring and nucleus -- not a smear of half-tones.
    A blurred downscale of the full mark would push the mid-tone share up.
    """
    response = client.get("/favicon.ico")
    image = Image.open(io.BytesIO(response.content))
    image.size = (16, 16)
    histogram = image.convert("L").histogram()
    mid = sum(histogram[61:140])
    assert mid / sum(histogram) < 0.4
