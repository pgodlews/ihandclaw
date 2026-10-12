# macOS deployment (prototype)

Runs the same headless backend (UxPlay receiver + `iphone_hand.daemon`) on Apple Silicon macOS,
using launchd LaunchAgents instead of systemd user units. The Linux deployment is
unchanged; only one backend may drive the ESP32 board at a time (see "Single board client"
below). No part of this was installed, loaded or measured on the target Mac yet.

## Install prerequisites

Xcode command-line tools and CMake, then GStreamer. Read (UxPlay README,
github.com/FDH2/UxPlay): two supported options —

1. The official GStreamer `.pkg` from gstreamer.freedesktop.org (runtime **and** devel;
   upstream-recommended; unsigned packages need "Open Anyway" in Privacy & Security), or
2. Homebrew: `brew install pkg-config gstreamer`. Read (homebrew-core `Formula/g/gstreamer.rb`):
   the single `gstreamer` formula now bundles all gst-* plugins (good/bad/ugly, `libav=enabled`,
   depends on ffmpeg), so it replaces the four Debian packages from deploy/README.md and
   includes `jpegenc`, `multifilesink` and `avdec_h264`. Estimated: the README says Homebrew's
   GStreamer "seems to have everything needed for UxPlay"; verify with `gst-inspect-1.0 jpegenc`
   and `gst-inspect-1.0 avdec_h264` after install.

## Build UxPlay (no Homebrew formula)

Read: there is no `uxplay` formula or cask in Homebrew (formulae.brew.sh/formula/uxplay is 404),
so build from source — use **v1.73.6**, the version measured working with iOS 18.7 on Linux
(deploy/README.md). If disk space is tight, clone and build on another
volume, here `<SCRATCH>`:

```sh
cd <SCRATCH>
git clone https://github.com/FDH2/UxPlay.git && cd UxPlay && git checkout v1.73.6
mkdir build && cd build
cmake .. -DCMAKE_INSTALL_PREFIX=$HOME/.local
make -j && make install
```

Read (UxPlay README): macOS builds default to Apple Bonjour for DNS-SD (`-DUSE_DNS_SD=1`), which
matches the avahi role on Linux. Then install the wrapper layout (same as Linux):

```sh
mv ~/.local/bin/uxplay ~/.local/bin/uxplay.bin
cp uxplay-wrapper.sh ~/.local/bin/uxplay && chmod +x ~/.local/bin/uxplay
mkdir -p ~/.config/iphone-hand && chmod 700 ~/.config/iphone-hand
```

The wrapper substitutes `script -q /dev/null` for GNU `stdbuf -oL -eL` (macOS has no stdbuf):
running under a pty makes libc line-buffer UxPlay's stdout, which — together with `GST_DEBUG=2` —
is the measured workaround for "receives data but writes no frames" (deploy/README.md; root cause
unknown). The wrapper also creates the frames dir (mode 700) and clears previous-generation
frames on receiver start, matching the Linux unit's `RuntimeDirectory` + `ExecStartPre`.

The receiver plist adds `-vsync no`: read (UxPlay README), macOS drops many video frames with
timestamp sync and upstream recommends the no-timestamp option. Everything else matches
`UxPlaySupervisor.command()`.

## Configure the daemon

`~/.config/iphone-hand/daemon.toml`, mode 600:

```toml
token = ""            # generate: python3 -c 'import secrets; print(secrets.token_urlsafe(32))'
bind = "localhost"    # or a name assigned to this Mac
port = 8790
link = "serial:/dev/cu.usbmodemXXXX"   # see "Single board client"
uxplay_name = "iPhone Hand Mac"        # must match the plist -n; must differ from the Linux receiver's name
receiver = "external"
frame_source = "airplay"               # or "usb" (see "USB capture"); Linux is airplay-only
frames_dir = "/Volumes/iphone-hand-frames/frames"   # airplay only; matches the receiver plist
# usb_pt_scale = 3.0027                # usb only: px/pt override; 0 = derive from the frame
```

Read: on macOS the default frames dir is `~/Library/Caches/iphone-hand/frames`
(`platforms.default_frames_dir`); Linux still requires `$XDG_RUNTIME_DIR`. Unknown keys are still
rejected.

## Install the LaunchAgents

Replace `<HOME>` in both plists with the absolute home directory, then:

