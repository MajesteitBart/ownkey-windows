# Ownkey Session Backlog

This file captures the compacted session summary and the agreed implementation order so the work is stored in the repo instead of only in chat.

## Current Context

- The app has a Python backend in `ownkey.py` and a Tauri/React overlay in `overlay-ui/`.
- The settings window and tray icon still need a visual refresh.
- The overlay visual needs to move away from bottom-aligned white bars inside a gray card.
- The desired direction is a centered, symmetric neon waveform without a background block, closer to a futuristic audio signature.

## Agreed Backlog

1. Fix overlay stability first.
   - Recover if the frontend overlay process or window disappears while the backend keeps running.
   - Re-sync overlay state after recovery.
   - Make recovery happen during startup and on recording start.

2. Replace the old bar visualizer with a real waveform.
   - Remove the gray card and border.
   - Use a centered, mirrored waveform around the middle axis.
   - Give it a neon palette and layered glow.
   - Make normal speech look believable instead of immediately maxed out.

3. Recalibrate audio response.
   - Revisit normalization, curve shaping, smoothing and thresholds.
   - Separate room noise from actual speech more clearly.

4. Modernize the settings popup.
   - Better spacing, hierarchy and grouping.
   - Cleaner controls and clearer primary actions.

5. Redesign the tray icon.
   - Stronger symbol than the current circle-plus-letter icon.
   - Clear state differences for idle, recording and processing.

6. End-to-end polish.
   - Multiple recording cycles.
   - Overlay recovery after frontend failure.
   - Visual consistency across popup, tray and overlay.

## Work Already Completed

- Added overlay recovery hooks in `ownkey.py`.
- Added overlay state resync support over the UDP bridge.
- Moved Tauri show/hide window work onto the main thread in `overlay-ui/src-tauri/src/lib.rs`.
- Made the overlay window non-focusable to avoid stealing input focus.
- Made the ready chime optional and defaulted it to off.
- Built hotfix installers up to version `0.2.3`.

## Immediate Next Step

- Restyle the overlay to the brand pill from the Ownkey website. The neon
  waveform direction above is superseded — follow `docs/OVERLAY_DESIGN_SPEC.md`.
