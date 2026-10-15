"""Scenario loader, validator, and schema definitions for the benchmark suite."""

from __future__ import annotations
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .yaml_util import load_yaml
from .resets import RESET_RECIPES


DEFAULT_SCENARIOS_DIR = Path(__file__).parent / "scenarios"

FORBIDDEN_KEYWORDS = [
    "password", "passcode", "hasło", "kod", "pin",
    "credit card", "apple pay", "purchase", "buy now",
    "delete account", "erase all", "reset all", "sign out",
    "send sms", "send message", "call", "dial",
]


class ScenarioValidationError(ValueError):
    """Raised when a scenario YAML fails schema or safety validation."""
    pass


@dataclass
class StartState:
    home: bool = True
    launch: str | None = None
    settle_s: float = 1.0
    reset: list[str] = field(default_factory=list)


@dataclass
class Budget:
    max_steps: int = 20
    max_time_seconds: float = 90.0


@dataclass
class Reliability:
    level: str = "high"  # "high", "medium", "low"
    note: str = ""


@dataclass
class SafetyMetadata:
    touches_accounts: bool = False
    sends_messages: bool = False
    makes_purchases: bool = False
    deletes_data: bool = False
    notes: str = ""


@dataclass
class Scenario:
    id: str
    title: str
    difficulty: str
    goal: str
    start_state: StartState
    budget: Budget
    predicates: dict[str, Any]
    reliability: Reliability
    safety: SafetyMetadata
    path: Path | None = None

    def validate(self) -> None:
        """Validate scenario integrity and enforce safety rules."""
        if not self.id:
            raise ScenarioValidationError("Scenario id cannot be empty")
        if self.difficulty not in ("easy", "medium", "hard", "gold"):
            raise ScenarioValidationError(f"Invalid difficulty: {self.difficulty}")
        if not self.goal:
            raise ScenarioValidationError("Scenario goal cannot be empty")
        if not isinstance(self.start_state.reset, list) or any(
            not isinstance(name, str) or name not in RESET_RECIPES
            for name in self.start_state.reset
        ):
            raise ScenarioValidationError("start_state.reset must be a list of known reset recipes")
        if len(set(self.start_state.reset)) != len(self.start_state.reset):
            raise ScenarioValidationError("start_state.reset cannot contain duplicate recipes")
        if not 0 <= self.start_state.settle_s <= 60:
            raise ScenarioValidationError("start_state.settle_s must be finite and in 0..60 seconds")
        if self.budget.max_steps <= 0:
            raise ScenarioValidationError(f"max_steps must be positive: {self.budget.max_steps}")
        if self.budget.max_time_seconds <= 0:
            raise ScenarioValidationError(f"max_time_seconds must be positive: {self.budget.max_time_seconds}")
        if not self.predicates:
            raise ScenarioValidationError("Scenario must define predicates")
        if self.reliability.level not in ("high", "medium", "low"):
            raise ScenarioValidationError(f"Invalid reliability level: {self.reliability.level}")

        # Enforce safety constraints: no passwords, no purchasing, no sending messages, no deleting data
        if self.safety.touches_accounts:
            raise ScenarioValidationError(f"Safety violation in {self.id}: touches_accounts must be false")
        if self.safety.sends_messages:
            raise ScenarioValidationError(f"Safety violation in {self.id}: sends_messages must be false")
        if self.safety.makes_purchases:
            raise ScenarioValidationError(f"Safety violation in {self.id}: makes_purchases must be false")
        if self.safety.deletes_data:
            raise ScenarioValidationError(f"Safety violation in {self.id}: deletes_data must be false")

        goal_lower = self.goal.lower()
        for kw in FORBIDDEN_KEYWORDS:
            if kw in goal_lower:
                raise ScenarioValidationError(
                    f"Safety violation in {self.id}: goal mentions forbidden action '{kw}'"
                )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "difficulty": self.difficulty,
            "goal": self.goal,
            "start_state": {
                "home": self.start_state.home,
                "launch": self.start_state.launch,
                "settle_s": self.start_state.settle_s,
                "reset": list(self.start_state.reset),
            },
            "budget": {
                "max_steps": self.budget.max_steps,
                "max_time_seconds": self.budget.max_time_seconds,
            },
            "predicates": self.predicates,
            "reliability": {
                "level": self.reliability.level,
                "note": self.reliability.note,
            },
            "safety": {
                "touches_accounts": self.safety.touches_accounts,
                "sends_messages": self.safety.sends_messages,
                "makes_purchases": self.safety.makes_purchases,
                "deletes_data": self.safety.deletes_data,
                "notes": self.safety.notes,
            },
        }


def load_scenario(path: Path | str) -> Scenario:
    p = Path(path)
    content = p.read_text(encoding="utf-8")
    raw = load_yaml(content)
    if not isinstance(raw, dict):
        raise ScenarioValidationError(f"Scenario at {p} must be a YAML mapping")

    raw_start = raw.get("start_state", {})
    start_state = StartState(
        home=bool(raw_start.get("home", True)),
        launch=raw_start.get("launch"),
        settle_s=float(raw_start.get("settle_s", 1.0)),
        reset=raw_start.get("reset", []),
    )

    raw_budget = raw.get("budget", {})
    budget = Budget(
        max_steps=int(raw_budget.get("max_steps", 20)),
        max_time_seconds=float(raw_budget.get("max_time_seconds", 90.0)),
    )

    raw_rel = raw.get("reliability", {})
    reliability = Reliability(
        level=str(raw_rel.get("level", "high")),
        note=str(raw_rel.get("note", "")),
    )

    raw_safety = raw.get("safety", {})
    safety = SafetyMetadata(
        touches_accounts=bool(raw_safety.get("touches_accounts", False)),
        sends_messages=bool(raw_safety.get("sends_messages", False)),
        makes_purchases=bool(raw_safety.get("makes_purchases", False)),
        deletes_data=bool(raw_safety.get("deletes_data", False)),
        notes=str(raw_safety.get("notes", "")),
    )

    scenario = Scenario(
        id=str(raw.get("id", p.stem)),
        title=str(raw.get("title", p.stem)),
        difficulty=str(raw.get("difficulty", "medium")),
        goal=str(raw.get("goal", "")),
        start_state=start_state,
        budget=budget,
        predicates=raw.get("predicates", {}),
        reliability=reliability,
        safety=safety,
        path=p,
    )
    scenario.validate()
    return scenario


def load_all_scenarios(directory: Path | str | None = None) -> list[Scenario]:
    d = Path(directory) if directory is not None else DEFAULT_SCENARIOS_DIR
    if not d.exists():
        raise FileNotFoundError(f"Scenarios directory not found: {d}")
    yaml_files = sorted(list(d.glob("*.yaml")) + list(d.glob("*.yml")))
    scenarios = [load_scenario(f) for f in yaml_files]
    return scenarios
