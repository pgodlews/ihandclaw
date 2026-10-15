"""Success predicates evaluated against /api/elements (OCR) and screenshot frames."""

from __future__ import annotations
from dataclasses import dataclass, field
import re
from typing import Any
from PIL import Image, ImageStat


@dataclass
class PredicateResult:
    passed: bool
    description: str
    details: dict[str, Any] = field(default_factory=dict)
    score: float = 0.0
    sub_results: list[PredicateResult] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.score == 0.0 and self.passed:
            self.score = 1.0


def calculate_image_luminance(image: Image.Image) -> float:
    """Calculate average perceptual luminance of an image or phone screen."""
    # Convert to RGB if not already
    rgb = image.convert("RGB")
    stat = ImageStat.Stat(rgb)
    # ITU-R BT.601 perceptual luminance weights: 0.299 R + 0.587 G + 0.114 B
    r, g, b = stat.mean[0], stat.mean[1], stat.mean[2]
    return 0.299 * r + 0.587 * g + 0.114 * b


class PredicateEvaluator:
    """Evaluates predicate configurations against elements and frames."""

    @classmethod
    def evaluate(
        cls,
        spec: dict[str, Any],
        elements: list[dict[str, Any]],
        screenshot: Image.Image | None = None,
    ) -> PredicateResult:
        ptype = spec.get("type", "unknown")

        if ptype == "all_of":
            children_specs = spec.get("predicates", [])
            results = [cls.evaluate(child, elements, screenshot) for child in children_specs]
            all_passed = all(r.passed for r in results) if results else True
            passed_count = sum(1 for r in results if r.passed)
            total = len(results) if results else 1
            score = passed_count / total
            failed_reasons = [r.description for r in results if not r.passed]
            desc = "All predicates passed" if all_passed else f"Failed {len(failed_reasons)} sub-predicate(s): {'; '.join(failed_reasons)}"
            return PredicateResult(
                passed=all_passed,
                description=desc,
                score=score,
                sub_results=results,
                details={"passed_count": passed_count, "total": total},
            )

        elif ptype == "any_of":
            children_specs = spec.get("predicates", [])
            results = [cls.evaluate(child, elements, screenshot) for child in children_specs]
            any_passed = any(r.passed for r in results) if results else False
            passed_results = [r for r in results if r.passed]
            desc = (
                f"Passed via: {passed_results[0].description}"
                if any_passed
                else f"None of {len(results)} alternatives matched"
            )
            return PredicateResult(
                passed=any_passed,
                description=desc,
                score=1.0 if any_passed else 0.0,
                sub_results=results,
                details={"matched_count": len(passed_results)},
            )

        elif ptype == "none_of":
            children_specs = spec.get("predicates", [])
            results = [cls.evaluate(child, elements, screenshot) for child in children_specs]
            none_passed = not any(r.passed for r in results)
            desc = "None matched as expected" if none_passed else "One or more forbidden predicates matched"
            return PredicateResult(
                passed=none_passed,
                description=desc,
                score=1.0 if none_passed else 0.0,
                sub_results=results,
            )

        elif ptype == "ocr_text_contains":
            target = spec.get("text", "")
            case_sensitive = spec.get("case_sensitive", False)
            elements_text = [str(el.get("text", "")) for el in elements]

            def match(t: str) -> bool:
                return (target in t) if case_sensitive else (target.lower() in t.lower())

            matched_elements = [el for el in elements if match(str(el.get("text", "")))]
            # Also check if text appears across whole screen concatenation
            full_corpus = " ".join(elements_text)
            full_match = match(full_corpus)

            passed = bool(matched_elements or full_match)
            desc = (
                f"Found OCR text containing '{target}'"
                if passed
                else f"Missing OCR text containing '{target}' (scanned {len(elements)} elements)"
            )
            return PredicateResult(
                passed=passed,
                description=desc,
                details={
                    "target": target,
                    "matched_count": len(matched_elements),
                    "full_match": full_match,
                    "sample_texts": elements_text[:10],
                },
            )

        elif ptype == "ocr_text_exact":
            target = spec.get("text", "")
            case_sensitive = spec.get("case_sensitive", False)

            def match(t: str) -> bool:
                t = t.strip()
                return (t == target) if case_sensitive else (t.lower() == target.lower())

            matched_elements = [el for el in elements if match(str(el.get("text", "")))]
            passed = len(matched_elements) > 0
            desc = (
                f"Found exact OCR text '{target}'"
                if passed
                else f"Missing exact OCR text '{target}'"
            )
            return PredicateResult(
                passed=passed,
                description=desc,
                details={"target": target, "matches": [el.get("id") for el in matched_elements]},
            )

        elif ptype == "ocr_text_matches":
            pattern_str = spec.get("pattern", "")
            regex = re.compile(pattern_str, re.IGNORECASE)
            matched_elements = [
                el for el in elements if regex.search(str(el.get("text", "")))
            ]
            full_corpus = " ".join(str(el.get("text", "")) for el in elements)
            full_match = bool(regex.search(full_corpus))
            passed = bool(matched_elements or full_match)
            desc = (
                f"Regex /{pattern_str}/ matched OCR elements"
                if passed
                else f"Regex /{pattern_str}/ found no match in elements"
            )
            return PredicateResult(
                passed=passed,
                description=desc,
                details={"pattern": pattern_str, "matched_count": len(matched_elements)},
            )

        elif ptype == "element_exists":
            target_text = spec.get("text")
            target_type = spec.get("type_filter") or spec.get("element_type")
            target_checked = spec.get("checked")
            target_selected = spec.get("selected")

            matches = []
            for el in elements:
                if target_type and el.get("type") != target_type:
                    continue
                if target_checked is not None and el.get("checked") != target_checked:
                    continue
                if target_selected is not None and el.get("selected") != target_selected:
                    continue
                if target_text:
                    if target_text.lower() not in str(el.get("text", "")).lower():
                        continue
                matches.append(el)

            passed = len(matches) > 0
            desc = f"Found {len(matches)} matching element(s)" if passed else "No element matched criteria"
            return PredicateResult(
                passed=passed,
                description=desc,
                details={"matches_count": len(matches)},
            )

        elif ptype == "screenshot_dark_mode":
            if screenshot is None:
                return PredicateResult(
                    passed=False,
                    description="Screenshot required for dark mode check, but none provided",
                )
            luminance = calculate_image_luminance(screenshot)
            max_lum = float(spec.get("max_mean_luminance", 120.0))
            passed = luminance <= max_lum
            desc = (
                f"Frame luminance {luminance:.1f} <= {max_lum:.1f} (dark mode active)"
                if passed
                else f"Frame luminance {luminance:.1f} > {max_lum:.1f} (light mode active)"
            )
            return PredicateResult(
                passed=passed,
                description=desc,
                details={"luminance": luminance, "threshold": max_lum},
            )

        else:
            return PredicateResult(
                passed=False,
                description=f"Unknown predicate type: '{ptype}'",
            )
