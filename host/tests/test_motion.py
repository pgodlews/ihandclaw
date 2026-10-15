import json
import math

import numpy as np
import pytest

from iphone_hand.fake import (
    FakeSerialDevice,
    SimulatedScreen,
    DEFAULT_SYNTHETIC_CURVE,
    IPHONE_XS_MEASURED_CURVE,
)
from iphone_hand.motion import (HOME_OFFSET, MAX_OBSERVATIONS, MotionError, PointerHidden,
                                ParametricGainModel)
from iphone_hand.phone import Phone
from iphone_hand.serial_link import SerialLink


@pytest.mark.parametrize("sensitivity", [(1, 1), (.65, 1.35), (1.6, .6)])
@pytest.mark.parametrize("target", [(100, 200), (300, 700), (0, 0), (375, 812),
    (1, 811), (374, 1), (0, 406), (375, 406), (187.5, 0), (187.5, 812)])
def test_move_converges_with_vision_and_acceleration(sensitivity, target):
    device = FakeSerialDevice(sensitivity=sensitivity)
    phone = Phone(SerialLink(transport=device), SimulatedScreen(device), settle=0,
                  persist_calibration=False)
    result = phone.move_to(*target)
    assert result["error_pt"] <= 3
    assert math.dist(device.position, target) <= 3
    assert result["iterations"] <= 6
    assert (device.step, device.interval) == (8, 15)


@pytest.mark.parametrize("sensitivity", [(1, 1), (.65, 1.35), (1.6, .6)])
def test_random_targets_and_edge_to_edge_sequence(sensitivity):
    device = FakeSerialDevice(position=(0, 0), sensitivity=sensitivity)
    phone = Phone(SerialLink(transport=device), SimulatedScreen(device), settle=0,
                  persist_calibration=False)
    rng = np.random.default_rng(812375)
    targets = [(375, 812), (0, 0), (1, 811), (374, 1)]
    targets += list(rng.uniform([0, 0], [375, 812], size=(24, 2)))
    for target in targets:
        result = phone.move_to(*target)
        assert result["error_pt"] <= 3, (target, result)
        assert math.dist(device.position, target) <= 3
        assert result["iterations"] <= 6


def test_home_when_unknown_and_settle_delay(rig):
    phone, device, screen = rig
    original = screen.grab
    hidden_grabs = 4  # first locate retries once, then the wake-up nudge retries once, then homing
    def grab():
        nonlocal hidden_grabs
        screen.hidden = hidden_grabs > 0
        hidden_grabs -= 1
        return original()
    screen.grab = grab
    waits = []
    phone.motion.settle = .350
    phone.motion.sleeper = waits.append
    result = phone.move_to(200, 400)
    moves = [c for c in device.commands if c[1] == "MOVE"]
    assert moves[0][2] == ("2", "2") and moves[1][2] == ("-2", "-2")  # wake a faded pointer
    assert moves[2][2] == ("-2000", "-2000") and moves[3][2] == ("-1000", "-1000")  # bounded corner pin
    assert moves[4][2] == tuple(str(c) for c in HOME_OFFSET)
    assert result["error_pt"] <= 3
    assert len(waits) == len(moves) - 3  # no settle after the nudge out or the two corner commands
    assert set(waits) == {.350}


def test_pointer_absent_after_homing_stops(rig):
    phone, device, screen = rig
    screen.hidden = True
    with pytest.raises(PointerHidden, match="pointer hidden.*press esc"):
        phone.move_to(100, 200)
    assert len([c for c in device.commands if c[1] == "MOVE"]) == 5  # nudge out/back, 2 corner, step in
    assert not any(c[1] == "KEY" for c in device.commands)


