from io import BytesIO
import struct
from types import SimpleNamespace

import numpy as np
from PIL import Image
import pytest

from iphone_hand.screen import PhoneRect, Screen, find_phone_rect, parse_png, parse_raw, phone_crop


def test_real_rectangle_and_mapping(real_screen):
    _, image = real_screen
    rect = find_phone_rect(image)
    assert rect == PhoneRect(747, 0, 666, 1440)
    assert rect.px_to_pt(747, 0) == (0, 0)
    assert rect.pt_to_px(375, 812) == (1413, 1440)
    for pt in [(0, 0), (100.25, 200.75), (375, 812)]:
        assert rect.px_to_pt(*rect.pt_to_px(*pt)) == pytest.approx(pt)
    assert phone_crop(image).size == (750, 1624)


def test_largest_column_band_and_fallback():
    image = Image.new("RGB", (2160, 1440))
    image.paste("white", (400, 0, 1066, 1440))
    image.paste("white", (50, 0, 80, 1440))
    assert find_phone_rect(image) == PhoneRect(400, 0, 666, 1440)
    assert find_phone_rect(Image.new("RGB", image.size)) == PhoneRect(747, 0, 666, 1440)
    assert find_phone_rect(Image.new("RGB", (750, 1624))) == PhoneRect(0, 0, 750, 1624)


@pytest.mark.parametrize("fmt", [1, 2])
def test_raw_parser_from_bytes(fmt):
    pixels = np.array([[[255, 10, 20, 0], [0, 255, 30, 255]],
                       [[10, 20, 255, 255], [1, 2, 3, 128]]], dtype=np.uint8)
    for header in (struct.pack("<III", 2, 2, fmt), struct.pack("<IIII", 2, 2, fmt, 1)):
        raw = header + pixels.tobytes()
        assert np.array_equal(np.asarray(parse_raw(raw)), pixels[:, :, :3])


@pytest.mark.parametrize("data", [b"", struct.pack("<III", 0, 1, 1),
    struct.pack("<III", 2, 2, 3) + bytes(16), struct.pack("<III", 2, 2, 1) + bytes(15),
    struct.pack("<IIIII", 2, 2, 1, 0, 0) + bytes(16)])
def test_bad_raw(data):
    with pytest.raises(ValueError):
        parse_raw(data)


def test_capture_both_paths_and_timing_without_adb():
    image = Image.new("RGBA", (2, 2), (1, 2, 3, 255))
    png = BytesIO()
    image.save(png, format="PNG")
    raw = struct.pack("<III", 2, 2, 1) + image.tobytes()
    calls = []
    def run(args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(stdout=png.getvalue() if "-p" in args else raw)
    screen = Screen(runner=run)
    assert screen.status()["grab_ms"] == {"png": None, "raw": None}
    assert np.array_equal(np.asarray(screen.grab("png")), np.asarray(screen.grab("raw")))
    assert calls[0][0] == ["adb", "exec-out", "screencap", "-p"]
    assert calls[1][0] == ["adb", "exec-out", "screencap"]
    assert all(c[1]["check"] and c[1]["timeout"] == 15 for c in calls)
    assert all(t is not None and t >= 0 for t in screen.status()["grab_ms"].values())


def test_png_rejects_other_formats():
    image = BytesIO()
    Image.new("RGB", (2, 2)).save(image, format="JPEG")
    with pytest.raises(ValueError, match="not PNG"):
        parse_png(image.getvalue())