```sh
cp local.iphone-hand.uxplay.plist local.iphone-hand.daemon.plist ~/Library/LaunchAgents/
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/local.iphone-hand.uxplay.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/local.iphone-hand.daemon.plist
launchctl print gui/$(id -u)/local.iphone-hand.uxplay   # what the daemon reads for status
launchctl kickstart -k gui/$(id -u)/local.iphone-hand.daemon   # daemon redeploys (receiver untouched)
```

Estimated (not measured): LaunchAgents only run while the user is logged in; there is no
`loginctl enable-linger` equivalent — for headless use enable auto-login or keep a session.

## macOS AirPlay Receiver conflict

Read (Apple HT202944; UxPlay README): AirPlay uses TCP 5000/6000/7000, UDP 5353 and dynamic ports;
UxPlay without `-p` chooses ports dynamically, so there is no hard port collision, but the
built-in receiver (System Settings > General > AirDrop & Handoff > AirPlay Receiver) advertises
the same mDNS service types and holds TCP 7000. Recommendation: turn it off while running UxPlay,
and never pass bare `-p` (which pins the legacy ports including 7000). Exception: UxPlay's `-p2p`
(AWDL) mode *requires* the built-in receiver enabled — not used here.

## Firewall prompts

On first connection macOS asks whether `uxplay.bin` (and `python`/`uvicorn`, if bound beyond
localhost) may accept incoming connections; allow it for uxplay or the phone cannot connect.
The same trusted-LAN/no-TLS caveats as deploy/README.md apply.

## Single board client: serial vs TCP

Read: the ESP32 board accepts ONE authenticated TCP client; a new connection replaces the old.
Never point both the Linux host and this Mac at `tcp://<board>:7878` at once. On the Mac prefer the board's
USB serial link, `link = "serial:/dev/cu.usbmodemXXXX"` (list ports with `ls /dev/cu.usbmodem*`),
and leave TCP to the Linux host — or stop one daemon before starting the other.

## Frames directory: RAM disk default for AirPlay

Measured (Linux, 2026-10-10): UxPlay rewrites JPEGs at up to ~49 fps while the screen changes;
`max-files=5` caps the footprint at a few MB, but constant rewrites consume SSD write cycles.
The receiver plist and wrapper therefore use a tiny RAM disk, created idempotently
by the wrapper itself (same logic as `deploy/macos/ramdisk.sh`; read `man hdiutil`: `ram://N` is
N 512-byte sectors; `diskutil erasevolume` formats and mounts it). Frames live at
`/Volumes/iphone-hand-frames/frames` — set the matching `frames_dir` in daemon.toml:

```toml
frames_dir = "/Volumes/iphone-hand-frames/frames"
```

The RAM disk vanishes on reboot; the wrapper recreates it on receiver start (the daemon only
reads it). The code-level fallback default (`~/Library/Caches/iphone-hand/frames`) remains for
setups that skip the wrapper; `$TMPDIR` is unsuitable because a per-boot random path cannot be
written into a plist. To use a different location, set `frames_dir`, the plist's
`multifilesink location=`, and `IPHONE_HAND_FRAMES` together. The USB frame source
(`frame_source = "usb"`) keeps frames in memory only and needs no frames directory.

## USB capture (`frame_source = "usb"`)

QuickTime-style screen capture over the USB cable (Lightning or USB-C — both appear to macOS as
the same kind of device; [measured 2026-10-12]: iPhone XS over Lightning shows
as USB product "iPhone"). No UxPlay, no GStreamer, no frames directory: frames stay in memory.

Implementation (`host/src/iphone_hand/usb_capture.py`): set the CoreMediaIO
`kCMIOHardwarePropertyAllowScreenCaptureDevices` property on the CMIO system object (UInt32 1,
global scope; [read] OBS Studio plugins/mac-avcapture/OBSAVCapture.m does exactly this, citing
WWDC 2014 session 508; the constant exists since the OS X 10.10 SDK — [read] phracker/MacOSX-SDKs
CMIOHardwareSystem.h), find the iPhone as an `AVCaptureDevice` of muxed device type, and run an
`AVCaptureSession` + `AVCaptureVideoDataOutput` on a background GCD queue, keeping only the
newest frame. Operational quirks from the field [read, codejam.info 2025-06 "USB iPhone screen
recording in Swift"]: discovery is not instant after enabling the property (devices appear
seconds later — the backend re-discovers, throttled), a first enumeration acts as warm-up, and
setting the property repeatedly can block.

