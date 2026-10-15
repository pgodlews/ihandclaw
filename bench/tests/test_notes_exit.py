"""Estimated synthetic transitions only; all clients use an in-process fake or mocked HTTP."""
from dataclasses import replace
import io
import json
from pathlib import Path
import urllib.error

import pytest

from bench.daemon_client import DaemonClient, DaemonError
from bench.fake_daemon import FakeDaemon, _make_element
from bench.resets import NotesExitOptions, ResetRunner, SetupError
from bench.runner import BenchmarkRunner
from bench.scenarios import load_scenario


def reset(daemon, tmp_path, options=None):
    records = []
    runner = ResetRunner(DaemonClient(base_url="", fake_daemon=daemon), tmp_path, 0, records,
                         options or NotesExitOptions(switcher_pause_s=0))
    try:
        runner.run(["notes_list"])
    except SetupError:
        pass
    return records[0]


@pytest.mark.parametrize("strategy", ["switcher_kill", "fka_find", "esc"])
@pytest.mark.parametrize("works", [True, False])
def test_each_strategy_success_and_failure(tmp_path, strategy, works):
    # Estimated: esc success is a synthetic alternate OS, NOT reference-phone behavior.
    daemon = FakeDaemon(fka_on=True, switcher_closes=works, fka_find_works=works,
                        esc_exits_editing=works)
    daemon.notes_text = "Existing note stays intact"
    row = reset(daemon, tmp_path, NotesExitOptions(strategies=(strategy,), fka_on=True,
                                                 switcher_pause_s=0))
    assert row["verified"] is works
    log, = row["strategies"]
    assert log["attempted"] and log["verified"] is works
    assert log["verification"]["no_nav_done"] is works
    assert log["verification"]["pointer_visible"] is works
    assert log["action_end"] > log["action_start"]
    assert daemon.notes_text == "Existing note stays intact"
    if works:
        assert daemon.notes_resume_screen == "notes_list"
        assert daemon.current_screen == "home"
    if strategy == "switcher_kill":
        assert log["verification"]["notes_list"] is works
        assert not any(a["action"].startswith("tap") for a in row["actions"])
    if strategy == "fka_find":
        combos = [a["args"]["combo"] for a in row["actions"] if a["action"] == "key"]
        assert combos[-3:] == ["ctrl+tab", "tab+f", "return"]


def test_default_escape_matches_measured_keyboard_only_behavior(tmp_path):
    daemon = FakeDaemon()
    row = reset(daemon, tmp_path, NotesExitOptions(strategies=("esc",)))
    assert not row["verified"]
    assert daemon.notes_editing and not daemon.notes_keyboard
    assert row["strategies"][0]["verification"]["no_nav_done"] is False


@pytest.mark.parametrize("tile", ["noteslist", "custom notes"])
def test_shortcut_exits_to_back_viewer_then_recovers_list(tmp_path, tile):
    daemon = FakeDaemon(shortcut_tile=tile)
    daemon.notes_text = "Existing note stays intact"
    # Tile path only (the Spotlight route has its own test).
    row = reset(daemon, tmp_path, NotesExitOptions(shortcut_name=tile, strategies=("shortcut", "fka_find", "esc")))
    assert row["verified"] and daemon.notes_text == "Existing note stays intact"
    log, = row["strategies"]
    assert log["strategy"] == "shortcut" and log["verified"]
    assert log["verification"]["notes_foreground"]
    assert log["verification"]["no_nav_done"] and log["verification"]["pointer_visible"]
    assert not log["verification"]["notes_list"]  # Still a viewer; recovery must tap Back.
    viewer = json.loads((tmp_path / (log["verification"]["observation"] + "_elements.json")).read_text())
    assert {"Back", "◀ Shortcuts"} <= {el["text"] for el in viewer}
    actions = row["actions"][log["action_start"]:log["action_end"]]
    steps = [(a["action"], a["args"]) for a in actions]
    # Goes through the reset's own checked Home + Spotlight launch (measured: daemon launch failed after a
    # Spotlight launch), never the daemon's unchecked launch action.
    assert ("launch", {"app": "Shortcuts"}) not in steps and steps[0] == ("home", {})
    order = [("type", {"text": "Shortcuts"}), ("key", {"combo": "enter"}),
             ("tap_element", {"id": "notes_shortcut"}), ("move", {"x": 187.5, "y": 406.0})]
    assert [s for s in steps if s in order] == order
    assert [a["args"]["id"] for a in row["actions"] if a["action"] == "tap_element"] == ["notes_shortcut", "t_back"]
    assert daemon.current_screen == "home" and daemon.notes_resume_screen == "notes_list"


