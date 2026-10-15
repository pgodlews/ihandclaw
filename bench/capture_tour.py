"""Scripted, fail-closed capture tours with bounded Calculator setup (SPEC 6/8)."""
from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
import json
import os
from pathlib import Path
import re
import secrets
import sys
import time
import tomllib
from typing import Callable

import yaml

from bench.daemon_client import DaemonClient
from bench.fake_daemon import CaptureTourFakeDaemon


class TourError(RuntimeError):
    """Safe diagnostics containing script landmarks, never raw daemon errors."""


@dataclass(frozen=True)
class Step:
    # Stage/subgoal describe the observation BEFORE this action.
    name: str
    stage: str
    active_subgoal: str
    action: str
    args: dict
    settle_s: float = 1.0  # Estimated mirror/UI settle time; not measured on hardware.
    expected: str = ""


def build_tour(tour_id: str, preset: str = "default", calc_a: int = 1234,
               calc_b: int = 56) -> list[Step]:
    if preset not in {"default", "recovery"}:
        raise ValueError("Unknown preset")
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", tour_id):
        raise ValueError("tour_id must be 1-40 ASCII letters, digits, underscores or hyphens")
    if not (1000 <= calc_a <= 9999 and 10 <= calc_b <= 99):
        raise ValueError("Calculator operands must be four and two digits")
    steps: list[Step] = []

    def add(name, stage, subgoal, action, args=None, expected=""):
        steps.append(Step(name, stage, subgoal, action, args or {}, expected=expected))

    add("start", "unknown", "calculate", "home")
    add("home", "home", "calculate", "key", {"combo": "cmd+space"}, "home")
    if preset == "recovery":
        add("spotlight_empty_wrong_query", "spotlight", "calculate", "type",
            {"text": "weather tomorrow"}, "spotlight-empty")
        add("spotlight_web_recover", "spotlight-web", "calculate", "key",
            {"combo": "cmd+a"}, "spotlight-web")
        add("spotlight_replace_query", "spotlight-web", "calculate", "type",
            {"text": "Calc"}, "spotlight-web")
    else:
        add("spotlight_empty", "spotlight", "calculate", "type", {"text": "Calc"}, "spotlight-empty")
    add("spotlight_calc", "spotlight", "calculate", "key", {"combo": "enter"}, "spotlight-calc")
    add("calculator_fresh", "calculator", "calculate", "tap_element", {"id": "@clear"}, "calculator")
    if preset == "recovery":
        add("calculator_clear_wrong_input", "calculator", "calculate", "type", {"text": "98765"}, "calc-clear")
        add("calculator_garbage_recover", "calculator", "calculate", "tap_element", {"id": "@clear"}, "calc-garbage")
    add("calculator_clear", "calculator", "calculate", "type", {"text": f"{calc_a}*"}, "calc-clear")
    add("calculator_partial", "calculator", "calculate", "type", {"text": str(calc_b)}, "calc-partial")
    add("calculator_full", "calculator", "calculate", "type", {"text": "="}, "calc-full")
    add("calculator_result", "calculator", "read-version", "home", expected="calc-result")
    add("home_after_calculator", "home", "read-version", "key", {"combo": "cmd+space"}, "home")
    add("spotlight_settings_empty", "spotlight", "read-version", "type", {"text": "Settings"}, "spotlight-empty")
    add("spotlight_settings", "spotlight", "read-version", "key", {"combo": "enter"}, "spotlight-settings")
    if preset == "recovery":
        # A fixed scroll to expose Apps; never scroll-until or guess a coordinate.
        add("settings_root_to_apps", "settings-root", "read-version", "scroll", {"dy": -6}, "settings-root")
        add("settings_apps_row", "settings-root", "read-version", "tap_element", {"id": "@Apps"}, "settings-apps-row")
        add("settings_apps_recover", "settings-apps", "read-version", "back", expected="settings-apps")
        add("settings_root_scroll_back", "settings-root", "read-version", "scroll", {"dy": 6}, "settings-root")
    add("settings_root", "settings-root", "read-version", "tap_element", {"id": "@General"}, "settings-general-row")
    add("general", "settings-general", "read-version", "tap_element", {"id": "@About"}, "general")
    add("about_scroll", "settings-about", "read-version", "scroll", {"dy": -1}, "about")
    add("about_ios_version", "settings-about", "new-note", "home", expected="about-version")
    add("home_after_about", "home", "new-note", "key", {"combo": "cmd+space"}, "home")
    add("spotlight_notes_empty", "spotlight", "new-note", "type", {"text": "Notes"}, "spotlight-empty")
    add("spotlight_notes", "spotlight", "new-note", "key", {"combo": "enter"}, "spotlight-notes")
    if preset == "recovery":
        add("notes_list_search", "notes-list", "new-note", "tap_element", {"id": "@Search"}, "notes-list")
        add("notes_search_recover", "notes-search", "new-note", "key", {"combo": "esc"}, "notes-search")
    add("notes_list", "notes-list", "new-note", "tap_element", {"id": "@compose"}, "notes-list")
    add("note_editor_keyboard", "notes-editor", "new-note", "type", {"text": f"Benchy {tour_id}"}, "note-empty")
    add("note_title", "notes-editor", "new-note", "type",
        {"text": "\niOS {ios_version}\nResult: " + str(calc_a * calc_b)}, "note-title")
    add("note_three_lines", "notes-editor", "end-editing", "key", {"combo": "esc"}, "note-complete")
    return steps