@pytest.mark.parametrize("action", ["move_to", "tap", "long_press", "drag", "scroll"])
@pytest.mark.parametrize("return_after", ["key", "reveal", "home"])
def test_pointer_recovery_success(rig, action, return_after):
    phone, device, screen = rig
    phone.motion.pointer_recover_key = "esc"
    screen.hidden = True
    original = phone.link.command
    key_sent = False
    moves_after_key = 0

    def command(name, *args, **kwargs):
        nonlocal key_sent, moves_after_key
        reply = original(name, *args, **kwargs)
        if name == "KEY":
            key_sent = True
        elif name == "MOVE" and key_sent:
            moves_after_key += 1
        threshold = {"key": 0, "reveal": 2, "home": 5}[return_after]
        if key_sent and moves_after_key >= threshold:
            screen.hidden = False
        return reply

    phone.link.command = command
    waits = []
    phone.motion.settle = .350
    phone.motion.sleeper = waits.append
    if action == "drag":
        result = phone.drag(100, 200, 150, 250, steps=2)
    elif action == "scroll":
        result = phone.scroll(-1, x=100, y=200)
    else:
        result = getattr(phone, action)(100, 200)
    assert result["error_pt"] <= 3
    assert result["recovered_with"] == "esc"
    assert [c[2] for c in device.commands if c[1] == "KEY"] == [("0", "41")]
    assert waits and set(waits) == {.350}
    # The first two sleeps precede recovery; the third settles the key.
    assert len(waits) >= 3
    assert "recovered_with" not in phone.move_to(100, 200)


def test_pointer_recovery_failure_is_bounded(rig):
    phone, device, screen = rig
    phone.motion.pointer_recover_key = "esc"
    screen.hidden = True
    with pytest.raises(PointerHidden) as error:
        phone.tap(100, 200)
    assert error.value.recovered_with == "esc"
    assert [c[2] for c in device.commands if c[1] == "KEY"] == [("0", "41")]
    assert len([c for c in device.commands if c[1] == "MOVE"]) == 10
    assert not any(c[1] == "CLICK" for c in device.commands)


def test_pointer_recovery_constructor_and_custom_key():
    device = FakeSerialDevice()
    screen = SimulatedScreen(device)
    phone = Phone(SerialLink(transport=device), screen, settle=0,
                  persist_calibration=False, pointer_recover_key="ESCAPE")
    assert phone.motion.pointer_recover_key == "escape"
    screen.hidden = True
    original = phone.link.command

    def command(name, *args, **kwargs):
        reply = original(name, *args, **kwargs)
        if name == "KEY":
            screen.hidden = False
        return reply

    phone.link.command = command
    assert phone.move_to(100, 200)["recovered_with"] == "escape"


@pytest.mark.parametrize("combo", [None, True, "shift", " ", "bogus+a"])
def test_invalid_recovery_key_rejected_before_commands(combo):
    device = FakeSerialDevice()
    with pytest.raises(ValueError, match="pointer_recover_key"):
        Phone(SerialLink(transport=device), SimulatedScreen(device),
              persist_calibration=False, pointer_recover_key=combo)
    assert not device.commands


def test_nonconvergence_is_bounded_and_tap_does_not_click(rig):
    phone, device, _ = rig
    phone.motion.locate = lambda: (187.5, 406, 1)
    result = phone.move_to(100, 100)
    assert result["iterations"] == 6 and result["error_pt"] > 3
    with pytest.raises(MotionError, match="failed to converge"):
        phone.tap(100, 100)
    assert not any(c[1] == "CLICK" for c in device.commands)


def test_lost_pointer_stops_immediately(rig):
    phone, device, _ = rig
    readings = iter([(187.5, 406, 1), None, None])  # a miss is retried once
    phone.motion.locate = lambda: next(readings)
    with pytest.raises(MotionError, match="lost"):
        phone.move_to(100, 100)
    assert len([c for c in device.commands if c[1] == "MOVE"]) == 1


@pytest.mark.parametrize("target", [(-1, 2), (376, 2), (0, 813), (float("nan"), 1),
                                  (1, float("inf"))])
def test_invalid_targets_do_not_send_commands(rig, target):
    phone, device, _ = rig
    with pytest.raises(ValueError):
        phone.move_to(*target)
    assert not device.commands


