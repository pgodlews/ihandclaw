"""Provision the board's Wi-Fi and TCP token over USB serial.

Run it yourself in a terminal: it asks for the SSID and password with hidden input, so they never
appear in a shell history, a chat or a file. The TCP token is generated once and kept in
~/.config/iphone-hand/board-token (mode 600); the daemon reads the same file.

    uv run --project host python scripts/provision_wifi.py [--port /dev/cu.usbmodemXXXX]
"""
from __future__ import annotations

import argparse
import getpass
import os
import re
import secrets
import sys
import time
from pathlib import Path
from urllib.parse import quote

from iphone_hand.serial_link import SerialLink

TOKEN_FILE = Path.home() / ".config/iphone-hand/board-token"


def board_token() -> str:
    if TOKEN_FILE.exists():
        return TOKEN_FILE.read_text().strip()
    TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    token = secrets.token_hex(32)
    fd = os.open(TOKEN_FILE, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(token + "\n")
    return token


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port", help="serial port (default: the only /dev/cu.usbmodem*)")
    args = parser.parse_args()

    ssid = input("Wi-Fi network name (SSID): ").strip()
    password = getpass.getpass("Wi-Fi password (hidden): ")
    if not ssid or not password:
        print("SSID and password are required.", file=sys.stderr)
        return 2

    link = SerialLink(args.port)
    link.open()
    link.command("TOKEN", board_token())
    link.command("WIFI", quote(ssid, safe=""), quote(password, safe=""))
    print(f"Sent. Token file: {TOKEN_FILE}. Waiting for the board to join Wi-Fi...")
    for _ in range(30):
        time.sleep(1)
        text = link.command("STATUS").text
        match = re.search(r"wifi=1 ip=(\S+)", text)
        if match and match.group(1) != "-":
            print(f"Connected. Board IP: {match.group(1)} (TCP port 7878)")
            return 0
    print("Not connected after 30 s. Check the SSID/password and that the network is 2.4 or 5 GHz WPA2/WPA3.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