def texts(elements):
    return [str(el.get("text", "")).strip() for el in elements]


# Measured 2026-10-13 on the reference phone: English UI with a Polish keyboard ("spacja", "szukaj"),
# and OCR reads the Spotlight/Notes search field with its magnifier as "Q Search".
LABEL_ALIASES = {
    "space": ("space", "spacja"),
    "return": ("return", "go", "szukaj", "idź", "enter"),
    "Search": ("Search", "Q Search", "QSearch"),
}


def exact(elements, text):
    wanted = {label.casefold() for label in LABEL_ALIASES.get(text, (text,))}
    return [el for el in elements if el.get("enabled", True)
            and str(el.get("text", "")).strip().casefold() in wanted]


def has_label(elements, text):
    return bool(exact(elements, text))


def nav_back_button(elements, state):
    """The navigation-bar back button: left-most element in the bar below the status bar.
    Measured: OCR reads it as "Back", the previous title, or garbled ("et ngs" for "Settings")."""
    w, h = state.get("screen", {}).get("pt", [375, 812])
    bar = [el for el in elements if el.get("id") and str(el.get("text", "")).strip()
           and .04 * h <= bounds(el)[1] < .12 * h and bounds(el)[0] < .3 * w]
    return min(bar, key=lambda el: bounds(el)[0]) if bar else None


def compose_candidates(elements, state):
    try:
        target_id("compose", elements, state)
        return True
    except TourError:
        return False


def compose_point(elements, state):
    """Compose button position anchored on the observed "N Notes" footer; None if the footer is absent."""
    w, h = state.get("screen", {}).get("pt", [375, 812])
    footers = [el for el in elements if re.fullmatch(r"\d+ Notes?", str(el.get("text", "")).strip())
               and bounds(el)[1] > .85 * h]
    if len(footers) != 1:
        return None
    b = bounds(footers[0])
    return [round(.923 * w, 1), round((b[1] + b[3]) / 2, 1)]


def bounds(el):
    value = el.get("bounds_pt", el.get("bounds"))
    if isinstance(value, str):
        value = [float(v) for v in re.findall(r"-?\d+(?:\.\d+)?", value)]
    if isinstance(value, (list, tuple)) and len(value) == 4:
        return [float(v) for v in value]
    raise TourError("Expected element bounds in phone points")


def read_version(elements):
    labels = [el for el in elements if re.match(r"^iOS Version\b", str(el.get("text", "")), re.I)]
    candidates = set()
    for label in labels:
        match = re.fullmatch(r"iOS Version\s+(\d+\.\d+(?:\.\d+)?)", str(label["text"]).strip(), re.I)
        if match:
            candidates.add(match[1])
            continue
        if not exact([label], "iOS Version"):
            continue
        lb = bounds(label)
        for el in elements:
            # Read only a version on the same row to the right of the label.
            value = str(el.get("text", "")).strip()
            if not re.fullmatch(r"\d+\.\d+(?:\.\d+)?", value):
                continue
            eb = bounds(el)
            if eb[0] >= lb[2] and min(eb[3], lb[3]) > max(eb[1], lb[1]):
                candidates.add(value)
    if len(candidates) != 1:
        raise TourError("Expected one unambiguous version value on the iOS Version row")
    return candidates.pop()