def test_gain_cache_is_pacing_specific_and_calibration(rig, tmp_path):
    phone, _, _ = rig
    result = phone.calibrate()
    assert all(s > 0 for s in result["samples"])
    saved = json.loads((tmp_path / "calibration.json").read_text())
    assert saved["counts_per_pt"] == result["counts_per_pt"]
    other = Phone(SerialLink(transport=FakeSerialDevice()), SimulatedScreen(FakeSerialDevice()),
                  settle=0, calibration_path=tmp_path / "calibration.json")
    assert other.motion.counts_per_pt == saved["counts_per_pt"]
    saved["step"] = 7
    (tmp_path / "calibration.json").write_text(json.dumps(saved))
    other.motion._load()
    # Setting mismatch must not overwrite the current gain.
    assert other.motion.counts_per_pt == result["counts_per_pt"]
    (tmp_path / "calibration.json").write_text("broken")
    other.motion._load()
    assert other.motion.cache_error


def test_parametric_gain_model_coupling_and_prediction():
    model = ParametricGainModel(a_x=0.43, b_x=0.22, a_y=0.43, b_y=0.22, is_calibrated=True)

    # Pure 1-axis move: speed is 8 counts/report
    disp_x_single, disp_y_single = model.predict_displacement(50, 0, step=8, interval=15)
    assert disp_y_single == 0.0

    # Diagonal move: speed is hypot(8, 8) ≈ 11.314 counts/report
    disp_x_diag, disp_y_diag = model.predict_displacement(50, 50, step=8, interval=15)

    # 2D vector coupling: diagonal movement on Y increases gain on X
    gain_single = disp_x_single / 50.0
    gain_diag = disp_x_diag / 50.0
    assert gain_diag > gain_single, f"Expected gain_diag ({gain_diag:.4f}) > gain_single ({gain_single:.4f})"

    # Inverse solver test
    targets = [(72.8, 0.0), (0.0, 72.8), (39.5, 39.5), (120.0, -250.0)]
    for tx, ty in targets:
        dx, dy = model.solve_counts(tx, ty, step=8, interval=15)
        px, py = model.predict_displacement(dx, dy, step=8, interval=15)
        err = math.dist((px, py), (tx, ty))
        assert err <= 0.5, f"Target ({tx}, {ty}) had prediction error {err:.2f} pt with counts ({dx}, {dy})"


def test_parametric_gain_model_fits_iphone_xs_measurements():
    # Measured on real iPhone XS (at step=8, interval=15):
    # +100 counts on one axis -> 72.8 pt (twice, both axes)
    # 50,50 diagonal -> 39.5 pt per axis
    # -50,-50 returned exactly to start
    # -204,+202 diagonal -> about 164.6 pt and 163 pt
    observations = [
        (100, 0, 72.8, 0.0, 8, 15),
        (-100, 0, -72.8, 0.0, 8, 15),
        (0, 100, 0.0, 72.8, 8, 15),
        (0, -100, 0.0, -72.8, 8, 15),
        (50, 50, 39.5, 39.5, 8, 15),
        (-50, -50, -39.5, -39.5, 8, 15),
        (-204, 202, -164.6, 163.0, 8, 15),
        (204, -202, 164.6, -163.0, 8, 15),
    ]
    model, stats = ParametricGainModel.fit(observations)
    assert model.is_calibrated
    assert stats["rmse_pt"][0] < 0.5
    assert stats["rmse_pt"][1] < 0.5

    # Check predictions against measured points
    p100_x, _ = model.predict_displacement(100, 0, step=8, interval=15)
    assert abs(p100_x - 72.8) < 0.5

    p50_x, p50_y = model.predict_displacement(50, 50, step=8, interval=15)
    assert abs(p50_x - 39.5) < 0.6
    assert abs(p50_y - 39.5) < 0.6

    p204_x, p202_y = model.predict_displacement(-204, 202, step=8, interval=15)
    assert abs(p204_x - (-164.6)) < 0.5
    assert abs(p202_y - 163.0) < 0.5


