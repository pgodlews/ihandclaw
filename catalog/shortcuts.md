# iOS Apple Shortcuts Catalog for iphone-hand

This document defines a recommended suite of **10 Apple Shortcuts** for the owner of the iPhone XS to configure once in the iOS Shortcuts app.

---

## 1. Context & Motivation

As detailed in `docs/SPEC.md` (Sections 5 & 8):
- The agent drives an iPhone XS (iOS 18, English UI) via hardware keyboard + BLE mouse over serial/TCP.
- Vision-only GUI interaction (OCR + mouse move + click) requires multi-frame roundtrips (settle delay ~350 ms, grab ~2.1 s over ADB or ~20 ms over UxPlay, OCR inference, closed-loop corrections).
- **Spotlight invocation (`Cmd+Space` -> type shortcut name -> `Return`)** executes registered Apple Shortcuts immediately and deterministically in $O(1)$ steps without touching the mouse pointer or hunting across screens.

---

## 2. Core Set of 10 Proposed Shortcuts

Every shortcut listed below uses native, built-in Apple Shortcuts actions present in iOS 18.

### Shortcut 1: `Toggle Dark Mode`
- **Spotlight Invocation:** `Cmd+Space` -> `Toggle Dark Mode` -> `Return`
- **Purpose:** Toggles system appearance between Light and Dark mode without navigating `Settings > Display & Brightness`.
- **Exact Shortcuts Actions:**
  1. Action: **Set Appearance**
     - Parameter: `Toggle` (alternates between `Turn Light` and `Turn Dark`)
- **Inputs:** None
- **Confirmation Landmark:** Background color of active apps or Home dock shifts dark/light.
- **Claim Status:** `read` (Apple Shortcuts built-in action *Set Appearance*).

---

### Shortcut 2: `Toggle Low Power Mode`
- **Spotlight Invocation:** `Cmd+Space` -> `Toggle Low Power Mode` -> `Return`
- **Purpose:** Toggles iOS battery saving mode on/off without opening `Settings > Battery`.
- **Exact Shortcuts Actions:**
  1. Action: **Set Low Power Mode**
     - Parameter: `Toggle` (alternates between `Turn On` and `Turn Off`)
- **Inputs:** None
- **Confirmation Landmark:** Status bar battery icon turns yellow (Low Power Mode on) or normal white (off).
- **Claim Status:** `read` (Apple Shortcuts built-in action *Set Low Power Mode*).

---

### Shortcut 3: `Toggle Do Not Disturb`
- **Spotlight Invocation:** `Cmd+Space` -> `Toggle Do Not Disturb` -> `Return`
- **Purpose:** Silences notifications and incoming alerts during automated agent execution.
- **Exact Shortcuts Actions:**
  1. Action: **Set Focus**
     - Focus: `Do Not Disturb`
     - State: `Toggle`
     - Duration: `Until Turned Off`
- **Inputs:** None
- **Confirmation Landmark:** Lock screen or Control Centre displays crescent moon icon / "Do Not Disturb".
- **Claim Status:** `read` (Apple Shortcuts built-in action *Set Focus*).

---

### Shortcut 4: `Open URL in Safari`
- **Spotlight Invocation:** `Cmd+Space` -> `Open URL in Safari` -> `Return`
- **Purpose:** Navigates directly to any web address placed on the clipboard, bypassing the Safari address bar UI, virtual keyboard, and suggestions popup.
- **Exact Shortcuts Actions:**
  1. Action: **Get Clipboard**
  2. Action: **Open URL**
     - URL: `[Clipboard]`
- **Inputs:** URL string previously placed in system clipboard via HID key typing (`Cmd+C` or agent paste).
- **Confirmation Landmark:** Safari launches directly displaying the web page.
- **Claim Status:** `read` (Apple Shortcuts built-in actions *Get Clipboard*, *Open URL*).

---

### Shortcut 5: `Set Timer 5 Min`
- **Spotlight Invocation:** `Cmd+Space` -> `Set Timer 5 Min` -> `Return`
- **Purpose:** Sets a standard 5-minute countdown timer without opening Clock and spinning numerical picker wheels.
- **Exact Shortcuts Actions:**
  1. Action: **Start Timer**
     - Duration: `5`
     - Unit: `Minutes`
- **Inputs:** Fixed 5-minute duration (or duplicates like `Set Timer 1 Min`, `Set Timer 10 Min`).
- **Confirmation Landmark:** Clock timer banner or Live Activity countdown starts.
- **Claim Status:** `read` (Apple Shortcuts built-in action *Start Timer*).

---

### Shortcut 6: `Toggle AssistiveTouch`
- **Spotlight Invocation:** `Cmd+Space` -> `Toggle AssistiveTouch` -> `Return`
- **Purpose:** Toggles AssistiveTouch on or off. Critical for `iphone-hand` because mouse pointer visibility and movement depend on AssistiveTouch (`docs/SPEC.md` lines 23-24, 31).
- **Exact Shortcuts Actions:**
  1. Action: **Set AssistiveTouch**
     - Parameter: `Toggle` (alternates between `Turn On` and `Turn Off`)
- **Inputs:** None
- **Confirmation Landmark:** Red ring mouse pointer (or AssistiveTouch menu button if configured) appears or disappears.
- **Claim Status:** `read` (Apple Shortcuts built-in accessibility action *Set AssistiveTouch*).

