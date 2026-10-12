"""Unit tests for BenchmarkRunner, budget enforcement, recording, and reporting."""

import json
from pathlib import Path
import pytest

from bench.scenarios import load_scenario, Scenario, StartState, Budget, Reliability, SafetyMetadata
from bench.runner import BenchmarkRunner, StepRecorderClient, BudgetExceededError, dummy_agent
from bench.fake_daemon import FakeDaemon
from bench.daemon_client import DaemonClient


@pytest.fixture
def fake_env(tmp_path: Path):
    daemon = FakeDaemon()
    client = DaemonClient(fake_daemon=daemon)
    runner = BenchmarkRunner(daemon_client=client, output_dir=tmp_path / "runs")
    return daemon, client, runner, tmp_path


def test_runner_executes_scenario_success(fake_env):
    daemon, client, runner, tmp_path = fake_env
    scenario = load_scenario(Path("bench/scenarios/01_open_settings.yaml"))

    result = runner.run_scenario(scenario, dummy_agent)
    assert result.success is True
    assert result.steps == 1
    assert result.wall_time_s >= 0.0

    # Verify artifacts recorded
    run_dir = result.run_dir
    assert (run_dir / "step_000_frame.jpg").exists()
    assert (run_dir / "step_000_elements.json").exists()
    assert (run_dir / "step_001_frame.jpg").exists()
    assert (run_dir / "step_001_action.json").exists()
    assert (run_dir / "trace.jsonl").exists()
    assert (run_dir / "result.json").exists()

    result_data = json.loads((run_dir / "result.json").read_text(encoding="utf-8"))
    assert result_data["scenario_id"] == "01_open_settings"
    assert result_data["success"] is True


def test_runner_enforces_step_budget(fake_env):
    daemon, client, runner, tmp_path = fake_env

    # Create scenario with tight step budget of 2
    scenario = Scenario(
        id="tight_steps",
        title="Tight Step Budget",
        difficulty="easy",
        goal="Test step budget limit",
        start_state=StartState(home=True, launch=None, settle_s=0.0),
        budget=Budget(max_steps=2, max_time_seconds=30.0),
        predicates={"type": "ocr_text_contains", "text": "UnreachableTarget"},
        reliability=Reliability(level="high", note="Testing step budget limit"),
        safety=SafetyMetadata(),
    )

    def greedy_agent(recorder_client, scn):
        # Tries to take 5 steps
        for _ in range(5):
            recorder_client.tap(100, 100)

    result = runner.run_scenario(scenario, greedy_agent)
    assert result.success is False
    assert result.steps == 2
    assert "Step budget exceeded" in result.reason


def test_runner_enforces_time_budget(fake_env):
    daemon, client, runner, tmp_path = fake_env

    # Create scenario with very short time budget (0.05s)
    scenario = Scenario(
        id="tight_time",
        title="Tight Time Budget",
        difficulty="easy",
        goal="Test time budget limit",
        start_state=StartState(home=True, launch=None, settle_s=0.0),
        budget=Budget(max_steps=20, max_time_seconds=0.05),
        predicates={"type": "ocr_text_contains", "text": "UnreachableTarget"},
        reliability=Reliability(level="high", note="Testing time budget limit"),
        safety=SafetyMetadata(),
    )

    def slow_agent(recorder_client, scn):
        import time
        time.sleep(0.08)
        recorder_client.tap(100, 100)

    result = runner.run_scenario(scenario, slow_agent)
    assert result.success is False
    assert "Time budget exceeded" in result.reason


def test_runner_benchmark_summary_reports(fake_env):
    daemon, client, runner, tmp_path = fake_env
    scenario1 = load_scenario(Path("bench/scenarios/01_open_settings.yaml"))
    scenario2 = load_scenario(Path("bench/scenarios/03_open_calculator_clear.yaml"))

    results = runner.run_benchmark([scenario1, scenario2], dummy_agent)
    assert len(results) == 2
    assert all(r.success for r in results)

    json_file = runner.output_dir / "results.json"
    md_file = runner.output_dir / "summary.md"

    assert json_file.exists()
    assert md_file.exists()

    summary_json = json.loads(json_file.read_text(encoding="utf-8"))
    assert summary_json["total_scenarios"] == 2
    assert summary_json["passed"] == 2
    assert summary_json["success_rate_pct"] == 100.0

    summary_md = md_file.read_text(encoding="utf-8")
    assert "01_open_settings" in summary_md
    assert "03_open_calculator_clear" in summary_md
    assert "PASS" in summary_md
    assert "100.0%" in summary_md


def test_cli_command_template_formatting(fake_env):
    daemon, client, runner, tmp_path = fake_env
    scenario = load_scenario(Path("bench/scenarios/01_open_settings.yaml"))

    template = (
        'droidclaw --device iphone --task "{goal}" --model {model} '
        '--daemon-url {daemon_url} --max-steps {max_steps}'
    )
    formatted = template.format(
        goal=scenario.goal,
        scenario_id=scenario.id,
        model="google/gemma-4-26b-a4b",
        daemon_url=client.base_url,
        token="",
        max_steps=scenario.budget.max_steps,
        max_time=scenario.budget.max_time_seconds,
        run_dir=str(tmp_path),
    )
    assert 'droidclaw --device iphone --task "Open the Settings app on the iPhone."' in formatted
    assert "--model google/gemma-4-26b-a4b" in formatted
    assert "--max-steps 6" in formatted
