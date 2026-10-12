"""Read: application-only OTA over one authenticated TcpLink session.

No upload replay/resume: close on any failure. Only reboot verification retries.
The ESP app descriptor supplies the expected version and ELF identity, allowing
verification even when successive builds share a version string.
"""

import base64
import hashlib
import math
from pathlib import Path
import re
import struct
import time
from typing import Callable

from .serial_link import ProtocolError, fields
from .tcp_link import TcpLink

SLOT_SIZE = 0x1F0000
CHUNK_SIZE = 168
BINARY_CHUNK_SIZE = 8192
WINDOW_SIZE = 4
# Read: esp_app_format.h, esp_chip_id_t (IDF 6.2).
CHIP_IDS = {0x05: "esp32c3", 0x17: "esp32c5"}


def image_chip(image: bytes) -> str:
    if len(image) < 24:
        raise ValueError("truncated ESP application header")
    chip_id = struct.unpack_from("<H", image, 12)[0]
    if chip_id not in CHIP_IDS:
        raise ValueError(f"unsupported application chip ID 0x{chip_id:04x}; expected ESP32-C3 or ESP32-C5")
    return CHIP_IDS[chip_id]


class OTAUpdateError(RuntimeError):
    def __init__(self, message: str, report: dict[str, str]):
        super().__init__(message)
        self.report = report


TOKEN_PATH = Path("~/.config/iphone-hand/board-token")


def image_identity(image: bytes) -> tuple[str, str]:
    """Read IDF's 24-byte image header + 8-byte segment header + app descriptor."""
    if not 288 <= len(image) <= SLOT_SIZE:
        raise ValueError(f"application image must be 288..{SLOT_SIZE} bytes (OTA slot size)")
    if image[0] != 0xE9 or not 1 <= image[1] <= 16:
        raise ValueError("not an ESP application image; use build/<target>/iphone_hand.bin")
    image_chip(image)
    if struct.unpack_from("<I", image, 32)[0] != 0xABCD5432:
        raise ValueError("missing ESP application descriptor")
    def text(offset: int) -> str:
        raw = image[offset:offset + 32]
        if b"\0" not in raw:
            raise ValueError("unterminated application descriptor field")
        return raw.split(b"\0", 1)[0].decode("ascii")
    version = text(48)
    if not re.fullmatch(r"[A-Za-z0-9._-]+", version) or text(80) != "iphone_hand":
        raise ValueError("expected an iphone_hand image with a protocol-safe version")
    return version, image[176:208].hex()


def _binary_window(link: TcpLink, image: bytes, offset: int, chunk: int, window: int) -> int:
    """Pipeline a bounded window; only its last frame requests a cumulative ACK."""
    link._id = link._id % 999999 + 1
    rid = link._id
    stop = min(len(image), offset + chunk * window)
    wire = bytearray()
    for pos in range(offset, stop, chunk):
        payload = image[pos:min(pos + chunk, stop)]
        ack = rid if pos + len(payload) == stop else 0
        wire.extend(struct.pack(">III", len(payload), pos, ack))
        wire.extend(payload)
    assert link.transport is not None
    deadline = time.monotonic() + 15
    sent = 0
    while sent < len(wire):
        sent += link.transport.write(wire[sent:])
        if time.monotonic() >= deadline:
            raise TimeoutError("OTA binary window write timed out")
    response = None
    while time.monotonic() < deadline:
        data = link.transport.read(512)
        for line in link._lines(data):
            if line.startswith(f"@{rid} ERR "):
                raise ProtocolError(line.split()[2])
            if line.startswith("@0 EVT ota_error"):
                raise RuntimeError("board rejected a binary OTA frame")
            if line.startswith(f"@{rid} OK"):
                response = fields(line)
        if response is not None:
            if response.get("received") != str(stop):
                raise RuntimeError("board OTA byte count differs from upload")
            return stop
        if not data:
            time.sleep(0.001)
    raise TimeoutError("OTA binary window ACK timed out")


def _matches(ping: dict[str, str], version: str, elf: str, slot: str) -> bool:
    # Read: legacy IDF helper returns CONFIG_APP_RETRIEVE_LEN_ELF_SHA (default 9).
    # Slot transition is mandatory, so an identical reinstall cannot match the old app.
    observed = ping.get("elf", "")
    return (ping.get("fw") == version and ping.get("slot") == slot and
            re.fullmatch(r"[0-9a-f]{8,64}", observed) is not None and elf.startswith(observed))


