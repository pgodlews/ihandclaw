"""Bounded, OCR-gated setup recipes, adapted from the scripted capture tour.

Read: ordinary targets are OCR-gated; switcher gestures are unmeasured placeholders.
Estimated: settle time comes from start_state.settle_s, not hardware measurements.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import json
import os

from .daemon_client import DaemonError
import math
from pathlib import Path
import re
import time

RESET_RECIPES = frozenset({"calculator_cleared", "notes_list"})


@dataclass(frozen=True)
class NotesExitOptions:
    """Estimated: fractional gesture coordinates/steps/pause are placeholders, not iOS measurements.

    Read: drag releases before pause; the API cannot hold at the endpoint (docs/SPEC.md §5).
    """
    strategies: tuple[str, ...] = ("shortcut_spotlight", "shortcut", "fka_find", "esc")
    shortcut_name: str = "noteslist"
    fka_on: bool = False
    switcher_combo: str | None = None  # Only set to an independently measured shortcut.
    open_drag: tuple[float, float, float, float, int] = (.5, .99, .5, .45, 40)
    close_drag: tuple[float, float, float, float, int] = (.5, .5, .5, .05, 20)
    switcher_pause_s: float = .5
    probe: tuple[float, float] = (.5, .5)

    @classmethod
    def from_env(cls):
        order = os.environ.get("IPHONE_HAND_NOTES_EXIT_STRATEGIES")
        return cls(strategies=tuple(v.strip() for v in order.split(",")) if order is not None
                   else cls.strategies,
                   shortcut_name=os.environ.get("IPHONE_HAND_NOTES_SHORTCUT_NAME", cls.shortcut_name))

    def validate(self):
        if not isinstance(self.shortcut_name, str) or not self.shortcut_name.strip():
            raise SetupError("Invalid Notes shortcut name")
        if type(self.fka_on) is not bool or (self.switcher_combo is not None and
                (not isinstance(self.switcher_combo, str) or not self.switcher_combo.strip())):
            raise SetupError("Invalid Notes keyboard options")
        if (not self.strategies or len(set(self.strategies)) != len(self.strategies)
                or any(s not in NOTES_EXIT_STRATEGIES for s in self.strategies)):
            raise SetupError("Invalid Notes exit strategy order")
        for gesture in (self.open_drag, self.close_drag):
            if (len(gesture) != 5 or any(not math.isfinite(v) or not 0 <= v <= 1 for v in gesture[:4])
                    or type(gesture[4]) is not int or not 1 <= gesture[4] <= 200):
                raise SetupError("Invalid Notes switcher gesture")
        if (not math.isfinite(self.switcher_pause_s) or not 0 <= self.switcher_pause_s <= 10
                or len(self.probe) != 2 or any(not math.isfinite(v) or not 0 <= v <= 1 for v in self.probe)):
            raise SetupError("Invalid Notes exit timing/probe")


def shortcut(reset):
    # Measured by the lead 2026-10-15: launch the app, not the shortcut via Spotlight.
    # Measured 2026-10-15: after a Spotlight launch, cmd+h lands on Spotlight and the daemon's launch then
    # toggles Spotlight closed. The reset's own launch handles the second Home and checks every screen.
    elements = reset.launch("Shortcuts", "Shortcuts")
    reset.require_labels("All Shortcuts", elements, ("All Shortcuts",))
    # Exact OCR text (not a substring or case-folded match) prevents selecting another tile.
    tiles = [el for el in elements if el.get("enabled", True)
             and el.get("text") == reset.notes_exit.shortcut_name]
    reset.action("tap_element", id=reset.target("Notes shortcut tile", tiles))
    # The measured tile brings Notes foreground; fresh chrome still gates verification.
    reset.foreground = "Notes"


def shortcut_spotlight(reset):
    # Measured 2026-10-15 21:37: once Spotlight indexes the shortcut, typing its name + Return runs it with the
    # keyboard only; after a model left a note in editing, the pointer stayed hidden even in Shortcuts.
    name = reset.notes_exit.shortcut_name
    reset.launch(name, name)
    reset.foreground = "Notes"


def switcher_kill(reset):
    reset.action("key", combo="cmd+h")
    if reset.notes_exit.switcher_combo:
        reset.action("key", combo=reset.notes_exit.switcher_combo)
    else:
        reset.gesture(reset.notes_exit.open_drag)
    if reset.notes_exit.switcher_pause_s:
        time.sleep(reset.notes_exit.switcher_pause_s)
    elements = reset.observe()
    # Never swipe an unidentified card or a Home icon. Placeholder layout gate.
    cards = [el for el in exact(elements, "Notes")
             if .20 * reset.width < bounds(el)[0] < .75 * reset.width
             and .12 * reset.height < bounds(el)[1] < .75 * reset.height]
    reset.check("unique Notes switcher card (estimated layout)", len(cards) == 1, cards)
    reset.gesture(reset.notes_exit.close_drag)
    reset.home()
    reset.action("launch", app="Notes")


def fka_find(reset):
    reset.action("key", combo="ctrl+tab")
    reset.action("key", combo="tab+f")
    reset.action("type", text="Done")
    reset.action("key", combo="return")


def escape_notes(reset):
    # Measured by task author 2026-10-13: hides keyboard only on reference phone.
    reset.action("key", combo="esc")


NOTES_EXIT_STRATEGIES = {"shortcut_spotlight": shortcut_spotlight, "shortcut": shortcut, "switcher_kill": switcher_kill, "fka_find": fka_find, "esc": escape_notes}


def calculator_keypad(elements):
    """Measured 2026-10-13: OCR reads digit keys reliably but not the orange operator keys
    ("=" came back as a CJK-like glyph, "x" and "/" not at all), so the keypad is recognised by its digits."""
    digits = {str(el.get("text", "")).strip() for el in elements if el.get("enabled", True)}
    return len(digits & set("0123456789")) >= 9 or {"=", "×"} <= digits


class SetupError(RuntimeError):
    """Fixed setup diagnostics; raw transport errors can contain credentials."""


# Measured 2026-10-13 on the reference phone: English UI with a Polish keyboard ("spacja", "szukaj"),
# and OCR reads the Spotlight/Notes search field with its magnifier as "Q Search".
LABEL_ALIASES = {
    "space": ("space", "spacja"),
    "return": ("return", "go", "szukaj", "idź", "enter"),
    "Search": ("Search", "Q Search", "QSearch", "Q lSearch", "QlSearch", "lSearch"),  # measured: cursor read as l
}


# Measured 2026-10-15: the search field's magnifier and text cursor come back as "Q", "l", "|" or "I".
SEARCH_FIELD = re.compile(r"(?:Q\s*)?[|lI]?\s*Search", re.IGNORECASE)


def exact(elements, text):
    wanted = {label.casefold() for label in LABEL_ALIASES.get(text, (text,))}
    return [el for el in elements if el.get("enabled", True)
            and (str(el.get("text", "")).strip().casefold() in wanted
                 or text == "Search" and SEARCH_FIELD.fullmatch(str(el.get("text", "")).strip()))]


def bounds(element):
    value = element.get("bounds_pt", element.get("bounds"))
    if isinstance(value, str):
        value = [float(v) for v in re.findall(r"-?\d+(?:\.\d+)?", value)]
    if isinstance(value, (list, tuple)) and len(value) == 4:
        box = [float(v) for v in value]
        if all(math.isfinite(v) for v in box) and box[0] < box[2] and box[1] < box[3]:
            return box
    raise SetupError("Expected element bounds in phone points")


class ResetRunner:
    def __init__(self, client, run_dir: Path, settle_s: float, records: list[dict],
                 notes_exit: NotesExitOptions | None = None):
        self.client, self.run_dir, self.records = client, run_dir, records
        self.settle_s = settle_s
        self.notes_exit = notes_exit or NotesExitOptions.from_env()
        self.foreground = None
        self.elements = []
        self.pointer_hidden_in_notes = False
        self.row = None
        self.observation = 0
        self.width, self.height = 375, 812  # Read: SPEC reference if state has no size.

    def action(self, name, **args):
        self.row["actions"].append({"action": name, "args": args, "ok": False})
        try:
            response = self.client._request("POST", f"/api/{name}", args)
        except DaemonError as exc:
            self.record_pointer_error(exc.error_class)
            raise SetupError("Daemon rejected setup action") from None
        if not isinstance(response, dict):
            raise SetupError("Setup action returned no acknowledgement")
        for value in (response, response.get("result")):
            if isinstance(value, dict) and (value.get("ok") is False or value.get("error")
                                           or value.get("status") == "off-path" or value.get("error_class")):
                self.record_pointer_error(value.get("error_class"))
                raise SetupError("Daemon rejected setup action")
        self.row["actions"][-1]["ok"] = True
        if name == "home" or name == "key" and args.get("combo") == "cmd+h":
            self.foreground = "Home"
        elif name == "launch":
            self.foreground = args.get("app")
        if self.settle_s:
            time.sleep(self.settle_s)

    def notes_foreground(self):
        state = self.client.get_state()
        if "current_app" in state:
            return state["current_app"] == "Notes"
        # Estimated fallback: real state has no app identity (daemon_control.py:118).
        # Require both launch history and fresh Notes navigation chrome.
        return self.foreground == "Notes" and bool(self.top(
            self.elements, ("Notes", "Folders", "All iCloud", "All on My iPhone", "Back")))

    def record_pointer_error(self, error_class):
        if error_class == "PointerHidden":
            self.row["actions"][-1]["error_class"] = "PointerHidden"
            if self.notes_foreground():
                self.pointer_hidden_in_notes = True

    def nav_done(self, elements):
        return [el for el in exact(elements, "Done")
                if bounds(el)[0] > .75 * self.width and bounds(el)[1] < .12 * self.height]

    def gesture(self, values):
        x1, y1, x2, y2, steps = values
        self.action("drag", x1=x1 * self.width, y1=y1 * self.height,
                    x2=x2 * self.width, y2=y2 * self.height, steps=steps)

    def pointer_probe(self):
        x, y = self.notes_exit.probe
        self.action("move", x=x * self.width, y=y * self.height)

    def leave_notes_editing(self):
        for name in self.notes_exit.strategies:
            log = {"strategy": name, "attempted": False, "verified": False,
                   "verification": {"notes_foreground": None, "no_nav_done": None,
                                    "pointer_visible": None, "notes_list": None},
                   "action_start": len(self.row["actions"])}
            self.row["strategies"].append(log)
            if name == "fka_find" and not self.notes_exit.fka_on:
                log.update(status="skipped", reason="Full Keyboard Access not enabled")
                continue
            log.update(attempted=True, status="failed")
            action_ok = True
            try:
                # A failed switcher attempt may have left Home or the switcher foreground.
                if not self.notes_foreground():
                    self.action("launch", app="Notes")
                NOTES_EXIT_STRATEGIES[name](self)
            except Exception:
                action_ok = False
                log["error"] = "Strategy action failed; inspect reset artifacts"
            try:
                elements = self.observe()
                foreground = self.notes_foreground()
                no_done = not self.nav_done(elements)
                pointer = False
                try:
                    self.pointer_probe()
                    pointer = True
                except Exception:
                    pass
                listed = bool(self.notes_view(elements))
                verified = action_ok and foreground and no_done and pointer and (name != "switcher_kill" or listed)
                log["verification"] = {"notes_foreground": foreground, "no_nav_done": no_done,
                                       "pointer_visible": pointer, "notes_list": listed,
                                       "observation": self.row["observations"][-1]}
                log.update(verified=verified, status="verified" if verified else "failed")
                if verified:
                    self.pointer_hidden_in_notes = False
                    return elements
            except Exception:
                log["error"] = "Strategy verification failed; inspect reset artifacts"
            finally:
                log["action_end"] = len(self.row["actions"])
        raise SetupError("Notes editing exit failed; inspect reset strategy log")

    def observe(self):
        self.observation += 1
        prefix = f"reset_{len(self.records):02d}_{self.observation:03d}"
        self.row["observations"].append(prefix)
        elements = self.client.get_elements()
        self.elements = elements
        (self.run_dir / f"{prefix}_elements.json").write_text(json.dumps(elements, indent=2) + "\n")
        self.client.get_frame(scale="pt2").save(self.run_dir / f"{prefix}_frame.jpg", format="JPEG")
        return elements

    def check(self, name, ok, matched):
        self.row["evidence"].append({"check": name, "verified": bool(ok),
                                     "matched_ocr": [str(el["text"]).strip() for el in matched],
                                     "observation": self.row["observations"][-1]})
        if not ok:
            raise SetupError(f"OCR could not verify {name}")

    def require_labels(self, name, elements, labels, extra=True):
        labels = tuple(dict.fromkeys(labels))
        matches = [el for text in labels for el in exact(elements, text)]
        self.check(name, all(exact(elements, text) for text in labels) and extra, matches)

    def target(self, name, matches):
        matches = [el for el in matches if el.get("clickable", True) and el.get("id")]
        self.check(f"unique {name} target", len(matches) == 1, matches)
        return matches[0]["id"]

    def home(self):
        self.action("home")
        elements = self.observe()
        if exact(elements, "Top Hit") or exact(elements, "Siri Suggestions") or exact(elements, "Search in App"):
            # Measured 2026-10-15: after an app was opened from Spotlight, Home returns to the Spotlight
            # results first; a second Home reaches the home screen. Bounded to one extra press.
            self.action("home")
            elements = self.observe()
        # Measured 2026-10-15: Calculator is not on the reference phone's first home page.
        self.require_labels("home", elements, ("Settings", "Notes"),
                            not exact(elements, "Cancel") and not exact(elements, "Done"))

    def launch(self, app, query):
        self.home()
        self.action("key", combo="cmd+space")
        elements = self.observe()
        # Measured 2026-10-15: OCR can miss space/return; single-letter keys in the lower third also prove a keyboard.
        letters = [el for el in elements if len(str(el.get("text", "")).strip()) == 1
                   and str(el.get("text", "")).strip().isalpha() and bounds(el)[1] > .65 * self.height]
        keyboard = exact(elements, "space") or exact(elements, "return") or len(letters) >= 5
        if keyboard and not exact(elements, "Search"):
            # Measured 2026-10-15: Spotlight restores the previous query ("Calc"); clear it, then re-check.
            self.action("key", combo="cmd+a")
            self.action("key", combo="backspace")
            elements = self.observe()
            letters = [el for el in elements if len(str(el.get("text", "")).strip()) == 1
                       and str(el.get("text", "")).strip().isalpha() and bounds(el)[1] > .65 * self.height]
            # Measured 2026-10-15: after clearing, iOS may hide the on-screen keyboard and dock the focused
            # (cursor) search field at the bottom; a hardware keyboard still types into it.
            keyboard = (exact(elements, "space") or exact(elements, "return") or len(letters) >= 5
                        or any(bounds(el)[1] > .85 * self.height for el in exact(elements, "Search")))
        # Tour approach: require an empty observed search before any typing. Siri Suggestions show a
        # "Notes" app icon, so only a Notes title in the nav band means Notes is still in front.
        self.require_labels("empty Spotlight", elements, ("Search",), keyboard
                            and not exact(elements, "Done")
                            and not self.top(elements, ("Notes",))
                            and not any(el.get("focused") and str(el.get("text", "")).strip()
                                        not in ("", "Search") for el in elements))
        self.action("type", text=query)
        elements = self.observe()
        self.require_labels(f"Spotlight {app} result", elements, (query, app),
                            bool(exact(elements, "space") or exact(elements, "return")
                                 or exact(elements, "Top Hit")) and not exact(elements, "Done"))
        self.action("key", combo="enter")
        elements = self.observe()
        self.foreground = app
        return elements

    def calculator_display(self, elements):
        # Read: use observed keypad bounds even when the clear key is an OCR-invisible glyph.
        keypad = [el for label in ("AC", "C", "=", "×", "÷", "+/-", "%")
                  for el in exact(elements, label)]
        keypad_top = min(bounds(el)[1] for el in keypad)
        return [el for el in elements
                if el.get("type") != "vc_item"
                and .08 * self.height <= bounds(el)[1]
                and bounds(el)[3] <= keypad_top
                and (str(el.get("text", "")).strip().casefold() == "error"
                     or re.fullmatch(r"[\d\s,.()%+*/xX×÷−+\-]+", str(el.get("text", "")).strip())
                     and re.search(r"\d", str(el.get("text", ""))))]

    def calculator_zero(self, elements):
        display = self.calculator_display(elements)
        return len(display) == 1 and str(display[0]["text"]).strip() == "0", display

    def calculator(self):
        elements = self.launch("Calculator", "Calc")
        self.require_labels("Calculator keypad", elements, (), calculator_keypad(elements))
        if not (exact(elements, "AC") or exact(elements, "C")):
            # Estimated: Escape may clear on iPhone; only fresh OCR can establish that it did.
            self.action("key", combo="esc")
            elements = self.observe()
            self.require_labels("Calculator keypad after Escape", elements, (), calculator_keypad(elements))
            zero, display = self.calculator_zero(elements)
            if zero:
                self.check("Calculator display is exactly 0", True, display)
            else:
                self.action("type", text="=")
                elements = self.observe()
                self.require_labels("Calculator keypad after evaluation", elements, (), calculator_keypad(elements))
            self.check("Calculator AC/C after bounded keyboard recovery",
                       bool(exact(elements, "AC") or exact(elements, "C")),
                       exact(elements, "AC") + exact(elements, "C"))
        # C clears the current entry; an additional AC clears pending arithmetic.
        for attempt in range(2):
            clears = exact(elements, "AC") + exact(elements, "C")
            self.require_labels("Calculator keypad", elements, (), calculator_keypad(elements) and bool(clears))
            identifier = self.target("Calculator AC/C", clears)
            was_ac = next(el for el in clears if el.get("id") == identifier)["text"].strip().casefold() == "ac"
            self.action("tap_element", id=identifier)
            # Measured 2026-10-15: the pointer ring left on AC makes OCR read it as "0"; park it on the
            # empty area above the display before observing.
            self.action("move", x=.5 * self.width, y=.18 * self.height)
            elements = self.observe()
            clears = exact(elements, "AC") + exact(elements, "C")
            self.require_labels("Calculator keypad after clear", elements, (), calculator_keypad(elements) and bool(clears))
            zero, display = self.calculator_zero(elements)
            self.check("Calculator display is exactly 0", zero, display)
            self.check("Calculator AC visible", bool(exact(elements, "AC")), exact(elements, "AC"))
            if was_ac:
                return
        # The second tap must have been AC, never an unbounded clear loop.
        raise SetupError("Calculator all-clear was not verified")

    def top(self, elements, labels, left=False):
        return [el for label in labels for el in exact(elements, label)
                if bounds(el)[1] < .15 * self.height
                and (not left or bounds(el)[0] < .25 * self.width)]

    def notes_view(self, elements):
        # Search/list chrome must be near the top, not words in an old note body.
        blocked = (self.nav_done(elements) or exact(elements, "Cancel")
                   or exact(elements, "space") or exact(elements, "return")
                   or any(el.get("focused") for el in elements))
        titles = self.top(elements, ("Notes", "Folders"))
        search = [el for el in exact(elements, "Search") if bounds(el)[1] < .30 * self.height]
        folder_rows = [el for label in ("Notes", "All iCloud", "On My iPhone")
                       for el in exact(elements, label) if bounds(el)[1] >= .15 * self.height]
        folder_title = self.top(elements, ("Folders",))
        if not blocked and titles and (search or folder_title and folder_rows):
            return titles + (search or folder_rows)
        # Measured 2026-10-15: a scrolled list hides Search and OCR may garble the title ("Ntes"); the
        # "N Notes" count footer plus the top-left "Folders" back button still identify the notes list.
        footer = [el for el in elements if re.fullmatch(r"\d+ Notes?", str(el.get("text", "")).strip())
                  and bounds(el)[1] > .85 * self.height]
        back = self.top(elements, ("Folders",), left=True)
        if not blocked and footer and back:
            return back + footer
        return []

    def notes(self):
        self.row["notes_exit_options"] = asdict(self.notes_exit)
        try:
            elements = self.launch("Notes", "Notes")
        except SetupError:
            if not self.pointer_hidden_in_notes:
                raise
            elements = self.observe()
        if self.notes_foreground():
            if not self.nav_done(elements):
                try:
                    self.pointer_probe()
                except SetupError:
                    if not self.pointer_hidden_in_notes:
                        raise
            if self.nav_done(elements) or self.pointer_hidden_in_notes:
                self.row["editing_detected"] = "PointerHidden" if self.pointer_hidden_in_notes else "nav_done"
                elements = self.leave_notes_editing()
        else:
            raise SetupError("Could not establish Notes foreground")
        escaped = False
        for attempt in range(5):  # At most four recovery actions, with OCR after each.
            matched = self.notes_view(elements)
            if matched:
                self.check("Notes list or folders", True, matched)
                return
            if attempt == 4:
                break
            titles = self.top(elements, ("Notes", "Folders", "All iCloud", "All on My iPhone", "Back"))
            searching = exact(elements, "Cancel") and exact(elements, "Search")
            editing = titles and not self.notes_view(elements)
            self.check("Notes recovery screen", bool(titles and (searching or editing)), titles)
            try:
                if not escaped and (searching or exact(elements, "space") or exact(elements, "return")
                                    or any(el.get("focused") for el in elements)):
                    self.action("key", combo="esc")
                    escaped = True
                else:
                    matches = exact(elements, "Cancel") if searching else self.top(
                        elements, ("Notes", "Folders", "All iCloud", "All on My iPhone", "Back"), left=True)
                    self.action("tap_element", id=self.target("Notes back/cancel", matches))
            except SetupError:
                if not self.pointer_hidden_in_notes or self.row["strategies"]:
                    raise
                self.row["editing_detected"] = "PointerHidden"
                elements = self.leave_notes_editing()
                continue
            elements = self.observe()
        self.check("Notes list or folders", False, [])

    def run(self, names):
        for name in names:
            self.row = {"recipe": name, "status": "running", "verified": False,
                        "actions": [], "observations": [], "evidence": [], "strategies": []}
            self.records.append(self.row)
            try:
                if name not in RESET_RECIPES:
                    raise SetupError("Unknown reset recipe")
                self.notes_exit.validate()
                self.pointer_hidden_in_notes = False
                self.width, self.height = self.client.screen_pt()
                if name == "calculator_cleared":
                    self.calculator()
                else:
                    self.notes()
                self.home()
                self.row.update(status="verified", verified=True)
            except Exception as exc:
                error = str(exc) if isinstance(exc, SetupError) else "Setup capture/action failed; inspect reset artifacts"
                self.row.update(status="failed", error=error)
                raise SetupError(error) from None
