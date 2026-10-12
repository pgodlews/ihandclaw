# iphone-hand - agent rules

Goal: let an AI agent (Claude via MCP, or a local LM Studio model) operate a real iPhone XS through an
ESP32-C5 or ESP32-C3 acting as a BLE keyboard + mouse, seeing the phone through an AirPlay mirror on a Meta
Portal+ (Wormhole Display) grabbed with ADB. The contract is `docs/SPEC.md`; change it only when
asked, and say so in your report if you think it is wrong.

- `firmware/`: ESP-IDF v6.2 project, targets esp32c5 and esp32c3. Activate the toolchain with
  `export IDF_TOOLS_PATH="<IDF_TOOLS_PATH>"; . "<ESP_IDF_PATH>/export.sh"`.
  Replace `<IDF_TOOLS_PATH>` with your ESP-IDF tools directory and `<ESP_IDF_PATH>`
  with your ESP-IDF checkout directory before running the command.
  Machine-specific values (paths, hosts) belong in an untracked `AGENTS.local.md`; read it if present.
  Build directories stay out of git.
- `host/`: Python >=3.11 uv project, package `iphone_hand`, tests with pytest. Tests must pass without
  hardware (fake serial device, fixture screenshots, FakePhone).
- `fixtures/screens/`: real Portal screenshots of the mirrored phone with labels in `labels.json`.
- `deploy/macos/`: launchd prototype assets to run the receiver + daemon on macOS (see its README);
  platform dispatch lives in `host/src/iphone_hand/platforms.py`, USB screen capture (macOS-only,
  `frame_source = "usb"`) in `host/src/iphone_hand/usb_capture.py`.
- Never flash the board, pair, send HID reports to the phone or drive the phone unless the task says so.
- Never commit secrets, IPs, hostnames, device serials or personal paths. No pushes.
- Label every claim measured / read / estimated. No invented measurements.
