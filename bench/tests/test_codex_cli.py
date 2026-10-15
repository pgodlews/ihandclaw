"""Read: local CLI help fixture; measured tests use fake subprocesses/daemon only."""
import json
import os
from pathlib import Path
import re
import signal
import stat
import subprocess
from types import SimpleNamespace

import pytest

from bench.agents import claude_cli, codex_cli
from bench.benchy import parse_line, parse_usage
from bench.fake_daemon import FakeDaemonServer


RESULT = 'BENCH_RESULT {"success": true, "ios_version": "synthetic", "calc_result": "42"}'
# Synthetic Codex JSONL, including cached input (a subset of input_tokens).
EVENTS = '\n'.join(json.dumps(event) for event in [
    {"type": "thread.started", "thread_id": "synthetic"},
    {"type": "turn.started"},
    {"type": "item.completed", "item": {"type": "agent_message", "text": RESULT}},
    {"type": "turn.completed", "usage": {"input_tokens": 120, "cached_input_tokens": 90, "output_tokens": 14}},
    {"type": "turn.completed", "usage": {"input_tokens": 30, "cached_input_tokens": 20, "output_tokens": 6}},
]) + '\n'


@pytest.fixture
def exec_help():
    # Captured from the user-specified installed `codex exec --help`, 2026-10-15.
    return Path(__file__).with_name("fixtures").joinpath("codex_exec_help.txt").read_text()


@pytest.fixture
def env(tmp_path):
    run = tmp_path / "run with spaces"
    run.mkdir()
    return dict(BENCH_RUN_DIR=str(run), BENCH_MODEL="gpt-6.1-sol", BENCH_GOAL="synthetic goal",
                IPHONE_HAND_DAEMON_URL="http://unused.invalid", IPHONE_HAND_TOKEN="synthetic-credential",
                BENCH_ACTION_LOG=str(run / "phone_actions.jsonl"))


def test_command_uses_only_installed_flags_and_run_dir(env, exec_help):
    args = codex_cli.command(env, (390, 844))
    assert args[:2] == [codex_cli.CODEX, "exec"]
    for flag in args[2:-1]:
        if flag.startswith("-"):
            assert re.search(r"(?<!\S)" + re.escape(flag) + r"(?=[\s,])", exec_help), flag
    for flag, value in [("-C", env["BENCH_RUN_DIR"]), ("-m", "gpt-6.1-sol"),
                        ("--sandbox", "workspace-write"),
                        ("-o", str(Path(env["BENCH_RUN_DIR"]) / "codex_last_message.txt"))]:
        assert args[args.index(flag) + 1] == value
    assert {"--json", "--ephemeral", "--skip-git-repo-check", "--ignore-user-config"} <= set(args)
    config = {args[i + 1] for i, arg in enumerate(args) if arg == "-c"}
    assert config == {'approval_policy="never"', 'sandbox_workspace_write.network_access=true',
                      'sandbox_workspace_write.writable_roots=[]',
                      'sandbox_workspace_write.exclude_tmpdir_env_var=true',
                      'sandbox_workspace_write.exclude_slash_tmp=true'}
    prompt = args[-1]
    shared = claude_cli.prompt(env, (390, 844), phone=codex_cli.PHONE,
                               image_viewer="Use your own image viewing tool")
    assert prompt.startswith(shared)
    assert "390x844 POINTS" in prompt and "780x1688 PIXELS" in prompt
    assert "Use Read" not in prompt and "phone_venv" in prompt
    assert f"Write all files only inside {env['BENCH_RUN_DIR']}" in prompt
    assert env["IPHONE_HAND_TOKEN"] not in str(args) and env["IPHONE_HAND_DAEMON_URL"] not in str(args)


def test_usage_jsonl_includes_cache_once_and_sums_turns(capsys):
    assert not codex_cli.emit_usage(EVENTS.splitlines())
    assert parse_usage(capsys.readouterr().out) == {"input_tokens": 150, "output_tokens": 20}


@pytest.mark.parametrize("usage", [{}, {"input_tokens": True, "output_tokens": 1},
                                   {"input_tokens": -1, "output_tokens": 2}])
def test_unknown_usage_is_not_invented(usage, capsys):
    codex_cli.emit_usage([json.dumps({"type": "turn.completed", "usage": usage})])
    assert "BENCH_USAGE" not in capsys.readouterr().out


@pytest.mark.parametrize("outcome", ["success", "nonzero", "exception", "invalid_json", "turn_failed",
                                     "missing_final", "sigterm"])
