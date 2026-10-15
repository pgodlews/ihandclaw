#!/usr/bin/env python3
"""Manual probe for USB iPhone screen capture. Run by the OWNER on the Mac, from host/:

    uv pip install pyobjc-framework-AVFoundation pyobjc-framework-CoreMedia \
        pyobjc-framework-CoreMediaIO pyobjc-framework-CoreImage pyobjc-framework-Quartz \
        pyobjc-framework-libdispatch
    uv run python ../scripts/usb_capture_probe.py --frames 60 --save /tmp/usb-frame.png

Lists muxed capture devices (names only, never unique IDs), captures N frames, prints frame
size and measured fps, and saves one PNG. Expect a macOS camera-permission prompt for the
running process on first use. Never run by the agent.
"""
import argparse
import sys
import time


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--frames', type=int, default=60, help='frames to collect (default 60)')
    parser.add_argument('--save', required=True, help='path for one PNG output frame')
    args = parser.parse_args()

    from iphone_hand.usb_capture import AVFoundationBackend, list_devices

    print('Muxed capture devices seen by AVFoundation (before enabling screen capture):')
    try:
        devices = list_devices()
    except Exception as error:
        print(f'  device listing failed: {type(error).__name__}: {error}')
        devices = []
    for device in devices:
        print(f"  {device['name']} ({device['model']})")
    if not devices:
        print('  (none yet - screen-capture devices appear only after enabling the CMIO property)')

    backend = AVFoundationBackend()
    backend.start()
    print(f'After enabling: device={backend.device_name()!r} running={backend.running()}')
    try:
        deadline = time.monotonic() + args.frames / 5 + 10
        collected = 0
        last = None
        while collected < args.frames and time.monotonic() < deadline:
            image, ts = backend.latest()
            if ts is not None and ts is not last:
                collected += 1
                last = ts
                if collected == 1:
                    print(f'first frame: {image.size[0]}x{image.size[1]} px')
            time.sleep(0.005)
        if collected == 0:
            print('No frames arrived. Is the iPhone connected by USB, unlocked, and trusted?')
            return 1
        print(f'captured {collected} frames, measured fps over session window: {backend.fps():.1f}')
        image, _ = backend.latest()
        image.save(args.save, 'PNG')
        print(f'saved {args.save} ({image.size[0]}x{image.size[1]})')
        return 0
    finally:
        backend.stop()


if __name__ == '__main__':
    sys.exit(main())