def upload(link: TcpLink, image: bytes, *, progress: Callable[[int, int], None] | None = None,
           phase: Callable[[str], None] | None = None, verify_timeout: float = 150.0,
           wait_confirm: bool = True) -> dict[str, str]:
    version, elf = image_identity(image)
    if not math.isfinite(verify_timeout) or verify_timeout <= 0:
        raise ValueError("verify timeout must be positive and finite")
    digest = hashlib.sha256(image).hexdigest()
    def announce(name):
        if phase:
            phase(name)
    with link._lock:
        binary = False
        begun = False
        try:
            announce("preflight")
            before = link.command("PING").fields
            chip = image_chip(image)
            board_chip = before.get("chip")
            if board_chip != chip:
                raise ValueError(f"OTA chip mismatch: image targets {chip}, board reports {board_chip or 'missing chip'}")
            # New firmware advertises binary capability in STATUS; legacy BEGIN syntax stays valid.
            status = link.command("STATUS", timeout=7).fields
            binary = status.get("ota_binary") == "1"
            args = ("BEGIN", len(image), digest) + (("BIN",) if binary else ())
            announce("begin")
            begun = True
            begin = link.command("OTA", *args, timeout=7).fields
            chunk = int(begin.get("chunk", "0"))
            window = int(begin.get("window", "1"))
            slot = begin.get("slot")
            if (not 1 <= chunk <= (BINARY_CHUNK_SIZE if binary else CHUNK_SIZE) or
                not 1 <= window <= WINDOW_SIZE or slot not in {"ota_0", "ota_1"} or
                slot == before.get("slot") or begin.get("size") != str(len(image)) or (binary and begin.get("mode") != "bin")):
                raise RuntimeError("board returned invalid OTA negotiation")
            announce("upload")
            if progress:
                progress(0, len(image))
            offset = 0
            while offset < len(image):
                if binary:
                    offset = _binary_window(link, image, offset, chunk, window)
                else:
                    data = image[offset:offset + chunk]
                    reply = link.command("OTA", "DATA", base64.b64encode(data).decode("ascii"), timeout=7)
                    offset += len(data)
                    if reply.fields.get("received") != str(offset):
                        raise RuntimeError("board OTA byte count differs from upload")
                if progress:
                    progress(offset, len(image))
            announce("validate/select boot")
            try:
                end = link.command("OTA", "END", timeout=15).fields
            except (OSError, TimeoutError):
                end = None
            if end is not None and (end.get("fw") != version or end.get("slot") != slot or
                                    end.get("reboot") != "1"):
                raise RuntimeError("board returned unexpected OTA completion")
        except BaseException:
            # Binary payload cannot be interrupted by a line; close to abort it.
            if begun and not binary and link.transport is not None:
                try:
                    link.command("OTA", "ABORT", timeout=2)
                except (OSError, RuntimeError, TimeoutError):
                    pass
            link.close()
            raise
        link.close()
        announce("reboot/verify")
        deadline = time.monotonic() + verify_timeout
        time.sleep(min(1.2, verify_timeout))
        observed = "board unavailable"
        seen_trial = False
        last_ping, last_status = {}, {}
        def result(ping, status, confirmed, outcome):
            return {"fw_before": before.get("fw", "?"), "slot_before": before.get("slot", "?"),
                    "fw": ping.get("fw", "?"), "slot": ping.get("slot", "?"),
                    "fw_after": ping.get("fw", "?"), "slot_after": ping.get("slot", "?"),
                    "elf": ping.get("elf", "?"), "sha256": digest, "size": str(len(image)),
                    "confirmed": str(int(confirmed)), "outcome": outcome,
                    "ota_last": status.get("ota_last", "legacy-unavailable")}
        try:
            while time.monotonic() < deadline:
                try:
                    ping = link.command("PING").fields
                    last_ping = ping
                    observed = f"fw={ping.get('fw', '?')} slot={ping.get('slot', '?')}"
                    if _matches(ping, version, elf, slot):
                        seen_trial = True
                        announce("confirm trial (authenticated STATUS)")
                        try:
                            status = link.command("STATUS", timeout=7).fields
                        except ProtocolError as exc:
                            last_status = fields(str(exc))
                            if not wait_confirm:
                                return result(ping, last_status, False, "trial_unconfirmed")
                            raise
                        last_status = status
                        if status.get("ota_pending") == "0" and status.get("wifi") == "1":
                            announce("confirmed")
                            return result(ping, status, True, "confirmed")
                        if not wait_confirm:
                            return result(ping, status, False, "trial_unconfirmed")
                    else:
                        # PING is read-only. Never confirm an unexpected image just to read diagnostics.
                        last_status = ping
                        if (seen_trial or ping.get("slot") == slot or
                            ping.get("ota_last", "").startswith(("rollback,", "boot_fallback,"))):
                            raise OTAUpdateError(
                                f"OTA failed: running {observed}; ota_last={ping.get('ota_last', '?')}",
                                result(ping, ping, False, "rollback" if ping.get("slot") != slot else "identity_mismatch"))
                except ProtocolError as exc:
                    observed = f"board health check failed: {exc.code}"
                except (OSError, TimeoutError):
                    observed = "board unavailable"
                link.close()
                time.sleep(min(0.5, max(0, deadline - time.monotonic())))
            # Loss of contact is an unknown outcome, not evidence of rollback.
            announce("verification inconclusive")
            return result(last_ping, last_status, False, f"unverified ({observed})")
        finally:
            link.close()


def update_file(path: str, host: str, port: int = 7878, *,
                progress: Callable[[int, int], None] | None = None,
                phase: Callable[[str], None] | None = None, wait_confirm: bool = False) -> dict[str, str]:
    image = Path(path).read_bytes()
    image_identity(image)  # Reject bad artifacts before reading token or opening TCP.
    token = TOKEN_PATH.expanduser().read_text(encoding="ascii").strip()
    return upload(TcpLink(host, port, token), image, progress=progress, phase=phase, wait_confirm=wait_confirm)
