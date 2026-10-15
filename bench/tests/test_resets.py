"""Synthetic setup/recovery tests; no hardware, network or model is used."""
import json
from pathlib import Path

import pytest

from bench.daemon_client import DaemonClient
from bench.fake_daemon import FakeDaemon, _make_element
from bench.runner import BenchmarkRunner
from bench.scenarios import load_scenario, ScenarioValidationError
from bench.yaml_util import parse_simple_yaml

SCENARIO = Path("bench/scenarios/00_benchy.yaml")

def no_park(actions):
    """Drop the pointer-parking moves the Calculator reset makes before OCR (measured occlusion fix)."""
    return [a for a in actions if a["action"] != "move"]



def run(daemon, tmp_path, agent):
    scenario = load_scenario(SCENARIO)
    scenario.start_state.settle_s = 0
    runner = BenchmarkRunner(DaemonClient(base_url="", fake_daemon=daemon), tmp_path,
                             ground_truth={"ios_version": "18.0"})
    return runner, runner.run_scenario(scenario, agent)


@pytest.mark.parametrize("notes_screen", ["notes", "notes_list", "notes_search", "notes_folders"])
@pytest.mark.parametrize("display", ["69 104", "32 111 119 314", "84 976+3+3+", "((-5 684))", "0"])
def test_resets_verify_clean_start_without_editing_notes(tmp_path, notes_screen, display):
    daemon = FakeDaemon()
    daemon.current_app, daemon.current_screen = "Notes", notes_screen
    daemon.notes_text = "Private existing note\nSearch\nNotes"
    daemon.calc_display, daemon.calc_buffer = display, "pending arithmetic"
    calls = []

    def agent(client, scenario):
        calls.append(1)
        assert daemon.current_screen == "home"
        assert daemon.calc_display == "0" and daemon.calc_buffer == ""
        assert daemon.notes_resume_screen in ("notes_list", "notes_folders")
        assert daemon.notes_text == "Private existing note\nSearch\nNotes"

    _, result = run(daemon, tmp_path, agent)
    assert calls == [1]
    data = json.loads((result.run_dir / "result.json").read_text())
    assert data["agent_started"] and not data["setup_failed"]
    assert data["actions"] == 0  # Setup actions never consume model steps/budget.
    assert data["resets_requested"] == ["calculator_cleared", "notes_list"]
    assert [r["recipe"] for r in data["resets"]] == data["resets_requested"]
    assert all(r["verified"] and r["status"] == "verified" for r in data["resets"])
    checks = {e["check"]: e for r in data["resets"] for e in r["evidence"]}
    assert checks["Calculator display is exactly 0"]["matched_ocr"] == ["0"]
    assert set(checks["Notes list or folders"]["matched_ocr"]) >= {"Search"}
    assert len([a for a in daemon.action_log if a["action"] == "tap_element"
                and a["params"]["id"] == "btn_ac"]) == (1 if display == "0" else 2)
    typed = [a["params"]["text"] for a in daemon.action_log if a["action"] == "type"]
    # The fake starts Notes in editing mode; the shortcut exit adds a Spotlight search for Shortcuts.
    assert typed[:2] == ["Calc", "Notes"] and set(typed) <= {"Calc", "Notes", "Shortcuts", "noteslist"}
    for recipe in data["resets"]:
        for evidence in recipe["evidence"]:
            assert evidence["verified"]
            saved = json.loads((result.run_dir / (evidence["observation"] + "_elements.json")).read_text())
            assert all(t in [el["text"] for el in saved] for t in evidence["matched_ocr"])
            assert (result.run_dir / (evidence["observation"] + "_frame.jpg")).exists()


