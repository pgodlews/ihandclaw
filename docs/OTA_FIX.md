# OTA fix prototype report

Read: changes are uncommitted on a local branch, based on
main's OTA implementation (`113115d`). SPEC, daemon,
server/web, deploy, bench, catalog and replay were not edited. No device, serial,
Wi-Fi, flashing, HID, commit or push operations were performed.

Measured (owner report): 1,355,440 application bytes uploaded in 585 s, followed
by verification failure reporting fw=0.2.0/ota_0. A subsequent USB reset loaded
0x10000/ota_0. Initial USB flashing preserved bonds and Wi-Fi. There is no log of
the immediate post-END boot, so the incident's precise boot/reset sequence
cannot be proved from those observations alone.

Read: a deterministic verification defect explains that sequence. Original
`113115d:firmware/main/main.c:170` passed a 65-byte buffer to
`esp_app_get_elf_sha256`, but the helper copies at most its **configured** internal
string length. Installed IDF `components/esp_app_format/Kconfig.projbuild:40`
defaults `APP_RETRIEVE_LEN_ELF_SHA` to 9. The helper's buffer is sized at
`components/esp_app_format/esp_app_desc.c:81`, and the copy is capped at `:116`.
Original `113115d:host/src/iphone_hand/ota.py:100` required equality with the
64-character descriptor hash; its confirming STATUS at `:101` was therefore
unreachable under the default configuration. The app's 120 s trial timer then
reboots an unconfirmed app, which the bootloader marks ABORTED and rolls back.

Measured (local build): generated sdkconfig retains
`CONFIG_APP_RETRIEVE_LEN_ELF_SHA=9`; rollback is enabled and anti-rollback is
unset. Native PING tests now verify all 32 descriptor bytes become 64 hex
characters. Fake-board tests verify an old nine-character hash, including an
identical-image reinstall, reaches confirmation before the 120 s deadline.

## Hypotheses and distinguishing evidence

Read: IDF references below are relative to `$IDF_PATH`; application references
are current files unless explicitly prefixed with `113115d`.

