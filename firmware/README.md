# iphone-hand firmware prototype

Read: this project implements SPEC sections 1–2 and 7 (with the discovery
fallback and hardening contract proposals below) for ESP-IDF 6.2.0 / ESP32-C5 and ESP32-C3, using bundled `esp_hid` and NimBLE.
Read (owner-provided hardware findings): the existing BLE firmware pairs, bonds
and reconnects on the board/phone. This Wi-Fi implementation has not been run
on hardware in this session.

## Build and native tests

Set `IDF_PATH` to the installed ESP-IDF 6.2.0 tree and `IDF_TOOLS_PATH` to its
installed tool cache, then run from the repository root:

```sh
. "$IDF_PATH/export.sh"
export IDF_COMPONENT_MANAGER=0
cd firmware
idf.py -B build/esp32c5 set-target esp32c5 build
idf.py -B build/esp32c3 set-target esp32c3 build
make -C test
make -C test sanitize
make -C test check-map
idf.py -B build/esp32c5 size
idf.py -B build/esp32c3 size
```

Read: no registry dependencies are used. Disabling the optional component manager
also avoids its `psutil` process-enumeration failure in restricted macOS sessions;
export it in the shell so it applies to the bootloader subprocess too.
`check-map` uses the installed IDF descriptor parser with tiny host-only shims;
the ordinary parser tests require only Mac `cc` and `make`, with no IDF or hardware.
All generated binaries, test executables and debug symbols stay in `firmware/build/`.
Read: each build stores generated `sdkconfig` inside its ignored target build directory;
shared settings live in `sdkconfig.defaults`. No per-target overrides are currently
needed; future differences belong in `sdkconfig.defaults.esp32c3` / `.esp32c5`.
Read: C3 uses 2.4 GHz Wi-Fi; C5 enables automatic dual-band selection. Both use
USB-Serial/JTAG and the same 4 MB OTA layout. Board LED/BOOT GPIO differences
are irrelevant to this firmware: it does not drive those board pins.
Estimated: first C3 boot must validate Wi-Fi initialization heap, task stacks,
BLE/Wi-Fi coexistence and OTA rollback; no C3 runtime behavior is measured here.

## OTA prototype: one-time USB installation

Read: OTA is a prototype extension to SPEC 1/7; this port changes SPEC only
for the additional chip, PING chip value and target build requirements.
It adds `OTA`, errors `SIZE`/`HASH`/`IMAGE`, `PING elf=<64 hex> slot=<label>`,
and authenticated `STATUS ota_pending=0` after the health checkpoint.
Read: the investigation and validation report is [docs/OTA_FIX.md](../docs/OTA_FIX.md).
Read: firmware version comes from the IDF application descriptor (`project(... VERSION ...)`
in `CMakeLists.txt`), currently `0.2.0`. Future releases should change that version.

Read: `partitions.csv` uses the following layout on the owner-measured 4 MB part:

| Partition | Offset | Size | End (exclusive) |
|---|---:|---:|---:|
| NVS | `0x9000` | `0x6000` | `0xF000` |
| PHY | `0xF000` | `0x1000` | `0x10000` |
| `ota_0` | `0x10000` | `0x1F0000` (2,031,616 B) | `0x200000` |
| `ota_1` | `0x200000` | `0x1F0000` (2,031,616 B) | `0x3F0000` |
| `otadata` | `0x3F0000` | `0x2000` | `0x3F2000` |
| Unallocated | `0x3F2000` | `0xE000` | `0x400000` |

Read: **NVS and PHY offsets/sizes are unchanged. No NVS migration is needed.**
The factory slot becomes `ota_0`, at the same application offset. NVS contains
bonds, BLE name, Wi-Fi provisioning and AUTH token. Do not run `erase-flash`.
A normal IDF `flash` writes the new bootloader, partition table, app and erased
initial OTA metadata, without writing NVS. An OTA application image alone
cannot install this table or enable the old bootloader's rollback support.

Read: the owner must perform this installation once over USB. From `firmware/`,
with the IDF toolchain active and `IDF_COMPONENT_MANAGER=0`:

```sh
TARGET=esp32c5  # Select esp32c3 for a C3 board.
idf.py -B "build/$TARGET" set-target "$TARGET" build
# Inspect build/$TARGET/sdkconfig: CONFIG_BOOTLOADER_APP_ROLLBACK_ENABLE=y and flash size 4MB.
# Inspect build/$TARGET/flasher_args.json for target-specific bootloader offset, table at 0x8000,
# app at 0x10000, ota_data_initial.bin at 0x3f0000; no NVS image.
# Set BOARD_PORT to the actual USB-Serial/JTAG port yourself.
# Optional private backup (contains credentials/bonds; never commit or share):
python -m esptool --chip "$TARGET" --port "$BOARD_PORT" read-flash 0x9000 0x6000 "build/$TARGET/nvs-backup.bin"
idf.py -B "build/$TARGET" -p "$BOARD_PORT" flash
```

Read: initial erased metadata makes the bootloader boot `ota_0` and record it
VALID (`bootloader_utility.c:set_actual_ota_seq`). This gives the first OTA update
a rollback destination. Verify existing bonds/provisioning, authenticated PING
and STATUS before returning the board to the power bank. No re-pair or
re-provision is expected when NVS is retained. Estimated: retaining the same NVS
bytes/schema should preserve the existing phone bond; this session did not
measure survival across a USB flash. A private NVS backup can be restored at the
same offset by the owner if a mistaken erase occurs; do not restore stale OTA
metadata from another layout.

## OTA usage and wire contract

Read: after that USB installation, build future releases with this partition
layout and rollback configuration, then upload **only** `build/<target>/iphone_hand.bin`:

```sh
# From host/; replace <board-name> with a resolvable board name/address.
# ~/.config/iphone-hand/board-token must contain the existing 32..64 hex AUTH token.
uv run iphone-hand ota ../firmware/build/esp32c5/iphone_hand.bin --host <board-name> --port 7878 --wait-confirm
```

Read: the existing DHCP-name/address fallback still applies; this change does
not add mDNS. Stop other clients (including the daemon) for the update: a new
TCP client replaces the current one. Progress goes to stderr, final verified
firmware/ELF identity/slot/file SHA-256/size JSON to stdout. No token appears in
progress or results. This command constructs neither Phone nor Screen nor a
serial link. Bad artifact headers/target/project/size are rejected locally
before TCP access; the image chip must match PING before STATUS or OTA BEGIN. IDF performs the authoritative complete image validation.

