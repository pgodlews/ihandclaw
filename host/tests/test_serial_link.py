import pytest

from iphone_hand.fake import FakeSerialDevice
from iphone_hand.serial_link import ProtocolError, SerialLink


@pytest.mark.parametrize("chip", ["esp32c3", "esp32c5"])
def test_ids_events_logs_fragmentation_and_wrap(chip):
    device = FakeSerialDevice(max_read=3, chip=chip)
    device.queue(b"garbled @2 OK\n@42 OK unrelated=1\n@0 EVT ready\n")
    device.queue(b"x" * 2000 + b"\n@0 EVT conn peer=-\n")
    link = SerialLink(transport=device)
    assert link.command("PING").fields == {"fw": "fake", "chip": chip}
    assert link.command("STATUS").id == 2
    assert [e["event"] for e in link.events] == ["boot", "ready", "conn"]
    link._id = 999998
    assert link.command("PING").id == 999999
    assert link.command("PING").id == 1
    assert [c[0] for c in device.commands] == [1, 2, 999999, 1]


def test_timeout_no_retry_and_late_reply_ignored():
    device = FakeSerialDevice()
    link = SerialLink(transport=device, timeout=0.01)
    device.drop_commands.add("MOVE")
    with pytest.raises(TimeoutError, match="MOVE"):
        link.command("MOVE", 10, 5, timeout=0.01)
    assert len(device.commands) == 1
    assert device.position != [187.5, 406]
    device.queue(b"@1 OK late=1\n@0 EVT disc reason=9\n")
    assert link.command("PING").id == 2
    assert link.events[-1] == {"event": "disc", "reason": "9"}


def test_events_after_reply_in_same_chunk():
    class Device(FakeSerialDevice):
        def write(self, data):
            n = super().write(data)
            self.queue(b"@0 EVT ready\n")
            return n
    link = SerialLink(transport=Device())
    link.command("PING")
    assert link.events[-1]["event"] == "ready"


def test_errors_and_command_integrity():
    device = FakeSerialDevice(connected=False)
    link = SerialLink(transport=device)
    with pytest.raises(ProtocolError) as exc:
        link.command("MOVE", 1, 1)
    assert exc.value.code == "NOCONN"
    with pytest.raises(ProtocolError, match="UNKNOWN"):
        link.command("BOGUS")
    for command, args in [("PING\n2 MOVE", ()), ("NAME", ("a\nb",)), ("NAME", ("é" * 128,))]:
        with pytest.raises(ValueError):
            link.command(command, *args)
    assert len(device.commands) == 2


def test_complete_protocol_and_pacing():
    device = FakeSerialDevice()
    link = SerialLink(transport=device)
    assert link.command("CFG", "step=5", "interval=20").fields == {"step": "5", "interval": "20"}
    assert link._budget("MOVE", (2000, 0)) >= 12.5
    link.command("BTN", 1)
    link.command("MOVE", -12, 7)
    assert all(max(abs(r[1]), abs(r[2])) <= 5 for r in device.reports if r[0] == "MOVE")
    assert any(r[3] == 1 for r in device.reports if r[0] == "MOVE")
    link.command("BTN", 0)
    link.command("CLICK")
    link.command("SCROLL", 300, -200)
    assert [r for r in device.reports if r[0] == "SCROLL"] == [
        ("SCROLL", 127, -127), ("SCROLL", 127, -73), ("SCROLL", 46, 0)]
    link.command("KEY", "0x08", "0x2c")
    link.command("KREPORT", 2, 4, 5)
    assert device.keyboard == (2, 4, 5)
    link.command("KREPORT", 0, 0)
    link.command("CONSUMER", "0xe9")
    link.command("NAME", "Test Hand")
    with pytest.raises(ProtocolError, match="ARG"):
        link.command("CFG", "step=128")
    link.command("UNPAIR")
    assert link.command("STATUS").fields["ready"] == "0"
    link.close()
    assert device.closed


