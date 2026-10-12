"""Claude Code headless adapter. Only invoked by an explicitly requested run."""
import json
import os
from pathlib import Path
import subprocess
import sys

# One fixed executable so the allow-rule matches (a looser command form made Claude Code ask
# for Bash permission mid-run; measured 2026-10-12).
PHONE = str(Path(__file__).resolve().with_name("phone"))


def command(env):
    prompt = f"""Operate the iPhone using only Bash commands of the exact form `{PHONE} <command> [args]`
(no cd, pipes, &&, env vars or other programs; nothing else is permitted).
Commands: state; elements; shot OUT.jpg; tap X Y; tap_el ID; tap_text TEXT [NTH];
type TEXT; key COMBO; launch APP; back; home; scroll DY. Quote text arguments.
Output is JSON with ok/error/detail. Check every action result and observe again after errors.
Typing returns a typed count, not proof of success. Literal \\n in type text becomes newline.
Coordinates are 375x812 POINTS. shot is pt2, 750x1624 PIXELS: divide pixel coordinates by 2.
Use Read to view shots, saved inside {env['BENCH_RUN_DIR']}.
While a text field has editing focus taps fail; press key esc first.
Notes reopens the last note: create a NEW note and verify its unique title.
Safety: no deleting or replacing old content, no passwords or credentials, no messages,
no calls, no purchases, no account/security changes. Do not clear existing notes.
Judge your success using the final screen, never a failed action or intention.
Finish with the requested BENCH_RESULT JSON on one line, without markdown fences.
Task: {env['BENCH_GOAL']}"""
    return ["claude", "-p", prompt, "--model", env["BENCH_MODEL"],
            "--safe-mode", "--tools", "Bash,Read", "--allowedTools", f"Bash({PHONE} *)", "Read",
            "--permission-mode", "dontAsk", "--output-format", "json",
            "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
            "--disable-slash-commands", "--no-session-persistence", "--setting-sources", ""]


def emit_response(raw):
    data = json.loads(raw)
    print(data.get("result", ""))
    usage = data.get("usage", {})
    if type(usage.get("input_tokens")) is int and type(usage.get("output_tokens")) is int:
        # Include cache tokens: otherwise cached runs look artificially cheap.
        usage = {"input_tokens": usage["input_tokens"] + usage.get("cache_read_input_tokens", 0)
                 + usage.get("cache_creation_input_tokens", 0), "output_tokens": usage["output_tokens"]}
        if "total_cost_usd" in data:
            usage["cost_usd"] = data["total_cost_usd"]
        print("BENCH_USAGE " + json.dumps(usage))
    return bool(data.get("is_error"))


def main():
    # Run inside the run dir so relative screenshots (shot OUT.jpg) stay out of the checkout.
    proc = subprocess.run(command(os.environ), capture_output=True, text=True, cwd=os.environ["BENCH_RUN_DIR"])
    Path(os.environ["BENCH_RUN_DIR"], "claude_response.json").write_text(proc.stdout)
    sys.stderr.write(proc.stderr)
    try:
        error = emit_response(proc.stdout)
    except (ValueError, TypeError):
        print("Claude JSON response unavailable", file=sys.stderr)
        return 1
    return proc.returncode or int(error)


if __name__ == "__main__":
    sys.exit(main())