Read: after `AUTH`, request-ID framing is unchanged:

```text
<id> OTA BEGIN <decimal-size> <64-hex-file-sha256>
<id> OTA DATA <canonical-base64>
<id> OTA END
<id> OTA ABORT
```

Read: STATUS/PING advertise `ota_binary=1` on this firmware. For binary mode use
`<id> OTA BEGIN <size> <sha256> BIN`; the ACK advertises
`chunk=8192 window=4 mode=bin size=<N> slot=<inactive-slot>`. After BEGIN's ACK,
DATA uses a 12-byte header of three unsigned big-endian 32-bit integers:
`payload_length, absolute_file_offset, ack_request_id`, then exactly 1..8192
payload bytes. Send up to four frames in flight (32 KiB); use id=0 on intermediate
frames and a normal request id on the last frame in the window. Only the last
frame receives `@<id> OK received=<cumulative-bytes>`, after all preceding
writes/hash updates. Every final file frame must request an ACK. Wait for that
ACK before sending the next window. LF/CR/NUL stay payload. The four-buffer pool
applies backpressure. Length, offset, id, file bounds and ACK bounds are checked.

Read: after the advertised file length, ingress returns to line mode for END.
Close the socket to abort an incomplete binary stream; an ABORT line would be
payload. Invalid headers close the session; flash/hash errors emit an OTA error
or an ERR on the window ACK. No frame/window/END replay or resume is supported.
A disconnect aborts the handle at the executor's next poll.

Read: the CLI detects legacy firmware without `ota_binary=1` and uses the old
BEGIN plus canonical base64 DATA of 1..168 bytes, one ACK per chunk. This allows
installing the fix without USB. That first upgrade retains its slow transfer;
binary speed starts with the next upload. New firmware also accepts the old
syntax for older clients. An older CLI's exact ELF check works against the new
full PING hash.

Read: OTA is refused over serial (`ERR ARG`), even when recognized arguments
are invalid. It requires a live authenticated TCP session both at ingress and
execution. This keeps firmware replacement behind the provisioned AUTH token;
USB remains for the owner's initial install/recovery. Another BEGIN during an
upload, an unconfirmed trial boot, or scheduled reboot returns BUSY.
`ABORT` releases an upload handle. OTA changes only its diagnostic NVS namespace;
bond/provisioning keys and the running slot stay intact during upload. A session replacement/disconnect or 30 seconds without a successful DATA
aborts at the executor's next poll. There is no resume: rerun the command to
begin a fresh upload. Invalid DATA syntax is rejected without advancing the
upload; a well-framed size overrun/short END, hash failure or flash-write failure
aborts the transaction. END errors are SIZE/HASH/IMAGE/INTERNAL as applicable.

Read: END checks exact received size and a streaming PSA SHA-256 over the file,
then calls `esp_ota_end` for complete ESP image/header/chip/revision/checksum
validation, checks the application descriptor project/version, and finally
calls `esp_ota_set_boot_partition`. Its success reply is
`OK fw=<version> reboot=1 slot=<slot>`; the executor schedules reboot one second
later to let the existing nonblocking TCP writer send it. A lost END ACK is
ambiguous: the host reconnects to verify the selected image without replaying
END or re-uploading. Lost DATA/BEGIN ACKs fail the upload and close the session.

Read: erasure is incremental with `OTA_WITH_SEQUENTIAL_WRITES`; each binary DATA
writes at most 8192 bytes (legacy DATA: 168 bytes). BLE/NimBLE and network tasks keep running, and the executor
feeds its existing watchdog between commands. HID commands retain their queue
and STOP behavior. Estimated: sector erase/write or final validation can pause
BLE on the single CPU; uninterrupted HID timing, flash latency, heap/stack
headroom, upload throughput and watchdog margin need hardware measurement.
The synchronous CLI owns its connection for the upload, so it cannot issue HID
concurrently on that connection. Reboot necessarily disconnects BLE.

## OTA rollback and confirmation

Read: `CONFIG_BOOTLOADER_APP_ROLLBACK_ENABLE=y` makes selected updates NEW;
the bootloader changes them to PENDING_VERIFY for their first boot. This app
**does not** confirm at startup or on AUTH/PING alone. A STATUS from the current
authenticated TCP session must complete the BLE host operation successfully
and observe Wi-Fi up; only then does `esp_ota_mark_app_valid_cancel_rollback`
mark the app VALID and STATUS reply `ota_pending=0`. Serial STATUS cannot
confirm. This proves the BLE host/event queue and bond-store query work, plus
Wi-Fi/TCP/AUTH/executor; it does not require the phone to be connected or prove
HID reports reach iOS. Read: STATUS reports confirmation errors in `ota_confirm=<code>` while retaining
`ota_pending=1` and diagnostics. BLE health errors return ERR with OTA fields.
Serial STATUS and PING expose diagnostics without confirming.

Read: the host checks PING version, ELF identity and expected inactive slot before
confirmation. PING now converts all 32 descriptor hash bytes directly; IDF's
`esp_app_get_elf_sha256` helper defaults to **9 hex characters**, even with a
65-byte destination. That mismatch prevented the old CLI reaching STATUS,
so the 120 s trial reboot could roll back a healthy app. The CLI accepts a matching
legacy 8..64-character hash prefix plus the changed slot, allowing an identical
reinstall into older firmware. New PING returns all 64 characters. There is no
application version-equality guard.

Read: CLI phases go to stderr; JSON includes `fw_before/slot_before`,
`fw_after/slot_after` (also `fw/slot`), ELF, file digest, byte count, `confirmed`,
`outcome`, and `ota_last`. A successful checkpoint returns `outcome=confirmed`.
By default a reachable trial with a failed checkpoint returns `trial_unconfirmed`;
`--wait-confirm` retries until confirmation, observed rollback or the 150 s
deadline. Loss of contact/deadline returns `unverified (...)` with exit 0: it is
unknown and callers must check `confirmed=0`. An observed rollback, unexpected
image in the target slot, rejected upload or local input/connection failure
returns nonzero. Rollback/identity failures include final JSON. Lost END ACKs are
verified without replay. PING diagnostics detect rollback without confirming an
unexpected image.

