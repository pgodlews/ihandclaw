"""Platform layer tests: all OS interactions are injected fakes; nothing touches launchctl/lsof/pgrep."""
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest

from iphone_hand import daemon, platforms
from iphone_hand.airplay import ExternalUxPlayReceiver, established_tcp
from iphone_hand.daemon import Config, create_app, runtime_frames
from iphone_hand.elements import Elements


def ok(stdout):
    return SimpleNamespace(returncode=0, stdout=stdout)


def test_current_platform_mapping(monkeypatch):
    for sysname, expected in [('linux', platforms.LINUX), ('linux2', platforms.LINUX),
                              ('darwin', platforms.MACOS), ('freebsd14', 'freebsd14')]:
        monkeypatch.setattr(platforms.sys, 'platform', sysname)
        assert platforms.current_platform() == expected


def test_default_frames_dir_linux_matches_legacy_runtime_frames(monkeypatch, tmp_path):
    monkeypatch.delenv('XDG_RUNTIME_DIR', raising=False)
    with pytest.raises(ValueError, match='XDG_RUNTIME_DIR'):
        platforms.default_frames_dir(platforms.LINUX, {})
    assert platforms.default_frames_dir(platforms.LINUX, {'XDG_RUNTIME_DIR': str(tmp_path)}) == \
        tmp_path/'iphone-hand/frames'
    # The legacy entry point is the same code with the same messages.
    with pytest.raises(ValueError, match='XDG_RUNTIME_DIR'):
        runtime_frames()
    monkeypatch.setenv('XDG_RUNTIME_DIR', str(tmp_path))
    assert runtime_frames() == tmp_path/'iphone-hand/frames'
    with pytest.raises(ValueError, match='GStreamer'):
        platforms.default_frames_dir(platforms.LINUX, {'XDG_RUNTIME_DIR': '/tmp/has space'})


def test_default_frames_dir_macos_uses_home_library_caches():
    path = platforms.default_frames_dir(platforms.MACOS, {'HOME': '/Users/test'})
    assert path == Path('/Users/test/Library/Caches/iphone-hand/frames')
    with pytest.raises(ValueError, match='HOME'):
        platforms.default_frames_dir(platforms.MACOS, {})
    with pytest.raises(ValueError, match='frames_dir'):
        platforms.default_frames_dir('plan9', {})


def test_resolve_frames_dir_explicit_override():
    assert platforms.resolve_frames_dir('/Volumes/scratch/frames') == Path('/Volumes/scratch/frames')
    with pytest.raises(ValueError, match='GStreamer'):
        platforms.resolve_frames_dir('/tmp/bang!dir')


@pytest.mark.parametrize('value', ['relative/path', '', 42, True, '/tmp/has space'])
def test_frames_dir_config_validation(value):
    with pytest.raises(ValueError, match='frames_dir|GStreamer'):
        Config(token='test-token', frames_dir=value)


def test_frames_dir_config_round_trip_and_unknown_keys(tmp_path):
    path = tmp_path/'daemon.toml'
    path.write_text('token = "test-token"\nframes_dir = "/Volumes/scratch/frames"\n')
    assert Config.load(path).frames_dir == '/Volumes/scratch/frames'
    assert Config(token='test-token').frames_dir is None
    path.write_text(path.read_text() + 'bogus_key = 1\n')
    with pytest.raises(TypeError):
        Config.load(path)


LAUNCHCTL_RUNNING = """local.iphone-hand.uxplay = {
\tactive count = 1
\tstate = running
\tprogram = /Users/test/.local/bin/uxplay
\tpid = 4242
\tlast exit code = 0
}
"""


def test_launchd_service_state_parsing():
    calls = []
    def runner(command, **kwargs):
        calls.append(command)
        assert kwargs['timeout'] == 0.5 and kwargs['capture_output']
        return ok(LAUNCHCTL_RUNNING)
    state = platforms.launchd_service_state(uid=501, runner=runner)
    assert calls[0] == ['launchctl', 'print', 'gui/501/local.iphone-hand.uxplay']
    assert state['LoadState'] == 'loaded' and state['ActiveState'] == 'active'
    assert state['MainPID'] == '4242' and state['NRestarts'] is None


@pytest.mark.parametrize('output, active, pid', [
    (LAUNCHCTL_RUNNING.replace('state = running', 'state = not running'), False, '4242'),
    (LAUNCHCTL_RUNNING.replace('pid = 4242', 'pid = 0'), True, '0'),
])
def test_launchd_service_state_not_running(output, active, pid):
    state = platforms.launchd_service_state(runner=lambda *a, **k: ok(output))
    assert state is not None
    assert (state['ActiveState'] == 'active') is active and state['MainPID'] == pid


def test_launchd_service_state_unavailable():
    assert platforms.launchd_service_state(
        runner=lambda *a, **k: SimpleNamespace(returncode=113, stdout='Could not find service')) is None
    for error in (FileNotFoundError(), subprocess.TimeoutExpired('launchctl', 0.5)):
        def fail(*args, **kwargs):
            raise error
        assert platforms.launchd_service_state(runner=fail) is None