@pytest.mark.parametrize("tile", [None, "noteslist extra", "Noteslist"])
def test_missing_or_inexact_shortcut_tile_fails_without_tapping(tmp_path, tile):
    daemon = FakeDaemon(shortcut_tile=tile)
    row = reset(daemon, tmp_path, NotesExitOptions(strategies=("shortcut",)))
    assert not row["verified"] and not row["strategies"][0]["verified"]
    assert not any(a["action"].startswith("tap") for a in row["actions"])
    assert daemon.notes_editing  # Shortcuts' visible pointer cannot count as Notes success.


def test_missing_tile_falls_back_to_enabled_fka(tmp_path):
    row = reset(FakeDaemon(shortcut_tile=None, fka_on=True), tmp_path, NotesExitOptions(fka_on=True))
    assert row["verified"]
    assert [(s["strategy"], s["verified"]) for s in row["strategies"]] == [
        ("shortcut_spotlight", False), ("shortcut", False), ("fka_find", True)]


@pytest.mark.parametrize("fault", ["duplicate", "disabled", "wrong_screen"])
def test_shortcut_requires_unique_enabled_tile_on_all_shortcuts(tmp_path, fault):
    class BadTile(FakeDaemon):
        def _reference_elements(self):
            elements = super()._reference_elements()
            if self.current_screen == "shortcuts":
                if fault == "duplicate":
                    elements.append(_make_element("duplicate", "noteslist"))
                elif fault == "disabled":
                    next(el for el in elements if el["id"] == "notes_shortcut")["enabled"] = False
                else:
                    elements = [el for el in elements if el["id"] != "shortcuts_title"]
            return elements
    row = reset(BadTile(), tmp_path, NotesExitOptions(strategies=("shortcut",)))
    assert not row["verified"]
    assert not any(a["action"].startswith("tap") for a in row["actions"])


def test_shortcut_name_from_env(monkeypatch):
    monkeypatch.setenv("IPHONE_HAND_NOTES_SHORTCUT_NAME", "custom tile")
    assert NotesExitOptions.from_env().shortcut_name == "custom tile"


@pytest.mark.parametrize("failure", ["open", "close", "restores_editor"])
def test_switcher_failure_falls_back_to_fka_and_stops(tmp_path, failure):
    daemon = FakeDaemon(fka_on=True, switcher_opens=failure != "open",
                        switcher_closes=failure != "close", kill_returns_list=failure != "restores_editor")
    row = reset(daemon, tmp_path, NotesExitOptions(strategies=("switcher_kill", "fka_find", "esc"), fka_on=True, switcher_pause_s=0))
    assert row["verified"]
    assert [s["strategy"] for s in row["strategies"]] == ["switcher_kill", "fka_find"]
    assert [s["verified"] for s in row["strategies"]] == [False, True]
    if failure == "open":
        assert len([a for a in row["actions"] if a["action"] == "drag"]) == 1


