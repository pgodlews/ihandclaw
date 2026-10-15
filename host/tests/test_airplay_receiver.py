import os
from pathlib import Path
import shlex
import subprocess
from types import SimpleNamespace

from fastapi.testclient import TestClient
import pytest

from iphone_hand import airplay, daemon
from iphone_hand.airplay import ExternalUxPlayReceiver, UxPlaySupervisor
from iphone_hand.daemon import Config, create_app
from iphone_hand.elements import Elements


ROOT = Path(__file__).resolve().parents[2]


def unit(pid='42', active='active', restarts='3'):
    return {'LoadState': 'loaded', 'MainPID': pid, 'ActiveState': active, 'NRestarts': restarts}


@pytest.mark.parametrize('value', ['invalid', '', None, True, [], {}])
def test_receiver_config_rejects_unknown_modes(value):
    with pytest.raises(ValueError, match='receiver'):
        Config(token='test-token', receiver=value)


def test_receiver_config_loads_modes(tmp_path):
    assert Config(token='test-token').receiver == 'external'
    path = tmp_path/'daemon.toml'
    for mode in ('external', 'child'):
        path.write_text(f'token = "test-token"\nreceiver = "{mode}"\n')
        assert Config.load(path).receiver == mode


def test_external_unit_state_and_pid_changes():
    current = unit()
    probes = []
    def fallback():
        pytest.fail('Loaded unit must not fall back to unrelated processes')
    observer = ExternalUxPlayReceiver(unit_lookup=lambda: current, pid_lookup=fallback,
                                     tcp_probe=lambda pid: probes.append(pid) or True, cache_for=0)
    assert observer.client_connected()
    state = observer.status()
    assert state['active'] and state['running'] and state['restarts'] == 3
    assert state['pid'] == 42 and state['active_state'] == 'active'
    current.update(unit('84', restarts='4'))
    assert observer.client_connected() and probes == [42, 84]
    assert observer.status()['restarts'] == 4
    current.update(unit('0', 'inactive'))
    assert not observer.client_connected()
    assert not observer.status()['active'] and not observer.status()['running']
    assert probes == [42, 84]


def test_external_proc_fallback_and_unknown_socket_status():
    observer = ExternalUxPlayReceiver(unit_lookup=lambda: None, pid_lookup=lambda: 57,
                                     tcp_probe=lambda pid: None if pid == 57 else False)
    assert observer.status()['running']
    assert observer.status()['active'] is None and observer.status()['restarts'] is None
    assert observer.client_connected() is None
    missing = ExternalUxPlayReceiver(unit_lookup=lambda: None, pid_lookup=lambda: None,
                                    tcp_probe=lambda pid: pytest.fail('No process to probe'))
    assert not missing.client_connected() and not missing.status()['running']


def test_external_observation_cache():
    now = [0.0]
    calls = []
    observer = ExternalUxPlayReceiver(unit_lookup=lambda: calls.append(1) or unit(),
                                     tcp_probe=lambda pid: True, clock=lambda: now[0])
    observer.status()
    observer.client_connected()
    assert len(calls) == 1
    now[0] = 0.5
    observer.status()
    assert len(calls) == 2


def test_systemctl_query_is_read_only_and_handles_unavailability(monkeypatch):
    calls = []
    def run(command, **kwargs):
        calls.append(command)
        assert kwargs['timeout'] == 0.5 and kwargs['capture_output']
        return SimpleNamespace(returncode=0, stdout='LoadState=loaded\nMainPID=42\nActiveState=active\nNRestarts=7\n')
    monkeypatch.setattr(airplay.subprocess, 'run', run)
    assert airplay.uxplay_unit_state() == unit(restarts='7')
    assert calls[0][:4] == ['systemctl', '--user', 'show', airplay.UXPLAY_UNIT]
    for output, code in [('LoadState=not-found\n', 0), ('', 1)]:
        monkeypatch.setattr(airplay.subprocess, 'run', lambda *a, **k: SimpleNamespace(returncode=code, stdout=output))
        assert airplay.uxplay_unit_state() is None
    for error in (FileNotFoundError(), subprocess.TimeoutExpired('systemctl', 0.5)):
        def fail(*args, **kwargs):
            raise error
        monkeypatch.setattr(airplay.subprocess, 'run', fail)
        assert airplay.uxplay_unit_state() is None


def test_proc_lookup_matches_binary_and_user(tmp_path):
    for pid, executable in [('10', '/local/bin/uxplay'), ('20', '/local/bin/uxplay.bin'),
                            ('30', '/local/bin/uxplay.bin (deleted)')]:
        path = tmp_path/pid
        path.mkdir()
        (path/'exe').symlink_to(executable)
    (tmp_path/'self').mkdir()
    assert airplay.find_uxplay_pid(tmp_path) == 20
    assert airplay.find_uxplay_pid(tmp_path, uid=os.getuid()+1) is None
    (tmp_path/'20/exe').unlink()
    assert airplay.find_uxplay_pid(tmp_path) == 30
    assert airplay.find_uxplay_pid(tmp_path/'missing') is None


