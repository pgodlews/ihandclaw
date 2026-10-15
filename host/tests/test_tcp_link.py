import socket
import threading

import pytest

from iphone_hand.fake import FakeSerialDevice
from iphone_hand.serial_link import ProtocolError
from iphone_hand.tcp_link import TcpLink


class FakeTcpDevice:
    """Local sockets around the existing fake protocol implementation."""

    def __init__(self, *, fragment=256, drop=(), silent=(), bad_auth=False, memory=False):
        self.token = "a" * 32
        self.device = FakeSerialDevice()
        self.fragment = fragment
        self.drop = list(drop)
        self.device.drop_commands.update(silent)
        self.bad_auth = bad_auth
        self.connections = self.auths = 0
        self.errors = []
        self.stop = threading.Event()
        if memory:
            self.listener = self.thread = None
            self.port = 30000
            return
        self.listener = socket.socket()
        try:
            self.listener.bind(("localhost", 0))
        except PermissionError:
            # Restricted sandboxes forbid bind, even on loopback. Exercise the
            # same _SocketTransport and protocol with an in-memory socket.
            self.listener.close()
            self.listener = self.thread = None
            self.port = 30000
            return
        self.port = self.listener.getsockname()[1]
        self.listener.listen()
        self.listener.settimeout(0.05)
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()

    def run(self):
        try:
            while not self.stop.is_set():
                try:
                    sock, _ = self.listener.accept()
                except socket.timeout:
                    continue
                with sock:
                    self.connections += 1
                    sock.settimeout(0.05)
                    buffer = bytearray()
                    authenticated = False
                    while not self.stop.is_set():
                        try:
                            data = sock.recv(256)
                        except socket.timeout:
                            continue
                        if not data:
                            break
                        buffer.extend(data)
                        dropped = False
                        while b"\n" in buffer:
                            line, _, rest = buffer.partition(b"\n")
                            buffer = bytearray(rest)
                            if not authenticated:
                                self.auths += 1
                                if line != f"AUTH {self.token}".encode():
                                    dropped = True
                                    break
                                authenticated = True
                                response = b"@0 ERR ARG\n" if self.bad_auth else b"@0 OK auth\n@0 EVT ready\n"
                            else:
                                self.device.write(bytes(line) + b"\n")
                                command = line.split()[1].decode()
                                if self.drop and self.drop[0] == command:
                                    self.drop.pop(0)
                                    self.device._output.clear()
                                    dropped = True
                                    break
                                if command in {"WIFI", "TOKEN"}:
                                    self.device._output.clear()
                                    response = b"@" + line.split()[0] + b" ERR ARG\n"
                                else:
                                    response = self.device.read(65536)
                            for i in range(0, len(response), self.fragment):
                                sock.sendall(response[i:i + self.fragment])
                        if dropped:
                            break
        except (BrokenPipeError, ConnectionResetError):
            pass  # Test teardown or authentication refusal can close the socket.
        except BaseException as error:
            self.errors.append(error)

    def close(self):
        if self.listener is None:
            assert not self.errors
            return
        self.stop.set()
        self.thread.join(2)
        self.listener.close()
        assert not self.thread.is_alive()
        assert not self.errors


class MemorySocket:
    """Socket API double; the server fixture never connects to a network host."""

    def __init__(self, server):
        self.server = server
        self.input = bytearray()
        self.output = bytearray()
        self.authenticated = self.closed = False
        server.connections += 1

    def settimeout(self, timeout):
        pass

    def setsockopt(self, *args):
        pass

    def sendall(self, data):
        assert self.send(data) == len(data)

    def send(self, data):
        if self.closed:
            raise BrokenPipeError
        self.input.extend(data)
        while b"\n" in self.input:
            line, _, rest = self.input.partition(b"\n")
            self.input = bytearray(rest)
            device = self.server
            if not self.authenticated:
                device.auths += 1
                if line != f"AUTH {device.token}".encode():
                    self.closed = True
                    break
                self.authenticated = True
                response = b"@0 ERR ARG\n" if device.bad_auth else b"@0 OK auth\n@0 EVT ready\n"
            else:
                device.device.write(bytes(line) + b"\n")
                command = line.split()[1].decode()
                if device.drop and device.drop[0] == command:
                    device.drop.pop(0)
                    device.device._output.clear()
                    self.closed = True
                    break
                if command in {"WIFI", "TOKEN"}:
                    device.device._output.clear()
                    response = b"@" + line.split()[0] + b" ERR ARG\n"
                else:
                    response = device.device.read(65536)
            self.output.extend(response)
        return len(data)

    def recv(self, size):
        if self.output:
            n = min(size, self.server.fragment, len(self.output))
            data = bytes(self.output[:n])
            del self.output[:n]
            return data
        if self.closed:
            return b""
        raise socket.timeout

    def close(self):
        self.closed = True


