#!/bin/sh
set -eu
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
if command -v python3.12 >/dev/null 2>&1; then
    exec python3.12 "$SCRIPT_DIR/install.py" "$@"
fi
if command -v python3 >/dev/null 2>&1; then
    exec python3 "$SCRIPT_DIR/install.py" "$@"
fi
echo "Python 3.12 is required. Install it, then run python3.12 install.py." >&2
exit 1
