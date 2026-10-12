"""Unit tests for success predicate evaluation against elements and screenshots."""

from PIL import Image
import pytest

from bench.predicates import PredicateEvaluator, calculate_image_luminance


def test_ocr_text_contains():
    elements = [
        {"id": "t1", "text": "Display & Brightness"},
        {"id": "t2", "text": "Appearance"},
    ]
    res_pass = PredicateEvaluator.evaluate(
        {"type": "ocr_text_contains", "text": "Display"}, elements
    )
    assert res_pass.passed is True
    assert res_pass.score == 1.0

    res_fail = PredicateEvaluator.evaluate(
        {"type": "ocr_text_contains", "text": "Bluetooth"}, elements
    )
    assert res_fail.passed is False
    assert res_fail.score == 0.0


def test_ocr_text_exact():
    elements = [{"id": "t1", "text": "Dark"}, {"id": "t2", "text": "Light Mode"}]
    res_pass = PredicateEvaluator.evaluate(
        {"type": "ocr_text_exact", "text": "Dark"}, elements
    )
    assert res_pass.passed is True

    res_fail = PredicateEvaluator.evaluate(
        {"type": "ocr_text_exact", "text": "Light"}, elements
    )
    assert res_fail.passed is False


def test_ocr_text_matches_regex():
    elements = [{"id": "t1", "text": "00:59"}, {"id": "t2", "text": "Timer"}]
    res_pass = PredicateEvaluator.evaluate(
        {"type": "ocr_text_matches", "pattern": r"00:5\d"}, elements
    )
    assert res_pass.passed is True

    res_fail = PredicateEvaluator.evaluate(
        {"type": "ocr_text_matches", "pattern": r"12:00"}, elements
    )
    assert res_fail.passed is False


def test_element_exists_filter():
    elements = [
        {"id": "t_dark", "type": "text", "text": "Dark", "selected": True},
        {"id": "t_light", "type": "text", "text": "Light", "selected": False},
    ]
    res_pass = PredicateEvaluator.evaluate(
        {"type": "element_exists", "text": "Dark", "selected": True}, elements
    )
    assert res_pass.passed is True

    res_fail = PredicateEvaluator.evaluate(
        {"type": "element_exists", "text": "Light", "selected": True}, elements
    )
    assert res_fail.passed is False


def test_screenshot_dark_mode_luminance():
    # Dark image: rgb(30, 30, 30) -> luminance ~ 30
    dark_img = Image.new("RGB", (100, 100), (30, 30, 30))
    lum_dark = calculate_image_luminance(dark_img)
    assert lum_dark < 50.0

    res_dark = PredicateEvaluator.evaluate(
        {"type": "screenshot_dark_mode", "max_mean_luminance": 120.0},
        [],
        dark_img,
    )
    assert res_dark.passed is True

    # Light image: rgb(240, 240, 240) -> luminance ~ 240
    light_img = Image.new("RGB", (100, 100), (240, 240, 240))
    lum_light = calculate_image_luminance(light_img)
    assert lum_light > 200.0

    res_light = PredicateEvaluator.evaluate(
        {"type": "screenshot_dark_mode", "max_mean_luminance": 120.0},
        [],
        light_img,
    )
    assert res_light.passed is False


def test_composite_predicates():
    elements = [
        {"id": "t1", "text": "Settings"},
        {"id": "t2", "text": "General"},
    ]
    all_spec = {
        "type": "all_of",
        "predicates": [
            {"type": "ocr_text_contains", "text": "Settings"},
            {"type": "ocr_text_contains", "text": "General"},
        ],
    }
    assert PredicateEvaluator.evaluate(all_spec, elements).passed is True

    any_spec = {
        "type": "any_of",
        "predicates": [
            {"type": "ocr_text_contains", "text": "Nonexistent"},
            {"type": "ocr_text_contains", "text": "General"},
        ],
    }
    assert PredicateEvaluator.evaluate(any_spec, elements).passed is True

    none_spec = {
        "type": "none_of",
        "predicates": [
            {"type": "ocr_text_contains", "text": "Passcode"},
            {"type": "ocr_text_contains", "text": "Password"},
        ],
    }
    assert PredicateEvaluator.evaluate(none_spec, elements).passed is True
