"""Fixed-pacing, bounded visual feedback and parametric pointer acceleration modeling.

The parametric gain model captures iOS AssistiveTouch mouse acceleration, where
report gain (pt per count) scales non-linearly with per-report speed
v = hypot(sx, sy) * (15 / interval). Axis coupling occurs through the 2D vector
magnitude, meaning orthogonal motion increases gain on both axes.
"""

from collections.abc import Callable
import json
import math
from pathlib import Path
import time

import numpy as np

from .geometry import REFERENCE_PT, size_label
from .serial_link import SerialLink
from . import keys


HOME_OFFSET = (120, 200)  # counts from the corner to a visible spot (~90, ~150 pt)
MAX_OBSERVATIONS = 1000  # newest unclamped moves kept for ParametricGainModel.fit


class MotionError(RuntimeError):
    pass


class PointerHidden(MotionError):
    message = "pointer hidden (text field editing?) - press esc to end editing"

    def __init__(self, recovered_with: str | None = None):
        super().__init__(self.message)
        self.recovered_with = recovered_with


def validate_recover_key(combo: str) -> str:
    """Validate eagerly and normalize the key name used in recovery metadata."""
    if not isinstance(combo, str):
        raise ValueError("pointer_recover_key must be a key combo or empty string")
    if combo == "":
        return ""
    try:
        keys.parse_combo(combo)
    except ValueError:
        raise ValueError("pointer_recover_key must be a valid key combo") from None
    return combo.strip().lower()


def validate_target(x: float, y: float, bounds: tuple[int, int] = REFERENCE_PT) -> None:
    w, h = bounds
    if not (math.isfinite(x) and math.isfinite(y) and 0 <= x <= w and 0 <= y <= h):
        raise ValueError(f"target must be finite phone points inside 0..{w} by 0..{h}")


def profile_key(bounds) -> str:
    """Calibration profile per screen size, orientation-independent ("375x812")."""
    return size_label(sorted(bounds))


REFERENCE_PROFILE = profile_key(REFERENCE_PT)