def test_nonlinear_acceleration():
    small, large = FakeSerialDevice(position=(100, 100)), FakeSerialDevice(position=(100, 100))
    SerialLink(transport=small).command("MOVE", 1, 0)
    SerialLink(transport=large).command("MOVE", 8, 0)
    assert (large.position[0] - 100) / 8 > small.position[0] - 100


def test_partial_writes():
    class Device(FakeSerialDevice):
        def write(self, data):
            return super().write(data[:2])
    assert SerialLink(transport=Device()).command("PING").fields["fw"] == "fake"


def test_hardening_stop_pair_and_status_fields():
    device = FakeSerialDevice(max_read=3)
    device.out_drops = 42
    link = SerialLink(transport=device)
    status = link.command("STATUS").fields
    assert status["out_drops"] == "42" and int(status["uptime_ms"]) >= 0
    assert status["reset"] == "poweron" and status["pairing"] == "0"
    link.command("BTN", 7)
    link.command("KREPORT", 8, 4)
    link.command("STOP")
    assert device.buttons == 0 and device.keyboard == (0, 0)
    assert device.reports[-3:] == [("KEY", 0, 0), ("BTN", 0), ("CONSUMER", 0)]
    for command in ["STOP", "PAIR"]:
        with pytest.raises(ProtocolError, match="ARG"):
            link.command(command, 1)
    link.command("PAIR")
    assert link.command("STATUS").fields["pairing"] == "1"
    link.command("STOP")  # Safe and successful with no BLE connection.


@pytest.mark.parametrize("command,args", [
    ("MOVE", (2001, 0)), ("MOVE", (0, -2001)),
    ("SCROLL", (2001,)), ("SCROLL", (0, -2001)),
    ("SCROLL", (-2147483648,)), ("SCROLL", (2147483647,)),
])
def test_hardened_axis_bounds_reject_without_reports(command, args):
    device = FakeSerialDevice()
    with pytest.raises(ProtocolError, match="ARG"):
        SerialLink(transport=device).command(command, *args)
    assert device.reports == []


def test_timeout_error_and_cancelled_reply_are_not_replayed():
    device = FakeSerialDevice()
    link = SerialLink(transport=device)
    with pytest.raises(ProtocolError, match="TIMEOUT"):
        link.command("KEY", 8, 4, 65535)
    assert device.keyboard == (0, 0)
    device.drop_commands.add("MOVE")
    device.queue(b"@2 ERR CANCELLED\n")
    with pytest.raises(ProtocolError, match="CANCELLED"):
        link.command("MOVE", 8, 0)
    assert [c[1] for c in device.commands] == ["KEY", "MOVE"]
    assert link._budget("KEY", (8, 4, 65535)) == 12.5
    assert link._budget("STOP", ()) == 4.5


class _BusyDevice(FakeSerialDevice):
    """Answers the first `busy` KEY commands with ERR BUSY without executing them."""

    def __init__(self, busy, **kwargs):
        super().__init__(**kwargs)
        self.busy = busy
        self.key_writes = 0

    def write(self, data):
        rid, command = data.decode().split()[:2]
        if command == "KEY" and self.key_writes < self.busy:
            self.key_writes += 1
            self.queue(f"@{rid} ERR BUSY\n".encode())
            return len(data)
        return super().write(data)


def test_busy_is_retried_with_bounded_backoff(monkeypatch):
    sleeps = []
    monkeypatch.setattr("iphone_hand.serial_link.time.sleep", sleeps.append)
    device = _BusyDevice(busy=2)
    link = SerialLink(transport=device)
    link.command("KEY", 8, 44, 100)
    assert sleeps == [0.2, 0.4]
    assert [c[1] for c in device.commands] == ["KEY"]  # executed exactly once


def test_busy_gives_up_after_three_attempts(monkeypatch):
    monkeypatch.setattr("iphone_hand.serial_link.time.sleep", lambda _: None)
    device = _BusyDevice(busy=3)
    with pytest.raises(ProtocolError, match="BUSY"):
        SerialLink(transport=device).command("KEY", 8, 44, 100)
    assert device.commands == []
