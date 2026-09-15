# Ownkey Overlay UI (Tauri + React + shadcn-style)

This folder contains a standalone desktop overlay built with:

- `Tauri` (native shell)
- `React + TypeScript`
- `Tailwind CSS` + shadcn-style component structure

The app is designed to replace the Tkinter overlay while keeping your Python audio/hotkey/transcription backend.

## Run

From this folder:

```bash
pnpm install
pnpm dev
```

This starts:

- Vite dev server on `http://localhost:1420`
- Tauri desktop window (transparent, always-on-top overlay)

## Build

```bash
pnpm build:debug
```

or a release executable without standalone Tauri installer bundles:

```bash
pnpm build:binary
```

`pnpm build` also creates Tauri's MSI and NSIS bundles. Ownkey's Windows
installer pipeline uses `build:binary` and packages only the resulting
`src-tauri/target/release/ownkey-overlay.exe` in Inno Setup.

## UI Design Workflow

- Edit overlay visuals in:
  - `src/components/overlay/voice-overlay.tsx`
  - `src/index.css`
  - `tailwind.config.js`
- Design helper controls (dev only) are in:
  - `src/components/overlay/dev-toolbar.tsx`

## Python Bridge Contract (UDP)

Tauri listens on:

- `127.0.0.1:38485` (UDP)

Send JSON payloads from Python either as full state or patch.

### Full state example

```json
{
  "connection": "online",
  "listening": "listening",
  "processing": "idle",
  "target": "selected",
  "level": 0.62,
  "visible": true,
  "message": null
}
```

### State mapping (overlay behavior)

- `visible: false` -> overlay hidden (ready/idle)
- `listening: "listening"` + low `level` -> idle-listening waveform
- `listening: "listening"` + high `level` -> active-listening waveform
- `processing: "processing"` -> processing animation
- `target: "not_selected"` or `connection: "offline"` -> warning/error tint + tip bubble

### Patch example

```json
{
  "processing": "processing",
  "level": 0.0
}
```

## Python Runtime Toggle

In your existing `ownkey.py` runtime, you can disable the Tkinter overlay and drive only Tauri:

```powershell
$env:OWNKEY_TAURI_OVERLAY_ONLY="1"
```

The Python app will still emit UDP patches to `127.0.0.1:38485`.

## Frontend/Tauri Commands

- `get_overlay_state` - returns the current state
- `set_overlay_state` - sets and broadcasts state (used by dev toolbar)

## Linux

From the repository root, `scripts/setup-linux.sh` installs Ubuntu build
prerequisites; `scripts/build-overlay-linux.sh` builds the release binary at
`overlay-ui/src-tauri/target/release/ownkey-overlay`. Node.js and pnpm are required.
`run-linux.sh` starts the backend and this overlay together.

On GNOME Wayland, the overlay uses XWayland for positioning and always-on-top
behavior. It starts hidden and does not take focus. The backend still uses
Wayland-compatible keyboard/clipboard integration. The overlay exits if its
owning backend exits, including a crash.

Run the real process lifecycle test with:

```bash
OWNKEY_TEST_OVERLAY_EXE="$PWD/overlay-ui/src-tauri/target/release/ownkey-overlay" \
  PYSTRAY_BACKEND=appindicator .venv/bin/python -m unittest discover -s tests -p test_overlay_integration.py
```