def target_id(target, elements, state):
    if target == "clear":
        matches = exact(elements, "AC") or exact(elements, "C")
    elif target == "compose":
        matches = [el for label in ("New Note", "Compose") for el in exact(elements, label)]
        if not matches:
            # Catalog: compose is bottom-right; accept only a unique observed VC target.
            w, h = state.get("screen", {}).get("pt", [375, 812])
            matches = [el for el in elements if el.get("type") == "vc_item"
                       and el.get("enabled", True) and el.get("clickable", True)
                       and re.fullmatch(r"\d*", str(el.get("text", "")).strip())
                       and bounds(el)[0] > .75 * w and bounds(el)[1] > .85 * h]
    else:
        matches = exact(elements, target)
    if len(matches) != 1 or not matches[0].get("id"):
        raise TourError(f"Expected one current enabled {target} element; refusing to guess a tap")
    return matches[0]["id"]


def calculator_keypad(elements):
    """Measured 2026-10-13: OCR reads digit keys reliably but not the orange operator keys
    ("=" came back as a CJK-like glyph, "x" and "/" not at all), so the keypad is recognised by its digits."""
    digits = {str(el.get("text", "")).strip() for el in elements if el.get("enabled", True)}
    return len(digits & set("0123456789")) >= 9 or {"=", "×"} <= digits


def calculator_display(elements):
    # Read: input replaces AC/C with a backspace glyph that OCR does not label.
    # Use observed top-row keypad labels to separate display OCR from key numerals.
    top_row = [el for label in ("AC", "C", "%", "÷") for el in exact(elements, label)]
    if not top_row:
        raise TourError("Expected observed Calculator top-row keypad labels")
    keypad_top = min(bounds(el)[1] for el in top_row)
    return [re.sub(r"[ ,]", "", str(el.get("text", "")).strip())
            .replace("×", "*").replace("x", "*")
            for el in elements if bounds(el)[3] <= keypad_top]


def calculator_is_zero(elements):
    display = calculator_display(elements)
    values = {s for s in display if re.fullmatch(r"[\d().+*/%−-]+|Error", s, re.I)}
    return values == {"0"}