def test_established_socket_belongs_to_receiver(tmp_path):
    process = tmp_path/'42'
    (process/'fd').mkdir(parents=True)
    (process/'net').mkdir()
    (process/'fd/3').symlink_to('socket:[123]')
    (process/'net/tcp6').write_text('header\n')
    table = process/'net/tcp'
    table.write_text('header\n0: local remote 01 tx rx tr uid timeout 999\n')
    assert airplay.established_tcp(42, proc=tmp_path) is False
    table.write_text('header\n0: local remote 01 tx rx tr uid timeout 123\n')
    assert airplay.established_tcp(42, proc=tmp_path) is True
    assert airplay.established_tcp(43, proc=tmp_path) is None


def test_external_app_restart_does_not_manage_receiver(tmp_path, monkeypatch, rig):
    phone, _, _ = rig
    monkeypatch.setattr(daemon.platforms, 'current_platform', lambda: daemon.platforms.LINUX)
    frames = tmp_path/'iphone-hand/frames'
    frames.mkdir(parents=True)
    frame = frames/'f00000.jpg'
    frame.write_bytes((ROOT/'fixtures/airplay/home-voicecontrol-pointer.jpg').read_bytes())
    os.utime(frame, (1000, 1000))  # Old frame remains usable with an established client socket.
    observer = ExternalUxPlayReceiver(unit_lookup=lambda: unit(), pid_lookup=lambda: 42,
                                     tcp_probe=lambda pid: pid == 42)
    def forbidden(*args, **kwargs):
        pytest.fail('External mode must not instantiate or manage a child supervisor')
    monkeypatch.setattr(daemon, 'UxPlaySupervisor', forbidden)
    monkeypatch.setattr(daemon, 'ExternalUxPlayReceiver', lambda: observer)
    monkeypatch.setenv('XDG_RUNTIME_DIR', str(tmp_path))
    monkeypatch.setattr(phone, 'close', lambda: None)
    config = Config(token='test-token', uxplay_name='Custom Receiver')
    for _ in range(2):
        app = create_app(config, phone=phone, elements=Elements(reader=None))
        with TestClient(app, base_url='http://iphone-hand.local') as client:
            state = client.get('/api/state', headers={'Authorization': 'Bearer test-token'}).json()
            assert state['receiver']['active'] and state['receiver']['restarts'] == 3
            assert state['airplay']['mirroring']
            assert state['airplay']['receiver_name'] == 'Custom Receiver'
            assert client.get('/api/frame.jpg', headers={'Authorization': 'Bearer test-token'}).status_code == 200
        assert frame.exists() and observer.client_connected()


def test_child_mode_app_keeps_supervisor_lifecycle(tmp_path, monkeypatch, rig):
    phone, _, _ = rig
    monkeypatch.setattr(daemon.platforms, 'current_platform', lambda: daemon.platforms.LINUX)
    calls = []
    class Child:
        def __init__(self, frames, **kwargs):
            calls.append(('init', frames, kwargs))
        async def start(self): calls.append('start')
        async def stop(self): calls.append('stop')
        def client_connected(self): return False
        def status(self): return {'running': False, 'restarts': 0}
    monkeypatch.setattr(daemon, 'UxPlaySupervisor', Child)
    monkeypatch.setenv('XDG_RUNTIME_DIR', str(tmp_path))
    config = Config(token='test-token', receiver='child', uxplay_name='Dev Receiver', allow_device_id='private-test-id')
    app = create_app(config, phone=phone, elements=Elements(reader=None))
    with TestClient(app):
        assert calls[-1] == 'start'
    assert calls[-1] == 'stop'
    assert calls[0][2]['name'] == 'Dev Receiver'
    assert calls[0][2]['allow_device_id'] == 'private-test-id'
    assert calls[0][2]['tcp_probe'] is airplay.established_tcp


def test_deployed_command_and_dependency_contract():
    receiver_unit = (ROOT/'deploy/iphone-hand-uxplay.service').read_text()
    daemon_unit = (ROOT/'deploy/iphone-hand.service').read_text()
    command = next(line.split('=', 1)[1] for line in receiver_unit.splitlines() if line.startswith('ExecStart='))
    # Read: expand the systemd specifiers to compare every argv item with the child command.
    command = command.replace('%%', '%').replace('%h', '/home/test').replace('%t', '/run/user/test')
    child = UxPlaySupervisor('/run/user/test/iphone-hand/frames', executable='/home/test/.local/bin/uxplay',
                             key_file='/home/test/.config/iphone-hand/uxplay.pem')
    assert shlex.split(command) == child.command()
    assert 'Restart=always' in receiver_unit and 'SyslogIdentifier=iphone-hand-uxplay' in receiver_unit
    for dependency in ('Wants', 'After'):
        assert any(line.startswith(dependency+'=') and airplay.UXPLAY_UNIT in line.split()
                   for line in daemon_unit.splitlines())
    for text in (receiver_unit, daemon_unit):
        assert 'PartOf=' not in text and 'BindsTo=' not in text