class CalculatorRecoveryDaemon(FakeDaemon):
    """Synthetic transitions for the lead's measured backspace/AC states.

    Estimated: Escape's clearing behavior is a test variant, not a phone measurement.
    """

    def __init__(self, display, *, escape_clears=False, evaluation="69 104",
                 clear_label=None, evaluated_clear="AC", stuck=False):
        super().__init__()
        self.calc_display, self.calc_buffer = display, "pending arithmetic"
        self.escape_clears = escape_clears
        self.evaluation = evaluation
        self.clear_label, self.evaluated_clear = clear_label, evaluated_clear
        self.stuck = stuck
        self.notes_resume_screen = "notes_list"

    def _reference_elements(self):
        elements = super()._reference_elements()
        if self.current_screen == "calculator":
            for element in elements:
                if element["id"] == "btn_ac":
                    # An icon/VC target exists, but OCR supplies no AC/C text for it.
                    element.update(text=self.clear_label or "", type="text" if self.clear_label else "vc_item")
        return elements

    def handle_action(self, action, params):
        if self.current_screen == "calculator":
            if action == "key" and params["combo"] == "esc":
                if self.escape_clears:
                    self.calc_display, self.calc_buffer, self.clear_label = "0", "", "AC"
            elif action == "type" and params["text"] == "=":
                self.calc_display, self.clear_label = self.evaluation, self.evaluated_clear
            elif action == "tap_element" and params["id"] == "btn_ac":
                assert self.clear_label in ("AC", "C"), "Must never tap the backspace glyph"
                if not self.stuck:
                    if self.clear_label == "AC":
                        self.calc_buffer = ""
                    self.calc_display, self.clear_label = "0", "AC"
            else:
                return super().handle_action(action, params)
            self.action_log.append({"action": action, "params": params})
            return {"ok": True}
        return super().handle_action(action, params)


@pytest.mark.parametrize("display", ["84 976+3+3+", "(-6)x3%", "((-5 684))", "32 111 119 314"])
@pytest.mark.parametrize("escape_clears", [True, False])
def test_calculator_backspace_keyboard_recovery(tmp_path, display, escape_clears):
    daemon = CalculatorRecoveryDaemon(display, escape_clears=escape_clears)
    calls = []

    def agent(*args):
        assert daemon.calc_display == "0" and daemon.calc_buffer == ""
        calls.append(1)

    _, result = run(daemon, tmp_path, agent)
    assert calls == [1] and not result.metrics["setup_failed"]
    reset = result.metrics["resets"][0]
    expected = [{"action": "key", "args": {"combo": "esc"}, "ok": True}]
    if not escape_clears:
        expected.append({"action": "type", "args": {"text": "="}, "ok": True})
    expected.append({"action": "tap_element", "args": {"id": "btn_ac"}, "ok": True})
    assert no_park(reset["actions"])[4:-1] == expected
    # The initial Calculator observation has an input display and no text clear target.
    initial = json.loads((result.run_dir / (reset["observations"][3] + "_elements.json")).read_text())
    assert not any(el["text"] in ("AC", "C") for el in initial)
    assert any(el["text"] == display for el in initial)
    assert reset["verified"]


@pytest.mark.parametrize("initial_error", [True, False])
@pytest.mark.parametrize("clear_label", ["AC", "C"])
def test_calculator_error_display_clears_with_observed_target(tmp_path, initial_error, clear_label):
    daemon = CalculatorRecoveryDaemon("Error" if initial_error else "((-5 684))",
                                      evaluation="Error", clear_label=clear_label if initial_error else None,
                                      evaluated_clear=clear_label)
    _, result = run(daemon, tmp_path, lambda *args: None)
    assert not result.metrics["setup_failed"]
    assert daemon.calc_display == "0" and daemon.calc_buffer == ""
    actions = no_park(result.metrics["resets"][0]["actions"])[4:-1]
    assert [a["action"] for a in actions] == ([] if initial_error else ["key", "type"]) + (
        ["tap_element"] if clear_label == "AC" else ["tap_element", "tap_element"])
    assert all(a["args"] == {"id": "btn_ac"} for a in actions if a["action"] == "tap_element")


