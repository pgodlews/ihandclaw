"""Run a droidclaw checkout (iPhone backend) as the Benchy agent.

The goal runs as a one-step droidclaw workflow. Bench env vars are mapped to droidclaw's
(IPHONE_DAEMON_URL/TOKEN, LM Studio settings from BENCH_MODEL). droidclaw has no stdout
channel for the model, so the model is asked to put the BENCH_RESULT line in its done
reason; that line is re-printed verbatim. Without it, only the workflow verdict is
reported as {"success": ...} with no values, so C4 fails by design (no inferred values).
"""
import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys

SUFFIX = (" When you finish, call done and put the exact BENCH_RESULT line (with its JSON) "
          "inside the done reason. If you could not complete the task, use done with success false.")


def droidclaw_env(env, run_dir):
    out = dict(env)
    out.update({
        "DEVICE": "iphone",
        "IPHONE_DAEMON_URL": env["IPHONE_HAND_DAEMON_URL"],
        "IPHONE_DAEMON_TOKEN": env.get("IPHONE_HAND_TOKEN", ""),
        "LLM_PROVIDER": "openai",
        "OPENAI_BASE_URL": env.get("OPENAI_BASE_URL", "http://localhost:1234/v1"),
        "OPENAI_API_KEY": env.get("OPENAI_API_KEY", "lm-studio"),
        "OPENAI_MODEL": env["BENCH_MODEL"],
        "JSON_SCHEMA_ENABLED": env.get("JSON_SCHEMA_ENABLED", "true"),
        "VISION_MODE": env.get("VISION_MODE", "always"),
        "LOG_DIR": str(Path(run_dir, "droidclaw-logs")),
    })
    return out


def result_line(output):
    """BENCH_RESULT from the last done reason, else the workflow verdict only."""
    for line in reversed(output.splitlines()):
        if line.startswith("Decision: done"):
            match = re.search(r"BENCH_RESULT\s*(\{.*?\})", line)
            if match:
                try:
                    return "BENCH_RESULT " + json.dumps(json.loads(match[1]))
                except ValueError:
                    pass
            break
    ok = "Result: All steps completed" in output
    return "BENCH_RESULT " + json.dumps({"success": ok})


def main():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--checkout", required=True, help="droidclaw checkout with the iPhone backend")
    p.add_argument("--max-steps", type=int, default=40)
    args = p.parse_args()
    run_dir = os.environ["BENCH_RUN_DIR"]
    workflow = Path(run_dir, "droidclaw-workflow.json")
    workflow.write_text(json.dumps({"name": "Benchy", "steps": [
        {"goal": os.environ["BENCH_GOAL"] + SUFFIX, "maxSteps": args.max_steps}]}))
    # Stream to disk so a run stopped by the time budget still leaves its log (measured 2026-10-12:
    # 10-minute kills left no droidclaw log when it was written only at exit).
    log = Path(run_dir, "droidclaw-stdout.log")
    with log.open("w") as sink:
        proc = subprocess.Popen(["bun", "run", "src/kernel.ts", "--device", "iphone", "--workflow", str(workflow)],
                                cwd=Path(args.checkout), env=droidclaw_env(os.environ, run_dir),
                                stdout=subprocess.PIPE, stderr=sys.stderr, text=True)
        for line in proc.stdout:
            sink.write(line)
            sink.flush()
        proc.wait()
    print(result_line(log.read_text()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
