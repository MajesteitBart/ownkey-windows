#!/usr/bin/env bash
# Ubuntu/Debian system dependencies. Run as root via sudo or pkexec.
set -euo pipefail
if [[ $EUID -ne 0 ]]; then
    exec sudo bash "$0" "$@"
fi
apt-get update
apt-get install -y python3-venv python3-dev python3-tk python3-gi \
    gir1.2-ayatanaappindicator3-0.1 libportaudio2 wl-clipboard xclip \
    build-essential acl pkg-config rustc cargo libwebkit2gtk-4.1-dev \
    libayatana-appindicator3-dev librsvg2-dev patchelf gir1.2-ibus-1.0 libxkbcommon0 xkb-data
# Grant only the active local desktop session access. No input-group membership
# or world-readable devices; logind removes ACLs when the session is inactive.
cat > /etc/udev/rules.d/70-ownkey-input.rules <<'RULES'
SUBSYSTEM=="input", KERNEL=="event*", ENV{ID_INPUT_KEYBOARD}=="1", TAG+="uaccess"
SUBSYSTEM=="misc", KERNEL=="uinput", TAG+="uaccess"
RULES
modprobe uinput
udevadm control --reload-rules
udevadm trigger --subsystem-match=input
udevadm trigger --subsystem-match=misc --sysname-match=uinput
udevadm settle
