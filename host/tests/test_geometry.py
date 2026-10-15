"""Variable screen geometry with synthetic frames only (generated here, no photos).

Sizes and scales come from geometry.IPHONE_SIZES (read: Apple sources cited there). Nothing here
is a measurement of a real phone other than the XS AirPlay frame size quoted in the SPEC.
"""
import json
import math
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw
import pytest

from iphone_hand import geometry
from iphone_hand.airplay import AirPlayScreen
from iphone_hand.daemon import Config, create_app
from iphone_hand.elements import Elements
from iphone_hand.fake import FakePhone, FakeSerialDevice, SimulatedScreen
from iphone_hand.geometry import IPHONE_SIZES, REFERENCE_PT, detect, parse_screen_pt
from iphone_hand.motion import validate_target
from iphone_hand.phone import Phone
from iphone_hand.pointer import candidates, find_pointer, get_detector
from iphone_hand.screen import PhoneRect, find_phone_rect, phone_crop
from iphone_hand.serial_link import SerialLink

# (points, native @3x pixels) for the sizes the task names; pixels match App Store Connect (read).
SIZES = [((375, 812), (1125, 2436)), ((390, 844), (1170, 2532)),
         ((393, 852), (1179, 2556)), ((430, 932), (1290, 2796))]
UXPLAY_BOX = (1125, 2436)  # UxPlay -s used by the daemon (airplay.py)


def airplay_size(native):
    # Estimated model of the receiver: the phone scales its screen to fit the requested box.
    s = min(UXPLAY_BOX[0] / native[0], UXPLAY_BOX[1] / native[1])
    return round(native[0] * s), round(native[1] * s)


def blank(size):
    return Image.new("RGB", size, (240, 240, 240))


def test_table_is_consistent_and_contains_the_reference():
    assert REFERENCE_PT in {d.pt for d in IPHONE_SIZES}
    rows = [(d.pt, d.scale) for d in IPHONE_SIZES]
    assert len(rows) == len(set(rows))
    for d in IPHONE_SIZES + geometry.IPAD_SIZES:
        assert d.pt_width < d.pt_height and d.scale in (2, 3) and d.models and d.source


@pytest.mark.parametrize("pt,native", SIZES)
def test_native_frames_detect_exact_size(pt, native):
    found = detect(native, native=True)
    assert found.pt == pt and found.source == "native" and found.px_per_pt == pytest.approx(3)
    assert found.orientation == "portrait" and not found.ambiguous
    landscape = detect(native[::-1], native=True)
    assert landscape.pt == pt[::-1] and landscape.orientation == "landscape"


def test_measured_xs_airplay_frame_keeps_the_reference():
    # Measured (SPEC): XS AirPlay frames are 1126x2434; aspect-only, ambiguous, reference kept.
    found = detect((1126, 2434))
    assert found.pt == REFERENCE_PT and found.source == "aspect" and found.ambiguous
    assert detect((1126, 2434), native=True).source == "native"


@pytest.mark.parametrize("pt,native", SIZES)
def test_airplay_frames_are_ambiguous_until_configured(pt, native):
    size = airplay_size(native)
    found = detect(size)
    # Face ID aspects are within ~0.002: honest ambiguity, the reference stays the default.
    assert found.pt == REFERENCE_PT and found.ambiguous and pt in found.candidates
    configured = detect(size, screen_pt=list(pt))
    assert configured.pt == pt and configured.source == "config"
    assert configured.px_per_pt == pytest.approx(size[0] / pt[0])
    # A rescaled frame can also fake a native match; the AirPlay source never claims native.
    assert AirPlayScreen.frames_native is False


