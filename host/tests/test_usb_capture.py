"""USB capture tests: a fake backend stands in for AVFoundation; no device, session or TCC."""
import json
import time

from PIL import Image
import pytest

from iphone_hand import daemon, platforms
from iphone_hand.airplay import FrameUnavailable
from iphone_hand.daemon import Config, create_app
from iphone_hand.elements import Elements
from iphone_hand.screen import PhoneRect
from iphone_hand.usb_capture import UsbCaptureScreen, UsbCaptureUnavailable, _pt_size


def frame(w=1125, h=2436):
    return Image.new('RGB', (w, h), (30, 30, 30))


class FakeBackend:
    def __init__(self, device='iPhone', running=True, frames=()):
        self.device, self._running = device, running
        self.frames = list(frames)  # (image, timestamp)
        self.started = self.stopped = 0
        self._fps = 0.0

    def start(self):
        self.started += 1

    def stop(self):
        self.stopped += 1

    def running(self):
        return self._running

    def device_present(self):
        return self.device is not None

    def device_name(self):
        return self.device

    def latest(self):
        return self.frames[-1] if self.frames else (None, None)

    def fps(self):
        return self._fps


def test_pt_size_derivation():
    assert _pt_size((1125, 2436), 0) == (375, 812)  # XS native @3x
    # AirPlay-measured XS frame (1126x2434) is within the native tolerance of 1125x2436:
    # 375x812, where the old per-axis rounding gave a non-uniform 375x811.
    assert _pt_size((1126, 2434), 0) == (375, 812)
    assert _pt_size((2000, 1000), 3.0027) == (666, 333)  # explicit per-model scale wins
    assert _pt_size((1170, 2532), 0) == (390, 844)  # iPhone 12/13/14 @3x
    assert _pt_size((828, 1792), 0) == (414, 896)  # iPhone 11/XR @2x
    assert _pt_size((1000, 1000), 0) == (333, 333)  # unknown: @3x fallback [estimated]


def test_usb_grab_and_point_mapping():
    backend = FakeBackend(frames=[(frame(), 1000.0)])
    screen = UsbCaptureScreen(backend=backend, clock=lambda: 1000.5)
    image = screen.grab()
    assert image.size == (1125, 2436)
    assert screen.rect == PhoneRect(0, 0, 1125, 2436, 375, 812)
    assert screen.rect.px_to_pt(1125, 2436) == (375.0, 812.0)
    assert screen.last_grab_ms is not None
    status = screen.status()
    assert status['mode'] == 'usb' and status['mirroring'] and status['client_connected']
    assert status['device'] == 'iPhone' and status['pt_size'] == [375, 812]
    assert backend.started == 1


def test_usb_static_frame_stays_valid_while_connected():
    # Same rule as AirPlay: iOS may send nothing on a still/locked screen.
    backend = FakeBackend(frames=[(frame(), 1000.0)])
    screen = UsbCaptureScreen(backend=backend, clock=lambda: 1600.0)
    assert screen.grab().size == (1125, 2436)
    assert screen.status()['mirroring'] and screen.status()['frame_age_s'] == 600.0
    assert screen.status()['fps'] == 0.0  # stale frames are not a live rate


def test_usb_unplugged_with_stale_frame_fails_closed():
    backend = FakeBackend(device=None, frames=[(frame(), 1000.0)])
    screen = UsbCaptureScreen(backend=backend, clock=lambda: 1600.0)
    assert not screen.status()['mirroring'] and not screen.status()['client_connected']
    with pytest.raises(FrameUnavailable, match='disconnected'):
        screen.grab()
    # A recently-seen frame right after unplug is still served briefly (settle window).
    recent = UsbCaptureScreen(backend=FakeBackend(device=None, frames=[(frame(), 1000.0)]),
                              clock=lambda: 1001.0)
    assert recent.grab().size == (1125, 2436)


def test_usb_replug_and_no_frames_yet():
    backend = FakeBackend(frames=[])
    screen = UsbCaptureScreen(backend=backend, clock=lambda: 1000.0)
    assert screen.status()['mirroring']  # Connected and running; first frame pending.
    assert screen.status()['frame_age_s'] is None
    with pytest.raises(FrameUnavailable, match='Waiting'):
        screen.grab()
    backend.frames.append((frame(750, 1624), 1000.5))  # Replug with a different resolution.
    assert screen.grab().size == (750, 1624)
    assert screen.rect.width == 750 and screen.rect.pt_width == 375
    session_dead = FakeBackend(device='iPhone', running=False, frames=[(frame(), 1000.0)])
    assert not UsbCaptureScreen(backend=session_dead, clock=lambda: 1001.0).status()['mirroring']


@pytest.mark.parametrize('value', ['mirror', '', None, True, 'USB'])
def test_frame_source_config_rejects_unknown_modes(value):
    with pytest.raises(ValueError, match='frame_source'):
        Config(token='test-token', frame_source=value)


def test_frame_source_config_round_trip(tmp_path):
    assert Config(token='test-token').frame_source == 'airplay'
    assert Config(token='test-token').usb_pt_scale == 0.0
    path = tmp_path/'daemon.toml'
    path.write_text('token = "test-token"\nframe_source = "usb"\nusb_pt_scale = 3.0027\n')
    config = Config.load(path)
    assert config.frame_source == 'usb' and config.usb_pt_scale == 3.0027
    with pytest.raises(ValueError, match='usb_pt_scale'):
        Config(token='test-token', usb_pt_scale=-1)


def test_usb_rejected_on_linux(tmp_path, monkeypatch, rig):
    phone, _, _ = rig
    monkeypatch.setattr(platforms, 'current_platform', lambda: platforms.LINUX)
    monkeypatch.setenv('XDG_RUNTIME_DIR', str(tmp_path))
    with pytest.raises(ValueError, match='requires macOS'):
        create_app(Config(token='test-token', frame_source='usb'),
                   phone=phone, elements=Elements(reader=None))


def test_usb_app_wiring_and_state(tmp_path, monkeypatch, rig):
    from fastapi.testclient import TestClient
    phone, _, _ = rig
    monkeypatch.setattr(platforms, 'current_platform', lambda: platforms.MACOS)
    backend = FakeBackend(frames=[(frame(), time.time())])
    def factory(**kwargs):
        assert kwargs['pt_scale'] == 3.0
        return UsbCaptureScreen(backend=backend, **kwargs)
    monkeypatch.setattr('iphone_hand.usb_capture.UsbCaptureScreen', factory)
    config = Config(token='test-token', frame_source='usb', usb_pt_scale=3.0)
    app = create_app(config, phone=phone, elements=Elements(reader=None))
    with TestClient(app) as client:
        state = client.get('/api/state', headers={'Authorization': 'Bearer test-token'}).json()
    assert state['airplay']['mode'] == 'usb' and state['airplay']['frame_source'] == 'usb'
    assert state['receiver'] is None  # No UxPlay needed in usb mode.
    assert backend.started == 1 and backend.stopped == 1  # Lifespan stops the session.


def test_usb_backend_unavailable_without_pyobjc(monkeypatch):
    import builtins
    real_import = builtins.__import__
    def blocked(name, *args, **kwargs):
        if name.split('.')[0] in ('AVFoundation', 'CoreMedia', 'CoreMediaIO', 'Quartz'):
            raise ImportError(name)
        return real_import(name, *args, **kwargs)
    monkeypatch.setattr(builtins, '__import__', blocked)
    import iphone_hand.usb_capture as usb
    with pytest.raises(UsbCaptureUnavailable, match='PyObjC'):
        usb.AVFoundationBackend()
