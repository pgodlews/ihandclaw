import math

import numpy as np
from PIL import Image, ImageDraw
import pytest

from iphone_hand.pointer import Candidate, candidates, confirm_moved, find_pointer
from iphone_hand.screen import PhoneRect, find_phone_rect


def without_pointer(image, label):
    image = image.copy()
    x, y = label["pointer_px"]
    ImageDraw.Draw(image).rectangle((x - 33, y - 33, x + 33, y + 33), fill="white")
    return image


def paste_ring(image, centre, *, radius=27.5):
    # Estimated synthetic ring: opaque red border, translucent grey centre,
    # elliptical measured aspect ratio; supersampling preserves smooth edges.
    patch = Image.new("RGBA", (240, 240))
    draw = ImageDraw.Draw(patch)
    rx, ry = radius * 4, radius * 52 / 55 * 4
    draw.ellipse((120 - rx, 120 - ry, 120 + rx, 120 + ry), fill=(210, 0, 0, 255))
    draw.ellipse((120 - rx * 0.62, 120 - ry * 0.62, 120 + rx * 0.62, 120 + ry * 0.62),
                 fill=(150, 150, 150, 190))
    patch = patch.resize((60, 60), Image.Resampling.LANCZOS)
    result = image.convert("RGBA")
    result.alpha_composite(patch, (round(centre[0] - 30), round(centre[1] - 30)))
    return result.convert("RGB")


def assert_centre(image, centre, rect):
    detected = find_pointer(image, rect)
    assert detected is not None
    actual = rect.pt_to_px(*detected[:2])
    assert math.dist(actual, centre) <= 3
    assert 0.8 <= detected[2] <= 1


def test_real_pointer_within_three_pixels_and_distractors(real_screen, labels):
    name, image = real_screen
    rect = find_phone_rect(image)
    assert_centre(image, labels[name]["pointer_px"], rect)
    assert len(candidates(image, rect)) == 1
    clean = without_pointer(image, labels[name])
    assert find_pointer(clean, rect) is None
    assert candidates(clean, rect) == []
    # Inspect each labelled distractor with its original coordinate transform.
    for bounds in labels["red_distractors_px"].values():
        x1, y1, x2, y2 = bounds
        crop = image.crop((x1 - 8, y1 - 8, x2 + 8, y2 + 8))
        isolated = Image.new("RGB", image.size, "white")
        isolated.paste(crop, (x1 - 8, y1 - 8))
        assert candidates(isolated, rect) == []


def test_random_ring_positions_over_each_fixture(real_screen, labels):
    name, image = real_screen
    clean = without_pointer(image, labels[name])
    rect = find_phone_rect(clean)
    rng = np.random.default_rng(818)
    for _ in range(24):
        centre = (int(rng.integers(780, 1380)), int(rng.integers(35, 1405)))
        assert_centre(paste_ring(clean, centre), centre, rect)


@pytest.mark.parametrize("background", ["white", "black", "red"])
@pytest.mark.parametrize("centre", [(180, 300), (0, 300), (665, 300), (180, 0),
                                  (180, 1439), (0, 0), (665, 1439), (6, 7)])
def test_synthetic_ring_backgrounds_and_partial_edges(background, centre):
    image = Image.new("RGB", (666, 1440), background)
    assert_centre(paste_ring(image, centre), centre, PhoneRect(0, 0, 666, 1440))


@pytest.mark.parametrize("radius", [20, 27.5, 35])
def test_expected_diameter_tolerance(radius):
    # Large patch for the +35% diameter case.
    image = Image.new("RGB", (666, 1440), "white")
    draw = ImageDraw.Draw(image)
    draw.ellipse((200 - radius, 300 - radius, 200 + radius, 300 + radius), fill=(210, 0, 0))
    draw.ellipse((200 - radius * .62, 300 - radius * .62,
                  200 + radius * .62, 300 + radius * .62), fill=(155, 155, 155))
    assert_centre(image, (200, 300), PhoneRect(0, 0, 666, 1440))


def test_multiple_candidates_and_motion_confirmation():
    rect = PhoneRect(0, 0, 666, 1440)
    image = Image.new("RGB", (666, 1440), "white")
    image = paste_ring(paste_ring(image, (100, 200)), (400, 600))
    assert len(candidates(image, rect)) == 2
    assert find_pointer(image, rect) is None
    before = [Candidate(10, 20, 1), Candidate(100, 200, 1)]
    after = [Candidate(10, 20, 1), Candidate(112, 205, 1)]
    assert confirm_moved(before, after) == after[1]
    assert confirm_moved(before, before) is None