def check_screen(expected, elements, state, context):
    """OCR checks gate the fixed script. Fake current_screen is never used for decisions."""
    ts = texts(elements)
    has = lambda t: bool(exact(elements, t))
    contains = lambda t: any(t.casefold() in s.casefold() for s in ts)
    ok = True
    if expected == "home":
        ok = all(has(t) for t in ("Settings", "Notes")) and not has("Cancel")  # Calculator may live on another page (measured: not on the reference home page)
    elif expected.startswith("spotlight"):
        ok = has("space") or has("return") or has("Top Hit")
        if expected == "spotlight-empty":
            # Measured: iOS keeps the previous Spotlight query for a while, so "empty" only means the field is open;
            # the type step selects all first so the new text replaces any leftover query.
            ok = ok and (has("Search") or has("Top Hit") or has("Suggestions"))
        elif expected == "spotlight-web":
            ok = ok and contains("weather tomorrow") and (has("Search Web") or has("Websites"))
        else:
            query, app = {"spotlight-calc": ("Calc", "Calculator"),
                          "spotlight-settings": ("Settings", "Settings"),
                          "spotlight-notes": ("Notes", "Notes")}[expected]
            ok = ok and has(query) and has(app)
    elif expected == "calculator" or expected.startswith("calc-"):
        ok = calculator_keypad(elements) and (has("AC") or has("C") or has("%"))
        display = calculator_display(elements) if ok else []
        a, b = context["calc_a"], context["calc_b"]
        choices = {"calc-clear": {"0"}, "calc-partial": {str(a), f"{a}*"},
                   "calc-full": {str(b), f"{a}*{b}"}, "calc-result": {str(a * b)},
                   "calc-garbage": {"98765"}}
        if expected in choices:
            ok = ok and bool(set(display) & choices[expected])
        if expected == "calc-clear":
            ok = ok and calculator_is_zero(elements)
    elif expected.startswith("settings-"):
        if expected == "settings-apps":
            ok = has("Apps") and has("Settings") and has("Search") and not has("General")
        else:
            ok = has("Settings") and (has("Wi-Fi") or has("Bluetooth") or has("Apps")) and not has("About")
            if expected.endswith("general-row"):
                ok = ok and has("General")
            if expected.endswith("apps-row"):
                ok = ok and has("Apps")
    elif expected == "general":
        ok = has("General") and has("About") and has("Software Update")
    elif expected.startswith("about"):
        ok = has("About") and (has("Name") or has("Model Name"))
        if ok and expected == "about-version":
            context["ios_version"] = read_version(elements)
    elif expected == "notes-list":
        ok = has("Notes") and has("Search") and not has("Done") and not has("Cancel")
    elif expected == "notes-search":
        ok = has("Notes") and has("Search") and has("Cancel") and state.get("pointer", "missing") is None
    elif expected.startswith("note-"):
        ok = has("Notes") and (has("Done") or expected == "note-ended")
        if expected == "note-empty":
            # Ignore status bar, navigation chrome, keyboard and VC badge numerals.
            # Never type into an editor with existing body text, including a single letter.
            _, h = state.get("screen", {}).get("pt", [375, 812])
            toolbar_bottom = max((bounds(el)[3] for el in exact(elements, "Notes") + exact(elements, "Done")
                                  if bounds(el)[1] < .15 * h), default=.15 * h)
            body = [s for el, s in zip(elements, ts)
                    if toolbar_bottom <= bounds(el)[1] < .55 * h
                    and not (el.get("type") == "vc_item" and re.fullmatch(r"\d+", s))]
            # Measured: OCR may miss space/return but reads the letter keys of the on-screen keyboard.
            letters = {s for el, s in zip(elements, ts) if re.fullmatch(r"[A-Za-z]", s) and bounds(el)[1] > .7 * h}
            ok = ok and (has("space") or has("return") or len(letters) >= 5) and all(
                s.casefold() in {"title", "body", ""} for s in body)
        elif expected == "note-title":
            ok = ok and contains("Benchy " + context["tour_id"])
        elif expected in {"note-complete", "note-ended"}:
            # Measured: OCR reads "iOS" as "IOS", so compare lines case-insensitively.
            lines = [line.strip().casefold() for s in ts for line in s.splitlines()]
            ok = ok and all(line.casefold() in lines for line in (
                "Benchy " + context["tour_id"], "iOS " + context["ios_version"],
                "Result: " + str(context["calc_a"] * context["calc_b"])))
        pointer = state.get("pointer", "missing")
        ok = ok and (isinstance(pointer, dict) if expected == "note-ended" else pointer is None)
    elif expected:
        raise TourError("Unknown screen check in script")
    if not ok:
        raise TourError(f"Unexpected screen: expected {expected}; saved observation, stopped before action")


def write_json(path, data):
    path.write_text(json.dumps(data, indent=2) + "\n")


def capture(client, run_dir, prefix):
    # Like SessionRecorder, capture pre-frame/elements/state before posting an action.
    client.get_frame(scale="pt2").save(run_dir / f"{prefix}_frame.jpg", format="JPEG")
    elements = client.get_elements()
    write_json(run_dir / f"{prefix}_elements.json", elements)
    state = client.get_state()
    write_json(run_dir / f"{prefix}_state.json", state)
    return elements, state


