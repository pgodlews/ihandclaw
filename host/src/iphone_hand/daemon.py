"""Headless entry point. No device is opened until main starts the application."""
import argparse
from dataclasses import dataclass, field
import importlib
import math
from pathlib import Path
import tomllib
from urllib.parse import urlsplit

from . import geometry, platforms
from .airplay import AirPlayScreen, ExternalUxPlayReceiver, UxPlaySupervisor
from .elements import Elements
from .phone import Phone
from .motion import validate_recover_key
from .pointer import get_detector
from .serial_link import SerialLink


@dataclass(frozen=True)
class Config:
    token: str = field(repr=False)
    link: str = 'serial:/dev/ttyACM0'
    link_token: str = field(default='', repr=False)
    bind: str = 'iphone-hand.local'
    port: int = 8790
    uxplay_name: str = 'iPhone Hand'
    receiver: str = 'external'
    allow_device_id: str | None = None
    home_action: str = 'cmd+h'
    pointer_recover_key: str = ''
    vc_offset_x: float = 10.0
    vc_offset_y: float = 12.0
    frames_dir: str | None = None
    frame_source: str = 'airplay'
    usb_pt_scale: float = 0.0  # px/pt for USB capture; 0 = derive from the frame
    # Screen geometry: None detects it from frames; [w, h] portrait points overrides it
    # (needed over AirPlay for Face ID iPhones other than 375x812, see geometry.detect).
    screen_pt: list[int] | None = None
    device: str = 'iphone'  # size table: iphone, or ipad (groundwork, untested)
    pointer_detector: str = 'assistivetouch'  # or ipados (prototype, untested)

    def __post_init__(self):
        validate_recover_key(self.pointer_recover_key)
        if not isinstance(self.token, str) or not self.token.strip():
            raise ValueError('daemon token is required')
        if not isinstance(self.link_token, str):
            raise ValueError('link_token must be a string')
        if self.receiver not in ('external', 'child'):
            raise ValueError('receiver must be external or child')
        for name in ('link', 'bind', 'uxplay_name', 'home_action'):
            if not isinstance(getattr(self, name), str) or not getattr(self, name):
                raise ValueError(f'{name} must be a nonempty string')
        if isinstance(self.port, bool) or not isinstance(self.port, int) or not 1 <= self.port <= 65535:
            raise ValueError('port must be 1..65535')
        if self.allow_device_id is not None and (not isinstance(self.allow_device_id, str) or not self.allow_device_id):
            raise ValueError('allow_device_id must be a nonempty string')
        for offset in (self.vc_offset_x, self.vc_offset_y):
            if isinstance(offset, bool) or not isinstance(offset, (int, float)) or not math.isfinite(offset) or not 0 <= offset <= 50:
                raise ValueError('VC offsets must be finite points in 0..50')
        if self.frames_dir is not None:
            if not isinstance(self.frames_dir, str) or not self.frames_dir:
                raise ValueError('frames_dir must be a nonempty string')
            if not Path(self.frames_dir).is_absolute():
                raise ValueError('frames_dir must be an absolute path')
            platforms.check_gstreamer_path(Path(self.frames_dir))
        if self.frame_source not in ('airplay', 'usb'):
            raise ValueError('frame_source must be airplay or usb')
        if isinstance(self.usb_pt_scale, bool) or not isinstance(self.usb_pt_scale, (int, float)) \
                or not math.isfinite(self.usb_pt_scale) or not 0 <= self.usb_pt_scale <= 10:
            raise ValueError('usb_pt_scale must be a finite number in 0..10')
        geometry.parse_screen_pt(self.screen_pt)
        geometry.sizes(self.device)
        get_detector(self.pointer_detector)

    @classmethod
    def load(cls, path=None):
        path = Path(path) if path else Path.home()/'.config/iphone-hand/daemon.toml'
        with path.open('rb') as source:
            data = tomllib.load(source)
        if 'token' not in data:
            raise ValueError('daemon token is required')
        return cls(**data)


def make_link(address, token=''):
    if address.startswith('serial:'):
        port = address[len('serial:'):]
        if not port.startswith('/'):
            raise ValueError('serial link needs an absolute device path')
        return SerialLink(port)
    parsed = urlsplit(address)
    if parsed.scheme == 'tcp' and parsed.hostname and parsed.port and not parsed.username and not parsed.password and parsed.path in ('', '/') and not parsed.query and not parsed.fragment:
        if not token:
            raise ValueError('TCP link requires link_token')
        # Section 7 is implemented separately; do not import or connect for serial users.
        cls = importlib.import_module('iphone_hand.tcp_link').TcpLink
        return cls(parsed.hostname, parsed.port, token)
    raise ValueError('link must be serial:/device or tcp://host:port')


def runtime_frames():
    return platforms.default_frames_dir(platforms.LINUX)


def create_app(config, *, phone=None, screen=None, elements=None, supervisor=None, receiver=None):
    from .daemon_http import create_app as http_app
    if screen is None:
        if config.frame_source == 'usb':
            if platforms.current_platform() != platforms.MACOS:
                raise ValueError('frame_source "usb" requires macOS (AVFoundation USB capture)')
            from .usb_capture import UsbCaptureScreen
            screen = UsbCaptureScreen(pt_scale=config.usb_pt_scale)
        else:
            frames = platforms.resolve_frames_dir(config.frames_dir)
            if receiver is None and supervisor is None:
                if config.receiver == 'child':
                    supervisor = UxPlaySupervisor(frames, name=config.uxplay_name,
                                                  allow_device_id=config.allow_device_id,
                                                  tcp_probe=platforms.tcp_probe())
                else:
                    receiver = ExternalUxPlayReceiver(**platforms.receiver_observers())
            observer = receiver if receiver is not None else supervisor
            screen = AirPlayScreen(frames, connected=observer.client_connected)
    phone = phone if phone is not None else Phone(make_link(config.link, config.link_token), screen,
                                                 home_action=config.home_action,
                                                 pointer_recover_key=config.pointer_recover_key,
                                                 screen_pt=config.screen_pt, device=config.device,
                                                 pointer_detector=config.pointer_detector)
    elements = elements if elements is not None else Elements(tap_offset=(config.vc_offset_x, config.vc_offset_y),
                                                              pointer_detector=config.pointer_detector)
    return http_app(config, phone, screen, elements, supervisor, receiver)


def main(argv=None):
    parser = argparse.ArgumentParser(prog='iphone-hand-daemon')
    parser.add_argument('--config', type=Path)
    args = parser.parse_args(argv)
    try:
        config = Config.load(args.config)
        app = create_app(config)
    except (OSError, ValueError, TypeError, ImportError):
        # Never echo a config or link token in startup diagnostics.
        parser.error('Cannot start: check daemon.toml, required tokens, runtime directory and link dependency')
    import uvicorn
    # Query tokens must not end up in access logs. Receiver stdout still reaches the journal.
    uvicorn.run(app, host=config.bind, port=config.port, access_log=False)


if __name__ == '__main__':
    main()