Read: one NVS blob at namespace `ota`, key `last`, records begin/end/set-boot ESP
results, trial start, confirmation and rollback reason with partition labels.
Each checkpoint commits once; DATA does not write NVS. STATUS and PING include
`ota_last=<event>,<from>><to>,b:<begin>,e:<end>,s:<set_boot>,t:<trial>,c:<confirm>,r:<reason>,x:<reset_code>`.
ESP result 0 means success; -1 means not attempted. `ota_nv` reports persistence
errors. `r:timeout` is saved before the trial reboot; an ABORTED target without
that checkpoint yields `unconfirmed_reset`, which does not distinguish panic,
watchdog or power loss. Current-boot reset code is included. Committed checkpoints
survive software resets and power loss. A rollback into legacy firmware cannot
expose new fields, although the blob survives; full remote diagnosis requires
both slots to contain this implementation. Reply capacity is 512 bytes; command
lines remain limited to 256 bytes.

Read: upload switches Wi-Fi to `WIFI_PS_NONE` and restores its previous mode on
END, abort, write/hash failure, idle timeout or session loss. Software coexistence
stays enabled; connectionless window/interval settings stay default. C5 docs say
PS_NONE still sleeps outside Wi-Fi's RF time slice. It reduces modem-sleep receive
delay, not BLE arbitration.
Estimated: the owner-sized image needs 166 frames and 42 ACK windows versus
8,069 legacy ACK waits. At the owner-measured 118..198 ms ping RTT, a simple
one-RTT-per-window budget is 5.0..8.3 s, leaving about 50 s for flash/RF/validation
within the <60 s target. This is an estimate, not a measured upload duration.
Measured (owner report): legacy upload was 1,355,440 B in 585 s (~2.32 kB/s),
then verification failed; a later USB reset loaded ota_0. Initial USB flashing
preserved bonds and Wi-Fi.

Read: a PENDING_VERIFY app that is not confirmed within 120 seconds from OTA
initialization reboots; any earlier reset, crash/watchdog or power loss also
causes the bootloader to mark it ABORTED and select the previous working slot.
A blocked executor is covered by the existing 15-second watchdog. These are
configured deadlines, not measured recovery times. A selected image must retain
this health/rollback code; the existing updater cannot force arbitrary future
firmware to implement it. Rollback requires a valid previous image and compatible
NVS schema. The initial USB installation itself cannot roll back to the former
factory firmware; USB recovery is needed if that installation is broken.

Read: uploads use the existing plaintext TCP token authentication. SHA-256
checks integrity, not publisher signatures; there is no TLS, Secure Boot or
anti-rollback/eFuse change here. Someone with the token/link can replace code,
and token confidentiality is limited by plaintext transport/NVS. Future NVS
schema changes must remain compatible with the previous firmware. Only app
updates are supported; bootloader/table updates require owner USB access.

Read: IDF citations below refer to the **installed 6.2 sources**. The online
[ESP32-C5 OTA guide](https://docs.espressif.com/projects/esp-idf/en/latest/esp32c5/api-reference/system/ota.html)
corroborates the two-slot/metadata and trial-boot model, but its latest rollback
Kconfig naming/defaults have changed; use this local tree's configuration.

| API / behavior | Path relative to `$IDF_PATH` |
|---|---|
| OTA handles, sequential writes, begin/write/end/abort, partition selection/description, state and mark-valid | `components/app_update/include/esp_ota_ops.h` |
| Incremental erase, first-byte magic, full validation and handle cleanup, boot selection, mark-valid persistence | `components/app_update/esp_ota_ops.c` |
| Image header/target constants and complete validation | `components/bootloader_support/include/esp_app_format.h`, `components/bootloader_support/src/esp_image_format.c` |
| Bootloader NEW/PENDING_VERIFY/ABORTED, initial ota_0 VALID sequence | `components/bootloader_support/src/bootloader_utility.c` |
| Descriptor hash and helper truncation | `components/esp_app_format/include/esp_app_desc.h:35`, `components/esp_app_format/esp_app_desc.c:81`, `:110`, `components/esp_app_format/Kconfig.projbuild:40` |
| PS_NONE latency and coexistence time slices | `docs/en/api-guides/wifi-driver/wifi-performance-and-power-save.rst:1555`, `:1559`; `docs/en/api-guides/coexist.rst:185`, `:221`; `components/esp_coex/Kconfig` |
| NVS blob/commit APIs | `components/nvs_flash/include/nvs.h` |
| Rollback-enable configuration | `components/bootloader/Kconfig.app_rollback` |
| Descriptor offsets/version/ELF identity | `components/esp_app_format/include/esp_app_desc.h` |
| PSA SHA-256 streaming APIs | `components/mbedtls/mbedtls/tf-psa-crypto/include/psa/crypto.h`, `components/mbedtls/test_apps/mbedtls_ut/main/test_psa_sha.c` |
| App slot alignment, partition bounds/generator, build size checks | `components/partition_table/gen_esp32part.py`, `components/partition_table/check_sizes.py` |
| Owner USB flash manifest | `build/flasher_args.json` (generated; ignored by git) |

Read: `test/test_ota_parser.c` exercises actual parser/framing with maximum
chunks, encoded controls, invalid padding/hash/size and overflow recovery.
`test/test_ota.c` executes the actual OTA implementation with deterministic
IDF/PSA doubles: it tests state/cleanup/boot-selection ordering, stale sessions,
interruptions/timeouts, size/hash/image failures and confirmation/reboot gates;
its hash double is not a cryptographic validation. `host/tests/test_ota.py`
uses actual TcpLink over exclusively in-memory sockets with real hashlib
SHA-256, fragmented replies/short writes, interrupted uploads, wrong sizes,
bad hashes, lost END ACK, rollback/failed health and version/identity checks.
Read: `test/test_ota_wire.c` tests fragmented binary frames, bounds, control-byte
payloads, pool backpressure and partial disconnect. Persistent journal/PS restore
checks execute the real OTA code; host fakes simulate delayed trial boot,
confirmation, transient health failures and the 120 s timeout/rollback. Native
runtime tests assert the complete 64-character PING hash. No hardware reliability
or successful physical rollback is claimed by these tests.

## Flash and pair (manual, not performed here)

Read: the board uses USB-Serial/JTAG, not USB-OTG. Choose its actual port yourself:

```sh
idf.py -p /dev/cu.usbmodemXXXX flash monitor
```

Read (SPEC): on the iPhone, open **Settings > Bluetooth**, select **iPhone Hand**
and complete pairing. Then enable **Settings > Accessibility > Touch >
AssistiveTouch**. Keep the phone unlocked for the smoke test. Button 2/3 actions can
be assigned in AssistiveTouch's device settings. Keyboard text assumes the host's
US hardware-keyboard mapping. Exit the IDF monitor with Ctrl+].

