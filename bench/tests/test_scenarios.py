"""Tests for benchmark scenarios validation and schema safety."""

from pathlib import Path
import pytest

from bench.scenarios import (
    load_all_scenarios,
    load_scenario,
    ScenarioValidationError,
    FORBIDDEN_KEYWORDS,
)


def test_load_all_thirteen_scenarios():
    scenarios = load_all_scenarios()
    assert len(scenarios) == 13, f"Expected exactly 13 scenarios, got {len(scenarios)}"
    ids = [s.id for s in scenarios]
    assert len(ids) == len(set(ids)), "Scenario IDs must be unique"


def test_scenario_difficulty_progression():
    scenarios = load_all_scenarios()
    assert scenarios[0].difficulty == "gold"
    difficulties = [s.difficulty for s in scenarios[1:]]
    assert "easy" in difficulties
    assert "medium" in difficulties
    assert "hard" in difficulties
    # First 3 should be easy
    assert all(d == "easy" for d in difficulties[:3])
    # Next scenarios are medium
    assert any(d == "medium" for d in difficulties[3:8])
    # Last scenarios are hard
    assert all(d == "hard" for d in difficulties[8:])


def test_scenario_safety_constraints():
    scenarios = load_all_scenarios()
    for s in scenarios:
        assert s.safety.touches_accounts is False, f"{s.id} touches accounts"
        assert s.safety.sends_messages is False, f"{s.id} sends messages"
        assert s.safety.makes_purchases is False, f"{s.id} makes purchases"
        assert s.safety.deletes_data is False, f"{s.id} deletes data"
        goal_lower = s.goal.lower()
        for kw in FORBIDDEN_KEYWORDS:
            assert kw not in goal_lower, f"{s.id} goal mentions '{kw}'"


def test_scenario_budgets_and_reliability():
    scenarios = load_all_scenarios()
    for s in scenarios:
        assert s.budget.max_steps >= 5, f"{s.id} max_steps too low"
        assert s.budget.max_steps <= (60 if s.difficulty == "gold" else 30), f"{s.id} max_steps too high"
        assert s.budget.max_time_seconds >= 20.0
        assert s.reliability.level in ("high", "medium", "low")
        assert len(s.reliability.note) > 10, f"{s.id} needs informative reliability note"
        assert s.predicates, f"{s.id} must define predicates"


def test_scenario_validation_rejects_unsafe(tmp_path: Path):
    bad_yaml = tmp_path / "unsafe.yaml"
    bad_yaml.write_text(
        """
id: unsafe_test
title: "Unsafe Scenario"
difficulty: easy
goal: "Enter your password to sign in"
start_state:
  home: true
budget:
  max_steps: 10
  max_time_seconds: 30
predicates:
  type: ocr_text_contains
  text: "Welcome"
reliability:
  level: high
  note: "Unsafe"
safety:
  touches_accounts: true
  sends_messages: false
  makes_purchases: false
  deletes_data: false
""",
        encoding="utf-8",
    )
    with pytest.raises(ScenarioValidationError):
        load_scenario(bad_yaml)
