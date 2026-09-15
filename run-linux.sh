#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "$(readlink -f -- "$0")")"
if [[ ! -x .venv/bin/python ]]; then
    echo 'Run scripts/install-linux.sh first.' >&2
    exit 1
fi
if [[ ! -x overlay-ui/src-tauri/target/release/ownkey-overlay ]]; then
    echo 'The Ownkey overlay is missing. Run scripts/build-overlay-linux.sh.' >&2
    exit 1
fi
export OWNKEY_TAURI_OVERLAY_ONLY=1
export PYSTRAY_BACKEND=appindicator
exec .venv/bin/python ownkey.py "$@"
