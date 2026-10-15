# Mac host prototype

Read (implementation): Python >=3.11 uv project. `iphone_hand.phone.Phone`
implements the section 3 API for MCP and future local-agent clients. Constructors,
imports and help are inert; a real operation opens only the device it needs.

From `host/`:

```sh
uv sync
uv run pytest
uv run iphone-hand --fake status
uv run iphone-hand --fake --fixture ../fixtures/screens/home-pointer-notes.png shot /tmp/phone.png
uv run iphone-hand-mcp --fake
```

Read (implementation): `uv.lock` remains local and ignored because it embeds
registry hostnames. Direct dependencies are numpy, Pillow, OpenCV headless,
pyserial and the official `mcp` SDK (v1 API, `<2`); pytest is a development dependency.

Read (interface): commands are `status`, `shot OUTPUT`, `pointer`, `move X Y`,
`tap X Y`, `type TEXT`, `key COMBO`, and `calibrate`. Connection options go before
the subcommand. `--fake` always uses the static fixture/call-recording FakePhone;
the accelerated protocol/vision rig is `FakeSerialDevice` + `SimulatedScreen` +
the real `Phone`, as exercised in the tests.

Read (device configuration): for authorised manual hardware use, set
`IPHONE_HAND_PORT` and `IPHONE_HAND_ADB_SERIAL`, or pass `--port` / `--adb-serial`.
Serial auto-detection requires exactly one matching USB modem port. An unset ADB
selector delegates selection to ADB (multiple connected devices produce an error).
`--capture raw` selects raw frames; PNG is the default. `--home-action button3`
selects the AssistiveTouch middle-button mapping instead of the default `cmd+h`.
The mapping and iOS settings must be configured by the owner.

```python
from iphone_hand.fake import FakeSerialDevice, SimulatedScreen
from iphone_hand.phone import Phone
from iphone_hand.serial_link import SerialLink

device = FakeSerialDevice()
with Phone(SerialLink(transport=device), SimulatedScreen(device),
           settle=0, persist_calibration=False) as phone:
    print(phone.move_to(100, 200))
    image = phone.screenshot()  # PIL image at 2 px/pt: 750 x 1624 for 375 x 812
    print(phone.geometry().to_dict())  # point size of the latest frame (SPEC section 9)
```

Read (MCP): launch `uv run iphone-hand-mcp` from `host/` for stdio. The eleven
tools in SPEC are exposed, with coordinates in phone points. `screenshot` returns
a PNG image block and a text block containing pointer metadata; its optional cyan
mark is applied to the returned image only. MCP wrappers use the shared Phone.
An optional `screenshot_with_pointer()` extension supplies atomic metadata from
the same frame while preserving the exact no-argument `screenshot()` API.

Read (serial design): commands are serialized, assigned wrapping IDs 1..999999,
and bounded to 256 UTF-8 bytes including newline. Fragmented replies are matched
by ID, log lines and stale replies are ignored, and bounded EVT history is drained
during requests. Large moves/wheel actions and holds get pacing-aware deadlines.
A timeout can mean the action already executed; actions are never retried
automatically. The transport is injected for hardware-free tests.

Read (screen design): the largest non-black column band supplies the phone bounds,
with the SPEC rectangle as fallback. Bounds are half-open: measured columns
747..1412 become `[747,1413)`. Horizontal and vertical point scales are independent
to account for pixel rounding. Detection runs at native resolution; the public
crop is exactly 2 px/pt. `status()` exposes the selected mode, last end-to-end grab
time (subprocess plus decode), and the last timing for each of PNG/raw. Unused modes
remain null. Status does not capture a frame or invoke ADB.

Read (pointer design): HSV red components and their hole/complement contours
generate expected-size, near-square circle proposals. Red angular support,
a hollow neutral-grey centre, and a size prior reject solid badges and icon arcs.
Only visible pixels are scored at screen edges; arc fits recover clipped rings.
Multiple spatially distinct candidates return null. `confirm_moved` is a pure
helper for comparing candidates after an explicitly requested probe; detection
itself sends no movement. Confidence is a heuristic score, not a probability.

Read (motion design): every move sets fixed `CFG step=8 interval=15`. Unknown
positions trigger the SPEC top-left homing move, followed by detection. At most
six visual correction iterations use per-axis learned counts/pt, a margin on
long moves, and a 350 ms default settle delay. Edge targets have an interior
staging point. Clamped displacements are excluded from gain observations.
`iterations` counts corrections; initial homing is separate. A failed move returns
its final error; taps, holds, drags and positioned scrolls stop if error exceeds
3 pt. Lost/ambiguous pointers raise `MotionError`; unavailable pointers after homing
raise its `PointerHidden` subclass. Optional `pointer_recover_key="esc"` on Phone/Motion
(daemon.toml uses the same key; empty string/default is off) sends the configured
key once, waits the settle delay, then retries detection/reveal/homing. Esc can end
editing and cancel searches/dialogs. Action results and daemon logs report
`recovered_with` when used. Drag releases BTN in a `finally`
block, including when the press ACK is lost.

Estimated (initial model): counts/pt start at 1 on each axis. Observations replace
these estimates and cache them with the pacing setting in
`~/.config/iphone-hand/calibration.json`. Use `--no-cache` for an isolated session.
`calibrate` visits three interior targets and reports learned gains. The fake
device's report-speed acceleration law is synthetic and does not establish how
this phone will behave.

Read (validation scope): tests prohibit opening pyserial devices and invoking
`subprocess.run`, use both supplied fixtures, synthesize rings/backgrounds, test
raw bytes and injected capture runners, and perform an actual MCP stdio round trip
against `--fake`. No hardware validation is included. US text mapping validates
the full string before any key is sent and raises on unsupported characters.

Estimated (open risks): real iOS gain, mirror staleness, pointer appearance changes
and BLE/serial timing still need owner-authorised validation. The measured ~2.1 s
PNG grab in SPEC was supplied ground truth; raw speed has not been measured here.
A screen containing another indistinguishable red/grey ring may require motion
confirmation. A release cannot be guaranteed if the serial/BLE link is broken.

Read (contract limitation): raw parsing intentionally follows SPEC's little-endian
12-byte header and packed RGBA/RGBX; other formats and header lengths fail clearly.
Proposed SPEC clarification: document header variants/stride if future captures
show a different layout, and distinguish end-to-end grab time from wire time.
The SPEC and firmware are unchanged. OCR and the local LM Studio agent are outside
this prototype.


## OTA fix prototype

Read: `iphone-hand ota <application.bin> --host <board> --wait-confirm` installs
only an ESP32-C3/C5 application whose header chip matches the board PING. It constructs no Phone/Screen/serial link. The
existing board-token file supplies AUTH. Phases/progress go to stderr; JSON
reports firmware/slot before and after, `outcome`, `confirmed`, and `ota_last`.
`--wait-confirm` retries a trial health checkpoint; without it a failed first
checkpoint returns `trial_unconfirmed`. Loss of contact is `unverified` (exit 0),
not proof of rollback; callers must check `confirmed`. Observed rollback and
rejected uploads return nonzero. Stop other board clients during the update.

Read: new firmware uses four pipelined 8 KiB frames per cumulative ACK, with
SHA-256 and IDF validation. The first upgrade from legacy firmware uses its
168-byte base64 protocol. The CLI accepts the old nine-character ELF hash plus
the expected changed slot, permitting identical-image reinstall confirmation
before the configured 120 s timeout. See [firmware/README.md](../firmware/README.md)
and [the investigation](../docs/OTA_FIX.md).
Estimated: <60 s is the binary transfer target; no hardware speed was measured
in this session.
