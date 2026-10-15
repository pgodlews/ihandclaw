"""Synthetic tours: in-process fake only; any network request fails the test."""
import json
from pathlib import Path
import re
import urllib.request

from PIL import Image
import pytest

from bench.capture_tour import TourError, build_tour, load_token, main, read_version, run_tour, target_id
from bench.daemon_client import DaemonClient
from bench.fake_daemon import CaptureTourFakeDaemon, _make_element


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Capture-tour tests must never call a network endpoint")
    monkeypatch.setattr(urllib.request, "urlopen", forbidden)



def scripted(daemon):
    """Daemon actions minus the select-all the tour sends before typing into Spotlight."""
    log = daemon.action_log
    return [entry for index, entry in enumerate(log)
            if not (entry["params"].get("combo") == "cmd+a" and index
                    and log[index - 1]["params"].get("combo") == "cmd+space")]

def run(tmp_path, daemon=None, preset="default", **kwargs):
    daemon = daemon or CaptureTourFakeDaemon()
    client = DaemonClient(base_url="", fake_daemon=daemon)
    directory = run_tour(client, tmp_path, "fixture", preset, sleep=lambda _: None, **kwargs)
    return daemon, directory


def rows(directory):
    return [json.loads(line) for line in (directory / "tour.jsonl").read_text().splitlines()]


def test_default_step_order():
    steps = build_tour("fixture")
    assert [s.name for s in steps] == [
        "start", "home", "spotlight_empty", "spotlight_calc", "calculator_fresh",
        "calculator_clear", "calculator_partial", "calculator_full", "calculator_result",
        "home_after_calculator", "spotlight_settings_empty", "spotlight_settings",
        "settings_root", "general", "about_scroll", "about_ios_version", "home_after_about",
        "spotlight_notes_empty", "spotlight_notes", "notes_list", "note_editor_keyboard",
        "note_title", "note_three_lines",
    ]
    assert [(s.action, s.args) for s in steps[5:9]] == [
        ("type", {"text": "1234*"}), ("type", {"text": "56"}),
        ("type", {"text": "="}), ("home", {}),
    ]
    assert all(s.settle_s > 0 for s in steps)
    assert not any(s.args.get("combo") == "cmd+n" for s in steps)


@pytest.mark.parametrize("preset", ["default", "recovery"])
def test_complete_artifacts_and_pre_action_pairing(tmp_path, preset):
    daemon = CaptureTourFakeDaemon()
    daemon.notes_text = "Existing note must survive"
    daemon, directory = run(tmp_path, daemon, preset)
    trace = rows(directory)
    actions = trace[:-1]
    assert len(actions) == len(scripted(daemon)) == len(build_tour("fixture", preset))
    for index, (row, action) in enumerate(zip(actions, scripted(daemon))):
        assert row["step"] == index
        assert row["action"] == action["action"]
        assert row["args"] == action["params"]
        assert row["action_attempted"] and row["action_ok"]
        prefix = f"step_{index:03d}"
        elements = json.loads((directory / f"{prefix}_elements.json").read_text())
        assert isinstance(elements, list)
        for el in elements:
            assert re.fullmatch(r"\[\d+,\d+\]\[\d+,\d+\]", el["bounds"])
            assert list(map(int, re.findall(r"\d+", el["bounds"]))) == el["bounds_pt"]
        assert (directory / f"{prefix}_state.json").exists()
        with Image.open(directory / f"{prefix}_frame.jpg") as frame:
            assert frame.format == "JPEG"
            assert frame.size == (750, 1624)
    by_name = {row["name"]: row for row in actions}
    def observation(name):
        return json.loads((directory / (by_name[name]["observation"] + "_elements.json")).read_text())
    assert any(el["text"] == "Calc" for el in observation("spotlight_calc"))
    assert any(el["text"] == "1234*" for el in observation("calculator_partial"))
    assert any(el["text"] == "1234*56" for el in observation("calculator_full"))
    assert any(el["text"] == "69,104" for el in observation("calculator_result"))
    assert by_name["note_title"]["args"]["text"] == "\niOS 18.0\nResult: 69104"
    assert daemon.notes_text == "Benchy fixture\niOS 18.0\nResult: 69104"
    assert daemon.created_notes == 1 and daemon.previous_notes == ["Existing note must survive"]
    for name in ("note_editor_keyboard", "note_title", "note_three_lines"):
        assert by_name[name]["pointer_visible"] is False
    assert trace[-1]["pointer_visible"] is True and trace[-1]["action"] is None
    assert (directory / "final_frame.jpg").exists()
    assert isinstance(json.loads((directory / "final_elements.json").read_text()), list)
    result = json.loads((directory / "result.json").read_text())
    assert result["scenario_id"] == "00_benchy" and result["model"] == "scripted-tour"
    assert result["checkpoints"] == {} and result["declaration"] is None
    assert result["provenance"] == "synthetic" and result["status"] == "captured"
    goal = json.loads((directory / "droidclaw-workflow.json").read_text())["steps"][0]["goal"]
    assert "1234 x 56" in goal and "Benchy fixture" in goal
    assert "BENCH_RESULT" in goal and "18.0" not in goal  # The goal still asks to read the version.
    assert not any(name in (directory / "result.json").read_text() for name in ("base_url", "token", "daemon_url"))


