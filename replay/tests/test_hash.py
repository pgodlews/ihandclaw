"""Tests for perceptual hashing and status-bar masking."""
from __future__ import annotations

import numpy as np
from PIL import Image

from replay.hash import (
    compute_dhash,
    compute_phash,
    hamming_distance,
    mask_status_bar,
    screens_match,
)
from replay.tests.conftest import make_screen_image


def test_mask_status_bar():
    img = make_screen_image("Test", clock_time="09:41")
    masked = mask_status_bar(img, top_ratio=0.06, bottom_ratio=0.05)
    arr = np.array(masked)
    h, w, _ = arr.shape

    top_px = int(h * 0.06)
    bottom_px = int(h * 0.95)

    # Top band must be completely zeroed (black)
    assert np.all(arr[:top_px, :] == 0)
    # Bottom band must be completely zeroed
    assert np.all(arr[bottom_px:, :] == 0)
    # Content area must contain nonzero pixels
    assert np.any(arr[top_px:bottom_px, :] > 0)


def test_status_bar_jitter_invariance():
    """Two screens identical except for the clock time (09:41 vs 10:55) must match with distance 0."""
    img1 = make_screen_image("Home Screen", clock_time="09:41")
    img2 = make_screen_image("Home Screen", clock_time="10:55")

    # With status bar masking (default), hash distance is 0
    ph1 = compute_phash(img1, mask_status=True)
    ph2 = compute_phash(img2, mask_status=True)
    assert hamming_distance(ph1, ph2) == 0
    assert screens_match(img1, img2, threshold=2, method="phash")

    dh1 = compute_dhash(img1, mask_status=True)
    dh2 = compute_dhash(img2, mask_status=True)
    assert hamming_distance(dh1, dh2) == 0
    assert screens_match(img1, img2, threshold=2, method="dhash")


def test_different_screens_distinguishable():
    """Completely different screens (Home with items vs Settings Dark) must have large distance."""
    home = make_screen_image(
        "Home Screen",
        bg_color=(255, 255, 255),
        items=[("Settings", (50, 150, 120, 220)), ("Safari", (150, 150, 220, 220))],
    )
    settings_dark = make_screen_image(
        "Settings Dark",
        bg_color=(10, 10, 15),
        items=[("Wi-Fi", (20, 120, 355, 170)), ("Dark Mode", (20, 180, 355, 230))],
    )

    p_dist = hamming_distance(compute_phash(home), compute_phash(settings_dark))
    assert p_dist >= 15
    assert not screens_match(home, settings_dark, threshold=6, method="phash")
