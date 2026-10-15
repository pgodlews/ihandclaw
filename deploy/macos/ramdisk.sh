#!/bin/sh
# Idempotently create and mount a tiny RAM disk for AirPlay frames; prints the mount point.
# Read (man hdiutil, "DEVICES": ram://SIZE creates a RAM disk of SIZE 512-byte sectors;
# -nomount leaves it unmounted) and man diskutil (erasevolume formats and mounts a volume).
# The volume lives until reboot; frames never touch the internal SSD.
set -eu
VOLUME="${1:-iphone-hand-frames}"
SECTORS="${2:-16384}"  # 8 MiB; max-files=5 JPEGs need far less [estimated: <=2 MB]
if [ -d "/Volumes/$VOLUME" ]; then
    echo "/Volumes/$VOLUME"
    exit 0
fi
diskutil erasevolume HFS+ "$VOLUME" "$(hdiutil attach -nomount ram://"$SECTORS")" >/dev/null
mkdir -p "/Volumes/$VOLUME/frames"
chmod 700 "/Volumes/$VOLUME/frames"
echo "/Volumes/$VOLUME"
