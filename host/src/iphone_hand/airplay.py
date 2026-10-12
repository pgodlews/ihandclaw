"""UxPlay observation, optional child lifecycle and safe full-frame JPEG capture."""
import asyncio
import os
from pathlib import Path
import subprocess
import threading
import time

from PIL import Image, UnidentifiedImageError

from .screen import PhoneRect


class FrameUnavailable(RuntimeError):
    pass


def established_tcp(pid, *, proc=Path('/proc')):
    """True when process `pid` holds an ESTABLISHED TCP socket (Linux /proc), None if unknown."""
    try:
        inodes = set()
        process = Path(proc)/str(pid)
        for fd in (process/'fd').iterdir():
            target = os.readlink(fd)
            if target.startswith('socket:['):
                inodes.add(target[8:-1])
        for table in ('tcp', 'tcp6'):
            for line in (process/'net'/table).read_text().splitlines()[1:]:
                fields = line.split()
                if fields[3] == '01' and fields[9] in inodes:
                    return True
        return False
    except OSError:
        return None


UXPLAY_UNIT = 'iphone-hand-uxplay.service'


def uxplay_unit_state():
    """Read the user unit only; never start/stop it. None means systemd is unavailable."""
    try:
        result = subprocess.run(
            ['systemctl', '--user', 'show', UXPLAY_UNIT, '--property=LoadState',
             '--property=ActiveState', '--property=MainPID', '--property=NRestarts'],
            capture_output=True, text=True, timeout=0.5, check=False)
        if result.returncode:
            return None
        properties = dict(line.split('=', 1) for line in result.stdout.splitlines() if '=' in line)
        if properties.get('LoadState') != 'loaded':
            return None
        return properties
    except (OSError, subprocess.TimeoutExpired):
        return None


def find_uxplay_pid(proc=Path('/proc'), *, uid=None):
    """Fallback: find this user's uxplay.bin executable, ignoring wrappers and other users."""
    uid = os.getuid() if uid is None else uid
    try:
        processes = sorted((p for p in Path(proc).iterdir() if p.name.isdecimal()),
                           key=lambda p: int(p.name))
    except OSError:
        return None
    for process in processes:
        try:
            if process.stat().st_uid == uid:
                executable = os.readlink(process/'exe').removesuffix(' (deleted)')
                if Path(executable).name == 'uxplay.bin':
                    return int(process.name)
        except OSError:
            continue  # A process can exit during the scan, or deny access to its executable.
    return None


class ExternalUxPlayReceiver:
    """Read-only receiver observer, with injectable lookups for hardware-free tests."""
    def __init__(self, *, unit_lookup=uxplay_unit_state, pid_lookup=find_uxplay_pid,
                 tcp_probe=established_tcp, clock=time.monotonic, cache_for=0.5,
                 service=UXPLAY_UNIT):
        self.unit_lookup, self.pid_lookup, self.tcp_probe = unit_lookup, pid_lookup, tcp_probe
        self.clock, self.cache_for = clock, cache_for
        self.service = service
        self._snapshot = None
        self._checked_at = float('-inf')
        self._lock = threading.Lock()

    def status(self):
        # The MJPEG path polls at 10 Hz. Bound systemctl calls and serialize concurrent readers.
        with self._lock:
            now = self.clock()
            if self._snapshot is None or now-self._checked_at >= self.cache_for:
                unit = self.unit_lookup()
                if unit is not None:
                    pid = int(unit.get('MainPID', '0') or 0) or None
                    active = unit.get('ActiveState') == 'active'
                    restarts = unit.get('NRestarts', '0')  # None where the OS does not count restarts
                    self._snapshot = {'mode': 'external', 'unit': self.service,
                                      'active': active, 'active_state': unit.get('ActiveState'),
                                      'running': active and pid is not None, 'pid': pid,
                                      'restarts': None if restarts is None else int(restarts), 'error': None}
                else:
                    pid = self.pid_lookup()
                    self._snapshot = {'mode': 'external', 'unit': self.service,
                                      'active': None, 'active_state': None,
                                      'running': pid is not None, 'pid': pid, 'restarts': None,
                                      'error': 'Receiver unit status unavailable'}
                self._checked_at = now
            return dict(self._snapshot)

    def client_connected(self):
        state = self.status()
        return self.tcp_probe(state['pid']) if state['running'] else False


