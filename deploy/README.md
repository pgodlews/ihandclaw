# Headless prototype deployment

Read: SPEC §6/§8 and its v0.2 facts are the deployment contract. No hardware or target-host deployment was performed while building this prototype. FastAPI + uvicorn reuse the existing MCP SDK's Starlette ASGI implementation on one port, with one token middleware. RapidOCR 3.10 with ONNX Runtime is the optional `ocr` extra (using bundled default models); badge reading uses NumPy/OpenCV already required by the host.

On Ubuntu 24.04, install:

```sh
sudo apt install uxplay avahi-daemon gstreamer1.0-plugins-good gstreamer1.0-plugins-bad gstreamer1.0-libav python3.12 python3.12-venv
```

Place this checkout at `~/iphone-hand` (or adjust the unit's WorkingDirectory and ExecStart). Install uv, then:

```sh
cd ~/iphone-hand/host
uv sync --python 3.12 --extra ocr
mkdir -p ~/.config/iphone-hand ~/.config/systemd/user
chmod 700 ~/.config/iphone-hand
```

Create `~/.config/iphone-hand/daemon.toml` locally, mode 600. A real HTTP token **must** be supplied: generate one with `python3 -c 'import secrets; print(secrets.token_urlsafe(32))'` and paste it into the `token` field. Do not store the resulting file in git. This schema lists empty secrets deliberately; it refuses to start unchanged:

```toml
token = ""
bind = "iphone-hand.local"
port = 8790
link = "serial:/dev/ttyACM0"
link_token = ""
uxplay_name = "iPhone Hand"
receiver = "external"
home_action = "cmd+h"
pointer_recover_key = "" # opt in with "esc"; may end editing/cancel searches or dialogs
vc_offset_x = 10.0
vc_offset_y = 12.0
# screen_pt = [390, 844]  # portrait points; needed over AirPlay for Face ID iPhones other than 375x812
```

Configure `bind` to a name/address assigned to this host. Optional `frames_dir` (absolute path) overrides where UxPlay writes JPEG frames; the default is `$XDG_RUNTIME_DIR/iphone-hand/frames` on Linux and `~/Library/Caches/iphone-hand/frames` on macOS (see `deploy/macos/README.md` for the macOS launchd variant). `iphone-hand.local` is only a placeholder; do not use the board's hostname as the daemon bind. Serial users need permission to access their actual board port (typically membership of `dialout`, followed by a fresh login). For Wi-Fi, change `link` to `tcp://iphone-hand.local:7878` and supply a **separate board token** in `link_token`. This branch lazily imports `iphone_hand.tcp_link.TcpLink`; install/merge that implementation before choosing TCP. The factory does not open either transport at import time. Read: in child mode, optional `allow_device_id` adds `-restrict -allow` to UxPlay; obtain and configure the value privately on the target. Read: in external mode, receiver arguments belong to the separate unit (see below). Unknown configuration keys are rejected.

Read: `screen_pt` (optional) fixes the phone's point size; without it the daemon detects it from frames and reports it in `/api/state` `screen`, keeping 375x812 when AirPlay frames are ambiguous (SPEC section 9). `device = "ipad"` and `pointer_detector = "ipados"` exist as untested groundwork (docs/ipad.md).

Read: `pointer_recover_key = "esc"` enables one key press after pointer reveal/homing fails, then waits the motion settle delay and retries detection/reveal/homing once. Empty string (default) disables recovery. Phone/Motion also default to off. Successful results and action logs include `recovered_with`; failed recovery returns HTTP 503 with `error_class = "PointerHidden"` and a fixed editing-focus hint, plus the attempted key when enabled. Measured (user observations, 2026-10-12): Esc restored the hidden pointer in three editing fields, ending editing; it may cancel a search or dismiss a dialog. This is an opt-in workaround, not an Apple-documented guarantee.

Read: install UxPlay 1.73.6 and the wrapper as described below **before** starting the receiver.

```sh
cp ../deploy/iphone-hand.service ../deploy/iphone-hand-uxplay.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now iphone-hand-uxplay.service iphone-hand.service
sudo loginctl enable-linger "$USER"
systemctl --user status iphone-hand.service
journalctl --user -u iphone-hand.service -f
journalctl --user -u iphone-hand-uxplay.service -f
```

Read: linger keeps the user manager running without an interactive login. Its `XDG_RUNTIME_DIR` supplies the tmpfs frame directory `iphone-hand/frames`; the daemon refuses to start without that environment variable. The receiver unit creates that directory with mode 700, writes to its own journal (`iphone-hand-uxplay`), restarts with `Restart=always` and a one-second delay, retains five JPEG files, and deletes previous-generation frames only when the receiver starts. Audio is disabled and decoding is CPU-only. Its command matches the current `UxPlaySupervisor.command()` defaults, including the persistent key and JPEG pipeline; `%%05d` in the unit becomes `%05d` in UxPlay's argument.

Read: `receiver = "external"` is the default. The daemon observes `MainPID`, `ActiveState`, and `NRestarts` using `systemctl --user show`; if the unit cannot be queried, it scans `/proc` for this user's `uxplay.bin`. Connection detection still checks the process's ESTABLISHED TCP sockets, so a static phone screen remains mirroring. `/api/state.receiver` includes `running`, `active`, `active_state`, `pid`, and `restarts`; fallback scanning leaves unit-only fields unknown (`null`). Lookups are cached for at most half a second. The page covers the image with the Screen Mirroring instructions when mirroring is false.

Read: the daemon has `Wants=`/`After=` on the receiver, with no `PartOf=`/`BindsTo=` coupling. For normal daemon redeploys, update the daemon code and unit, run `systemctl --user daemon-reload`, then `systemctl --user restart iphone-hand.service`. Do not restart the receiver as part of that operation. Measured on the Ubuntu target (user-supplied, 2026-10-10): restarting the old child receiver drops mirroring and iOS does not reconnect automatically. Estimated: this split should preserve the existing connection across daemon redeploys; no target validation was performed for this prototype. Migrating from child mode requires one receiver stop/start and the owner must re-select it on the iPhone.

Read: the external unit defaults to the name `iPhone Hand`. For a custom `uxplay_name` or private device restriction, use `systemctl --user edit iphone-hand-uxplay.service` and replace `ExecStart` (first clear it with an empty `ExecStart=`) with the supplied command, changing `-n` and optionally appending `-restrict -allow <private-device-id>`. Keep `uxplay_name` in daemon.toml matched to the unit name so the page gives the correct instructions. Config changes alone do not alter an external receiver. Restart the receiver explicitly to apply its arguments, then re-select it on the phone.

Read: for tests/dev, `receiver = "child"` keeps the original supervisor, argument configuration, and exponential restart backoff (1–30 seconds). To use it on the deployed host, stop both units, disable the external receiver, and edit the installed daemon unit's `Wants=` and `After=` lines to omit `iphone-hand-uxplay.service` and run `systemctl --user daemon-reload` before starting the daemon. Never run child and external receivers against the same frame directory.

Firewall: permit the configured HTTP port only for trusted clients. AirPlay also uses **dynamic TCP/UDP ports and mDNS**; opening only the HTTP port is insufficient. UxPlay device restriction is recommended where available. This prototype provides plain HTTP; use a trusted LAN or a TLS reverse proxy for remote access. It sets no CORS headers and refuses cross-origin browser requests. The SDK's localhost-only host guard is replaced by the application's token and same-origin middleware, so configured LAN bindings work.

Open the daemon URL with `?token=` containing the configured HTTP token. The page stores it in sessionStorage and uses bearer auth for fetches; image/MJPEG requests use a query token. The initial token-bearing URL supports reload; query tokens can remain in browser history, so use a private browser session if appropriate. Uvicorn access logging is disabled and responses use `Cache-Control: no-store` and `Referrer-Policy: no-referrer`. Do not enable proxy query-string logging. `/`, every `/api` path, `/mcp`, unknown paths, and method errors are authenticated first. `/mcp` accepts the official MCP streamable-HTTP transport without a trailing-slash redirect; SDK sessions are stateless and return JSON.

REST API: the SPEC GETs and action POSTs are implemented. Additional POSTs `/api/pause` (`{"paused":true}`), `/api/wait_for`, `/api/scroll_until`, and `/api/run_shortcut` expose the pause switch and bounded skills. Action bodies use Phone parameter names (`x`, `y`, etc.); `type` uses `text`, `launch` uses `app`, `tap_element` uses `id`, `tap_text` uses `text` and optional `nth`. All successful action responses contain `result` and post-action `state`; the latter includes a bounded in-memory action log. Failed actions are also logged. Text, combos, names, tokens, board peers, and raw exception messages are omitted from logs. `GET /api/elements` returns the SPEC list of UIElement objects; `X-Pointer-Pt`, `X-VC-Tap-Offset-Pt`, `X-OCR-Available`, and `X-Elements-Latency-Ms` headers report metadata; `annotate=1` returns a JPEG of that analyzed frame.

Estimated: badges are found by neutral dark surrounds and white numeral geometry; digit matching uses synthetic Arial templates, not fixture crops. This is a heuristic detector for this VC appearance, not an accessibility tree. VC bounds are estimated local item regions; OCR text is merged by containment. Bounds use droidclaw’s `[left,top][right,bottom]` string, with an additional `bounds_pt` numeric array; center and size are two-number arrays. Read: this matches droidclaw’s upstream UIElement interface in src/sanitizer.ts. Boolean accessibility fields are defaults, not OS-reported facts. Tap centers are offset 10 pt right and 12 pt below the badge's bottom-right by default, configurable above, and need target tuning. OCR-only text centers may not be tappable. Back uses an estimated left-edge swipe. Shortcut launching through Spotlight is best effort and depends on the phone's search results.

Typing, including app/shortcut names, fails closed without working OCR or when OCR reports Password, Passcode, Hasło, or Kod. It cannot recognize every language/secure prompt. Pause is shared by HTTP and MCP and checked between reports, including ongoing drags and skills. It allows held-button releases; it cannot cancel a MOVE/CLICK already queued in firmware. Pause is not a physical emergency stop, and it does not shut down viewing. Wait/scroll skills stop on pause; timeouts bound polling but cannot interrupt an already running OCR/link call. Stale mirroring blocks actions. OCR errors and missing frames are reported without sending text.

Tests are hardware/network-free:

```sh
uv sync --python 3.12
uv run pytest
```

Read: tests inject fake OCR, serial, screens, receiver unit/PID lookups, and a local fake receiver subprocess. Existing CLI and stdio MCP remain available. `uv sync --extra ocr` additionally enables an explicit local fixture OCR benchmark; runtime OCR does not need a cloud service.

Read: SPEC §6 still requires a child receiver and frame-age mirroring detection, conflicting with this requested prototype and the measured static-screen behavior; SPEC was not edited. Proposed SPEC clarifications: define `/api/pause`, element metadata headers and annotation content type, guessed accessibility flags, OCR fail-closed typing, pause semantics for already queued reports, and the link status fields guaranteed by TcpLink. Give elements frame identity/expiry semantics before relying on OCR ids across changing screens; specify how shortcut execution is confirmed.

## UxPlay version (measured 2026-10-10)

Ubuntu 24.04's `uxplay` 1.68 stopped decoding an iPhone XS on iOS 18.7.10 after the phone restarted
(every slice reported as `broken/invalid nal`, no frames). UxPlay **1.73.6** built from source works.
Read: the external unit requires `~/.local/bin/uxplay`; child mode prefers it when present:

```sh
sudo apt install cmake build-essential pkg-config libssl-dev libplist-dev libavahi-compat-libdnssd-dev \
  libgstreamer1.0-dev libgstreamer-plugins-base1.0-dev
git clone https://github.com/FDH2/UxPlay.git && cd UxPlay && git checkout v1.73.6
mkdir build && cd build && cmake .. -DCMAKE_INSTALL_PREFIX=$HOME/.local && make -j && make install
```

The receiver key is kept in `~/.config/iphone-hand/uxplay.pem` so its identity survives restarts.
Install the 1.73.6 binary as `~/.local/bin/uxplay.bin` and `deploy/uxplay-wrapper.sh` as
`~/.local/bin/uxplay`. **Measured 2026-10-10:** run straight from the daemon (output to the journal
pipe, no GST_DEBUG), UxPlay 1.73.6 received mirroring data (13.8 MB) but wrote no frames; every run
with line-buffered output and `GST_DEBUG=2` produced frames. The root cause is not understood yet; keep
the wrapper until it is.

iOS sends no frames while the screen is still; "mirroring" therefore means "UxPlay has an open client
connection", and the newest frame stays valid while it does.
