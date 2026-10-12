"""Synthetic protocol checks; no hardware timing claims."""
from types import SimpleNamespace
import pytest
from iphone_hand import serial_link
from iphone_hand.fake import FakeSerialDevice
from iphone_hand.serial_link import PipelineError, ProtocolError, SerialLink

@pytest.fixture
def clock(monkeypatch):
    class Clock:
        now = 0.0
        def monotonic(self): return self.now
        def sleep(self, seconds): self.now += seconds
    clock = Clock()
    monkeypatch.setattr(serial_link, 'time', SimpleNamespace(monotonic=clock.monotonic, sleep=clock.sleep))
    return clock

class TrackingDevice(FakeSerialDevice):
    def __init__(self, *, busy_at=None, silent_at=None, max_read=256):
        super().__init__(max_read=max_read)
        self._output.clear()
        self.busy_at, self.silent_at = busy_at, silent_at
        self.outstanding = set()
        self.peak = self.reads = 0
        self.consumed = bytearray()
    def _handle(self, line):
        rid = int(line.split()[0])
        self.outstanding.add(rid)
        self.peak = max(self.peak, len(self.outstanding))
        if rid in (self.busy_at, self.silent_at):
            words = line.decode().split()
            self.commands.append((rid, words[1], tuple(words[2:])))
            if rid == self.busy_at: self.queue(f'@{rid} ERR BUSY\n'.encode())
        else: super()._handle(line)
    def read(self, size=1):
        self.reads += 1
        data = super().read(size)
        self.consumed.extend(data)
        while b'\n' in self.consumed:
            line, _, rest = self.consumed.partition(b'\n')
            self.consumed = bytearray(rest)
            if line.startswith(b'@'): self.outstanding.discard(int(line.split()[0][1:]))
        return data

def commands(n): return [('KEY', 0, 4 + i % 26) for i in range(n)]

@pytest.mark.parametrize('window', [1, 3, 8, 12])
@pytest.mark.parametrize('fragment', [3, 256])
def test_order_window_events_wrap(window, fragment):
    device = TrackingDevice(max_read=fragment)
    device.queue(b'@0 EVT ready\n@777 OK unrelated=1\n')
    link = SerialLink(transport=device)
    link._id = 999995
    replies = link.pipeline(commands(40), window=window)
    expected = [(999995 + i) % 999999 + 1 for i in range(40)]
    assert [r.id for r in replies] == expected
    assert [c[0] for c in device.commands] == expected
    assert [c[2] for c in device.commands] == [tuple(map(str, c[1:])) for c in commands(40)]
    assert device.peak <= window and not device.outstanding
    assert link.events[-1] == {'event': 'ready'}

def test_busy_stops_refills_drains_successes():
    device = TrackingDevice(busy_at=3, max_read=256)
    link = SerialLink(transport=device)
    with pytest.raises(ProtocolError) as caught: link.pipeline(commands(20), window=8)
    error = caught.value
    assert isinstance(error, PipelineError) and error.code == 'BUSY'
    assert error.sent == 8 and error.confirmed == 7
    assert error.confirmed_indices == (0, 1, 3, 4, 5, 6, 7)
    assert len(device.commands) == 8 and not device.outstanding
    assert link.command('PING').id == 9

def test_timeout_stops_refills_bounded_drain(clock):
    device = TrackingDevice(silent_at=3)
    link = SerialLink(transport=device)
    with pytest.raises(TimeoutError) as caught:
        link.pipeline(commands(20), window=4, timeout_per_command=.01, drain_timeout=.02)
    error = caught.value
    assert isinstance(error, PipelineError)
    assert error.sent == 6 and error.confirmed == 5
    assert error.confirmed_indices == (0, 1, 3, 4, 5)
    assert len(device.commands) == 6 and clock.now == pytest.approx(.03)
    assert device.closed and link.transport is None

def test_late_ack_counted_during_drain(clock):
    class LateDevice(TrackingDevice):
        def read(self, size=1):
            if clock.now >= .015 and self.silent_at:
                self.queue(b'@2 OK\n')
                self.silent_at = None
            return super().read(size)
    device = LateDevice(silent_at=2)
    with pytest.raises(TimeoutError) as caught:
        SerialLink(transport=device).pipeline(commands(10), window=2, timeout_per_command=.01, drain_timeout=.02)
    assert caught.value.sent == caught.value.confirmed == 3
    assert clock.now < .03 and not device.outstanding

def test_partial_writes_validation():
    class PartialDevice(TrackingDevice):
        def write(self, data): return super().write(data[:2])
    device = PartialDevice()
    link = SerialLink(transport=device)
    assert len(link.pipeline(commands(10))) == 10
    before = list(device.commands)
    for batch in [[('KEY', 0, 4), ('bad',)], [('KEY', 0, 4), ('NAME', 'bad\narg')], [('NAME', 'x' * 256)]]:
        with pytest.raises(ValueError): link.pipeline(batch)
    assert device.commands == before
    for window in [0, 13, True, 1.5]:
        with pytest.raises(ValueError): link.pipeline(commands(1), window=window)
    for budget in [0, -1, float('nan'), float('inf')]:
        with pytest.raises(ValueError): link.pipeline(commands(1), timeout_per_command=budget)
        with pytest.raises(ValueError): link.pipeline(commands(1), drain_timeout=budget)

def test_fewer_blocking_reply_reads(clock):
    class RoundTripDevice(TrackingDevice):
        def read(self, size=1):
            clock.sleep(.2)  # Synthetic per-read round trip; not a hardware measurement.
            return super().read(size)
    sequential, pipelined = RoundTripDevice(), RoundTripDevice()
    one = SerialLink(transport=sequential)
    for command in commands(64): one.command(*command)
    sequential_time = clock.now
    SerialLink(transport=pipelined).pipeline(commands(64), window=8)
    assert sequential_time == pytest.approx(12.8)
    assert clock.now - sequential_time == pytest.approx(1.6)
    assert sequential.reads == 64 and pipelined.reads == 8
    assert pipelined.peak == 8
    assert [c[1:] for c in pipelined.commands] == [c[1:] for c in sequential.commands]


def test_immediate_busy_before_earlier_acks_still_drains_by_id():
    class BusyFirst(TrackingDevice):
        def read(self, size=1):
            if self.reads == 0:
                self._output = bytearray(b'@8 ERR BUSY\n' + b''.join(f'@{i} OK\n'.encode() for i in range(1, 8)))
            return super().read(size)
    device = BusyFirst(busy_at=8, max_read=3)
    with pytest.raises(ProtocolError) as caught:
        SerialLink(transport=device).pipeline(commands(20))
    assert caught.value.code == 'BUSY'
    assert caught.value.confirmed == 7 and caught.value.sent == 8
    assert caught.value.confirmed_indices == tuple(range(7))
    assert not device.outstanding


def test_drain_read_timeout_is_bounded(clock):
    class ReadTimeout(TrackingDevice):
        def read(self, size=1): raise TimeoutError('synthetic transport timeout')
    device = ReadTimeout()
    with pytest.raises(TimeoutError) as caught:
        SerialLink(transport=device).pipeline(commands(20), window=2, drain_timeout=.02)
    assert caught.value.sent == 2 and caught.value.confirmed == 0
    assert clock.now == pytest.approx(.02)
    assert device.closed