@pytest.mark.parametrize("evaluation, evaluated_clear, stuck, diagnostic", [
    ("69 104", None, False, "Calculator AC/C after bounded keyboard recovery"),
    ("69 104", "AC", True, "Calculator display is exactly 0"),
    ("Error", "AC", True, "Calculator display is exactly 0"),
    ("69 104", "C", True, "Calculator display is exactly 0"),
])
def test_calculator_keyboard_recovery_is_bounded_and_fails_closed(
        tmp_path, evaluation, evaluated_clear, stuck, diagnostic):
    daemon = CalculatorRecoveryDaemon("(-6)x3%", evaluation=evaluation,
                                      evaluated_clear=evaluated_clear, stuck=stuck)
    _, result = run(daemon, tmp_path, lambda *args: pytest.fail("Agent must not start"))
    data = json.loads((result.run_dir / "result.json").read_text())
    assert data["verdict"] == "SETUP_FAILED" and not data["agent_started"]
    assert len(data["resets"]) == 1
    reset = data["resets"][0]
    assert reset["status"] == "failed" and diagnostic in reset["error"]
    actions = no_park(reset["actions"])[4:]
    assert [a["action"] for a in actions] == ["key", "type"] + (["tap_element"] if evaluated_clear else [])
    assert actions[0]["args"] == {"combo": "esc"}
    assert actions[1]["args"] == {"text": "="}
    assert not any(a["action"] in ("tap", "tap_text") for a in daemon.action_log)


@pytest.mark.parametrize("fault", ["keypad_zero_only", "zero_and_error", "ambiguous_clear"])
def test_calculator_keyboard_recovery_keeps_ocr_safety_gates(tmp_path, fault):
    class RecoveryFaultDaemon(CalculatorRecoveryDaemon):
        def _reference_elements(self):
            elements = super()._reference_elements()
            if self.current_screen == "calculator":
                if fault == "keypad_zero_only":
                    elements = [el for el in elements if el["id"] != "t_display"]
                elif fault == "zero_and_error" and self.calc_display == "0":
                    elements.append(_make_element("error", "Error", bounds=(20, 180, 355, 210)))
                elif fault == "ambiguous_clear" and self.clear_label:
                    elements.append(_make_element("other_ac", "AC", bounds=(100, 220, 150, 280)))
            return elements

    daemon = RecoveryFaultDaemon("84 976+3+3+")
    _, result = run(daemon, tmp_path, lambda *args: pytest.fail("Agent must not start"))
    assert result.metrics["setup_failed"] and not result.metrics["agent_started"]
    actions = no_park(result.metrics["resets"][0]["actions"])[4:]
    assert actions[0]["args"] == {"combo": "esc"}
    assert actions[1]["args"] == {"text": "="}
    assert [a["action"] for a in actions] == ["key", "type"] + (
        [] if fault == "ambiguous_clear" else ["tap_element"])


class FaultDaemon(FakeDaemon):
    def __init__(self, fault):
        super().__init__()
        self.fault = fault
        if fault in ("ambiguous_back", "notes_stuck"):
            self.notes_editing = False  # Test viewer back gates after editing has ended.
        if fault == "notes_editor_looks_like_list":
            self.switcher_closes = False
        self.calc_display = "69104"
        self.notes_text = "Existing note"

    def _reference_elements(self):
        elements = super()._reference_elements()
        if self.current_screen == "calculator":
            if self.fault == "keypad_zero_only":
                elements = [el for el in elements if el["id"] != "t_display"]
            elif self.fault == "ambiguous_clear":
                elements += [_make_element("another_clear", "C", bounds=(100, 220, 150, 280))]
            elif self.fault == "zero_and_garbage":
                elements += [_make_element("garbage", "84 976+3+3+", bounds=(20, 180, 355, 210))]
            elif self.fault == "bad_bounds":
                elements[0]["bounds"] = [float("nan"), 100, 355, 180]
        if self.current_screen in ("notes", "notes_list", "notes_search"):
            if self.fault == "unknown_notes":
                elements = [_make_element("unknown", "Unknown screen")]
            elif self.fault == "notes_editor_looks_like_list":
                elements += [_make_element("old_body", "Search", bounds=(20, 140, 355, 180))]
            elif self.fault == "ambiguous_back":
                elements += [_make_element("other_back", "Notes", bounds=(10, 85, 80, 110))]
        if self.current_screen == "spotlight" and self.fault == "spotlight_not_empty":
            elements += [_make_element("old_query", "old search", focused=True)]
        return elements

    def handle_action(self, action, params):
        if self.fault == "reject_home" and action == "home":
            return {"result": {"ok": False, "error": "synthetic secret"}}
        if self.fault == "no_clear" and action == "tap_element" and params["id"] == "btn_ac":
            return {"ok": True}
        if self.fault in ("notes_stuck", "notes_editor_looks_like_list") and action == "tap_element" and params["id"] == "t_back":
            self.action_log.append({"action": action, "params": params})
            return {"ok": True}
        return super().handle_action(action, params)


