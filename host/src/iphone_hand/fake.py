"""Hardware-free API double, protocol emulator, and accelerated visual rig.

The acceleration law is estimated/synthetic, never a measurement of iOS.
"""

from collections.abc import Callable
import math
import random
import time

from PIL import Image, ImageDraw

from .geometry import REFERENCE_PT, Geometry, parse_screen_pt
from .motion import validate_target
from .screen import find_phone_rect, phone_crop


# Presets for acceleration curves: (a, b) in gain = a + b * ln(1 + speed)
DEFAULT_SYNTHETIC_CURVE = (0.43, 0.22)
IPHONE_XS_MEASURED_CURVE = (0.196, 0.245)


def _int(value: str) -> int:
    return int(value, 16) if value.lower().startswith("0x") else int(value, 10)


class FakeSerialDevice:
    def __init__(self, *, position=None, sensitivity=(1.0, 1.0),
                 max_read: int = 256, connected: bool = True,
                 accel_curve: Callable[[float], float] | tuple[float, float] | None = None,
                 noise_std: float = 0.0, rng_seed: int | None = None,
                 chip: str = "esp32c5", screen_pt=REFERENCE_PT):
        if chip not in {"esp32c3", "esp32c5"}:
            raise ValueError("unsupported fake chip")
        self.chip = chip
        # Simulated screen size in points; the pointer is clamped to it like iOS clamps it.
        self.screen_pt = parse_screen_pt(screen_pt)
        self.position = list(position if position is not None else
                             (self.screen_pt[0] / 2, self.screen_pt[1] / 2))
        self.sensitivity = sensitivity
        self.max_read = max_read
        self.connected = self.ready = connected
        self.step, self.interval = 8, 15
        self.accel_curve = accel_curve
        self.noise_std = float(noise_std)
        self._rng = random.Random(rng_seed)
        self.pairing = not connected
        self.out_drops = 0
        self.boot_time = time.monotonic()
        self.buttons = 0
        self.keyboard = (0,)
        self.commands: list[tuple[int, str, tuple[str, ...]]] = []
        self.reports: list[tuple] = []
        self.drop_commands: set[str] = set()
        self.closed = False
        self._input = bytearray()
        self._output = bytearray(b"I fake boot log\n@0 EVT boot fw=fake\n")

    def queue(self, data: bytes) -> None:
        self._output.extend(data)

    def read(self, size: int = 1) -> bytes:
        if self.closed:
            raise RuntimeError("fake serial is closed")
        n = min(size, self.max_read, len(self._output))
        data = bytes(self._output[:n])
        del self._output[:n]
        return data

    def write(self, data: bytes) -> int:
        if self.closed:
            raise RuntimeError("fake serial is closed")
        self._input.extend(data)
        while b"\n" in self._input:
            line, _, rest = self._input.partition(b"\n")
            self._input = bytearray(rest)
            self._handle(bytes(line))
        return len(data)

    def close(self) -> None:
        self.closed = True

    def _handle(self, line: bytes) -> None:
        try:
            words = line.decode("utf-8").split()
            rid = int(words[0])
            if not 1 <= rid <= 999999 or len(words) < 2 or len(line) + 1 > 256:
                raise ValueError
        except (ValueError, IndexError, UnicodeError):
            self.queue(b"@0 ERR PARSE invalid line\n")
            return
        command, args = words[1], tuple(words[2:])
        self.commands.append((rid, command, args))
        try:
            reply = self._execute(command, args)
        except (ValueError, IndexError):
            reply = "ERR ARG invalid arguments"
        if command not in self.drop_commands:
            self.queue(f"@{rid} {reply}\n".encode())

    @staticmethod
    def _numbers(args, count, bounds):
        if len(args) != count:
            raise ValueError
        numbers = tuple(_int(a) for a in args)
        if any(not low <= n <= high for n, (low, high) in zip(numbers, bounds)):
            raise ValueError
        return numbers

    def _execute(self, command, args):
        if command == "PING" and not args:
            return f"OK fw=fake chip={self.chip}"
        if command == "STATUS" and not args:
            return (f"OK conn={int(self.connected)} ready={int(self.ready)} bonded=0 peer=- "
                    f"wifi=0 ip=- pairing={int(self.pairing)} out_drops={self.out_drops} "
                    f"uptime_ms={int((time.monotonic() - self.boot_time) * 1000)} reset=poweron reset_code=1")
        if command in {"STOP", "PAIR"}:
            if args:
                raise ValueError
            if command == "PAIR":
                self.connected = self.ready = False
                self.pairing = True
            else:
                self.buttons, self.keyboard = 0, (0, 0)
                if self.connected:
                    self.reports.extend([("KEY", 0, 0), ("BTN", 0), ("CONSUMER", 0)])
            return "OK"
        if command == "CFG":
            if not 1 <= len(args) <= 2:
                raise ValueError
            settings = dict(a.split("=", 1) for a in args)
            if set(settings) - {"step", "interval"}:
                raise ValueError
            step, interval = int(settings.get("step", self.step)), int(settings.get("interval", self.interval))
            if not (1 <= step <= 127 and 5 <= interval <= 100):
                raise ValueError
            self.step, self.interval = step, interval
            return f"OK step={step} interval={interval}"
        if command == "UNPAIR" and not args:
            self.connected = self.ready = False
            self.pairing = True
            self.queue(b"@0 EVT disc reason=0\n")
            return "OK"
        if command == "NAME":
            if not args:
                raise ValueError
            return "OK"
        hid = {"MOVE", "BTN", "CLICK", "SCROLL", "KEY", "KREPORT", "CONSUMER"}
        if command not in hid:
            return "ERR UNKNOWN unsupported command"
        if not self.connected or not self.ready:
            return "ERR NOCONN no simulated BLE host"
        if command == "MOVE":
            dx, dy = self._numbers(args, 2, [(-2000, 2000)] * 2)
            if max(0, math.ceil(max(abs(dx), abs(dy)) / self.step) - 1) * self.interval >= 10000:
                return self._timeout()
            self._move(dx, dy)
        elif command == "BTN":
            self.buttons, = self._numbers(args, 1, [(0, 7)])
            self.reports.append(("BTN", self.buttons))
        elif command == "CLICK":
            if len(args) > 2:
                raise ValueError
            mask, hold = self._numbers(args + ("1", "60")[len(args):], 2, [(1, 7), (0, 65535)])
            if hold >= 10000:
                return self._timeout()
            self.reports.extend([("BTN", mask), ("WAIT", hold), ("BTN", 0)])
            self.buttons = 0
        elif command == "SCROLL":
            v, h = self._numbers(args + (("0",) if len(args) == 1 else ()), 2, [(-2000, 2000)] * 2)
            while v or h:
                sv, sh = max(-127, min(127, v)), max(-127, min(127, h))
                self.reports.append(("SCROLL", sv, sh))
                v, h = v - sv, h - sh
        elif command == "KEY":
            mods, usage, hold = self._numbers(args + (("30",) if len(args) == 2 else ()),
                                               3, [(0, 255), (0, 255), (0, 65535)])
            if hold >= 10000:
                return self._timeout()
            self.reports.extend([("KEY", mods, usage), ("WAIT", hold), ("KEY", 0, 0)])
            self.keyboard = (0, 0)
        elif command == "KREPORT":
            if not 2 <= len(args) <= 7:
                raise ValueError
            self.keyboard = self._numbers(args, len(args), [(0, 255)] * len(args))
            self.reports.append(("KREPORT", *self.keyboard))
        elif command == "CONSUMER":
            usage, hold = self._numbers(args + (("30",) if len(args) == 1 else ()), 2,
                                         [(0, 65535), (0, 65535)])
            if hold >= 10000:
                return self._timeout()
            self.reports.extend([("CONSUMER", usage), ("WAIT", hold), ("CONSUMER", 0)])
        return "OK"

    def _timeout(self):
        # Synthetic time-budget outcome; native executor tests cover real logic.
        self.buttons, self.keyboard = 0, (0, 0)
        self.reports.extend([("KEY", 0, 0), ("BTN", 0), ("CONSUMER", 0)])
        return "ERR TIMEOUT"

    def _gain(self, speed: float) -> float:
        if self.accel_curve is None:
            return 0.43 + 0.22 * math.log1p(speed)
        if callable(self.accel_curve):
            return float(self.accel_curve(speed))
        a, b = self.accel_curve
        return a + b * math.log1p(speed)

    def _move(self, dx: int, dy: int) -> None:
        while dx or dy:
            sx, sy = max(-self.step, min(self.step, dx)), max(-self.step, min(self.step, dy))
            speed = math.hypot(sx, sy) * 15 / self.interval
            gain = self._gain(speed)
            for axis, (delta, bound) in enumerate(zip((sx, sy), self.screen_pt)):
                noise = self._rng.gauss(0, self.noise_std) if self.noise_std > 0 else 0.0
                self.position[axis] = max(0, min(bound, self.position[axis] +
                                                delta * gain * self.sensitivity[axis] + noise))
            self.reports.append(("MOVE", sx, sy, self.buttons))
            dx, dy = dx - sx, dy - sy


