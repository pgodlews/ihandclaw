# Contributor-readiness prototype validation

Measured on 2026-10-12, without real phone, daemon, board, flashing or pairing:

| Check | Outcome |
|---|---|
| Host pytest suite | 393 passed, 1 skipped; optional pymobiledevice3 is absent. One existing Starlette TestClient deprecation warning. |
| Bench pytest suite, after final validator changes | 86 passed, 1 skipped; the fake-daemon socket-binding integration test could not bind a local socket. |
| Catalog pytest suite | 21 passed, including all 11 formerly failing loader tests and 10 playbook validation tests. |
| Replay pytest suite | 23 passed; one existing Starlette TestClient deprecation warning. |
| `make -C firmware/test` | All seven executables passed, including C3/C5 runtime variants and 23,139 protocol checks. |
| Native ASan/UBSan | Passed all targets in the existing `sanitize` recipe. |
| Installed IDF report-map parser | Passed `make -C firmware/test check-map`. |
| IDF 6.2 C3 build | Passed `idf.py -B build/esp32c3 set-target esp32c3 build`. |
| IDF 6.2 C5 build | Passed `idf.py -B build/esp32c5 set-target esp32c5 build`. |
| Empty results validation/leaderboard | Passed; no real or invented benchmark submissions. |
| Workflow YAML, local document links, whitespace | Passed structural/parse and local-link checks; `git diff --check` plus whitespace review of new files. Hosted CI was not run. |

Measured: each IDF build emitted five CMake component-validation warnings about
private include-directory dependencies. These did not prevent either build.

Read: local Python validation used Python 3.14.3, pytest 9.1.1, a copy of existing
cached base dependencies, and a locally available PyYAML 6.0.3 package. PyYAML
was not downloaded. Clean offline installation could not resolve the cached
package set; in particular PyYAML was absent from uv's cache. The declared
runtime requirement is `PyYAML>=6.0.2,<7` in `host/pyproject.toml:7`.
**Owner TODO:** run clean online `uv sync --project host` and hosted CI before
publication. Local tests validate the change, not a clean dependency installation.
All validation environments, build output and temporary logs remain ignored
inside the worktree.

Read: changes are uncommitted on a local branch; no pushes or publication occurred.
`docs/SPEC.md` is unchanged.

Read: contributor entry points are `README.md`, `CONTRIBUTING.md`,
`catalog/README.md`, `bench/results/README.md`, `.github/workflows/ci.yml` and
`.github/ISSUE_TEMPLATE/`. Choices and release TODOs are recorded in
[publication notes](publication.md) and [CI notes](contributor-ci.md).
