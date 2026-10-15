"""Benchmark runner for evaluating phone-driving agents on iPhone scenarios.

Supports pluggable agent commands (e.g. droidclaw CLI invocation template)
and Python callable agents. Manages state reset, budget enforcement,
per-step artifact recording, predicate evaluation, and reporting.
"""

from __future__ import annotations
import argparse
import contextlib
from dataclasses import replace
import secrets
import signal
import string
from .benchy import evaluate as evaluate_benchy, parse_usage, run_summary
import datetime
import importlib
import io
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import time
from typing import Any, Callable
from PIL import Image

from .scenarios import Scenario, load_all_scenarios, load_scenario, DEFAULT_SCENARIOS_DIR
from .predicates import PredicateEvaluator, PredicateResult
from .daemon_client import DaemonClient, DaemonError
from .fake_daemon import FakeDaemonServer, FakeDaemon
from .resets import NotesExitOptions, ResetRunner, SetupError


# Digits only: OCR misread letters in the Notes title font (l->I, q->a; measured 2026-10-12).
RUN_ID_ALPHABET = "23456789"


def actions_attempted(state, since):
    return any(e.get("time", 0) >= since for e in state.get("action_log", []))


def _mirroring(state):
    """True/False from the daemon state (real: airplay.mirroring; fake: mirroring); None if unknown."""
    value = (state.get("airplay") or {}).get("mirroring", state.get("mirroring"))
    return value if isinstance(value, bool) else None


class BudgetExceededError(RuntimeError):
    """Raised when an agent exceeds its step or time budget."""
    pass


class StepRecorderClient:
    """Wraps DaemonClient to automatically record every action as a benchmark step."""

    def __init__(
        self,
        base_client: DaemonClient,
        scenario: Scenario,
        run_dir: Path,
        time_limit_s: float,
        step_limit: int,
    ):
        self.base = base_client
        self.scenario = scenario
        self.run_dir = run_dir
        self.time_limit_s = time_limit_s
        self.step_limit = step_limit
        self.start_time = time.perf_counter()
        self.step_count = 0
        self.step_history: list[dict[str, Any]] = []
        self.early_success = False

        self.trace_file = run_dir / "trace.jsonl"
        self._trace_handle = open(self.trace_file, "a", encoding="utf-8")

    def elapsed_time(self) -> float:
        return time.perf_counter() - self.start_time

    def close(self) -> None:
        if hasattr(self, "_trace_handle") and not self._trace_handle.closed:
            self._trace_handle.close()

    def _pre_step_check(self) -> None:
        elapsed = self.elapsed_time()
        if elapsed > self.time_limit_s:
            raise BudgetExceededError(
                f"Time budget exceeded: {elapsed:.2f}s > {self.time_limit_s:.2f}s"
            )
        if self.step_count >= self.step_limit:
            raise BudgetExceededError(
                f"Step budget exceeded: {self.step_count} >= {self.step_limit}"
            )

    def _record_step(self, action_name: str, params: dict[str, Any], result: Any, duration_ms: float) -> None:
        self.step_count += 1
        elapsed = self.elapsed_time()

        # Capture frame for this step
        frame_filename = f"step_{self.step_count:03d}_frame.jpg"
        frame_path = self.run_dir / frame_filename
        frame = None
        try:
            frame = self.base.get_frame(scale="pt2")
            frame.save(frame_path, format="JPEG", quality=85)
        except Exception as e:
            # Non-fatal if frame capture fails
            pass

        # Also get elements if possible
        elements = []
        try:
            elements = self.base.get_elements()
        except Exception:
            pass

        step_record = {
            "step": self.step_count,
            "action": action_name,
            "params": params,
            "duration_ms": round(duration_ms, 2),
            "elapsed_s": round(elapsed, 2),
            "frame_file": frame_filename if frame_path.exists() else None,
            "elements_count": len(elements),
        }

        # Write action json
        action_file = self.run_dir / f"step_{self.step_count:03d}_action.json"
        action_file.write_text(json.dumps(step_record, indent=2), encoding="utf-8")

        # Append to trace.jsonl
        self._trace_handle.write(json.dumps(step_record) + "\n")
        self._trace_handle.flush()
        self.step_history.append(step_record)

        # Check early success
        if elements:
            pred_res = PredicateEvaluator.evaluate(self.scenario.predicates, elements, frame)
            if pred_res.passed:
                self.early_success = True

    def _execute(self, action_name: str, method: Callable[..., Any], params: dict[str, Any]) -> Any:
        self._pre_step_check()
        t0 = time.perf_counter()
        try:
            res = method(**params)
        except Exception:
            self._record_step(action_name, params, {"ok": False}, (time.perf_counter() - t0) * 1000)
            raise
        self._record_step(action_name, params, res, (time.perf_counter() - t0) * 1000)
        return res

    def get_state(self) -> dict[str, Any]:
        return self.base.get_state()

    def get_elements(self, annotate: bool = False) -> list[dict[str, Any]]:
        return self.base.get_elements(annotate=annotate)

    def get_frame(self, scale: str | None = None) -> Image.Image:
        return self.base.get_frame(scale=scale)

    def home(self) -> dict[str, Any]:
        return self._execute("home", self.base.home, {})

    def back(self) -> dict[str, Any]:
        return self._execute("back", self.base.back, {})

    def launch(self, app: str) -> dict[str, Any]:
        return self._execute("launch", self.base.launch, {"app": app})

    def tap(self, x: float, y: float, button: str = "left") -> dict[str, Any]:
        return self._execute("tap", self.base.tap, {"x": x, "y": y, "button": button})

    def tap_text(self, text: str, nth: int = 0) -> dict[str, Any]:
        return self._execute("tap_text", self.base.tap_text, {"text": text, "nth": nth})

    def tap_element(self, element_id: str) -> dict[str, Any]:
        return self._execute("tap_element", self.base.tap_element, {"element_id": element_id})

    def type_text(self, text: str) -> dict[str, Any]:
        return self._execute("type_text", self.base.type_text, {"text": text})

    def key(self, combo: str) -> dict[str, Any]:
        return self._execute("key", self.base.key, {"combo": combo})

    def scroll(self, dy: int, dx: int = 0) -> dict[str, Any]:
        return self._execute("scroll", self.base.scroll, {"dy": dy, "dx": dx})

    def long_press(self, x: float, y: float, ms: int = 800) -> dict[str, Any]:
        return self._execute("long_press", self.base.long_press, {"x": x, "y": y, "ms": ms})

    def drag(self, x1: float, y1: float, x2: float, y2: float, steps: int = 10) -> dict[str, Any]:
        return self._execute("drag", self.base.drag, {"x1": x1, "y1": y1, "x2": x2, "y2": y2, "steps": steps})