class AirPlayScreen:
    # Estimated: the phone scales its screen into the receiver's requested display size
    # (UxPlay -s), so frame pixels do not identify the model; only the aspect ratio does.
    frames_native = False
    # iOS sends no frames while the screen is still (measured 2026-10-10, UxPlay 1.73.6), so
    # frame age alone cannot tell "static screen" from "disconnected". When `connected` reports
    # an active AirPlay client, the newest complete frame is valid however old it is.
    def __init__(self, frames: Path, *, stale_after=2.0, clock=time.time, connected=None,
                 settle=0.2):
        self.frames = Path(frames)
        self.stale_after, self.clock = stale_after, clock
        self.connected, self.settle = connected, settle
        self.last_grab_ms = None
        self.rect = None

    def files(self):
        # Sequence numbers reset on receiver restart. mtime is the generation order.
        files = []
        for path in self.frames.glob('f*.jpg'):
            try:
                files.append((path.stat().st_mtime_ns, path.name, path))
            except FileNotFoundError:
                pass
        return sorted(files)

    def _client(self):
        return self.connected() if self.connected is not None else None

    def status(self):
        files = self.files()
        age = max(0, self.clock()-files[-1][0]/1e9) if files else None
        delta = (files[-1][0]-files[0][0])/1e9 if len(files) >= 2 else 0
        client = self._client()
        fresh = age is not None and age < self.stale_after
        mirroring = bool(files) and client if client is not None else fresh
        return {"mode": "airplay", "mirroring": mirroring, "client_connected": client,
                "frame_age_s": age, "fps": (len(files)-1)/delta if delta > 0 and fresh else 0,
                "last_grab_ms": self.last_grab_ms}

    def grab(self, *, newest=False):
        start = time.perf_counter()
        files = self.files()
        if not files:
            raise FrameUnavailable("Waiting for AirPlay frames")
        client = self._client()
        now = self.clock()
        # A file still being written is only possible for the newest one and only briefly.
        # Prefer the newest frame once it has settled (a file may still be being written for a
        # few ms); otherwise the one before it; a lone fresh frame is tried last.
        recent = list(reversed(files[-2:]))
        choices = [f for f in recent if newest or f is not files[-1] or now-f[0]/1e9 >= self.settle]
        choices += [f for f in recent if f not in choices]
        for mtime, _, path in choices:
            if not client and now-mtime/1e9 >= self.stale_after:
                continue
            try:
                with Image.open(path) as source:
                    source.load()
                    image = source.convert("RGB")
                self.rect = PhoneRect(0, 0, *image.size)
                self.last_grab_ms = (time.perf_counter()-start)*1000
                return image
            except (OSError, UnidentifiedImageError):
                continue
        raise FrameUnavailable("AirPlay frames are stale, rotating, or incomplete")


class UxPlaySupervisor:
    def __init__(self, frames, *, name="iPhone Hand", allow_device_id=None,
                 executable=None, key_file=None, tcp_probe=established_tcp,
                 process_factory=asyncio.create_subprocess_exec,
                 backoff_initial=1.0, backoff_max=30.0):
        self.frames = Path(frames)
        self.name, self.allow_device_id = name, allow_device_id
        self.tcp_probe = tcp_probe
        # UxPlay 1.68 (Ubuntu 24.04 apt) decoded iOS 18.7 mirroring as broken NALs after a
        # phone restart; 1.73.6 built into ~/.local works (measured 2026-10-10).
        local = Path.home()/'.local/bin/uxplay'
        self.executable = executable or (str(local) if local.exists() else 'uxplay')
        # A persistent key keeps the receiver identity stable across restarts.
        self.key_file = key_file or Path.home()/'.config/iphone-hand/uxplay.pem'
        self.process_factory = process_factory
        self.backoff_initial, self.backoff_max = backoff_initial, backoff_max
        self.process = None
        self.restarts = 0
        self.last_exit = None
        self.error = None
        self.task = None
        self.stopping = asyncio.Event()

    def command(self):
        args = [self.executable, '-n', self.name, '-nh', '-key', str(self.key_file), '-s', '1125x2436@60', '-fps', '60',
                '-as', '0', '-avdec', '-vs',
                f'jpegenc quality=90 ! multifilesink location={self.frames}/f%05d.jpg max-files=5']
        if self.allow_device_id:
            args += ['-restrict', '-allow', self.allow_device_id]
        return args

    async def start(self):
        self.frames.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.stopping.clear()
        self.task = asyncio.create_task(self._run())

    async def _run(self):
        delay = self.backoff_initial
        while not self.stopping.is_set():
            started = time.monotonic()
            # Prevent a restarted child's new f00000 from mixing with its predecessor.
            try:
                for path in self.frames.glob('f*.jpg'):
                    path.unlink(missing_ok=True)
                # Inherit stdout/stderr so systemd captures receiver logs in the journal.
                self.process = await self.process_factory(*self.command())
                self.error = None
                if self.stopping.is_set() and self.process.returncode is None:
                    await self._terminate(self.process)
                self.last_exit = await self.process.wait()
            except OSError:
                self.error = "UxPlay could not be started"
            finally:
                self.process = None
            if self.stopping.is_set():
                break
            self.restarts += 1
            if time.monotonic()-started > 30:
                delay = self.backoff_initial
            try:
                await asyncio.wait_for(self.stopping.wait(), timeout=delay)
            except asyncio.TimeoutError:
                pass
            delay = min(self.backoff_max, delay*2)

    async def stop(self):
        self.stopping.set()
        process = self.process
        if process and process.returncode is None:
            await self._terminate(process)
        if self.task:
            await self.task
            self.task = None

    @staticmethod
    async def _terminate(process):
        try:
            process.terminate()
            await asyncio.wait_for(process.wait(), timeout=5)
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()
        except ProcessLookupError:
            pass

    def client_connected(self):
        process = self.process
        if process is None or process.returncode is not None or not getattr(process, 'pid', None):
            return False
        return self.tcp_probe(process.pid)

    def status(self):
        return {"running": self.process is not None and self.process.returncode is None,
                "restarts": self.restarts, "last_exit": self.last_exit, "error": self.error}
