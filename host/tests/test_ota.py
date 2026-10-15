"""All sockets are in-memory doubles; no bind, network, serial or board access."""

import base64
import hashlib
import socket
import struct

import pytest

from iphone_hand import ota
from iphone_hand.cli import main
from iphone_hand.serial_link import ProtocolError
from iphone_hand.tcp_link import TcpLink


def app_image(version="0.2.0", payload=bytes(range(256)) * 3, chip="esp32c5"):
    image = bytearray(288)
    image[0:2] = b"\xe9\x03"
    struct.pack_into("<H", image, 12, {"esp32c3": 0x05, "esp32c5": 0x17}[chip])
    struct.pack_into("<I", image, 32, 0xABCD5432)
    image[48:48 + len(version)] = version.encode()
    image[80:91] = b"iphone_hand"
    image[176:208] = hashlib.sha256(payload).digest()
    return bytes(image) + payload


class Board:
    """Synthetic OTA board with real hashlib and exact-byte validation."""
    token = "a" * 32

    def __init__(self, *, fault=None, binary=True, short_elf=False, chip="esp32c5",
                 binary_chunk=4096, binary_window=2):
        self.binary_chunk, self.binary_window = binary_chunk, binary_window
        self.chip = chip
        self.binary = binary
        self.short_elf = short_elf
        self.binary_mode = False
        self.clock = lambda: 0
        self.trial_at = None
        self.scheduled = None
        self.old = None
        self.ota_last = "none"
        self.frames = self.windows = 0
        self.fault = fault
        self.version, self.elf, self.slot = "old", "0" * 64, "ota_0"
        self.active = False
        self.pending = False
        self.data = bytearray()
        self.auths = self.confirms = self.ends = self.aborts = 0
        self.commands = []
        self.next_slot = "ota_1"
        self.sockets = []

    def connect(self, address, timeout):
        sock = MemorySocket(self)
        self.sockets.append(sock)
        return sock

    def tick(self):
        if self.scheduled and self.clock() >= self.scheduled[0]:
            _, self.version, self.elf, self.slot = self.scheduled
            self.scheduled = None
            self.pending = True
            self.trial_at = self.clock()
        if self.pending and self.clock() - self.trial_at >= 120:
            self.version, self.elf, self.slot = self.old
            self.pending = False
            self.ota_last = "rollback,ota_0>ota_1,r:timeout"

    def payload(self, payload, rid, offset=None):
        self.frames += 1
        if rid != "0": self.windows += 1
        if offset is not None: assert offset == len(self.data)
        if self.fault == "silent_data": return b""
        if self.fault == "interrupt":
            self.fault = None
            return None
        if self.fault == "bad_hash" and not self.data:
            payload = bytes([payload[0] ^ 1]) + payload[1:]
        self.data.extend(payload)
        if len(self.data) > self.size:
            self.active = False
            return f"@{rid} ERR SIZE\n".encode()
        count = len(self.data) + (1 if self.fault == "bad_count" else 0)
        return f"@{rid} OK received={count}\n".encode() if rid != "0" else b""

    def command(self, line):
        self.tick()
        rid, command, *args = line.decode("ascii").split()
        self.commands.append((command, args.copy()))
        def reply(body):
            return f"@{rid} {body}\n".encode()
        if command == "PING":
            chip_field = f" chip={self.chip}" if self.chip is not None else ""
            return reply(f"OK fw={self.version}{chip_field} elf={self.elf[:9] if self.short_elf else self.elf} slot={self.slot} ota_last={self.ota_last}")
        if command == "STATUS":
            if self.pending:
                if self.fault == "health":
                    return reply("ERR INTERNAL")
                if self.fault == "transient_health":
                    self.fault = None
                    return reply("ERR INTERNAL")
                self.pending = False
                self.confirms += 1
                self.ota_last = "confirmed,ota_0>ota_1,c:0"
            return reply(f"OK conn=0 ready=0 wifi=1 ota_pending={int(self.pending)} ota_binary={int(self.binary)} ota_last={self.ota_last}")
        assert command == "OTA"
        op = args.pop(0)
        if op == "BEGIN":
            assert not self.active
            self.active = True
            self.data.clear()
            self.size, self.sha = int(args[0]), args[1]
            if self.fault == "short_size":
                self.size -= 1
            if self.fault == "long_size":
                self.size += 1
            self.binary_mode = len(args) == 3 and args[2] == "BIN"
            chunk = self.binary_chunk if self.binary_mode else 168
            return reply(f"OK chunk={chunk} window={self.binary_window if self.binary_mode else 1} mode={'bin' if self.binary_mode else 'base64'} size={self.size} slot={self.next_slot}")
        if op == "ABORT":
            self.active = False
            self.aborts += 1
            return reply("OK")
        assert self.active
        if op == "DATA":
            payload = base64.b64decode(args[0], validate=True)
            assert 0 < len(payload) <= 168
            return self.payload(payload, rid)
        assert op == "END"
        self.ends += 1
        self.active = False
        if len(self.data) != self.size:
            return reply("ERR SIZE")
        if hashlib.sha256(self.data).hexdigest() != self.sha:
            return reply("ERR HASH")
        if self.fault == "image":
            return reply("ERR IMAGE")
        version, elf = ota.image_identity(self.data)
        self.old = self.version, self.elf, self.slot
        if self.fault != "rollback":
            self.version, self.elf, self.slot = version, elf, self.next_slot
            self.pending = True
            self.trial_at = self.clock()
            self.ota_last = "trial,ota_0>ota_1,r:await_status"
        else:
            self.ota_last = "rollback,ota_0>ota_1,r:unconfirmed_reset"
        if self.fault == "identity": self.elf = "f" * 64
        if self.fault == "delayed_boot":
            self.scheduled = self.clock() + 3, self.version, self.elf, self.slot
            self.version, self.elf, self.slot = self.old
            self.pending = False
        if self.fault == "lost_end":
            return None  # Boot selected, END ack lost: host may verify, never replay.
        return reply(f"OK fw={version} reboot=1 slot={self.next_slot}")


