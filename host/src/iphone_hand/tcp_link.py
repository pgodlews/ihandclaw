"""SPEC 7 TCP transport, sharing SerialLink's framing, replies and pacing API.

Read: dropped sockets get one recovery attempt; only PING/STATUS or unsubmitted
commands may be replayed. Timeouts invalidate the socket for the next command.
Failed recovery uses a non-sleeping 0.25–2 second backoff. PING/STATUS share one
I/O budget across connect, AUTH and replay. The token never appears in diagnostics.
"""

import math
import re
import socket
import time

from .serial_link import PipelineError, ProtocolError, SerialLink, fields
from .link_errors import ReconnectDeferred, link_error_class


class _SocketTransport:
    def __init__(self, sock: socket.socket, timeout: float):
        self.sock = sock
        self.timeout = timeout
        self.write_attempted = False
        self.deadline = None

    def remaining(self, limit: float) -> float:
        if self.deadline is None:
            return limit
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError('TCP I/O timed out')
        return min(limit, remaining)

    def write(self, data: bytes) -> int:
        self.sock.settimeout(self.remaining(self.timeout))
        self.write_attempted = True
        n = self.sock.send(data)
        if n == 0:
            raise ConnectionError("TCP connection closed during write")
        return n

    def read(self, size: int = 1) -> bytes:
        self.sock.settimeout(self.remaining(0.02))
        try:
            data = self.sock.recv(size)
        except socket.timeout:
            return b""
        if not data:
            raise ConnectionError("TCP connection closed during read")
        return data

    def close(self) -> None:
        self.sock.close()


class TcpLink(SerialLink):
    def __init__(self, host: str, port: int, token: str, *, timeout: float = 2.0):
        if not isinstance(token, str) or not re.fullmatch(r"[0-9a-fA-F]{32,64}", token):
            raise ValueError("token must contain 32–64 hexadecimal characters")
        if not isinstance(port, int) or not 1 <= port <= 65535:
            raise ValueError("port must be between 1 and 65535")
        if not isinstance(host, str) or not host:
            raise ValueError("host must be nonempty")
        super().__init__(timeout=timeout)
        self.host, self.port = host, port
        self._token = token
        self._deadline = None
        self._retry_at = 0.0
        self._backoff = 0.25
        self._last_error = 'closed'

    def open(self) -> None:
        with self._lock:
            if self.transport is not None:
                return
            if time.monotonic() < self._retry_at:
                raise ReconnectDeferred(self._last_error)
            deadline = time.monotonic() + self.timeout
            if self._deadline is not None:
                deadline = min(deadline, self._deadline)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError('TCP connect budget exhausted')
            sock = socket.create_connection((self.host, self.port), timeout=remaining)
            transport = _SocketTransport(sock, self.timeout)
            transport.deadline = deadline
            self._buffer.clear()
            self._discard_line = False
            try:
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                sock.settimeout(transport.remaining(self.timeout))
                sock.sendall(f"AUTH {self._token}\n".encode("ascii"))
                authenticated = False
                while time.monotonic() < deadline:
                    data = transport.read(256)
                    for line in self._lines(data):
                        if line == "@0 OK auth":
                            authenticated = True
                        elif line.startswith("@0 EVT "):
                            event, _, detail = line[7:].partition(" ")
                            self.events.append({"event": event, **fields(detail)})
                        else:
                            raise ProtocolError("AUTH", "Unexpected authentication response")
                    if authenticated:
                        transport.deadline = self._deadline
                        self.transport = transport
                        return
                    if not data:
                        time.sleep(min(0.001, max(0, deadline - time.monotonic())))
                raise TimeoutError("TCP authentication timed out")
            except BaseException:
                transport.close()
                self._buffer.clear()
                self._discard_line = False
                raise

    def close(self) -> None:
        super().close()
        self._discard_line = False

    def command(self, command: str, *args, timeout: float | None = None):
        with self._lock:
            budget = self._budget(command, args) if timeout is None else timeout
            if not math.isfinite(budget) or budget <= 0:
                raise ValueError('timeout must be positive and finite')
            safe = command in {'PING', 'STATUS'}
            self._deadline = time.monotonic() + budget if safe else None
            try:
                for attempt in range(2):
                    if isinstance(self.transport, _SocketTransport):
                        self.transport.write_attempted = False
                        self.transport.deadline = self._deadline
                    try:
                        reply = super().command(command, *args, timeout=timeout)
                    except ReconnectDeferred:
                        raise  # Polling during cooldown must not extend it.
                    except ProtocolError as error:
                        if error.code == 'AUTH':
                            self.close()
                            self._defer(error)
                        raise
                    except OSError as error:
                        transport = self.transport
                        submitted = isinstance(transport, _SocketTransport) and transport.write_attempted
                        self.close()  # Includes timeouts and failures on the retry.
                        # OTA state belongs to one AUTH session. Never replay it.
                        if command == 'OTA':
                            raise
                        if submitted and not safe:
                            error_type = TimeoutError if isinstance(error, TimeoutError) else ConnectionError
                            raise error_type(
                                'TCP command outcome is unknown (acknowledgement lost)'
                            ) from error
                        if isinstance(error, TimeoutError) or attempt or transport is None:
                            self._defer(error)
                            raise
                        # One immediate retry for a dropped, read-only command.
                    else:
                        self._retry_at = 0.0
                        self._backoff = 0.25
                        return reply
            finally:
                self._deadline = None
                if isinstance(self.transport, _SocketTransport):
                    self.transport.deadline = None

    def _pipeline_deadline(self, deadline):
        if isinstance(self.transport, _SocketTransport):
            self.transport.deadline = deadline

    def pipeline(self, commands, **kwargs):
        # Never enter command()'s reconnect/replay loop, even before the first ACK.
        with self._lock:
            try:
                result = super().pipeline(commands, **kwargs)
            except PipelineError as error:
                self.close()
                if isinstance(error.cause, OSError) or error.code == 'AUTH':
                    self._defer(error.cause)
                raise
            else:
                self._retry_at = 0.0
                self._backoff = 0.25
                return result
            finally:
                self._pipeline_deadline(None)

    def _defer(self, error: Exception) -> None:
        self._last_error = link_error_class(error)
        self._retry_at = time.monotonic() + self._backoff
        self._backoff = min(2.0, self._backoff * 2)