| Hypothesis | Evidence and assessment (read) | A distinguishing log/reply |
|---|---|---|
| ELF truncation skips confirmation | Original main:170 and host ota.py:100; IDF esp_app_desc.c:81/116 and Kconfig.projbuild:40. Confirmed code defect; strongest explanation. Fixed at `firmware/main/main.c:172` and `host/src/iphone_hand/ota.py:91`. | Old PING shows `slot=ota_1 elf=<9 chars>` matching the expected hash prefix, but no post-boot STATUS; then rollback. New STATUS/PING can show `ota_last=rollback,...,r:timeout`. |
| Same version/image is rejected | `firmware/main/ota.c:155` validates version syntax only; `:247` checks project/version formatting. IDF set-boot at `components/app_update/esp_ota_ops.c:814` has no application-version equality test. CLI explicitly requires a slot change. Rejected as an explanation. | END `OK fw=0.2.0 reboot=1 slot=ota_1` and trial PING with the same version/hash followed by `ota_last=confirmed`. Native and host identical-version tests pass. |
| Trial timeout itself caused rollback | `firmware/main/ota.c:125` starts a 120 s app timer; `:138` records timeout then restarts. This timer starts in trial initialization, not BEGIN, so the 585 s upload does not consume it. | `ota_last=trial_timeout,...,r:timeout`, then `rollback,...,r:timeout`; bootloader debug `otadata[...] is marking as ABORTED`. |
| Crash/watchdog/power/reset before confirmation | IDF `components/bootloader_support/src/bootloader_utility.c:397` changes any PENDING_VERIFY entry to ABORTED on the next boot. The 15 s configured watchdog is independent of the 120 s trial timer. Possible alternative; no immediate boot log exists. | A trial checkpoint followed by `rollback,...,r:unconfirmed_reset` rather than a saved timeout. Reset code is current-boot evidence; it cannot establish a precise historical crash cause by itself. |
| Health checkpoint or mark-valid fails | `firmware/main/main.c:291` only confirms after BLE status succeeds and current-session Wi-Fi is up; `firmware/main/ota.c:143` checks session/deadline and mark-valid result. Possible alternative. BLE connection/subscription is not required, only a successful host/bond query. | STATUS ERR with journal fields for BLE failure, or `ota_confirm=INTERNAL ota_pending=1 ota_last=confirm_failed,...,c:<ESP error>,r:mark_valid`. |
| END validation/hash or set-boot fails | `firmware/main/ota.c:233` checks exact size, `:238` SHA-256, `:241` esp_ota_end, `:247` descriptor and `:251` set-boot. Handles are freed by end, not aborted afterward. These errors normally raise an upload error before reboot verification, so they fit the reported CLI message less well. | `end_failed` with hash/size/image/descriptor reason; numeric `e:<error>` or `set_boot_failed,...,s:<error>`. Cleanup ABORT no longer overwrites an earlier failure. |
| Anti-rollback rejects equal version | IDF `components/app_update/esp_ota_ops.c:829` checks numeric secure_version against eFuse only when anti-rollback is enabled; equality is allowed. It does not compare fw strings. Current build has anti-rollback unset. Rejected for this configuration. | IDF `This a new partition can not be booted...secure version is lower...`, or `set_boot_failed` with ESP_ERR_OTA_SMALL_SEC_VER. |
| Wrong otadata state/boot selection | IDF `esp_ota_ops.c:127` selects NEW with rollback enabled; bootloader_utility.c:445 changes NEW to PENDING_VERIFY. IDF `esp_ota_ops.c:1187` writes VALID on mark-valid, without restarting. Existing state-machine usage is correct; damaged metadata/boot failure remains an unproved alternative. | Saved set_boot success but no trial; `boot_fallback,...,r:boot_not_selected`, bootloader load/validation failure, or a boot partition different from the selected label. |
| CLI confirms before the intended reboot | Original host sends preflight STATUS before BEGIN, waits 1.2 s after END and requires target slot/hash before trial STATUS. Preflight can confirm a previous trial, not this upload. Original exact-hash guard suppresses this trial's STATUS entirely. New `host/src/iphone_hand/ota.py:179` retains identity/slot gating; mismatches use read-only PING diagnostics. | END ACK, then old-slot PINGs, then target-slot PING and confirming STATUS. Fake delayed-boot test observes precisely two STATUS calls: preflight and the matching trial. |
| END ACK is lost though selection succeeds | END schedules restart one second after successful selection (`firmware/main/ota.c:257`). New CLI verifies on a fresh AUTH without replaying END. Possible ambiguity handled by verification. | Missing END ACK followed by matching target PING and `ota_last=confirmed`; fake lost-END test passes. |

## Implemented behavior

Read: PING formats the full descriptor hash directly. The CLI accepts a matching
legacy 8..64-character hash prefix only alongside the expected version and
changed target slot. Same-image reinstall therefore remains distinguishable.

Read: the `ota/last` NVS blob records begin/end/set-boot ESP results, source and
target labels, trial start, confirmation, and rollback reason. STATUS and
read-only PING expose `ota_last`; STATUS retains diagnostics on health/confirmation
errors. Results 0 mean success, -1 mean not attempted/unavailable; trial=1 means
started. `ota_nv` distinguishes persistence errors. The blob is committed at
checkpoints, never per DATA. Committed entries survive power loss. ABORT preserves
an earlier failure. If power is cut after otadata becomes VALID but before the
confirm journal commit, startup recovers confirmation from the authoritative
otadata state. A trial reached through a legacy updater records correct labels,
with unavailable BEGIN/END results left at -1.

Read: binary BEGIN negotiates 8192-byte chunks and four frames per ACK window.
Each frame has a big-endian length/absolute-offset/ACK-id header; intermediate
ids are zero and the final window frame receives a cumulative ACK. Payload is
raw bytes. Four buffers provide executor backpressure; image SHA-256, sequential
sector erasure and esp_ota_end validation remain. Invalid headers/disconnects
abort; there is no upload replay/resume. END resumes line mode only after the
advertised file length. Older firmware/clients retain base64 support.

Read: upload temporarily selects WIFI_PS_NONE and restores the prior mode on
END/abort/failure/session loss/idle timeout. IDF C5 constraints:
`docs/en/api-guides/wifi-driver/wifi-performance-and-power-save.rst:1555` describes
modem-sleep receive delay and PS_NONE; `:1559` says coexistence still sleeps
outside Wi-Fi's time slice. `docs/en/api-guides/coexist.rst:185` describes the
Wi-Fi/BLE slices and `:221` recommends default connectionless window/interval.
Software coexistence stays enabled; no BLE Mesh status API or dual-core affinity
change was introduced.

