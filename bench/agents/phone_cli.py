"""Small JSON CLI: use through the host uv project, from the repository root."""
import argparse
import json
import math
import os
from pathlib import Path
import sys
import time

from bench.daemon_client import DaemonClient

class JSONParserError(ValueError):
    pass


class JSONArgumentParser(argparse.ArgumentParser):
    def error(self, message):
        raise JSONParserError(message)


ACTIONS = {"tap", "tap_el", "tap_text", "type", "key", "launch", "back", "home", "scroll"}


def parser():
    p = JSONArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="command", required=True)
    for name in ("state", "elements", "back", "home"):
        sub.add_parser(name)
    sub.add_parser("shot").add_argument("out")
    tap = sub.add_parser("tap")
    tap.add_argument("x", type=float)
    tap.add_argument("y", type=float)
    sub.add_parser("tap_el").add_argument("id")
    tap_text = sub.add_parser("tap_text")
    tap_text.add_argument("text")
    tap_text.add_argument("nth", type=int, nargs="?", default=0)
    sub.add_parser("type").add_argument("text")
    sub.add_parser("key").add_argument("combo")
    sub.add_parser("launch").add_argument("app")
    sub.add_parser("scroll").add_argument("dy", type=int)
    return p


def execute(args, client):
    cmd = args.command
    if cmd == "state":
        return client.get_state()
    if cmd == "elements":
        return client.get_elements()
    if cmd == "shot":
        client.get_frame(scale="pt2").save(args.out, format="JPEG")
        return {"file": args.out, "pixels_per_point": 2}
    if cmd == "tap":
        if not (math.isfinite(args.x) and math.isfinite(args.y)
                and 0 <= args.x < 375 and 0 <= args.y < 812):
            raise ValueError("tap coordinates must be within 375x812 points")
        return client.tap(args.x, args.y)
    if cmd == "tap_el":
        return client.tap_element(args.id)
    if cmd == "tap_text":
        if args.nth < 0:
            raise ValueError("NTH must be nonnegative")
        return client.tap_text(args.text, args.nth)
    if cmd == "type":
        return client.type_text(args.text.replace("\\n", "\n"))
    if cmd == "key":
        return client.key(args.combo)
    if cmd == "launch":
        return client.launch(args.app)
    if cmd == "scroll":
        return client.scroll(args.dy)
    return getattr(client, cmd)()


def main(argv=None, client=None):
    # argparse errors also use the same JSON envelope.
    p = parser()
    args = None
    started = time.time()
    ok = False
    try:
        args = p.parse_args(argv)
        if client is None:
            url = os.environ.get("IPHONE_HAND_DAEMON_URL")
            if not url:
                raise ValueError("IPHONE_HAND_DAEMON_URL is required")
            client = DaemonClient(url, os.environ.get("IPHONE_HAND_TOKEN"))
        detail = execute(args, client)
        # A typed count or recovered_with is a valid action result, not a success claim.
        ok = not (isinstance(detail, dict) and (detail.get("ok") is False or detail.get("error")))
        response = {"ok": ok, "error": None if ok else "daemon_action_failed", "detail": detail}
    except Exception as exc:
        # Raw daemon errors can contain credentials, URLs or user content.
        response = {"ok": False, "error": type(exc).__name__, "detail": "Command failed; inspect state/elements before retrying."}
    finally:
        log = os.environ.get("BENCH_ACTION_LOG")
        if args and args.command in ACTIONS and log:
            with Path(log).open("a") as f:
                f.write(json.dumps({"action": args.command, "time": started, "ok": ok}) + "\n")
    output = json.dumps(response)
    token = os.environ.get("IPHONE_HAND_TOKEN") or (getattr(client, "token", None) if client else None)
    if token:
        output = output.replace(json.dumps(token)[1:-1], "[redacted]")
    print(output)
    return 0 if response["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
