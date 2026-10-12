"""ADB frame capture, byte parsers, phone rectangle, and point coordinates."""

from dataclasses import dataclass
from io import BytesIO
import os
import struct
import subprocess
import time
from collections.abc import Callable

import numpy as np
from PIL import Image

PHONE_PT = (375, 812)
PUBLIC_SIZE = (750, 1624)


@dataclass(frozen=True)
class PhoneRect:
    x: int
    y: int
    width: int
    height: int
    # Point space the frame maps to. 375x812 (iPhone X/XS/11 Pro) is the SPEC contract and
    # stays the default; USB capture derives it from the frame and a per-model px/pt scale.
    pt_width: int = 375
    pt_height: int = 812

    @property
    def box(self) -> tuple[int, int, int, int]:
        return self.x, self.y, self.x + self.width, self.y + self.height

    def px_to_pt(self, x: float, y: float) -> tuple[float, float]:
        return (x - self.x) * self.pt_width / self.width, (y - self.y) * self.pt_height / self.height

    def pt_to_px(self, x: float, y: float) -> tuple[float, float]:
        return self.x + x * self.width / self.pt_width, self.y + y * self.height / self.pt_height


def find_phone_rect(image: Image.Image) -> PhoneRect:
    w, h = image.size
    # Already a phone crop (also used by the synthetic screen source).
    if abs(w / h - 375 / 812) < 0.01:
        return PhoneRect(0, 0, w, h)
    rgb = np.asarray(image.convert("RGB"))
    active = (rgb.max(axis=2) > 12).mean(axis=0) >= 0.01
    edges = np.diff(np.r_[False, active, False].astype(int))
    bands = list(zip(np.flatnonzero(edges == 1), np.flatnonzero(edges == -1)))
    if bands:
        left, right = max(bands, key=lambda b: b[1] - b[0])
        if abs((right - left) / h - 375 / 812) < 0.07:
            return PhoneRect(int(left), 0, int(right - left), h)
    # Read: measured Portal bounds in SPEC, scaled for a resized Portal frame.
    left, right = round(747 * w / 2160), round(1413 * w / 2160)
    return PhoneRect(left, 0, right - left, h)


def phone_crop(image: Image.Image, rect: PhoneRect | None = None) -> Image.Image:
    return image.crop((rect or find_phone_rect(image)).box).convert("RGB").resize(
        PUBLIC_SIZE, Image.Resampling.LANCZOS)


def parse_png(data: bytes) -> Image.Image:
    with Image.open(BytesIO(data)) as image:
        if image.format != "PNG":
            raise ValueError("screencap payload is not PNG")
        image.load()
        return image.convert("RGB")


def parse_raw(data: bytes) -> Image.Image:
    """Raw `screencap` output: little-endian w, h, format header, then packed RGBA/RGBX.

    The header is 12 bytes on older Android and 16 bytes (plus colour space) on Android 9+;
    the Portal+ sends 16 (measured 2026-10-09).
    """
    if len(data) < 12:
        raise ValueError("raw screencap header is truncated")
    w, h, fmt = struct.unpack_from("<III", data)
    if not w or not h or w * h > 100_000_000:
        raise ValueError("invalid raw screencap dimensions")
    if fmt not in (1, 2):
        raise ValueError(f"unsupported raw pixel format {fmt}; expected RGBA_8888/RGBX_8888")
    header = len(data) - w * h * 4
    if header not in (12, 16):
        raise ValueError("raw screencap length mismatch; expected a 12- or 16-byte header and packed RGBA")
    return Image.frombytes("RGBA", (w, h), data[header:]).convert("RGB")


class Screen:
    def __init__(self, serial: str | None = None, *, mode: str = "png", timeout: float = 15,
                 runner: Callable | None = None):
        if mode not in {"png", "raw"}:
            raise ValueError("capture mode must be png or raw")
        self.serial = serial or os.environ.get("IPHONE_HAND_ADB_SERIAL")
        self.mode, self.timeout, self.runner = mode, timeout, runner or subprocess.run
        self.last_grab_ms: float | None = None
        self.grab_ms: dict[str, float | None] = {"png": None, "raw": None}
        self.rect: PhoneRect | None = None

    def grab(self, mode: str | None = None) -> Image.Image:
        mode = mode or self.mode
        if mode not in self.grab_ms:
            raise ValueError("capture mode must be png or raw")
        args = ["adb"]
        if self.serial:
            args += ["-s", self.serial]
        args += ["exec-out", "screencap"]
        if mode == "png":
            args.append("-p")
        start = time.perf_counter()
        result = self.runner(args, capture_output=True, check=True, timeout=self.timeout)
        image = parse_png(result.stdout) if mode == "png" else parse_raw(result.stdout)
        self.last_grab_ms = (time.perf_counter() - start) * 1000
        self.grab_ms[mode] = self.last_grab_ms
        self.rect = find_phone_rect(image)
        return image

    def status(self) -> dict:
        return {"device": self.serial, "mode": self.mode, "last_grab_ms": self.last_grab_ms,
                "grab_ms": dict(self.grab_ms)}