def run_tour(client: DaemonClient, out: Path, tour_id: str, preset="default",
             calc_a=1234, calc_b=56, sleep: Callable = time.sleep) -> Path:
    steps = build_tour(tour_id, preset, calc_a, calc_b)
    run_dir = Path(out) / tour_id
    run_dir.mkdir(parents=True, exist_ok=False)  # Never replace an earlier run.
    context = dict(tour_id=tour_id, calc_a=calc_a, calc_b=calc_b)
    goal = yaml.safe_load((Path(__file__).parent / "scenarios/00_benchy.yaml").read_text())["goal"]
    for name, value in (("run_id", tour_id), ("calc_a", calc_a), ("calc_b", calc_b)):
        goal = goal.replace("{" + name + "}", str(value))
    write_json(run_dir / "droidclaw-workflow.json", {"steps": [{"goal": goal}]})
    result = dict(scenario_id="00_benchy", model="scripted-tour", tour_id=tour_id,
                  preset=preset, checkpoints={}, declaration=None, status="running",
                  provenance="synthetic" if client.fake_daemon is not None else "measured",
                  actions_completed=0)
    failure = None
    with (run_dir / "tour.jsonl").open("w") as trace:
        def record(row):
            trace.write(json.dumps(row) + "\n")
            trace.flush()

        def perform(step, allow_escape=False):
            index = result["actions_completed"]
            row = {**asdict(step), "step": index, "observation": f"step_{index:03d}",
                   "validated": False, "action_attempted": False, "action_ok": False}
            clear_label = None
            try:
                elements, state = capture(client, run_dir, row["observation"])
                if step.expected == "settings-general-row":
                    # Measured: Settings reopens on the last page (About). Tap Back, bounded, until the root list shows.
                    for _ in range(4):
                        if has_label(elements, "Wi-Fi"):
                            break
                        # Measured: the daemon's edge-swipe back did not leave Settings > General, so tap the top-left
                        # back button, which OCR reads as "Back" or the previous title ("Settings").
                        button = nav_back_button(elements, state)
                        if button is None:
                            break  # the screen check below reports the unexpected screen
                        try:
                            back = client._request("POST", "/api/tap_element", {"id": button["id"]})
                        except Exception:
                            break
                        if isinstance(back, dict) and (back.get("ok") is False or back.get("error")):
                            break
                        sleep(step.settle_s)
                        elements, state = capture(client, run_dir, row["observation"])
                if step.expected == "notes-list":
                    # Measured: Notes reopens inside the last note with the keyboard up. Bounded: end editing with esc,
                    # then tap the top-left back button ("Notes"/"Back"/"Folders") until the list check passes.
                    for _ in range(4):
                        try:
                            check_screen(step.expected, elements, state, context)
                            break
                        except TourError:
                            pass
                        # Measured: after esc the editor can keep "Done" with no keyboard; then only the back button helps.
                        if exact(elements, "space") or exact(elements, "return"):
                            request = ("/api/key", {"combo": "esc"})
                        else:
                            button = nav_back_button(elements, state)
                            if button is None:
                                break
                            request = ("/api/tap_element", {"id": button["id"]})
                        try:
                            reply = client._request("POST", *request)
                        except Exception:
                            break
                        if isinstance(reply, dict) and (reply.get("ok") is False or reply.get("error")):
                            break
                        sleep(step.settle_s)
                        elements, state = capture(client, run_dir, row["observation"])
                row["pointer_visible"] = isinstance(state.get("pointer"), dict) if "pointer" in state else None
                check_screen(step.expected, elements, state, context)
                row["validated"] = bool(step.expected)
                action, args = step.action, dict(step.args)
                if action == "tap_element":
                    target = args["id"][1:]
                    if target == "clear" and allow_escape and not (exact(elements, "AC") or exact(elements, "C")):
                        action, args = "key", {"combo": "esc"}
                    elif target == "compose" and compose_point(elements, state) and not compose_candidates(elements, state):
                        # Measured: with Voice Control numbers off the compose icon has no element; it sits on the
                        # "N Notes" footer row at the right edge (346, 756 pt on the reference XS).
                        action, args = "tap", dict(zip(("x", "y"), compose_point(elements, state)))
                    else:
                        args["id"] = target_id(target, elements, state)
                        if target == "clear":
                            clear_label = "AC" if exact(elements, "AC") else "C"
                if action == "type" and step.expected == "spotlight-empty":
                    select = client._request("POST", "/api/key", {"combo": "cmd+a"})
                    if isinstance(select, dict) and (select.get("ok") is False or select.get("error")):
                        raise TourError("Daemon rejected select-all in Spotlight; stopped without retry")
                if action == "type":
                    args["text"] = args["text"].replace("{ios_version}", context.get("ios_version", "{ios_version}"))
                    if "{ios_version}" in args["text"]:
                        raise TourError("iOS Version was not read; refusing to type a guessed version")
                row.update(action=action, args=args, action_attempted=True)
                response = client._request("POST", f"/api/{action}", args)
                detail = response.get("result") if isinstance(response, dict) else None
                for value in (response, detail):
                    if isinstance(value, dict) and (value.get("ok") is False or value.get("error") or value.get("status") == "off-path"):
                        raise TourError("Daemon rejected scripted action; stopped without retry")
                row["action_ok"] = True
                result["actions_completed"] += 1
                sleep(step.settle_s)
                return clear_label
            finally:
                record(row)

        def clear_calculator(step):
            def corrective(suffix, action, args):
                return Step(step.name + suffix, step.stage, step.active_subgoal,
                            action, args, step.settle_s, "calculator")

            def observe(suffix):
                elements, state = capture(client, run_dir, step.name + suffix)
                check_screen("calculator", elements, state, context)
                return elements

            try:
                label = perform(step, allow_escape=True)
                elements = observe("_after_initial_clear")
                if label is None:  # No AC/C: Escape support is unverified on iPhone.
                    if calculator_is_zero(elements):
                        return
                    perform(corrective("_evaluate", "type", {"text": "="}))
                    observe("_after_evaluate")
                    # Even an Error/result of zero must expose a current AC/C target.
                    label = perform(corrective("_clear_result", "tap_element", {"id": "@clear"}))
                    elements = observe("_after_clear_result")
                if label == "C":
                    # C may only clear the current entry. Require AC to reset everything.
                    perform(corrective("_all_clear", "tap_element", {"id": "@AC"}))
                    elements = observe("_after_all_clear")
                if not calculator_is_zero(elements):
                    raise TourError("Calculator display did not clear to 0")
            except TourError as exc:
                raise TourError(f"SETUP_FAILED: bounded Calculator clear stopped: {exc}") from exc

        try:
            for step in steps:
                if step.action == "tap_element" and step.args.get("id") == "@clear":
                    clear_calculator(step)
                else:
                    perform(step)
            elements, state = capture(client, run_dir, "final")
            check_screen("note-ended", elements, state, context)
            record(dict(step=result["actions_completed"], observation="final", name="note_editing_ended",
                        stage="notes-editor", active_subgoal="report", action=None, args={},
                        validated=True, pointer_visible=True))
            result["status"] = "captured"
            result["ios_version_read"] = context["ios_version"]
        except Exception as exc:
            # Do not leak endpoint credentials, private OCR or raw transport diagnostics.
            failure = exc if isinstance(exc, TourError) else TourError("Capture/action failed; inspect saved artifacts; no retry was attempted")
            result.update(status="failed", error=str(failure))
            # Preserve a post-action observation even after an action error, when available.
            if not (run_dir / "final_elements.json").exists():
                try:
                    capture(client, run_dir, "final")
                except Exception:
                    pass
            record(dict(observation="final", name="failed_final", stage="unknown",
                        active_subgoal=step.active_subgoal, action=None, args={},
                        validated=False, error=str(failure)))
        finally:
            write_json(run_dir / "result.json", result)
    if failure:
        raise failure
    return run_dir


