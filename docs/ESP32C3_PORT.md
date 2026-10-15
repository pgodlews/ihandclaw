# ESP32-C3/C5 prototype validation — 2026-10-12

Read: changes are uncommitted on a local branch. No flashing, erasing, monitoring,
serial-device access or pushes
were performed. C3 runtime operation remains unmeasured.

Read: changes:

- `firmware/main/wifi_link.c:174`: gate automatic band selection on
  `SOC_WIFI_SUPPORT_5G`; C3 uses its 2.4 GHz driver path.
- `firmware/main/main.c:175`: PING chip comes from `CONFIG_IDF_TARGET`.
- `firmware/CMakeLists.txt:3`: generated sdkconfig lives in the chosen build
  directory. Both targets use unchanged shared defaults; no target overrides
  are needed. Generated configurations and binaries remain ignored.
- `host/src/iphone_hand/ota.py:25` and `:126`: accept header IDs 0x05/0x17 and
  compare with PING before STATUS or OTA BEGIN. Mismatched, missing or unsupported
  board chips are refused without OTA ABORT or trial confirmation.
- `host/src/iphone_hand/fake.py:31`, `host/src/iphone_hand/cli.py:76`,
  `host/tests/test_serial_link.py:7` and `host/tests/test_ota.py:248`: chip-aware
  fake/help/tests, both upload encodings and current/legacy binary negotiation.
- `firmware/test/Makefile:9`: runtime/PING tests compile for both chip strings.
  `firmware/test/test_ota_wire.c:25` fixes the existing four-buffer assumption
  to use the actual `IH_OTA_WINDOW` bound; firmware pool allocation is unchanged.
- `firmware/README.md:18`, `:33`, `:74`: both target builds, ignored configs,
  2.4 GHz C3 notes, irrelevant LED/BOOT GPIO differences and target-specific flash
  instructions. `AGENTS.md:4`, `:8` and `host/README.md:122`: supported chip wording.

Measured: both requested ESP-IDF 6.2 build commands completed successfully from
`firmware/`, with `IDF_COMPONENT_MANAGER=0` and the installed toolchain activated:

```sh
idf.py -B build/esp32c5 set-target esp32c5 build
idf.py -B build/esp32c3 set-target esp32c3 build
```

Measured: actual application file sizes, against each 0x1f0000 (2,031,616-byte)
OTA slot; host image parsing also verified each real binary's target and version.

| Target | App bytes | Hex | Slot bytes remaining |
|---|---:|---:|---:|
| esp32c5 | 1,358,992 | 0x14bc90 | 672,624 |
| esp32c3 | 1,127,440 | 0x113410 | 904,176 |

Measured: each target emitted five IDF CMake private include-directory dependency
warnings between esp_wifi and wpa_supplicant; no compiler warnings were observed.
Both image partition checks passed. Toolchain sources were not modified.

Measured: `idf.py size` reported C3 `.bss` 20,392 B + `.data` 14,808 B
(35,200 B), with 185,120 B of linker-reported DRAM remaining. C5 `.bss`
27,208 B + `.data` 19,532 B (46,740 B), with 160,960 B HP SRAM remaining.
Estimated: linker remaining memory is not runtime free heap.
Read: two 64-entry output queues (`firmware/main/main.c:336`,
`firmware/main/wifi_link.c:296`), task stacks and Wi-Fi/BLE allocations consume
heap at boot. OTA retains its lazy two-frame allocation
(`firmware/main/ota_wire.c:28`, `firmware/protocol/protocol.h:14`), approximately
8 KiB plus frame metadata (estimated); no new static buffer was introduced.

Measured: host `UV_NO_SYNC=1 uv run pytest`:
334 passed, 1 skipped, 1 warning, Python 3.14.3. The skip was the optional
pymobiledevice3 integration test (`host/tests/test_devservices.py:337`); the
warning was Starlette's httpx TestClient deprecation. Read: the test environment
used `pydantic<2.14`; project dependency requirements were unchanged.

Measured: `make -C firmware/test` passed every suite, including both runtime
variants and 23,139 protocol checks. `make -C firmware/test sanitize` passed
AddressSanitizer/UndefinedBehaviorSanitizer checks. `git diff --check` passed.

Read: SPEC edits are limited to `docs/SPEC.md:18` (C3 hardware row), `:65`
(PING chip alternatives), `:80` (section 2 targets), and `:90` (build command).
Section 1's framing, commands and behavior are otherwise unchanged.

Estimated: first C3 flash should check USB-Serial/JTAG and `chip=esp32c3`,
free heap/largest free block around Wi-Fi initialization and OTA allocation,
stack high-water marks, watchdog/reset diagnostics, BLE pairing/reconnect while
using 2.4 GHz Wi-Fi, and authenticated OTA trial confirmation/rollback. Silent
panic reboot can hide an allocation failure. Builds and host doubles establish
no C3 runtime reliability; no hardware was exercised during this validation.