def test_override_follows_frame_orientation_and_validation():
    assert detect((2532, 1170), screen_pt=[390, 844]).pt == (844, 390)
    assert detect((1170, 2532), screen_pt=(844, 390)).pt == (390, 844)
    for bad in ([390], [390, "844"], [True, 844], [10, 844], "390x844", [390, 844, 1]):
        with pytest.raises(ValueError, match="screen_pt"):
            parse_screen_pt(bad)
    with pytest.raises(ValueError, match="screen_pt"):
        Config(token="t", screen_pt=[390])
    assert Config(token="t", screen_pt=[390, 844]).screen_pt == [390, 844]
    with pytest.raises(ValueError, match="device"):
        Config(token="t", device="watch")
    with pytest.raises(ValueError, match="pointer_detector"):
        Config(token="t", pointer_detector="laser")


def test_home_button_ipad_and_unknown_shapes():
    assert detect((750, 1334), native=True).pt == (375, 667)
    scaled = detect((700, 1245))  # 375x667, 414x736 and 320x568 share ~9:16
    assert scaled.ambiguous and scaled.pt in {(375, 667), (414, 736), (320, 568)}
    assert detect((1640, 2360), native=True, family="ipad").pt == (820, 1180)
    assert detect((2360, 1640), native=True, family="ipad").orientation == "landscape"
    assert detect((1640, 2360), native=True).source == "estimated"  # not an iPhone shape
    assert detect((1000, 1000)).pt == (333, 333)
    with pytest.raises(ValueError, match="device"):
        detect((1, 2), family="watch")


@pytest.mark.parametrize("pt,native", SIZES)
def test_pt_px_mapping_and_crop(pt, native):
    rect = find_phone_rect(blank(native), native=True)
    assert rect.box == (0, 0, *native) and rect.pt == pt
    assert rect.px_to_pt(*native) == pytest.approx(pt)
    assert rect.pt_to_px(*pt) == pytest.approx(native)
    for p in [(0, 0), (pt[0] / 3, pt[1] * 0.7), pt]:
        assert rect.px_to_pt(*rect.pt_to_px(*p)) == pytest.approx(p)
    assert phone_crop(blank(native), rect).size == (2 * pt[0], 2 * pt[1])
    assert rect.geometry.public_size == (2 * pt[0], 2 * pt[1])


@pytest.mark.parametrize("pt,native", SIZES)
def test_letterboxed_mirror_crop(pt, native):
    # A Portal-like landscape display with the phone as a centred column band.
    frame = Image.new("RGB", (2160, 1440))
    width = round(1440 * pt[0] / pt[1])
    frame.paste("white", (700, 0, 700 + width, 1440))
    rect = find_phone_rect(frame, screen_pt=pt)
    assert rect.box == (700, 0, 700 + width, 1440) and rect.pt == pt
    assert rect.px_to_pt(700 + width, 1440) == pytest.approx(pt)


@pytest.mark.parametrize("pt,native", SIZES)
def test_bounds_follow_the_screen(pt, native):
    w, h = pt
    validate_target(w, h, pt)
    with pytest.raises(ValueError, match=f"0..{w} by 0..{h}"):
        validate_target(w + 1, 1, pt)
    phone = FakePhone(screen_pt=pt)
    assert phone.screenshot().size == (2 * w, 2 * h)
    assert phone.move_to(w - 1, h - 1)["error_pt"] == 0
    with pytest.raises(ValueError):
        phone.move_to(w + 1, 1)
    assert phone.status()["screen"]["pt"] == [w, h]


@pytest.mark.parametrize("pt,native", SIZES)
def test_pointer_search_scales_with_the_screen(pt, native):
    w, h = pt
    for position in [(w / 2, h / 2), (40, 60), (w - 30, h - 40)]:
        device = FakeSerialDevice(screen_pt=pt, position=position)
        screen = SimulatedScreen(device, px_per_pt=3)
        frame = screen.grab()
        assert frame.size == native
        rect = find_phone_rect(frame, native=True)
        found = find_pointer(frame, rect)
        assert found is not None and math.dist(found[:2], position) < 1
    # The same ring read with a wrong point size is still found, but mis-mapped: the reason a
    # configured or detected size matters (estimated tolerance of the radius check: +/-35 %).
    if pt != REFERENCE_PT:
        wrong = PhoneRect(0, 0, *native)
        assert candidates(frame, wrong)[0].x != pytest.approx(position[0], abs=1)


