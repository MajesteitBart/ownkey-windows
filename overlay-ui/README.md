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

or release:

```bash
pnpm build
```

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