class ScenarioResult:
    def __init__(
        self,
        scenario: Scenario,
        success: bool,
        steps: int,
        wall_time_s: float,
        predicate_result: PredicateResult,
        reason: str = "",
        run_dir: Path | None = None,
        model: str = "",
    ):
        self.scenario = scenario
        self.success = success
        self.steps = steps
        self.wall_time_s = wall_time_s
        self.predicate_result = predicate_result
        self.reason = reason
        self.run_dir = run_dir
        self.model = model
        self.metrics = {}

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.metrics,
            "scenario_id": self.scenario.id,
            "title": self.scenario.title,
            "difficulty": self.scenario.difficulty,
            "model": self.model,
            "success": self.success,
            "steps": self.metrics.get("actions", self.steps),
            "max_steps": self.scenario.budget.max_steps,
            "wall_time_s": round(self.wall_time_s, 2),
            "max_time_s": self.scenario.budget.max_time_seconds,
            "reason": self.reason,
            "reliability": {
                "level": self.scenario.reliability.level,
                "note": self.scenario.reliability.note,
            },
            "predicate_evaluation": {
                "passed": self.predicate_result.passed,
                "score": self.predicate_result.score,
                "description": self.predicate_result.description,
                "details": self.predicate_result.details,
            },
            "run_dir": str(Path(self.scenario.id) / self.metrics.get("run_id", "")) if self.run_dir else None,
        }


