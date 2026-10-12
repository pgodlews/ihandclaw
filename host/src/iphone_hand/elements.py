"""CPU-only, heuristic UI elements. Coordinates are always phone points.

Badge geometry and tap offsets are estimates; no accessibility tree is available.
Digit templates are synthetic Arial raster glyphs, not crops of the test fixture.
"""
from dataclasses import dataclass
import json
from pathlib import Path
import re
import threading
import time

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont

from .pointer import find_pointer
from .screen import PhoneRect


@dataclass(frozen=True)
class TextBox:
    text: str
    bounds: tuple[float, float, float, float]  # pixels, left/top/right/bottom
    confidence: float = 1.0


class OCRUnavailable(RuntimeError):
    pass


class RapidOCRReader:
    """Lazy optional dependency, with a bounded CPU thread count."""
    def __init__(self):
        self.engine = None

    def read(self, image: Image.Image) -> list[TextBox]:
        if self.engine is None:
            try:
                from rapidocr import RapidOCR
            except ImportError as exc:
                raise OCRUnavailable("Install iphone-hand[ocr] to enable text OCR and typing") from exc
            self.engine = RapidOCR(params={
                "EngineConfig.onnxruntime.intra_op_num_threads": 2,
                "EngineConfig.onnxruntime.inter_op_num_threads": 1,
                "Global.log_level": "error",
            })
        # PIL input lets RapidOCR perform its own RGB→BGR conversion.
        result = self.engine(image.convert("RGB"))
        boxes = []
        rows = zip(result.boxes, result.txts, result.scores) if result.boxes is not None else []
        for polygon, text, confidence in rows:
            points = np.asarray(polygon)
            boxes.append(TextBox(text, (float(points[:, 0].min()), float(points[:, 1].min()),
                                        float(points[:, 0].max()), float(points[:, 1].max())),
                                 float(confidence)))
        return boxes


_TEMPLATES = None


def _digit(mask):
    global _TEMPLATES
    if _TEMPLATES is None:
        raw = json.loads(Path(__file__).with_name("digit_templates.json").read_text())
        _TEMPLATES = {d: np.array([[int(c) for c in row] for row in rows], dtype=np.uint8)
                      for d, rows in raw.items()}
    norm = cv2.resize(mask.astype(np.uint8), (24, 36), interpolation=cv2.INTER_NEAREST)
    scores = {d: np.logical_and(norm, template).sum() / max(1, np.logical_or(norm, template).sum())
              for d, template in _TEMPLATES.items()}
    d = max(scores, key=scores.get)
    return d, float(scores[d])


def badges(image: Image.Image) -> list[dict]:
    """Find white numerals surrounded by a neutral dark badge, at any frame scale."""
    # Work at reference width to make the measured fixture's glyph geometry scale-independent.
    w, h = image.size
    scale = w / 1126
    rgb = np.asarray(image.convert("RGB").resize((1126, round(h / scale))))
    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    mask = (gray > 205).astype(np.uint8)
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    glyphs = sorted([cv2.boundingRect(c) for c in contours
                     if 29 <= cv2.boundingRect(c)[3] <= 36 and 7 <= cv2.boundingRect(c)[2] <= 27],
                    key=lambda r: (r[1], r[0]))
    groups = []
    while glyphs:
        first = glyphs[0]
        row = sorted([g for g in glyphs if abs(g[1]-first[1]) <= 2], key=lambda g: g[0])
        for g in row:
            glyphs.remove(g)
        group = []
        for g in row:
            if group and g[0]-(group[-1][0]+group[-1][2]) > 12:
                groups.append(group)
                group = []
            group.append(g)
        if group:
            groups.append(group)
    found = []
    for group in groups:
        if len(group) > 3:
            continue
        x = min(g[0] for g in group); y = min(g[1] for g in group)
        right = max(g[0] + g[2] for g in group); bottom = max(g[1] + g[3] for g in group)
        # The border immediately above/below the white glyphs must be dark neutral grey.
        strips = np.concatenate((rgb[max(0, y-6):y-2, x:right].reshape(-1, 3),
                                 rgb[bottom+2:bottom+6, x:right].reshape(-1, 3)))
        if not len(strips):
            continue
        neutral = (strips.max(1) - strips.min(1) < 24) & (strips.mean(1) > 50) & (strips.mean(1) < 145)
        if neutral.mean() < 0.75:
            continue
        digits = [_digit(mask[gy:gy+gh, gx:gx+gw]) for gx, gy, gw, gh in group]
        if min(score for _, score in digits) < 0.48:
            continue
        number = int(''.join(d for d, _ in digits))
        if not 1 <= number <= 999:
            continue
        found.append({"number": number, "bounds": (max(0, x-14)*scale, max(0, y-11)*scale,
                                                      (right+26)*scale, (bottom+11)*scale),
                      "confidence": min(score for _, score in digits)})
    # Duplicate readings are ambiguous; retain the most convincing badge.
    unique = {}
    for badge in found:
        n = badge["number"]
        if n not in unique or badge["confidence"] > unique[n]["confidence"]:
            unique[n] = badge
    return sorted(unique.values(), key=lambda b: b["number"])