def test_recovery_observations_and_corrective_actions(tmp_path):
    daemon, directory = run(tmp_path, preset="recovery")
    by_name = {row["name"]: row for row in rows(directory)}
    for name, action, args in [
        ("spotlight_web_recover", "key", {"combo": "cmd+a"}),
        ("calculator_garbage_recover", "key", {"combo": "esc"}),
        ("settings_apps_recover", "back", {}),
        ("notes_search_recover", "key", {"combo": "esc"}),
    ]:
        row = by_name[name]
        assert row["validated"] and row["action"] == action and row["args"] == args
    assert by_name["spotlight_replace_query"]["args"] == {"text": "Calc"}
    assert by_name["notes_search_recover"]["pointer_visible"] is False


@pytest.mark.parametrize("broken_screen", ["spotlight", "calculator", "settings_root", "settings_general",
                                            "settings_about", "notes_list", "notes_editor"])
def test_stops_before_action_on_unexpected_screen(tmp_path, broken_screen):
    class Broken(CaptureTourFakeDaemon):
        def get_elements_list(self):
            if self.current_screen == broken_screen:
                return [_make_element("unexpected", "Unrelated screen")]
            return super().get_elements_list()
    daemon = Broken()
    with pytest.raises(TourError, match="Unexpected screen"):
        run(tmp_path, daemon)
    directory = tmp_path / "fixture"
    trace = rows(directory)
    assert trace[-1]["observation"] == "final" and not trace[-1]["validated"]
    rejected = trace[-2]
    assert rejected["validated"] is False and rejected["action_attempted"] is False
    assert len(scripted(daemon)) == rejected["step"]
    assert (directory / (rejected["observation"] + "_frame.jpg")).exists()
    assert (directory / "final_elements.json").exists()
    assert json.loads((directory / "result.json").read_text())["status"] == "failed"


@pytest.mark.parametrize("failure", ["wrong-result", "hidden-after-esc", "visible-editor", "old-note", "bad-version"])
def test_specific_screen_content_and_pointer_fail_closed(tmp_path, failure):
    class Broken(CaptureTourFakeDaemon):
        def handle_action(self, action, params):
            response = super().handle_action(action, params)
            if failure == "wrong-result" and params.get("text") == "=":
                self.calc_display = "111"
            if failure == "old-note" and params.get("id") == "compose":
                self.notes_text = "Do not overwrite this existing note"
            return response
        def get_state_dict(self):
            state = super().get_state_dict()
            if self.current_screen == "notes_editor":
                if failure == "hidden-after-esc" and not self.editing:
                    state["pointer"] = None
                elif failure == "visible-editor" and self.editing:
                    state["pointer"] = {"x": 1, "y": 1}
            return state
    daemon = Broken()
    if failure == "bad-version":
        daemon.ios_version = "Unrecognized"
    with pytest.raises(TourError):
        run(tmp_path, daemon)
    assert json.loads((tmp_path / "fixture/result.json").read_text())["status"] == "failed"
    if failure == "old-note":
        assert daemon.notes_text == "Do not overwrite this existing note"


def test_rejected_action_preserved_and_not_retried(tmp_path):
    class Rejected(CaptureTourFakeDaemon):
        def handle_action(self, action, params):
            if params.get("combo") == "enter":
                return {"result": {"status": "off-path"}}
            return super().handle_action(action, params)
    with pytest.raises(TourError, match="rejected"):
        run(tmp_path, Rejected())
    trace = rows(tmp_path / "fixture")
    assert trace[-2]["name"] == "spotlight_calc"
    assert trace[-2]["action_attempted"] and not trace[-2]["action_ok"]
    assert (tmp_path / "fixture/final_frame.jpg").exists()