class BenchmarkRunner:
    def __init__(
        self,
        daemon_client: DaemonClient,
        output_dir: Path | str | None = None,
        model: str = "google/gemma-4-26b-a4b",
        ground_truth: dict[str, str] | None = None,
        notes_exit: NotesExitOptions | None = None,
    ):
        self.daemon_client = daemon_client
        self.ground_truth = ground_truth or {}
        self.notes_exit = notes_exit
        self._run_ids = set()
        self.model = model
        if output_dir:
            self.output_dir = Path(output_dir)
        else:
            ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            self.output_dir = Path("runs") / ts
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def reset_state(self, scenario: Scenario, run_dir: Path, resets: list[dict] | None = None) -> None:
        """Reset phone state before running scenario and capture initial frame."""
        settle = scenario.start_state.settle_s
        if getattr(self.daemon_client, "fake_daemon", None) is not None:
            settle = 0
        ResetRunner(self.daemon_client, run_dir, settle, resets if resets is not None else [],
                    notes_exit=self.notes_exit).run(scenario.start_state.reset)
        # Each recipe already returns to an OCR-verified Home screen.
        if scenario.start_state.home and not scenario.start_state.reset:
            try:
                self.daemon_client.home()
            except Exception as e:
                pass

        if scenario.start_state.launch:
            try:
                self.daemon_client.launch(scenario.start_state.launch)
            except Exception as e:
                pass

        if getattr(self.daemon_client, "fake_daemon", None) is not None or "127.0.0.1" in self.daemon_client.base_url:
            settle = min(settle, 0.05)
        if settle > 0:
            time.sleep(settle)

        # Record step 0 baseline
        try:
            baseline_frame = self.daemon_client.get_frame(scale="pt2")
            baseline_frame.save(run_dir / "step_000_frame.jpg", format="JPEG", quality=85)
        except Exception:
            pass

        try:
            elements = self.daemon_client.get_elements()
            (run_dir / "step_000_elements.json").write_text(
                json.dumps(elements, indent=2), encoding="utf-8"
            )
        except Exception:
            pass

    def run_scenario(
        self,
        scenario: Scenario,
        agent: str | Callable[..., Any],
    ) -> ScenarioResult:
        run_id = "".join(secrets.choice(RUN_ID_ALPHABET) for _ in range(5))
        while run_id in self._run_ids:
            run_id = "".join(secrets.choice(RUN_ID_ALPHABET) for _ in range(5))
        self._run_ids.add(run_id)
        if scenario.id == "00_benchy" and not self.ground_truth.get("ios_version"):
            raise ValueError("Benchy requires ground truth ios_version")
        # Fresh operands per run: Calculator keeps its last result and old notes stay visible, so a
        # fixed sum leaked the answer to later runs (measured 2026-10-12).
        calc_a, calc_b = secrets.randbelow(9000) + 1000, secrets.randbelow(89) + 11
        goal = scenario.goal.replace("{run_id}", run_id).replace("{calc_a}", str(calc_a)).replace("{calc_b}", str(calc_b))
        scenario = replace(scenario, goal=goal,
            predicates=json.loads(json.dumps(scenario.predicates).replace("{run_id}", run_id)))
        run_dir = self.output_dir / scenario.id / run_id
        run_dir.mkdir(parents=True, exist_ok=True)

        resets = []
        setup_start = time.perf_counter()
        try:
            self.reset_state(scenario, run_dir, resets)
        except SetupError as exc:
            result = ScenarioResult(scenario, False, 0, 0.0,
                PredicateResult(False, "Setup failed; model was not started", score=0.0),
                f"Setup failed: {exc}", run_dir, self.model)
            result.metrics = dict(run_id=run_id, status="setup_failed", verdict="SETUP_FAILED",
                setup_failed=True, setup_error=str(exc), agent_started=False, resets=resets,
                resets_requested=list(scenario.start_state.reset),
                setup_wall_time_s=round(time.perf_counter() - setup_start, 2),
                checkpoints={}, invalid=None, claimed_success=None, honest=None,
                false_success=False, declaration=None, usage=None, actions=0,
                action_source="model not started")
            (run_dir / "result.json").write_text(json.dumps(result.to_dict(), indent=2), encoding="utf-8")
            return result
        setup_wall_time = time.perf_counter() - setup_start
        try:
            mirroring_before = _mirroring(self.daemon_client.get_state())
        except Exception:
            mirroring_before = None

        recorder = StepRecorderClient(
            base_client=self.daemon_client,
            scenario=scenario,
            run_dir=run_dir,
            time_limit_s=scenario.budget.max_time_seconds,
            step_limit=scenario.budget.max_steps,
        )

        action_start = time.time()
        t_start = time.perf_counter()
        failure_reason = ""

        try:
            if callable(agent):
                # Python callable agent
                with (run_dir / "agent_stdout.log").open("w") as out, (run_dir / "agent_stderr.log").open("w") as err:
                    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                        agent(recorder, scenario)
            elif isinstance(agent, str):
                if ":" in agent and " " not in agent:
                    # Dynamically imported callable: "module.sub:func"
                    mod_name, func_name = agent.split(":", 1)
                    mod = importlib.import_module(mod_name)
                    callable_obj = getattr(mod, func_name)
                    with (run_dir / "agent_stdout.log").open("w") as out, (run_dir / "agent_stderr.log").open("w") as err:
                        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                            callable_obj(recorder, scenario)
                else:
                    # CLI command invocation template
                    self._run_cli_agent(agent, scenario, recorder, run_dir)
            else:
                raise ValueError(f"Unsupported agent type: {type(agent)}")

        except BudgetExceededError as e:
            failure_reason = str(e)
        except Exception as e:
            failure_reason = f"Agent error: {e}"
        finally:
            wall_time = time.perf_counter() - t_start
            recorder.close()

        # Capture final frame and elements
        final_elements = []
        final_frame = None
        try:
            final_elements = self.daemon_client.get_elements()
            (run_dir / "final_elements.json").write_text(
                json.dumps(final_elements, indent=2), encoding="utf-8"
            )
        except Exception:
            pass

        try:
            final_frame = self.daemon_client.get_frame(scale="pt2")
            final_frame.save(run_dir / "final_frame.jpg", format="JPEG", quality=85)
        except Exception:
            pass

        # Evaluate predicates
        predicate_result = PredicateEvaluator.evaluate(
            scenario.predicates,
            final_elements,
            final_frame,
        )

        stdout_path = run_dir / "agent_stdout.log"
        stdout = stdout_path.read_text() if stdout_path.exists() else ""
        checkpoints, claim, declaration = {}, None, None
        if scenario.id == "00_benchy":
            checkpoints, claim, declaration = evaluate_benchy(
                final_elements, run_id, self.ground_truth["ios_version"], stdout, product=calc_a * calc_b)
            predicate_result = PredicateResult(all(checkpoints.values()),
                "Benchy checkpoints" + ("; C4: missing/invalid BENCH_RESULT" if declaration is None
                 else "; failed: " + ", ".join(k for k, v in checkpoints.items() if not v)),
                details=checkpoints, score=sum(checkpoints.values()) / 4)
        success = predicate_result.passed and not bool(failure_reason)
        if not success and not failure_reason:
            failure_reason = f"Predicate failed: {predicate_result.description}"

        result = ScenarioResult(
            scenario=scenario,
            success=success,
            steps=recorder.step_count,
            wall_time_s=wall_time,
            predicate_result=predicate_result,
            reason=failure_reason or "All predicates satisfied",
            run_dir=run_dir,
            model=self.model,
        )

        actions, source = recorder.step_count, "StepRecorderClient (attempted actions)"
        is_cli = isinstance(agent, str) and not (":" in agent and " " not in agent)
        if is_cli:
            actions, source = None, "unavailable"
            action_file = run_dir / "phone_actions.jsonl"
            if action_file.exists():
                actions, source = len(action_file.read_text().splitlines()), "phone_cli recorder (attempted actions)"
            else:
                try:
                    state = self.daemon_client.get_state()
                    log = state.get("action_log", [])
                    # A full bounded buffer may have lost actions; do not invent a count.
                    if "action_log" in state and len(log) < 100:
                        actions = sum(e.get("time", 0) >= action_start for e in log)
                        source = "daemon action log (attempted actions; exclusive use required)"
                except Exception:
                    pass
        # A run without screen frames measures the rig, not the model (measured 2026-10-12: mirroring
        # dropped mid-batch and later runs were scored as model failures).
        invalid = None
        try:
            state = self.daemon_client.get_state()
            frame_errors = sum(e.get("error") == "FrameUnavailable" and e.get("time", 0) >= action_start
                               for e in state.get("action_log", []))
            # Measured 2026-10-12: after a reconnect UxPlay stayed "connected" but wrote no frames for
            # 54 min, so mirroring=True alone is not proof of a live screen.
            age = (state.get("airplay") or {}).get("frame_age_s")
            acted = bool(actions) or actions_attempted(state, action_start)
            stale = isinstance(age, (int, float)) and age > wall_time + 5 and acted
            if mirroring_before is False or _mirroring(state) is False or frame_errors or stale:
                invalid = (f"no screen frames (mirroring before={mirroring_before}, after={_mirroring(state)}, "
                           f"frame errors={frame_errors}, newest frame {age} s old)")
        except Exception:
            pass
        if invalid:
            success = False
            result.success = False
        result.metrics = dict(run_id=run_id, status="completed", setup_failed=False, agent_started=True,
            resets=resets, resets_requested=list(scenario.start_state.reset),
            setup_wall_time_s=round(setup_wall_time, 2), checkpoints=checkpoints, invalid=invalid,
            verdict="INVALID" if invalid else "PASS" if success else "FAIL", claimed_success=claim,
            honest=None if invalid else claim == success if claim is not None else None,
            false_success=not invalid and claim is True and not success, declaration=declaration,
            usage=parse_usage(stdout), actions=actions, action_source=source)
        (run_dir / "result.json").write_text(
            json.dumps(result.to_dict(), indent=2), encoding="utf-8"
        )
        return result

    def _run_cli_agent(
        self,
        cmd_template: str,
        scenario: Scenario,
        recorder: StepRecorderClient,
        run_dir: Path,
    ) -> None:
        values = dict(
            goal=scenario.goal,
            scenario_id=scenario.id,
            model=self.model,
            daemon_url=self.daemon_client.base_url,
            token=self.daemon_client.token or "",
            max_steps=scenario.budget.max_steps,
            max_time=scenario.budget.max_time_seconds,
            run_dir=str(run_dir),
        )

        args = [arg.format(**values) for arg in shlex.split(cmd_template)]
        env = os.environ.copy()
        env["IPHONE_HAND_DAEMON_URL"] = self.daemon_client.base_url
        if self.daemon_client.token:
            env["IPHONE_HAND_TOKEN"] = self.daemon_client.token
        env["BENCH_SCENARIO_ID"] = scenario.id
        env["BENCH_GOAL"] = scenario.goal
        env["BENCH_MODEL"] = self.model
        env["BENCH_RUN_DIR"] = str(run_dir.resolve())
        env["BENCH_ACTION_LOG"] = str((run_dir / "phone_actions.jsonl").resolve())

        stdout_log = run_dir / "agent_stdout.log"
        stderr_log = run_dir / "agent_stderr.log"

        with open(stdout_log, "w", encoding="utf-8") as out, open(stderr_log, "w", encoding="utf-8") as err:
            proc = subprocess.Popen(args, stdout=out, stderr=err, env=env, start_new_session=True)

            while proc.poll() is None:
                elapsed = recorder.elapsed_time()
                if elapsed > scenario.budget.max_time_seconds:
                    os.killpg(proc.pid, signal.SIGTERM)
                    try:
                        proc.wait(timeout=2.0)
                    except subprocess.TimeoutExpired:
                        os.killpg(proc.pid, signal.SIGKILL)
                        proc.wait()
                    raise BudgetExceededError(
                        f"Time budget exceeded: {elapsed:.2f}s > {scenario.budget.max_time_seconds:.2f}s"
                    )
                time.sleep(0.2)

            ret = proc.returncode
            if ret != 0:
                raise RuntimeError(f"CLI agent exited with return code {ret}")

    def run_benchmark(
        self,
        scenarios: list[Scenario],
        agent: str | Callable[..., Any],
        repeat: int = 1,
    ) -> list[ScenarioResult]:
        results: list[ScenarioResult] = []
        if repeat < 1:
            raise ValueError("repeat must be positive")
        for _ in range(repeat):
            for s in scenarios:
                res = self.run_scenario(s, agent)
                results.append(res)
                print(run_summary(res))
                if res.metrics.get("setup_failed"):
                    print("Stopping: setup failed. Inspect reset evidence and re-run.")
                    self.write_summary_reports(results)
                    return results
                if res.metrics.get("invalid"):
                    print("Stopping: " + res.metrics["invalid"] + ". Restore mirroring and re-run.")
                    self.write_summary_reports(results)
                    return results

        self.write_summary_reports(results)
        return results

    def write_summary_reports(self, results: list[ScenarioResult]) -> tuple[Path, Path]:
        total = len(results)
        setup_failed = sum(bool(r.metrics.get("setup_failed")) for r in results)
        evaluated = total - setup_failed
        passed = sum(1 for r in results if r.success)
        failed = evaluated - passed
        success_rate = (passed / evaluated * 100.0) if evaluated > 0 else 0.0
        total_wall_time = sum(r.wall_time_s for r in results)

        summary_data = {
            "timestamp": datetime.datetime.now().isoformat(),
            "model": self.model,
            "false_successes": sum(bool(r.metrics.get("false_success")) for r in results),
            "aggregate": aggregate_results(results),
            "total_scenarios": total,
            "evaluated_scenarios": evaluated,
            "setup_failed": setup_failed,
            "passed": passed,
            "failed": failed,
            "success_rate_pct": round(success_rate, 2),
            "total_wall_time_s": round(total_wall_time, 2),
            "scenarios": [r.to_dict() for r in results],
        }

        # Write results.json
        json_path = self.output_dir / "results.json"
        json_path.write_text(json.dumps(summary_data, indent=2), encoding="utf-8")

        # Write summary.md table
        md_lines = [
            "# iPhone Hand Benchmark Run Summary",
            "",
            f"- **Date/Time**: `{summary_data['timestamp']}`",
            f"- **Model**: `{self.model}`",
            f"- **FALSE SUCCESS (worst outcome)**: {summary_data['false_successes']}",
            f"- **Setup failed (model not started)**: {setup_failed}",
            f"- **Success Rate**: **{passed}/{evaluated}** ({success_rate:.1f}%)",
            f"- **Total Wall Time**: {total_wall_time:.2f}s",
            "",
            "| Scenario | Title | Difficulty | Result | Steps / Max | Time (s) | Reliability | Notes |",
            "|---|---|---|---|---|---|---|---|",
        ]

        for r in results:
            status = r.metrics.get("verdict", "PASS" if r.success else "FAIL")
            rel = f"{r.scenario.reliability.level.capitalize()}"
            note_snip = r.reason.replace("|", "/")
            md_lines.append(
                f"| `{r.scenario.id}` | {r.scenario.title} | {r.scenario.difficulty.capitalize()} | "
                f"**{status}** | {r.steps} / {r.scenario.budget.max_steps} | {r.wall_time_s:.2f}s | "
                f"{rel} | {note_snip} |"
            )

        md_lines.extend(["", "## Per-run measured results", ""])
        md_lines.extend(run_summary(r) + "  " for r in results)
        md_lines.extend(["", "Action sources: " + "; ".join(sorted({r.metrics.get("action_source", "unknown") for r in results})),
                         "", "## Aggregate by model (measured)", "", "```json",
                         json.dumps(summary_data["aggregate"], indent=2), "```"])
        md_lines.append("")
        md_path = self.output_dir / "summary.md"
        md_path.write_text("\n".join(md_lines), encoding="utf-8")

        return json_path, md_path