@pytest.mark.parametrize("pt,native", SIZES)
def test_element_mapping_uses_the_screen_points(pt, native):
    from iphone_hand import elements
    data = json.loads(Path(elements.__file__).with_name("digit_templates.json").read_text())
    w, h = pt
    image = Image.new("RGB", native, "black")
    draw = ImageDraw.Draw(image)
    spots = {1: (40, 200), 2: (w - 60, 300), 3: (w / 2, h - 120)}
    k = 3 / (1126 / 375)  # template glyphs are drawn at the measured XS px/pt
    for n, (x, y) in spots.items():
        px, py = x * 3, y * 3
        draw.rounded_rectangle((px - 14 * k, py - 11 * k, px + 50 * k, py + 47 * k), radius=12, fill=(85, 85, 85))
        mask = Image.fromarray(np.array([[int(v) * 255 for v in row] for row in data[str(n)]], dtype=np.uint8))
        mask = mask.resize((round(24 * k), round(36 * k)))
        image.paste(Image.new("RGB", mask.size, "white"), (round(px), round(py)), mask)
    rect = find_phone_rect(image, native=True)
    result = Elements(type("OCR", (), {"read": lambda self, image: []})()).analyze(image, rect)
    found = {int(i["id"][2:]): i for i in result["elements"] if i["type"] == "vc_item"}
    assert set(found) == {1, 2, 3} and result["screen_pt"] == [w, h]
    for n, (x, y) in spots.items():
        left, top = found[n]["badge_bounds"][:2]
        assert math.dist((left, top), (x - 14 / 3.0027, y - 11 / 3.0027)) < 2
        assert found[n]["bounds_pt"][2] <= w and found[n]["center"][0] < w
    marked = Elements.annotate(image, result)
    assert marked.size == image.size


@pytest.mark.parametrize("pt", [(390, 844), (430, 932)])
def test_phone_moves_and_calibrates_per_device(pt, tmp_path):
    w, h = pt
    cache = tmp_path / "calibration.json"
    device = FakeSerialDevice(screen_pt=pt)
    phone = Phone(SerialLink(transport=device), SimulatedScreen(device, px_per_pt=3), settle=0,
                  calibration_path=cache)
    # First action past the reference width: one frame decides the size, no command rejected.
    result = phone.move_to(w - 8, h - 10)
    assert result["error_pt"] <= 3 and phone.geometry().pt == pt and phone.motion.profile == f"{w}x{h}"
    with pytest.raises(ValueError):
        phone.move_to(w + 1, 10)
    calibrated = phone.calibrate()
    assert calibrated["profile"] == f"{w}x{h}" and all(s > 0 for s in calibrated["samples"])
    saved = json.loads(cache.read_text())
    assert "step" not in saved and saved["profiles"][f"{w}x{h}"]["screen_pt"] == [w, h]
    # The reference (XS) profile is untouched and still loads from the version-2 top level.
    xs = FakeSerialDevice()
    xs_phone = Phone(SerialLink(transport=xs), SimulatedScreen(xs), settle=0, calibration_path=cache)
    assert xs_phone.motion.profile == "375x812" and not xs_phone.motion.model.is_calibrated
    xs_phone.calibrate()
    both = json.loads(cache.read_text())
    assert both["step"] == 8 and f"{w}x{h}" in both["profiles"]
    again = Phone(SerialLink(transport=device), SimulatedScreen(device, px_per_pt=3), settle=0,
                  calibration_path=cache)
    again.find_pointer()
    again.motion._ensure_profile()
    assert again.motion.model.is_calibrated and again.motion.profile == f"{w}x{h}"