def ui_element(id, text, type, bounds, center=None, **extra):
    left, top, right, bottom = bounds
    return {"id": id, "text": text, "type": type, "bounds": f"[{left:g},{top:g}][{right:g},{bottom:g}]", "bounds_pt": list(bounds),
            "center": list(center or ((left+right)/2, (top+bottom)/2)),
            "size": [right-left, bottom-top], "clickable": True, "editable": False,
            "enabled": True, "checked": False, "focused": False, "selected": False,
            "scrollable": False, "longClickable": True, "password": False, "hint": "",
            "action": "tap", "parent": "", "depth": 0, **extra}


class Elements:
    def __init__(self, reader=None, *, tap_offset=(10.0, 12.0)):
        self.reader = reader if reader is not None else RapidOCRReader()
        self.tap_offset = tap_offset  # pt below/right of badge bottom-right; estimated, configurable
        self.lock = threading.Lock()

    def analyze(self, image: Image.Image) -> dict:
        with self.lock:
            return self._analyze(image)

    def _analyze(self, image):
        start = time.perf_counter()
        rect = PhoneRect(0, 0, *image.size)
        items = []
        for badge in badges(image):
            l, t, r, b = badge["bounds"]
            left, top = rect.px_to_pt(l, t); right, bottom = rect.px_to_pt(r, b)
            center = (min(374, right+self.tap_offset[0]), min(811, bottom+self.tap_offset[1]))
            # Estimated local region; stop before the next badge to the right/below.
            items.append(ui_element(f"vc{badge['number']}", str(badge['number']), "vc_item",
                                    (left, top, min(375, left+85), min(812, top+85)), center,
                                    badge_bounds=[left, top, right, bottom], confidence=badge["confidence"]))
        for item in items:
            l, t, r, b = item["bounds_pt"]
            for other in items:
                ol, ot, _, _ = other["bounds_pt"]
                if abs(ot-t) < 10 and ol > l:
                    r = min(r, ol)
                if abs(ol-l) < 20 and ot > t:
                    b = min(b, ot)
            item["bounds_pt"] = [l, t, r, b]
            item["size"] = [r-l, b-t]
        error = None
        try:
            texts = self.reader.read(image)
        except OCRUnavailable as exc:
            texts = []; error = str(exc)
        for k, box in enumerate(texts):
            l, t = rect.px_to_pt(*box.bounds[:2]); r, b = rect.px_to_pt(*box.bounds[2:])
            cx, cy = (l+r)/2, (t+b)/2
            # Do not turn the badge's own OCR numeral into a label.
            owners = [i for i in items if i["type"] == "vc_item" and i["bounds_pt"][0] <= cx <= i["bounds_pt"][2]
                      and i["bounds_pt"][1] <= cy <= i["bounds_pt"][3]]
            if owners:
                owner = min(owners, key=lambda i: (i["center"][0]-cx)**2+(i["center"][1]-cy)**2)
                if box.text.strip() != owner["id"][2:]:
                    owner["text"] = box.text if owner["text"] == owner["id"][2:] else owner["text"]+' '+box.text
            else:
                items.append(ui_element(f"t{k}", box.text, "text", (l, t, r, b), confidence=box.confidence))
        for item in items:
            l, t, r, b = item["bounds_pt"]
            item["bounds"] = f"[{l:g},{t:g}][{r:g},{b:g}]"
        position = find_pointer(image, rect)
        return {"elements": items, "pointer": dict(zip(("x", "y", "confidence"), position)) if position else None,
                "tap_offset_pt": list(self.tap_offset), "ocr_available": error is None, "ocr_error": error,
                "latency_ms": (time.perf_counter()-start)*1000}

    @staticmethod
    def annotate(image, result):
        image = image.copy().convert("RGB")
        draw = ImageDraw.Draw(image)
        font = ImageFont.load_default(size=max(14, round(image.width/40)))
        sx, sy = image.width/375, image.height/812
        for item in result["elements"]:
            l,t,r,b = item["bounds_pt"]
            draw.rectangle((l*sx,t*sy,r*sx,b*sy), outline="cyan", width=2)
            draw.text((l*sx+2,t*sy+2), item["id"], font=font,
                      fill="black", stroke_fill="cyan", stroke_width=2)
            x,y = item["center"]
            draw.ellipse((x*sx-4,y*sy-4,x*sx+4,y*sy+4),fill="cyan")
        if result['pointer']:
            x, y = result['pointer']['x']*sx, result['pointer']['y']*sy
            radius = 17*sx
            draw.ellipse((x-radius,y-radius,x+radius,y+radius),outline='yellow',width=3)
        return image


def password_prompt(result):
    # Match words, not substrings ("Kodak" is not "Kod"). Include merged labels.
    return any(re.search(r"\b(password|passcode|hasło|kod)\b", item["text"], re.IGNORECASE)
               for item in result["elements"])
