"""Estimated OCR geometry. Inputs and output bounds are phone points, xyxy.

No pixel-to-point conversion is implicit. Bounds describe text, not a hit area.
"""

from dataclasses import dataclass, field
from difflib import SequenceMatcher
from itertools import combinations
import math
import unicodedata
from collections.abc import Mapping, Sequence

from .captions import Element

Bounds = tuple[float, float, float, float]


@dataclass(frozen=True)
class OCRBox:
    text: str = field(repr=False)
    bounds: Bounds
    confidence: float = 1.0

    def __post_init__(self):
        x1, y1, x2, y2 = self.bounds
        if not all(math.isfinite(v) for v in self.bounds) or x2 <= x1 or y2 <= y1:
            raise ValueError("OCR bounds must be finite positive-area xyxy rectangles")
        if not math.isfinite(self.confidence) or not 0 <= self.confidence <= 1:
            raise ValueError("OCR confidence must be in [0, 1]")


@dataclass(frozen=True)
class GeometryMatch:
    element: Element
    bounds: Bounds | None
    center: tuple[float, float] | None
    confidence: float
    ocr_indices: tuple[int, ...] = ()
    source: str = "ocr_estimated"


def _normal(text: str) -> str:
    text = unicodedata.normalize("NFKD", text.casefold())
    return " ".join("".join(c if c.isalnum() else " " for c in text
                            if not unicodedata.combining(c)).split())


def _similarity(label: str, text: str) -> float:
    a, b = _normal(label), _normal(text)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    # A suffix word alone is not evidence for the caption's leading label.
    if a.endswith(" " + b):
        return 0.0
    # A clipped OCR label may omit the ellipsis. Require enough visible text.
    if len(b) >= 4 and a.startswith(b) and len(b) / len(a) >= 0.45:
        return 0.86
    if label.rstrip().endswith(("…", "...")) and len(a) >= 4 and b.startswith(a):
        return 0.86
    return SequenceMatcher(None, a, b).ratio()


def _same_row(a: Bounds, b: Bounds) -> bool:
    overlap = min(a[3], b[3]) - max(a[1], b[1])
    return overlap >= 0.5 * min(a[3] - a[1], b[3] - b[1])


def _union(boxes: Sequence[OCRBox], indices: tuple[int, ...]) -> Bounds:
    return (min(boxes[i].bounds[0] for i in indices), min(boxes[i].bounds[1] for i in indices),
            max(boxes[i].bounds[2] for i in indices), max(boxes[i].bounds[3] for i in indices))


def match_to_ocr(
    items: Sequence[Element], ocr_boxes: Sequence[OCRBox], *,
    row_hints: Mapping[str, float] | None = None, min_similarity: float = 0.72,
) -> list[GeometryMatch]:
    """Align leading labels, merging up to four adjacent boxes on the same row.

    Exact matches win over fuzzy/truncated ones. Same-row value/state evidence
    breaks label ties; optional UID->y hints favor previously known rows. A box
    is assigned only once. Near ties reduce confidence (not a probability).
    Missing labels return bounds/center None, confidence 0. AX traversal order
    is deliberately not assumed to be spatial order.
    """
    if not 0 <= min_similarity <= 1:
        raise ValueError("min_similarity must be in [0, 1]")
    row_hints = row_hints or {}
    if not all(math.isfinite(v) for v in row_hints.values()):
        raise ValueError("row hints must be finite phone points")
    order = sorted(range(len(ocr_boxes)), key=lambda i: (ocr_boxes[i].bounds[1], ocr_boxes[i].bounds[0]))
    groups = {(i,) for i in order}
    for i in order:
        # Extend only through nearest neighbors; never combine distant columns.
        neighbors = sorted((j for j in order if ocr_boxes[j].bounds[0] >= ocr_boxes[i].bounds[2]
                            and _same_row(ocr_boxes[i].bounds, ocr_boxes[j].bounds)),
                           key=lambda j: ocr_boxes[j].bounds[0])
        group = [i]
        for j in neighbors[:3]:
            prev = ocr_boxes[group[-1]].bounds
            curr = ocr_boxes[j].bounds
            if curr[0] - prev[2] > 2.5 * max(prev[3] - prev[1], curr[3] - curr[1]):
                break
            if not all(_same_row(ocr_boxes[k].bounds, curr) for k in group):
                break
            group.append(j)
            groups.add(tuple(group))

    candidates = []
    for k, item in enumerate(items):
        for indices in sorted(groups):
            text = " ".join(ocr_boxes[i].text for i in indices)
            similarity = _similarity(item.label, text)
            if similarity < min_similarity or not _normal(item.label):
                continue
            bounds = _union(ocr_boxes, indices)
            score = similarity * min(ocr_boxes[i].confidence for i in indices)
            evidence = {_normal(s) for s in item.states}
            if item.value:
                evidence.add(_normal(item.value))
            if evidence and any(_normal(box.text) in evidence and _same_row(bounds, box.bounds)
                                for j, box in enumerate(ocr_boxes) if j not in indices):
                score += 0.06
            if item.uid in row_hints:
                cy = (bounds[1] + bounds[3]) / 2
                height = max(bounds[3] - bounds[1], 1)
                score -= min(0.25, abs(cy - row_hints[item.uid]) / height * 0.04)
            candidates.append((score, k, indices, bounds))

    # Ambiguity includes both one element with two locations and two elements
    # competing for one location. Overlapping word/phrase candidates are not
    # distinct locations when comparing alternatives for the same element.
    ambiguous = set()
    for a, b in combinations(candidates, 2):
        if abs(a[0] - b[0]) > 0.05:
            continue
        overlap = set(a[2]) & set(b[2])
        if (a[1] == b[1] and not overlap) or (a[1] != b[1] and overlap):
            ambiguous.update(((a[1], a[2]), (b[1], b[2])))
    assigned, used = {}, set()
    for score, k, indices, bounds in sorted(candidates, key=lambda c: (-c[0], c[1], c[2])):
        if k in assigned or used.intersection(indices):
            continue
        confidence = min(1.0, score) * (0.6 if (k, indices) in ambiguous else 1.0)
        assigned[k] = GeometryMatch(items[k], bounds,
                                   ((bounds[0] + bounds[2]) / 2, (bounds[1] + bounds[3]) / 2),
                                   confidence, indices)
        used.update(indices)
    return [assigned.get(k, GeometryMatch(item, None, None, 0.0)) for k, item in enumerate(items)]
