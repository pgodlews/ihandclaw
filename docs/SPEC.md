# iphone-hand: spec v0.1 (2026-10-09)

Let an AI agent operate a real iPhone with a keyboard and a mouse, and see the result.

```
agent ⇄ MCP server (Mac, host/) ──USB serial──> ESP32-C5 (firmware/) ──BLE HID──> iPhone XS
          ↑                                                                         │ AirPlay mirror
          └── adb exec-out screencap ←── Wormhole Display on Meta Portal+ ←─────────┘
```

## Hardware and measured facts

| Fact | Value | Source |
|---|---|---|
| Phone | iPhone XS, iOS 18, Lightning (no USB-C) | owner |
| Phone screen | 375 × 812 pt (the reference size; other sizes: section 9) | Apple spec (read) |
| Board | ESP32-C5HF4 rev v1.0, BT 5 LE only, 4 MB flash, **USB-Serial/JTAG only (no USB-OTG, no USB HID)** | esptool chip-id (measured) |
| Additional board | ESP32-C3 (QFN32) rev v0.4, single core 160 MHz, BT 5 LE, embedded 4 MB flash, 40 MHz crystal, USB-Serial/JTAG; Wi-Fi 2.4 GHz only (C5: dual band) | owner esptool findings 2026-10-12 (measured); Wi-Fi bands (read) |
| Board port on the Mac | `/dev/cu.usbmodem*` | measured |
| Toolchain | ESP-IDF v6.2.0, NimBLE, `examples/bluetooth/esp_hid_device` present | measured |
| Mirror display | Meta Portal+ Gen 2, ADB over USB, screen 2160 × 1440 | `adb shell wm size` (measured) |
| Mirrored phone image on the Portal | x 747–1412, y 0–1439 (666 × 1440 px), scale **1.7734 px per pt** (1440/812) | fixtures (measured) |
| `adb exec-out screencap -p` | ~2.1 s per frame, PNG 2160 × 1440 RGBA | measured once |
| Pointer | AssistiveTouch pointer, **red ring** border with a translucent grey centre, ~55 × 52 px on the Portal (~31 pt), auto-hide off | fixtures (measured) |
| AssistiveTouch menu button | hidden ("Always Show Menu" off) | owner |