class SimulatedScreen:
    """Render a pointer driven by FakeSerialDevice onto a static phone image.

    `px_per_pt` sets the frame scale: 2 gives pt2 frames (750x1624 for the reference), 3 the
    native @3x pixels of a real phone (1170x2532 for 390x844). Frames are unscaled renders,
    so the geometry can be detected from exact native pixels.
    """
    frames_native = True

    def __init__(self, device: FakeSerialDevice, image: Image.Image | None = None, *,
                 px_per_pt: float = 2.0):
        self.device = device
        self.px_per_pt = float(px_per_pt)
        size = tuple(round(p * self.px_per_pt) for p in device.screen_pt)
        background = phone_crop(image) if image is not None else Image.new("RGB", size, "white")
        self.background = background if background.size == size else background.resize(size)
        self.last_grab_ms = None
        self.hidden = False

    def grab(self) -> Image.Image:
        start = time.perf_counter()
        image = self.background.copy()
        if not self.hidden:
            x, y = (p * self.px_per_pt for p in self.device.position)
            # Ring: 31 pt across (15.5 pt radius) with a 9.5 pt grey centre, as on the Portal.
            outer, inner = 15.5 * self.px_per_pt, 9.5 * self.px_per_pt
            draw = ImageDraw.Draw(image)
            draw.ellipse((x - outer, y - outer, x + outer, y + outer), fill=(210, 0, 0))
            draw.ellipse((x - inner, y - inner, x + inner, y + inner), fill=(155, 155, 155))
        self.last_grab_ms = (time.perf_counter() - start) * 1000
        return image

    def status(self) -> dict:
        return {"device": None, "mode": "simulated", "last_grab_ms": self.last_grab_ms,
                "grab_ms": {"png": None, "raw": None}}


