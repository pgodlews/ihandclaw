"""Claude Code headless adapter. Only invoked by an explicitly requested run."""
import json
import os
from pathlib import Path
import subprocess
import sys

from bench.daemon_client import REFERENCE_PT, DaemonClient

# One fixed executable so the allow-rule matches (a looser command form made Claude Code ask
# for Bash permission mid-run; measured 2026-10-12).
PHONE = str(Path(__file__).resolve().with_name("phone"))


def prompt(env, screen_pt=REFERENCE_PT, phone=PHONE, image_viewer="Use Read"):
    """`screen_pt` is the daemon's reported size (GET /api/state screen.pt), not an assumption."""
    w, h = screen_pt
    return f"""Operate the iPhone using only Bash commands of the exact form `{phone} <command> [args]`
(no cd, pipes, &&, env vars or other programs; nothing else is permitted).
Commands: state; elements; shot OUT.jpg; tap X Y; tap_el ID; tap_text TEXT [NTH];
type TEXT; key COMBO; launch APP; back; home; scroll DY. Quote text arguments.
Output is JSON with ok/error/detail. Check every action result and observe again after errors.
Typing returns a typed count, not proof of success. Literal \\n in type text becomes newline.
Coordinates are {w}x{h} POINTS. shot is pt2, {2 * w}x{2 * h} PIXELS: divide pixel coordinates by 2.
{image_viewer} to view shots, saved inside {env['BENCH_RUN_DIR']}.
While a text field has editing focus taps fail; press key esc first.
Notes reopens the last note: create a NEW note and verify its unique title.
Safety: no deleting or replacing old content, no passwords or credentials, no messages,
no calls, no purchases, no account/security changes. Do not clear existing notes.
Judge your success using the final screen, never a failed action or intention.
Finish with the requested BENCH_RESULT JSON on one line, without markdown fences.
Task: {env['BENCH_GOAL']}"""


def command(env, screen_pt=REFERENCE_PT):
    return ["claude", "-p", prompt(env, screen_pt), "--model", env["BENCH_MODEL"],
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


def screen_size(env, client=None):
    """Ask the daemon for the screen size; the reference only when it cannot be read."""
    try:
        client = client or DaemonClient(env["IPHONE_HAND_DAEMON_URL"], env.get("IPHONE_HAND_TOKEN"))
        return client.screen_pt()
    except Exception as exc:
        # Raw errors can contain the daemon URL or token; name the class only.
        print(f"screen size unavailable ({type(exc).__name__}); prompting with "
              f"{REFERENCE_PT[0]}x{REFERENCE_PT[1]}", file=sys.stderr)
        return REFERENCE_PT


def main():
    # Run inside the run dir so relative screenshots (shot OUT.jpg) stay out of the checkout.
    proc = subprocess.run(command(os.environ, screen_size(os.environ)), capture_output=True, text=True,
                          cwd=os.environ["BENCH_RUN_DIR"])
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