Red distractors on a normal home screen (fixtures): calendar widget text, battery widget arc,
small red icon dot, multicolour app icon (red arc, 71 × 43 px, fill 0.42, similar to the ring's 0.44),
notification badge (42 × 41 px, solid), red app icon (80 × 56 px, solid). Colour alone is not enough.

iOS facts that shape the design (read, not yet measured on this phone):
- Mouse needs AssistiveTouch on. Movement is **relative only, with acceleration**; there is no absolute pointing.
- A BLE HID keyboard works system-wide. With Full Keyboard Access on, the whole UI is keyboard-navigable
  (Tab/arrows/Space). Cmd+Space = Spotlight. Text typing is exact.
- Mouse buttons 2/3 can be mapped to actions (Home, App Switcher...) in AssistiveTouch → Custom Actions.

## 1. Serial protocol (host ⇄ firmware)

USB-Serial/JTAG, 115200 baud nominal (the port ignores baud), UTF-8, `\n`-terminated lines.
ESP_LOG output shares the port, so **protocol replies start with `@`** and the host ignores every
other line. Host commands:

```
<id> <CMD> [args...]\n
```
`<id>` is a decimal 1–999999 chosen by the host. The firmware answers each command exactly once:
```
@<id> OK [key=value ...]
@<id> ERR <code> [message]
```
Error codes: `PARSE`, `UNKNOWN`, `ARG`, `NOCONN` (no BLE host connected/subscribed), `BUSY`, `INTERNAL`.
The reply for a command that sends HID reports comes **after the last report was queued to the BLE
stack**. Commands are executed strictly in order. Max line length 256 bytes.

Unsolicited events (also start with `@`, id `0`):
```
@0 EVT conn peer=<addr>         connected and encrypted
@0 EVT disc reason=<n>
@0 EVT ready                    host subscribed to input reports (HID usable)
@0 EVT boot fw=<version>
```

| Command | Args | Effect |
|---|---|---|
| `PING` | | `OK fw=<ver> chip=<esp32c5 or esp32c3>` |
| `STATUS` | | `OK conn=0/1 ready=0/1 bonded=<n> peer=<addr or ->` |
| `MOVE` | `dx dy` (int, ±32767) | relative move; firmware splits into reports of at most `step` counts per axis, one every `interval` ms |
| `CFG` | `step=<1..127> interval=<ms 5..100>` (either or both) | defaults `step=8 interval=15`; `OK step=.. interval=..` |
| `BTN` | `mask` (bit0 left, bit1 right, bit2 middle) | set held buttons (for drag) |
| `CLICK` | `[mask=1] [hold_ms=60]` | press, hold, release |
| `SCROLL` | `v [h]` (int, ±127 per report, split if larger) | wheel / AC Pan |
| `KEY` | `mods usage [hold_ms=30]` (hex or dec; mods = HID modifier byte, usage = keyboard page usage, 0 = modifiers only) | press + release |
| `KREPORT` | `mods k1 [k2..k6]` | send raw keyboard state (all zero = release) |
| `CONSUMER` | `usage [hold_ms=30]` (consumer page, e.g. 0xE9 vol+, 0x40 menu, 0x223 AC Home) | press + release |
| `UNPAIR` | | delete all bonds, restart advertising |
| `NAME` | `<name>` | persist BLE device name (NVS), restart advertising; default `iPhone Hand` |

Keyboard layout and text → key mapping live on the **host**, not in the firmware.

## 2. Firmware requirements (ESP-IDF 6.2, NimBLE, targets esp32c5 and esp32c3)

- One BLE HID device (HID over GATT) with a report map containing: keyboard (report ID 1, boot-style
  8-byte layout + LED output report), mouse (report ID 2: 3 buttons, **16-bit** relative X/Y, 8-bit
  wheel, 8-bit AC Pan), consumer control (report ID 3, 16-bit usage). Battery service and Device
  Information (PnP ID) present; iOS pairs more reliably with them.
- Appearance: generic HID (0x03C0) or keyboard; name `iPhone Hand`. Bonding on, Just Works (no IO caps),
  bonds in NVS; reconnect to a bonded phone automatically; advertise when disconnected.
- Serial protocol above on the USB-Serial/JTAG console. Reports go through a queue so the serial
  parser never blocks on BLE; `MOVE` pacing uses the `interval` timer, not busy-wait.
- Builds with `idf.py -B build/<target> set-target <target> build` (`esp32c5` or `esp32c3`) from `firmware/`. `firmware/README.md` gives build,
  flash and pairing steps.

## 3. Host (Python ≥3.11, uv project in `host/`, package `iphone_hand`)

### Modules
- `serial_link` - opens the port (auto-detect `/dev/cu.usbmodem*` or `IPHONE_HAND_PORT`), sends
  commands, matches `@<id>` replies with timeouts, collects `@0 EVT` lines, ignores log lines.
  Works against a fake device in tests.
- `screen` - grabs a frame from ADB (`IPHONE_HAND_ADB_SERIAL`), supports PNG (`screencap -p`) and raw
  (`screencap` without `-p`: 12-byte header w,h,format + RGBA) and reports grab time. Finds the mirrored
  phone rectangle automatically (largest non-black column band; fall back to the measured
  x 747–1412) and maps **Portal px ⇄ phone pt** using the screen geometry of section 9. Public
  image API returns the phone crop resized to **exactly 2 px per pt** (750 × 1624 for 375 × 812).
- `pointer` - finds the red ring: red mask (HSV) → connected components → keep near-square blobs of
  expected diameter (±35 %) whose centre is hollow/greyish and whose ring is roughly circular.
  Returns `(x_pt, y_pt, confidence)` or `None`. Optional confirm step: if more than one candidate,
  move the pointer by a known delta, grab again, pick the candidate that moved.
- `motion` - closed loop `move_to(x_pt, y_pt)`: (1) if the position is unknown, home by moving far to
  the top-left (`MOVE -3000 -3000`) and detect; (2) estimate counts per pt (per axis, learned and
  cached in `~/.config/iphone-hand/calibration.json`); (3) move, wait for the mirror (settle delay,
  default 350 ms), detect, correct; stop when error ≤ 3 pt or after 6 iterations; report final error.
  Pointer acceleration makes gain depend on speed, so moves use a fixed `CFG step/interval` and the
  gain is learned at that setting. Edge clamping: the pointer cannot leave the screen; a target near
  an edge is reached from inside.
- `keys` - text → HID usages for the US hardware layout (iOS: Settings → General → Keyboard →
  Hardware Keyboard). Unsupported characters raise a clear error. Combos parsed from strings like
  `cmd+space`, `cmd+shift+3`, `tab`, `enter`, `esc`, arrows.
- `mcp_server` - MCP stdio server.

### MCP tools (all coordinates are phone points, origin top-left; size per section 9, 375 × 812 on the reference)
| Tool | Args | Returns |
|---|---|---|
| `screenshot` | `mark_pointer: bool = true` | PNG at 2 px/pt (750 × 1624 on the reference), pointer position if found |
| `find_pointer` | | `{x, y, confidence}` or null |
| `move_to` | `x, y` | final pointer pos + error |
| `tap` | `x, y, button = "left"` | `move_to` then `CLICK`; final pos + error |
| `long_press` | `x, y, ms = 800` | |
| `drag` | `x1, y1, x2, y2, steps = 10` | `move_to`, `BTN 1`, paced moves, `BTN 0` |
| `scroll` | `dy, dx = 0, x?, y?` | wheel units |
| `type_text` | `text` | |
| `key` | `combo` (e.g. `cmd+space`, `enter`) | |
| `home` | | configurable: default `KEY` for `cmd+h`, fallback mouse button 3 |
| `status` | | serial link, BLE conn/ready, ADB device, last grab ms, `screen` geometry (section 9) |

CLI for manual testing: `iphone-hand status|shot out.png|pointer|move X Y|tap X Y|type TEXT|key COMBO|calibrate`.

### Python API shared by the MCP server and the local agent
`iphone_hand.phone.Phone` is the one object both front ends use (MCP tools are thin wrappers):
```python
class Phone:
    def screenshot(self) -> "PIL.Image.Image": ...          # 2 px/pt (750 x 1624 on the reference)
    def find_pointer(self) -> tuple[float, float, float] | None
    def move_to(self, x: float, y: float) -> dict           # {"x","y","error_pt","iterations"}
    def tap(self, x: float, y: float, button: str = "left") -> dict
    def long_press(self, x: float, y: float, ms: int = 800) -> dict
    def drag(self, x1: float, y1: float, x2: float, y2: float, steps: int = 10) -> dict
    def scroll(self, dy: int, dx: int = 0, x: float | None = None, y: float | None = None) -> None
    def type_text(self, text: str) -> None
    def key(self, combo: str) -> None
    def home(self) -> None
    def status(self) -> dict
```
`iphone_hand.fake.FakePhone` implements it without hardware (static fixture image, records calls) for tests.

## 5. Local driver model (LM Studio)

Besides Claude via MCP, a small local vision model drives the phone through the same `Phone` API:
`iphone-hand agent "open Settings and turn on Dark Mode"`.

- OpenAI-compatible chat completions with tool calling and images, default
  `base_url=http://localhost:1234/v1`, model `google/gemma-4-26b-a4b` (both overridable by flag/env).
  Example models (read, 2026-10-09): gemma-4-26b-a4b (+ QAT), gemma-4-e4b/e2b, gemma-4-31b, qwen3.8-27b.
- Small models are poor at pixel coordinates, so give them easier targets:
  - `tap_text(text, nth=0)`: OCR the screenshot with **macOS Vision** (pyobjc `VNRecognizeTextRequest`,
    local, no network) and tap the centre of the best fuzzy match; the OCR result list is also sent
    to the model each step as `[i] "Settings" (x,y)`.
  - Screenshot sent to the model with a light labelled grid (e.g. 6 columns × 13 rows, cells named
    A1..F13) and a `tap_cell(cell)` tool, plus raw `tap(x,y)` for models that can do it.
  - Keyboard-first hint in the system prompt: Spotlight (`cmd+space` then type the app name, `enter`)
    beats hunting for icons.
- Loop: screenshot (+ OCR list) → model → tool call(s) → execute → repeat; stop on a `done(summary)`
  tool, max 30 steps, or the model repeating the same action 3 times. Keep only the last 2 screenshots
  in context to fit small context windows. Log every step (image, OCR, model output, action) to a run
  folder for debugging.
- Guard rails in code, not only in the prompt: `--dry-run` (print actions, execute none); `--confirm`
  (ask on the terminal before each action); `type_text` refuses when OCR on screen shows a password
  or passcode prompt (`Password`, `Passcode`, or localized password prompts, e.g. Polish),
  and the agent stops.

## 4. Safety rules for the agent using it
The agent never types the phone passcode or any password, never confirms purchases or payments,
and asks the owner before sending messages or deleting anything. Unlocking is done by the owner.

---

# v0.2 additions (2026-10-10): headless daemon on a Linux host, Wi-Fi link, light UI layer

## Measured facts (2026-10-10)
| Fact | Value | Source |
|---|---|---|
| Daemon host | Ubuntu 24.04 mini PC, avahi running, same LAN as the phone | measured |
| AirPlay receiver | `uxplay` 1.68 (Ubuntu apt) + gstreamer1.0-plugins-{good,bad}, gstreamer1.0-libav | installed, measured |
| Working receiver command | `uxplay -n "<name>" -nh -s 1125x2436@60 -fps 60 -as 0 -avdec -vs "jpegenc quality=90 ! multifilesink location=<dir>/f%05d.jpg max-files=5"` | measured |
| Resulting UxPlay pipeline | `appsrc ! queue ! h264parse ! avdec_h264 ! videoconvert ! <-vs string> name=video_sink sync=true` | uxplay -d log |
| Frames from the iPhone XS | **1126 × 2434 JPEG**, ~49 fps while the screen changes, ~10 % of one core for uxplay | measured |
| Scale | full frame = phone, **3.0027 px per pt** (1126/375) | measured |
| Pointer | red ring detected by `pointer.candidates` with `PhoneRect(0,0,w,h)` | fixtures/airplay |
| Voice Control "Item Numbers" overlay | visible in the stream: dark rounded badge with a white number + small arrow at the top-left of every tappable item | fixtures/airplay (measured) |
| Stale frames | the newest file in the dir may be partly written: read the second-newest | (estimated, observed safe) |
| Board BLE gain | 0.728 pt/count straight, ~0.80 diagonal at step=8 interval=15 | measured |
| Wi-Fi on the board | ESP32-C5 dual-band Wi-Fi 6, coexists with BLE (needs IDF coexistence) | chip id (measured), coex (read) |

## 6. Daemon (`iphone_hand.daemon`, runs on the Linux daemon host as a systemd user service)

- Supervises **uxplay** as a child process (restart on exit, log to journal), name `iPhone Hand`,
  frames into a tmpfs dir (`$XDG_RUNTIME_DIR/iphone-hand/frames`), optional `-restrict -allow <deviceID>`
  from config. Knows whether a phone is mirroring (frames fresher than 2 s) and reports it.
- `AirPlayScreen` - same interface as `screen.Screen` (`grab()`, `status()`), reads the second-newest
  JPEG, phone rect = full frame. `Phone` works unchanged on top of it.
- Link to the board from config: `serial:/dev/ttyACM0` or `tcp://iphone-hand.local:7878` (+ token),
  via a small factory; `TcpLink` comes from `iphone_hand.tcp_link` (section 7) with the SerialLink API.
- HTTP server (one port, default 8790, bind address from config):
  - `GET /` web page: live view, overlay of pointer + elements (toggle), click = tap, drag = drag,
    wheel = scroll, keyboard passthrough toggle, buttons Home / Spotlight / Back, action log, status bar
    (mirroring, fps, BLE, pointer, link).
  - `GET /api/frame.jpg` (newest frame; `?scale=pt2` = 2 px per pt, 750×1624 on the reference),
    `GET /api/stream.mjpeg` (≤ 10 fps, pt2), `GET /api/state` (includes `screen`, section 9),
    `GET /api/elements`.
  - `POST /api/{tap,long_press,drag,scroll,move,type,key,home,back,launch,tap_text,tap_element}` JSON bodies,
    coordinates in pt; every action returns the post-action state and is appended to the action log.
  - `/mcp` MCP streamable-HTTP endpoint with the section-3 tools + `elements`, `tap_element`, `tap_text`, `launch`, `back`.
- **Auth**: every route needs the token from `~/.config/iphone-hand/daemon.toml` (`Authorization: Bearer`,
  or `?token=` for `<img>`/MJPEG; the web page keeps it in sessionStorage after the first `?token=` load).
  Refuse to start without a token. No CORS.
- Safety in code: `type` refuses when OCR shows a password/passcode prompt; `/api` actions can be
  globally paused from the web page (kill switch).

## 7. Board Wi-Fi link (firmware + `iphone_hand.tcp_link`)

- USB serial stays as is. New serial-only commands (never accepted over TCP):
  `WIFI <ssid> <password>` (percent-encoded, `%20` for space; stored in NVS; connects),
  `WIFI off`, `TOKEN <32-64 hex chars>` (stored in NVS). `STATUS` gains `wifi=0/1 ip=<ip or ->`; the password and token are never echoed or logged.
- When Wi-Fi is up and a token is set: TCP server on port 7878, mDNS host `iphone-hand.local`, service
  `_iphonehand._tcp`. One client at a time (a new client replaces the old one). First line must be
  `AUTH <token>` → `@0 OK auth`, else the socket is closed. After that the section-1 protocol, same replies.
  Commands from serial and TCP go through one ordered queue; replies go to the transport the command came from;
  `@0 EVT` lines go to both.
- `TcpLink(host, port, token)` mirrors `SerialLink`'s public API (`open`, `close`, `command`, `events`,
  `step`, `interval`, timeouts) and reconnects once on a dropped socket.

