"""Bounded synchronous and pipelined clients for SPEC section 1."""

from collections import deque
from dataclasses import dataclass
import glob
import math
import os
import re
import threading
import time
from typing import Protocol


class Transport(Protocol):
    def write(self, data: bytes) -> int: ...
    def read(self, size: int = 1) -> bytes: ...
    def close(self) -> None: ...


class ProtocolError(RuntimeError):
    def __init__(self, code: str, message: str = ""):
        self.code = code
        super().__init__(f"{code}: {message}" if message else code)


class PipelineError(RuntimeError):
    """Partial outcome. Zero-based indices; missing ACKs have unknown outcomes."""
    def __init__(self, cause, *, sent=0, replies=None):
        self.cause = cause
        self.sent = sent
        self.replies = replies or {}
        self.confirmed_indices = tuple(sorted(self.replies))
        self.confirmed = len(self.confirmed_indices)
        self.code = getattr(cause, "code", None)
        RuntimeError.__init__(self, f"Pipeline stopped: {type(cause).__name__}; "
                              f"confirmed={self.confirmed}, sent={sent}")


class PipelineTimeout(PipelineError, TimeoutError):
    pass


class PipelineConnectionError(PipelineError, ConnectionError):
    pass


class PipelineProtocolError(PipelineError, ProtocolError):
    pass


@dataclass(frozen=True)
class Reply:
    id: int
    fields: dict[str, str]
    text: str


def fields(text: str) -> dict[str, str]:
    return dict(token.split("=", 1) for token in text.split() if "=" in token)


