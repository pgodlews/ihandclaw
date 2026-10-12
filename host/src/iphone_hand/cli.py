"""Manual CLI; --fake uses no devices. Import and --help are also inert."""

import argparse
import asyncio
from dataclasses import asdict
import json
import os
import sys

from PIL import Image

from .fake import FakePhone
from .phone import Phone
from .screen import Screen
from .serial_link import SerialLink


def connection_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--fake", action="store_true", help="use static FakePhone, no hardware")
    parser.add_argument("--fixture", help="image for --fake")
    parser.add_argument("--port", help="serial port (or IPHONE_HAND_PORT)")
    parser.add_argument("--adb-serial", help="ADB device selector (or IPHONE_HAND_ADB_SERIAL)")
    parser.add_argument("--capture", choices=["png", "raw"], default="png")
    parser.add_argument("--settle", type=float, default=0.350)
    parser.add_argument("--home-action", default="cmd+h", help="key combo or button3")
    parser.add_argument("--no-cache", action="store_true", help="do not load/save calibration")


def make_phone(args):
    if args.fixture and not args.fake:
        raise ValueError("--fixture requires --fake")
    if args.fake:
        if args.fixture:
            with Image.open(args.fixture) as image:
                return FakePhone(image)
        return FakePhone()
    return Phone(SerialLink(args.port), Screen(args.adb_serial, mode=args.capture),
                 settle=args.settle, persist_calibration=not args.no_cache,
                 home_action=args.home_action)


async def run_dev(args):
    from .devservices import DevServices

    # This path never constructs Phone, Screen, or SerialLink.
    async with DevServices(device_id=args.device, auto_mount=False, timeout=args.timeout) as dev:
        if args.dev_command == "mount":
            await dev.mount()
            return {"mounted": True}
        if args.dev_command == "apps":
            return [asdict(app) for app in await dev.list_apps()]
        await dev.mount()
        if args.dev_command == "items":
            return [asdict(element) for element in await dev.list_items()]
        bundle = await dev.resolve_bundle(args.app)
        return {"bundle_id": bundle, "pid": await dev.launch(bundle)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="iphone-hand")
    connection_options(parser)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ["status", "pointer", "calibrate"]:
        commands.add_parser(name)
    shot = commands.add_parser("shot")
    shot.add_argument("output")
    for name in ["move", "tap"]:
        p = commands.add_parser(name)
        p.add_argument("x", type=float)
        p.add_argument("y", type=float)
    typing = commands.add_parser("type")
    typing.add_argument("text")
    key = commands.add_parser("key")
    key.add_argument("combo")
    ota = commands.add_parser("ota", help="update board firmware over authenticated Wi-Fi")
    ota.add_argument("firmware", help="ESP32-C3/C5 application .bin; target must match PING chip; no merged flash images")
    ota.add_argument("--host", required=True, help="board DNS name or address")
    ota.add_argument("--port", type=int, default=7878, help="board TCP port (default: 7878)")
    ota.add_argument("--wait-confirm", action="store_true",
                     help="keep retrying the trial health checkpoint until confirmation or the 150s deadline")
    dev = commands.add_parser("dev", help="Apple developer services via an existing tunneld")
    dev.add_argument("--device", default=os.environ.get("IPHONE_HAND_DEVSERVICES_DEVICE"),
                     help="runtime device selector; required when multiple tunnels are present")
    dev.add_argument("--timeout", type=float, default=60.0, help="seconds per operation")
    dev_commands = dev.add_subparsers(dest="dev_command", required=True)
    for name in ("items", "apps", "mount"):
        dev_commands.add_parser(name)
    launch = dev_commands.add_parser("launch")
    launch.add_argument("app", help="bundle identifier or exact display name (quote spaces)")
    args = parser.parse_args(argv)
    phone = None
    try:
        if args.command == "ota":
            if args.fake:
                raise ValueError("--fake is unavailable for OTA; use the in-memory OTA tests")
            from .ota import update_file
            last_percent = -1
            def progress(sent, total):
                nonlocal last_percent
                percent = sent * 100 // total
                if percent != last_percent:
                    print(f"OTA {percent:3d}% ({sent}/{total} bytes)", file=sys.stderr)
                    last_percent = percent
            print(json.dumps(update_file(args.firmware, args.host, args.port, progress=progress,
                                         phase=lambda name: print(f"OTA phase: {name}", file=sys.stderr),
                                         wait_confirm=args.wait_confirm)))
            return 0
        if args.command == "dev":
            if args.fake:
                raise ValueError("--fake is for the pixel layer; dev requires an existing tunneld")
            print(json.dumps(asyncio.run(run_dev(args)), ensure_ascii=False))
            return 0
        phone = make_phone(args)
        if args.command == "shot":
            phone.screenshot().save(args.output, format="PNG")
            result = {"saved": args.output, "size": [750, 1624]}
        elif args.command == "pointer":
            position = phone.find_pointer()
            result = dict(zip(("x", "y", "confidence"), position)) if position is not None else None
        elif args.command in {"move", "tap"}:
            method = phone.move_to if args.command == "move" else phone.tap
            result = method(args.x, args.y)
            if result["error_pt"] > 3:
                print(json.dumps(result))
                return 2
        elif args.command == "type":
            result = phone.type_text(args.text)
        elif args.command == "key":
            result = phone.key(args.combo)
        else:
            result = getattr(phone, args.command)()
        print(json.dumps(result))
        return 0
    except (OSError, RuntimeError, ValueError, TimeoutError) as exc:
        if args.command == "ota" and hasattr(exc, "report"):
            print(json.dumps(exc.report))
        print(f"iphone-hand: {exc}", file=sys.stderr)
        return 1
    finally:
        if phone is not None:
            phone.close()


if __name__ == "__main__":
    raise SystemExit(main())
