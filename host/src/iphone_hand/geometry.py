"""Device geometry: point size, px/pt scale and orientation, decided in one place.

Phone coordinates are points (pt), origin top-left of the screen as mirrored. 375x812 (iPhone
X/XS/11 Pro, the reference phone) stays the default. Other sizes come from the `screen_pt`
override or are detected from a frame (`detect`).

Read (2026-10-12): Apple's HIG Layout page,
https://developer.apple.com/design/human-interface-guidelines/layout, no longer shows a
device-size table; its change log says it was revised on 2026-09-09. The table below is rebuilt
from these sources (cited per row):
  A  Apple, iOS Device Compatibility Reference, Displays (UIKit points and scale):
     https://developer.apple.com/library/archive/documentation/DeviceInformation/Reference/iOSDeviceCompatibility/Displays/Displays.html
  S  Apple, App Store Connect screenshot specifications (portrait pixels per display class):
     https://developer.apple.com/help/app-store-connect/reference/screenshot-specifications
  G  secondary cross-check of points and scale per model (cites Apple's pages):
     https://gist.github.com/ricsantos/e7baee6885626b9cb87c021a5097623f
For "S,G" rows the pixels come from S and the points and scale from G (points x scale == S pixels).
The only measured row is the XS: AirPlay frames are 1126x2434 px for 375x812 pt.
"""
from dataclasses import dataclass
import math

REFERENCE_PT = (375, 812)
# Measured (2026-10-10): iPhone XS AirPlay frames are 1126x2434, 0.0008 off the 375/812 aspect.
ASPECT_TOLERANCE = 0.002
NATIVE_TOLERANCE_PX = 4


@dataclass(frozen=True)
class DeviceSize:
    pt_width: int  # portrait
    pt_height: int
    scale: int     # UIKit scale: native frame px per pt
    models: str
    source: str

    @property
    def pt(self) -> tuple[int, int]:
        return self.pt_width, self.pt_height

    @property
    def aspect(self) -> float:
        return self.pt_width / self.pt_height


IPHONE_SIZES = (
    DeviceSize(375, 812, 3, "iPhone X, XS, 11 Pro, 12 mini, 13 mini", "A,G; XS measured"),
    DeviceSize(390, 844, 3, "iPhone 12, 12 Pro, 13, 13 Pro, 14", "S,G"),
    DeviceSize(393, 852, 3, "iPhone 14 Pro, 15, 15 Pro, 16", "S,G"),
    DeviceSize(402, 874, 3, "iPhone 16 Pro, 17, 17 Pro", "S,G"),
    DeviceSize(414, 896, 3, "iPhone XS Max, 11 Pro Max", "S,G"),
    DeviceSize(414, 896, 2, "iPhone XR, 11", "G"),
    DeviceSize(420, 912, 3, "iPhone Air", "S,G"),
    DeviceSize(428, 926, 3, "iPhone 12 Pro Max, 13 Pro Max, 14 Plus", "S,G"),
    DeviceSize(430, 932, 3, "iPhone 14 Pro Max, 15 Plus, 15 Pro Max, 16 Plus", "S,G"),
    DeviceSize(440, 956, 3, "iPhone 16 Pro Max, 17 Pro Max", "S,G"),
    DeviceSize(320, 568, 2, "iPhone 5, 5s, 5c, SE (1st generation)", "A,G"),
    DeviceSize(375, 667, 2, "iPhone 6, 6s, 7, 8, SE (2nd/3rd generation)", "A,S,G"),
    DeviceSize(414, 736, 3, "iPhone 6 Plus, 6s Plus, 7 Plus, 8 Plus", "A,S,G"),
)

# Phase 2 groundwork, untested: iPads, portrait points at @2x. "S/2" rows halve S's pixels
# (scale 2 is estimated for them; G has no row).
IPAD_SIZES = (
    DeviceSize(768, 1024, 2, "iPad Pro 9.7-inch, iPad Air 2, iPad mini 4", "A,G"),
    DeviceSize(810, 1080, 2, "iPad (7th-9th generation)", "G"),
    DeviceSize(820, 1180, 2, "iPad (10th generation), iPad Air (4th generation)", "S,G"),
    DeviceSize(744, 1133, 2, "iPad mini (6th generation)", "S,G"),
    DeviceSize(834, 1112, 2, "iPad Pro 10.5-inch, iPad Air (3rd generation)", "A,G"),
    DeviceSize(834, 1194, 2, "iPad Pro 11-inch (1st-4th generation)", "S,G"),
    DeviceSize(834, 1210, 2, "iPad Pro 11-inch (M4, M5)", "S/2"),
    DeviceSize(1024, 1366, 2, "iPad Pro 12.9-inch", "A,G"),
    DeviceSize(1032, 1376, 2, "iPad Pro 13-inch (M4, M5), iPad Air 13-inch", "S/2"),
)

FAMILIES = {"iphone": IPHONE_SIZES, "ipad": IPAD_SIZES}


