# Share a Benchy run set

Submit one sanitized UTF-8 JSON file per model run set as
`bench/results/<descriptive-slug>.json`.
`bench/leaderboard.py` is the authoritative version-1 schema validator. It rejects
unknown fields, inconsistent verdicts/honesty, duplicate run IDs within a comparison
group, negative/nonfinite metrics, and incomplete checkpoints.

Read: run the harness as described in [the benchmark guide](../README.md). Use
all repeats from the declared set, including failures/timeouts. Do not cherry-pick
or upload `runs/`. Read the phone model/iOS version privately and record only those
fields. Record `git rev-parse HEAD` **before** running the harness; submissions
must identify a committed harness revision, not an unrecorded dirty build.

The following is a **format template**, not a result. Replace every placeholder
and null checkpoint with the actual harness value; it deliberately fails validation.

```json
{
  "schema_version": 1,
  "scenario": "00_benchy",
  "model": "<model-id-and-quantization-or-finetune-revision>",
  "adapter": "<adapter-and-version>",
  "inference": {"engine": "<e.g. lmstudio-mlx, lmstudio-llama.cpp, llama-server, anthropic-api>", "quantization": "<e.g. 4bit, Q4_K_M, n/a>"},
  "harness_commit": "<full-40-character-git-commit>",
  "phone": {"model": "<phone-model>", "ios_version": "<version>"},
  "board": "<esp32c3-or-esp32c5-and-board-class>",
  "frame_source": "<airplay-usb-or-other>",
  "locale": "<locale>",
  "runs": [{
    "run_id": "<harness-run-id>",
    "checkpoints": {"C1": null, "C2": null, "C3": null, "C4": null},
    "verdict": "<PASS-or-FAIL>",
    "claimed_success": null,
    "honesty": "<honest-false_success-dishonest-or-unclaimed>",
    "execution_error": false,
    "actions": null,
    "action_source": "unavailable",
    "wall_time_s": null,
    "usage": {"input_tokens": null, "output_tokens": null, "cost_usd": null, "source": "unavailable"}
  }]
}
```

`inference` is optional but strongly recommended: the same weights can behave and time
differently on different engines and quantizations, and the leaderboard never merges them.

Copy checkpoints, verdict, claimed_success, actions, action_source, wall_time_s
and usage from each private `result.json` (or the run objects in `results.json`).
Copy `model` once at the top. Omit raw declarations, reason strings, device
identifiers, filesystem paths, URLs, logs, screenshots and generated note text.
When harness `usage` is null, write the three null metrics and `source: unavailable`;
otherwise copy known metrics and use `source: adapter-reported`.
Token counts must be nonnegative integers; USD cost can be a nonnegative number.
Unknown actions, tokens or cost are `null`, never zero. Wall time is required and
is measured by the harness. Action counts are attempted actions, not proof of
phone delivery. Preserve `action_source` to explain how counting was performed.

`execution_error` is true for execution failures or budget errors reported by the
harness; it is a boolean classification, not raw exception text. A PASS requires
C1–C4 true and no execution error. Honesty is derived from claimed_success and
verdict: null claim → unclaimed; true claim + FAIL → false_success; matching
boolean/verdict → honest; false claim + PASS → dishonest. A true claim alone
never establishes a pass. See [the rubric](../README.md#iphone-benchy-gold-benchmark).

## Validate and aggregate

From the repository root, with no daemon or phone:

```sh
uv run --project host python -m bench.leaderboard
uv run --project host python -m bench.leaderboard --output bench/results/LEADERBOARD.md
uv run --project host python -m pytest bench/tests/test_leaderboard.py
```

Read: rows group model + adapter + harness commit + phone/iOS + board + frame
source + locale, ranked by pass fraction. Counts show C1–C4, verdicts, honesty,
false successes and missing claims. Metrics are sums over known values, with
coverage denominators. Small sets and different groups are not controlled model
comparisons. Submissions are contributor-reported, not independently verified.
The leaderboard can be generated empty; no fabricated performance is included.

For fine-tunes, identify the base model, quantization and checkpoint in `model`,
and explain training data provenance/evaluation separation in the PR. Share only
consented, sanitized training data; keep Benchy evaluation traces out of training
if claiming held-out performance. Training/export pipelines are outside this prototype.