def test_fka_requires_explicit_opt_in_and_all_failures_persist(tmp_path):
    daemon = FakeDaemon(fka_on=True, shortcut_tile=None)
    scenario = load_scenario(Path("bench/scenarios/00_benchy.yaml"))
    scenario.start_state.settle_s = 0
    runner = BenchmarkRunner(DaemonClient(base_url="", fake_daemon=daemon), tmp_path,
                             ground_truth={"ios_version": "synthetic"},
                             notes_exit=NotesExitOptions(switcher_pause_s=0))
    result = runner.run_scenario(scenario, lambda *a: pytest.fail("Agent started"))
    data = json.loads((result.run_dir / "result.json").read_text())
    assert data["verdict"] == "SETUP_FAILED" and not data["agent_started"]
    log = data["resets"][-1]["strategies"]
    assert [s["status"] for s in log] == ["failed", "failed", "skipped", "failed"]
    assert not log[2]["attempted"]
    assert not any(a["params"].get("combo") == "ctrl+tab" for a in daemon.action_log)
    assert data["resets"][-1]["notes_exit_options"]["strategies"] == ["shortcut_spotlight", "shortcut", "fka_find", "esc"]
    for attempt in (log[1], log[3]):  # the tile attempt and Esc both observed after acting
        assert attempt["verification"] is not None
        assert (result.run_dir / (attempt["verification"]["observation"] + "_frame.jpg")).exists()


@pytest.mark.parametrize("error_shape", ["flat", "nested", "http"])
def test_pointer_hidden_detects_editor_when_done_ocr_missing(tmp_path, error_shape):
    class MissingDone(FakeDaemon):
        def _reference_elements(self):
            return [el for el in super()._reference_elements() if el["id"] != "btn_done"]

        def handle_action(self, action, params):
            response = super().handle_action(action, params)
            if response.get("error_class") == "PointerHidden":
                if error_shape == "nested":
                    return {"result": response}
                if error_shape == "http":
                    raise DaemonError("private transport detail", error_class="PointerHidden")
            return response

    row = reset(MissingDone(), tmp_path)
    assert row["verified"] and row["editing_detected"] == "PointerHidden"
    assert row["strategies"][0]["verified"]
    assert any(a.get("error_class") == "PointerHidden" for a in row["actions"])
    assert "private transport detail" not in json.dumps(row)


@pytest.mark.parametrize("done_box", [(300, 130, 365, 160), (20, 50, 80, 80),
                                       (281.25, 50, 365, 80), (300, 97.44, 365, 130)])
def test_done_outside_strict_nav_region_does_not_trigger_exit(tmp_path, done_box):
    class OtherDone(FakeDaemon):
        def _reference_elements(self):
            els = super()._reference_elements()
            if self.current_screen == "notes_list":
                els.append(_make_element("body_done", "Done", bounds=done_box))
            return els
    daemon = OtherDone()
    daemon.notes_resume_screen = "notes_list"
    row = reset(daemon, tmp_path)
    assert row["verified"] and row["strategies"] == []


@pytest.mark.parametrize("strategy", ["shortcut", "fka_find"])
@pytest.mark.parametrize("fault", ["wrong_app", "pointer_still_hidden", "done_still_visible"])
def test_strategy_verification_requires_all_conditions(tmp_path, fault, strategy):
    class FalseSuccess(FakeDaemon):
        def handle_action(self, action, params):
            response = super().handle_action(action, params)
            if (action == "key" and params.get("combo") == "return"
                    or action == "tap_element" and params.get("id") == "notes_shortcut"):
                if fault == "wrong_app":
                    self.current_app = "Reminders"
            if action == "move" and fault == "pointer_still_hidden":
                return {"ok": False, "error_class": "PointerHidden"}
            return response

        def _reference_elements(self):
            els = super()._reference_elements()
            if fault == "done_still_visible" and self.current_screen == "notes" and not self.notes_editing:
                els.append(_make_element("stale_done", "Done", bounds=(310, 50, 365, 80)))
            return els
    row = reset(FalseSuccess(fka_on=True), tmp_path, NotesExitOptions(strategies=(strategy,), fka_on=True))
    assert not row["verified"] and not row["strategies"][0]["verified"]