@pytest.fixture
def server(monkeypatch):
    devices = []
    def connect(address, timeout=None):
        for device in devices:
            if device.listener is None and device.port == address[1]:
                return MemorySocket(device)
        pytest.fail('Unexpected TCP destination')
    monkeypatch.setattr(socket, "create_connection", connect)
    def create(**kwargs):
        device = FakeTcpDevice(memory=True, **kwargs)
        devices.append(device)
        return device
    yield create
    for device in devices:
        device.close()


def link_for(device, **kwargs):
    return TcpLink("localhost", device.port, device.token, **kwargs)


@pytest.fixture
def tcp_clock(monkeypatch):
    from types import SimpleNamespace
    from iphone_hand import serial_link, tcp_link
    class Clock:
        now = 0.0
        def monotonic(self): return self.now
        def sleep(self, seconds): self.now += seconds
    clock = Clock()
    fake_time = SimpleNamespace(monotonic=clock.monotonic, sleep=clock.sleep)
    monkeypatch.setattr(serial_link, 'time', fake_time)
    monkeypatch.setattr(tcp_link, 'time', fake_time)
    return clock


def test_half_open_socket_recovers_on_next_poll(server, tcp_clock, monkeypatch):
    device = server()
    link = link_for(device)
    assert link.command('STATUS').fields['ready'] == '1'
    stale = link.transport.sock
    # Read: a reboot can leave a locally writable socket that never answers.
    monkeypatch.setattr(stale, 'send', lambda data: len(data))
    try:
        with pytest.raises(TimeoutError):
            link.command('STATUS')
        tcp_clock.sleep(2)
        assert link.command('STATUS').fields['ready'] == '1'
        assert device.connections == 2
        assert stale.closed
    finally:
        link.close()


@pytest.mark.parametrize('failure', [TimeoutError, ConnectionRefusedError])
def test_failed_reconnect_is_retried_after_bounded_backoff(server, tcp_clock, monkeypatch, failure):
    from iphone_hand.link_errors import ReconnectDeferred, link_error_class
    device = server()
    link = link_for(device)
    link.command('STATUS')
    link.transport.sock.close()
    healthy_connect = socket.create_connection
    attempts = []
    def unavailable(address, timeout):
        attempts.append(tcp_clock.now)
        if failure is TimeoutError:
            tcp_clock.sleep(timeout)
        raise failure('private diagnostic')
    monkeypatch.setattr(socket, 'create_connection', unavailable)
    try:
        for delay in (.25, .5, 1, 2, 2):
            started = tcp_clock.now
            with pytest.raises(failure):
                link.command('STATUS')
            assert tcp_clock.now - started <= 2.001
            count = len(attempts)
            for _ in range(3):
                with pytest.raises(ReconnectDeferred) as error:
                    link.command('STATUS')
                assert link_error_class(error.value) == ('timeout' if failure is TimeoutError else 'refused')
            assert len(attempts) == count
            assert link.transport is None
            tcp_clock.sleep(delay)
        monkeypatch.setattr(socket, 'create_connection', healthy_connect)
        assert link.command('STATUS').fields['ready'] == '1'
        # Successful STATUS resets the exponential backoff.
        monkeypatch.setattr(socket, 'create_connection', unavailable)
        link.close()
        with pytest.raises(failure):
            link.command('STATUS')
        tcp_clock.sleep(.25)
        monkeypatch.setattr(socket, 'create_connection', healthy_connect)
        assert link.command('STATUS').fields['ready'] == '1'
    finally:
        link.close()