@pytest.mark.parametrize("fault", ["keypad_zero_only", "ambiguous_clear", "zero_and_garbage", "bad_bounds",
                                   "unknown_notes", "ambiguous_back", "notes_stuck",
                                   "notes_editor_looks_like_list", "spotlight_not_empty", "reject_home", "no_clear"])
@pytest.mark.parametrize("agent_kind", ["callable", "cli", "import"])
def test_unverifiable_setup_stops_before_any_agent(tmp_path, monkeypatch, fault, agent_kind):
    def never(*args, **kwargs):
        pytest.fail("A model/agent must not start after setup failure")
    monkeypatch.setattr("bench.runner.subprocess.Popen", never)
    monkeypatch.setattr("bench.runner.importlib.import_module", never)
    agent = never if agent_kind == "callable" else "synthetic command" if agent_kind == "cli" else "synthetic:agent"
    runner, result = run(FaultDaemon(fault), tmp_path, agent)
    assert not result.success and result.steps == 0 and result.wall_time_s == 0
    data = json.loads((result.run_dir / "result.json").read_text())
    assert data["status"] == "setup_failed" and data["verdict"] == "SETUP_FAILED"
    assert data["setup_failed"] and not data["agent_started"]
    assert data["checkpoints"] == {} and data["honest"] is None and not data["false_success"]
    assert data["actions"] == 0 and data["usage"] is None
    assert data["resets"][-1]["status"] == "failed"
    assert not (result.run_dir / "agent_stdout.log").exists()
    assert not (result.run_dir / "trace.jsonl").exists()
    runner.write_summary_reports([result])
    report = json.loads((tmp_path / "results.json").read_text())
    assert report["setup_failed"] == 1 and report["failed"] == 0
    assert report["evaluated_scenarios"] == 0
    assert report["aggregate"][runner.model]["model_runs"] == 0
    assert "SETUP_FAILED" in (tmp_path / "summary.md").read_text()


def test_capture_error_is_setup_failure_with_safe_diagnostic(tmp_path):
    daemon = FakeDaemon()
    def broken(*args, **kwargs):
        raise RuntimeError("synthetic credential must not appear")
    daemon.get_frame_image = broken
    _, result = run(daemon, tmp_path, lambda *args: pytest.fail("agent started"))
    assert result.metrics["setup_failed"]
    assert "synthetic credential" not in (result.run_dir / "result.json").read_text()


def test_setup_failure_stops_repeat_batch(tmp_path):
    runner = BenchmarkRunner(DaemonClient(base_url="", fake_daemon=FaultDaemon("no_clear")), tmp_path,
                             ground_truth={"ios_version": "18.0"})
    results = runner.run_benchmark([load_scenario(SCENARIO)], lambda *args: pytest.fail("agent started"), repeat=3)
    assert len(results) == 1 and results[0].metrics["setup_failed"]