Estimated: iOS should reconnect when this peripheral advertises after reset or
disconnection; only the iPhone, as BLE central, can initiate that connection.
Read: bonding uses Just Works (no passkey), encryption, Secure Connections
capability with legacy fallback, and NVS. A fresh, unbonded board opens a
120-second pairing window at initial
NimBLE sync. A board with saved bonds boots with the window closed. `PAIR`
(serial or authenticated TCP) disconnects the current link and opens a new
120-second window. `UNPAIR` also opens a window after deleting all bonds.
The first successful new bond closes it early. With no bonds and no window,
connectable advertising stops. Otherwise closed-window undirected advertising
continues for iOS reconnect, but unknown identities are terminated before
security initiation and cannot become accepted HID peers. Identity checks use
stored bonds and IDF's RPA/IRK resolver. The deadline is checked again after
security, and an unfinished new pairing is terminated/deleted at expiry.

Read: if the iPhone has forgotten its bond, use `PAIR` to authorize stale-bond
REPEAT_PAIRING deletion and retry. Outside a window that request is rejected,
including for a known address: knowledge of the address is not proof of the old
keys. Store overflow fails instead of automatically evicting trusted bonds;
use `UNPAIR` when intentionally clearing a full store. Just Works still allows
any nearby central to win pairing while a window is open; it supplies no MITM
protection. Closed-window admission can still be disrupted by connection spam.

Read: `NAME` disconnects an active link, commits the new name, then restarts advertising; iOS may retain a
cached displayed name until forgetting/re-pairing.

## Manual smoke test

Read: the following is a proposed session, not a measured transcript. Type one
line at a time, waiting for its matching reply. LF terminates commands; CRLF is
also accepted. Monitor local echo is optional. Ignore log lines without `@`.

Before pairing:

```text
1 PING
2 STATUS
3 MOVE 8 0
4 CFG step=8 interval=15
```

Estimated expected replies: `@1 OK fw=0.2.0 chip=esp32c5`,
`@2 OK conn=0 ready=0 bonded=0 peer=- wifi=0 ip=- pairing=1 ...`
on a fresh NVS during the window,
`@3 ERR NOCONN`, and `@4 OK step=8 interval=15`.
Boot emits `@0 EVT boot fw=0.2.0`. Pairing should emit `@0 EVT conn peer=...`
only after encryption, then `@0 EVT ready` when all three report-mode input
characteristics are subscribed. `STATUS conn=1` means encrypted, not just a radio link.

After pairing, with AssistiveTouch on:

```text
5 STATUS
6 MOVE 40 0
7 MOVE -40 0
8 CLICK
9 BTN 1
10 MOVE 0 24
11 BTN 0
12 SCROLL -3
13 SCROLL 0 3
14 KEY 0x08 0x2c
15 KEY 0 0x29
16 KREPORT 0 0x04 0x05
17 KREPORT 0 0
18 CONSUMER 0xE9
19 CONSUMER 0x223
20 CFG interval=5
21 MOVE 32768 0
22 WHATEVER
23 NAME iPhone Hand Test
24 STATUS
```

Estimated: movement should show the pointer, CLICK should tap, BTN/MOVE/BTN should
drag, vertical/horizontal SCROLL should act where iOS supports it, Cmd+Space should
open Spotlight and Escape should dismiss it. Test KREPORT only in a harmless text
field (`a` and `b`, then release); volume up should change volume. Consumer AC Home
may be ignored by iOS. Commands 21/22 should return `ARG`/`UNKNOWN`. NAME should
persist across a manually initiated board reset. These effects need hardware validation.

Read: every HID success reply follows the final notification being queued to
NimBLE; it does not prove delivery, UI response, or mirror settling. A disconnect
mid-command returns `NOCONN` and stops the remaining reports. After an unsuccessful
release, the executor requests disconnection to clear a potentially held state.

Manual bond cleanup, when wanted (this deletes all peripheral bonds):

```text
25 UNPAIR
26 STATUS
27 NAME iPhone Hand
```

Estimated: `UNPAIR` should disconnect, clear bonds and advertise again; STATUS
should report zero bonds. Forget the corresponding iPhone entry too. To verify
bond persistence, reconnect, manually reset the board without erasing flash, and
check `bonded` and automatic reconnection.

## Protocol decisions and scheduling

Read (implementation): ordinary commands and parse errors share a 16-envelope
FIFO executor. The parser calls no BLE APIs. Full-queue requests receive `BUSY`
(or their existing parse error) immediately, and ingress keeps reading so STOP
cannot be hidden behind a paused parser. Overload replies may precede older
replies. Hosts should pace commands by replies and not exceed the USB RX buffer
(4096 bytes). STOP has a separate four-envelope priority queue and cancellation
fence; it preempts pacing and also cancels HID commands admitted before it.
Requests admitted afterward may execute normally. A saturated STOP queue returns
BUSY but still raises the cancellation fence. Administrative commands retain
FIFO ordering. Only valid, argument-free STOP requests cancel anything.

Read: `STOP` is accepted over serial or authenticated TCP. The interrupted HID
command replies `ERR CANCELLED`; old queued HID commands do too. STOP sends
neutral keyboard, mouse and consumer reports, clears remembered buttons, and
replies after that safety cleanup. With no encrypted connection, STOP succeeds
as an idempotent no-op. If a release cannot be queued (including a missing report
subscription), the link is terminated and STOP reports the failure. A paced
command has a 10-second monotonic budget, returning `ERR TIMEOUT` on expiry,
then up to 2 seconds total for safety cleanup. Execution failures use cleanup;
parse/ARG rejections emit no HID reports and leave existing held state alone.
Holds still parse as 0–65535 ms, but holds approaching/exceeding the command
budget can time out and release early. Previously queued reports cannot be
retracted. This is a cooperative budget, not a hard real-time guarantee.

Read: the synchronous SerialLink/TcpLink `command()` lock waits for its current
reply. To interrupt an in-flight action, a protocol client must send STOP without
waiting for that action's reply (raw/pipelined ingress), rather than calling
`command("STOP")` behind that lock. Default host action reply budgets are 12.5 s,
STOP gets 4.5 s (allowing an existing cleanup plus STOP cleanup), and PAIR/NAME/
UNPAIR get 7 s. Explicit caller timeouts still override them. Host motion splits
large/homing deltas into commands within +/-2000 and under 8 s nominal pacing;
congestion may still exhaust the firmware budget.