def test_calibration_plan_and_persistence(rig, tmp_path):
    phone, device, _ = rig
    result = phone.calibrate()

    assert "model" in result
    assert result["model"]["type"] == "log_speed"
    assert result["model"]["is_calibrated"]
    assert len(phone.motion.observations) >= 20

    # Ensure saved JSON contains model parameters
    saved = json.loads((tmp_path / "calibration.json").read_text())
    assert saved["version"] == 2
    assert "model" in saved
    assert saved["model"]["a_x"] == result["model"]["a_x"]

    # Loading in a new phone restores calibrated model
    other = Phone(SerialLink(transport=FakeSerialDevice()), SimulatedScreen(FakeSerialDevice()),
                  settle=0, calibration_path=tmp_path / "calibration.json")
    assert other.motion.model.is_calibrated
    assert other.motion.model.a_x == result["model"]["a_x"]
    assert other.motion.model.b_x == result["model"]["b_x"]


def test_move_to_step_interval_and_adaptive_options(rig):
    phone, device, _ = rig
    # Custom step/interval for a move
    result = phone.motion.move_to(200, 300, step=16, interval=10)
    assert result["error_pt"] <= 3

    # Check CFG commands: was step=16 interval=10 configured and then restored to (8, 15)?
    cfg_cmds = [c for c in device.commands if c[1] == "CFG"]
    assert any("step=16" in " ".join(c[2]) for c in cfg_cmds)
    assert (device.step, device.interval) == (8, 15)

    # Adaptive long move
    res_adapt = phone.motion.move_to(50, 100, adaptive=True)
    assert res_adapt["error_pt"] <= 3
    assert (device.step, device.interval) == (8, 15)


def test_adaptive_pacing_reverts_for_corrections(rig):
    phone, device, _ = rig
    device.position = [20.0, 20.0]
    result = phone.motion.move_to(350, 780, adaptive=True)
    pacing, paces = (8, 15), []
    for _, command, args in device.commands:
        if command == "CFG":
            settings = dict(a.split("=") for a in args)
            pacing = (int(settings["step"]), int(settings["interval"]))
        elif command == "MOVE":
            paces.append(pacing)
    assert result["iterations"] >= 2 and result["error_pt"] <= 3
    assert paces[0] == (16, 10)  # The long first move runs fast...
    assert set(paces[1:]) == {(8, 15)}  # ...and every correction at the standard pacing.
    assert {o[4:] for o in phone.motion.observations[1:]} == {(8, 15)}


def test_calibration_reports_lost_pointer_after_recentring(rig):
    phone, _, _ = rig
    # Near an edge, so the plan re-centres; the pointer is then lost.
    positions = iter([(10.0, 10.0), None])
    phone.motion._position = lambda attempts=2: next(positions)
    phone.motion.move_to = lambda x, y: {}
    with pytest.raises(MotionError, match="Pointer unavailable for calibration"):
        phone.motion.calibrate()


def test_observations_are_bounded(rig):
    phone, _, _ = rig
    for _ in range(MAX_OBSERVATIONS + 50):
        phone.motion._learn((100.0, 100.0), (110.0, 110.0), [10, 10])
    assert len(phone.motion.observations) == MAX_OBSERVATIONS


