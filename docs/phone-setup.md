# Phone setup and measured run caveats

**Measured (owner-supplied, 2026-10-12/13):** these observations concern one
iPhone XS running iOS 18.7.10, with a 375 × 812 pt screen. No new phone measurements
were taken for this documentation update. Unless stated otherwise, observations
below are from 2026-10-13. **Estimated:** applicability to other phones or iOS
versions remains unverified.

## Settings for agent runs

**Measured:** all paths start in Settings. The current phone configuration is:

| iOS 18 menu path | Current setting |
|---|---|
| Accessibility > Pointer Control > Automatically Hide Pointer | OFF |
| Accessibility > Motion > Reduce Motion | ON |
| Accessibility > Display & Text Size > On/Off Labels | ON |
| Accessibility > Display & Text Size > Button Shapes | ON |
| Display & Brightness > Auto-Lock | Never |
| Accessibility > Keyboards & Typing > Full Keyboard Access | OFF |
| Accessibility > Voice Control | OFF |

**Measured:** Pointer Control and Keyboards & Typing appear under the
**ACCESSORIES** heading in Accessibility. Pointer Control is not inside
AssistiveTouch. Disabling Automatically Hide Pointer did not fix the Notes
editing case below. **Estimated benefit:** On/Off Labels may make switch state
readable to OCR; Button Shapes may make tap targets easier to identify.

**Measured:** hardware keyboard Auto-Capitalisation changed line-start `iOS` to
`IOS`. **Estimated recommendation, not yet applied or validated:** turn OFF
General > Keyboard > Hardware Keyboard > Auto-Capitalisation, then verify exact
case at a line start.

## Full Keyboard Access and Notes

**Measured (2026-10-12/13):** Notes editing, identified by the visible **Done**
button, hides the AssistiveTouch pointer. Mouse moves do not bring it back.
Esc hides only the on-screen keyboard; Done and editing remain, and the pointer
stays hidden. Turning off Automatically Hide Pointer did not change this.

**Measured with FKA ON:** `ctrl+tab` left Typing Mode; `tab+f` opened Find;
typing `Done` and pressing `return` ended editing and restored the pointer
(**2/2** attempts). This is a tested sequence under FKA, not a recommendation to
leave FKA enabled. Other measured outcomes with FKA ON:

| Action | Measured outcome |
|---|---|
| Type digits into Calculator | Ignored, **3/3** attempts |
| Launch an app through Spotlight | App did not open, **1/1** attempt |
| Type the first space in a freshly focused text field | Space dropped, **2/2** attempts |
| Send `tab+b` | Moved focus instead of going back; attempt count not supplied |

**Measured:** FKA is now OFF again. Notes reopened its last note in editing mode;
`cmd+n` did nothing in Notes. **Estimated / unverified:** a reliable way to end
Notes editing through the keyboard with FKA OFF remains unresolved on this phone.
The successful FKA sequence does not establish such a method.

## App state and OCR during a run

- **Measured:** Spotlight retained its query, Settings its page, and Notes its
  last note/editing state. **Estimated guidance:** inspect the current frame
  after launch rather than assuming a fresh app screen.
- **Measured:** Settings was navigated by tapping. The daemon `back` action
  (left-edge swipe) failed there; tapping the top-left navigation back button
  worked.
- **Measured:** Calculator showed backspace, not AC, while input was on screen.
- **Measured:** OCR read `=` as a CJK glyph, missed `×` / `÷`, and read `iOS` as
  `IOS`. This OCR error is separate from the measured Auto-Capitalisation change
  to the actual typed text. **Estimated guidance:** compare the image when exact
  case or an operator matters.

## Key chords supported by the host

**Read:** the daemon forwards its `key` action's `combo` to `Phone.key`
(`host/src/iphone_hand/daemon_control.py:176`). A modifier plus one plain key,
such as `cmd+space` or `ctrl+tab`, uses the existing `KEY` press/release command.
If that parser rejects the input, `Phone.key` tries `parse_chord` and requires
at least two plain keys for this fallback (`host/src/iphone_hand/phone.py:192`).

**Read:** `parse_chord` accepts modifiers and up to six distinct non-modifier
keys, preserves their order, and rejects duplicate or unknown keys. Use `plus`
for the literal plus key. `_chord` sends cumulative `KREPORT` states: for `tab+f`,
Tab alone, then Tab and F held together, then an all-zero release in `finally`.
The configured default wait after each press state is 0.06 s; this is code, not
a measured BLE timing guarantee (`host/src/iphone_hand/keys.py:32`,
`host/src/iphone_hand/phone.py:205`; wire format: `docs/SPEC.md:73`).

**Estimated / unverified:** accepting and sending a chord does not establish an
iOS shortcut. The measured `tab+f` result above required FKA ON, and `tab+b` did
not perform Back in the observed run.

## Ending Notes editing without the pointer (noteslist shortcut)

**Measured (2026-10-15):** Notes reopens the last note in editing mode, where the pointer is not drawn.
With Full Keyboard Access OFF, these did **not** end editing: Esc (hides the keyboard only),
cmd+Return, turning off Automatically Hide Pointer. What works: a Shortcuts shortcut named
`noteslist` containing the single action *Show Notes Folder* (folder: Notes). Running it while a
note is being edited brings Notes to the front showing that note **not** in editing mode, with a
`< Back` button; the pointer is visible again (move error < 3 pt).

**Measured:** a brand-new shortcut did not appear in Spotlight at first (web results only); it
appeared later. The Benchy reset therefore opens the Shortcuts app (pointer visible there) and taps
the tile whose OCR text is exactly `noteslist`, then taps `Back` until the notes list is verified.
**Measured:** after an app was opened from Spotlight, Home returns to the Spotlight results first; a
second Home reaches the home screen. The reset handles this with one bounded extra Home.