def test_find_pid_by_name():
    def runner(command, **kwargs):
        assert command[:3] == ['pgrep', '-x', '-u']
        if command[-1] == 'uxplay.bin':
            return SimpleNamespace(returncode=1, stdout='')
        return ok('4242\nextra\n')
    assert platforms.find_pid_by_name(uid=501, runner=runner) == 4242
    missing = lambda *a, **k: SimpleNamespace(returncode=1, stdout='')
    assert platforms.find_pid_by_name(uid=501, runner=missing) is None
    broken = lambda *a, **k: SimpleNamespace(returncode=2, stdout='')
    assert platforms.find_pid_by_name(uid=501, runner=broken) is None
    def fail(*args, **kwargs):
        raise FileNotFoundError
    assert platforms.find_pid_by_name(uid=501, runner=fail) is None


def test_established_tcp_lsof():
    listing = 'COMMAND PID USER FD TYPE NAME\nuxplay 42 me 9u TCP 10.0.0.1:7000->10.0.0.2:5000 (ESTABLISHED)\n'
    assert platforms.established_tcp_lsof(42, runner=lambda *a, **k: ok(listing)) is True
    assert platforms.established_tcp_lsof(42, runner=lambda *a, **k: ok('COMMAND PID\n')) is False
    assert platforms.established_tcp_lsof(42, runner=lambda *a, **k: SimpleNamespace(returncode=1, stdout='')) is False
    assert platforms.established_tcp_lsof(42, runner=lambda *a, **k: SimpleNamespace(returncode=2, stdout='')) is None
    def fail(*args, **kwargs):
        raise subprocess.TimeoutExpired('lsof', 1.0)
    assert platforms.established_tcp_lsof(42, runner=fail) is None


def test_probe_and_observer_dispatch(monkeypatch):
    monkeypatch.setattr(platforms.sys, 'platform', 'linux')
    assert platforms.receiver_observers() == {}
    assert platforms.tcp_probe() is established_tcp
    monkeypatch.setattr(platforms.sys, 'platform', 'darwin')
    observers = platforms.receiver_observers()
    assert observers['service'] == platforms.LAUNCHD_UXPLAY_LABEL
    assert observers['unit_lookup'] is platforms.launchd_service_state
    assert observers['pid_lookup'] is platforms.find_pid_by_name
    assert observers['tcp_probe'] is platforms.established_tcp_lsof
    assert platforms.tcp_probe() is platforms.established_tcp_lsof


def test_macos_external_receiver_status_shape():
    observer = ExternalUxPlayReceiver(
        unit_lookup=lambda: {'LoadState': 'loaded', 'ActiveState': 'active',
                             'MainPID': '4242', 'NRestarts': None},
        tcp_probe=lambda pid: True, service=platforms.LAUNCHD_UXPLAY_LABEL)
    state = observer.status()
    assert state['unit'] == 'local.iphone-hand.uxplay'
    assert state['running'] and state['active'] and state['pid'] == 4242
    assert state['restarts'] is None
    assert observer.client_connected()


def test_create_app_macos_frames_and_receiver(tmp_path, monkeypatch, rig):
    phone, _, _ = rig
    monkeypatch.setattr(platforms.sys, 'platform', 'darwin')
    monkeypatch.setenv('HOME', str(tmp_path))
    made = {}
    class Receiver:
        def __init__(self, **kwargs):
            made.update(kwargs)
        def client_connected(self):
            return False
        def status(self):
            return {'mode': 'external'}
    monkeypatch.setattr(daemon, 'ExternalUxPlayReceiver', Receiver)
    config = Config(token='test-token')
    app = create_app(config, phone=phone, elements=Elements(reader=None))
    assert made['service'] == platforms.LAUNCHD_UXPLAY_LABEL
    assert app.state.controller.screen.frames == tmp_path/'Library/Caches/iphone-hand/frames'
    override = Config(token='test-token', frames_dir=str(tmp_path/'elsewhere'))
    app = create_app(override, phone=phone, elements=Elements(reader=None))
    assert app.state.controller.screen.frames == tmp_path/'elsewhere'


def test_create_app_macos_child_mode_uses_lsof_probe(tmp_path, monkeypatch, rig):
    phone, _, _ = rig
    monkeypatch.setattr(platforms.sys, 'platform', 'darwin')
    monkeypatch.setenv('HOME', str(tmp_path))
    made = {}
    class Child:
        def __init__(self, frames, **kwargs):
            made.update(kwargs)
        async def start(self): pass
        async def stop(self): pass
        def client_connected(self): return False
        def status(self): return {'running': False}
    monkeypatch.setattr(daemon, 'UxPlaySupervisor', Child)
    config = Config(token='test-token', receiver='child')
    create_app(config, phone=phone, elements=Elements(reader=None))
    assert made['tcp_probe'] is platforms.established_tcp_lsof


def test_linux_create_app_is_unchanged(tmp_path, monkeypatch, rig):
    phone, _, _ = rig
    monkeypatch.setattr(platforms.sys, 'platform', 'linux')
    monkeypatch.setenv('XDG_RUNTIME_DIR', str(tmp_path))
    captured = {}
    Original = ExternalUxPlayReceiver
    def factory(**kwargs):
        captured.update(kwargs)
        return Original(**kwargs)
    monkeypatch.setattr(daemon, 'ExternalUxPlayReceiver', factory)
    config = Config(token='test-token')
    app = create_app(config, phone=phone, elements=Elements(reader=None))
    assert captured == {}  # No platform kwargs: Linux defaults are byte-for-byte identical.
    assert app.state.controller.screen.frames == tmp_path/'iphone-hand/frames'
    assert app.state.controller.receiver.service == 'iphone-hand-uxplay.service'