def test_daemon_state_and_pt2_at_a_non_reference_size(tmp_path):
    from fastapi.testclient import TestClient
    from io import BytesIO
    pt = (393, 852)
    device = FakeSerialDevice(screen_pt=pt)
    screen = SimulatedScreen(device, px_per_pt=3)
    phone = Phone(SerialLink(transport=device), screen, settle=0, calibration_path=tmp_path / "c.json")
    elements = Elements(type("OCR", (), {"read": lambda self, image: []})())
    app = create_app(Config(token="test-token", receiver="external"), phone=phone, screen=screen,
                     elements=elements)
    with TestClient(app) as client:
        headers = {"Authorization": "Bearer test-token"}
        state = client.get("/api/state", headers=headers).json()
        assert state["screen"]["pt"] == [393, 852] and state["screen"]["source"] == "native"
        assert state["pointer"]["x"] == pytest.approx(393 / 2, abs=1)
        frame = Image.open(BytesIO(client.get("/api/frame.jpg?scale=pt2", headers=headers).content))
        assert frame.size == (786, 1704)
        tapped = client.post("/api/tap", json={"x": 380, "y": 840}, headers=headers)
        assert tapped.status_code == 200 and tapped.json()["result"]["error_pt"] <= 3
        assert client.post("/api/tap", json={"x": 394, "y": 10}, headers=headers).status_code == 422
        assert client.post("/api/back", json={}, headers=headers).status_code == 200


def test_configured_size_over_an_airplay_shaped_frame(tmp_path):
    # AirPlay-shaped frames (not native) need screen_pt; the override wins over the aspect.
    device = FakeSerialDevice(screen_pt=(430, 932))
    screen = SimulatedScreen(device, px_per_pt=UXPLAY_BOX[1] / 932)
    screen.frames_native = False
    phone = Phone(SerialLink(transport=device), screen, settle=0, persist_calibration=False,
                  screen_pt=[430, 932])
    assert phone.move_to(420, 920)["error_pt"] <= 3
    assert phone.geometry().source == "config"
    unconfigured = Phone(SerialLink(transport=device), screen, settle=0, persist_calibration=False)
    unconfigured.find_pointer()
    assert unconfigured.geometry().pt == REFERENCE_PT and unconfigured.geometry().ambiguous


def test_pointer_detector_registry_and_ipados_prototype():
    assert get_detector().name == "assistivetouch"
    with pytest.raises(ValueError, match="pointer_detector"):
        get_detector("laser")
    # Prototype only: a translucent grey disc (estimated ~19 pt) on a light iPad-sized frame.
    rect = PhoneRect(0, 0, 1640, 2360, 820, 1180)
    image = Image.new("RGB", rect.box[2:], (235, 235, 240))
    disc = Image.new("RGB", image.size, (120, 120, 120))
    mask = Image.new("L", image.size, 0)
    ImageDraw.Draw(mask).ellipse((800 - 19, 1000 - 19, 800 + 19, 1000 + 19), fill=150)
    image.paste(disc, (0, 0), mask)
    ipados = get_detector("ipados")
    found = find_pointer(image, rect, ipados)
    assert found is not None and math.dist(found[:2], (400, 500)) < 2
    assert find_pointer(Image.new("RGB", image.size, (235, 235, 240)), rect, ipados) is None
    # The red-ring detector ignores the grey disc and vice versa.
    assert find_pointer(image, rect) is None


def test_cli_screen_size_and_airplay_frames_option(tmp_path, capsys):
    import argparse
    from iphone_hand import cli
    from iphone_hand.airplay import AirPlayScreen
    assert cli.main(["--fake", "--screen-pt", "430", "932", "shot", str(tmp_path / "s.png")]) == 0
    assert json.loads(capsys.readouterr().out)["size"] == [860, 1864]
    assert cli.main(["--fake", "--screen-pt", "430", "932", "move", "420", "900"]) == 0
    assert cli.main(["--fake", "move", "420", "900"]) == 1  # outside the 375x812 reference
    parser = argparse.ArgumentParser()
    cli.connection_options(parser)
    args = parser.parse_args(["--frames-dir", str(tmp_path), "--screen-pt", "390", "844"])
    phone = cli.make_phone(args)  # lazy: no serial port or receiver is opened here
    assert isinstance(phone.screen, AirPlayScreen) and phone.geometry().pt == (390, 844)
    assert phone.link.transport is None
