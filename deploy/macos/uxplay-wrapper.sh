#!/bin/sh
# macOS receiver wrapper, installed as ~/.local/bin/uxplay next to the built uxplay.bin.
# macOS has no GNU stdbuf; run UxPlay under `script` so its stdout is a pty and libc
# line-buffers it. Read (deploy/README.md, measured 2026-10-10): unbuffered output without
# GST_DEBUG meant no frames, root cause unknown - keep the pty and GST_DEBUG=2 until understood.
# `script -q /dev/null cmd ...` is the BSD/macOS form and discards the typescript.
set -eu
FRAMES="${IPHONE_HAND_FRAMES:-/Volumes/iphone-hand-frames/frames}"
# Default frames dir lives on a tiny RAM disk to reduce SSD writes. UxPlay
# rewrites JPEGs at up to ~49 fps on Linux (measured 2026-10-10). Create it idempotently;
# read (man hdiutil): ram://N is N 512-byte sectors. 16384 sectors = 8 MiB.
case "$FRAMES" in
    /Volumes/iphone-hand-frames|/Volumes/iphone-hand-frames/*)
        [ -d /Volumes/iphone-hand-frames ] || \
            diskutil erasevolume HFS+ iphone-hand-frames \
                "$(hdiutil attach -nomount ram://16384)" >/dev/null ;;
esac
mkdir -p "$FRAMES"
chmod 700 "$FRAMES"
# Clear only when the receiver (re)starts, never while the daemon redeploys.
rm -f "$FRAMES"/f*.jpg
export GST_DEBUG="${GST_DEBUG:-2}"
exec script -q /dev/null "$(dirname "$0")/uxplay.bin" "$@"
