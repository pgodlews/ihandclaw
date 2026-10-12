"""Screen graph and action-matching engine for the offline replay simulator.

A ScreenGraph models recorded phone states as nodes and agent actions as edges.
Incoming actions (tap, key, type, etc.) are matched against transitions from the
current screen. If an action matches (within element bbox or configurable tolerance),
the simulator transitions to the next screen. Otherwise it stays on the current screen
and logs an 'off-path' event.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
import json
import math
from pathlib import Path
from typing import Any

from .manifest import REFERENCE_PT, UIElement, is_point_in_bounds, parse_bounds


# Spellings the host accepts for one key (iphone_hand.keys), mapped to one canonical name.
_COMBO_ALIASES = {"command": "cmd", "gui": "cmd", "control": "ctrl", "option": "alt", "opt": "alt",
                  "return": "enter", "escape": "esc"}
_MODIFIERS = {"cmd", "ctrl", "alt", "shift", "fn", "rcmd", "rctrl", "ralt", "rshift"}


def normalize_combo(combo: str) -> str:
    """Normalize keyboard shortcut combo (e.g. 'Shift+Command+Space' -> 'cmd+shift+space')."""
    if not combo:
        return ""
    parts = [p.strip().lower() for p in combo.split("+") if p.strip()]
    parts = [_COMBO_ALIASES.get(p, p) for p in parts]
    modifiers = sorted(set(p for p in parts if p in _MODIFIERS))
    keys = [p for p in parts if p not in _MODIFIERS]
    return "+".join(modifiers + keys)


def normalize_text(text: str) -> str:
    """Normalize text for comparison (stripped, lowercase)."""
    return text.strip().lower() if text else ""


@dataclass
class Transition:
    """Directed edge in the screen graph representing an action."""
    transition_id: str
    source_id: str
    target_id: str
    action_name: str
    action_args: dict[str, Any] = field(default_factory=dict)
    target_element: dict[str, Any] | None = None
    response_result: dict[str, Any] = field(default_factory=dict)
    tap_tolerance_pt: float = 15.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Transition:
        return cls(**data)


@dataclass
class ScreenNode:
    """Node in the screen graph representing an iPhone screen state."""
    screen_id: str
    frame_path: str
    elements: list[dict[str, Any]] = field(default_factory=list)
    pointer: dict[str, Any] | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    outgoing_transitions: list[Transition] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "screen_id": self.screen_id,
            "frame_path": self.frame_path,
            "elements": self.elements,
            "pointer": self.pointer,
            "metadata": self.metadata,
            "outgoing_transitions": [t.to_dict() for t in self.outgoing_transitions],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ScreenNode:
        transitions = [Transition.from_dict(t) for t in data.get("outgoing_transitions", [])]
        return cls(
            screen_id=data["screen_id"],
            frame_path=data["frame_path"],
            elements=data.get("elements", []),
            pointer=data.get("pointer"),
            metadata=data.get("metadata", {}),
            outgoing_transitions=transitions,
        )

    def find_element_at(self, x: float, y: float, tolerance: float = 0.0) -> dict[str, Any] | None:
        """Find the smallest element whose bounds enclose point (x, y)."""
        candidates = []
        for elem in self.elements:
            bounds = elem.get("bounds_pt") or parse_bounds(elem.get("bounds", ""))
            if is_point_in_bounds((x, y), bounds, tolerance=tolerance):
                w = bounds[2] - bounds[0]
                h = bounds[3] - bounds[1]
                area = w * h if w > 0 and h > 0 else float("inf")
                candidates.append((area, elem))
        if candidates:
            candidates.sort(key=lambda c: c[0])
            return candidates[0][1]
        return None

    def find_element_by_id(self, elem_id: str) -> dict[str, Any] | None:
        """Find an element by its ID (e.g. 'vc1', 't0')."""
        for elem in self.elements:
            if elem.get("id") == elem_id:
                return elem
        return None

    def find_element_by_text(self, text: str, nth: int = 0) -> dict[str, Any] | None:
        """Find an element matching text."""
        norm = normalize_text(text)
        if not norm:
            return None
        matches = []
        for elem in self.elements:
            elem_text = normalize_text(elem.get("text", ""))
            if norm == elem_text or norm in elem_text:
                matches.append(elem)
        if 0 <= nth < len(matches):
            return matches[nth]
        return None


class ScreenGraph:
    """Directed graph of screens and transitions with fuzzy action matching."""

    def __init__(self, initial_screen_id: str | None = None, screen_size_pt: list[int] | None = None):
        self.nodes: dict[str, ScreenNode] = {}
        self.initial_screen_id: str | None = initial_screen_id
        # Point space of every frame, element and recorded coordinate in this graph.
        self.screen_size_pt: list[int] = list(screen_size_pt or REFERENCE_PT)

    def add_node(self, node: ScreenNode) -> None:
        self.nodes[node.screen_id] = node
        if self.initial_screen_id is None:
            self.initial_screen_id = node.screen_id

    def get_node(self, screen_id: str) -> ScreenNode | None:
        return self.nodes.get(screen_id)

    def add_transition(self, transition: Transition) -> None:
        source = self.nodes.get(transition.source_id)
        if not source:
            raise KeyError(f"Source screen '{transition.source_id}' does not exist in graph")
        if transition.target_id not in self.nodes:
            raise KeyError(f"Target screen '{transition.target_id}' does not exist in graph")
        source.outgoing_transitions.append(transition)

    def match_action(
        self,
        current_screen_id: str,
        action_name: str,
        action_args: dict[str, Any],
        tolerance_pt: float = 15.0,
    ) -> tuple[ScreenNode, Transition] | None:
        """Find a transition from the current screen that matches the incoming action.

        Returns (next_screen_node, transition) on match, or None if off-path.
        """
        current_node = self.get_node(current_screen_id)
        if not current_node:
            return None

        candidates: list[tuple[float, Transition]] = []

        for trans in current_node.outgoing_transitions:
            match_score = self._score_match(current_node, trans, action_name, action_args, tolerance_pt)
            if match_score is not None:
                candidates.append((match_score, trans))

        if not candidates:
            return None

        # Sort by match score (lower score = better/more specific match)
        candidates.sort(key=lambda c: c[0])
        best_trans = candidates[0][1]
        next_node = self.get_node(best_trans.target_id)
        if next_node:
            return next_node, best_trans
        return None

    def _score_match(
        self,
        node: ScreenNode,
        trans: Transition,
        action_name: str,
        args: dict[str, Any],
        tolerance_pt: float,
    ) -> float | None:
        """Score how well an incoming action matches a transition. Returns score or None if no match."""
        # 1. Tap action (x, y)
        if action_name in {"tap", "long_press"}:
            x = float(args.get("x", 0.0))
            y = float(args.get("y", 0.0))

            # Matching against recorded tap / long_press
            if trans.action_name in {"tap", "long_press"}:
                rec_x = float(trans.action_args.get("x", 0.0))
                rec_y = float(trans.action_args.get("y", 0.0))
                dist = math.hypot(x - rec_x, y - rec_y)

                # Check element bbox match first (higher priority)
                if trans.target_element:
                    bounds = trans.target_element.get("bounds_pt") or parse_bounds(
                        trans.target_element.get("bounds", "")
                    )
                    if is_point_in_bounds((x, y), bounds, tolerance=tolerance_pt):
                        return 0.1 + (dist / 1000.0)

                # Check proximity to recorded coordinate
                if dist <= tolerance_pt:
                    return 1.0 + dist

            # Matching against recorded tap_element
            elif trans.action_name == "tap_element" and trans.target_element:
                bounds = trans.target_element.get("bounds_pt") or parse_bounds(
                    trans.target_element.get("bounds", "")
                )
                if is_point_in_bounds((x, y), bounds, tolerance=tolerance_pt):
                    return 0.2

            # Matching against recorded tap_text
            elif trans.action_name == "tap_text" and trans.target_element:
                bounds = trans.target_element.get("bounds_pt") or parse_bounds(
                    trans.target_element.get("bounds", "")
                )
                if is_point_in_bounds((x, y), bounds, tolerance=tolerance_pt):
                    return 0.3

            return None

        # 2. Tap element by ID (id)
        if action_name == "tap_element":
            elem_id = str(args.get("id", ""))
            if trans.action_name == "tap_element":
                if str(trans.action_args.get("id", "")) == elem_id:
                    return 0.0
            if trans.target_element and str(trans.target_element.get("id", "")) == elem_id:
                return 0.1
            # If incoming id's element contains the recorded tap point
            elem = node.find_element_by_id(elem_id)
            if elem and trans.action_name == "tap":
                rec_x = float(trans.action_args.get("x", 0.0))
                rec_y = float(trans.action_args.get("y", 0.0))
                bounds = elem.get("bounds_pt") or parse_bounds(elem.get("bounds", ""))
                if is_point_in_bounds((rec_x, rec_y), bounds, tolerance=tolerance_pt):
                    return 0.5
            return None

        # 3. Tap text (text, nth)
        if action_name == "tap_text":
            text = normalize_text(str(args.get("text", "")))
            if not text:
                return None  # "" is a substring of every label; the daemon rejects it too.
            if trans.action_name == "tap_text":
                rec_text = normalize_text(str(trans.action_args.get("text", "")))
                if text == rec_text or text in rec_text:
                    return 0.0
            if trans.target_element:
                elem_text = normalize_text(str(trans.target_element.get("text", "")))
                if text == elem_text or text in elem_text:
                    return 0.1
            return None

        # 4. Keyboard key combo (combo)
        if action_name == "key":
            combo = normalize_combo(str(args.get("combo", "")))
            if trans.action_name == "key":
                rec_combo = normalize_combo(str(trans.action_args.get("combo", "")))
                if combo == rec_combo:
                    return 0.0
            if combo == "cmd+h" and trans.action_name == "home":
                return 0.0
            return None

        # 5. Type text (text)
        if action_name == "type":
            text = str(args.get("text", ""))
            rec_text = str(trans.action_args.get("text", "")) if trans.action_name == "type" else None
            if rec_text is not None:
                if text == rec_text:
                    return 0.0
                if normalize_text(text) == normalize_text(rec_text):
                    return 0.2
            return None

        # 6. Launch app (app)
        if action_name == "launch":
            app = normalize_text(str(args.get("app", "")))
            if trans.action_name == "launch":
                rec_app = normalize_text(str(trans.action_args.get("app", "")))
                if app == rec_app:
                    return 0.0
            return None

        # 7. Home button
        if action_name == "home":
            if trans.action_name == "home":
                return 0.0
            if trans.action_name == "key" and normalize_combo(trans.action_args.get("combo", "")) == "cmd+h":
                return 0.1
            return None

        # 8. Back gesture/action
        if action_name == "back":
            if trans.action_name == "back":
                return 0.0
            return None

        # 9. Scroll (dy, dx)
        if action_name in {"scroll", "scroll_until"}:
            dy = int(args.get("dy", -3))
            if trans.action_name in {"scroll", "scroll_until"}:
                rec_dy = int(trans.action_args.get("dy", -3))
                # Match direction
                if (dy > 0 and rec_dy > 0) or (dy < 0 and rec_dy < 0):
                    return abs(dy - rec_dy) / 100.0
            return None

        # 10. Drag (x1, y1, x2, y2)
        if action_name == "drag" and trans.action_name == "drag":
            x1, y1 = float(args.get("x1", 0)), float(args.get("y1", 0))
            x2, y2 = float(args.get("x2", 0)), float(args.get("y2", 0))
            rx1, ry1 = float(trans.action_args.get("x1", 0)), float(trans.action_args.get("y1", 0))
            rx2, ry2 = float(trans.action_args.get("x2", 0)), float(trans.action_args.get("y2", 0))
            start_dist = math.hypot(x1 - rx1, y1 - ry1)
            end_dist = math.hypot(x2 - rx2, y2 - ry2)
            if start_dist <= tolerance_pt and end_dist <= tolerance_pt:
                return (start_dist + end_dist) / 10.0
            return None

        # 11. Run shortcut
        if action_name == "run_shortcut":
            name = normalize_text(str(args.get("name", "")))
            if trans.action_name == "run_shortcut":
                rec_name = normalize_text(str(trans.action_args.get("name", "")))
                if name == rec_name:
                    return 0.0
            return None

        return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "initial_screen_id": self.initial_screen_id,
            "screen_size_pt": self.screen_size_pt,
            "nodes": {k: node.to_dict() for k, node in self.nodes.items()},
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ScreenGraph:
        graph = cls(initial_screen_id=data.get("initial_screen_id"),
                    screen_size_pt=data.get("screen_size_pt"))
        for node_id, node_data in data.get("nodes", {}).items():
            graph.add_node(ScreenNode.from_dict(node_data))
        return graph

    def save(self, path: Path | str) -> None:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(self.to_dict(), indent=2))

    @classmethod
    def load(cls, path: Path | str) -> ScreenGraph:
        data = json.loads(Path(path).read_text())
        return cls.from_dict(data)
