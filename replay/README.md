# iPhone Hand - Record/Replay Simulator (`replay/`)

The **Record/Replay Simulator** enables autonomous agents (such as DroidClaw, Claude via MCP, or local LM Studio models) to be developed, evaluated, and tested offline without physical iPhone XS hardware, ESP32 BLE boards, or live AirPlay mirroring.

The simulator implements the daemon HTTP specification defined in `docs/SPEC.md` (sections 6 and 8), using the recorded phone's point coordinates and the DroidClaw `UIElement` contract. `screen_size_pt` is read from the daemon's `/api/state` `screen.pt` at the first recorded step (**375 × 812 pt** for the iPhone XS reference, and for daemons without the field). A graph never mixes screen sizes: the builder refuses sessions recorded at different sizes.

---

## Architecture

The system consists of three core components:

```
+---------------------------------------------------------------------------------------+
| 1. LIVE RECORDING (replay/recorder.py)                                                |
|                                                                                       |
|   Agent / Script ──> Recorder ──> Live Daemon (Linux) ──> Real iPhone XS              |
|                         │                                                             |
|                         ▼ Saves pre/post JPEGs, elements JSON & action manifest       |
|                 [Recording Dataset]                                                   |
+---------------------------------------------------------------------------------------+
                                  │
                                  ▼
+---------------------------------------------------------------------------------------+
| 2. GRAPH MERGER & BUILDER (replay/builder.py)                                         |
|                                                                                       |
|   Multiple Recordings ──> Perceptual Hash (DCT pHash) ──> Merged Screen Graph         |
|                           (Masks clock/battery jitter)    (Branching states & edges)  |
|                                                           & Summary Reports           |
+---------------------------------------------------------------------------------------+
                                  │
                                  ▼
+---------------------------------------------------------------------------------------+
| 3. SIMULATOR SERVER (replay/server.py)                                                |
|                                                                                       |
|   Agent / DroidClaw ──HTTP (Bearer Auth)──> Replay Server (Fake Daemon)               |
|                                                    │                                  |
|                               Matches Action?      │                                  |
|                                 ├── YES ───────────┴──> Advances to next screen node  |
|                                 └── NO (Off-path) ────> Stays on screen & logs event  |
+---------------------------------------------------------------------------------------+
```

---

## Dataset Format

Each recorded session produces a self-contained directory:

```
dataset/
├── manifest.json                # Step-by-step metadata, actions, and timings
├── frames/
│   ├── frame_0000_pre.jpg       # Frame before action (AirPlay JPEG)
│   ├── frame_0000_post.jpg      # Frame after action
│   └── ...
└── elements/
    ├── elements_0000.json       # Interactive elements (DroidClaw UIElement format)
    └── ...
```

### `manifest.json` Structure

```json
{
  "version": "1.0",
  "session_id": "session_1770000000",
  "created_at": "2026-10-10T11:00:00Z",
  "daemon_url": "http://iphone-hand.local:8790",
  "screen_size_pt": [375, 812],
  "steps": [
    {
      "step": 0,
      "timestamp": 1770000000.12,
      "action": {
        "name": "tap",
        "args": { "x": 85.0, "y": 185.0 }
      },
      "pre_frame": "frames/frame_0000_pre.jpg",
      "elements_file": "elements/elements_0000.json",
      "post_frame": "frames/frame_0000_post.jpg",
      "response": { "result": { "status": "ok" }, "state": { ... } }
    }
  ]
}
```

### UIElement Schema (SPEC Section 8)

Each element in `elements.json` provides:
- `id`: Element ID (e.g. Voice Control badge `"vc1"`, OCR text `"t0"`)
- `text`: Label or OCR text
- `type`: Element classification (`"vc_item"`, `"text"`)
- `bounds`: String representation `"[left,top][right,bottom]"` in pt
- `bounds_pt`: Float array `[left, top, right, bottom]` in pt
- `center`: Interaction point `[x, y]` in pt
- `size`: Element dimensions `[width, height]` in pt
- `clickable`, `editable`, `enabled`, `checked`, `focused`, `scrollable`, `longClickable`: Boolean flags

---

## 1. Recording Live Sessions (`replay/recorder.py`)

### Programmatic Client Usage

```python
from replay.recorder import SessionRecorder

with SessionRecorder(
    daemon_url="http://iphone-hand.local:8790",
    token="YOUR_DAEMON_TOKEN",
    output_dir="./recordings/settings_flow",
) as recorder:
    # Captures pre-frame & elements, posts action, captures post-frame, updates manifest
    recorder.record_step("tap", x=85.0, y=185.0)
    recorder.record_step("key", combo="cmd+space")
    recorder.record_step("type", text="Safari")
    recorder.record_step("key", combo="enter")
```

### Transparent Proxy Mode

You can run `recorder.py` as a reverse proxy. Configure DroidClaw or your agent to connect to `http://127.0.0.1:8791` instead of the live daemon:

