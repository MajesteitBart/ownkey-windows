#!/usr/bin/env bash
# Run as your regular desktop user, after setup-linux.sh.
set -euo pipefail
cd -- "$(dirname -- "$(readlink -f -- "$0")")/.."
python3 -m venv --system-site-packages .venv
.venv/bin/python -m pip install -r requirements.txt
# Install/build the actual product overlay, not the legacy Tk fallback.
./scripts/build-overlay-linux.sh
.venv/bin/python - <<'PY'
from pathlib import Path
import linux_desktop
path = Path.home() / '.local/share/applications/ownkey.desktop'
path.parent.mkdir(parents=True, exist_ok=True)
entry = linux_desktop.desktop_entry()
# Use the launcher to select the desktop tray backend.
script = str(Path.cwd() / 'run-linux.sh')
import re
entry = re.sub(r'^Exec=.*$', lambda _: 'Exec="' + script.replace('\\', '\\\\').replace('"', '\\"').replace('`', '\\`').replace('$', '\\$').replace('%', '%%') + '" --settings', entry, flags=re.M)
path.write_text(entry)
print('Installed Ownkey in the application menu. Run ./run-linux.sh --settings')
PY
