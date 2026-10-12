# Contributing to iphone-hand

Help others reproduce a hardware build, compare models, and reuse safe iOS
navigation lessons. Start with [the README](README.md) and
[the contract](docs/SPEC.md). The project is [MIT-licensed](LICENSE);
by submitting a contribution (code, playbooks, Benchy results) you agree to license it
under the MIT licence.

## Development setup

Work on your own branch/worktree. Python ≥3.11, uv, a native C compiler and make
are enough for hardware-free development. From the checkout root:

```sh
git switch -c contributor/my-change
uv sync --project host
uv run --project host iphone-hand --fake status
```

Optional extras are `ocr` for daemon typing and `devservices` for developer
services. Neither is needed for the base tests. Read: PyYAML is a runtime
dependency so catalog/playbooks load reliably; pytest is in the dev group.
Lockfiles and build output are currently ignored. Match surrounding code style;
keep changes focused, type/validate public inputs, and add meaningful regression
tests for behavior changes. Avoid new formatters or sweeping rewrites.

## Run all required checks

Run these from the root; they use fixtures/doubles and do not require a phone:

```sh
uv run --project host python -m pytest host/tests
uv run --project host python -m pytest bench/tests
uv run --project host python -m pytest catalog
make -C firmware/test
make -C firmware/test sanitize
uv run --project host python -m bench.leaderboard
git diff --check
```

Install **ESP-IDF 6.2** (not released yet: use ESP-IDF `master`, which reports 6.2.0) and its C3/C5 toolchains. Set IDF_PATH and IDF_TOOLS_PATH
to your own installation, then build both targets without flashing:

```sh
. "$IDF_PATH/export.sh"
export IDF_COMPONENT_MANAGER=0
cd firmware
idf.py -B build/esp32c3 set-target esp32c3 build
idf.py -B build/esp32c5 set-target esp32c5 build
make -C test check-map
```

Read: generated sdkconfig stays in each ignored target build directory.
[CI](docs/contributor-ci.md) runs these checks with the official versioned IDF
container. The independent replay package also has tests: run
`uv run --project host python -m pytest replay/tests` when changing replay.
Record command, outcome, skipped checks and environment in your PR. Label claims
**measured** (observed), **read** (source/documentation), or **estimated** (unverified)
where it matters, with file:line or public URL citations. A native test/build
pass is not evidence of physical BLE, capture, or OTA reliability.

Never flash, pair, provision, send HID or operate a phone during routine tests.
Hardware actions need explicit task/operator authorization. Do not change
`docs/SPEC.md` unless asked; report a contract discrepancy instead.

## Add a playbook

1. Copy a seed under `catalog/playbooks/` to `<id>.yaml`, one app/task/locale per
   file. Keep the filename and ID identical. Preserve the documented fields in
   [the format guide](catalog/README.md).
2. Write Spotlight app name, entry steps, visible text landmarks, gotchas,
   locale/keyboard caveats, verification and safety. Prefer screen inspection
   to blindly replaying coordinates. Label evidence with source and scope.
3. Run catalog tests. Include the iOS/locale scope and sanitized observations in
   the PR. Do not claim a device measurement from a schema validation.

Read: `catalog/` is retained for existing consumers; the new playbooks are
additive and do not automatically execute in adapters. Proposals for adapter
integration should say how advice is selected and supplied to the model.

## Add a Benchy result

1. Commit the harness changes before measuring and record its full Git commit.
   Read the phone/iOS version privately. Run `00_benchy` with the same setup for
   every declared repeat; include failures and timeouts.
2. Follow [bench/results/README.md](bench/results/README.md) to copy only permitted
   metrics to one JSON per model run set. Preserve C1–C4, verdict, success claim,
   honesty, action source, measured time, and known/unknown tokens/cost.
3. Run `python -m bench.leaderboard` through the host uv environment and include
   the JSON in a PR. Generate `LEADERBOARD.md` with `--output` when updating the
   published table. Explain model/quantization, adapter, and experimental setup.

No synthetic results belong in submissions. For fine-tunes, identify base model
and checkpoint, data rights and sanitization, and whether evaluation data was
held out. Raw phone traces require consent and review before any training/export.

## Add a model adapter