@dataclass(frozen=True)
class Geometry:
    pt_width: int
    pt_height: int
    px_per_pt: float | None = None  # frame pixels per point, when a frame was seen
    source: str = "default"  # config | scale | native | aspect | estimated | default
    candidates: tuple[tuple[int, int], ...] = ()

    @property
    def pt(self) -> tuple[int, int]:
        return self.pt_width, self.pt_height

    @property
    def orientation(self) -> str:
        return "landscape" if self.pt_width > self.pt_height else "portrait"

    @property
    def ambiguous(self) -> bool:
        return len(self.candidates) > 1

    @property
    def public_size(self) -> tuple[int, int]:
        """The pt2 image size: exactly 2 px per pt (750x1624 for the reference)."""
        return 2 * self.pt_width, 2 * self.pt_height

    def contains(self, x: float, y: float) -> bool:
        return (math.isfinite(x) and math.isfinite(y) and
                0 <= x <= self.pt_width and 0 <= y <= self.pt_height)

    def to_dict(self) -> dict:
        return {"pt": [self.pt_width, self.pt_height],
                "px_per_pt": None if self.px_per_pt is None else round(self.px_per_pt, 4),
                "orientation": self.orientation, "source": self.source,
                "ambiguous": self.ambiguous, "candidates": [list(c) for c in self.candidates]}


def parse_screen_pt(value) -> tuple[int, int] | None:
    """Validate a `screen_pt = [w, h]` override; None means detect."""
    if value is None:
        return None
    if (not isinstance(value, (list, tuple)) or len(value) != 2 or
            any(isinstance(v, bool) or not isinstance(v, int) or not 100 <= v <= 4000 for v in value)):
        raise ValueError("screen_pt must be [width, height] integer points in 100..4000")
    return int(value[0]), int(value[1])


def sizes(family: str = "iphone") -> tuple[DeviceSize, ...]:
    if family not in FAMILIES:
        raise ValueError("device must be iphone or ipad")
    return FAMILIES[family]


def known_aspect(width: float, height: float, *, family: str = "iphone", tolerance: float = 0.01) -> bool:
    """Whether a frame (either orientation) has the shape of a known screen."""
    short, long = sorted((width, height))
    return long > 0 and any(abs(short / long - d.aspect) < tolerance for d in sizes(family))


def detect(frame_size, *, screen_pt=None, px_per_pt: float = 0.0, native: bool = False,
           family: str = "iphone") -> Geometry:
    """Point geometry for a phone-screen frame (already cropped to the screen).

    Order: config override, explicit px/pt scale, exact native pixels (only when the source
    says its frames are unscaled, e.g. USB capture), aspect ratio, @3x fallback. Orientation
    follows the frame: a landscape frame swaps the portrait table size.
    Estimated (not measured): an AirPlay receiver scales every phone into its requested
    display size (UxPlay `-s 1125x2436`), so AirPlay frames carry only the aspect ratio, and
    all Face ID iPhones are within ~0.002 of each other. Ambiguous matches keep the reference
    375x812 when it is a candidate and report the candidates; set `screen_pt` to resolve.
    """
    w, h = frame_size
    landscape = w > h

    def oriented(pt):
        short, long = sorted(pt)
        return (long, short) if landscape else (short, long)

    if screen_pt is not None:
        pw, ph = oriented(parse_screen_pt(screen_pt))
        return Geometry(pw, ph, w / pw, "config")
    if px_per_pt and px_per_pt > 0:
        return Geometry(round(w / px_per_pt), round(h / px_per_pt), float(px_per_pt), "scale")
    table = sizes(family)
    if native:
        found = sorted({oriented(d.pt) for d in table
                        if abs(w - oriented(d.pt)[0] * d.scale) <= NATIVE_TOLERANCE_PX and
                        abs(h - oriented(d.pt)[1] * d.scale) <= NATIVE_TOLERANCE_PX})
        if len(found) == 1:
            return Geometry(*found[0], w / found[0][0], "native")
    short, long = sorted((w, h))
    aspect = short / long
    ranked = sorted({d.pt for d in table if abs(aspect - d.aspect) <= ASPECT_TOLERANCE},
                    key=lambda pt: (abs(aspect - pt[0] / pt[1]), pt))
    if ranked:
        chosen = REFERENCE_PT if REFERENCE_PT in ranked else ranked[0]
        pw, ph = oriented(chosen)
        return Geometry(pw, ph, w / pw, "aspect", tuple(oriented(pt) for pt in ranked))
    # Estimated: unknown shape, keep pixels square at the most common Retina scale.
    return Geometry(round(w / 3), round(h / 3), 3.0, "estimated")


def default_geometry(screen_pt=None) -> Geometry:
    """Geometry before any frame: the override, else the reference."""
    pt = parse_screen_pt(screen_pt)
    return Geometry(*pt, None, "config") if pt else Geometry(*REFERENCE_PT)


def size_label(pt) -> str:
    return f"{pt[0]}x{pt[1]}"
