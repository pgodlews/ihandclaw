# Before public publication

The project is MIT-licensed (`LICENSE`, Copyright (c) 2026 Piotr Godlewski).
The facts below are the third-party pieces that keep their own terms; this is not legal advice.

## Dependency and asset facts

| Item | Read fact / source | Owner decision |
|---|---|---|
| UxPlay 1.73.6 | [GPLv3 licence](https://github.com/FDH2/UxPlay/blob/v1.73.6/LICENSE). The host supervises an external executable; it does not import or link UxPlay (`host/src/iphone_hand/airplay_receiver.py`). | Review obligations if shipping binaries/images with UxPlay and its linked libraries; the current setup asks users to install it separately. |
| ESP-IDF | Apache-2.0 top-level licence (`$IDF_PATH/LICENSE`; [upstream](https://github.com/espressif/esp-idf/blob/master/LICENSE)). Individual bundled components have their own licences. | Review firmware redistribution notices for the actual SDK/component versions. |
| pymobiledevice3 11.26.0 | [Package metadata](https://github.com/doronz88/pymobiledevice3/blob/v11.26.0/pyproject.toml) declares GPL-3.0-or-later. Optional `devservices` extra; `host/src/iphone_hand/devservices.py` imports it. | This optional imported dependency deserves separate compatibility review; it is not merely an external UxPlay subprocess. |
| PyYAML 6.0.3 | [MIT licence](https://github.com/yaml/pyyaml/blob/6.0.3/LICENSE). New runtime dependency in `host/pyproject.toml`. | Preserve required notices when redistributing it. |
| Digit recognition templates | `host/src/iphone_hand/elements.py:4` says the existing raster templates are synthetic Arial glyphs; `digit_templates.json` holds masks. No font file is added by this prototype. | Owner decision 2026-10-12: not a release blocker. The file holds ten ~24 px binary masks rendered from Arial, no font file or outline data (read). Optional later: rebuild from real Voice Control badge crops (iOS draws them in SF), which should also improve recognition. |
| UI and annotation fonts | `host/src/iphone_hand/static/index.html` uses system-ui; `elements.py` uses Pillow's default font for image labels. | Review bundled font notices if distributing rendered assets or a packaged runtime. No new fonts or third-party document templates were added here. |
| Models / training data | Supplied separately by contributors. This repo does not grant rights to upstream model weights or private phone traces. | Verify model/checkpoint terms and consent before sharing training sets or weights. |

Read: this is a focused list of licence-sensitive integration facts, not a
complete transitive dependency inventory. Before selecting terms, inventory the
resolved Python packages, optional OCR/ONNX models, SDK components, fixture rights
and any bundled runtime artifacts. Record versions and notices for what is
actually distributed.

## Release checklist for the owner

- [x] Project licence: MIT (2026-10-12).
- Audit existing tracked files **and Git history** for private hosts, paths,
  session references, tokens and identifiers; older technical docs contain
  internal deployment references. This prototype's new examples use placeholders.
- Review screenshots/labels and template provenance before publication.
- Run clean online `uv sync` and the GitHub workflow. Verify the official IDF 6.2
  image tag and record a digest; local builds do not validate the hosted image.
- [x] First sanitized Benchy submission: claude-haiku-4-5, 3/3 (2026-10-12).
- Validate macOS capture/permissions and sustained C3 behavior on hardware.

Read: SPEC still describes child-receiver/frame-age behavior and mDNS discovery,
while current deployment uses an external receiver/connection state and firmware
has a discovery fallback. Host docs also retain historical prototype-scope notes.
These discrepancies were found by reading the repo; `docs/SPEC.md` is unchanged.
Reconcile them in a separately authorized contract/documentation update.