Read: MOVE uses `step=8`, `interval=15` by default; independent axes are clamped
and consumed without losing counts. SCROLL clamps each axis to 127 and uses the
same interval. First report is immediate; subsequent reports use FreeRTOS timed
suspension (`vTaskDelay`), never a busy wait, with a 1 ms configured tick. Bluetooth
and USB scheduling can extend the interval. `CFG` affects subsequent commands.
Notification resource failures yield the NimBLE host task between retries,
probe MSYS buffers at most every 20 ms, and have a 2-second total limit per report.
Successful NOTIFY_TX from other traffic, disconnect/reset and STOP wake the
pending attempt. Failed synchronous NOTIFY_TX never wakes its own retry.
Controller ACL completion/draining remains NimBLE's responsibility. Protocol
Mode is cached through the HID write callback so exhausted mbufs cannot make a
characteristic read falsely select boot mode.
Buttons survive MOVE/SCROLL. CLICK restores the preceding BTN state; KEY releases
all keyboard usages/modifiers, replacing any prior KREPORT state. Held buttons
are forgotten when the connection generation changes.

Read: the 256-byte limit excludes LF, includes optional terminal CR, and is enforced
before tokenization. An overlong line is discarded through its LF and gets one
`PARSE` reply. An unrecoverable ID replies as `@0 ERR PARSE` (SPEC leaves this case
undefined). IDs are decimal 1–999999; leading zeroes are accepted. Other integers
are decimal except KEY/KREPORT/CONSUMER, which also accept `0x` hex. Optional args
are positional, as used by SPEC's host examples. `hold_ms` accepts 0–65535;
MOVE and SCROLL accept -2000..2000 per axis; larger totals return ARG
before emitting reports (no silent truncation). NAME accepts nonempty UTF-8 without control
characters, up to 248 bytes subject to the overall line limit. Long names are
UTF-8-truncated to 29 bytes in the scan response and remain complete in GAP/NVS.

## HID and iOS uncertainties

