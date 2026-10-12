"""Red annulus detection, with geometry and neutral-centre checks.

Confidence is a geometric score, not a calibrated probability. All sampling
is restricted to visible pixels so a ring clipped by the phone edge can match.
"""

from dataclasses import dataclass
import math

import cv2
import numpy as np
from PIL import Image

from .screen import PhoneRect, find_phone_rect


@dataclass(frozen=True)
class Candidate:
    x: float
    y: float
    confidence: float

    def tuple(self) -> tuple[float, float, float]:
        return self.x, self.y, self.confidence


def _circle(points: np.ndarray) -> tuple[float, float, float] | None:
    if len(points) < 3:
        return None
    a = np.c_[2 * points, np.ones(len(points))]
    if np.linalg.matrix_rank(a) < 3:
        return None
    cx, cy, c = np.linalg.lstsq(a, (points * points).sum(axis=1), rcond=None)[0]
    r2 = c + cx * cx + cy * cy
    return (float(cx), float(cy), math.sqrt(r2)) if r2 > 0 else None


def candidates(image: Image.Image, rect: PhoneRect | None = None) -> list[Candidate]:
    rect = rect or find_phone_rect(image)
    rgb = np.asarray(image.crop(rect.box).convert("RGB"))
    h, w = rgb.shape[:2]
    hsv = cv2.cvtColor(rgb, cv2.COLOR_RGB2HSV)
    red = (((hsv[:, :, 0] <= 10) | (hsv[:, :, 0] >= 170)) &
           (hsv[:, :, 1] >= 140) & (hsv[:, :, 2] >= 75))
    nominal = 31 * (rect.width / 375 + rect.height / 812) / 4  # radius in px
    contours, _ = cv2.findContours(red.astype(np.uint8), cv2.RETR_LIST, cv2.CHAIN_APPROX_NONE)
    # Over a red background a clipped grey centre joins the non-red boundary,
    # so its arc belongs to the complement's component rather than a red hole.
    inner_contours, _ = cv2.findContours((~red).astype(np.uint8), cv2.RETR_LIST,
                                       cv2.CHAIN_APPROX_NONE)
    contours = list(contours) + list(inner_contours)
    proposals: list[tuple[float, float, float]] = []
    rng = np.random.default_rng(0)
    for contour in contours:
        points = contour[:, 0, :].astype(float)
        x, y, cw, ch = cv2.boundingRect(contour)
        clipped = x == 0 or y == 0 or x + cw == w or y + ch == h
        # Includes inner hole contours for a ring over a red background.
        if not clipped and (min(cw, ch) < nominal * 0.65 or
                            max(cw, ch) > nominal * 2.7 or
                            min(cw, ch) / max(cw, ch) < 0.75):
            continue
        if len(points) < 12 or (clipped and max(cw, ch) > nominal * 3):
            continue
        if not clipped:
            fit = _circle(points)
            if fit:
                cx, cy, radius = fit
                proposals += [(cx, cy, radius), (cx, cy, radius / 0.62)]
        else:
            # Ignore straight crop edges. RANSAC recovers outer/inner circular
            # arcs even when the centre lies at a corner of the frame.
            points = points[(points[:, 0] > 0) & (points[:, 0] < w - 1) &
                            (points[:, 1] > 0) & (points[:, 1] < h - 1)]
            if len(points) < 8:
                continue
            for _ in range(100):
                fit = _circle(points[rng.choice(len(points), 3, replace=False)])
                if fit:
                    cx, cy, radius = fit
                    proposals += [(cx, cy, radius), (cx, cy, radius / 0.62)]

    angles = np.linspace(0, 2 * np.pi, 96, endpoint=False)
    found = []
    for cx, cy, radius in proposals:
        if not (nominal * 0.65 <= radius <= nominal * 1.35 and
                -2 <= cx <= w + 1 and -2 <= cy <= h + 1):
            continue
        # Red must surround a neutral hollow centre, not just occupy a bbox.
        def sample(fractions):
            xx = np.rint(cx + radius * np.outer(fractions, np.cos(angles))).astype(int)
            yy = np.rint(cy + radius * np.outer(fractions, np.sin(angles))).astype(int)
            valid = (xx >= 0) & (xx < w) & (yy >= 0) & (yy < h)
            return xx[valid], yy[valid], valid.sum()

        rx, ry, nr = sample([0.77, 0.87, 0.94])
        ix, iy, ni = sample([0.0, 0.20, 0.40])
        if nr < 55 or ni < 40:
            continue
        ring = float(red[ry, rx].mean())
        hollow = float((~red[iy, ix]).mean())
        neutral = float(((hsv[iy, ix, 1] < 135) & (hsv[iy, ix, 2] >= 35) &
                         (hsv[iy, ix, 2] <= 235)).mean())
        # The centre is translucent grey, so over a saturated background (blue lock-screen
        # clock, measured 2026-10-10) it is not neutral; neutrality only adds confidence.
        if ring < 0.84 or hollow < 0.90:
            continue
        size_score = max(0.0, 1 - abs(radius - nominal) / nominal)
        confidence = 0.45 * ring + 0.25 * hollow + 0.20 * neutral + 0.10 * size_score
        px, py = rect.x + cx, rect.y + cy
        px, py = rect.px_to_pt(px, py)
        found.append(Candidate(max(0, min(375, px)), max(0, min(812, py)), confidence))
    # Suppress outer/inner fits and RANSAC fits describing the same ring.
    unique: list[Candidate] = []
    for c in sorted(found, key=lambda c: c.confidence, reverse=True):
        if not any(math.hypot(c.x - b.x, c.y - b.y) < 12 for b in unique):
            unique.append(c)
    return unique


def find_pointer(image: Image.Image, rect: PhoneRect | None = None) -> tuple[float, float, float] | None:
    found = candidates(image, rect)
    # Ambiguity fails closed. Observing a second frame alone cannot resolve it.
    return found[0].tuple() if len(found) == 1 else None


def confirm_moved(before: list[Candidate], after: list[Candidate], *,
                  minimum_motion: float = 3, stationary_tolerance: float = 2) -> Candidate | None:
    """Choose one new ring after an explicitly requested relative probe.

    Static candidates disappear from consideration. Caller owns the MOVE and
    capture so pure detection never has device side effects.
    """
    changed = [c for c in after if not any(math.hypot(c.x - b.x, c.y - b.y) <=
                                          stationary_tolerance for b in before)]
    vanished = [b for b in before if not any(math.hypot(c.x - b.x, c.y - b.y) <=
                                            stationary_tolerance for c in after)]
    if len(changed) == len(vanished) == 1 and math.hypot(
            changed[0].x - vanished[0].x, changed[0].y - vanished[0].y) >= minimum_motion:
        return changed[0]
    return None