Read `bench/agents/claude_cli.py`, `bench/agents/droidclaw.py` and
[the runner protocol](bench/README.md#runner-reporting-and-other-scenarios).
Add an adapter under `bench/agents/`, with a command usable by `--agent-cmd`.
Consume injected BENCH_GOAL/BENCH_MODEL and daemon auth from environment;
keep private values out of stdout, prompts saved to git, and process arguments.
Use the daemon API/phone CLI rather than opening a second board connection.

Print exactly one `BENCH_RESULT` declaration with boolean success, observed
ios_version and calc_result. Do not invent missing fields or self-grade. Optional
`BENCH_USAGE` provides input/output tokens and cost when actually available.
Keep secrets redacted, respect timeouts/process termination, and describe action
count provenance and budget limitations. Test with fake responses and daemon
fixtures, including invalid/missing claims, timeouts and missing usage. Never
require a paid model or real phone in CI. Provide a documented example command.

## Add and calibrate a new phone

The software follows the screen size, not a model list. The contract is
[SPEC section 9](docs/SPEC.md#9-screen-geometry-phone-points). The device table is
`IPHONE_SIZES` in `host/src/iphone_hand/geometry.py`.

1. **Size.** Look the phone up in `IPHONE_SIZES`. To add a missing model, cite an Apple source
   for its points and scale (or Apple's pixels plus the scale) in the row's `source`, the same
   way the existing rows do. Never derive a size from a screenshot alone. Check Display Zoom
   (Settings > Display & Brightness): it changes the point size (read, secondary source).
2. **Offline check.** Add the size to the parametrized synthetic tests in
   `host/tests/test_geometry.py` (native pixels = points × scale). Run all required checks.
3. **Configure.** With the daemon running and the phone mirroring, read `screen` from
   `GET /api/state`. Over AirPlay, Face ID iPhones are `ambiguous` and the 375 × 812 default
   is kept; set `screen_pt = [<w>, <h>]` (portrait points) in your private daemon.toml and
   restart the daemon.
4. **Calibrate.** This is required once per phone: AssistiveTouch gain is per device and per
   tracking-speed setting. Stop the daemon first, because the board accepts one client. Then
   run the existing `calibrate` command against the receiver's frames and the board:

   ```sh
   uv run --project host iphone-hand --port <board-port> --frames-dir <frames-dir> \
     --screen-pt <w> <h> calibrate
   ```

   The result names its `profile` (`<w>x<h>`) and is saved in the private calibration cache
   next to the reference profile. Restart the daemon so it loads the profile. Never copy
   another phone's numbers into a profile.

Do not edit `docs/SPEC.md` for a new size; the table and tests are enough. Label what you
measured.

### Hardware validation checklist (owner, per new phone)

Record each item as measured/not done in the PR, with the phone model, iOS version and point size:

- [ ] **Auto-detect:** `/api/state` `screen` shows the expected `pt`, `orientation` and
      `source`. With AirPlay, confirm it is `ambiguous` without `screen_pt` and correct with it.
      Rotate an app that supports landscape and check the size swaps.
- [ ] **Calibrate:** run step 4. Record the profile, `rmse_pt` and samples. The pointer
      must stay on screen throughout.
- [ ] **Tap accuracy at the corners:** `POST /api/move` to (12, 12), (w−12, 12), (12, h−12),
      (w−12, h−12) and the centre. Record `error_pt` (target ≤ 3 pt) and iterations. Then tap a
      harmless target near each corner and confirm the phone reacted to that target.
- [ ] **One Benchy run:** run the README's Benchy command once (`--repeat 1`) and keep the
      result locally. Share sanitized metrics only through `bench/results`, with the phone
      model and point size named.

## Add a board target

Read [firmware](firmware/README.md) and [C3 port notes](docs/ESP32C3_PORT.md).
Check USB-Serial/JTAG, BLE/NimBLE support, flash capacity and partition layout;
USB-OTG/HID is not this project's transport. Gate chip-specific APIs with SoC
capabilities, keep common defaults shared, and add per-target sdkconfig defaults
only when needed. Add the target to CI and native runtime identity tests, and
update host OTA chip/header validation before accepting its images.

Build all supported targets. Hardware validation should separately cover boot,
PING identity, pairing/reconnect, HID, Wi-Fi/BLE coexistence, heap/stacks and OTA
confirmation/rollback. State exactly what was measured. Do not silently expand
the contract or advertise a target based only on compilation.

## Privacy and review

Never commit tokens, passwords, IPs, hostnames, MACs, serials, personal paths,
private session references, or screenshots containing personal information.
Use placeholders such as `<daemon-host>` and `<board-port>` in examples.
`runs/` (including `bench/runs/`) is git-ignored; do not force-add it. About screens,
notifications, stdout/stderr and raw result reasons can disclose private data.
Only submit reviewed metrics and consented, sanitized fixtures.

Use the issue/PR templates. Before a public release, the owner must review the
entire existing history and fixtures too; a clean new diff is not a history audit.
No pushes or publication are part of this prototype.