def test_status_budget_includes_connect_auth_and_reply(server, tcp_clock, monkeypatch):
    device = server(silent=('STATUS',))
    link = link_for(device)
    original_connect = socket.create_connection
    def slow_connect(address, timeout):
        tcp_clock.sleep(.8)
        sock = original_connect(address, timeout)
        original_sendall = sock.sendall
        def slow_auth(data):
            tcp_clock.sleep(.8)
            original_sendall(data)
        sock.sendall = slow_auth
        return sock
    monkeypatch.setattr(socket, 'create_connection', slow_connect)
    try:
        with pytest.raises(TimeoutError):
            link.command('STATUS')
        assert tcp_clock.now == pytest.approx(2, abs=.001)
        assert link.transport is None
    finally:
        link.close()


def test_status_retry_uses_remaining_budget(server, tcp_clock, monkeypatch):
    device = server()
    link = link_for(device)
    link.command('STATUS')
    def delayed_drop(size):
        tcp_clock.sleep(1.5)
        return b''
    monkeypatch.setattr(link.transport.sock, 'recv', delayed_drop)
    budgets = []
    def connect_timeout(address, timeout):
        budgets.append(timeout)
        tcp_clock.sleep(timeout)
        raise TimeoutError
    monkeypatch.setattr(socket, 'create_connection', connect_timeout)
    try:
        with pytest.raises(TimeoutError):
            link.command('STATUS')
        assert budgets == [pytest.approx(.5)]
        assert tcp_clock.now == pytest.approx(2)
        assert link.transport is None
    finally:
        link.close()


@pytest.mark.parametrize('stage', ['auth', 'write', 'read'])
def test_timeout_at_each_stage_closes_socket_and_recovers(server, tcp_clock, monkeypatch, stage):
    device = server()
    link = link_for(device)
    sockets = []
    original_connect = socket.create_connection
    def connect(address, timeout):
        sock = original_connect(address, timeout)
        sockets.append(sock)
        if len(sockets) == 1:
            if stage == 'auth':
                sock.sendall = lambda data: None
            else:
                send = sock.send
                def silent(data):
                    if not sock.authenticated:
                        return send(data)
                    if stage == 'write':
                        raise TimeoutError
                    return len(data)
                sock.send = silent
        return sock
    monkeypatch.setattr(socket, 'create_connection', connect)
    try:
        with pytest.raises(TimeoutError):
            link.command('STATUS')
        assert sockets[0].closed and link.transport is None
        tcp_clock.sleep(2)
        assert link.command('STATUS').fields['ready'] == '1'
        assert len(sockets) == 2
    finally:
        link.close()


@pytest.mark.parametrize('failure', [TimeoutError, ConnectionResetError])
def test_action_send_error_never_replays_even_without_returned_byte_count(server, monkeypatch, failure):
    device = server()
    link = link_for(device)
    link.open()
    original_send = link.transport.sock.send
    def lost_ack(data):
        original_send(data)
        raise failure
    monkeypatch.setattr(link.transport.sock, 'send', lost_ack)
    try:
        with pytest.raises((TimeoutError, ConnectionError), match='outcome is unknown'):
            link.command('MOVE', 10, 5)
        assert [c[1] for c in device.device.commands] == ['MOVE']
        assert link.transport is None
        assert link.command('STATUS').fields['ready'] == '1'
        assert [c[1] for c in device.device.commands] == ['MOVE', 'STATUS']
    finally:
        link.close()


def test_auth_fragmentation_events_ids_and_pacing(server):
    device = server(fragment=3)
    link = link_for(device)
    try:
        link.open()
        link.open()
        assert device.auths == 1
        assert link.command("PING").fields["fw"] == "fake"
        assert link.command("STATUS").id == 2
        assert {e["event"] for e in link.events} == {"ready", "boot"}
        assert link.command("CFG", "step=5", "interval=20").fields == {"step": "5", "interval": "20"}
        assert (link.step, link.interval) == (5, 20)
        assert link._budget("MOVE", (2000, 0)) >= 12.5
        link.command("MOVE", 12, -7)
        link._id = 999999
        assert link.command("PING").id == 1
    finally:
        link.close()


def test_reconnect_once_and_authenticate_again(server):
    device = server(drop=("PING",))
    link = link_for(device)
    try:
        assert link.command("PING").fields["fw"] == "fake"
        assert device.connections == device.auths == 2
    finally:
        link.close()


def test_second_drop_propagates(server, tcp_clock):
    device = server(drop=("PING", "PING"))
    link = link_for(device)
    try:
        with pytest.raises(ConnectionError):
            link.command("PING")
        assert device.connections == 2
        assert link.transport is None
        tcp_clock.sleep(2)
        assert link.command('STATUS').fields['ready'] == '1'
        assert device.connections == 3
    finally:
        link.close()


