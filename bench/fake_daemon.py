"""Standalone fake daemon implementing SPEC sections 6 and 8 for offline testing and dry runs.

Provides both an in-process direct dispatch interface and a standard HTTP server
using Python's built-in http.server (no external network or server packages needed).
"""

from __future__ import annotations
import io
import json
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from typing import Any
import urllib.parse
from PIL import Image, ImageDraw


def _make_element(
    el_id: str,
    text: str,
    el_type: str = "text",
    bounds: tuple[int, int, int, int] = (10, 10, 100, 40),
    **attrs: Any,
) -> dict[str, Any]:
    x1, y1, x2, y2 = bounds
    center = [(x1 + x2) // 2, (y1 + y2) // 2]
    size = [x2 - x1, y2 - y1]
    data = {
        "id": el_id,
        "text": text,
        "type": el_type,
        "bounds": [x1, y1, x2, y2],
        "center": center,
        "size": size,
        "clickable": attrs.get("clickable", True),
        "editable": attrs.get("editable", False),
        "enabled": attrs.get("enabled", True),
        "checked": attrs.get("checked", False),
        "focused": attrs.get("focused", False),
        "selected": attrs.get("selected", False),
        "scrollable": attrs.get("scrollable", False),
        "longClickable": attrs.get("longClickable", False),
    }
    data.update(attrs)
    return data


REFERENCE_PT = (375, 812)


class FakeDaemon:
    """Scripted iOS screens. Layouts are written in 375x812 reference points and scaled
    proportionally to `screen_pt`, a synthetic stand-in for other phones (not a real layout).
    Native frames are 1126x2434 for the reference (measured XS AirPlay size), else @3x.
    """

    def __init__(self, token: str | None = None, required_token: str | None = None,
                 screen_pt: tuple[int, int] = REFERENCE_PT, *,
                 fka_on: bool = False, switcher_opens: bool = True,
                 switcher_closes: bool = True, kill_returns_list: bool = True,
                 fka_find_works: bool = True, esc_exits_editing: bool = False,
                 shortcut_tile: str | None = "noteslist"):
        self.shortcut_tile = shortcut_tile
        self.spotlight_indexes_shortcut = True
        self.notes_from_shortcuts = False
        # Estimated: configurable synthetic outcomes, not claims about real iOS.
        self.fka_on = fka_on
        self.switcher_opens, self.switcher_closes = switcher_opens, switcher_closes
        self.kill_returns_list = kill_returns_list
        self.fka_find_works, self.esc_exits_editing = fka_find_works, esc_exits_editing
        self.notes_editing = True
        self.notes_keyboard = True
        self.fka_typing_mode = True
        self.fka_find_active = False
        self.fka_query = ""
        self.notes_card = True
        self.token = token
        self.required_token = required_token
        self.screen_pt = (int(screen_pt[0]), int(screen_pt[1]))
        self.frame_px = (1126, 2434) if self.screen_pt == REFERENCE_PT else \
            (3 * self.screen_pt[0], 3 * self.screen_pt[1])
        self.lock = threading.RLock()
        self.current_app: str = "Home"
        self.current_screen: str = "home"
        self.dark_mode: bool = False
        self.low_power_mode: bool = False
        self.timer_running: bool = False
        self.calc_display: str = "0"
        self.calc_buffer: str = ""
        self.notes_text: str = ""
        self.notes_resume_screen: str = "notes"
        self.reminders_text: str = ""
        self.safari_query: str = ""
        self.spotlight_query: str = ""
        self.action_log: list[dict[str, Any]] = []

    def reset_state(self) -> None:
        with self.lock:
            self.current_app = "Home"
            self.current_screen = "home"
            self.dark_mode = False
            self.low_power_mode = False
            self.timer_running = False
            self.calc_display = "0"
            self.calc_buffer = ""
            self.notes_text = ""
            self.notes_resume_screen = "notes"
            self.notes_editing = self.notes_keyboard = self.fka_typing_mode = True
            self.fka_find_active = False
            self.fka_query = ""
            self.notes_card = True
            self.notes_from_shortcuts = False
            self.reminders_text = ""
            self.safari_query = ""
            self.spotlight_query = ""
            self.action_log.clear()

    def get_state_dict(self) -> dict[str, Any]:
        with self.lock:
            return {
                "mirroring": True,
                "fps": 50,
                "ble_ready": True,
                "current_app": self.current_app,
                "current_screen": self.current_screen,
                "dark_mode": self.dark_mode,
                "low_power_mode": self.low_power_mode,
                "paused": False,
                "action_count": len(self.action_log),
                "screen": {"pt": list(self.screen_pt), "px_per_pt": round(self.frame_px[0] / self.screen_pt[0], 4),
                           "orientation": "portrait", "source": "config", "ambiguous": False,
                           "candidates": []},
            }

    def get_elements_list(self) -> list[dict[str, Any]]:
        elements = self._reference_elements()
        if self.screen_pt == REFERENCE_PT:
            return elements
        sx, sy = self.screen_pt[0] / REFERENCE_PT[0], self.screen_pt[1] / REFERENCE_PT[1]
        scaled = []
        for el in elements:
            x1, y1, x2, y2 = el["bounds"]
            bounds = (round(x1 * sx), round(y1 * sy), round(x2 * sx), round(y2 * sy))
            extra = {k: v for k, v in el.items() if k not in ("id", "text", "type", "bounds", "center", "size")}
            scaled.append(_make_element(el["id"], el["text"], el["type"], bounds=bounds, **extra))
        return scaled

    def _reference_elements(self) -> list[dict[str, Any]]:
        with self.lock:
            screen = self.current_screen

            if screen == "home":
                return [
                    _make_element("t_dock", "Home Screen", bounds=(0, 0, 375, 40)),
                    _make_element("app_settings", "Settings", bounds=(30, 100, 90, 160)),
                    _make_element("app_clock", "Clock", bounds=(120, 100, 180, 160)),
                    _make_element("app_calc", "Calculator", bounds=(210, 100, 270, 160)),
                    _make_element("app_notes", "Notes", bounds=(300, 100, 360, 160)),
                    _make_element("app_remind", "Reminders", bounds=(30, 200, 90, 260)),
                    _make_element("app_safari", "Safari", bounds=(120, 200, 180, 260)),
                ]

            elif screen == "spotlight":
                els = [
                    _make_element("search", "Search", bounds=(20, 60, 355, 90)),
                    _make_element("cancel", "Cancel", bounds=(290, 100, 355, 130)),
                    _make_element("space", "space", bounds=(100, 700, 270, 730)),
                    _make_element("return", "return", bounds=(280, 700, 370, 730)),
                ]
                if self.spotlight_query:
                    app = self._spotlight_app()
                    els += [
                        _make_element("query", self.spotlight_query, bounds=(20, 140, 355, 170), focused=True),
                        _make_element("top_hit", app or "Search Web", bounds=(20, 200, 355, 240)),
                    ]
                return els

            elif screen == "settings_root":
                return [
                    _make_element("t_title", "Settings", bounds=(20, 60, 200, 100)),
                    _make_element("t_wifi", "Wi-Fi", bounds=(20, 120, 350, 160)),
                    _make_element("t_bt", "Bluetooth", bounds=(20, 160, 350, 200)),
                    _make_element("t_general", "General", bounds=(20, 220, 350, 260)),
                    _make_element("t_disp", "Display & Brightness", bounds=(20, 280, 350, 320)),
                    _make_element("t_batt", "Battery", bounds=(20, 340, 350, 380)),
                ]

            elif screen == "settings_display":
                return [
                    _make_element("t_back", "Settings", bounds=(10, 50, 80, 80)),
                    _make_element("t_title", "Display & Brightness", bounds=(20, 90, 300, 130)),
                    _make_element("t_app_sec", "Appearance", bounds=(20, 150, 150, 180)),
                    _make_element("t_light", "Light", bounds=(40, 200, 120, 250), selected=not self.dark_mode),
                    _make_element("t_dark", "Dark", bounds=(180, 200, 260, 250), selected=self.dark_mode),
                    _make_element("t_bright", "Brightness", bounds=(20, 300, 150, 330)),
                    _make_element("t_truetone", "True Tone", bounds=(20, 350, 200, 380)),
                ]

            elif screen == "settings_battery":
                return [
                    _make_element("t_back", "Settings", bounds=(10, 50, 80, 80)),
                    _make_element("t_title", "Battery", bounds=(20, 90, 200, 130)),
                    _make_element(
                        "t_lpm", "Low Power Mode", bounds=(20, 150, 250, 190), checked=self.low_power_mode
                    ),
                    _make_element(
                        "t_lpm_desc",
                        "Low Power Mode temporarily reduces background activity like downloads and mail fetch.",
                        bounds=(20, 195, 355, 240),
                    ),
                    _make_element("t_bh", "Battery Health & Charging", bounds=(20, 260, 350, 300)),
                    _make_element("t_last_ch", "Last Charged to 100%", bounds=(20, 320, 350, 360)),
                ]

            elif screen == "settings_battery_health":
                return [
                    _make_element("t_back", "Battery", bounds=(10, 50, 80, 80)),
                    _make_element("t_title", "Battery Health & Charging", bounds=(20, 90, 340, 130)),
                    _make_element("t_max_cap", "Maximum Capacity", bounds=(20, 160, 250, 200)),
                    _make_element("t_cap_val", "84%", bounds=(300, 160, 355, 200)),
                    _make_element("t_peak", "Peak Performance Capability", bounds=(20, 220, 350, 260)),
                ]

            elif screen == "clock_timer":
                time_str = "00:59" if self.timer_running else "00:01:00"
                start_label = "Pause" if self.timer_running else "Start"
                return [
                    _make_element("t_title", "Timer", bounds=(20, 60, 150, 100)),
                    _make_element("t_time_picker", time_str, bounds=(50, 160, 325, 260)),
                    _make_element("t_cancel", "Cancel", bounds=(40, 320, 120, 380)),
                    _make_element("t_start", start_label, bounds=(250, 320, 330, 380)),
                    _make_element("t_sound", "When Timer Ends", bounds=(20, 420, 350, 460)),
                    _make_element("tab_world", "World Clock", bounds=(10, 760, 90, 800)),
                    _make_element("tab_alarm", "Alarm", bounds=(100, 760, 180, 800)),
                    _make_element("tab_stop", "Stopwatch", bounds=(190, 760, 270, 800)),
                    _make_element("tab_timer", "Timer", bounds=(280, 760, 360, 800)),
                ]

            elif screen == "calculator":
                return [
                    _make_element("t_display", self.calc_display, bounds=(20, 100, 355, 180)),
                    _make_element("btn_ac", "AC" if self.calc_display == "0" else "C", bounds=(20, 220, 90, 280)),
                    _make_element("btn_plusminus", "+/-", bounds=(110, 220, 180, 280)),
                    _make_element("btn_pct", "%", bounds=(200, 220, 270, 280)),
                    _make_element("btn_div", "÷", bounds=(290, 220, 360, 280)),
                    _make_element("btn_mul", "×", bounds=(290, 300, 360, 360)),
                    _make_element("btn_sub", "-", bounds=(290, 380, 360, 440)),
                    _make_element("btn_add", "+", bounds=(290, 460, 360, 520)),
                    _make_element("btn_eq", "=", bounds=(290, 540, 360, 600)),
                    _make_element("btn_0", "0", bounds=(20, 540, 180, 600)),
                ]

            elif screen in ("notes_list", "notes_search", "notes_folders"):
                els = [
                    _make_element("notes_title", "Folders" if screen == "notes_folders" else "Notes",
                                  bounds=(20, 60, 200, 100)),
                    _make_element("search", "Search", bounds=(20, 140, 355, 180),
                                  focused=screen == "notes_search"),
                    _make_element("btn_new_note", "New Note", bounds=(300, 750, 365, 800)),
                ]
                if screen == "notes_search":
                    els.append(_make_element("cancel", "Cancel", bounds=(290, 100, 355, 130)))
                return els

            elif screen == "shortcuts":
                # Estimated layout around the lead's measured tile center (~62, 292 pt).
                return [_make_element("shortcuts_title", "All Shortcuts", bounds=(20, 100, 300, 145))] + (
                    [_make_element("notes_shortcut", self.shortcut_tile, bounds=(20, 270, 104, 314))]
                    if self.shortcut_tile is not None else [])

            elif screen == "app_switcher":
                return ([_make_element("notes_card", "Notes", bounds=(110, 180, 265, 220))]
                        if self.notes_card else [])

            elif screen == "notes":
                els = [
                    _make_element("t_back", "Back" if self.notes_from_shortcuts else "Notes", bounds=(10, 50, 80, 80)),
                ]
                if self.notes_from_shortcuts:
                    els.append(_make_element("shortcut_breadcrumb", "◀ Shortcuts", bounds=(5, 20, 95, 40)))
                if self.notes_editing:
                    els.append(_make_element("btn_done", "Done", bounds=(310, 50, 365, 80)))
                if self.fka_find_active:
                    els.append(_make_element("fka_find", "Find", bounds=(20, 400, 355, 450)))
                if self.notes_text:
                    els.append(
                        _make_element("t_note_body", self.notes_text, bounds=(20, 110, 355, 300))
                    )
                else:
                    els.append(
                        _make_element("btn_new_note", "New Note", bounds=(300, 750, 365, 800))
                    )
                return els

            elif screen == "reminders":
                els = [
                    _make_element("t_title", "Reminders", bounds=(20, 60, 180, 100)),
                    _make_element("btn_done", "Done", bounds=(310, 50, 365, 80)),
                    _make_element("btn_details", "Details", bounds=(20, 750, 100, 790)),
                ]
                if self.reminders_text:
                    els.append(
                        _make_element("t_rem_item", self.reminders_text, bounds=(20, 140, 355, 180))
                    )
                else:
                    els.append(
                        _make_element("btn_new_rem", "New Reminder", bounds=(20, 750, 160, 790))
                    )
                return els

            elif screen == "safari":
                els = [
                    _make_element(
                        "t_urlbar",
                        self.safari_query if self.safari_query else "Search or enter website name",
                        bounds=(20, 750, 355, 790),
                    ),
                    _make_element("tab_tabs", "Tabs", bounds=(310, 750, 365, 790)),
                ]
                if "apple status" in self.safari_query.lower():
                    els.extend([
                        _make_element("t_apple_hdr", "Apple - System Status", bounds=(20, 80, 355, 130)),
                        _make_element("t_all_ok", "All services are operating normally.", bounds=(20, 140, 355, 180)),
                        _make_element("t_apple_domain", "apple.com", bounds=(20, 200, 150, 230)),
                        _make_element("t_search_res", "Search Results", bounds=(20, 240, 180, 270)),
                    ])
                return els

            return [_make_element("t_screen", screen, bounds=(0, 0, 375, 812))]

    def _spotlight_app(self):
        apps = {"calc": "Calculator", "notes": "Notes", "shortcuts": "Shortcuts"}
        if self.shortcut_tile and self.spotlight_indexes_shortcut:
            apps[self.shortcut_tile.lower()] = self.shortcut_tile
        return apps.get(self.spotlight_query.lower())

    def get_frame_image(self, scale: str | None = None) -> Image.Image:
        with self.lock:
            # pt2 (2 px per pt, 750 x 1624 for the reference) or native (1126 x 2434)
            w, h = self.screen_pt
            size = (2 * w, 2 * h) if scale == "pt2" else self.frame_px
            # Background color reflects dark mode:
            bg_color = (25, 25, 28) if self.dark_mode else (245, 245, 247)
            text_color = (240, 240, 240) if self.dark_mode else (20, 20, 20)

            img = Image.new("RGB", size, bg_color)
            draw = ImageDraw.Draw(img)

            # Draw top notch / status bar area
            scale_fac = size[0] / w
            draw.text((int(20 * scale_fac), int(20 * scale_fac)), f"App: {self.current_app}", fill=text_color)
            draw.text((int(20 * scale_fac), int(50 * scale_fac)), f"Screen: {self.current_screen}", fill=text_color)

            # Draw representation of elements
            for el in self.get_elements_list():
                b = el.get("bounds_pt", el["bounds"])
                x1, y1 = int(b[0] * scale_fac), int(b[1] * scale_fac)
                x2, y2 = int(b[2] * scale_fac), int(b[3] * scale_fac)
                border_color = (80, 80, 80) if self.dark_mode else (200, 200, 200)
                draw.rectangle((x1, y1, x2, y2), outline=border_color)
                draw.text((x1 + 4, y1 + 4), el["text"][:30], fill=text_color)

            return img

    def handle_action(self, action: str, params: dict[str, Any]) -> dict[str, Any]:
        with self.lock:
            entry = {"action": action, "params": params}

            if (self.current_screen == "notes" and self.notes_editing
                    and action in ("move", "tap", "tap_element", "tap_text", "drag", "long_press", "scroll")):
                self.action_log.append(entry)
                return {"ok": False, "error_class": "PointerHidden", "error": "Pointer hidden"}

            if action == "home" or action == "key" and params.get("combo") == "cmd+h":
                if self.current_screen in ("notes", "notes_list", "notes_search", "notes_folders"):
                    self.notes_resume_screen = self.current_screen
                self.current_app = "Home"
                self.current_screen = "home"
                self.fka_find_active = False

            elif action == "drag":
                w, h = self.screen_pt
                if (self.current_screen == "home" and self.switcher_opens
                        and params.get("y1", 0) > .9 * h and params.get("y2", h) < .6 * h
                        and abs(params.get("x1", 0) - .5 * w) < .1 * w):
                    self.current_app, self.current_screen = "App Switcher", "app_switcher"
                elif (self.current_screen == "app_switcher" and self.switcher_closes
                      and self.notes_card and .3 * h < params.get("y1", 0) < .7 * h
                      and params.get("y2", h) < .15 * h):
                    self.notes_card = False
                    if self.kill_returns_list:
                        self.notes_resume_screen = "notes_list"
                        self.notes_editing = self.notes_keyboard = False

            elif action == "launch":
                app = params.get("app", "")
                self.current_app = app
                if app.lower() == "settings":
                    self.current_screen = "settings_root"
                elif app.lower() == "clock":
                    self.current_screen = "clock_timer"
                elif app.lower() == "calculator":
                    self.current_screen = "calculator"
                elif app.lower() == "shortcuts":
                    self.current_screen = "shortcuts"
                elif app.lower() == "notes":
                    self.current_screen = self.notes_resume_screen
                    self.notes_card = True
                elif app.lower() == "reminders":
                    self.current_screen = "reminders"
                elif app.lower() == "safari":
                    self.current_screen = "safari"
                elif "display" in app.lower():
                    self.current_app = "Settings"
                    self.current_screen = "settings_display"
                else:
                    self.current_screen = f"app_{app.lower()}"

            elif action == "tap_text":
                text = params.get("text", "").lower()
                if "dark" in text:
                    self.dark_mode = True
                elif "light" in text:
                    self.dark_mode = False
                elif "display & brightness" in text:
                    self.current_screen = "settings_display"
                elif "battery health" in text:
                    self.current_screen = "settings_battery_health"
                elif "battery" in text:
                    self.current_screen = "settings_battery"
                elif "low power mode" in text:
                    self.low_power_mode = not self.low_power_mode
                elif "start" in text:
                    self.timer_running = True
                elif "pause" in text:
                    self.timer_running = False
                elif "timer" in text:
                    self.current_screen = "clock_timer"
                elif "ac" in text or "c" in text:
                    self.calc_display = "0"
                    self.calc_buffer = ""

            elif action == "tap_element":
                el_id = params.get("id", "")
                if self.current_screen == "shortcuts" and el_id == "notes_shortcut" and self.shortcut_tile is not None:
                    self.current_app = "Notes"
                    self.current_screen = self.notes_resume_screen = "notes"
                    self.notes_editing = self.notes_keyboard = False
                    self.notes_from_shortcuts = True
                elif el_id in ("t_dark", "btn_dark"):
                    self.dark_mode = True
                elif el_id in ("t_light", "btn_light"):
                    self.dark_mode = False
                elif el_id == "t_disp":
                    self.current_screen = "settings_display"
                elif el_id == "t_batt":
                    self.current_screen = "settings_battery"
                elif el_id == "t_bh":
                    self.current_screen = "settings_battery_health"
                elif el_id == "t_lpm":
                    self.low_power_mode = not self.low_power_mode
                elif el_id == "t_start":
                    self.timer_running = not self.timer_running
                elif el_id in ("btn_ac", "btn_c"):
                    # Synthetic C clears only the entry; AC also clears pending input.
                    if self.calc_display == "0":
                        self.calc_buffer = ""
                    self.calc_display = "0"
                elif self.current_screen == "notes" and el_id == "t_back":
                    self.current_screen = self.notes_resume_screen = "notes_list"
                elif self.current_screen == "notes_search" and el_id == "cancel":
                    self.current_screen = self.notes_resume_screen = "notes_list"
                elif self.current_screen in ("notes_list", "notes_folders") and el_id == "btn_new_note":
                    self.current_screen = self.notes_resume_screen = "notes"
                    self.notes_editing = self.notes_keyboard = self.fka_typing_mode = True
                elif self.current_screen == "notes_list" and el_id == "search":
                    self.current_screen = self.notes_resume_screen = "notes_search"

            elif action == "type":
                text = params.get("text", "")
                if self.fka_find_active:
                    self.fka_query += text
                elif self.current_screen == "calculator":
                    self.calc_buffer += text
                    # Simple evaluation of 125 * 8
                    if "125" in self.calc_buffer and "8" in self.calc_buffer:
                        self.calc_display = "1,000"
                    elif text.isdigit():
                        self.calc_display = text
                elif self.current_screen == "notes":
                    self.notes_text = text
                elif self.current_screen == "reminders":
                    self.reminders_text = text
                elif self.current_screen == "safari":
                    self.safari_query = text
                elif self.current_screen == "home":
                    self.spotlight_query = text
                elif self.current_screen == "spotlight":
                    self.spotlight_query += text

            elif action == "key":
                combo = params.get("combo", "")
                if combo == "ctrl+tab" and self.fka_on and self.current_screen == "notes":
                    self.fka_typing_mode = False
                elif combo == "tab+f" and self.fka_on and not self.fka_typing_mode and self.current_screen == "notes":
                    self.fka_find_active = True
                    self.fka_query = ""
                elif combo in ("enter", "return") and self.fka_find_active:
                    if self.fka_query == "Done" and self.fka_find_works:
                        self.notes_editing = self.notes_keyboard = False
                    self.fka_find_active = False
                elif combo == "esc" and self.current_screen == "notes":
                    self.notes_keyboard = False
                    if self.esc_exits_editing:
                        self.notes_editing = False
                elif combo == "cmd+space":
                    self.current_screen = "spotlight"
                    self.spotlight_query = ""
                elif combo == "esc" and self.current_screen == "notes_search":
                    self.current_screen = self.notes_resume_screen = "notes_list"
                elif combo in ("enter", "return"):
                    app = self._spotlight_app()
                    if self.current_screen == "spotlight" and app and app == self.shortcut_tile:
                        # The indexed shortcut runs from Spotlight like the tile tap (measured 2026-10-15).
                        self.current_app = "Notes"
                        self.current_screen = self.notes_resume_screen = "notes"
                        self.notes_editing = self.notes_keyboard = False
                        self.notes_from_shortcuts = True
                        self.spotlight_query = ""
                    elif self.current_screen == "spotlight" and app:
                        self.current_app = app
                        self.current_screen = {"Calculator": "calculator", "Shortcuts": "shortcuts"}.get(
                            app, self.notes_resume_screen)
                        self.spotlight_query = ""
                    elif self.current_screen == "safari":
                        pass
                    elif "display" in self.spotlight_query.lower() or "display" in self.calc_buffer.lower() or "display" in self.notes_text.lower():
                        self.current_app = "Settings"
                        self.current_screen = "settings_display"
                        self.spotlight_query = ""
                    elif "125" in self.calc_buffer and "8" in self.calc_buffer:
                        self.calc_display = "1,000"

            elif action == "back":
                if self.current_screen == "settings_battery_health":
                    self.current_screen = "settings_battery"
                elif self.current_screen in ("settings_battery", "settings_display"):
                    self.current_screen = "settings_root"

            self.action_log.append(entry)
            return {
                "ok": True,
                "action": action,
                "current_app": self.current_app,
                "current_screen": self.current_screen,
            }

    def handle_in_process(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        url_parts = urllib.parse.urlparse(path)
        p = url_parts.path

        if method == "GET":
            if p == "/api/state":
                return self.get_state_dict()
            elif p == "/api/elements":
                return self.get_elements_list()
            elif p == "/api/frame.jpg":
                q = urllib.parse.parse_qs(url_parts.query)
                scale = q.get("scale", [None])[0]
                img = self.get_frame_image(scale=scale)
                buf = io.BytesIO()
                img.save(buf, format="JPEG", quality=90)
                return buf.getvalue()
            raise RuntimeError(f"Unknown GET path: {p}")

        elif method == "POST":
            action = p.replace("/api/", "")
            return self.handle_action(action, body or {})

        raise RuntimeError(f"Unsupported method: {method}")


class CaptureTourFakeDaemon(FakeDaemon):
    """Opt-in synthetic tour screens; legacy FakeDaemon behavior stays unchanged.

    This tests action ordering and failure gates, not iOS keyboard support or OCR.
    No sockets are opened. Version and pointer states are synthetic fixture data.
    """

    def __init__(self, calc_escape_clears=True, **kwargs):
        super().__init__(**kwargs)
        # Synthetic switches, not measured iOS Escape behavior.
        self.calc_escape_clears = calc_escape_clears
        self.calc_evaluated = False
        self.ios_version = "18.0"
        self.editing = False
        self.select_all = False
        self.about_scrolled = False
        self.apps_visible = False
        self.created_notes = 0
        self.previous_notes: list[str] = []

    def get_elements_list(self):
        # Read: real daemon emits both DroidClaw bounds strings and phone-point lists.
        elements = super().get_elements_list()
        for el in elements:
            x1, y1, x2, y2 = el["bounds"]
            el["bounds_pt"] = [x1, y1, x2, y2]
            el["bounds"] = f"[{x1:g},{y1:g}][{x2:g},{y2:g}]"
        return elements

    def get_state_dict(self):
        state = super().get_state_dict()
        hidden = self.editing or self.current_screen in {"spotlight", "notes_search"}
        state["pointer"] = None if hidden else {"x": 180, "y": 400, "confidence": 1.0}
        return state

    def _reference_elements(self):
        def item(identifier, text, y, **attrs):
            return _make_element(identifier, text, bounds=(20, y, 355, y + 30), **attrs)

        screen = self.current_screen
        if screen == "calculator":
            els = super()._reference_elements()
            clear = next(el for el in els if el["id"] == "btn_ac")
            if self.calc_display != "0" and self.calc_display != "Error" and not self.calc_evaluated:
                # The real backspace glyph has no OCR text; never offer an AC/C tap.
                clear.update(id="btn_backspace", text="")
            else:
                clear["text"] = "AC"
            return els
        if screen == "spotlight":
            els = [item("search_label", "Search", 60), item("cancel", "Cancel", 100),
                   item("space", "space", 650), item("return", "return", 690)]
            if self.spotlight_query:
                els.append(item("query", self.spotlight_query, 140, editable=True, focused=True))
                app = {"calc": "Calculator", "settings": "Settings", "notes": "Notes"}.get(self.spotlight_query.lower())
                els.append(item("top_hit", app or "Search Web", 200))
            return els
        if screen == "settings_root":
            els = super()._reference_elements()
            if self.apps_visible:
                els = [item("settings_title", "Settings", 60)]
            return els + [item("apps", "Apps", 600)] if self.apps_visible else els
        if screen == "settings_general":
            return [item("settings_back", "Settings", 50), item("general_title", "General", 90),
                    item("about", "About", 150), item("update", "Software Update", 200)]
        if screen == "settings_about":
            els = [item("general_back", "General", 50), item("about_title", "About", 90), item("name", "Name", 140)]
            if self.about_scrolled:
                els.extend([_make_element("version_label", "iOS Version", bounds=(20, 190, 180, 220)),
                            _make_element("version_value", self.ios_version, bounds=(240, 190, 355, 220))])
            return els
        if screen == "settings_apps":
            return [item("settings_back", "Settings", 50), item("apps_title", "Apps", 90),
                    item("apps_search", "Search", 130), item("app_calc", "Calculator", 190)]
        if screen in {"notes_list", "notes_search"}:
            els = [item("notes_title", "Notes", 70), item("search", "Search", 130,
                                                           editable=True, focused=screen == "notes_search"),
                   _make_element("compose", "New Note", bounds=(300, 750, 365, 800))]
            if screen == "notes_search":
                els.append(item("cancel", "Cancel", 170))
            return els
        if screen == "notes_editor":
            els = [item("notes_back", "Notes", 50), item("done", "Done", 90)]
            if self.notes_text:
                els.append(item("body", self.notes_text, 150, editable=True, focused=self.editing))
            if self.editing:
                els.extend([item("space", "space", 650), item("return", "return", 690)])
            return els
        return super()._reference_elements()

    def handle_action(self, action, params):
        with self.lock:
            screen = self.current_screen
            if action == "home":
                self.current_app, self.current_screen = "Home", "home"
                self.editing = False
            elif action == "key":
                combo = params["combo"]
                if combo == "cmd+space":
                    self.current_screen = "spotlight"
                    self.spotlight_query = ""
                    self.select_all = False
                elif combo == "cmd+a" and screen == "spotlight":
                    self.select_all = True
                elif combo in {"enter", "return"} and screen == "spotlight":
                    app = {"calc": "Calculator", "settings": "Settings", "notes": "Notes"}.get(self.spotlight_query.lower())
                    if not app:
                        raise ValueError("Synthetic Spotlight has no app match")
                    self.current_app = app
                    self.current_screen = {"Calculator": "calculator", "Settings": "settings_root", "Notes": "notes_list"}[app]
                    self.apps_visible = False
                elif combo == "esc":
                    if screen == "calculator":
                        if self.calc_escape_clears:
                            self.calc_buffer, self.calc_display = "", "0"
                            self.calc_evaluated = False
                    elif screen == "notes_search":
                        self.current_screen = "notes_list"
                    elif screen == "notes_editor":
                        self.editing = False
                    else:
                        raise ValueError("Unsupported synthetic Esc")
                else:
                    raise ValueError("Unsupported synthetic key")
            elif action == "type":
                text = params["text"]
                if screen == "spotlight":
                    self.spotlight_query = text if self.select_all else self.spotlight_query + text
                    self.select_all = False
                elif screen == "calculator":
                    import re
                    if text == "=":
                        match = re.fullmatch(r"(\d+)\*(\d+)", self.calc_buffer)
                        if match:
                            self.calc_display = f"{int(match[1]) * int(match[2]):,}"
                        elif self.calc_buffer.isdigit():
                            self.calc_display = f"{int(self.calc_buffer):,}"
                        else:
                            self.calc_display = "Error"
                        self.calc_evaluated = True
                    else:
                        self.calc_evaluated = False
                        self.calc_buffer += text
                        self.calc_display = self.calc_buffer
                elif screen == "notes_editor" and self.editing:
                    self.notes_text += text
                else:
                    raise ValueError("Synthetic text field is not focused")
            elif action == "tap_element":
                identifier = params["id"]
                if not any(el["id"] == identifier for el in self.get_elements_list()):
                    raise ValueError("Synthetic target is not visible")
                if screen == "calculator" and identifier == "btn_ac":
                    self.calc_buffer, self.calc_display = "", "0"
                    self.calc_evaluated = False
                elif screen == "settings_root" and identifier == "t_general":
                    self.current_screen = "settings_general"
                elif screen == "settings_root" and identifier == "apps":
                    self.current_screen = "settings_apps"
                elif screen == "settings_general" and identifier == "about":
                    self.current_screen = "settings_about"
                    self.about_scrolled = False
                elif screen == "notes_list" and identifier == "search":
                    self.current_screen = "notes_search"
                elif screen == "notes_list" and identifier == "compose":
                    if self.notes_text:
                        self.previous_notes.append(self.notes_text)
                    self.notes_text = ""
                    self.created_notes += 1
                    self.current_screen = "notes_editor"
                    self.editing = True
                else:
                    raise ValueError("Unsupported synthetic tap")
            elif action == "scroll":
                if screen == "settings_root":
                    self.apps_visible = params["dy"] < 0
                elif screen == "settings_about":
                    self.about_scrolled = True
                else:
                    raise ValueError("Unsupported synthetic scroll")
            elif action == "back" and screen == "settings_apps":
                self.current_screen = "settings_root"
            else:
                raise ValueError("Unsupported synthetic tour action")
            self.action_log.append({"action": action, "params": dict(params)})
            return {"ok": True, "state": self.get_state_dict()}


class _FakeDaemonHandler(BaseHTTPRequestHandler):
    daemon_instance: FakeDaemon

    def log_message(self, format: str, *args: Any) -> None:
        # Suppress noisy HTTP request logging in test stdout
        pass

    def _check_auth(self) -> bool:
        req_token = self.daemon_instance.required_token
        if not req_token:
            return True
        auth = self.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            token = auth[len("Bearer "):].strip()
            return token == req_token
        # Also check ?token=
        parsed = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(parsed.query)
        token = qs.get("token", [None])[0]
        return token == req_token

    def do_GET(self) -> None:
        if not self._check_auth():
            self.send_response(401)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"error":"unauthorized"}')
            return

        parsed = urllib.parse.urlparse(self.path)
        p = parsed.path
        if p == "/api/state":
            data = json.dumps(self.daemon_instance.get_state_dict()).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(data)
        elif p == "/api/elements":
            data = json.dumps(self.daemon_instance.get_elements_list()).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(data)
        elif p == "/api/frame.jpg":
            qs = urllib.parse.parse_qs(parsed.query)
            scale = qs.get("scale", [None])[0]
            img = self.daemon_instance.get_frame_image(scale=scale)
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=90)
            raw = buf.getvalue()
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(raw)
        else:
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.send_header("Connection", "close")
            self.end_headers()

    def do_POST(self) -> None:
        if not self._check_auth():
            self.send_response(401)
            self.send_header("Content-Type", "application/json")
            data = b'{"error":"unauthorized"}'
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(data)
            return

        parsed = urllib.parse.urlparse(self.path)
        action = parsed.path.replace("/api/", "")
        content_len = int(self.headers.get("Content-Length", 0))
        body = {}
        if content_len > 0:
            raw = self.rfile.read(content_len).decode("utf-8")
            if raw:
                try:
                    body = json.loads(raw)
                except Exception:
                    pass

        res = self.daemon_instance.handle_action(action, body)
        data = json.dumps(res).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(data)


class FakeDaemonServer:
    """Threaded HTTP server hosting FakeDaemon on localhost."""

    def __init__(self, port: int = 0, required_token: str | None = None,
                 screen_pt: tuple[int, int] = REFERENCE_PT):
        self.daemon = FakeDaemon(required_token=required_token, screen_pt=screen_pt)

        class HandlerWithDaemon(_FakeDaemonHandler):
            daemon_instance = self.daemon

        from http.server import ThreadingHTTPServer
        self.server = ThreadingHTTPServer(("127.0.0.1", port), HandlerWithDaemon)
        self.port = self.server.server_port
        self.url = f"http://127.0.0.1:{self.port}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def start(self) -> None:
        self.thread.start()

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()
