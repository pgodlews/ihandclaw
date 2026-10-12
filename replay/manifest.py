"""Data structures and serialization for recorded replay sessions and manifests.

Follows the droidclaw UIElement specification and docs/SPEC.md (sections 6 and 8).
Phone coordinates are always in points (iPhone XS: 375x812 pt).
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import json
from pathlib import Path
import re
from typing import Any


@dataclass
class UIElement:
    """DroidClaw-compatible UIElement representation (SPEC section 8)."""
    id: str
    text: str
    type: str
    bounds: str
    center: list[float]
    size: list[float]
    clickable: bool = True
    editable: bool = False
    enabled: bool = True
    checked: bool = False
    focused: bool = False
    selected: bool = False
    scrollable: bool = False
    longClickable: bool = True
    password: bool = False
    hint: str = ""
    action: str = "tap"
    parent: str = ""
    depth: int = 0
    bounds_pt: list[float] = field(default_factory=list)

    def __post_init__(self):
        if not self.bounds_pt:
            self.bounds_pt = parse_bounds(self.bounds)
        if not self.center:
            l, t, r, b = self.bounds_pt
            self.center = [(l + r) / 2.0, (t + b) / 2.0]
        if not self.size:
            l, t, r, b = self.bounds_pt
            self.size = [max(0.0, r - l), max(0.0, b - t)]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> UIElement:
        clean = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        return cls(**clean)


def parse_bounds(bounds: str | list[float] | tuple[float, ...]) -> list[float]:
    """Parse bounds formatted as '[left,top][right,bottom]' or [l, t, r, b] into [l, t, r, b] float list."""
    if isinstance(bounds, (list, tuple)):
        return [float(x) for x in bounds]
    if isinstance(bounds, str):
        # Format: "[10,20][50,60]" or "[10.5, 20.2, 50.1, 60.3]"
        numbers = re.findall(r"[-+]?\d*\.?\d+", bounds)
        if len(numbers) == 4:
            return [float(n) for n in numbers]
    return [0.0, 0.0, 0.0, 0.0]


def is_point_in_bounds(
    pt: tuple[float, float] | list[float],
    bounds: str | list[float] | tuple[float, ...],
    tolerance: float = 0.0,
) -> bool:
    """Check if point (x, y) is within bounds [l, t, r, b], with optional tolerance padding."""
    x, y = pt[0], pt[1]
    l, t, r, b = parse_bounds(bounds)
    return (l - tolerance) <= x <= (r + tolerance) and (t - tolerance) <= y <= (b + tolerance)


@dataclass
class ActionRecord:
    """A recorded action with name and arguments."""
    name: str
    args: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ActionRecord:
        return cls(name=data["name"], args=dict(data.get("args", {})))


@dataclass
class StepRecord:
    """One interaction step in a recorded session."""
    step: int
    timestamp: float
    action: ActionRecord
    pre_frame: str
    elements_file: str
    post_frame: str
    response: dict[str, Any] = field(default_factory=dict)
    post_elements_file: str | None = None

    def to_dict(self) -> dict[str, Any]:
        res = asdict(self)
        res["action"] = self.action.to_dict() if isinstance(self.action, ActionRecord) else self.action
        return res

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> StepRecord:
        action_data = data["action"]
        action = ActionRecord.from_dict(action_data) if isinstance(action_data, dict) else action_data
        return cls(
            step=int(data["step"]),
            timestamp=float(data.get("timestamp", 0.0)),
            action=action,
            pre_frame=str(data["pre_frame"]),
            elements_file=str(data["elements_file"]),
            post_frame=str(data["post_frame"]),
            response=dict(data.get("response", {})),
            post_elements_file=data.get("post_elements_file"),
        )


@dataclass
class SessionManifest:
    """Manifest describing a recorded session dataset."""
    session_id: str
    created_at: str
    daemon_url: str
    version: str = "1.0"
    screen_size_pt: list[int] = field(default_factory=lambda: [375, 812])
    steps: list[StepRecord] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "session_id": self.session_id,
            "created_at": self.created_at,
            "daemon_url": self.daemon_url,
            "screen_size_pt": self.screen_size_pt,
            "steps": [s.to_dict() for s in self.steps],
        }

    def save(self, path: Path | str) -> None:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(self.to_dict(), indent=2))

    @classmethod
    def load(cls, path: Path | str) -> SessionManifest:
        data = json.loads(Path(path).read_text())
        steps = [StepRecord.from_dict(s) for s in data.get("steps", [])]
        return cls(
            version=data.get("version", "1.0"),
            session_id=data.get("session_id", "session"),
            created_at=data.get("created_at", datetime.now(timezone.utc).isoformat()),
            daemon_url=data.get("daemon_url", "http://iphone-hand.local:8790"),
            screen_size_pt=data.get("screen_size_pt", [375, 812]),
            steps=steps,
        )
