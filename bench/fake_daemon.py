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


class FakeDaemon:
    def __init__(self, token: str | None = None, required_token: str | None = None):
        self.token = token
        self.required_token = required_token
        self.lock = threading.RLock()
        self.current_app: str = "Home"
        self.current_screen: str = "home"
        self.dark_mode: bool = False
        self.low_power_mode: bool = False
        self.timer_running: bool = False
        self.calc_display: str = "0"
        self.calc_buffer: str = ""
        self.notes_text: str = ""
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
            }

    def get_elements_list(self) -> list[dict[str, Any]]:
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

            elif screen == "notes":
                els = [
                    _make_element("t_back", "Notes", bounds=(10, 50, 80, 80)),
                    _make_element("btn_done", "Done", bounds=(310, 50, 365, 80)),
                ]
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

    def get_frame_image(self, scale: str | None = None) -> Image.Image:
        with self.lock:
            # 750 x 1624 or 1126 x 2434
            size = (750, 1624) if scale == "pt2" else (1126, 2434)
            # Background color reflects dark mode:
            bg_color = (25, 25, 28) if self.dark_mode else (245, 245, 247)
            text_color = (240, 240, 240) if self.dark_mode else (20, 20, 20)

            img = Image.new("RGB", size, bg_color)
            draw = ImageDraw.Draw(img)

            # Draw top notch / status bar area
            scale_fac = size[0] / 375.0
            draw.text((int(20 * scale_fac), int(20 * scale_fac)), f"App: {self.current_app}", fill=text_color)
            draw.text((int(20 * scale_fac), int(50 * scale_fac)), f"Screen: {self.current_screen}", fill=text_color)

            # Draw representation of elements
            for el in self.get_elements_list():
                b = el["bounds"]
                x1, y1 = int(b[0] * scale_fac), int(b[1] * scale_fac)
                x2, y2 = int(b[2] * scale_fac), int(b[3] * scale_fac)
                border_color = (80, 80, 80) if self.dark_mode else (200, 200, 200)
                draw.rectangle((x1, y1, x2, y2), outline=border_color)
                draw.text((x1 + 4, y1 + 4), el["text"][:30], fill=text_color)

            return img

    def handle_action(self, action: str, params: dict[str, Any]) -> dict[str, Any]:
        with self.lock:
            entry = {"action": action, "params": params}

            if action == "home":
                self.current_app = "Home"
                self.current_screen = "home"

            elif action == "launch":
                app = params.get("app", "")
                self.current_app = app
                if app.lower() == "settings":
                    self.current_screen = "settings_root"
                elif app.lower() == "clock":
                    self.current_screen = "clock_timer"
                elif app.lower() == "calculator":
                    self.current_screen = "calculator"
                elif app.lower() == "notes":
                    self.current_screen = "notes"
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
                if el_id in ("t_dark", "btn_dark"):
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
                    self.calc_display = "0"
                    self.calc_buffer = ""

            elif action == "type":
                text = params.get("text", "")
                if self.current_screen == "calculator":
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

            elif action == "key":
                combo = params.get("combo", "")
                if combo == "cmd+space":
                    # Spotlight opened
                    pass
                elif combo in ("enter", "return"):
                    if self.current_screen == "safari":
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

    def __init__(self, port: int = 0, required_token: str | None = None):
        self.daemon = FakeDaemon(required_token=required_token)

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