def test_recipes_run_before_every_repeat(tmp_path):
    daemon = FakeDaemon()
    calls = []
    def dirty_agent(client, scenario):
        assert daemon.calc_display == "0" and daemon.notes_resume_screen == "notes_list"
        daemon.calc_display = "69 104"
        client.launch("Notes")
        client.tap_element("btn_new_note")
        client.type_text("Existing note")
        calls.append(1)
    runner = BenchmarkRunner(DaemonClient(base_url="", fake_daemon=daemon), tmp_path,
                             ground_truth={"ios_version": "18.0"})
    results = runner.run_benchmark([load_scenario(SCENARIO)], dirty_agent, repeat=2)
    assert calls == [1, 1]
    assert all(len(r.metrics["resets"]) == 2 and all(v["verified"] for v in r.metrics["resets"]) for r in results)


@pytest.mark.parametrize("resets", [None, "calculator_cleared", {}, ["unknown"], [True],
                                    ["calculator_cleared", "calculator_cleared"]])
def test_reset_schema_rejects_invalid_recipes(resets):
    scenario = load_scenario(SCENARIO)
    scenario.start_state.reset = resets
    with pytest.raises(ScenarioValidationError, match="start_state.reset"):
        scenario.validate()


def test_reset_schema_roundtrip_and_yaml_fallback():
    scenario = load_scenario(SCENARIO)
    assert scenario.to_dict()["start_state"]["reset"] == ["calculator_cleared", "notes_list"]
    assert parse_simple_yaml(SCENARIO.read_text())["start_state"]["reset"] == scenario.start_state.reset
    assert load_scenario("bench/scenarios/01_open_settings.yaml").start_state.reset == []


@pytest.mark.parametrize("screen_pt", [(375, 812), (390, 844), (320, 568)])
def test_resets_use_phone_point_geometry(tmp_path, screen_pt):
    _, result = run(FakeDaemon(screen_pt=screen_pt), tmp_path, lambda *args: None)
    assert not result.metrics["setup_failed"]


@pytest.mark.parametrize("mode", ["editor_keyboard", "search_cancel", "folders_without_search"])
def test_notes_recovery_paths(tmp_path, mode):
    class RecoveryDaemon(FakeDaemon):
        def __init__(self):
            super().__init__()
            self.notes_resume_screen = {"editor_keyboard": "notes", "search_cancel": "notes_search",
                                        "folders_without_search": "notes_folders"}[mode]
            self.notes_text = "Existing note remains unchanged"
            self.keyboard = mode == "editor_keyboard"

        def _reference_elements(self):
            elements = super()._reference_elements()
            if self.current_screen == "notes" and self.keyboard:
                elements.append(_make_element("space", "space", bounds=(100, 700, 270, 730)))
            if self.current_screen == "notes_folders":
                elements = [el for el in elements if el["text"] != "Search"]
                elements.append(_make_element("folder_notes", "Notes", bounds=(20, 180, 355, 220)))
            return elements

        def handle_action(self, action, params):
            if action == "key" and params["combo"] == "esc":
                self.keyboard = False
                self.action_log.append({"action": action, "params": params})
                # Search intentionally keeps Cancel, forcing the observed cancel tap.
                return {"ok": True}
            return super().handle_action(action, params)

    daemon = RecoveryDaemon()
    _, result = run(daemon, tmp_path, lambda *args: None)
    assert not result.metrics["setup_failed"]
    assert daemon.notes_text == "Existing note remains unchanged"
    actions = result.metrics["resets"][1]["actions"]
    combos = [a["args"].get("combo") for a in actions if a["action"] == "key"]
    assert combos.count("esc") == (0 if mode == "folders_without_search" else 1)
    taps = [a["args"]["id"] for a in actions if a["action"] == "tap_element"]
    assert taps == {"editor_keyboard": ["t_back"],  # indexed shortcut runs from Spotlight, no tile tap
                     "search_cancel": ["cancel"],
                    "folders_without_search": []}[mode]
