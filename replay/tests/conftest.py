"""Pytest fixtures for synthetic iPhone screens and replay test suites."""
from __future__ import annotations

import json
from pathlib import Path
import sys
from typing import Any

from PIL import Image, ImageDraw, ImageFont
import pytest

# Ensure root repository is on sys.path
REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from replay.graph import ScreenGraph, ScreenNode, Transition
from replay.manifest import SessionManifest, StepRecord, UIElement


def make_screen_image(
    title: str,
    clock_time: str = "10:49",
    bg_color: tuple[int, int, int] = (240, 240, 245),
    items: list[tuple[str, tuple[int, int, int, int]]] | None = None,
) -> Image.Image:
    """Create a synthetic iPhone XS screen (1126x2434 px for 375x812 pt)."""
    img = Image.new("RGB", (1126, 2434), color=bg_color)
    draw = ImageDraw.Draw(img)
    font = ImageFont.load_default()

    # Draw top status bar (top ~135 px)
    draw.text((100, 40), clock_time, fill=(0, 0, 0) if bg_color[0] > 100 else (255, 255, 255), font=font)
    draw.text((950, 40), "100%", fill=(0, 0, 0) if bg_color[0] > 100 else (255, 255, 255), font=font)

    # Title
    draw.text((100, 250), title, fill=(0, 0, 0) if bg_color[0] > 100 else (255, 255, 255), font=font)

    # Items / buttons
    if items:
        for name, box_pt in items:
            sx, sy = 1126 / 375.0, 2434 / 812.0
            l, t, r, b = box_pt
            draw.rectangle((l * sx, t * sy, r * sx, b * sy), outline="gray", width=3)
            draw.text((l * sx + 10, t * sy + 10), name, fill="black", font=font)

    # Bottom home indicator (bottom ~100 px)
    draw.rectangle((450, 2380, 676, 2390), fill=(120, 120, 120))
    return img


@pytest.fixture
def sample_home_elements() -> list[dict[str, Any]]:
    return [
        {
            "id": "vc1",
            "text": "Settings",
            "type": "vc_item",
            "bounds": "[50,150][120,220]",
            "bounds_pt": [50.0, 150.0, 120.0, 220.0],
            "center": [85.0, 185.0],
            "size": [70.0, 70.0],
            "clickable": True,
            "editable": False,
            "enabled": True,
            "checked": False,
            "focused": False,
            "selected": False,
            "scrollable": False,
            "longClickable": True,
            "password": False,
            "hint": "",
            "action": "tap",
            "parent": "",
            "depth": 0,
        },
        {
            "id": "vc2",
            "text": "Safari",
            "type": "vc_item",
            "bounds": "[150,150][220,220]",
            "bounds_pt": [150.0, 150.0, 220.0, 220.0],
            "center": [185.0, 185.0],
            "size": [70.0, 70.0],
            "clickable": True,
            "editable": False,
            "enabled": True,
            "checked": False,
            "focused": False,
            "selected": False,
            "scrollable": False,
            "longClickable": True,
            "password": False,
            "hint": "",
            "action": "tap",
            "parent": "",
            "depth": 0,
        },
    ]


@pytest.fixture
def sample_settings_elements() -> list[dict[str, Any]]:
    return [
        {
            "id": "vc1",
            "text": "Dark Mode",
            "type": "vc_item",
            "bounds": "[20,120][355,170]",
            "bounds_pt": [20.0, 120.0, 355.0, 170.0],
            "center": [187.5, 145.0],
            "size": [335.0, 50.0],
            "clickable": True,
            "editable": False,
            "enabled": True,
            "checked": False,
            "focused": False,
            "selected": False,
            "scrollable": False,
            "longClickable": True,
            "password": False,
            "hint": "",
            "action": "tap",
            "parent": "",
            "depth": 0,
        }
    ]
