# How the agent sees the phone: frame sources and their trade-offs

The input side (taps, typing, keys) is always the ESP32 acting as a Bluetooth keyboard and mouse.
The phone never needs a jailbreak, an app, or Developer Mode for that. What differs is how the
agent *sees* the screen. Three sources exist; pick one per deployment.

Labels: **measured** = observed on our iPhone XS (iOS 18.7.10); **read** = from docs/source;
**estimated** = not yet verified.

| | AirPlay mirror (UxPlay) | USB capture (macOS) | Developer services screenshot |
|---|---|---|---|
| Config | `frame_source = "airplay"` (default) | `frame_source = "usb"` | `iphone_hand.devservices` (separate module) |
| Host | Linux or macOS | macOS only | Linux or macOS |
| Fully headless (no monitor, no logged-in desktop) | **Yes on Linux** (systemd user unit + linger; measured on our Ubuntu host). macOS: LaunchAgent needs a logged-in user (read) | **No** - needs a logged-in macOS user and a one-time camera permission grant at the Mac (estimated; see risks) | Yes (read), but needs a root tunnel service |
| Phone setup | Start Screen Mirroring to the receiver from Control Centre | Lightning or USB-C cable, tap Trust once, keep unlocked | **Developer Mode on**, cable/pairing, root `tunneld` |
| Developer Mode needed | **No** | **No** (QuickTime-style capture; read) | **Yes** |
| Cable needed | No (same LAN) | Yes | Yes for pairing; Wi-Fi possible after (read) |
| Frame rate / size | ~49 fps while the screen changes, 1126x2434 JPEG (measured) | 30-60 fps at native resolution (estimated, not yet measured) | single frames on request, slow (read) |
| Extra info | - | - | accessibility tree without geometry, app launch (measured) |
| Known issues | Phone sends no frames while the screen is still (measured, handled); receiver must not be restarted mid-session or the phone has to re-select it (measured) | Locking the phone freezes the stream with no signal (read); camera permission for a background `python` may be silently denied without a signed app wrapper (estimated) | Requires sudo for the tunnel; Developer Mode warning on the phone |
| Status | **Production** on our Linux host | Implemented, **not yet tested on hardware** (`scripts/usb_capture_probe.py`) | Prototype (docs/devservices.md) |

## Which one to use

- **Headless box (recommended):** Linux + AirPlay. Nothing to install on the phone, no
  Developer Mode, no cable, no desktop session. The only manual step is starting Screen
  Mirroring on the phone after it reboots or the receiver restarts.
- **Laptop on a desk, phone on a cable:** macOS + USB capture. No Wi-Fi mirroring to keep alive,
  but the Mac has to stay logged in and the capture permission has to be granted once at the Mac.
- **Need the accessibility tree:** add developer services on top of either, accepting Developer Mode.

The board link is independent of the frame source: Wi-Fi TCP or USB serial on any host. The
board accepts one controlling client at a time, so only one backend can drive a given phone.

## Privacy note

All three sources see everything on the phone screen, including notifications and the device
serial number on Settings > General > About. Benchmark runs save frames under `runs/`, which is
git-ignored; do not publish run folders without checking them.