def test_pointer_hidden_on_back_action_also_triggers_exit(tmp_path):
    class LateEditing(FakeDaemon):
        def handle_action(self, action, params):
            if action == "tap_element" and params.get("id") == "t_back" and not self.notes_from_shortcuts:
                self.notes_editing = True
            return super().handle_action(action, params)
    daemon = LateEditing()
    daemon.notes_editing = False
    row = reset(daemon, tmp_path)
    assert row["verified"] and row["editing_detected"] == "PointerHidden"


def test_unknown_pointer_error_does_not_trigger_strategy(tmp_path):
    class BrokenMove(FakeDaemon):
        def handle_action(self, action, params):
            if action == "move":
                return {"ok": False, "error": "private detail"}
            return super().handle_action(action, params)
    daemon = BrokenMove()
    daemon.notes_resume_screen = "notes_list"
    row = reset(daemon, tmp_path)
    assert not row["verified"] and row["strategies"] == []
    assert "private detail" not in json.dumps(row)


@pytest.mark.parametrize("size", [(375, 812), (390, 844), (320, 568)])
def test_custom_gestures_scale_and_options_are_logged(tmp_path, size):
    options = NotesExitOptions(strategies=("switcher_kill",), open_drag=(.52, .98, .48, .4, 55), close_drag=(.48, .55, .52, .08, 25),
                               switcher_pause_s=0, probe=(.4, .3))
    row = reset(FakeDaemon(screen_pt=size), tmp_path, options)
    assert row["verified"]
    drags = [a["args"] for a in row["actions"] if a["action"] == "drag"]
    for actual, expected in zip(drags, (options.open_drag, options.close_drag)):
        x1, y1, x2, y2, steps = expected
        assert actual == dict(x1=x1 * size[0], y1=y1 * size[1], x2=x2 * size[0], y2=y2 * size[1], steps=steps)
    assert row["notes_exit_options"]["open_drag"] == options.open_drag


def test_configurable_switcher_open_shortcut(tmp_path):
    class SyntheticShortcut(FakeDaemon):
        def handle_action(self, action, params):
            response = super().handle_action(action, params)
            if action == "key" and params.get("combo") == "synthetic_test_only":
                self.current_app, self.current_screen = "App Switcher", "app_switcher"
            return response
    row = reset(SyntheticShortcut(), tmp_path, NotesExitOptions(strategies=("switcher_kill",), switcher_combo="synthetic_test_only", switcher_pause_s=0))
    assert row["verified"]
    assert len([a for a in row["actions"] if a["action"] == "drag"]) == 1


def test_env_order_and_explicit_override(tmp_path, monkeypatch):
    monkeypatch.setenv("IPHONE_HAND_NOTES_EXIT_STRATEGIES", "esc,switcher_kill")
    options = replace(NotesExitOptions.from_env(), switcher_pause_s=0)
    row = reset(FakeDaemon(), tmp_path, options)
    assert [s["strategy"] for s in row["strategies"]] == ["esc", "switcher_kill"]
    assert row["verified"]
    row = reset(FakeDaemon(), tmp_path, NotesExitOptions(strategies=("esc",)))
    assert not row["verified"] and len(row["strategies"]) == 1


@pytest.mark.parametrize("options", [NotesExitOptions(strategies=()), NotesExitOptions(strategies=("bad",)),
    NotesExitOptions(strategies=("esc", "esc")), NotesExitOptions(open_drag=(0, 0, 2, 0, 10)),
    NotesExitOptions(close_drag=(0, 0, 1, 1, 1.5)), NotesExitOptions(switcher_pause_s=float("nan")),
    NotesExitOptions(probe=(1, 2)), NotesExitOptions(shortcut_name=""),
    NotesExitOptions(shortcut_name=None)])
def test_invalid_config_fails_before_actions(tmp_path, options):
    daemon = FakeDaemon()
    row = reset(daemon, tmp_path, options)
    assert not row["verified"] and not daemon.action_log


def test_foreground_fallback_without_daemon_app_field(tmp_path):
    class NoAppField(FakeDaemon):
        def get_state_dict(self):
            state = super().get_state_dict()
            del state["current_app"]
            return state
    assert reset(NoAppField(), tmp_path)["verified"]