---

### Shortcut 7: `Toggle Voice Control`
- **Spotlight Invocation:** `Cmd+Space` -> `Toggle Voice Control` -> `Return`
- **Purpose:** Toggles the Voice Control system overlay. Under iOS 18 with "Item Numbers" enabled, this displays number badges next to all clickable elements (`docs/SPEC.md` Section 8, line 196).
- **Exact Shortcuts Actions:**
  1. Action: **Set Voice Control**
     - Parameter: `Toggle`
- **Inputs:** None
- **Confirmation Landmark:** Numbered badges (`vc1`, `vc2`...) appear at top-left of tappable screen elements.
- **Claim Status:** `read` (Apple Shortcuts built-in action *Set Voice Control*).

---

### Shortcut 8: `Toggle Wi-Fi`
- **Spotlight Invocation:** `Cmd+Space` -> `Toggle Wi-Fi` -> `Return`
- **Purpose:** Toggles Wi-Fi radio on/off for network diagnostics, reconnecting dropped AirPlay streams, or cycling local LAN.
- **Exact Shortcuts Actions:**
  1. Action: **Set Wi-Fi**
     - Parameter: `Toggle`
- **Inputs:** None
- **Confirmation Landmark:** Status bar Wi-Fi indicator toggles; Settings > Wi-Fi state flips.
- **Claim Status:** `read` (Apple Shortcuts built-in action *Set Wi-Fi*).

---

### Shortcut 9: `Toggle Bluetooth`
- **Spotlight Invocation:** `Cmd+Space` -> `Toggle Bluetooth` -> `Return`
- **Purpose:** Toggles Bluetooth radio for reconnecting or re-bonding the ESP32-C5 HID adapter.
- **Exact Shortcuts Actions:**
  1. Action: **Set Bluetooth**
     - Parameter: `Toggle`
- **Inputs:** None
- **Confirmation Landmark:** Control Centre Bluetooth icon status changes.
- **Claim Status:** `read` (Apple Shortcuts built-in action *Set Bluetooth*).
- **Caveat:** Turning Bluetooth off disconnects the BLE keyboard/mouse. Agent should only toggle off if an alternative recovery channel (e.g. owner interaction or scheduled turn-on automation) is present.

---

### Shortcut 10: `Mute Volume`
- **Spotlight Invocation:** `Cmd+Space` -> `Mute Volume` -> `Return`
- **Purpose:** Sets media playback and alert volume to 0% to prevent unexpected audio output during unattended testing.
- **Exact Shortcuts Actions:**
  1. Action: **Set Volume**
     - Target: `0%`
- **Inputs:** None
- **Confirmation Landmark:** iOS volume HUD badge displays muted speaker icon.
- **Claim Status:** `read` (Apple Shortcuts built-in action *Set Volume*).

---

## 3. Bonus Utility Shortcuts (Optional)

These shortcuts provide additional convenience for advanced diagnostics:

| Shortcut Name | Actions | Purpose |
|---|---|---|
| `Copy Device Diagnostics` | 1. **Get Battery Level**<br>2. **Get Current Wi-Fi Network**<br>3. **Text** (Format: `battery={Battery Level}% wifi={Wi-Fi Network}`)<br>4. **Copy to Clipboard** | Dumps device battery and SSID to clipboard for OCR/agent readback. |
| `Take Screenshot` | 1. **Take Screenshot**<br>2. **Save to Recent Photos** | Captures native resolution screenshot to Camera Roll. |
| `Open Settings Root` | 1. **Open App** -> `Settings` | Instant return to top-level Settings page. |

---

## 4. One-Time Setup Guide for Phone Owner

To configure these shortcuts on the iPhone XS:

1. **Open the Shortcuts app** (`com.apple.shortcuts`).
2. Tap the **`+`** button in the top-right corner to create a new shortcut.
3. Tap the shortcut title at the top and rename it exactly as specified (e.g. `Toggle Dark Mode`).
4. Tap **Add Action**, search for the action name in the search bar (e.g. `Set Appearance`), and tap to insert it.
5. Tap the parameter pill (e.g. `Turn`) and change it to `Toggle` (or target value).
6. Tap **Done** in the top-right corner.
7. Repeat steps 2–6 for the remaining 9 shortcuts.

### Ensuring Spotlight Indexing
In iOS 18:
- Shortcuts are automatically indexed in Spotlight under their exact names.
- Verify that Spotlight Search is enabled for Shortcuts:
  `Settings > Search > Shortcuts` -> ensure **Show App in Search** and **Show Content in Search** are turned **ON**.

---

## 5. Verification & Safety Notes

- **Naming Collision Avoidance:** Names are chosen to be unambiguous (e.g. `Toggle Dark Mode` rather than simply `Dark`). This ensures Spotlight ranks the shortcut as the **Top Hit** rather than web search or dictionary definitions.
- **Execution Confirmation:** Because Spotlight launches the shortcut in the background or displays a compact status banner, OCR verification should check for the expected visual landmark (e.g. appearance shift, status bar badge, or overlay changes).
- **Status:** Action specifications are verified (`read`) against standard iOS 18 Shortcuts action library. First-run Spotlight ranking on an unindexed phone should be confirmed on device (`unknown - verify on device`).