class SerialLink:
    def __init__(self, port: str | None = None, *, transport: Transport | None = None,
                 timeout: float = 2.0):
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("timeout must be positive and finite")
        self.port = port or os.environ.get("IPHONE_HAND_PORT")
        self.transport = transport
        self.timeout = timeout
        self.events: deque[dict] = deque(maxlen=100)
        self._id = 0
        self._buffer = bytearray()
        self._discard_line = False
        self._lock = threading.RLock()
        self.step, self.interval = 8, 15

    def open(self) -> None:
        if self.transport is not None:
            return
        port = self.port
        if port is None:
            ports = sorted(glob.glob("/dev/cu.usbmodem*"))
            if len(ports) != 1:
                raise RuntimeError("Set IPHONE_HAND_PORT; expected exactly one USB modem port")
            port = ports[0]
        import serial
        # Keep pyserial's default DTR/RTS high on open: setting them low on the C5's
        # USB-Serial/JTAG reboots the chip (measured 2026-10-10) and drops the BLE link.
        self.transport = serial.Serial(port, 115200, timeout=0.02, write_timeout=self.timeout)
        self.port = port

    def close(self) -> None:
        with self._lock:
            if self.transport is not None:
                self.transport.close()
                self.transport = None
            self._buffer.clear()

    def _lines(self, data: bytes):
        # Serial logs can be arbitrarily long; do not grow an unbounded buffer.
        for byte in data:
            if byte == 10:
                if not self._discard_line:
                    yield self._buffer.decode("utf-8", errors="replace").rstrip("\r")
                self._buffer.clear()
                self._discard_line = False
            elif not self._discard_line:
                self._buffer.append(byte)
                if len(self._buffer) > 512:
                    self._buffer.clear()
                    self._discard_line = True

    def _budget(self, command: str, args: tuple) -> float:
        # Read (firmware policy): paced actions are capped at 10 seconds,
        # then safety release gets 2 seconds. Leave 0.5 s for reply transport.
        # An explicit caller timeout still overrides this default.
        if command in {"MOVE", "SCROLL", "BTN", "CLICK", "KEY", "KREPORT", "CONSUMER"}:
            return max(self.timeout, 12.5)
        if command == "STOP":
            return max(self.timeout, 4.5)
        if command in {"PAIR", "NAME", "UNPAIR"}:
            return max(self.timeout, 7.0)
        return self.timeout

    def command(self, command: str, *args, timeout: float | None = None) -> Reply:
        if not re.fullmatch(r"[A-Z]+", command):
            raise ValueError("command must contain uppercase letters only")
        if any("\n" in str(a) or "\r" in str(a) for a in args):
            raise ValueError("arguments cannot contain newlines")
        budget = self._budget(command, args) if timeout is None else timeout
        if not math.isfinite(budget) or budget <= 0:
            raise ValueError("timeout must be positive and finite")
        with self._lock:
            next_id = self._id % 999999 + 1
            line = f"{next_id} {command}" + (" " + " ".join(map(str, args)) if args else "") + "\n"
            wire = line.encode("utf-8")
            if len(wire) > 256:
                raise ValueError("command exceeds 256 bytes including newline")
            self.open()
            self._id = next_id
            assert self.transport is not None
            deadline = time.monotonic() + budget
            offset = 0
            while offset < len(wire):
                n = self.transport.write(wire[offset:])
                if not n or time.monotonic() >= deadline:
                    raise TimeoutError(f"Serial write timed out for command {next_id}")
                offset += n
            response = None
            while time.monotonic() < deadline:
                data = self.transport.read(256)
                for incoming in self._lines(data):
                    match = re.fullmatch(r"@(\d+) (OK|ERR|EVT)(?: (.*))?", incoming)
                    if not match:
                        continue
                    rid, kind, body = int(match[1]), match[2], match[3] or ""
                    if rid == 0 and kind == "EVT":
                        event, _, detail = body.partition(" ")
                        self.events.append({"event": event, **fields(detail)})
                    elif rid == next_id and kind in {"OK", "ERR"}:
                        response = (kind, body)
                # Consume the entire chunk, including any EVT after the ACK.
                if response:
                    kind, body = response
                    if kind == "ERR":
                        code, _, detail = body.partition(" ")
                        raise ProtocolError(code, detail)
                    result = Reply(next_id, fields(body), body)
                    if command == "CFG":
                        self.step = int(result.fields.get("step", self.step))
                        self.interval = int(result.fields.get("interval", self.interval))
                    return result
                if not data:
                    time.sleep(min(0.001, max(0, deadline - time.monotonic())))
            # A timed-out action may have executed. Never retry it automatically.
            raise TimeoutError(f"No reply for command {next_id} ({command}) within {budget:.3f}s")

    def pipeline(self, commands, *, window=8, timeout_per_command=None,
                 drain_timeout=2.0, before_send=None) -> list[Reply]:
        """Submit (command, *args) tuples without replay, return ordered replies.

        Read: firmware queue depth is 16. Cap at 12 outstanding to reserve four
        slots; default eight reserves eight. Explicit deadlines run from submission
        including queue residence. Defaults include preceding execution budgets.
        After failure await only submitted commands for at most drain_timeout.
        Validate all input before opening/writing. before_send is a pause guard.
        """
        if isinstance(window, bool) or not isinstance(window, int) or not 1 <= window <= 12:
            raise ValueError("window must be an integer in 1..12")
        for budget in (timeout_per_command, drain_timeout):
            if budget is not None and (not math.isfinite(budget) or budget <= 0):
                raise ValueError("timeouts must be positive and finite")
        if drain_timeout is None:
            raise ValueError("drain_timeout must be positive and finite")
        prepared = []
        for command, *args in commands:
            if not re.fullmatch(r"[A-Z]+", command):
                raise ValueError("command must contain uppercase letters only")
            if any("\n" in str(a) or "\r" in str(a) for a in args):
                raise ValueError("arguments cannot contain newlines")
            suffix = command + (" " + " ".join(map(str, args)) if args else "") + "\n"
            if len(("999999 " + suffix).encode("utf-8")) > 256:
                raise ValueError("command exceeds 256 bytes including newline")
            prepared.append((command, suffix, self._budget(command, tuple(args))))
        if not prepared:
            return []
        with self._lock:
            pending = {}  # id -> (input index, deadline, execution budget)
            replies = {}
            ordered = deque()  # Later ACKs cannot release slots ahead of an earlier ACK.
            sent = 0
            failure = None
            drain_deadline = None

            def fail(error):
                nonlocal failure, drain_deadline
                if failure is None:
                    failure = error
                    drain_deadline = time.monotonic() + drain_timeout

            try:
                self.open()
            except Exception as error:
                fail(error)
            while pending or (failure is None and sent < len(prepared)):
                while failure is None and sent < len(prepared) and len(ordered) < window:
                    try:
                        if before_send is not None:
                            before_send()
                        if pending and time.monotonic() >= min(p[1] for p in pending.values()):
                            raise TimeoutError("Pipeline reply deadline expired")
                        command, suffix, budget = prepared[sent]
                        rid = self._id % 999999 + 1
                        self._id = rid
                        # Count partial writes as submitted: their outcome is unknown.
                        deadline = time.monotonic() + (timeout_per_command if timeout_per_command is not None
                                                      else budget + sum(p[2] for p in pending.values()))
                        pending[rid] = (sent, deadline, budget)
                        ordered.append(rid)
                        sent += 1
                        wire = f"{rid} {suffix}".encode("utf-8")
                        self._pipeline_deadline(min(p[1] for p in pending.values()))
                        offset = 0
                        while offset < len(wire):
                            n = self.transport.write(wire[offset:])
                            if not n or time.monotonic() >= deadline:
                                raise TimeoutError("Pipeline write timed out")
                            offset += n
                    except Exception as error:
                        fail(error)
                if not pending:
                    break
                now = time.monotonic()
                if failure is None and now >= min(p[1] for p in pending.values()):
                    fail(TimeoutError("Pipeline reply deadline expired"))
                if failure is not None and now >= drain_deadline:
                    break
                deadline = drain_deadline if failure is not None else min(p[1] for p in pending.values())
                self._pipeline_deadline(deadline)
                try:
                    data = self.transport.read(256)
                    for incoming in self._lines(data):
                        match = re.fullmatch(r"@(\d+) (OK|ERR|EVT)(?: (.*))?", incoming)
                        if not match:
                            continue
                        rid, kind, body = int(match[1]), match[2], match[3] or ""
                        if rid == 0 and kind == "EVT":
                            event, _, detail = body.partition(" ")
                            self.events.append({"event": event, **fields(detail)})
                        elif rid in pending and kind in {"OK", "ERR"}:
                            index, _, _ = pending.pop(rid)
                            if kind == "ERR":
                                code, _, detail = body.partition(" ")
                                fail(ProtocolError(code, detail))
                            else:
                                reply = Reply(rid, fields(body), body)
                                replies[index] = reply
                                if prepared[index][0] == "CFG":
                                    self.step = int(reply.fields.get("step", self.step))
                                    self.interval = int(reply.fields.get("interval", self.interval))
                    while ordered and ordered[0] not in pending:
                        ordered.popleft()
                    if not data:
                        time.sleep(min(.001, max(0, deadline - time.monotonic())))
                except Exception as error:
                    fail(error)
                    if isinstance(error, OSError) and not isinstance(error, TimeoutError):
                        break  # A dropped transport cannot be drained.
                    time.sleep(min(.001, max(0, drain_deadline - time.monotonic())))
            self._pipeline_deadline(None)
            if failure is not None:
                error_type = (PipelineTimeout if isinstance(failure, TimeoutError) else
                              PipelineConnectionError if isinstance(failure, OSError) else
                              PipelineProtocolError if isinstance(failure, ProtocolError) else PipelineError)
                if pending:
                    self.close()  # Partial lines/lost replies cannot contaminate a later call.
                raise error_type(failure, sent=sent, replies=replies) from failure
            return [replies[i] for i in range(sent)]

    def _pipeline_deadline(self, deadline):
        """Transport hook; serial reads already have a short bounded timeout."""