def test_auth_file_lifecycle_and_sanitized_errors(env, monkeypatch, capsys, outcome):
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(codex_cli, "screen_size", lambda _: (375, 812))
    auth_paths = []
    run = Path(env["BENCH_RUN_DIR"])
    # A failed invocation must not emit an old success.
    (run / "codex_last_message.txt").write_text("stale " + RESULT)
    previous_handler = signal.getsignal(signal.SIGTERM)

    def invoke(args, *, cwd, env: dict, stdout, stderr, text):
        auth = Path(env["IPHONE_HAND_AUTH_FILE"])
        auth_paths.append(auth)
        assert auth.exists() and not auth.resolve().is_relative_to(run)
        assert stat.S_IMODE(auth.stat().st_mode) == 0o600
        assert auth.read_text() == "synthetic-credential"
        assert "IPHONE_HAND_TOKEN" not in env
        assert cwd == run and stdout.name == str(run / "codex_events.jsonl")
        assert not (run / "codex_last_message.txt").exists()
        if outcome == "exception":
            raise RuntimeError("synthetic-credential http://unused.invalid")
        if outcome == "sigterm":
            signal.raise_signal(signal.SIGTERM)
        stdout.write("invalid" if outcome == "invalid_json" else EVENTS)
        if outcome == "turn_failed":
            stdout.write(json.dumps({"type": "turn.failed", "error": {"message": "synthetic-credential http://unused.invalid"}}) + '\n')
        if outcome not in {"missing_final", "nonzero"}:
            (run / "codex_last_message.txt").write_text(RESULT)
        return SimpleNamespace(returncode=7 if outcome == "nonzero" else 0,
                               stderr="synthetic-credential http://unused.invalid")

    monkeypatch.setattr(codex_cli.subprocess, "run", invoke)
    if outcome == "sigterm":
        with pytest.raises(SystemExit) as error:
            codex_cli.main()
        assert error.value.code == 128 + signal.SIGTERM
    else:
        code = codex_cli.main()
        assert code == (0 if outcome == "success" else 7 if outcome == "nonzero" else 1)
    assert len(auth_paths) == 1 and not auth_paths[0].exists()
    assert signal.getsignal(signal.SIGTERM) == previous_handler
    captured = capsys.readouterr()
    assert "synthetic-credential" not in captured.out + captured.err
    assert "http://unused.invalid" not in captured.out + captured.err
    assert "stale" not in captured.out
    if outcome == "success":
        assert parse_line(captured.out, "BENCH_RESULT")["success"] is True
        assert parse_usage(captured.out)["input_tokens"] == 150


def test_wrapper_imports_from_run_dir_without_uv(env):
    child_env = os.environ.copy()
    child_env.pop("IPHONE_HAND_AUTH_FILE", None)
    result = subprocess.run([codex_cli.PHONE, "--help"], cwd=env["BENCH_RUN_DIR"],
                            env=child_env, capture_output=True, text=True)
    assert result.returncode == 0 and "tap_text" in result.stdout


def test_wrapper_missing_auth_file_is_sanitized(env):
    child_env = dict(os.environ, IPHONE_HAND_AUTH_FILE=str(Path(env["BENCH_RUN_DIR"]) / "missing"))
    result = subprocess.run([codex_cli.PHONE, "state"], cwd=env["BENCH_RUN_DIR"],
                            env=child_env, capture_output=True, text=True)
    assert result.returncode == 1 and result.stderr == ""
    assert json.loads(result.stdout)["error"] == "AuthFileUnavailable"


def test_wrapper_authenticates_fake_daemon_after_token_is_filtered(env):
    try:
        server = FakeDaemonServer(required_token="synthetic-credential")
    except PermissionError:
        pytest.skip("Sandbox forbids loopback socket binding")
    server.start()
    auth = Path(env["BENCH_RUN_DIR"]).parent / "auth"
    auth.write_text("synthetic-credential")
    auth.chmod(0o600)
    try:
        child_env = dict(os.environ, IPHONE_HAND_AUTH_FILE=str(auth),
                         IPHONE_HAND_DAEMON_URL=server.url)
        child_env.pop("IPHONE_HAND_TOKEN", None)
        result = subprocess.run([codex_cli.PHONE, "state"], cwd=env["BENCH_RUN_DIR"],
                                env=child_env, capture_output=True, text=True)
        assert result.returncode == 0 and json.loads(result.stdout)["detail"]["current_app"] == "Home"
    finally:
        auth.unlink(missing_ok=True)
        server.stop()
