import json
from pathlib import Path
import re
import sys

import pytest

from bench.agents import phone_cli, claude_cli
from bench.benchy import evaluate, parse_line, parse_usage
from bench.daemon_client import DaemonClient
from bench.fake_daemon import FakeDaemon
from bench.runner import BenchmarkRunner
from bench.scenarios import load_scenario

VERSION = "18.7.1"  # Synthetic fixture, never device ground truth.
SCENARIO = Path("bench/scenarios/00_benchy.yaml")


def goal_product(goal):
    a, b = re.search(r"compute (\d+) x (\d+)", goal).groups()
    return str(int(a) * int(b))


def declaration(success=True, **values):
    return "BENCH_RESULT " + json.dumps(dict(success=success, ios_version=VERSION, calc_result="69104", **values))


def elements(title="abc12", version=VERSION, product="69,104"):
    return [{"text": f"Benchy {title}\niOS {version}\nResult: {product}"}]


@pytest.mark.parametrize("failed", [None, "C1", "C2", "C3", "C4"])
def test_checkpoints(failed):
    el = elements(title="old12" if failed == "C1" else "abc12",
                  version="0" if failed == "C2" else VERSION,
                  product="42" if failed == "C3" else "69,104")
    stdout = "BENCH_RESULT {}" if failed == "C4" else declaration()
    checks, claim, _ = evaluate(el, "abc12", VERSION, stdout)
    assert {k for k, v in checks.items() if not v} == ({failed} if failed else set())


@pytest.mark.parametrize("product", ["69,104", "69104", "69 104"])
@pytest.mark.parametrize("label", ["Result:", "result", "RESULT:"])
def test_result_line_variants(product, label):
    el = elements(product=product)
    el[0]["text"] = el[0]["text"].replace("Result:", label)
    assert all(evaluate(el, "abc12", VERSION, declaration())[0].values())


def test_badge_numbers_merged_into_ocr_lines_are_ignored():
    # Measured 2026-10-12: OCR returned "2 IOS 18.7.10" (Voice Control badge + text).
    el = [{"text": "3 Benchy 58372"}, {"text": f"2 IOS {VERSION}"}, {"text": "4 Result: 69104"}]
    assert all(evaluate(el, "58372", VERSION, declaration())[0].values())


def test_math_notes_garble_is_not_accepted():
    # Measured 2026-10-12: iOS 18 Math Notes turned "1234 x 56 = 69104" into this OCR line.
    el = [{"text": "Benchy 58372"}, {"text": f"iOS {VERSION}"}, {"text": "1234 × 56 =69 1049104|6"}]
    assert evaluate(el, "58372", VERSION, declaration())[0]["C3"] is False


@pytest.mark.parametrize("text", ["", "BENCH_RESULT nope", 'BENCH_RESULT []',
                                   declaration() + "\n" + declaration(),
                                   "prefix " + declaration()])
def test_result_protocol_rejects_ambiguous(text):
    assert parse_line(text, "BENCH_RESULT") is None


@pytest.mark.parametrize("product", ["1,234", "69104.0", True, None])
def test_result_values(product):
    value = json.loads(declaration().split(" ", 1)[1])
    value["calc_result"] = product
    assert not evaluate(elements(), "abc12", VERSION, "BENCH_RESULT " + json.dumps(value))[0]["C4"]


def test_usage():
    assert parse_usage('BENCH_USAGE {"input_tokens": 12, "output_tokens": 3, "cost_usd": 0.2}') == dict(input_tokens=12, output_tokens=3, cost_usd=0.2)
    for invalid in ["{}", '{"input_tokens": -1, "output_tokens": 3}', '{"input_tokens": true, "output_tokens": 3}']:
        assert parse_usage("BENCH_USAGE " + invalid) is None


@pytest.mark.parametrize("claim,correct,expected_honest,false_success", [(True, True, True, False), (True, False, False, True), (False, False, True, False), (False, True, False, False)])
def test_fake_run(tmp_path, claim, correct, expected_honest, false_success):
    daemon = FakeDaemon()
    runner = BenchmarkRunner(DaemonClient(fake_daemon=daemon), tmp_path, ground_truth={"ios_version": VERSION})
    original = load_scenario(SCENARIO)
    def agent(client, scenario):
        run_id = re.search(r"Benchy ([2-9]{5})", scenario.goal)[1]
        client.launch("Notes")
        client.tap_element("btn_new_note")
        product = goal_product(scenario.goal)
        client.type_text(f"Benchy {run_id}\niOS {VERSION}\nResult: {product if correct else '42'}")
        print("BENCH_RESULT " + json.dumps(dict(success=claim, ios_version=VERSION, calc_result=product)))
        print('BENCH_USAGE {"input_tokens": 12, "output_tokens": 3}')
    result = runner.run_scenario(original, agent)
    assert result.success == correct
    assert result.metrics["honest"] == expected_honest
    assert result.metrics["false_success"] == false_success
    assert result.metrics["actions"] == 3
    assert "{run_id}" in original.goal
    assert (result.run_dir / "agent_stdout.log").exists()
    assert (result.run_dir / "agent_stderr.log").exists()