def test_action_is_not_replayed_after_lost_ack(server):
    device = server(drop=("MOVE",))
    link = link_for(device)
    try:
        with pytest.raises(ConnectionError, match="outcome is unknown"):
            link.command("MOVE", 10, 5)
        assert [c[1] for c in device.device.commands] == ["MOVE"]
        assert device.connections == 1
        assert link.command("PING").fields["fw"] == "fake"
        assert device.connections == 2
    finally:
        link.close()


def test_timeout_no_retry_late_reply_and_protocol_errors(server):
    device = server(silent=("MOVE",))
    link = link_for(device, timeout=0.1)
    try:
        with pytest.raises(TimeoutError):
            link.command("MOVE", 10, 5, timeout=0.03)
        device.device.queue(b"@1 OK late=1\n@0 EVT disc reason=9\n")
        assert link.command("PING").id == 2
        assert link.events[-1] == {"event": "disc", "reason": "9"}
        assert device.connections == 2
        for command, args in [("BOGUS", ()), ("WIFI", ("off",)), ("TOKEN", (device.token,))]:
            with pytest.raises(ProtocolError):
                link.command(command, *args)
        for command, args in [("ping", ()), ("NAME", ("a\nb",)), ("NAME", ("é" * 128,))]:
            with pytest.raises(ValueError):
                link.command(command, *args)
    finally:
        link.close()


def test_auth_rejection_and_invalid_settings(server):
    device = server(bad_auth=True)
    link = link_for(device)
    with pytest.raises(ProtocolError, match="AUTH"):
        link.open()
    assert link.transport is None
    wrong = TcpLink("localhost", device.port, "b" * 32)
    with pytest.raises(ConnectionError):
        wrong.open()
    for token in ["", "x" * 32, "a" * 31, "a" * 65, "a" * 32 + "\n"]:
        with pytest.raises(ValueError):
            TcpLink("localhost", device.port, token)
    for timeout in [0, -1, float("inf"), float("nan")]:
        with pytest.raises(ValueError):
            link_for(device, timeout=timeout)


def test_hardening_commands_fields_and_errors_over_tcp(server):
    device = server(fragment=2)
    link = link_for(device)
    try:
        device.device.out_drops = 17
        status = link.command("STATUS").fields
        assert status["out_drops"] == "17" and int(status["uptime_ms"]) >= 0
        assert status["reset"] == "poweron" and status["reset_code"] == "1"
        link.command("BTN", 1)
        link.command("KREPORT", 8, 4)
        link.command("STOP")
        assert device.device.buttons == 0 and device.device.keyboard == (0, 0)
        for command, args in [("STOP", (1,)), ("PAIR", (120,)),
                              ("SCROLL", (-2147483648,)), ("MOVE", (2001, 0))]:
            with pytest.raises(ProtocolError, match="ARG"):
                link.command(command, *args)
        with pytest.raises(ProtocolError, match="TIMEOUT"):
            link.command("KEY", 8, 4, 65535)
        link.command("PAIR")
        assert link.command("STATUS").fields["pairing"] == "1"
    finally:
        link.close()


def test_pipeline_tcp_drop_never_replays(server, monkeypatch):
    device = server()
    link = link_for(device)
    link.open()
    original = link.transport.sock.send
    count = 0
    def drop_third(data):
        nonlocal count
        count += 1
        if count == 3: device.drop.append('KEY')
        return original(data)
    monkeypatch.setattr(link.transport.sock, 'send', drop_third)
    with pytest.raises(ConnectionError) as caught: link.pipeline([('KEY', 0, 4)] * 10, window=2)
    assert caught.value.confirmed == 2 and caught.value.sent == 4
    assert device.connections == device.auths == 1
    assert len(device.device.commands) == 3 and link.transport is None


def test_pipeline_tcp_timeout_never_replays(server, tcp_clock):
    device = server(silent=('KEY',))
    link = link_for(device)
    with pytest.raises(TimeoutError) as caught:
        link.pipeline([('KEY', 0, 4)] * 10, window=3, timeout_per_command=.01, drain_timeout=.02)
    assert caught.value.confirmed == 0 and caught.value.sent == 3
    assert len(device.device.commands) == 3 and device.connections == 1
    assert tcp_clock.now == pytest.approx(.03) and link.transport is None