def aggregate_results(results):
    aggregate = {}
    for r in results:
        row = aggregate.setdefault(r.model, dict(runs=0, model_runs=0, setup_failed=0, passed=0, honest=0,
            false_success=0, wall_time_s=0, actions=0, actions_unknown=0,
            input_tokens=0, output_tokens=0, usage_unknown=0, cost_usd=0, cost_unknown=0,
            checkpoints={f"C{i}": 0 for i in range(1, 5)}))
        row["runs"] += 1
        if r.metrics.get("setup_failed"):
            row["setup_failed"] += 1
            continue
        row["model_runs"] += 1
        row["passed"] += r.success
        row["honest"] += bool(r.metrics.get("honest"))
        row["false_success"] += bool(r.metrics.get("false_success"))
        row["wall_time_s"] += r.wall_time_s
        actions = r.metrics.get("actions")
        row["actions"] += actions or 0
        row["actions_unknown"] += actions is None
        usage = r.metrics.get("usage") or {}
        row["usage_unknown"] += not bool(usage)
        for k in ("input_tokens", "output_tokens", "cost_usd"):
            row[k] += usage.get(k, 0)
        row["cost_unknown"] += "cost_usd" not in usage
        for k, passed in r.metrics.get("checkpoints", {}).items():
            row["checkpoints"][k] += passed
    return aggregate


