"""One synchronous Phone object shared by all front ends, per SPEC section 3."""

from functools import wraps
import threading
import time
from pathlib import Path

from PIL import Image

from . import geometry, keys, pointer
from .motion import Motion, MotionError, validate_target
from .screen import PhoneRect, Screen, find_phone_rect, phone_crop
from .serial_link import PipelineError, SerialLink

SCROLL_MAX = 2000  # wheel units per axis per SCROLL command (firmware IH_DELTA_MAX)


def synchronized(method):
    @wraps(method)
    def wrapped(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)
    return wrapped


def integer(value, name: str, low: int, high: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise ValueError(f"{name} must be an integer in {low}..{high}")


class TypeIncomplete(RuntimeError):
    """Confirmed character count and indices; no text in diagnostics.

    Lost ACKs can hide successful KEYs. Later successes can leave holes after
    an ERR. Inspect the phone before resuming in either case.
    """
    def __init__(self, error: PipelineError):
        self.typed = error.confirmed
        self.sent = error.sent
        self.confirmed_indices = error.confirmed_indices
        self.cause = error.cause
        self.code = error.code
        super().__init__(f"Typing incomplete: typed={self.typed}, sent={self.sent}")


class Phone:
    def __init__(self, link: SerialLink | None = None, screen: Screen | None = None, *,
                 settle: float = 0.350, calibration_path: Path | None = None,
                 persist_calibration: bool = True, home_action: str = "cmd+h", sleeper=time.sleep,
                 pointer_recover_key: str = "", screen_pt=None, device: str = "iphone",
                 pointer_detector: str = "assistivetouch"):
        self.link = link if link is not None else SerialLink()
        self.screen = screen if screen is not None else Screen()
        self._lock = threading.RLock()
        self._last_pointer = None
        self.home_action = home_action
        # Geometry inputs: override, family table, detector. The detected PhoneRect of the latest
        # frame is the single source of truth for the point size (exposed in /api/state).
        self.screen_pt = geometry.parse_screen_pt(screen_pt)
        geometry.sizes(device)
        self.device = device
        self.pointer_detector = pointer.get_detector(pointer_detector)
        self.rect: PhoneRect | None = None
        self.motion = Motion(self.link, self.find_pointer, settle=settle,
                             calibration_path=calibration_path, persist=persist_calibration,
                             sleeper=sleeper, pointer_recover_key=pointer_recover_key,
                             bounds=self.bounds, discover=self.discover_geometry)

    def phone_rect(self, frame: Image.Image) -> PhoneRect:
        """Screen rectangle and point size of one frame; also remembered as the current one."""
        rect = find_phone_rect(frame, screen_pt=self.screen_pt,
                               px_per_pt=getattr(self.screen, "pt_scale", 0) or 0,
                               native=getattr(self.screen, "frames_native", False),
                               family=self.device)
        self.rect = rect
        return rect

    def geometry(self) -> geometry.Geometry:
        return self.rect.geometry if self.rect else geometry.default_geometry(self.screen_pt)

    def bounds(self) -> tuple[int, int]:
        """Screen size in points of the latest frame (before any frame: override or reference)."""
        return self.geometry().pt

    @synchronized
    def discover_geometry(self) -> None:
        """Look at one frame when no frame decided the screen size yet (no device commands)."""
        if self.rect is None:
            self.find_pointer()

    def check_target(self, x: float, y: float) -> None:
        if not self.geometry().contains(x, y):
            self.discover_geometry()  # A larger phone's target, before its first frame.
        validate_target(x, y, self.bounds())

    @synchronized
    def screenshot(self) -> Image.Image:
        frame = self.screen.grab()
        rect = self.phone_rect(frame)
        self._last_pointer = pointer.find_pointer(frame, rect, self.pointer_detector)
        return phone_crop(frame, rect)

    @synchronized
    def screenshot_with_pointer(self) -> tuple[Image.Image, tuple[float, float, float] | None]:
        """Atomic capture metadata for the MCP wrapper; screenshot() is unchanged."""
        image = self.screenshot()
        return image, self._last_pointer

    @synchronized
    def find_pointer(self) -> tuple[float, float, float] | None:
        frame = self.screen.grab()
        self._last_pointer = pointer.find_pointer(frame, self.phone_rect(frame), self.pointer_detector)
        return self._last_pointer

    @synchronized
    def move_to(self, x: float, y: float) -> dict:
        return self.motion.move_to(x, y)

    def _require_position(self, x, y) -> dict:
        result = self.move_to(x, y)
        if result["error_pt"] > 3:
            raise MotionError(f"Pointer failed to converge (error {result['error_pt']:.2f} pt); action stopped")
        return result

    @synchronized
    def tap(self, x: float, y: float, button: str = "left") -> dict:
        masks = {"left": 1, "right": 2, "middle": 4}
        if button not in masks:
            raise ValueError("button must be left, right or middle")
        result = self._require_position(x, y)
        self.link.command("CLICK", masks[button])
        return result

    @synchronized
    def long_press(self, x: float, y: float, ms: int = 800) -> dict:
        integer(ms, "ms", 1, 60000)
        result = self._require_position(x, y)
        self.link.command("CLICK", 1, ms)
        return result

    @synchronized
    def drag(self, x1: float, y1: float, x2: float, y2: float, steps: int = 10) -> dict:
        self.check_target(x1, y1)
        self.check_target(x2, y2)
        integer(steps, "steps", 1, 1000)
        result = self._require_position(x1, y1)
        recovered_with = result.get("recovered_with")
        # BTN's ACK could time out after the button was pressed. Release even
        # when the press or a subsequent capture/move raises an exception.
        try:
            self.link.command("BTN", 1)
            for i in range(1, steps + 1):
                result = self._require_position(x1 + (x2 - x1) * i / steps,
                                                y1 + (y2 - y1) * i / steps)
                recovered_with = result.get("recovered_with", recovered_with)
        finally:
            self.link.command("BTN", 0)
        if recovered_with:
            result["recovered_with"] = recovered_with
        return result

    @synchronized
    def scroll(self, dy: int, dx: int = 0, x: float | None = None, y: float | None = None) -> dict | None:
        # Read (firmware protocol.h IH_DELTA_MAX): SCROLL accepts +/-2000 per axis. Reject
        # before positioning so an out-of-range scroll never leaves a half-done action.
        integer(dy, "dy", -SCROLL_MAX, SCROLL_MAX)
        integer(dx, "dx", -SCROLL_MAX, SCROLL_MAX)
        if (x is None) != (y is None):
            raise ValueError("scroll location needs both x and y")
        result = self._require_position(x, y) if x is not None else None
        self.link.command("SCROLL", dy, dx)
        if result and "recovered_with" in result:
            return result

    @synchronized
    def type_text(self, text: str, *, window=8, hold_ms=None) -> dict:
        if not isinstance(text, str):
            raise ValueError("text must be a string")
        mapped = keys.text_keys(text)  # Validate the whole string before submission.
        integer(window, "window", 1, 12)
        if hold_ms is not None:
            integer(hold_ms, "hold_ms", 0, 65535)
        commands = [("KEY", key.mods, key.usage, *(() if hold_ms is None else (hold_ms,)))
                    for key in mapped]
        try:
            replies = self.link.pipeline(commands, window=window)
        except PipelineError as error:
            raise TypeIncomplete(error) from error
        return {"typed": len(replies)}

    @synchronized
    def key(self, combo: str) -> None:
        try:
            key = keys.parse_combo(combo)
        except ValueError:
            # Two plain keys held together (Full Keyboard Access: tab+f Find, tab+b Back).
            # KEY carries one usage, so press the chord as raw KREPORT states.
            mods, usages = keys.parse_chord(combo)
            if len(usages) < 2:
                raise
            self._chord(mods, usages)
            return
        self.link.command("KEY", key.mods, key.usage)

    def _chord(self, mods: int, usages: list[int], hold: float = 0.06) -> None:
        try:
            for count in range(1, len(usages) + 1):
                self.link.command("KREPORT", mods, *usages[:count])
                self.motion.sleeper(hold)
        finally:
            self.link.command("KREPORT", 0, 0)

    @synchronized
    def home(self) -> None:
        if self.home_action == "button3":
            self.link.command("CLICK", 4)
        else:
            self.key(self.home_action)

    @synchronized
    def status(self) -> dict:
        result = self.link.command("STATUS").fields
        return {"serial": {"open": self.link.transport is not None, "port": self.link.port},
                "ble": {"conn": result.get("conn") == "1", "ready": result.get("ready") == "1",
                        "bonded": int(result.get("bonded", 0)), "peer": result.get("peer")},
                "adb": self.screen.status(), "last_grab_ms": self.screen.last_grab_ms,
                "screen": self.geometry().to_dict(),
                "events": list(self.link.events), "calibration": {
                    "profile": self.motion.profile,
                    "counts_per_pt": list(self.motion.counts_per_pt), "step": self.motion.step,
                    "interval": self.motion.interval, "cache_error": self.motion.cache_error}}

    @synchronized
    def calibrate(self) -> dict:
        return self.motion.calibrate()

    def close(self) -> None:
        with self._lock:
            self.link.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
