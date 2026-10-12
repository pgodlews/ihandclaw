# Contributor CI

Read: `.github/workflows/ci.yml` runs independent host, benchmark, catalog/playbook,
and native firmware checks on Ubuntu 24.04. Native tests also run ASan/UBSan.
A separate matrix compiles `esp32c3` and `esp32c5` and runs the IDF report-map
check. It never flashes, pairs or accesses a phone. No secrets are required;
checkout credentials are not retained and repository access is read-only.

Read: Python is pinned to 3.12.10, uv to 0.9.2, checkout to v7.0.1's commit,
and setup-python to v7.0.0's commit. ESP-IDF 6.2 has no release yet: "6.2" in
this repo means ESP-IDF master, which reports 6.2.0 in `esp_idf_version.h`
(local reference build: master at 25fe69f9, 2026-07-11). Docker Hub has no
`v6.2` tag (measured 2026-10-12: the first CI run failed with "manifest unknown"),
so firmware CI uses `espressif/idf:latest` pinned by digest
(`sha256:6ad927cfb85a5fea82da64b00960b01adff4fc6019ffcdb18caa3da642d55343`, the image published 2026-10-12). Move to the `v6.2` tag
once Espressif publishes it. Python library ranges remain those in
`host/pyproject.toml`; the repository currently ignores its uv lockfile.

Read: Espressif's [Docker image guide](https://docs.espressif.com/projects/esp-idf/en/latest/esp32/api-guides/tools/idf-docker-image.html)
documents `espressif/idf`, fixed-release tags, bundled tools, and `idf.py build`.
GitHub's [container-job guide](https://docs.github.com/en/actions/how-tos/write-workflows/choose-where-workflows-run/run-jobs-in-a-container)
documents job containers and their default shell; the build explicitly selects
Bash and sources IDF because job steps must not rely on the image entrypoint.
The [checkout](https://github.com/actions/checkout) and
[setup-python](https://github.com/actions/setup-python) documentation describe
credential persistence and Python selection.

Measured 2026-10-12: offline tests passed on GitHub on the first run; see the
Actions tab for the firmware matrix on the pinned master image. The master image
is newer than the local reference build, so a firmware CI failure may come from
upstream IDF changes rather than this repo.