## 8. Light UI layer (`GET /api/elements`, for droidclaw-style agents)

Returns a list compatible with droidclaw's `UIElement` (`id, text, type, bounds, center, size, clickable,
editable, enabled, checked, focused, selected, scrollable, longClickable`), centre/bounds in **pt**:
- **Voice Control numbers** (when the overlay is on): find the badges, read the number, `id = "vc<n>"`,
  `type = "vc_item"`. Tap point = a point inside the item, just below/right of the badge (offset to be tuned on
  real screens; report it as a parameter). Badges are not tapped themselves.
- **OCR text** with RapidOCR (onnxruntime, CPU) - `id = "t<k>"`, `type = "text"`, merged with a VC item when the text
  lies inside its likely region.
- Optional later: icon detector (OmniParser YOLO) if VC numbers + OCR are not enough.
- `GET /api/elements?annotate=1` returns the frame with numbered boxes (set-of-marks) for vision models.
Skills on top: `launch(app)` = ⌘Space, type name, Enter; `home` = configurable; `back` = left-edge swipe;
`tap_text(text, nth)`; `scroll_until(text)`; `wait_for(text, timeout)`; `run_shortcut(name)` via Spotlight.

---

# v0.3 additions (2026-10-12): variable screen geometry

## 9. Screen geometry (phone points)

375 × 812 pt (iPhone XS) remains the **reference**: the default, and the size of every
fixture and measured number in this document. Other phones are described by a **geometry**.
`Phone.phone_rect(frame)` (`host/src/iphone_hand/phone.py`) is its single source, and every
point ⇄ pixel conversion, bound, pt2 size, pointer radius and element clamp uses it.

- Coordinates are points, origin top-left of the screen **as mirrored**, x to the right.
  Valid targets are `0 ≤ x ≤ w`, `0 ≤ y ≤ h`. pt2 images are exactly `2w × 2h` pixels.
- `GET /api/state` and the `status` tool include:
  ```json
  "screen": {"pt": [w, h], "px_per_pt": 3.0027, "orientation": "portrait",
             "source": "aspect", "ambiguous": true, "candidates": [[375, 812], [390, 844], ...]}
  ```
  `source` is one of `config`, `scale`, `native`, `aspect`, `estimated`, `default`. Clients
  (agents, adapters, the web page, the replay recorder) read `screen.pt`. A daemon without
  the field is the reference.
- Detection (`geometry.detect`), in order: the `screen_pt = [w, h]` daemon.toml override; an
  explicit px/pt scale (`usb_pt_scale`); exact native pixels for sources whose frames are
  unscaled (USB capture: estimated); the aspect ratio after cropping letterboxing; a @3x
  fallback for unknown shapes. A landscape frame (wider than tall) swaps the size. The device
  table is `geometry.IPHONE_SIZES`, with the sources cited there (read). Apple's HIG Layout
  page no longer carries its device table (read 2026-10-12).
- Limitation (estimated): AirPlay frames are scaled by the phone into the receiver's
  requested size, and all Face ID iPhones have aspect ratios within ~0.002 of each other. So
  over AirPlay detection reports `ambiguous`, keeps 375 × 812 when it is a candidate, and
  `screen_pt` must be set for any other Face ID iPhone. Display Zoom also changes a phone's
  point size (read, secondary source). Internal mapping stays self-consistent with a wrong
  size, but agent-visible points, tap offsets and pointer size then differ from iOS points.
- Daemon config: `screen_pt` (default none = detect), `device = "iphone"` (or `"ipad"`:
  groundwork, untested, docs/ipad.md), `pointer_detector = "assistivetouch"` (or `"ipados"`:
  prototype, untested).
- Pointer gain is calibrated **per screen size**. The calibration cache keeps the version-2
  layout for the reference (top-level keys) and stores other sizes under
  `"profiles": {"<w>x<h>": {...}}`. A new size starts uncalibrated (estimated gain, closed
  loop) until `iphone-hand calibrate` runs on that phone (docs/motion.md 5.4).
