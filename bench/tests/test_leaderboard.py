from copy import deepcopy
import json

import pytest

from bench.leaderboard import SubmissionError, load_submissions, render_leaderboard, validate_submission


def submission():
    # Synthetic values for tests only; never a published benchmark result.
    return dict(schema_version=1, scenario="00_benchy", model="synthetic", adapter="fake",
                harness_commit="0" * 40, phone=dict(model="synthetic", ios_version="test"),
                board="fake", frame_source="fixture", locale="en_US", runs=[dict(
                    run_id="test", checkpoints=dict(C1=True, C2=True, C3=True, C4=True),
                    verdict="PASS", claimed_success=True, honesty="honest", execution_error=False,
                    actions=None, action_source="unavailable", wall_time_s=1,
                    usage=dict(input_tokens=None, output_tokens=None, cost_usd=None, source="unavailable"))])


@pytest.mark.parametrize("changes", [
    {"verdict": "FAIL"}, {"honesty": "false_success"}, {"actions": True},
    {"wall_time_s": float("nan")}, {"claimed_success": "true"},
    {"claimed_success": None, "honesty": "unclaimed"},
    {"checkpoints": dict(C1=True, C2=True, C3=True)},
    {"usage": dict(input_tokens=-1, output_tokens=None, cost_usd=None, source="test")},
])
def test_rejects_inconsistent_or_invalid_run(changes):
    data = submission()
    data["runs"][0].update(changes)
    with pytest.raises(SubmissionError):
        validate_submission(data)


def test_honesty_and_unknowns():
    data = submission()
    run = data["runs"][0]
    run.update(checkpoints=dict(C1=False, C2=True, C3=True, C4=True), verdict="FAIL", honesty="false_success")
    validate_submission(data)
    run.update(claimed_success=None, honesty="unclaimed")
    run["checkpoints"]["C4"] = False
    validate_submission(data)
    text = render_leaderboard([data])
    assert "unknown" in text and "1 (1/1 known)" in text


def test_groups_and_markdown_escaping():
    data = submission()
    other = deepcopy(data)
    other["phone"]["ios_version"] = "test2"
    data["model"] = "a|<b>"
    text = render_leaderboard([data, other])
    assert "a&#124;&lt;b&gt;" in text
    assert "test2" in text
    assert text.count("| fake / fixture / en_US |") == 2


def test_loading_duplicate_and_malformed_submissions(tmp_path):
    data = submission()
    (tmp_path / "one.json").write_text(json.dumps(data))
    assert load_submissions(tmp_path) == [data]
    (tmp_path / "two.json").write_text(json.dumps(data))
    with pytest.raises(SubmissionError, match="duplicate run"):
        load_submissions(tmp_path)
    (tmp_path / "two.json").write_text("[]")
    with pytest.raises(SubmissionError, match="two.json"):
        load_submissions(tmp_path)


def test_empty_directory(tmp_path):
    assert "No submissions yet" in render_leaderboard(load_submissions(tmp_path))


def test_inference_engine_is_optional_validated_and_never_merged():
    plain, lms, srv = submission(), submission(), submission()
    lms["inference"] = dict(engine="lmstudio-mlx", quantization="4bit")
    srv["inference"] = dict(engine="llama-server", quantization="Q4_K_M")
    for data in (plain, lms, srv):
        validate_submission(data)
    table = render_leaderboard([plain, lms, srv])
    assert "lmstudio-mlx / 4bit" in table and "llama-server / Q4_K_M" in table and "unreported" in table
    assert table.count("| synthetic / fake |") == 3
    bad = submission()
    bad["inference"] = dict(engine="lmstudio-mlx")
    with pytest.raises(SubmissionError):
        validate_submission(bad)
