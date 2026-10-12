# Navigation catalog and playbooks

Playbooks are reusable advice for an agent operating an iOS app. They describe
how to enter a task, which text to look for, what can go wrong, and how to verify
the result. They are data, not scripts: loading one cannot send phone actions.

Read: `apps.yaml`, `settings.yaml`, `shortcuts.md` and `Catalog` retain their
existing interfaces. We keep `catalog/` so existing navigation consumers continue
to work; task-level contributions live in `catalog/playbooks/<id>.yaml`, one
file per app/task and locale. Existing catalog entries are navigation seeds,
not proof that a workflow succeeds. In particular, Shortcut invocation and
Spotlight pane jumps must be checked on the actual phone.

## Format version 1

Copy `playbooks/notes_new_note.yaml`. All fields are required and unknown fields
are rejected by `catalog/playbooks.py`:

| Field | Meaning |
|---|---|
| `schema_version` | Integer `1` |
| `id` | Unique lowercase snake_case; matches the filename stem |
| `app_name` | App name to type into Spotlight |
| `task` | What this playbook achieves |
| `ios_version`, `locale` | Scope, such as iOS 18 and en_US; not universal compatibility |
| `entry_steps` | Ordered nonempty text steps; inspect screen state between actions |
| `landmarks` | Nonempty list of expected visible text, not guaranteed tap targets |
| `gotchas` | Nonempty list of UI traps and recovery advice |
| `locale_notes` | Nonempty list of translations, keyboard/layout and autocorrect caveats |
| `verification` | Nonempty list of observable success checks |
| `evidence` | Nonempty list of `{status, source, scope}` mappings |
| `safety` | Nonempty text describing permitted side effects and stop conditions |

Evidence status is exactly `measured`, `read`, or `estimated`. State the scope of
each observation: device/iOS/locale if known, and a repository file or public URL.
Keep unverified advice estimated; do not promote a source reading to a device
measurement. The seed distinguishes owner observations from this prototype's
hardware-free work. Prefer visible text and relative locations over fixed pixel
coordinates. The phone contract uses points; screens returned at pt2 use two
pixels per point. Never treat OCR IDs as stable after the UI changes.

Read: YAML is loaded with `yaml.safe_load`. PyYAML is now a declared host runtime
dependency, since navigation data is useful outside tests too. The catalog does
not depend on the benchmark package or its limited fallback parser. Use ordinary
YAML lists/mappings/scalars; no Python objects or executable content.

## Load and validate

From the repository root:

```sh
uv sync --project host
uv run --project host python -m pytest catalog
uv run --project host python -c 'from catalog.playbooks import load_playbooks; print(list(load_playbooks()))'
```

Adapters can call `load_playbooks()` and provide the appropriate entry to the
model prompt. Read: automatic playbook selection/execution is not wired into the
existing adapters; contributors should document how their adapter uses it.
See [CONTRIBUTING](../CONTRIBUTING.md) for contribution and privacy rules.