class MemorySocket:
    def __init__(self, board):
        self.board = board
        self.input, self.output = bytearray(), bytearray()
        self.authed = self.closed = False

    def settimeout(self, timeout):
        pass

    def setsockopt(self, *args):
        pass

    def sendall(self, data):
        while data:
            data = data[self.send(data):]

    def send(self, data):
        if self.closed:
            raise BrokenPipeError
        # Short writes and reads force framing through the actual TcpLink path.
        count = min(len(data), 19)
        self.input.extend(data[:count])
        while self.input:
            if self.authed and self.board.binary_mode:
                if len(self.input) < 12: break
                length, offset, rid = struct.unpack_from(">III", self.input)
                assert 0 < length <= 8192
                if len(self.input) < 12 + length: break
                payload = bytes(self.input[12:12 + length])
                del self.input[:12 + length]
                response = self.board.payload(payload, str(rid), offset)
                # Wire mode ends at the advertised byte count, independently of validation.
                if offset + length >= self.board.size: self.board.binary_mode = False
                if response is None:
                    self.close()
                    break
                self.output.extend(response)
                continue
            if b"\n" not in self.input: break
            line, _, rest = self.input.partition(b"\n")
            self.input = bytearray(rest)
            if not self.authed:
                assert line == f"AUTH {self.board.token}".encode()
                self.authed = True
                self.board.auths += 1
                response = b"@0 OK auth\n@0 EVT ready\n"
            else:
                response = self.board.command(line)
            if response is None:
                self.close()
                break
            self.output.extend(response)
        return count

    def recv(self, size):
        if self.output:
            size = min(size, len(self.output), 7)
            data = bytes(self.output[:size])
            del self.output[:size]
            return data
        if self.closed:
            return b""
        raise socket.timeout

    def close(self):
        self.closed = True
        self.board.binary_mode = False
        self.board.active = False  # Production executor polls AUTH session loss.


@pytest.fixture
def board(monkeypatch):
    boards = []
    elapsed = [0.0]
    def monotonic():
        elapsed[0] += 0.0001
        return elapsed[0]
    def sleep(seconds):
        elapsed[0] += seconds
    monkeypatch.setattr(ota.time, "monotonic", monotonic)
    monkeypatch.setattr(ota.time, "sleep", sleep)
    def create(**kwargs):
        b = Board(**kwargs)
        b.clock = lambda: elapsed[0]
        boards.append(b)
        monkeypatch.setattr(socket, "create_connection", b.connect)
        return b, TcpLink("memory-board", 7878, b.token)
    yield create
    assert all(s.closed for b in boards for s in b.sockets)


@pytest.mark.parametrize("chip", ["esp32c3", "esp32c5"])
@pytest.mark.parametrize("binary", [True, False])
def test_upload_progress_hash_version_slot_auth_and_framing(board, chip, binary):
    b, link = board(chip=chip, binary=binary)
    image = app_image(chip=chip)
    progress = []
    result = ota.upload(link, image, progress=lambda sent, total: progress.append((sent, total)))
    assert result["fw"] == "0.2.0" and result["confirmed"] == "1" and result["slot"] == "ota_1"
    assert result["sha256"] == hashlib.sha256(image).hexdigest()
    assert b.data == image and b.auths == 2 and b.confirms == 1
    assert progress[0] == (0, len(image)) and progress[-1] == (len(image), len(image))
    window_bytes = b.binary_chunk * b.binary_window if binary else ota.CHUNK_SIZE
    assert len(progress) == 1 + (len(image) + window_bytes - 1) // window_bytes
    assert link.transport is None