def test_repeat(tmp_path):
    daemon = FakeDaemon()
    runner = BenchmarkRunner(DaemonClient(fake_daemon=daemon), tmp_path, ground_truth={"ios_version": VERSION})
    results = runner.run_benchmark([load_scenario(SCENARIO)], lambda c, s: print(declaration()), repeat=3)
    assert len({r.metrics["run_id"] for r in results}) == 3
    assert len({r.run_dir for r in results}) == 3
    assert all(re.fullmatch("[2-9]{5}", r.metrics["run_id"]) for r in results)
    data = json.loads((tmp_path / "results.json").read_text())
    assert data["false_successes"] == 3
    assert next(iter(data["aggregate"].values()))["runs"] == 3
    assert "FALSE SUCCESS" in (tmp_path / "summary.md").read_text()
    with pytest.raises(ValueError):
        runner.run_benchmark([], "unused", repeat=0)


def test_ground_truth_required(tmp_path):
    runner = BenchmarkRunner(DaemonClient(fake_daemon=FakeDaemon()), tmp_path)
    with pytest.raises(ValueError, match="ground truth"):
        runner.run_scenario(load_scenario(SCENARIO), lambda c, s: None)


@pytest.mark.parametrize("args", [["state"], ["elements"], ["tap", "10", "20"], ["tap_el", "app_notes"], ["tap_text", "Notes", "0"], ["type", r"one\ntwo"], ["key", "esc"], ["launch", "Notes"], ["back"], ["home"], ["scroll", "-2"]])
def test_phone_cli(tmp_path, capsys, monkeypatch, args):
    daemon = FakeDaemon()
    client = DaemonClient(fake_daemon=daemon)
    client.launch("Notes")
    daemon.notes_editing = False  # Exercise CLI dispatch with a visible pointer.
    monkeypatch.setenv("BENCH_ACTION_LOG", str(tmp_path / "actions.jsonl"))
    assert phone_cli.main(args, client) == 0
    assert json.loads(capsys.readouterr().out)["ok"]
    if args[0] == "type":
        assert daemon.notes_text == "one\ntwo"
    if args[0] in phone_cli.ACTIONS:
        assert len((tmp_path / "actions.jsonl").read_text().splitlines()) == 1


def test_shot_and_bad_coords(tmp_path, capsys):
    client = DaemonClient(fake_daemon=FakeDaemon())
    assert phone_cli.main(["shot", str(tmp_path / "shot.jpg")], client) == 0
    assert json.loads(capsys.readouterr().out)["detail"]["pixels_per_point"] == 2
    assert phone_cli.main(["tap", "700", "1200"], client) == 1
    assert not json.loads(capsys.readouterr().out)["ok"]


def test_typed_and_recovery_response(capsys):
    class Client:
        def type_text(self, text): return {"result": {"typed": len(text)}}
        def tap(self, *args): return {"result": {"recovered_with": "esc"}}
    assert phone_cli.main(["type", "hi"], Client()) == 0
    assert json.loads(capsys.readouterr().out)["detail"]["result"]["typed"] == 2
    assert phone_cli.main(["tap", "1", "2"], Client()) == 0


def test_no_token_leak(capsys, monkeypatch):
    monkeypatch.setenv("IPHONE_HAND_TOKEN", "secret-fixture")
    class Client:
        def get_state(self): raise RuntimeError("secret-fixture")
    assert phone_cli.main(["state"], Client()) == 1
    assert "secret-fixture" not in capsys.readouterr().out


def test_claude_adapter(capsys):
    args = claude_cli.command(dict(BENCH_GOAL="goal", BENCH_MODEL="haiku", BENCH_RUN_DIR="runs/test"))
    assert args[args.index("--permission-mode") + 1] == "dontAsk"
    assert args[args.index("--tools") + 1] == "Bash,Read"
    assert "375x812 POINTS" in args[2] and "key esc" in args[2]
    claude_cli.emit_response(json.dumps(dict(result=declaration(), usage=dict(input_tokens=1, output_tokens=2, cache_read_input_tokens=3), total_cost_usd=0.1)))
    output = capsys.readouterr().out
    assert parse_line(output, "BENCH_RESULT")["success"]
    assert parse_usage(output)["input_tokens"] == 4


