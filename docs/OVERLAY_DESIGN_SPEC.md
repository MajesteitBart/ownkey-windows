# Overlay design spec — the brand pill

Restyle the Tauri overlay to match the "listening pill" from the Ownkey website
(https://majesteitbart.github.io/ownkey-site/, Windows section; source:
`MajesteitBart/ownkey-site` → `style.css`, `.overlaypill` / `.wave`). The pill
is the source of truth. This spec supersedes the "neon waveform" direction in
`SESSION_COMPACTING_BACKLOG.md` — the current dot-grid/particle/neon look and
its purple/cyan palette are replaced entirely.

## 1. Why

The current overlay is a 293×117 card with a navy gradient, a gradient border,
and a purple/cyan dot-grid visualizer. None of that is Ownkey. The brand system
(see `assets/ownkey-delano-brand-reference-2026-05-31-1.html` in this repo) is:
Keycap Black surfaces, Bone text, JetBrains Mono for spec-like labels, and one
orange waveform that means "Ownkey is listening." The site's pill nails it;
the app should render the identical artifact.

## 2. Design tokens

| Token      | Value       | Use in overlay                          |
|------------|-------------|------------------------------------------|
| `key`      | `#0E0E0E`   | Pill background                          |
| `graphite` | `#171717`   | (not used in pill; panels elsewhere)     |
| `slate`    | `#202020`   | kbd-chip background (optional hint)      |
| `hairline` | `#3A3A3A`   | Pill border, kbd border                  |
| `bone`     | `#F3F1EC`   | Emphasis text (warnings, messages)       |
| `ash`      | `#8E8A7F`   | Default status-label color               |
| `orange`   | `#DE5F14`   | Waveform bars (gradient end), glow       |
| `amber`    | `#F4A23C`   | Waveform bars (gradient start), warning  |
| `green`    | `#9DCB3B`   | "Done" confirmation tint                 |
| `red`      | `#E2574B`   | Error tint (from brand book misuse ✕)    |

Font: **JetBrains Mono 400/500**. Vendor a subset woff2 into
`overlay-ui/src/assets/fonts/` and register with `@font-face` — the packaged
app must not fetch from a CDN. Fallback stack:
`"JetBrains Mono", "Cascadia Mono", Consolas, monospace`.

## 3. The pill — reference CSS

This is the site's implementation, verbatim. Translate to Tailwind/JSX but do
not redesign it:

```css
.overlaypill{
  display:flex;align-items:center;gap:14px;
  background:#0E0E0E;border:1px solid #3a3a3a;border-radius:30px;
  padding:12px 20px;
  box-shadow:0 24px 50px -12px rgba(0,0,0,.9),0 0 40px -18px rgba(222,95,20,.55);
  white-space:nowrap;
}
.pill-label{
  font-family:"JetBrains Mono",monospace;font-size:.72rem;color:#8E8A7F;
  letter-spacing:.14em;text-transform:uppercase;
}
.wave{display:flex;align-items:center;gap:3px;height:22px}
.wave .bar{
  width:4px;height:var(--h);border-radius:3px;
  background:linear-gradient(180deg,#F4A23C,#DE5F14);
  animation:bounce 1.1s ease-in-out infinite;animation-delay:var(--d);
}
@keyframes bounce{0%,100%{transform:scaleY(.4)}50%{transform:scaleY(1)}}
```

Overlay metrics (px, at 100% scale):

- Pill height: **46** (22px wave + 12px vertical padding). Radius **23**
  (fully rounded — use `border-radius:9999px`).
- Contents, left→right: waveform (5 bars), status label. Gap **14**.
- Padding: **12 20**. Border: **1px `#3A3A3A`**.
- Bars: **5**, width **4**, gap **3**, radius **3**, container height **22**.
  Rest heights (`--h`): `40% 75% 100% 65% 45%` — center-peaked, asymmetric,
  matching the logo's waveform muzzle silhouette.
- Label: **11px**, tracking **.14em**, uppercase, color `ash`.
- Shadows: keep both site shadows. The orange glow
  (`0 0 40px -18px rgba(222,95,20,.55)`) is what seats the pill on any
  wallpaper — do not drop it.

Optional (off by default): a trailing kbd chip (`Right Alt`) styled
`11px JetBrains Mono, #F3F1EC on #202020, 1px #3A3A3A border (2px bottom),
radius 6, padding 2 8`. Show only in a future "armed/ready" hint state.

## 4. Window (tauri.conf.json)

