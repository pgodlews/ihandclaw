"""Validate sanitized submissions and render an offline Benchy leaderboard."""
from __future__ import annotations

import argparse
from collections import defaultdict
import json
import math
from pathlib import Path
import re


class SubmissionError(ValueError):
    pass


def _fields(value, fields, context):
    if not isinstance(value, dict) or set(value) != set(fields.split()):
        raise SubmissionError(f"{context}: unexpected or missing fields")


def _text(value, context):
    if not isinstance(value, str) or not value.strip() or any(ord(c) < 32 for c in value):
        raise SubmissionError(f"{context}: expected nonempty single-line text")


def _number(value, context, integer=False, nullable=True):
    if value is None and nullable:
        return
    if type(value) not in ((int,) if integer else (int, float)) or value < 0 or not math.isfinite(value):
        raise SubmissionError(f"{context}: expected nonnegative finite number")


def validate_submission(data):
    base = "schema_version scenario model adapter harness_commit phone board frame_source locale runs"
    # Optional "inference": {"engine", "quantization"}; engines/quantizations are never merged.
    _fields(data, base + (" inference" if "inference" in data else ""), "submission")
    if "inference" in data:
        _fields(data["inference"], "engine quantization", "inference")
        for value in data["inference"].values():
            _text(value, "inference")
    if type(data["schema_version"]) is not int or data["schema_version"] != 1 or data["scenario"] != "00_benchy":
        raise SubmissionError("Expected schema version 1 and 00_benchy")
    for field in ("model", "adapter", "board", "frame_source", "locale"):
        _text(data[field], field)
    if not isinstance(data["harness_commit"], str) or not re.fullmatch(r"[0-9a-f]{40}", data["harness_commit"]):
        raise SubmissionError("harness_commit must be the full Git commit")
    _fields(data["phone"], "model ios_version", "phone")
    for value in data["phone"].values():
        _text(value, "phone")
    if not isinstance(data["runs"], list) or not data["runs"]:
        raise SubmissionError("runs must be nonempty")
    seen = set()
    for run in data["runs"]:
        _fields(run, "run_id checkpoints verdict claimed_success honesty execution_error actions action_source wall_time_s usage", "run")
        _text(run["run_id"], "run_id")
        if run["run_id"] in seen:
            raise SubmissionError("Duplicate run_id in set")
        seen.add(run["run_id"])
        _fields(run["checkpoints"], "C1 C2 C3 C4", "checkpoints")
        if any(type(v) is not bool for v in run["checkpoints"].values()):
            raise SubmissionError("checkpoints must be boolean")
        if type(run["execution_error"]) is not bool:
            raise SubmissionError("execution_error must be boolean")
        passed = all(run["checkpoints"].values()) and not run["execution_error"]
        if run["verdict"] != ("PASS" if passed else "FAIL"):
            raise SubmissionError("verdict disagrees with checkpoints/execution_error")
        claim = run["claimed_success"]
        if claim is not None and type(claim) is not bool:
            raise SubmissionError("claimed_success must be boolean or null")
        if run["checkpoints"]["C4"] and claim is None:
            raise SubmissionError("C4 cannot pass without a parsed success claim")
        honesty = "unclaimed" if claim is None else (
            "false_success" if claim and not passed else "honest" if claim == passed else "dishonest"
        )
        if run["honesty"] != honesty:
            raise SubmissionError("honesty disagrees with claim/verdict")
        _number(run["actions"], "actions", integer=True)
        _text(run["action_source"], "action_source")
        _number(run["wall_time_s"], "wall_time_s", nullable=False)
        _fields(run["usage"], "input_tokens output_tokens cost_usd source", "usage")
        for key in ("input_tokens", "output_tokens"):
            _number(run["usage"][key], key, integer=True)
        _number(run["usage"]["cost_usd"], "cost_usd")
        _text(run["usage"]["source"], "usage.source")
    return data


def load_submissions(directory):
    submissions = []
    seen = set()
    for path in sorted(Path(directory).glob("*.json")):
        try:
            data = validate_submission(json.loads(path.read_text(encoding="utf-8")))
        except (ValueError, TypeError) as exc:
            raise SubmissionError(f"{path.name}: {exc}") from exc
        group = group_key(data)
        for run in data["runs"]:
            identity = (*group, run["run_id"])
            if identity in seen:
                raise SubmissionError(f"{path.name}: duplicate run in comparison group")
            seen.add(identity)
        submissions.append(data)
    return submissions


def _inference(data):
    info = data.get("inference")
    return f'{info["engine"]} / {info["quantization"]}' if info else "unreported"


def group_key(data):
    return tuple(data[k] for k in ("model", "adapter", "harness_commit")) + (_inference(data),) + (
        data["phone"]["model"], data["phone"]["ios_version"],
        data["board"], data["frame_source"], data["locale"],
    )


def _cell(value):
    return str(value).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("|", "&#124;")


def _total(values):
    known = [v for v in values if v is not None]
    return f"{sum(known):g} ({len(known)}/{len(values)} known)" if known else "unknown"


def render_leaderboard(submissions):
    groups = defaultdict(list)
    for data in submissions:
        validate_submission(data)
        groups[group_key(data)].extend(data["runs"])
    lines = ["# Benchy submissions", "", "Read: contributor-reported harness results; not independently verified.",
             "Groups keep model, adapter, harness commit, phone/iOS, board, frame source and locale separate.",
             "Totals include known values only; parentheses show coverage. Usage/cost are adapter-reported.", ""]
    if not groups:
        return "\n".join(lines + ["No submissions yet.", ""])
    lines += ["| Model / adapter | Engine / quant | Harness commit | Phone / iOS | Board / frames / locale | Runs | PASS | Honest | False success | Dishonest | Unclaimed | C1/C2/C3/C4 | Actions total | Seconds total | Input tokens | Output tokens | USD total |",
              "|---|---|---|---|---|---:|---:|---:|---:|---:|---:|---|---|---|---|---|---|"]
    ordered = sorted(groups.items(), key=lambda item: (-sum(r["verdict"] == "PASS" for r in item[1]) / len(item[1]), item[0]))
    for key, runs in ordered:
        model, adapter, commit, engine, phone, ios, board, frames, locale = key
        counts = [sum(r["honesty"] == h for r in runs) for h in ("honest", "false_success", "dishonest", "unclaimed")]
        row = [f"{model} / {adapter}", engine, commit, f"{phone} / {ios}", f"{board} / {frames} / {locale}", len(runs),
               sum(r["verdict"] == "PASS" for r in runs), *counts,
               "/".join(str(sum(r["checkpoints"][f"C{i}"] for r in runs)) for i in range(1, 5)),
               _total([r["actions"] for r in runs]), _total([r["wall_time_s"] for r in runs])]
        row += [_total([r["usage"][k] for r in runs]) for k in ("input_tokens", "output_tokens", "cost_usd")]
        lines.append("| " + " | ".join(_cell(v) for v in row) + " |")
    return "\n".join(lines) + "\n"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--results-dir", type=Path, default=Path(__file__).with_name("results"))
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    try:
        markdown = render_leaderboard(load_submissions(args.results_dir))
    except SubmissionError as exc:
        parser.exit(2, f"Invalid submission: {exc}\n")
    if args.output:
        args.output.write_text(markdown, encoding="utf-8")
    else:
        print(markdown, end="")


if __name__ == "__main__":
    main()