@pytest.mark.parametrize("fault,code", [("bad_hash", "HASH"), ("short_size", "SIZE"),
                                        ("long_size", "SIZE"), ("image", "IMAGE")])
def test_upload_bad_hash_wrong_size_or_image_never_confirms(board, fault, code):
    b, link = board(fault=fault)
    with pytest.raises(RuntimeError if fault in {"short_size", "long_size"} else ProtocolError,
                       match="negotiation" if fault in {"short_size", "long_size"} else code):
        ota.upload(link, app_image())
    assert b.version == "old" and not b.active and b.confirms == 0 and b.auths == 1


def test_interrupted_upload_not_replayed_and_fresh_upload_works(board):
    b, link = board(fault="interrupt")
    with pytest.raises(ConnectionError):
        ota.upload(link, app_image())
    assert b.auths == 1 and b.ends == 0 and not b.active
    assert b.frames == 1
    assert ota.upload(link, app_image())["confirmed"] == "1"
    assert b.auths == 3 and b.ends == 1


def test_data_timeout_closes_session_without_retry(board):
    b, link = board(fault="silent_data")
    with pytest.raises(TimeoutError):
        ota.upload(link, app_image())
    assert b.auths == 1 and b.ends == 0 and not b.active
    assert b.frames == 1
    assert link.transport is None


def test_identical_image_reinstall_verifies_the_new_slot(board):
    b, link = board()
    image = app_image()
    b.version, b.elf = ota.image_identity(image)
    assert b.slot == "ota_0"
    result = ota.upload(link, image)
    assert result["slot"] == "ota_1" and b.confirms == 1


def test_lost_end_ack_verifies_without_replay(board):
    b, link = board(fault="lost_end")
    assert ota.upload(link, app_image())["confirmed"] == "1"
    assert b.ends == 1 and b.auths == 2


def test_byte_count_mismatch_aborts(board):
    b, link = board(fault="bad_count")
    with pytest.raises(RuntimeError, match="byte count"):
        ota.upload(link, app_image())
    assert not b.active and b.ends == 0


def test_rollback_reports_real_failure(board):
    b, link = board(fault="rollback")
    with pytest.raises(RuntimeError, match="OTA failed.*rollback"):
        ota.upload(link, app_image(), verify_timeout=3)
    assert b.confirms == 0


def test_health_timeout_is_unknown_and_does_not_claim_rollback(board):
    b, link = board(fault="health")
    result = ota.upload(link, app_image(), verify_timeout=3)
    assert result["confirmed"] == "0" and result["outcome"].startswith("unverified")
    assert b.confirms == 0


def test_wait_confirm_observes_trial_timeout_and_rollback(board):
    b, link = board(fault="health")
    with pytest.raises(RuntimeError, match="OTA failed.*timeout"):
        ota.upload(link, app_image(), wait_confirm=True)
    assert b.confirms == 0 and b.ota_last.startswith("rollback")


def test_wait_confirm_retries_transient_health_failure(board):
    b, link = board(fault="transient_health")
    assert ota.upload(link, app_image(), wait_confirm=True)["confirmed"] == "1"
    assert b.confirms == 1


def test_no_wait_reports_trial_without_claiming_confirmation(board):
    b, link = board(fault="health")
    result = ota.upload(link, app_image(), wait_confirm=False)
    assert result["outcome"] == "trial_unconfirmed" and result["confirmed"] == "0"


def test_same_version_different_build_is_distinguished(board):
    b, link = board()
    b.version = "0.2.0"
    assert ota.upload(link, app_image())["elf"] != "0" * 64
    assert b.confirms == 1


@pytest.mark.parametrize("mutation", ["short", "large", "magic", "chip", "descriptor", "version", "project"])
def test_bad_artifacts_rejected_before_network(board, mutation):
    b, link = board()
    image = bytearray(app_image())
    if mutation == "short": image = image[:100]
    elif mutation == "large": image = b"\0" * (ota.SLOT_SIZE + 1)
    elif mutation == "magic": image[0] = 0
    elif mutation == "chip": image[12] = 0
    elif mutation == "descriptor": image[32] = 0
    elif mutation == "version": image[48] = 32
    elif mutation == "project": image[80] = ord("x")
    with pytest.raises(ValueError): ota.upload(link, bytes(image))
    assert b.auths == 0