The pill is far smaller than the current 293×117 card. Change the window to
**320 × 96**, pill centered horizontally and anchored to the bottom, with
~24px transparent margin on all sides so the glow shadow never clips. Longest
label ("SELECT A TEXT BOX") must fit within 320 — verify, and let the pill hug
its content rather than stretching. Keep: transparent, no decorations,
always-on-top, skip taskbar, non-focusable, fixed size.

## 5. State mapping

Keep `deriveMode` and `statusLabel` in `overlay-ui/src/lib/overlay.ts` as-is.
Replace `modePalette`/`activityPalette` (purple/cyan) with brand values. The
label renders `statusLabel(...)` uppercased; bars carry the state color; the
label stays `ash` unless noted.

| Mode              | Label (existing)          | Bars                                        | Tint |
|-------------------|---------------------------|---------------------------------------------|------|
| `loading`         | STARTING                  | left→right ripple, low amplitude            | orange |
| `listening_wait`  | LISTENING                 | slow breathe, scaleY .25–.45, stagger       | orange |
| `listening_audio` | LISTENING / SPEAK AN EDIT | level-driven (see §6)                       | orange |
| `processing`      | TRANSCRIBING / REWRITING  | site bounce: scaleY .4→1, 1.1s ease-in-out  | orange |
| `done`            | DONE                      | settle to rest heights, freeze              | green bars, label `ash` |
| `warning`         | SELECT A TEXT BOX / CHECK INPUT | static at rest heights                | amber bars, label `bone` |
| `error`           | TRY AGAIN / NO CONNECTION | static, low (scaleY .3)                     | red bars, label `red` |
| `idle` + message  | message text              | rest heights, breathe                       | orange |

Rewrite activity keeps the orange system — delete the cyan
`REWRITE_PALETTE`. Brand rule: orange is the one accent that means voice/AI;
rewrite is distinguished by its label, not a new color. Tinted bars swap the
gradient for a flat fill of the tint color (green/amber/red) — gradients are
reserved for the orange voice state.

## 6. Motion

- **Height only.** Bars animate exclusively with `transform: scaleY()`,
  `transform-origin: center`. Never x-translate, tilt, rotate, recolor
  per-bar, or outline (brand book, "Rules of use").
- `listening_audio`: per-bar target = rest height profile × (0.35 + 0.65 ×
  eased level), with per-bar phase offsets so bars move independently.
  Smooth with ~90ms attack / ~160ms release lerp in the frontend (backend
  already gates and smooths `level`).
- Keep the existing enter/exit choreography and timings:
  `ownkey-overlay-in` 220ms, exit fade 240ms (`EXIT_MS`), state cross-fade
  140/90ms (`ownkey-state-in/out`) — they're good; only the visuals change.
- `prefers-reduced-motion`: bars hold rest heights (no bounce, no
  level-driven motion); keep the existing reduced-motion fade variants.

## 7. Implementation map

| File | Change |
|------|--------|
| `overlay-ui/src/index.css` | Add brand tokens + `@font-face`; add pill/bar keyframes; drop navy/dark shadcn values that become unused |
| `overlay-ui/src/components/ui/brand-waveform.tsx` | **New** — the 5-bar component (props: `mode`, `level`, `tint`) |
| `overlay-ui/src/components/overlay/voice-overlay.tsx` | Replace the card (gradient background, gradient border, dot grid, glow text) with the pill; render `BrandWaveform` + label |
| `overlay-ui/src/lib/overlay.ts` | Swap palettes to brand tokens; delete `REWRITE_PALETTE`, `buildWaves`, `wavePoints`, `smoothPath` once unused |
| `overlay-ui/src/components/ui/{dot-grid,particle,neon}-*.tsx` | Delete after the swap (git history keeps them) |
| `overlay-ui/src-tauri/tauri.conf.json` | Window 320×96 (all four min/max values) |
| `ownkey.py` | No change required; UDP contract (`connection/listening/processing/target/level/visible/message/activity`) is untouched |

## 8. Acceptance checklist

- [ ] Side-by-side with the site's Windows-section pill at 100% zoom: same
      shape, border, glow, bar geometry, label style.
- [ ] All modes exercised (`run-all.bat --show-test`, plus real dictation,
      rewrite, offline, and no-focus cases) show the states in §5.
- [ ] Bars move by height only; no color cycling during listening.
- [ ] Label legible on white and black wallpapers (the glow + border do the
      seating; if not, raise border alpha, never add a background wash).
- [ ] 100% / 125% / 150% Windows display scaling: no clipping of the glow,
      no blurry hairline border.
- [ ] Reduced motion honored end-to-end.
- [ ] No CDN requests at runtime (fonts bundled).
