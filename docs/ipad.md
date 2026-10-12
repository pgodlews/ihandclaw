# iPad: design notes and what must be measured

**Status: groundwork only. Nothing here has run on an iPad; iPads are not supported.**
Labels: **measured** = observed on hardware (nothing in this file is), **read** = from docs or
source, **estimated** = unverified.

The BLE HID side (`firmware/`) does not depend on the device. The host assumes an iPhone in
several ways, and this note lists them for iPad.

## What exists in code (offline, untested on hardware)

| Piece | Where | Status |
|---|---|---|
| iPad point sizes (portrait, @2x) | `geometry.IPAD_SIZES` (`host/src/iphone_hand/geometry.py`) | read: Apple Displays reference and App Store Connect pixel sizes; two rows estimated (pixels / 2) |
| Size table per family | `device = "ipad"` in daemon.toml, `--device ipad` on the CLI | unit tested with synthetic frames |
| Portrait **and** landscape geometry | `geometry.detect`: a frame wider than tall swaps the table size; `/api/state` `screen.orientation` | unit tested (synthetic) |
| Rotation detection | per frame: the point size follows the frame's orientation | unit tested (synthetic); estimated: AirPlay frames change shape on rotation |
| Pointer detector interface | `pointer.get_detector(name)`, `pointer_detector = "assistivetouch" \| "ipados"` | interface tested; `ipados` is a prototype |
| `ipados` prototype | `pointer.ipados_candidates` (Hough circle + grey, flat, low-contrast fill) | synthetic disc only; size and colour estimated |

Calibration profiles are keyed by the portrait size (`motion.profile_key`). Portrait and
landscape share one profile. Estimated: this holds only if iPadOS maps mouse X/Y to the
rotated screen axes with the same gain. Measure it.

## Pointer (read: Apple HIG, Pointing devices)

Source: https://developer.apple.com/design/human-interface-guidelines/pointing-devices
(read 2026-10-12).

- The default pointer is a **circle**. Over text-entry areas it becomes an I-beam. Apps can
  also give it custom shapes.
- **Highlight** effect: over bar buttons, tab bars and segmented controls, the pointer turns
  into a translucent rounded rectangle behind the control.
- **Lift** effect: over app icons and Control Center buttons, the pointer fades out beneath
  the element and the element scales up with a shadow.
- **Magnetism**: near lift/highlight elements and text, the pointer is pulled toward the
  element's centre, including after a flick.

Consequences for this project:

1. The AssistiveTouch red-ring detector does not apply to the native pointer. Single-frame
   detection loses the pointer whenever it morphs or fades, which is exactly over tap
   targets. Closed-loop `move_to` therefore needs another strategy. Candidates: confirm the
   position on an empty area before the final approach; detect the highlight shape; or trust
   the model for the last step. All are unbuilt.
2. Magnetism breaks the linear-in-parameters gain model near targets. A move that ends next
   to a button may land on its centre. The calibration plan must run over empty background.
3. Estimated (not re-read for this note): iPadOS has a Settings > Accessibility > Pointer
   Control page with pointer colour, size, contrast, auto-hide and tracking speed. A
   coloured pointer border might let a ring-style detector work. Measure what it looks like.
   Also measure whether AssistiveTouch is needed for a BLE mouse on iPadOS at all.

## Screen and mirroring

- Read (App Store Connect screenshot specifications): iPad screens are about 3:4 portrait, at
  pixel sizes from 1488 × 2266 to 2064 × 2752.
- Estimated: the daemon starts UxPlay with `-s 1125x2436` (a portrait iPhone box). An iPad,
  above all in landscape, would be scaled into that box and arrive small. The receiver size
  probably needs to fit the iPad. Untested.
- Stage Manager, Split View and Slide Over put several apps on screen. Coordinates stay
  full-screen points. Home, Back (left-edge swipe) and Spotlight may behave differently per
  window. The agent prompt and the Benchy predicates assume one full-screen app.

## Keyboard shortcuts (all unverified)

Estimated, from general iPadOS knowledge; not re-read and not measured. Apple's support pages
could not be fetched for this note:

| Intent | Candidate shortcut | Note |
|---|---|---|
| Home | Cmd+H; Globe+H | Cmd+H is the current `home_action` default for iPhone |
| Spotlight | Cmd+Space | as on iPhone |
| App switcher | Cmd+Tab; Globe+Up | |
| Dock | Globe+A | |
| Control Center / Notification Center | Globe+C / Globe+N | |

Open HID question: the Globe key is not a usage on the standard keyboard page that
`host/src/iphone_hand/keys.py` maps. Estimated: Apple keyboards send it through a vendor or
consumer usage. The firmware sends keyboard and consumer reports separately, so a held
Globe+H combination may need a new firmware command. Measure which usage iPadOS accepts
before changing `docs/SPEC.md`.

## Measurement checklist for a real iPad (owner)

1. Pair the board; check that a BLE mouse shows the native pointer, with and without
   AssistiveTouch.
2. Screenshots or AirPlay frames of the pointer at rest: over plain background, over a text
   field, over a bar button and over an app icon. Record diameter (pt), colour and opacity.
   Repeat with Pointer Control colour/size changes.
3. `/api/state` with `device = "ipad"`: check `screen.pt`, `orientation` and `ambiguous` in
   portrait and landscape. Set `screen_pt` if needed.
4. Gain: run `calibrate` over an empty area (stop the daemon first; see CONTRIBUTING).
   Record the fit and residuals. Repeat in landscape. Note pointer tracking-speed settings.
5. Magnetism: moves that end 5–30 pt from a button. Record where the pointer lands.
6. Keyboard: which of the shortcuts above work; which HID usage the Globe key needs.
7. Stage Manager on/off: what the mirror shows, and what Home and Back do.

Until these are measured, keep `docs/SPEC.md` and the README iPhone-only.