def load_token(config: Path | None = None):
    token = os.environ.get("IPHONE_HAND_TOKEN")
    if token:
        return token
    path = config or Path.home() / ".config" / "iphone-hand" / "daemon.toml"
    try:
        with path.open("rb") as source:
            token = tomllib.load(source).get("token")
        if isinstance(token, str) and token.strip():
            return token
    except (OSError, tomllib.TOMLDecodeError):
        pass
    raise TourError("Set IPHONE_HAND_TOKEN or the token in the daemon configuration file")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--daemon-url", default=os.environ.get("IPHONE_HAND_DAEMON_URL"))
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--preset", choices=("default", "recovery"), default="default")
    parser.add_argument("--tour-id", default=None)
    parser.add_argument("--calc-a", type=int, default=1234)
    parser.add_argument("--calc-b", type=int, default=56)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--fake", action="store_true", help="In-process synthetic daemon; no network or phone")
    args = parser.parse_args(argv)
    tour_id = args.tour_id or secrets.token_hex(4)
    try:
        steps = build_tour(tour_id, args.preset, args.calc_a, args.calc_b)
        if args.dry_run:
            for index, step in enumerate(steps):
                print(json.dumps({"step": index, **asdict(step)}))
            return 0
        if args.fake:
            client = DaemonClient(base_url="", fake_daemon=CaptureTourFakeDaemon())
        else:
            if not args.daemon_url:
                raise TourError("--daemon-url or IPHONE_HAND_DAEMON_URL is required")
            client = DaemonClient(base_url=args.daemon_url, token=load_token(), timeout=60.0)  # measured: a tap_element took 14.3 s (pointer travel)
        run_dir = run_tour(client, args.out, tour_id, args.preset, args.calc_a, args.calc_b,
                           sleep=(lambda seconds: None) if args.fake else time.sleep)
        print(f"Read: {args.preset} tour artifacts saved to {run_dir}")
        return 0
    except (TourError, ValueError, FileExistsError) as exc:
        # FileExistsError includes paths; use a fixed message for that case.
        print("Read: " + ("Tour directory already exists; choose a fresh tour ID" if isinstance(exc, FileExistsError) else str(exc)), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