class ParametricGainModel:
    """Parametric gain model for iOS pointer acceleration.

    iOS AssistiveTouch mouse acceleration scales report gain as a function
    of report speed: v = hypot(sx, sy) * (15.0 / interval).
    The gain (pt per count) for each axis is modeled as:
        gain_x(v) = a_x + b_x * ln(1 + v)
        gain_y(v) = a_y + b_y * ln(1 + v)
    where a represents base low-speed sensitivity and b represents acceleration
    gain coupled through the 2D vector magnitude v.
    """

    def __init__(self, a_x: float = 0.43, b_x: float = 0.22,
                 a_y: float = 0.43, b_y: float = 0.22,
                 is_calibrated: bool = False):
        self.a_x = float(a_x)
        self.b_x = float(b_x)
        self.a_y = float(a_y)
        self.b_y = float(b_y)
        self.is_calibrated = is_calibrated

    def gain_x(self, speed: float) -> float:
        return max(0.01, self.a_x + self.b_x * math.log1p(max(0.0, speed)))

    def gain_y(self, speed: float) -> float:
        return max(0.01, self.a_y + self.b_y * math.log1p(max(0.0, speed)))

    def predict_displacement(self, dx: int, dy: int, step: int = 8, interval: int = 15) -> tuple[float, float]:
        """Simulate firmware report pacing and predict resulting displacement in points."""
        rem_x, rem_y = dx, dy
        px, py = 0.0, 0.0
        scale = 15.0 / interval
        while rem_x or rem_y:
            sx = max(-step, min(step, rem_x))
            sy = max(-step, min(step, rem_y))
            v = math.hypot(sx, sy) * scale
            px += sx * self.gain_x(v)
            py += sy * self.gain_y(v)
            rem_x -= sx
            rem_y -= sy
        return px, py

    def solve_counts(self, delta_x_pt: float, delta_y_pt: float, step: int = 8, interval: int = 15) -> tuple[int, int]:
        """Invert the acceleration model to find optimal integer (dx, dy) counts."""
        if abs(delta_x_pt) < 0.1 and abs(delta_y_pt) < 0.1:
            return 0, 0

        v_nom = step * (15.0 / interval)
        gx0 = self.gain_x(v_nom)
        gy0 = self.gain_y(v_nom)

        dx = round(delta_x_pt / gx0)
        dy = round(delta_y_pt / gy0)

        # Iterative residual correction
        for _ in range(4):
            px, py = self.predict_displacement(dx, dy, step, interval)
            rx = delta_x_pt - px
            ry = delta_y_pt - py
            if abs(rx) < 0.15 and abs(ry) < 0.15:
                break
            adj_x = round(rx / gx0)
            adj_y = round(ry / gy0)
            if adj_x == 0 and adj_y == 0:
                break
            dx += adj_x
            dy += adj_y

        # Local 1-count neighborhood search to minimize Euclidean error
        best_dx, best_dy = dx, dy
        best_err = math.dist(self.predict_displacement(dx, dy, step, interval), (delta_x_pt, delta_y_pt))
        for ox in (-1, 0, 1):
            for oy in (-1, 0, 1):
                cand_x, cand_y = dx + ox, dy + oy
                px, py = self.predict_displacement(cand_x, cand_y, step, interval)
                err = math.dist((px, py), (delta_x_pt, delta_y_pt))
                if err < best_err:
                    best_err = err
                    best_dx, best_dy = cand_x, cand_y

        return best_dx, best_dy

    def nominal_counts_per_pt(self, step: int = 8, interval: int = 15) -> list[float]:
        v_nom = step * (15.0 / interval)
        return [round(1.0 / self.gain_x(v_nom), 4), round(1.0 / self.gain_y(v_nom), 4)]

    def to_dict(self) -> dict:
        return {
            "type": "log_speed",
            "a_x": round(self.a_x, 6),
            "b_x": round(self.b_x, 6),
            "a_y": round(self.a_y, 6),
            "b_y": round(self.b_y, 6),
            "is_calibrated": self.is_calibrated,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "ParametricGainModel":
        return cls(
            a_x=data.get("a_x", 0.43),
            b_x=data.get("b_x", 0.22),
            a_y=data.get("a_y", 0.43),
            b_y=data.get("b_y", 0.22),
            is_calibrated=data.get("is_calibrated", True),
        )

    @classmethod
    def fit(cls, observations: list[tuple[int, int, float, float, int, int]]) -> tuple["ParametricGainModel", dict]:
        """Fit parametric model parameters from observations via OLS."""
        Ax, yx = [], []
        Ay, yy = [], []

        for dx, dy, dx_pt, dy_pt, step, interval in observations:
            rem_x, rem_y = dx, dy
            scale = 15.0 / interval
            sx_log_sum = 0.0
            sy_log_sum = 0.0
            while rem_x or rem_y:
                sx = max(-step, min(step, rem_x))
                sy = max(-step, min(step, rem_y))
                v = math.hypot(sx, sy) * scale
                log_v = math.log1p(v)
                sx_log_sum += sx * log_v
                sy_log_sum += sy * log_v
                rem_x -= sx
                rem_y -= sy

            if dx != 0 and abs(dx_pt) > 0.5:
                Ax.append([dx, sx_log_sum])
                yx.append(dx_pt)
            if dy != 0 and abs(dy_pt) > 0.5:
                Ay.append([dy, sy_log_sum])
                yy.append(dy_pt)

        # Fit X axis
        if len(Ax) >= 2:
            p_x, _, _, _ = np.linalg.lstsq(np.array(Ax), np.array(yx), rcond=None)
            a_x, b_x = float(p_x[0]), float(p_x[1])
            if b_x < 0 or a_x <= 0:
                a_x = max(0.05, float(np.sum(yx) / np.sum([r[0] for r in Ax])))
                b_x = 0.0
        elif Ax:
            a_x = max(0.05, float(np.sum(yx) / np.sum([r[0] for r in Ax])))
            b_x = 0.0
        else:
            a_x, b_x = 0.43, 0.22

        # Fit Y axis
        if len(Ay) >= 2:
            p_y, _, _, _ = np.linalg.lstsq(np.array(Ay), np.array(yy), rcond=None)
            a_y, b_y = float(p_y[0]), float(p_y[1])
            if b_y < 0 or a_y <= 0:
                a_y = max(0.05, float(np.sum(yy) / np.sum([r[0] for r in Ay])))
                b_y = 0.0
        elif Ay:
            a_y = max(0.05, float(np.sum(yy) / np.sum([r[0] for r in Ay])))
            b_y = 0.0
        else:
            a_y, b_y = 0.43, 0.22

        model = cls(a_x=a_x, b_x=b_x, a_y=a_y, b_y=b_y, is_calibrated=True)

        errs_x = [abs(a_x * r[0] + b_x * r[1] - y) for r, y in zip(Ax, yx)] if Ax else [0.0]
        errs_y = [abs(a_y * r[0] + b_y * r[1] - y) for r, y in zip(Ay, yy)] if Ay else [0.0]
        rmse_x = float(np.sqrt(np.mean(np.square(errs_x)))) if errs_x else 0.0
        rmse_y = float(np.sqrt(np.mean(np.square(errs_y)))) if errs_y else 0.0

        stats = {
            "rmse_pt": [round(rmse_x, 3), round(rmse_y, 3)],
            "samples_x": len(Ax),
            "samples_y": len(Ay),
        }
        return model, stats


class Motion:
    """Closed-loop pointer motion in the current screen's points.

    `bounds()` returns the screen size in points (Phone passes its detected geometry). Gains
    are learned per screen size: the 375x812 reference profile keeps the version-2 top-level
    cache layout, other sizes live under "profiles" and start uncalibrated until `calibrate`
    runs on that phone (estimated: AssistiveTouch tracking speed and acceleration can differ
    per model and per user setting; never copy another phone's numbers).
    """

    def __init__(self, link: SerialLink, locate: Callable, *, settle: float = 0.350,
                 calibration_path: Path | None = None, persist: bool = True,
                 step: int = 8, interval: int = 15, sleeper: Callable = time.sleep,
                 pointer_recover_key: str = "", bounds: Callable | None = None,
                 discover: Callable | None = None):
        if not (1 <= step <= 127 and 5 <= interval <= 100):
            raise ValueError("CFG step must be 1..127 and interval 5..100")
        if not math.isfinite(settle) or settle < 0:
            raise ValueError("settle must be nonnegative and finite")
        self.link, self.locate = link, locate
        self.pointer_recover_key = validate_recover_key(pointer_recover_key)
        self.recovery_count = 0
        self.last_recover_key: str | None = None
        self.settle, self.sleeper = settle, sleeper
        self.step, self.interval = step, interval
        self.path = calibration_path or Path.home() / ".config/iphone-hand/calibration.json"
        self.persist = persist
        self.bounds = bounds or (lambda: REFERENCE_PT)
        # Called before rejecting a target outside bounds() that may come from no frame yet.
        self.discover = discover
        self.screen = REFERENCE_PT
        self.profile = REFERENCE_PROFILE
        self._reset_gains()
        if persist:
            self._load()

    def _reset_gains(self) -> None:
        self.counts_per_pt = [1.0, 1.0]  # Estimated until observations replace them.
        self.samples = [0, 0]
        self.observations: list[tuple[int, int, float, float, int, int]] = []
        self.model = ParametricGainModel()
        self.cache_error: str | None = None

    def _ensure_profile(self) -> tuple[int, int]:
        """Follow the current screen size; switching size loads that size's profile."""
        bounds = tuple(self.bounds())
        key = profile_key(bounds)
        if key != self.profile:
            self.profile = key
            self._reset_gains()
            if self.persist:
                self._load()
        self.screen = bounds
        return bounds

    def _profile_data(self, data: dict) -> dict | None:
        if self.profile == REFERENCE_PROFILE:
            return data if "step" in data else None
        profile = data.get("profiles", {}).get(self.profile)
        return profile if isinstance(profile, dict) else None

    def _load(self) -> None:
        try:
            data = self._profile_data(json.loads(self.path.read_text()))
            if data is None:
                return
            if data["step"] != self.step or data["interval"] != self.interval:
                return
            if "model" in data and isinstance(data["model"], dict):
                self.model = ParametricGainModel.from_dict(data["model"])
            gains = data["counts_per_pt"]
            if len(gains) == 2 and all(math.isfinite(g) and 0.05 <= g <= 50 for g in gains):
                self.counts_per_pt = list(gains)
                self.samples = [0, 0]  # New session observes current gain again.
        except FileNotFoundError:
            pass
        except (OSError, ValueError, KeyError, TypeError):
            self.cache_error = "calibration cache could not be read; using estimated gain"

    def _save(self) -> None:
        if not self.persist or not any(self.samples):
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temp = self.path.with_suffix(".tmp")
            payload = {
                "version": 2,
                "step": self.step,
                "interval": self.interval,
                "counts_per_pt": self.counts_per_pt,
                "model": self.model.to_dict(),
            }
            try:
                existing = json.loads(self.path.read_text())
                existing = existing if isinstance(existing, dict) else {}
            except (OSError, ValueError):
                existing = {}
            if self.profile == REFERENCE_PROFILE:
                # Version-2 readers keep working: the reference stays at the top level.
                payload = {**payload, **({"profiles": existing["profiles"]} if "profiles" in existing else {})}
            else:
                profiles = dict(existing.get("profiles", {}))
                profiles[self.profile] = {**payload, "screen_pt": sorted(self.screen)}
                payload = {**existing, "profiles": profiles}
            temp.write_text(json.dumps(payload) + "\n")
            temp.replace(self.path)
        except OSError:
            self.cache_error = "calibration cache could not be written; keeping gains in memory"

    def _position(self, attempts: int = 2) -> tuple[float, float] | None:
        # A single mirrored frame occasionally loses the ring (measured ~3 in 16 on the
        # Portal); one more grab is cheaper than failing the move.
        for _ in range(attempts):
            found = self.locate()
            if found is not None:
                return tuple(found[:2])
        return None

    def _send_move(self, dx: int, dy: int) -> None:
        # Read (firmware policy): +/-2000 per axis and a 10 s command budget.
        # Keep nominal pacing under 8 s to leave room for notify congestion.
        limit = min(2000, self.link.step * max(1, 8000 // self.link.interval))
        if dx == dy == 0:
            self.link.command("MOVE", 0, 0)
        while dx or dy:
            sx, sy = (max(-limit, min(limit, n)) for n in (dx, dy))
            self.link.command("MOVE", sx, sy)
            dx, dy = dx - sx, dy - sy

    def _move(self, dx: int, dy: int) -> tuple[float, float] | None:
        self._send_move(dx, dy)
        self.sleeper(self.settle)
        return self._position()

    def _learn(self, before, after, counts, step: int | None = None, interval: int | None = None) -> None:
        step = step or self.step
        interval = interval or self.interval
        width, height = self.screen
        for axis, bound in enumerate(self.screen):
            delta = after[axis] - before[axis]
            if abs(counts[axis]) < 4 or abs(delta) < 2 or delta * counts[axis] <= 0:
                continue
            observed = abs(counts[axis] / delta)
            if after[axis] <= 1 or after[axis] >= bound - 1:
                # A clamped displacement is not a gain observation. Reduce the
                # estimate conservatively to avoid repeated edge overshoots.
                self.counts_per_pt[axis] *= 0.7
            elif 0.05 <= observed <= 50:
                weight = 1.0 if self.samples[axis] == 0 else 0.75
                self.counts_per_pt[axis] = (1 - weight) * self.counts_per_pt[axis] + weight * observed
                self.samples[axis] += 1

        # Record unclamped observation for model fitting
        if (1 < after[0] < width - 1 and 1 < after[1] < height - 1 and
            1 < before[0] < width - 1 and 1 < before[1] < height - 1):
            dx_pt = after[0] - before[0]
            dy_pt = after[1] - before[1]
            if abs(dx_pt) > 0.5 or abs(dy_pt) > 0.5:
                self.observations.append((counts[0], counts[1], dx_pt, dy_pt, step, interval))
                # Every move_to records one; a long-running daemon must not grow without bound.
                del self.observations[:-MAX_OBSERVATIONS]

    def _reveal_and_home(self) -> tuple[float, float] | None:
        position = self._position()
        if position is None:
            # AssistiveTouch fades an idle pointer out; a tiny there-and-back move brings it back
            # without changing its position (measured 2026-10-10: ring absent after ~idle, then
            # detected right after a 30-count move).
            self.link.command("MOVE", 2, 2)
            position = self._move(-2, -2)
        if position is None:
            # Pin the pointer in the top-left corner, then step back in: on the iPhone XS the
            # corner itself sits under the rounded display edge and the ring is not drawn
            # (measured 2026-10-10).
            self._send_move(-3000, -3000)
            position = self._move(*HOME_OFFSET)
        return position

    def move_to(self, x: float, y: float, *, step: int | None = None,
                interval: int | None = None, adaptive: bool = False) -> dict:
        bounds = self._ensure_profile()
        if self.discover is not None and not (0 <= x <= bounds[0] and 0 <= y <= bounds[1]):
            self.discover()
            bounds = self._ensure_profile()
        validate_target(x, y, bounds)
        eff_step = step if step is not None else self.step
        eff_interval = interval if interval is not None else self.interval
        self.link.command("CFG", f"step={eff_step}", f"interval={eff_interval}")

        recovered_with = None
        position = self._reveal_and_home()
        if position is None and self.pointer_recover_key:
            # Opt-in because it may cancel searches/dialogs. Measured 2026-10-13: in Notes,
            # Esc only hides the keyboard; editing and the hidden ring remain (docs/phone-setup.md).
            key = keys.parse_combo(self.pointer_recover_key)
            self.link.command("KEY", key.mods, key.usage)
            recovered_with = self.pointer_recover_key
            self.recovery_count += 1
            self.last_recover_key = recovered_with
            self.sleeper(self.settle)
            position = self._reveal_and_home()
        if position is None:
            raise PointerHidden(recovered_with)
        if self._ensure_profile() != bounds:
            # The first frame changed the screen size; check the target again before moving.
            bounds = self.screen
            validate_target(x, y, bounds)
        width, height = bounds

        target = (x, y)
        # Approach edge targets from an interior staging point, then finish.
        inside = (max(12, min(width - 12, x)), max(12, min(height - 12, y)))
        staging = inside != target and math.dist(position, target) > 20
        iterations = 0
        fast = False  # Whether the board is currently at the adaptive fast pacing.

        for iteration in range(1, 7):
            if math.dist(position, target) <= 3:
                break
            if staging and math.dist(position, inside) <= 3:
                staging = False
            goal = inside if staging else target
            delta = [goal[i] - position[i] for i in (0, 1)]

            # Adaptive pacing: fast moves for long distance, precise for fine adjustment
            current_step = eff_step
            current_interval = eff_interval
            if adaptive and iteration == 1 and math.hypot(*delta) > 100:
                current_step = min(32, eff_step * 2)
                current_interval = max(10, eff_interval - 5)
                self.link.command("CFG", f"step={current_step}", f"interval={current_interval}")
                fast = True
            elif fast:
                # Fine corrections run (and are learned) at the standard pacing again.
                self.link.command("CFG", f"step={eff_step}", f"interval={eff_interval}")
                fast = False

            if self.model.is_calibrated:
                counts = list(self.model.solve_counts(delta[0], delta[1], step=current_step, interval=current_interval))
            else:
                # Leave margin on long uncalibrated moves; fixed step/interval makes
                # report-speed gain repeatable, while the remainder may accelerate less.
                damping = 0.85 if math.hypot(*delta) > 50 else 1.0
                counts = [max(-32767, min(32767, round(damping * d * g)))
                          for d, g in zip(delta, self.counts_per_pt)]

            if counts == [0, 0]:
                axis = 0 if abs(delta[0]) >= abs(delta[1]) else 1
                counts[axis] = 1 if delta[axis] > 0 else -1

            after = self._move(*counts)
            iterations = iteration
            if after is None:
                raise MotionError("Pointer lost during move; no further moves sent")
            self._learn(position, after, counts, current_step, current_interval)
            position = after

        if step is not None or interval is not None or adaptive:
            self.link.command("CFG", f"step={self.step}", f"interval={self.interval}")

        self._save()
        result = {"x": position[0], "y": position[1], "error_pt": math.dist(position, target),
                  "iterations": iterations}
        if recovered_with:
            result["recovered_with"] = recovered_with
        return result

    def calibrate(self) -> dict:
        """Observe fixed-pacing moves inside the screen, fitting the parametric model.

        Run once per phone (per screen-size profile) on hardware; the plan stays inside the
        screen's interior (80 pt from the sides, 150 pt from the top and 152 pt from the bottom,
        the XS window [80..295, 150..660] scaled to the current size).
        """
        self.link.command("CFG", f"step={self.step}", f"interval={self.interval}")
        width, height = self._ensure_profile()
        centre = (width / 2, height / 2)

        def recentre():
            self.move_to(*centre)
            position = self._position()
            if position is None:
                raise MotionError("Pointer unavailable for calibration")
            return position

        pos = recentre()

        # Calibration sample plan: sizes, directions, and balanced return moves
        # designed to fit well within a ~60s budget and stay strictly away from edges.
        plan = [
            # Pure X moves
            (16, 0), (-16, 0),
            (60, 0), (-60, 0),
            (120, 0), (-120, 0),
            # Pure Y moves
            (0, 16), (0, -16),
            (0, 60), (0, -60),
            (0, 140), (0, -140),
            # Symmetric coupled diagonals
            (40, 40), (-40, -40),
            (60, -60), (-60, 60),
            (90, 90), (-90, -90),
            # Asymmetric coupled diagonals
            (80, 25), (-80, -25),
            (25, 80), (-25, -80),
        ]

        for dx, dy in plan:
            # Re-center if pointer drifted too close to bounds
            if pos[0] < 80 or pos[0] > width - 80 or pos[1] < 150 or pos[1] > height - 152:
                pos = recentre()

            after = self._move(dx, dy)
            if after is None:
                raise MotionError("Pointer lost during calibration move")
            self._learn(pos, after, [dx, dy], self.step, self.interval)
            pos = after

        if not self.observations:
            raise MotionError("Calibration did not observe any unclamped moves")

        # Fit parametric model from observations
        model, stats = ParametricGainModel.fit(self.observations)
        self.model = model
        self.counts_per_pt = self.model.nominal_counts_per_pt(self.step, self.interval)

        # Move back to center
        self.move_to(*centre)
        self._save()

        return {
            "profile": self.profile,
            "counts_per_pt": list(self.counts_per_pt),
            "step": self.step,
            "interval": self.interval,
            "samples": list(self.samples),
            "cache_error": self.cache_error,
            "model": self.model.to_dict(),
            "rmse_pt": stats["rmse_pt"],
        }