def test_dry_run_prints_plan_without_client_or_files(tmp_path, capsys, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Dry run must not initialize a daemon client or read auth")
    monkeypatch.setattr("bench.capture_tour.DaemonClient", forbidden)
    monkeypatch.setattr("bench.capture_tour.load_token", forbidden)
    out = tmp_path / "absent"
    assert main(["--dry-run", "--out", str(out), "--tour-id", "fixture"]) == 0
    plan = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [row["name"] for row in plan] == [s.name for s in build_tour("fixture")]
    assert plan[-2]["args"]["text"].startswith("\niOS {ios_version}")
    assert not out.exists()


def test_fake_cli_and_fresh_run_directory(tmp_path, capsys):
    args = ["--fake", "--preset", "recovery", "--out", str(tmp_path), "--tour-id", "fixture"]
    assert main(args) == 0
    original = (tmp_path / "fixture/tour.jsonl").read_bytes()
    assert main(args) == 1
    assert "already exists" in capsys.readouterr().err
    assert (tmp_path / "fixture/tour.jsonl").read_bytes() == original


def test_current_elements_required_and_compose_geometry():
    state = {"screen": {"pt": [375, 812]}}
    vc = _make_element("vc12", "12", "vc_item", bounds=(310, 755, 350, 790))
    assert target_id("compose", [vc], state) == "vc12"
    wrong = _make_element("vc13", "13", "vc_item", bounds=(10, 755, 50, 790))
    with pytest.raises(TourError):
        target_id("compose", [wrong], state)
    with pytest.raises(TourError):
        target_id("compose", [vc, {**vc, "id": "vc14"}], state)
    with pytest.raises(TourError):
        target_id("General", [_make_element("disabled", "General", enabled=False)], state)


def test_version_row_reading_avoids_unrelated_numbers():
    label = _make_element("label", "iOS Version", bounds=(10, 200, 170, 230))
    value = _make_element("value", "18.7.1", bounds=(220, 200, 350, 230))
    unrelated = _make_element("other", "19.2", bounds=(220, 300, 350, 330))
    assert read_version([label, value, unrelated]) == "18.7.1"
    assert read_version([_make_element("joined", "iOS Version 18.7.1")]) == "18.7.1"
    with pytest.raises(TourError):
        read_version([label, unrelated])
    with pytest.raises(TourError):
        read_version([label, value, {**value, "text": "18.6"}])


def test_token_env_precedes_spec_config_file(tmp_path, monkeypatch):
    config = tmp_path / "daemon.toml"
    config.write_text('token = "fixture-file-value"\n')
    monkeypatch.setenv("IPHONE_HAND_TOKEN", "fixture-env-value")
    assert load_token(config) == "fixture-env-value"
    monkeypatch.delenv("IPHONE_HAND_TOKEN")
    assert load_token(config) == "fixture-file-value"
    config.write_text("invalid config")
    with pytest.raises(TourError):
        load_token(config)


def test_custom_operands_are_consistent(tmp_path):
    daemon, directory = run(tmp_path, calc_a=4321, calc_b=12)
    assert daemon.notes_text.endswith("Result: 51852")
    goal = json.loads((directory / "droidclaw-workflow.json").read_text())["steps"][0]["goal"]
    assert "4321 x 12" in goal


@pytest.mark.parametrize("tour_id,a,b", [("../escape", 1234, 56), ("id", 10, 56), ("id", 1234, 100)])
def test_bad_plan_inputs_rejected(tour_id, a, b):
    with pytest.raises(ValueError):
        build_tour(tour_id, calc_a=a, calc_b=b)


def test_whole_screen_ocr_chrome_does_not_make_blank_note_nonempty(tmp_path):
    class WithChrome(CaptureTourFakeDaemon):
        def get_elements_list(self):
            elements = super().get_elements_list()
            if self.current_screen == "notes_editor":
                elements.extend([
                    _make_element("time", "9:41", bounds=(10, 10, 80, 35)),
                    _make_element("suggestion", "Hello", bounds=(10, 600, 80, 635)),
                    _make_element("vc20", "20", "vc_item", bounds=(10, 200, 60, 250)),
                ])
            return elements
    daemon, _ = run(tmp_path, WithChrome())
    assert daemon.notes_text.startswith("Benchy fixture")


def test_even_one_existing_character_prevents_typing(tmp_path):
    class Existing(CaptureTourFakeDaemon):
        def handle_action(self, action, params):
            result = super().handle_action(action, params)
            if params.get("id") == "compose":
                self.notes_text = "x"
            return result
    daemon = Existing()
    with pytest.raises(TourError):
        run(tmp_path, daemon)
    assert daemon.notes_text == "x"


def test_display_below_quarter_screen_but_above_keypad(tmp_path):
    class LowerDisplay(CaptureTourFakeDaemon):
        def get_elements_list(self):
            elements = super().get_elements_list()
            if self.current_screen == "calculator":
                for el in elements:
                    if el["id"] == "t_display":
                        el.update(bounds="[20,185][355,215]", bounds_pt=[20, 185, 355, 215])
            return elements
    run(tmp_path, LowerDisplay())


def test_existing_title_just_below_navigation_is_not_ignored(tmp_path):
    class ExistingTitle(CaptureTourFakeDaemon):
        def get_elements_list(self):
            elements = super().get_elements_list()
            if self.current_screen == "notes_editor":
                for el in elements:
                    if el["id"] == "done":
                        el.update(bounds="[310,50][365,80]", bounds_pt=[310, 50, 365, 80])
                elements.append(_make_element("old_title", "Existing title", bounds=(20, 110, 350, 140)))
            return elements
    daemon = ExistingTitle()
    with pytest.raises(TourError):
        run(tmp_path, daemon)
    assert not any(entry["params"].get("text", "").startswith("Benchy ") for entry in scripted(daemon))


@pytest.mark.parametrize("display", ["84 976+3+3+", "(-6)x3%", "((-5 684))", "32 111 119 314"])
def test_leftover_input_without_clear_label_esc_clears(tmp_path, display):
    daemon = CaptureTourFakeDaemon()
    daemon.calc_buffer = daemon.calc_display = display
    _, directory = run(tmp_path, daemon)
    first = next(row for row in rows(directory) if row["name"] == "calculator_fresh")
    assert first["action"] == "key" and first["args"] == {"combo": "esc"}
    elements = json.loads((directory / (first["observation"] + "_elements.json")).read_text())
    assert not any(el["text"] in {"AC", "C"} for el in elements)
    assert any(el["id"] == "btn_backspace" and el["text"] == "" for el in elements)
    verified = json.loads((directory / "calculator_fresh_after_initial_clear_elements.json").read_text())
    assert next(el["text"] for el in verified if el["id"] == "t_display") == "0"
    assert not any(row["name"] == "calculator_fresh_evaluate" for row in rows(directory))


@pytest.mark.parametrize("preset", ["default", "recovery"])
@pytest.mark.parametrize("input_text", ["98765", "((-5 684))"])
def test_esc_no_effect_evaluates_then_taps_observed_ac(tmp_path, preset, input_text):
    daemon = CaptureTourFakeDaemon(calc_escape_clears=False)
    daemon.calc_buffer = daemon.calc_display = input_text
    _, directory = run(tmp_path, daemon, preset)
    trace = rows(directory)
    clear_actions = [row for row in trace if row["name"].startswith("calculator_fresh")]
    assert [(row["action"], row["args"]) for row in clear_actions] == [
        ("key", {"combo": "esc"}), ("type", {"text": "="}),
        ("tap_element", {"id": "btn_ac"}),
    ]
    observed = json.loads((directory / "calculator_fresh_after_evaluate_elements.json").read_text())
    assert next(el["text"] for el in observed if el["id"] == "t_display") == (
        "98,765" if input_text.isdigit() else "Error")
    assert any(el["text"] == "AC" for el in observed)
    if preset == "recovery":
        garbage = [row for row in trace if row["name"].startswith("calculator_garbage_recover")]
        assert [(row["action"], row["args"]) for row in garbage] == [
            ("key", {"combo": "esc"}), ("type", {"text": "="}),
            ("tap_element", {"id": "btn_ac"}),
        ]
    for index, (row, action) in enumerate(zip(trace[:-1], scripted(daemon), strict=True)):
        assert row["step"] == index
        assert row["observation"] == f"step_{index:03d}"
        assert (row["action"], row["args"]) == (action["action"], action["params"])
        assert (directory / f"step_{index:03d}_elements.json").exists()


def test_error_display_cleared_by_ac_without_keyboard_recovery(tmp_path):
    daemon = CaptureTourFakeDaemon()
    daemon.calc_buffer = daemon.calc_display = "Error"
    _, directory = run(tmp_path, daemon)
    clear_actions = [row for row in rows(directory) if row["name"].startswith("calculator_fresh")]
    assert [(row["action"], row["args"]) for row in clear_actions] == [
        ("tap_element", {"id": "btn_ac"})]


@pytest.mark.parametrize("failure", ["clear-no-effect", "no-ac-after-evaluate", "ambiguous-ac", "missing-display"])
def test_calculator_setup_failure_is_bounded_and_stops(tmp_path, failure):
    class Stuck(CaptureTourFakeDaemon):
        def handle_action(self, action, params):
            response = super().handle_action(action, params)
            if failure == "clear-no-effect" and params.get("id") == "btn_ac":
                self.calc_display = "98,765"
                self.calc_evaluated = True
            return response

        def get_elements_list(self):
            elements = super().get_elements_list()
            if self.current_screen == "calculator" and self.calc_evaluated:
                if failure == "no-ac-after-evaluate":
                    elements = [el for el in elements if el["text"] != "AC"]
                elif failure == "ambiguous-ac":
                    ac = next(el for el in elements if el["text"] == "AC")
                    elements.append({**ac, "id": "duplicate_ac"})
            if self.current_screen == "calculator" and failure == "missing-display" and any(
                    entry["params"].get("text") == "=" for entry in self.action_log):
                elements = [el for el in elements if el["id"] != "t_display"]
            return elements

    daemon = Stuck(calc_escape_clears=False)
    daemon.calc_buffer = daemon.calc_display = "98765"
    with pytest.raises(TourError, match="SETUP_FAILED"):
        run(tmp_path, daemon)
    calc_actions = scripted(daemon)[4:]
    assert calc_actions[:2] == [
        {"action": "key", "params": {"combo": "esc"}},
        {"action": "type", "params": {"text": "="}},
    ]
    assert len(calc_actions) <= 3
    assert not any(entry["params"].get("text") == "1234*" for entry in scripted(daemon))
    result = json.loads((tmp_path / "fixture/result.json").read_text())
    assert result["status"] == "failed" and result["error"].startswith("SETUP_FAILED")
    assert (tmp_path / "fixture/final_elements.json").exists()


@pytest.mark.parametrize("evaluate_first", [False, True])
def test_c_then_ac_clears_entire_calculator(tmp_path, evaluate_first):
    class EntryClear(CaptureTourFakeDaemon):
        def _reference_elements(self):
            elements = super()._reference_elements()
            if self.current_screen == "calculator" and self.calc_evaluated and self.calc_display != "0":
                next(el for el in elements if el["id"] == "btn_ac")["text"] = "C"
            return elements

    daemon = EntryClear(calc_escape_clears=False)
    daemon.calc_buffer = daemon.calc_display = "98765"
    daemon.calc_evaluated = not evaluate_first
    _, directory = run(tmp_path, daemon)
    clear_actions = [row for row in rows(directory) if row["name"].startswith("calculator_fresh")]
    expected = [("key", {"combo": "esc"}), ("type", {"text": "="})] if evaluate_first else []
    expected += [("tap_element", {"id": "btn_ac"}), ("tap_element", {"id": "btn_ac"})]
    assert [(row["action"], row["args"]) for row in clear_actions] == expected
    assert clear_actions[-1]["name"] == "calculator_fresh_all_clear"


@pytest.mark.parametrize("screen_pt", [(375, 812), (430, 932)])
def test_tour_fake_real_element_format_at_each_geometry(screen_pt):
    daemon = CaptureTourFakeDaemon(screen_pt=screen_pt)
    elements = daemon.get_elements_list()
    dock = next(el for el in elements if el["id"] == "t_dock")
    assert dock["bounds_pt"] == [0, 0, screen_pt[0], round(40 * screen_pt[1] / 812)]
    for el in elements:
        assert list(map(int, re.findall(r"\d+", el["bounds"]))) == el["bounds_pt"]
    assert daemon.get_frame_image(scale="pt2").size == (2 * screen_pt[0], 2 * screen_pt[1])