def test_cli_token_file_and_port_without_phone_construction(board, tmp_path, monkeypatch, capsys):
    b, _ = board()
    token = tmp_path / "board-token"
    token.write_text(b.token + "\n")
    image = tmp_path / "firmware.bin"
    image.write_bytes(app_image())
    monkeypatch.setattr(ota, "TOKEN_PATH", token)
    assert main(["ota", str(image), "--host", "memory-board", "--port", "7878"]) == 0
    output = capsys.readouterr()
    assert '"confirmed": "1"' in output.out and "100%" in output.err
    assert b.token not in output.out + output.err


def test_cli_missing_token_is_clear_and_no_network(board, tmp_path, monkeypatch, capsys):
    b, _ = board()
    image = tmp_path / "firmware.bin"
    image.write_bytes(app_image())
    monkeypatch.setattr(ota, "TOKEN_PATH", tmp_path / "absent")
    assert main(["ota", str(image), "--host", "memory-board"]) == 1
    assert "No such file" in capsys.readouterr().err and b.auths == 0


@pytest.mark.parametrize("identical", [False, True])
def test_legacy_nine_character_elf_reaches_status_before_120s(board, identical):
    b, link = board(binary=False, short_elf=True)
    image = app_image()
    if identical: b.version, b.elf = ota.image_identity(image)
    result = ota.upload(link, image)
    assert result["confirmed"] == "1" and b.confirms == 1 and b.clock() < 120
    assert result["slot_before"] == "ota_0" and result["slot"] == "ota_1"
    assert result["fw_before"] == ("0.2.0" if identical else "old")


@pytest.mark.parametrize("chip", ["esp32c3", "esp32c5"])
@pytest.mark.parametrize("chunk,window", [(4096, 2), (8192, 4)])
def test_binary_large_image_has_one_ack_per_window_and_exact_hash(board, chip, chunk, window):
    b, link = board(chip=chip, binary_chunk=chunk, binary_window=window)
    image = app_image(payload=bytes(range(256)) * 5294, chip=chip)
    result = ota.upload(link, image)
    assert b.data == image and result["confirmed"] == "1"
    assert b.frames == (len(image) + chunk - 1) // chunk
    assert b.windows == (len(image) + chunk * window - 1) // (chunk * window)
    assert all(op != "DATA" for command, args in b.commands if command == "OTA" for op in args[:1])


def test_cli_real_rollback_returns_nonzero(board, tmp_path, monkeypatch, capsys):
    b, _ = board(fault="rollback")
    token = tmp_path / "token"; token.write_text(b.token)
    image = tmp_path / "app.bin"; image.write_bytes(app_image())
    monkeypatch.setattr(ota, "TOKEN_PATH", token)
    assert main(["ota", str(image), "--host", "memory-board", "--wait-confirm"]) == 1
    assert "OTA failed" in capsys.readouterr().err



def test_unexpected_target_image_fails_without_confirmation(board):
    b, link = board(fault="identity")
    with pytest.raises(ota.OTAUpdateError) as error:
        ota.upload(link, app_image())
    assert error.value.report["outcome"] == "identity_mismatch"
    assert error.value.report["fw_before"] == "old" and error.value.report["slot_after"] == "ota_1"
    assert b.confirms == 0


def test_delayed_reboot_does_not_send_confirming_status_to_old_slot(board):
    b, link = board(fault="delayed_boot")
    assert ota.upload(link, app_image())["confirmed"] == "1"
    assert b.confirms == 1 and b.clock() < 120
    assert sum(command == "STATUS" for command, args in b.commands) == 2


@pytest.mark.parametrize("board_chip,image_target", [
    ("esp32c3", "esp32c5"), ("esp32c5", "esp32c3"),
    (None, "esp32c3"), ("esp32", "esp32c5"),
])
def test_chip_mismatch_refused_before_status_or_ota(board, board_chip, image_target):
    b, link = board(chip=board_chip)
    b.pending = True
    b.trial_at = b.clock()
    with pytest.raises(ValueError, match=f"image targets {image_target}, board reports"):
        ota.upload(link, app_image(chip=image_target))
    assert [command for command, _ in b.commands] == ["PING"]
    assert b.confirms == b.aborts == 0 and not b.active and not b.data


def test_unsupported_chip_header():
    image = bytearray(app_image())
    struct.pack_into("<H", image, 12, 0xffff)
    with pytest.raises(ValueError, match="unsupported application chip ID 0xffff"):
        ota.image_identity(image)
