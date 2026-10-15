"""Codex exec adapter. Only invoked by an explicitly requested benchmark run."""
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile

from bench.agents.claude_cli import prompt, screen_size
from bench.daemon_client import REFERENCE_PT

PHONE = str(Path(__file__).resolve().with_name("phone_venv"))
CODEX = shutil.which("codex") or "/Applications/ChatGPT.app/Contents/Resources/codex-cli/bin/codex"


def command(env, screen_pt=REFERENCE_PT):
    run_dir = Path(env["BENCH_RUN_DIR"]).resolve()
    instructions = prompt(env, screen_pt, phone=PHONE,
                          image_viewer="Use your own image viewing tool")
    instructions += f"\nWrite all files only inside {run_dir}."
    return [CODEX, "exec", "--ignore-user-config", "--skip-git-repo-check", "--ephemeral",
            "-C", str(run_dir), "-m", env["BENCH_MODEL"], "--sandbox", "workspace-write",
            "-c", 'approval_policy="never"',
            "-c", "sandbox_workspace_write.network_access=true",
            "-c", "sandbox_workspace_write.writable_roots=[]",
            "-c", "sandbox_workspace_write.exclude_tmpdir_env_var=true",
            "-c", "sandbox_workspace_write.exclude_slash_tmp=true",
            "--json", "-o", str(run_dir / "codex_last_message.txt"), instructions]


def emit_usage(events):
    """Read turn totals. Codex input_tokens already includes cached_input_tokens."""
    totals = {"input_tokens": 0, "output_tokens": 0}
    found = False
    failed = False
    for line in events:
        event = json.loads(line)
        failed |= event.get("type") in {"turn.failed", "error"}
        if event.get("type") != "turn.completed":
            continue
        usage = event.get("usage", {})
        if all(type(usage.get(k)) is int and usage[k] >= 0 for k in totals):
            for key in totals:
                totals[key] += usage[key]
            found = True
    if found:
        print("BENCH_USAGE " + json.dumps(totals))
    return failed


def main():
    env = os.environ.copy()
    run_dir = Path(env["BENCH_RUN_DIR"]).resolve()
    auth_path = None

    def terminate(signum, frame):
        # The runner sends SIGTERM to the whole process group on a budget timeout.
        raise SystemExit(128 + signum)

    previous_handler = signal.signal(signal.SIGTERM, terminate)
    try:
        # Keep the credential out of the agent's workspace. mkstemp creates mode 0600.
        auth_dir = Path(tempfile.gettempdir()).resolve()
        if auth_dir.is_relative_to(run_dir):
            auth_dir = run_dir.parent
        fd, name = tempfile.mkstemp(prefix="iphone-hand-auth-", dir=auth_dir)
        auth_path = Path(name)
        with os.fdopen(fd, "w") as auth:
            os.fchmod(auth.fileno(), 0o600)
            auth.write(env.pop("IPHONE_HAND_TOKEN", ""))
        env["IPHONE_HAND_AUTH_FILE"] = str(auth_path)
        # Query size with the original adapter environment, before launching the model.
        args = command(env, screen_size(os.environ))
        last = run_dir / "codex_last_message.txt"
        last.unlink(missing_ok=True)
        with (run_dir / "codex_events.jsonl").open("w") as events:
            proc = subprocess.run(args, cwd=run_dir, env=env, stdout=events,
                                  stderr=subprocess.PIPE, text=True)
        # Raw CLI errors/events can contain auth or URLs; never forward diagnostics.
        if last.exists():
            final = last.read_text()
            for value in (os.environ.get("IPHONE_HAND_TOKEN"), env.get("IPHONE_HAND_DAEMON_URL")):
                if value:
                    final = final.replace(value, "[redacted]").replace(json.dumps(value)[1:-1], "[redacted]")
            print(final)
        else:
            print("Codex final response unavailable", file=sys.stderr)
        with (run_dir / "codex_events.jsonl").open() as events:
            failed = emit_usage(events)
        if proc.returncode:
            print("Codex execution failed; inspect run artifacts", file=sys.stderr)
        return proc.returncode or int(failed or not last.exists())
    except Exception as exc:
        # No exception text: it can include the daemon URL or credentials.
        print(f"Codex adapter failed ({type(exc).__name__})", file=sys.stderr)
        return 1
    finally:
        try:
            if auth_path is not None:
                auth_path.unlink(missing_ok=True)
        finally:
            signal.signal(signal.SIGTERM, previous_handler)


if __name__ == "__main__":
    sys.exit(main())
