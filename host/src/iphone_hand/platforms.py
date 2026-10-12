"""Platform dispatch: Linux (systemd + /proc) stays the default; macOS uses launchd, pgrep and lsof.

Every OS interaction is injectable so tests never touch real launchctl, pgrep, lsof or /proc.
"""
import os
from pathlib import Path
import subprocess
import sys

LINUX = 'linux'
MACOS = 'macos'

# Matches deploy/macos/local.iphone-hand.uxplay.plist.
LAUNCHD_UXPLAY_LABEL = 'local.iphone-hand.uxplay'


def current_platform():
    if sys.platform.startswith('linux'):
        return LINUX
    if sys.platform == 'darwin':
        return MACOS
    return sys.platform


def check_gstreamer_path(path):
    """The GStreamer pipeline receives a string; reject characters interpreted as pipeline syntax."""
    if any(c.isspace() or c in '"\'!\\' for c in str(path)):
        raise ValueError('runtime path contains unsupported GStreamer characters')
    return path


def default_frames_dir(platform=None, environ=None):
    """Where UxPlay writes JPEG frames when daemon.toml does not set frames_dir.

    Linux keeps the systemd tmpfs under $XDG_RUNTIME_DIR (identical to the original
    daemon.runtime_frames). macOS uses ~/Library/Caches: $TMPDIR is per-boot random and
    cannot be expressed in a LaunchAgent plist; see deploy/macos/README.md.
    """
    platform = current_platform() if platform is None else platform
    environ = os.environ if environ is None else environ
    if platform == LINUX:
        runtime = environ.get('XDG_RUNTIME_DIR')
        if not runtime:
            raise ValueError('XDG_RUNTIME_DIR is required for tmpfs AirPlay frames')
        return check_gstreamer_path(Path(runtime)/'iphone-hand/frames')
    if platform == MACOS:
        home = environ.get('HOME')
        if not home:
            raise ValueError('HOME is required for the AirPlay frames cache')
        return check_gstreamer_path(Path(home)/'Library/Caches/iphone-hand/frames')
    raise ValueError(f'no default frames dir on platform {platform!r}; set frames_dir in daemon.toml')


def resolve_frames_dir(configured=None, *, platform=None, environ=None):
    if configured is not None:
        return check_gstreamer_path(Path(configured))
    return default_frames_dir(platform, environ)


def launchd_service_state(label=LAUNCHD_UXPLAY_LABEL, *, uid=None, runner=subprocess.run):
    """Read the LaunchAgent only; never load/unload it. None means the service is not loaded."""
    uid = os.getuid() if uid is None else uid
    try:
        result = runner(['launchctl', 'print', f'gui/{uid}/{label}'],
                        capture_output=True, text=True, timeout=0.5, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode:
        return None
    fields = {}
    for line in result.stdout.splitlines():
        key, sep, value = line.strip().partition(' = ')
        if sep:
            fields[key] = value
    state = fields.get('state')
    pid = fields.get('pid', '0')
    # Same shape as airplay.uxplay_unit_state; launchd does not report a restart count.
    return {'LoadState': 'loaded',
            'ActiveState': 'active' if state == 'running' else (state or 'inactive'),
            'MainPID': pid if pid.isdecimal() else '0',
            'NRestarts': None}


def find_pid_by_name(names=('uxplay.bin', 'uxplay'), *, uid=None, runner=subprocess.run):
    """Fallback PID lookup without /proc: this user's uxplay process via pgrep."""
    uid = os.getuid() if uid is None else uid
    for name in names:
        try:
            result = runner(['pgrep', '-x', '-u', str(uid), name],
                            capture_output=True, text=True, timeout=0.5, check=False)
        except (OSError, subprocess.TimeoutExpired):
            return None
        if result.returncode == 0:
            for line in result.stdout.splitlines():
                if line.strip().isdecimal():
                    return int(line.strip())
        elif result.returncode != 1:  # 1 means "no processes matched"
            return None
    return None


def established_tcp_lsof(pid, *, runner=subprocess.run):
    """True when process `pid` holds an ESTABLISHED TCP socket (macOS lsof), None if unknown."""
    try:
        result = runner(['lsof', '-nP', '-a', '-p', str(pid), '-iTCP', '-sTCP:ESTABLISHED'],
                        capture_output=True, text=True, timeout=1.0, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode == 0:
        return 'ESTABLISHED' in result.stdout
    if result.returncode == 1:
        return False
    return None


def tcp_probe(platform=None):
    platform = current_platform() if platform is None else platform
    if platform == MACOS:
        return established_tcp_lsof
    from .airplay import established_tcp
    return established_tcp


def receiver_observers(platform=None):
    """Keyword arguments for ExternalUxPlayReceiver. Empty on Linux: behaviour is unchanged."""
    platform = current_platform() if platform is None else platform
    if platform == MACOS:
        return {'unit_lookup': launchd_service_state, 'pid_lookup': find_pid_by_name,
                'tcp_probe': established_tcp_lsof, 'service': LAUNCHD_UXPLAY_LABEL}
    return {}