Read: CLI phases are preflight, begin, upload, validate/select boot, reboot/verify,
confirm trial and confirmed/inconclusive. JSON includes running fw and slot
before/after, identity, file SHA-256/size, outcome, confirmed and ota_last.
`--wait-confirm` retries an unsuccessful trial checkpoint. Without it, that
checkpoint returns trial_unconfirmed. Lost contact/deadline is unverified with
exit 0, never asserted rollback; callers must inspect confirmed. Proven rollback,
target identity mismatch and rejected upload return nonzero. Rollback/identity
errors include final JSON. Full wire and usage details are in firmware/README.md.

## Speed and limits

Estimated: the reported 1,355,440 B image needs 166 binary frames and 42 ACK
windows versus 8,069 legacy chunks/ACKs. Using owner-measured ping RTT as a rough
one-RTT-per-window budget gives 5.0..8.3 s of ACK waiting. A <60 s complete upload
needs at least 22.6 kB/s of payload throughput before allowing for validation and
reboot. This design removes the known RTT bottleneck; it does not prove the
<60 s target on hardware.

Read: first installation from legacy firmware still uses its base64 receiver;
binary speed begins on subsequent uploads. Rollback into legacy firmware cannot
expose the new journal fields, although stored NVS bytes survive. Both slots
must contain the logging implementation for reliable remote rollback diagnosis.
These are bootstrap limits, not issues a new host protocol can fix remotely.

Estimated: runtime heap/stack margin, flash-write/watchdog duration, BLE behavior
and sustained RF throughput still require board measurements. Linker unused SRAM
below is not a measurement of free runtime heap.

## Validation and sizes

Measured (hardware-free local execution):

- `make -C firmware/test test sanitize`: all six native harnesses passed normally
  and with ASan/UBSan. The existing protocol harness reports 23,139 checks.
  OTA tests cover parser negotiation, binary bounds/fragmentation/control bytes,
  pool backpressure and partial disconnect, flash/hash errors, persistent
  trial/confirm/rollback checkpoints, timeout/reset reasons and PS restoration.
  Native SHA is a deterministic double, not a cryptographic test.
- `UV_CACHE_DIR=<writable-cache> uv run --offline --project host pytest host/tests -q`:
  274 passed, 1 skipped (optional pymobiledevice3 absent), 1 existing FastAPI
  deprecation warning. The 31 OTA host tests use in-memory sockets and real
  hashlib, covering exact large binary bytes/hash/window ACKs, nine-character
  legacy identity, same-image reinstall, delayed trial boot, confirmation,
  transient health failure, 120 s rollback, interrupted transfer, bad hash,
  lost END ACK and CLI success/failure reports.
- ESP-IDF 6.2 / esp32c5 `idf.py -C firmware -B firmware/build/ota-fix build size`
  passed with warnings treated as errors in the main component. No flashing.
- Application `.bin`: **1,358,960 B** (`0x14bc70`); slot **2,031,616 B**;
  **672,656 B** free (33%). Bootloader: **22,416 B**; partition-table binary:
  **3,072 B**. Artifacts remain under ignored firmware/build/ota-fix.
- ELF symbol sizes: binary pool **32,832 B**, journal **112 B**, OTA runtime state
  **184 B**. IDF reports HP SRAM used **192,800 B**, linker unused **128,128 B**.
- `git diff --check` passed. No measured physical upload/confirmation/rollback
  result is claimed for this patch.

## Changed files

Read: changed source/documentation files are:

- `README.md`, `firmware/README.md`, `host/README.md`, `docs/OTA_FIX.md`.
- `firmware/main/CMakeLists.txt`, `link.h`, `main.c`, `ota.c`, `ota.h`,
  `wifi_link.c`, new `ota_wire.c` and `ota_wire.h`.
- `firmware/protocol/protocol.c`, `protocol.h`.
- `firmware/test/Makefile`, `test_ota.c`, `test_ota_parser.c`, `test_runtime.c`,
  new `test_ota_wire.c`; `runtime_stubs/esp_app_desc.h`, `esp_ota_ops.h`, new
  `esp_wifi.h` and `nvs.h`.
- `host/src/iphone_hand/ota.py`, `cli.py`, `serial_link.py`; `host/tests/test_ota.py`.