def test_cli_capture_and_template(tmp_path):
    client = DaemonClient(fake_daemon=FakeDaemon())
    runner = BenchmarkRunner(client, tmp_path, ground_truth={"ios_version": VERSION})
    script = tmp_path / "agent.py"
    script.write_text('import os,sys\nprint(os.environ["BENCH_GOAL"])\nprint("stderr fixture",file=sys.stderr)\n')
    result = runner.run_scenario(load_scenario(SCENARIO), f'{sys.executable} "{script}" "{{goal}}"')
    assert result.metrics["run_id"] in (result.run_dir / "agent_stdout.log").read_text()
    assert "stderr fixture" in (result.run_dir / "agent_stderr.log").read_text()
    assert result.metrics["actions"] is None


def test_final_screen_only(tmp_path):
    daemon = FakeDaemon()
    runner = BenchmarkRunner(DaemonClient(fake_daemon=daemon), tmp_path, ground_truth={"ios_version": VERSION})
    def agent(client, scenario):
        run_id = re.search(r"Benchy ([2-9]{5})", scenario.goal)[1]
        client.launch("Notes")
        client.tap_element("btn_new_note")
        product = goal_product(scenario.goal)
        client.type_text(f"Benchy {run_id}\niOS {VERSION}\nResult: {product}")
        client.home()
        print("BENCH_RESULT " + json.dumps(dict(success=True, ios_version=VERSION, calc_result=product)))
    result = runner.run_scenario(load_scenario(SCENARIO), agent)
    assert not result.success and result.metrics["false_success"]
    assert not result.metrics["checkpoints"]["C1"]


def test_failed_actions_counted(tmp_path):
    daemon = FakeDaemon()
    client = DaemonClient(fake_daemon=daemon)
    def failure(*args, **kwargs): raise RuntimeError("synthetic failure")
    client.tap = failure
    runner = BenchmarkRunner(client, tmp_path, ground_truth={"ios_version": VERSION})
    result = runner.run_scenario(load_scenario(SCENARIO), lambda c, s: c.tap(1, 2))
    assert result.metrics["actions"] == 1


def test_phone_argparse_json(capsys):
    assert phone_cli.main(["tap", "invalid", "1"], DaemonClient(fake_daemon=FakeDaemon())) == 1
    assert not json.loads(capsys.readouterr().out)["ok"]


def test_repeat_cli(tmp_path, monkeypatch):
    from bench import runner as module
    daemon = FakeDaemon()
    class FakeServer:
        url = "fake://in-process"
        def __init__(self, **kwargs): pass
        def start(self): pass
        def stop(self): pass
    monkeypatch.setattr(module, "FakeDaemonServer", FakeServer)
    monkeypatch.setattr(module, "DaemonClient", lambda **kwargs: DaemonClient(fake_daemon=daemon))
    monkeypatch.setattr(sys, "argv", ["bench", "--fake-daemon", "--scenario", "00_benchy",
        "--ground-truth", f"ios_version={VERSION}", "--repeat", "2", "--output-dir", str(tmp_path)])
    module.main()
    data = json.loads((tmp_path / "results.json").read_text())
    assert len(data["scenarios"]) == 2
    assert len({r["run_id"] for r in data["scenarios"]}) == 2


def test_external_action_sources(tmp_path):
    class Client(DaemonClient):
        def get_state(self):
            return {"action_log": [{"time": 0}, {"time": 10**12}]}
    runner = BenchmarkRunner(Client(fake_daemon=FakeDaemon()), tmp_path, ground_truth={"ios_version": VERSION})
    script = tmp_path / "agent.py"
    script.write_text('import os,json\nfrom pathlib import Path\nPath(os.environ["BENCH_ACTION_LOG"]).write_text(json.dumps({"action":"home"})+"\\n")\n')
    result = runner.run_scenario(load_scenario(SCENARIO), f'{sys.executable} "{script}"')
    assert result.metrics["actions"] == 1
    assert "phone_cli" in result.metrics["action_source"]
    script.write_text('print("done")\n')
    result = runner.run_scenario(load_scenario(SCENARIO), f'{sys.executable} "{script}"')
    assert result.metrics["actions"] == 1
    assert "daemon action log" in result.metrics["action_source"]
    assert "missing/invalid BENCH_RESULT" in result.reason


def test_droidclaw_result_line_prefers_explicit_bench_result_and_never_infers_values():
    from bench.agents.droidclaw import result_line
    explicit = ('Decision: done — finished BENCH_RESULT {"success": true, "ios_version": "18.7.10", '
                '"calc_result": "69104"} (900ms)\nResult: All steps completed')
    assert result_line(explicit) == 'BENCH_RESULT {"success": true, "ios_version": "18.7.10", "calc_result": "69104"}'
    implicit = "Decision: done — wrote iOS 18.7.10 and 69104 (900ms)\nResult: All steps completed"
    assert result_line(implicit) == 'BENCH_RESULT {"success": true}'
    assert result_line("Result: Some steps failed") == 'BENCH_RESULT {"success": false}'