Read: `main/report_map.h` defines report ID 1 (8-byte keyboard input and 1-byte
LED output), ID 2 (7-byte mouse: three buttons, little-endian signed 16-bit X/Y,
signed 8-bit wheel and AC Pan), and ID 3 (2-byte unsigned Consumer Page array
selector, zero for release). The payload excludes the report ID because HOGP's
Report Reference descriptor identifies it. AC Pan uses Consumer Page usage 0x0238;
see the [USB-IF HID usage tables](https://usb.org/hid).

Estimated/unmeasured: iOS 18 acceptance of this composite map, 16-bit relative
axes, AC Pan, the full 16-bit consumer selector range, and individual consumer
usages must be tested on the iPhone XS. The map follows the specified wire layout
instead of claiming that iOS will act on every valid usage. IDF adds boot keyboard
and three-byte boot mouse characteristics; keyboard and button/stepped movement
work in boot mode, while SCROLL and CONSUMER require report mode. `ready=1` requires
all three report-mode subscriptions; individual commands check their own report's
subscription, so keyboard/mouse can work even if iOS omits consumer subscription.
LED writes are handled by the bundled HID service; this board has no keyboard LEDs.

Read (owner-provided hardware findings): retain `CONFIG_BT_NIMBLE_SM_LVL=2`
(iOS did not pair at 1), `CONFIG_ESP_MAIN_TASK_STACK_SIZE=8192` (the default
stack overflowed in `esp_hid`), input notifications despite HID suspend,
REPEAT_PAIRING stale-bond deletion followed by retry during PAIR windows, and
GAP event logging. Hardening retains these fixes; GAP logs use the nonblocking
USB sink described below.
Bonding, no-I/O capability, MITM off, Secure Connections with legacy fallback,
and explicit security initiation remain as in the working BLE firmware.

Read: battery service reports a fixed 100 (USB powered, no battery sensor), not a
measured battery charge. DIS exposes manufacturer and PnP ID. Prototype PnP fields
are source=2 (USB), VID=0, PID=0, version=0; no manufacturer's allocated identifier
is claimed. The bundled DIS implementation uses `strlen()` on binary PnP data;
these zero fields deliberately survive that behavior. Estimated/unmeasured: iOS
acceptance of the unassigned PnP identity and constant battery level needs testing.

## IDF source/API references (paths relative to `$IDF_PATH`)

Read: these are the installed 6.2.0 sources used, not APIs inferred from another version.

| API / behavior | Source path |
|---|---|
| `esp_hidd_dev_init`, battery setter | `components/esp_hid/include/esp_hidd.h`, `components/esp_hid/src/nimble_hidd.c` |
| Descriptor parser, generated boot entries | `components/esp_hid/src/esp_hid_common.c` |
| HID service, Report Reference handles, LED handling, write callbacks | `components/bt/host/nimble/nimble/nimble/host/services/hid/include/services/hid/ble_svc_hid.h`, adjacent `src/ble_svc_hid.c` |
| `nimble_port_init`, event queue, host task | `components/bt/host/nimble/nimble/porting/nimble/include/nimble/nimble_port.h`, adjacent `src/nimble_port.c`, `include/nimble/nimble_port_freertos.h` |
| Advertisement, encryption, disconnect | `components/bt/host/nimble/nimble/nimble/host/include/host/ble_gap.h` |
| Notification queue and mbuf ownership, registration | `components/bt/host/nimble/nimble/nimble/host/include/host/ble_gatt.h` |
| GAP name/appearance | `components/bt/host/nimble/nimble/nimble/host/services/gap/include/services/gap/ble_svc_gap.h` |
| PnP setter and binary handling | `components/bt/host/nimble/nimble/nimble/host/services/dis/src/ble_svc_dis.c` |
| Bond enumeration/deletion, NVS store init | `components/bt/host/nimble/nimble/nimble/host/include/host/ble_store.h`, `host/store/config/src/ble_store_config.c` under that same NimBLE tree |
| NVS persistence and security levels | `components/bt/host/nimble/Kconfig.in` |
| USB RX and console driver | `components/esp_driver_usb_serial_jtag/include/driver/usb_serial_jtag.h`, `usb_serial_jtag_vfs.h` in the same directory |
| NVS name storage | `components/nvs_flash/include/nvs.h` |
| Timed pacing | `components/freertos/FreeRTOS-Kernel/include/freertos/task.h` |
| HID initialization/store-init declaration; iOS security caveat | `examples/bluetooth/esp_hid_device/main/esp_hid_device_main.c`, `examples/bluetooth/esp_hid_device/README.md` |

Read: `esp_hid` constructs the services and handles. Notifications use NimBLE's
`ble_gatts_notify_custom` directly rather than `esp_hidd_dev_input_set`, which
asserts on unavailable report-mode entries or mbuf allocation. This permits
explicit boot-mode routing and bounded recovery from resource exhaustion. Registration wraps and
preserves IDF's own callback; control/protocol writes use an application callback
on the NimBLE task, without relying on optional asynchronous esp_hid events.
No private esp_hid struct layout or hardcoded attribute handles are used.

## Validation recorded for this prototype

Measured (hardening prototype, hardware-free): `idf.py set-target esp32c5 build`
with the component manager disabled, then `idf.py build size`, succeeded.
Final padded application binary: 1,339,136 bytes (0x146f00), with 758,016 bytes
(36%) free in the 2 MiB factory partition. The image-size tool reports 1,338,970
bytes and 159,482 bytes static HP SRAM use; these are not runtime free-heap or
stack-headroom measurements. Bootloader: 22,272 bytes. Our C code compiles with
`-Wall -Wextra -Werror`. Configure emitted the installed upstream NimBLE
Kconfig bool-default note; no IDF sources were changed.

Measured: native parser and ASan/UBSan each passed 23,139 parser checks. Native
runtime tests compile the actual `main.c` against clock/queue/USB/BLE doubles;
BLE tests compile the actual `ble.c` against narrow IDF/NimBLE doubles. Both
normal and sanitizer runs pass. They cover charger/no-reader USB output, TCP
isolation from a full USB queue, STOP during MOVE/SCROLL/CLICK/KEY/CONSUMER,
queued cancellations, overload, action deadlines, failed-release disconnection,
STATUS fields, delayed mbuf availability, notify failure without spin, bounded
retry/cancel, subscription/generation guards, pairing expiry, closed-window
rejection, bonded admission and RPA identity matching via a resolver double.
The installed-IDF descriptor check passed (keyboard 8+1 bytes, mouse 7,
consumer 2, plus three generated boot entries).

Measured: the full host suite passed 202 tests. Loopback `bind()` was unavailable,
so TCP tests used their in-memory socket fallback through the real
`_SocketTransport`; this is not real TCP socket validation. One unrelated
Starlette/httpx deprecation warning remains. The fake device exercises new
commands/fields/errors and motion chunking without hardware.

Estimated/unmeasured: real USB attachment transitions, TCP/BLE latency, slow
connection intervals, RF coexistence, RPA rotation/reconnection on the iPhone,
watchdog reboot timing, runtime heap/stack headroom and iOS release behavior
still need hardware validation. No board was flashed, serial port opened,
Wi-Fi joined or device paired in this session.

Read: SPEC was left unchanged. Proposed clarifications: define the reply for an
invalid request ID, whether LF/CR counts in the line limit, positional optional
arguments, the +/-2000 MOVE/SCROLL bounds, STOP/PAIR and new errors/status fields,
command budgets, pairing admission, and lossy output/overload behavior.

## Wi-Fi provisioning and discovery

Read (implementation): provision only through the USB serial monitor. Replace
all angle-bracket placeholders locally; they are not literal command arguments.
Disable monitor local echo and recording before entering secrets. Firmware never
echoes input, passwords or tokens, and disables Wi-Fi driver diagnostics. No
credentials are compiled into the image. Use a private LAN: AUTH is plaintext
TCP and the ordinary NVS partition is not encrypted.

```text
101 TOKEN <32-to-64-hex-characters>
102 WIFI <percent-encoded-ssid> <percent-encoded-password>
103 STATUS
```

Read: token characters are case-sensitive ASCII hex. Percent-encode UTF-8 bytes
in SSID/password, particularly space (`%20`), percent (`%25`) and any delimiter.
`+` stays a literal plus; `%2B` also decodes to plus. Decoded SSID length is 1–32
bytes, password length 1–64 bytes, with no NUL, ASCII controls or DEL. Use the AP's valid
WPA password (normally 8–63 bytes, or a 64-character hex PSK); invalid AP
credentials cannot establish a connection. The overall 256-byte line limit
still applies, including ID and delimiters, excluding LF. This prototype has
no empty-password syntax for open networks.

Read: `@101 OK` and `@102 OK` acknowledge an NVS commit and scheduling, not an
AP connection. Poll `STATUS` until `wifi=1 ip=<assigned-address>`; otherwise
`wifi=0 ip=-`. Provisioning failures return `ERR INTERNAL` without secret detail.
Driver configuration/start failures emit `@0 EVT wifi_error code=<numeric-error>`.
A Wi-Fi disconnect closes the TCP listener/client. Connection failures retry
with delays of 1, 2, 4, 8, 16 and then 30 seconds; successful IP acquisition resets
the backoff. Credentials and token survive reset. The Wi-Fi driver uses RAM
storage; application NVS owns persistence. To stop and persistently disable Wi-Fi:

```text
104 WIFI off
105 STATUS
```

Read: `WIFI off` clears the saved SSID/password blob, retains the token and BLE
bonds, and schedules station shutdown. Send `WIFI <ssid> <password>` again to
reenable. Replacing TOKEN closes existing TCP authentication; clients must
connect again using the replacement token.

Read (installed IDF tree): there is no bundled Espressif `components/mdns`
component. lwIP has upstream mDNS source files but no enabled/integrated responder
in this IDF build; DNS mDNS *queries* are not an advertiser. Component manager
stays disabled. The smallest dependency-free fallback implemented here sets the
DHCP hostname to `iphone-hand`. Find its lease in the router or read its IP from
serial `STATUS`, then configure TcpLink with that address and port 7878. Router
DNS may resolve the DHCP name; this does **not** advertise `iphone-hand.local`
or `_iphonehand._tcp` as SPEC 7 requests. SPEC is unchanged. Adding Espressif's
external mDNS component later requires relaxing the bundled-components limit.

Read: the listener exists only with a station IP and a configured token. A new
connection replaces the previous client, even before authentication. First line
must be `AUTH <token>` followed by LF (CRLF also works); success is `@0 OK auth`.
Bad/overlong authentication closes the socket; incomplete authentication has a
5-second deadline. Thereafter send the section-1 protocol. WIFI/TOKEN always
return ARG over TCP when recognized, and never provision. Replies go only to
the originating transport; EVT goes to serial and the authenticated TCP session.
Queued replies are pinned to a session, so they cannot leak to a replacement
client. Already admitted commands may still execute after their client closes.

Read: both parsers feed the ordinary FIFO, with STOP priority and immediate
overload replies as described above. Order is admission order, not a comparison
of transport arrival timestamps. TCP output has a separate 64-line queue; a full
queue or a line stalled for 2 seconds disconnects that client. Socket operations
are nonblocking, with TCP_NODELAY and a 5 ms polling interval. TCP dispatch occurs
directly in the publisher, outside the USB queue/task, with atomic session and
slow-client flags; a stalled USB reader cannot block or fill TCP output.

Read: `host/src/iphone_hand/tcp_link.py` uses SerialLink's public API, framing,
event collection, IDs, CFG state and paced-command timeout calculation. Socket
drops trigger one reconnect/AUTH attempt. PING/STATUS can be retried; a submitted
action is not replayed because its lost ACK leaves its execution uncertain.
It raises ConnectionError after recovery; the next command uses the recovered
connection. Timeouts propagate without reconnect/replay. Events are collected
while opening or executing commands, as with the synchronous SerialLink API.

## Wi-Fi troubleshooting

- Read: `wifi=0` after provisioning means no usable DHCP lease yet. Check
  percent escapes, decoded lengths, AP credentials and availability; wait for
  the reconnect backoff. A numeric `wifi_error` refers to `esp_err.h` or
  `esp_wifi.h`. Correct credentials by provisioning again over serial.
- Read: `wifi=1` but no TCP connection: configure the address from serial STATUS,
  port 7878 and the provisioned token. Check AP client isolation and LAN firewall
  rules. The listener requires both an IP and a nonempty token.
- Read: an immediate authentication close usually means a wrong/case-mismatched
  token, malformed first line or another client replacing this one. Reprovision
  TOKEN over serial if necessary. A new client always evicts the old client.
- Read: `.local` discovery is unavailable in this build; use serial STATUS or
  the router's DHCP lease for `iphone-hand`. DHCP hostname resolution depends
  on the router's DNS configuration.
- Read: `ERR NOCONN` is a BLE connection/subscription issue, separate from Wi-Fi.
  Inspect STATUS and GAP logs. Preserve the working security/stack settings.
  After a TCP lost-ACK error, inspect phone state before repeating an action:
  reconnection cannot establish whether the preceding HID reports executed.
- Estimated: under traffic, increasing DTIM delay or RF contention can affect
  both transports' BLE timing. Hardware testing should check free heap, stack
  high-water marks and BLE reconnection with Wi-Fi active before deployment.

## Coexistence and latency

Read (installed ESP-IDF 6.2): `CONFIG_ESP_COEX_SW_COEXIST_ENABLE=y` enables
software RF arbitration. Its Kconfig **selects**
`CONFIG_ESP_WIFI_STA_DISCONNECTED_PM_ENABLE`; keep that enabled. Use station
mode with `WIFI_BAND_MODE_AUTO` for the C5's 2.4/5 GHz support. Band mode is set
**after** `esp_wifi_start`, following IDF's power-save example. Use
`esp_wifi_set_ps(WIFI_PS_MIN_MODEM)` and leave connectionless window/interval
settings at defaults. Ordinary Wi-Fi/BLE coexistence transitions are automatic;
the BLE Mesh status APIs are unnecessary for this HID peripheral. C5 has a
single application CPU, so dual-core affinity advice does not apply.

Read: minimum modem sleep receives on DTIM boundaries. Even `WIFI_PS_NONE`
cannot keep RF active outside Wi-Fi's coexistence time slice. Disconnected
station power saving is forced by software-coexistence Kconfig, rather than an
optional latency tweak. These statements come from the local IDF docs/Kconfig
listed below; see also Espressif's
[Wi-Fi power-save guide](https://docs.espressif.com/projects/esp-idf/en/stable/esp32c5/api-guides/wifi-driver/wifi-performance-and-power-save.html).

Estimated, not measured: USB should have lower and steadier transport latency.
TCP adds up to one 5 ms ingress polling period plus LAN scheduling; modem sleep
can add an AP DTIM cycle, and coexistence/contention can extend delays further.
A healthy LAN might add milliseconds to hundreds of milliseconds relative to
USB, depending on DTIM and radio load. No latency percentile or BLE reliability
claim is made without a hardware test. MOVE's 15 ms default pacing and key/click
hold defaults are unchanged. Read: the new command budget can cut long holds
short; replies still mean reports queued, not UI completion.
Runtime free heap, task stack high-water marks, AP loss/recovery, 2.4/5 GHz
behavior and sustained Wi-Fi/BLE operation remain hardware validation risks.
Plain TCP authentication and plaintext NVS also remain prototype limitations.

Read: every new IDF API/Kconfig used by this change is sourced below, relative
to `$IDF_PATH`; the earlier table covers existing USB/BLE APIs.

| API / configuration | Installed IDF source |
|---|---|
| `WIFI_INIT_CONFIG_DEFAULT`, `esp_wifi_init`, `esp_wifi_set_storage`, `esp_wifi_set_mode`, `esp_wifi_set_config`, `esp_wifi_start`, `esp_wifi_stop`, `esp_wifi_connect`, `esp_wifi_set_ps`, `esp_wifi_set_band_mode` | `components/esp_wifi/include/esp_wifi.h` |
| STA fields, Wi-Fi/IP events, band/power/storage enums; `IPSTR`/`IP2STR` | `components/esp_wifi/include/esp_wifi_types_generic.h`, `components/esp_netif/include/esp_netif_types.h`, `components/esp_netif/include/esp_netif_ip_addr.h` |
| `esp_netif_create_default_wifi_sta` | `components/esp_wifi/include/esp_wifi_default.h` |
| `esp_netif_init`, `esp_netif_set_hostname` | `components/esp_netif/include/esp_netif.h` |
| `esp_event_loop_create_default`, `esp_event_handler_register`, event bases | `components/esp_event/include/esp_event.h`, `components/esp_event/include/esp_event_base.h` |
| `nvs_open`, `nvs_get_blob`, `nvs_set_blob`, `nvs_get_str`, `nvs_set_str`, `nvs_commit`, `nvs_close`; `nvs_flash_init` | `components/nvs_flash/include/nvs.h`, `components/nvs_flash/include/nvs_flash.h` |
| `socket`, `setsockopt`, `bind`, `listen`, `accept`, `recv`, `send`, `shutdown`, `close`, `fcntl`, TCP_NODELAY and byte-order macros | `components/lwip/lwip/src/include/lwip/sockets.h`, `components/lwip/lwip/src/include/lwip/tcp.h`, `components/lwip/lwip/src/include/lwip/inet.h` |
| `xQueueCreate`, `xQueueSend`, `xQueueReceive` | `components/freertos/FreeRTOS-Kernel/include/freertos/queue.h` |
| `xSemaphoreCreateMutex`, `xSemaphoreTake`, `xSemaphoreGive` | `components/freertos/FreeRTOS-Kernel/include/freertos/semphr.h` |
| `xTaskCreate`, `vTaskDelay`, `xTaskGetTickCount`; tick macros/assertions | `components/freertos/FreeRTOS-Kernel/include/freertos/task.h`, `components/freertos/FreeRTOS-Kernel/include/freertos/FreeRTOS.h` |
| `esp_log_level_set`; `ESP_ERROR_CHECK` | `components/log/include/esp_log.h`, `components/esp_common/include/esp_err.h` |
| Software coexistence / forced disconnected power management | `components/esp_coex/Kconfig`, `components/esp_wifi/Kconfig` |
| Automatic arbitration / default connectionless settings / CPU-specific advice | `docs/en/api-guides/coexist.rst` |
| Modem-sleep DTIM delay, RF time slicing with PS_NONE, disconnected sleep restriction | `docs/en/api-guides/wifi-driver/wifi-performance-and-power-save.rst` |
| Band-mode call order | `examples/wifi/power_save/main/power_save.c` |
| Single application CPU | `components/soc/esp32c5/include/soc/soc_caps.h` (`SOC_CPU_CORES_NUM`) |
| Retained stack and security settings | `components/esp_system/Kconfig`, `components/bt/host/nimble/Kconfig.in` |

## Unattended output and watchdog

Read: protocol output uses zero-wait enqueue. Normal ESP_LOG output (including
GAP diagnostics) also uses a nonblocking USB queue sink, preserving log fragments.
Serial protocol lines include a leading LF so an intervening log fragment or
dropped log newline cannot hide the reply/event `@` marker.
The USB task skips writes without USB SOF and uses `usb_serial_jtag_write_bytes`
with a zero-tick timeout otherwise. This is an all-or-zero driver ring-buffer
write, not a per-character VFS write. No printf/fflush/wait-for-drain runs in the
USB task. Each dropped USB queue item, USB write, or full TCP queue enqueue
increments `out_drops` once; a successfully queued driver write is not a delivery
measurement. Diagnostic fragments count too. The counter is unsigned 32-bit,
wraps modulo 2^32 and resets at reboot. Charger-only operation can legitimately
increase it while TCP replies work. Boot/ROM/early diagnostics retain IDF's
console fallback rather than the application's runtime sink.

Read: STATUS retains prior fields and appends `pairing=<0|1> out_drops=<N>
uptime_ms=<N> reset=<reason> reset_code=<IDF-enum>`. Uptime is the monotonic
ESP timer since boot; reset is the reason for the current boot, not a persistent
history. Named reset tokens include poweron, software, panic, int_wdt, task_wdt,
wdt, brownout, deepsleep, usb, and other (the numeric code disambiguates it).

Read: executor and USB output tasks subscribe to a 15-second task watchdog.
Idle queue waits poll within 100 ms; paced waits feed within 100 ms. A blocked
NimBLE operation cannot feed the executor watchdog. The timeout invokes panic,
with silent reboot configured so recovery needs no USB reader. The idle-task
subscription is removed when configuring these explicit task subscriptions.
Estimated: a hard task hang should reboot after watchdog expiry; actual timing
and recovery have not been measured on the board.

Read (installed IDF 6.2; paths relative to `$IDF_PATH`):

| Behavior checked | Source citation |
|---|---|
| USB host consumes FIFO before TX progresses | `components/esp_driver_usb_serial_jtag/src/usb_serial_jtag.c:63` |
| Write mutex and ring buffer each use caller timeout; all-or-zero return | `components/esp_driver_usb_serial_jtag/src/usb_serial_jtag.c:270` |
| VFS no-reader fail-fast policy is 50 ms; driver per-character fallback | `components/esp_driver_usb_serial_jtag/src/usb_serial_jtag_vfs.c:72`, `:740` |
| Connected means USB SOF, not a CDC reader | `components/esp_driver_usb_serial_jtag/src/usb_serial_jtag_connection_monitor.c:33` |
| NOTIFY_TX is emitted synchronously for an attempt; mbuf consumed | `components/bt/host/nimble/nimble/nimble/host/src/ble_gattc.c:5498` |
| Controller backlog is retained by NimBLE | `components/bt/host/nimble/nimble/nimble/host/src/ble_l2cap.c:501` |
| Controller completion returns credits and wakes TX | `components/bt/host/nimble/nimble/nimble/host/src/ble_hs_hci_evt.c:648` |
| Public MSYS free-block probe | `components/bt/host/nimble/nimble/porting/nimble/include/os/os_mbuf.h:311` |
| Protocol Mode write callback after successful write | `components/bt/host/nimble/nimble/nimble/host/services/hid/src/ble_svc_hid.c:682` |
| Public RPA resolver against stored peer IRKs | `components/bt/host/nimble/nimble/nimble/host/include/host/ble_esp_gap.h:360`, `components/bt/host/nimble/nimble/nimble/host/src/ble_gap.c:12470` |
| Bond persistence precedes encryption callback | `components/bt/host/nimble/nimble/nimble/host/src/ble_sm.c:1173` |
| Repeat-pair retry requires stale-bond deletion | `components/bt/host/nimble/nimble/nimble/host/include/host/ble_gap.h:1069` |
| Nonzero store-status callback aborts storage operation | `components/bt/host/nimble/nimble/nimble/host/include/host/ble_store.h:356` |
| Watchdog reconfiguration/subscriptions; TASK_WDT reset hint | `components/esp_system/task_wdt/task_wdt.c:558`, `:381` |
| Silent panic reboot needs no output | `components/esp_system/Kconfig:51` |