def test_first_move_error_statistics_before_vs_after():
    """Compare first-move error statistics: baseline (constant gain) vs calibrated parametric model.
    
    Verifies that the parametric model achieves <= 3 pt on the first move most of the time (>95%),
    whereas the baseline without speed-dependent gain has large first-move errors.
    """
    rng = np.random.default_rng(20261010)
    targets = list(rng.uniform([40, 40], [335, 770], size=(50, 2)))

    def benchmark_session(calibrated: bool, noise_std: float = 0.0):
        first_errors = []
        iterations = []
        for target in targets:
            device = FakeSerialDevice(position=(187.5, 406.0), noise_std=noise_std, rng_seed=42)
            screen = SimulatedScreen(device)
            phone = Phone(SerialLink(transport=device), screen, settle=0, persist_calibration=False)

            if calibrated:
                # Use calibrated model matching the simulator curve
                phone.motion.model = ParametricGainModel(0.43, 0.22, 0.43, 0.22, is_calibrated=True)
            else:
                # Baseline: uncalibrated motion with nominal counts_per_pt and damping
                phone.motion.model.is_calibrated = False
                phone.motion.counts_per_pt = [1.09, 1.09]

            first_pos = None
            orig_move = phone.motion._move
            def tracked_move(dx, dy):
                nonlocal first_pos
                pos = orig_move(dx, dy)
                if first_pos is None:
                    first_pos = pos
                return pos
            phone.motion._move = tracked_move

            res = phone.move_to(*target)
            first_err = math.dist(first_pos, target)
            first_errors.append(first_err)
            iterations.append(res["iterations"])

        return {
            "mean": float(np.mean(first_errors)),
            "median": float(np.median(first_errors)),
            "p95": float(np.percentile(first_errors, 95)),
            "max": float(np.max(first_errors)),
            "under_3pt_pct": float(np.mean([e <= 3.0 for e in first_errors]) * 100.0),
            "iter_mean": float(np.mean(iterations)),
        }

    stats_before = benchmark_session(calibrated=False, noise_std=0.0)
    stats_after = benchmark_session(calibrated=True, noise_std=0.0)
    stats_noise = benchmark_session(calibrated=True, noise_std=0.05)

    print("\n" + "=" * 80)
    print("FIRST-MOVE ERROR STATISTICS (50 targets across screen):")
    print(f"{'Metric':<24} | {'Before (Baseline)':<18} | {'After (Parametric)':<18} | {'After + Noise (0.05)':<18}")
    print("-" * 80)
    print(f"{'Mean Error (pt)':<24} | {stats_before['mean']:18.2f} | {stats_after['mean']:18.2f} | {stats_noise['mean']:18.2f}")
    print(f"{'Median Error (pt)':<24} | {stats_before['median']:18.2f} | {stats_after['median']:18.2f} | {stats_noise['median']:18.2f}")
    print(f"{'95th % Error (pt)':<24} | {stats_before['p95']:18.2f} | {stats_after['p95']:18.2f} | {stats_noise['p95']:18.2f}")
    print(f"{'Max Error (pt)':<24} | {stats_before['max']:18.2f} | {stats_after['max']:18.2f} | {stats_noise['max']:18.2f}")
    print(f"{'<= 3 pt Rate (%)':<24} | {stats_before['under_3pt_pct']:18.1f} | {stats_after['under_3pt_pct']:18.1f} | {stats_noise['under_3pt_pct']:18.1f}")
    print(f"{'Mean Iterations':<24} | {stats_before['iter_mean']:18.2f} | {stats_after['iter_mean']:18.2f} | {stats_noise['iter_mean']:18.2f}")
    print("=" * 80)

    # Verifications for Deliverable B & C:
    # Before: first move error is large (average > 15 pt), few moves land <= 3 pt
    assert stats_before["mean"] > 15.0
    assert stats_before["under_3pt_pct"] < 25.0

    # After: first move lands <= 3 pt most of the time (>95%), mean error <= 1.0 pt
    assert stats_after["mean"] <= 1.0
    assert stats_after["max"] <= 3.0
    assert stats_after["under_3pt_pct"] >= 95.0

    # Under noise: robust performance
    assert stats_noise["mean"] <= 1.5
    assert stats_noise["under_3pt_pct"] >= 90.0



def test_move_chunks_respect_totals_and_nominal_command_budget(rig):
    phone, device, _ = rig
    phone.link.command("CFG", "step=1", "interval=100")
    phone.motion._send_move(-3000, 2101)
    moves = [tuple(map(int, c[2])) for c in device.commands if c[1] == "MOVE"]
    assert tuple(map(sum, zip(*moves))) == (-3000, 2101)
    assert all(max(abs(x), abs(y)) <= 80 for x, y in moves)