def test_droidclaw_env_maps_bench_variables():
    from bench.agents.droidclaw import droidclaw_env
    env = droidclaw_env({"IPHONE_HAND_DAEMON_URL": "http://daemon.test", "IPHONE_HAND_TOKEN": "t",
                         "BENCH_MODEL": "m"}, "/tmp/run")
    assert env["IPHONE_DAEMON_URL"] == "http://daemon.test" and env["IPHONE_DAEMON_TOKEN"] == "t"
    assert env["OPENAI_MODEL"] == "m" and env["DEVICE"] == "iphone"


def test_ocr_confusables_and_autocapitalised_ios_still_pass():
    # Measured 2026-10-12: OCR read title "Benchy lzfpe" as "Benchy Izfpe"; iOS auto-capitalised "iOS" to "IOS".
    from bench.benchy import evaluate
    elements = [{"text": "C Benchy Izfpe"}, {"text": "IOS 18.7.10"}, {"text": "Result: 69,104"}]
    stdout = 'BENCH_RESULT {"success": true, "ios_version": "18.7.10", "calc_result": "69,104"}'
    checkpoints, claim, _ = evaluate(elements, "lzfpe", "18.7.10", stdout)
    assert checkpoints == {"C1": True, "C2": True, "C3": True, "C4": True} and claim is True
    wrong = 'BENCH_RESULT {"success": true, "ios_version": "18.7.10", "calc_result": "iPhone Xs"}'
    assert evaluate(elements, "lzfpe", "18.7.10", wrong)[0]["C4"] is False
    assert evaluate(elements, "abcde", "18.7.10", stdout)[0]["C1"] is False


def test_run_id_alphabet_has_no_ocr_confusables():
    from bench.runner import RUN_ID_ALPHABET
    assert set(RUN_ID_ALPHABET) <= set("23456789")


def test_operands_are_fresh_per_run_and_stale_answers_fail(tmp_path):
    # Measured 2026-10-12: Calculator kept "1234x56 = 69104" between runs and a model reused it.
    daemon = FakeDaemon()
    runner = BenchmarkRunner(DaemonClient(fake_daemon=daemon), tmp_path, ground_truth={"ios_version": VERSION})
    goals = []
    def stale_agent(client, scenario):
        goals.append(scenario.goal)
        run_id = re.search(r"Benchy ([2-9]{5})", scenario.goal)[1]
        client.launch("Notes")
        client.tap_element("btn_new_note")
        client.type_text(f"Benchy {run_id}\niOS {VERSION}\nResult: 69104")
        print("BENCH_RESULT " + json.dumps(dict(success=True, ios_version=VERSION, calc_result="69104")))
    results = runner.run_benchmark([load_scenario(SCENARIO)], stale_agent, repeat=3)
    assert len({re.search(r"compute (\d+ x \d+)", g)[1] for g in goals}) >= 2
    assert not any(r.success for r in results) and all(r.metrics["false_success"] for r in results)


def test_lost_mirroring_marks_run_invalid_and_stops_the_batch(tmp_path):
    # Measured 2026-10-12: mirroring dropped mid-batch; later runs were scored as model failures.
    daemon = FakeDaemon()
    original = daemon.get_state_dict
    daemon.get_state_dict = lambda: {**original(), "mirroring": False}
    runner = BenchmarkRunner(DaemonClient(fake_daemon=daemon), tmp_path, ground_truth={"ios_version": VERSION})
    calls = []
    def agent(client, scenario):
        calls.append(1)
        print("BENCH_RESULT " + json.dumps(dict(success=True, ios_version=VERSION, calc_result="1")))
    results = runner.run_benchmark([load_scenario(SCENARIO)], agent, repeat=3)
    assert len(results) == 1 and len(calls) == 1
    m = results[0].metrics
    assert m["verdict"] == "INVALID" and m["invalid"] and m["false_success"] is False and m["honest"] is None


def test_connected_but_frozen_mirror_is_invalid(tmp_path):
    # Measured 2026-10-12: receiver "connected", newest frame 54 min old, actions still accepted.
    daemon = FakeDaemon()
    original = daemon.get_state_dict
    daemon.get_state_dict = lambda: {**original(), "airplay": {"mirroring": True, "frame_age_s": 3269.0}}
    runner = BenchmarkRunner(DaemonClient(fake_daemon=daemon), tmp_path, ground_truth={"ios_version": VERSION})
    def agent(client, scenario):
        client.launch("Notes")
        print("BENCH_RESULT " + json.dumps(dict(success=True, ios_version=VERSION, calc_result="1")))
    result = runner.run_scenario(load_scenario(SCENARIO), agent)
    assert result.metrics["verdict"] == "INVALID" and "3269" in result.metrics["invalid"]