@pytest.mark.parametrize("back_taps", [2, 5])
def test_back_recovery_repeats_but_stays_bounded(tmp_path, back_taps):
    class NestedBack(FakeDaemon):
        taps = 0

        def handle_action(self, action, params):
            response = super().handle_action(action, params)
            if action == "tap_element" and params.get("id") == "t_back":
                self.taps += 1
                if self.taps < back_taps:
                    self.current_screen = self.notes_resume_screen = "notes"
            return response

    daemon = NestedBack()
    row = reset(daemon, tmp_path)
    assert row["verified"] is (back_taps <= 4)
    assert daemon.taps == min(back_taps, 4)
    assert row["strategies"][0]["verified"]  # Exit success alone cannot complete setup.


def test_http_pointer_hidden_classification_without_network(monkeypatch):
    def fail(*args, **kwargs):
        raise urllib.error.HTTPError("", 503, "synthetic", {},
            io.BytesIO(b'{"error":"private detail","error_class":"PointerHidden"}'))
    monkeypatch.setattr("urllib.request.urlopen", fail)
    with pytest.raises(DaemonError) as error:
        DaemonClient(base_url="http://unused.invalid").key("esc")
    assert error.value.error_class == "PointerHidden"


def test_switcher_reopening_nonediting_editor_is_not_list_success(tmp_path):
    class RestoresViewer(FakeDaemon):
        def handle_action(self, action, params):
            response = super().handle_action(action, params)
            if action == "launch" and params.get("app") == "Notes":
                self.notes_editing = False
            return response
    row = reset(RestoresViewer(kill_returns_list=False), tmp_path,
                NotesExitOptions(strategies=("switcher_kill",), switcher_pause_s=0))
    assert not row["verified"]
    verification = row["strategies"][0]["verification"]
    assert verification["no_nav_done"] and verification["pointer_visible"]
    assert not verification["notes_list"]


@pytest.mark.parametrize("fault", ["capture", "action"])
def test_strategy_exceptions_are_logged_and_sanitized(tmp_path, fault):
    class BrokenStrategy(FakeDaemon):
        def handle_action(self, action, params):
            response = super().handle_action(action, params)
            if action == "key" and params.get("combo") == "esc" and fault == "action":
                raise RuntimeError("private diagnostic")
            return response

        def get_frame_image(self, scale=None):
            if not self.notes_keyboard and fault == "capture":
                raise RuntimeError("private diagnostic")
            return super().get_frame_image(scale)
    row = reset(BrokenStrategy(), tmp_path, NotesExitOptions(strategies=("esc",)))
    assert not row["verified"]
    log, = row["strategies"]
    assert log["attempted"] and not log["verified"] and log["error"]
    assert isinstance(log["verification"], dict)
    assert "private diagnostic" not in json.dumps(row)


def test_no_done_in_other_app_does_not_establish_notes_foreground(tmp_path):
    class WrongLaunch(FakeDaemon):
        def handle_action(self, action, params):
            response = super().handle_action(action, params)
            if self.current_app == "Notes":
                self.current_app = "Reminders"
            return response
    row = reset(WrongLaunch(), tmp_path)
    assert not row["verified"] and not row["strategies"]


def test_unknown_app_without_chrome_fails_closed(tmp_path):
    class NoIdentity(FakeDaemon):
        def get_state_dict(self):
            state = super().get_state_dict()
            del state["current_app"]
            return state

        def _reference_elements(self):
            if self.current_screen == "notes":
                return [_make_element("done", "Done", bounds=(310, 50, 365, 80))]
            return super()._reference_elements()
    row = reset(NoIdentity(), tmp_path)
    assert not row["verified"] and not row["strategies"]


