import inspect

from PIL import Image
import pytest

from iphone_hand.fake import FakePhone
from iphone_hand.phone import Phone


def test_api_signature_parity_and_fake_records(real_screen):
    _, image = real_screen
    phone = FakePhone(image)
    for method in ["screenshot", "find_pointer", "move_to", "tap", "long_press", "drag", "scroll",
                   "type_text", "key", "home", "status"]:
        actual = inspect.signature(getattr(Phone, method)).parameters
        fake = inspect.signature(getattr(FakePhone, method)).parameters
        assert list(actual) == list(fake)
        assert [p.default for p in actual.values()] == [p.default for p in fake.values()]
    assert phone.screenshot().size == (750, 1624)
    assert phone.tap(100, 200)["error_pt"] == 0
    assert ("tap", 100, 200, "left") in phone.calls


def test_actions_and_status(rig):
    phone, device, screen = rig
    assert phone.screenshot().size == (750, 1624)
    assert phone.tap(100, 200, "right")["error_pt"] <= 3
    assert ("BTN", 2) in device.reports
    phone.long_press(100, 200)
    assert ("WAIT", 800) in device.reports
    phone.drag(100, 200, 150, 250, 3)
    assert device.buttons == 0
    assert any(r[0] == "MOVE" and r[3] == 1 for r in device.reports)
    phone.scroll(-10, 3, 150, 250)
    assert ("SCROLL", -10, 3) in device.reports
    phone.type_text("Hi!")
    phone.key("cmd+space")
    phone.home()
    assert ("KEY", 8, 11) in device.reports
    phone.home_action = "button3"
    phone.home()
    assert ("BTN", 4) in device.reports
    status = phone.status()
    assert status["ble"]["ready"]
    assert status["last_grab_ms"] == screen.last_grab_ms
    assert status["adb"]["grab_ms"] == {"png": None, "raw": None}


def test_drag_release_on_capture_failure_and_press_timeout(rig):
    phone, device, _ = rig
    def fail_move(x, y):
        if device.buttons:
            raise RuntimeError("capture failed")
        return {"x": x, "y": y, "error_pt": 0, "iterations": 0}
    phone.move_to = fail_move
    with pytest.raises(RuntimeError, match="capture failed"):
        phone.drag(1, 1, 10, 10)
    assert device.buttons == 0
    assert device.commands[-1][1:] == ("BTN", ("0",))
    original = phone.link.command
    def fail_ack(command, *args, **kwargs):
        result = original(command, *args, **kwargs)
        if command == "BTN" and args == (1,):
            raise TimeoutError("press ACK lost")
        return result
    phone.link.command = fail_ack
    with pytest.raises(TimeoutError, match="ACK lost"):
        phone.drag(1, 1, 10, 10)
    assert device.buttons == 0


def test_validate_before_any_action(rig):
    phone, device, _ = rig
    calls = [lambda: phone.type_text("valid prefix☃"), lambda: phone.tap(10, 10, "wrong"),
             lambda: phone.scroll(1, x=10), lambda: phone.drag(1, 1, 999, 2),
             lambda: phone.long_press(10, 10, -1), lambda: phone.key("bogus+a")]
    for call in calls:
        with pytest.raises(ValueError):
            call()
    assert not device.commands


def test_scroll_outside_firmware_range_is_rejected_before_positioning(rig):
    phone, device, _ = rig
    for dy, dx in [(2001, 0), (0, -2001), (32767, 0)]:
        with pytest.raises(ValueError):
            phone.scroll(dy, dx, 100, 200)
    assert not device.commands
    phone.scroll(-2000, 2000)
    assert device.commands[-1][1:] == ("SCROLL", ("-2000", "2000"))


def test_constructor_is_lazy_and_status_does_not_grab(rig):
    phone = Phone(persist_calibration=False)
    assert phone.link.transport is None
    phone.close()
    phone, _, screen = rig
    screen.grab = lambda: pytest.fail("status should not capture a frame")
    assert phone.status()["last_grab_ms"] is None


def test_pipeline_typing_options_and_release(rig):
    phone, device, _ = rig
    assert phone.type_text('book') == {'typed': 4}
    assert [r for r in device.reports if r[0] == 'WAIT'] == [('WAIT', 30)] * 4
    assert device.reports.count(('KEY', 0, 0)) == 4
    assert phone.type_text('Hi', window=2, hold_ms=5) == {'typed': 2}
    assert all(c[2][-1] == '5' for c in device.commands[-2:])
    assert phone.type_text('') == {'typed': 0}
    before = len(device.commands)
    for kwargs in [{'window': 13}, {'hold_ms': -1}, {'hold_ms': True}]:
        with pytest.raises(ValueError): phone.type_text('ok', **kwargs)
    assert len(device.commands) == before


def test_type_incomplete_count_and_no_text(rig):
    from iphone_hand.phone import TypeIncomplete
    phone, device, _ = rig
    execute = device._execute
    def busy(command, args):
        return 'ERR BUSY' if len(device.commands) == 3 else execute(command, args)
    device._execute = busy
    with pytest.raises(TypeIncomplete) as caught: phone.type_text('private text longer than window')
    assert caught.value.typed == 7 and caught.value.sent == 8
    assert caught.value.confirmed_indices == (0, 1, 3, 4, 5, 6, 7)
    assert 'private' not in str(caught.value)


def test_key_chord_holds_keys_together_then_releases(rig):
    phone, device, _ = rig
    phone.key("tab+f")
    reports = [r for r in device.reports if r[0] == "KREPORT"]
    assert reports == [("KREPORT", 0, 43), ("KREPORT", 0, 43, 9), ("KREPORT", 0, 0)]
    assert device.keyboard == (0, 0)
    with pytest.raises(ValueError):
        phone.key("tab+banana")