```bash
uv run python -m replay.recorder \
  --daemon http://iphone-hand.local:8790 \
  --token YOUR_DAEMON_TOKEN \
  --out ./recordings/agent_session_1 \
  --proxy-port 8791
```

All `GET` requests pass through; every `POST /api/{action}` is intercepted and recorded into the dataset folder with pre/post frames.

---

## 2. Building & Merging Datasets (`replay/builder.py`)

Multiple recorded sessions can be merged into a single multi-branch `ScreenGraph`.

### Perceptual Hashing & Status Bar Masking

Because recordings taken at different times feature varying clock times (`09:41` vs `10:55`), battery levels, and Wi-Fi indicators, `replay/hash.py` applies **status-bar masking** (top 6% and bottom 5% zeroed out) before computing a 2D DCT perceptual hash (`pHash`).

Screens representing the same view match within a Hamming distance threshold (default `6`), uniting separate linear traces into a branched graph.

### Merging Recordings via CLI

```bash
uv run python -m replay.builder merge \
  recordings/session_1/ recordings/session_2/ \
  --out datasets/merged_app_graph/ \
  --threshold 6 \
  --method phash
```

### Exporting Summaries

```bash
# Markdown report
uv run python -m replay.builder summary datasets/merged_app_graph/ --format markdown

# JSON summary
uv run python -m replay.builder summary datasets/merged_app_graph/ --format json

# Graphviz DOT graph
uv run python -m replay.builder summary datasets/merged_app_graph/ --format dot > graph.dot
```

---

## 3. Replay Simulator Daemon (`replay/server.py`)

The replay server emulates the full HTTP interface of the real daemon from a recorded dataset or screen graph.

### Starting the Server

```bash
uv run python -m replay.server \
  --dataset datasets/merged_app_graph/ \
  --token SECRET_TOKEN \
  --port 8790
```

### Supported Routes (SPEC Section 6 & 8)

| Route | Method | Description |
|---|---|---|
| `/` | `GET` | Web dashboard: live screen view, element overlays, status bar, and real-time action log |
| `/api/frame.jpg` | `GET` | Current screen JPEG frame (`?scale=pt2` resizes to 2 px/pt of `screen_size_pt`: 750×1624 for 375×812) |
| `/api/stream.mjpeg` | `GET` | Multi-part MJPEG stream of current screen |
| `/api/elements` | `GET` | DroidClaw UIElement JSON (`?annotate=1` returns JPEG with cyan numbered bounding boxes) |
| `/api/state` | `GET` | Simulator status: current screen ID, `screen.pt`, off-path count, BLE/link status, and action log |
| `/api/pause` | `POST` | Global kill-switch: `{"paused": true/false}` |
| `/api/reset` | `POST` | Reset simulator back to graph's initial screen |
| `/api/{action}` | `POST` | Execute action (`tap`, `long_press`, `drag`, `scroll`, `move`, `type`, `key`, `home`, `back`, `launch`, `tap_text`, `tap_element`) |

### Action Matching & Off-Path Handling

When an agent posts an action:
1. **Match**: If the action matches a recorded transition from the current screen:
   - For `tap`: point falls inside the same element bounding box (or within distance `tolerance_pt`).
   - For `key`: same key combo (normalized, e.g. `cmd+space`).
   - For `type`: matching text string.
   - For `home` / `back` / `launch`: matching action or target app.
   - The simulator advances `current_screen_id` to the next recorded screen and returns HTTP 200 with the recorded post-action result.
2. **Off-Path**: If the action has no recorded outgoing transition:
   - The simulator **remains on the current screen**.
   - Increments `off_path_count`.
   - Logs an `off-path` event warning.
   - Appends to `action_log` with status `off_path: true`.
   - Returns HTTP 200 with `{"result": {"status": "off-path", "matched": false, "screen": ...}, "state": ...}`.

### Safety Guards

- **Token Authentication**: Every route requires `Authorization: Bearer <token>` or `?token=<token>`. Rejects unauthenticated requests with HTTP 401.
- **Cross-Origin (CORS) Protection**: Checks `Origin` header against `Host` header to prevent browser cross-origin requests.
- **Password Prompt Guard**: `POST /api/type` checks whether the current screen contains password or passcode prompts (`password`, `passcode`, `hasło`, `kod`). If detected, typing is refused with HTTP 409.

---

## 4. Running Tests

Tests use synthetic datasets (no physical devices or network calls required):

```bash
# Run all replay tests from repository root:
uv run --directory host pytest ../replay/tests

# Run specific test modules:
uv run --directory host pytest ../replay/tests/test_hash.py
uv run --directory host pytest ../replay/tests/test_manifest.py
uv run --directory host pytest ../replay/tests/test_graph.py
uv run --directory host pytest ../replay/tests/test_server.py
uv run --directory host pytest ../replay/tests/test_builder.py
```