Setup:

```sh
cd <checkout>/host
uv pip install pyobjc-framework-AVFoundation pyobjc-framework-CoreMedia \
    pyobjc-framework-CoreMediaIO pyobjc-framework-CoreImage pyobjc-framework-Quartz \
    pyobjc-framework-libdispatch
uv run python ../scripts/usb_capture_probe.py --frames 60 --save /tmp/usb-frame.png
```

(Estimated: a proper `usb` extra in pyproject.toml was not added because this prototype's
environment cannot reach the package registry; add it when online.) Then set
`frame_source = "usb"` in daemon.toml. The receiver LaunchAgent is not needed in usb mode —
bootout or just ignore it; both may also coexist (UxPlay keeps working in airplay mode).

Permission (TCC): muxed iPhone devices are governed by the **Camera** permission, and the prompt
is attributed to the responsible process and only shown if its bundle has
`NSCameraUsageDescription` ([read] Apple "Requesting authorization for media capture on macOS";
t3code issue #728; anthropics/claude-code issue #82543 — without the key the request is
*silently denied*). Estimated: a bare `python` under a LaunchAgent has no Info.plist, so expect
silent denial; practical fix is a minimal signed `.app` payload with the usage description as the
LaunchAgent program (or granting Camera to the owning terminal/app and testing). This must be
validated on the target before relying on usb mode.

Phone state: [read] the phone must be unlocked and must have trusted the computer (passcode).
If it locks while streaming, the feed freezes on the last frame with no staleness signal
(Apple Feedback FB21253528) — the screen treats "device still connected" as mirroring (same rule
as AirPlay), so a locked phone serves a stale frame; re-unlock to resume.

Expected stream [read]: qvh issue #81 — default 60 fps at the device's native resolution
(iPhone XS: 1125x2436; [read] Apple HIG), wire codec H.264 decoded by the system; qvh issue #95
— real throughput is variable, often below 60 fps, use PTS. [estimated] Expect ~30-60 fps
depending on motion. Points mapping: the frame size comes from the capture; points are derived
via `usb_pt_scale` (XS: 3.0027 px/pt, measured 2026-10-12 over AirPlay) or auto-derived
(integer Retina scale + known iPhone point widths — see `_pt_size`). Open risk: 375x812 is still
hard-coded in docs/SPEC.md and the droidclaw UIElement contract (`elements.py` bounds strings),
so non-XS point spaces are only mapped, not contract-supported.

Can AirPlay mirroring and USB capture run simultaneously? Not determinable from public sources
(no statement in qvh, Apple docs, or forums; treat as unknown and test on hardware). The daemon
uses one frame source at a time regardless.

Fallback: `iphone_hand.devservices` (pymobiledevice3 DVT `screenshot()`) — read
(docs/devservices.md): single frames via Developer Mode + root tunneld, much slower than a
stream; keep it as the diagnostic path, not the realtime source.

## Status observation (what changed vs Linux)


Read (`host/src/iphone_hand/platforms.py`): on macOS the daemon reads receiver state from
`launchctl print gui/<uid>/local.iphone-hand.uxplay` instead of `systemctl --user show`, finds
the PID with `pgrep -x -u <uid> uxplay.bin` instead of scanning /proc, and probes the phone
connection with `lsof -nP -a -p <pid> -iTCP -sTCP:ESTABLISHED` instead of /proc/net/tcp. No new
Python dependency: psutil would do all three portably, but it is not in the cached environment
and the stock macOS tools above suffice. `restarts` reports `null` on macOS (launchd does not
count restarts).

## USB capture: first hardware check (measured 2026-10-12, iPhone XS on Lightning, pyobjc 12.0)

- Enabling `kCMIOHardwarePropertyAllowScreenCaptureDevices` returned OSStatus 0 (after fixing the
  qualifier argument, see `usb_capture.py`).
- Camera authorization status for the probing process was `notDetermined` (0).
- The iPhone was present on USB (ioreg) but did **not** appear as a muxed capture device within
  20 s, with the run loop spinning. Not yet determined which step is missing: Trust for this Mac
  on the phone, the one-time camera permission (granted interactively at the Mac), or the phone
  being awake at enable time. Next step needs a person at the Mac: run
  `scripts/usb_capture_probe.py`, approve the camera prompt and the Trust prompt on the phone.