def dummy_agent(client: StepRecorderClient, scenario: Scenario) -> None:
    """Built-in demonstration / test agent that solves mock scenarios against FakeDaemon."""
    sid = scenario.id
    if "open_settings" in sid:
        client.launch("Settings")
    elif "open_clock_timer" in sid:
        client.launch("Clock")
        client.tap_text("Timer")
    elif "calculator_clear" in sid:
        client.launch("Calculator")
        client.tap_element("btn_ac")
    elif "spotlight" in sid:
        client.key("cmd+space")
        client.type_text("Display & Brightness")
        client.key("enter")
    elif "toggle_dark_mode" in sid:
        client.launch("Settings")
        client.tap_text("Display & Brightness")
        client.tap_element("t_dark")
    elif "set_timer_1min" in sid:
        client.launch("Clock")
        client.tap_text("Timer")
        client.tap_element("t_start")
    elif "low_power_mode" in sid:
        client.launch("Settings")
        client.tap_text("Battery")
        client.tap_element("t_lpm")
    elif "safari" in sid:
        client.launch("Safari")
        client.type_text("apple status")
        client.key("enter")
    elif "create_note" in sid:
        client.launch("Notes")
        client.tap_element("btn_new_note")
        client.type_text("Meeting notes: bench ready")
    elif "add_reminder" in sid:
        client.launch("Reminders")
        client.tap_element("btn_new_rem")
        client.type_text("Buy oat milk")
    elif "battery_health" in sid:
        client.launch("Settings")
        client.tap_text("Battery")
        client.tap_element("t_bh")
    elif "calculator_multistep" in sid:
        client.launch("Calculator")
        client.type_text("125*8")
        client.key("enter")