class FakePhone:
    """Static fixture image and call recorder, with the exact public Phone API."""

    def __init__(self, image: Image.Image | None = None, *, position=None, screen_pt=None):
        # screen_pt: the simulated point size; default from the image's shape, else 375x812.
        rect = find_phone_rect(image, screen_pt=screen_pt) if image is not None else None
        pt = rect.pt if rect else (parse_screen_pt(screen_pt) or REFERENCE_PT)
        self.screen = Geometry(*pt, 2.0, "config" if screen_pt else rect.source if rect else "default",
                               rect.candidates if rect else ())
        self.image = phone_crop(image, rect) if image is not None else Image.new("RGB", self.screen.public_size, "white")
        self.position = tuple(position if position is not None else (pt[0] / 2, pt[1] / 2))
        self.calls: list[tuple] = []

    def bounds(self) -> tuple[int, int]:
        return self.screen.pt

    def geometry(self) -> Geometry:
        return self.screen

    def screenshot(self) -> Image.Image:
        self.calls.append(("screenshot",))
        return self.image.copy()

    def screenshot_with_pointer(self) -> tuple[Image.Image, tuple[float, float, float] | None]:
        return self.screenshot(), self.find_pointer()

    def find_pointer(self) -> tuple[float, float, float] | None:
        self.calls.append(("find_pointer",))
        return (*self.position, 1.0)

    def move_to(self, x: float, y: float) -> dict:
        validate_target(x, y, self.screen.pt)
        self.calls.append(("move_to", x, y))
        self.position = (x, y)
        return {"x": x, "y": y, "error_pt": 0.0, "iterations": 1}

    def tap(self, x: float, y: float, button: str = "left") -> dict:
        self.calls.append(("tap", x, y, button))
        return self.move_to(x, y)

    def long_press(self, x: float, y: float, ms: int = 800) -> dict:
        self.calls.append(("long_press", x, y, ms))
        return self.move_to(x, y)

    def drag(self, x1: float, y1: float, x2: float, y2: float, steps: int = 10) -> dict:
        self.calls.append(("drag", x1, y1, x2, y2, steps))
        return self.move_to(x2, y2)

    def scroll(self, dy: int, dx: int = 0, x: float | None = None, y: float | None = None) -> None:
        self.calls.append(("scroll", dy, dx, x, y))

    def type_text(self, text: str, *, window=8, hold_ms=None) -> dict:
        from .keys import text_keys
        from .phone import integer
        if not isinstance(text, str):
            raise ValueError("text must be a string")
        text_keys(text)
        integer(window, "window", 1, 12)
        if hold_ms is not None:
            integer(hold_ms, "hold_ms", 0, 65535)
        self.calls.append(("type_text", text))
        return {"typed": len(text)}

    def key(self, combo: str) -> None:
        self.calls.append(("key", combo))

    def home(self) -> None:
        self.calls.append(("home",))

    def status(self) -> dict:
        self.calls.append(("status",))
        return {"fake": True, "last_grab_ms": None, "screen": self.screen.to_dict()}

    def calibrate(self) -> dict:
        self.calls.append(("calibrate",))
        return {"counts_per_pt": [1.0, 1.0], "step": 8, "interval": 15}

    def close(self) -> None:
        self.calls.append(("close",))