def test_runner_cli_wires_explicit_options_without_network(tmp_path, monkeypatch):
    from bench import runner as module
    class FakeServerStub:
        url = ""
        def __init__(self, **kwargs):
            pass
        def start(self):
            pass
        def stop(self):
            pass
    monkeypatch.setattr(module, "FakeDaemonServer", FakeServerStub)
    captured = []
    def run_benchmark(self, *args, **kwargs):
        captured.append(self.notes_exit)
        return []
    monkeypatch.setattr(module.BenchmarkRunner, "run_benchmark", run_benchmark)
    monkeypatch.setenv("IPHONE_HAND_NOTES_EXIT_STRATEGIES", "esc")
    monkeypatch.setenv("IPHONE_HAND_NOTES_SHORTCUT_NAME", "envtile")
    monkeypatch.setattr("sys.argv", ["bench.runner", "--fake-daemon", "--scenario", "00_benchy",
        "--ground-truth", "ios_version=synthetic", "--output-dir", str(tmp_path),
        "--notes-exit-strategies", "fka_find,switcher_kill", "--notes-fka-on",
        "--notes-shortcut-name", "customtile",
        "--notes-switcher-open-drag", ".5", ".98", ".5", ".4", "30",
        "--notes-switcher-close-drag", ".5", ".6", ".5", ".1", "15",
        "--notes-switcher-pause", "0", "--notes-switcher-combo", "synthetic_test_only"])
    module.main()
    options, = captured
    assert options.strategies == ("fka_find", "switcher_kill") and options.fka_on
    assert options.shortcut_name == "customtile"
    assert options.open_drag == (.5, .98, .5, .4, 30)
    assert options.close_drag == (.5, .6, .5, .1, 15)
    assert options.switcher_pause_s == 0 and options.switcher_combo == "synthetic_test_only"


@pytest.mark.parametrize("args", [["--notes-exit-strategies", "unknown"],
    ["--notes-switcher-open-drag", "0", "0", "1", "1", "1.5"],
    ["--notes-switcher-pause", "-1"]])
def test_invalid_cli_config_stops_before_daemon_creation(monkeypatch, args):
    from bench import runner as module
    monkeypatch.setattr(module, "FakeDaemonServer", lambda **k: pytest.fail("Daemon constructed"))
    monkeypatch.setattr("sys.argv", ["bench.runner", "--fake-daemon", *args])
    with pytest.raises(SystemExit) as error:
        module.main()
    assert error.value.code == 2


def test_registered_strategy_uses_shared_verification(tmp_path, monkeypatch):
    from bench.resets import NOTES_EXIT_STRATEGIES
    monkeypatch.setitem(NOTES_EXIT_STRATEGIES, "synthetic_plugin",
                        lambda runner: runner.action("key", combo="esc"))
    row = reset(FakeDaemon(esc_exits_editing=True), tmp_path,
                NotesExitOptions(strategies=("synthetic_plugin",)))
    assert row["verified"] and row["strategies"][0]["verified"]


def test_pointer_hidden_on_launch_acknowledgement_triggers_exit(tmp_path):
    class HiddenAfterLaunch(FakeDaemon):
        def handle_action(self, action, params):
            response = super().handle_action(action, params)
            if action == "key" and params.get("combo") == "enter" and self.current_app == "Notes":
                return {"ok": False, "error_class": "PointerHidden"}
            return response
    row = reset(HiddenAfterLaunch(), tmp_path)
    assert row["verified"] and row["editing_detected"] == "PointerHidden"


def test_shortcut_spotlight_runs_indexed_shortcut_without_the_pointer(tmp_path):
    """Measured 2026-10-15: Spotlight Top Hit 'noteslist' + Return ends Notes editing, keyboard only."""
    daemon = FakeDaemon()
    row = reset(daemon, tmp_path, NotesExitOptions(switcher_pause_s=0))
    assert row["verified"]
    first = row["strategies"][0]
    assert first["strategy"] == "shortcut_spotlight" and first["verified"]
    taps = [a for a in row["actions"][first["action_start"]:first["action_end"]] if a["action"] in ("tap", "tap_element")]
    assert taps == []  # no pointer needed
    typed = [a["args"]["text"] for a in row["actions"] if a["action"] == "type"]
    assert "noteslist" in typed