def main() -> None:
    parser = argparse.ArgumentParser(description="iphone-hand scenario benchmark runner")
    parser.add_argument("--scenarios-dir", default=str(DEFAULT_SCENARIOS_DIR), help="Path to scenarios directory")
    parser.add_argument("--scenario", default=None, help="Specific scenario ID or file to run (default: all)")
    parser.add_argument("--model", default="google/gemma-4-26b-a4b", help="Model name identifier")
    parser.add_argument("--agent-cmd", default=None, help="Command line template for agent (e.g. droidclaw)")
    parser.add_argument("--agent-callable", default="bench.runner:dummy_agent", help="Python callable for agent")
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--ground-truth", action="append", default=[], metavar="KEY=VALUE")
    parser.add_argument("--daemon-url", default=os.environ.get("IPHONE_HAND_DAEMON_URL"), help="Daemon API URL")
    parser.add_argument("--token", default=os.environ.get("IPHONE_HAND_TOKEN"), help="Daemon auth token")
    parser.add_argument("--fake-daemon", action="store_true", help="Spin up an in-process FakeDaemon on localhost")
    parser.add_argument("--output-dir", default=None, help="Custom output directory for run artifacts")
    parser.add_argument("--notes-exit-strategies", default=None,
                        help="Comma-separated order; defaults to IPHONE_HAND_NOTES_EXIT_STRATEGIES or shortcut,fka_find,esc")
    parser.add_argument("--notes-shortcut-name", default=None,
                        help="Exact Shortcuts tile OCR; defaults to IPHONE_HAND_NOTES_SHORTCUT_NAME or noteslist")
    parser.add_argument("--notes-fka-on", action="store_true", help="Assert Full Keyboard Access is already ON")
    parser.add_argument("--notes-switcher-combo", help="Measured switcher shortcut, replacing placeholder opening drag")
    parser.add_argument("--notes-switcher-open-drag", type=float, nargs=5,
                        metavar=("X1", "Y1", "X2", "Y2", "STEPS"),
                        help="Unmeasured default: .5 .99 .5 .45 40; coordinates are screen fractions")
    parser.add_argument("--notes-switcher-close-drag", type=float, nargs=5,
                        metavar=("X1", "Y1", "X2", "Y2", "STEPS"),
                        help="Unmeasured default: .5 .5 .5 .05 20; coordinates are screen fractions")
    parser.add_argument("--notes-switcher-pause", type=float, default=.5,
                        help="Unmeasured post-release pause in seconds (default .5)")
    parser.add_argument("--list", action="store_true", help="List scenarios and exit")

    args = parser.parse_args()

    defaults = NotesExitOptions.from_env()
    def gesture(values, fallback):
        if values is None:
            return fallback
        if not values[-1].is_integer():
            parser.error("Notes gesture steps must be an integer")
        return (*values[:4], int(values[-1]))
    notes_exit = NotesExitOptions(
        strategies=tuple(v.strip() for v in args.notes_exit_strategies.split(","))
        if args.notes_exit_strategies is not None else defaults.strategies,
        shortcut_name=args.notes_shortcut_name if args.notes_shortcut_name is not None else defaults.shortcut_name,
        fka_on=args.notes_fka_on, switcher_combo=args.notes_switcher_combo,
        open_drag=gesture(args.notes_switcher_open_drag, defaults.open_drag),
        close_drag=gesture(args.notes_switcher_close_drag, defaults.close_drag),
        switcher_pause_s=args.notes_switcher_pause)
    try:
        notes_exit.validate()
    except SetupError as exc:
        parser.error(str(exc))

    scenarios = load_all_scenarios(args.scenarios_dir)
    if args.list:
        print(f"Loaded {len(scenarios)} scenarios from {args.scenarios_dir}:")
        print(f"{'ID':<35} {'Difficulty':<12} {'Max Steps':<10} {'Reliability':<12} Title")
        print("-" * 85)
        for s in scenarios:
            print(f"{s.id:<35} {s.difficulty:<12} {s.budget.max_steps:<10} {s.reliability.level:<12} {s.title}")
        return

    if args.scenario:
        scenarios = [s for s in scenarios if s.id == args.scenario or str(s.path) == args.scenario]
        if not scenarios:
            print(f"No scenario matched '{args.scenario}'", file=sys.stderr)
            sys.exit(1)

    if args.repeat < 1:
        parser.error("--repeat must be positive")
    try:
        ground_truth = dict(item.split("=", 1) for item in args.ground_truth)
    except ValueError:
        parser.error("--ground-truth requires KEY=VALUE")
    if any(s.id == "00_benchy" for s in scenarios) and not ground_truth.get("ios_version"):
        parser.error("Benchy requires --ground-truth ios_version=...")
    if not args.fake_daemon and not args.daemon_url:
        parser.error("Provide --daemon-url or IPHONE_HAND_DAEMON_URL")
    fake_server = None
    daemon_url = args.daemon_url
    if args.fake_daemon:
        fake_server = FakeDaemonServer(required_token=args.token)
        fake_server.start()
        daemon_url = fake_server.url
        print(f"[FakeDaemon] Started mock daemon at {daemon_url}")

    try:
        client = DaemonClient(base_url=daemon_url, token=args.token, timeout=60.0)  # measured: a tap_element took 14.3 s (pointer travel)
        runner = BenchmarkRunner(daemon_client=client, output_dir=args.output_dir, model=args.model, ground_truth=ground_truth, notes_exit=notes_exit)
        agent_target = args.agent_cmd if args.agent_cmd else args.agent_callable
        print(f"Running benchmark with {len(scenarios)} scenario(s)")
        results = runner.run_benchmark(scenarios, agent_target, repeat=args.repeat)
        passed = sum(1 for r in results if r.success)
        print(f"Benchmark finished: {passed}/{len(results)} passed.")
        print(f"Summary written to: {runner.output_dir / 'summary.md'}")
    finally:
        if fake_server:
            fake_server.stop()
            print("[FakeDaemon] Server stopped.")


if __name__ == "__main__":
    main()
