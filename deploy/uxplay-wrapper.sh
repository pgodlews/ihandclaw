#!/bin/sh
# Wrapper used by the receiver unit or child supervisor: line-buffered output (logs reach the journal
# immediately) and GStreamer warnings on. Runs the 1.73.6 build next to it.
export GST_DEBUG="${GST_DEBUG:-2}"
exec stdbuf -oL -eL "$(dirname "$0")/uxplay.bin" "$@"
