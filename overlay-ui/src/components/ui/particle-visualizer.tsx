import { useEffect, useRef } from "react";

import type { OverlayPalette, ParticleVariant } from "@/lib/overlay";

// Canvas particle engine for the pill overlay. Each state renders its own
// particle system; positions are pure functions of hashed indices and time,
// so systems stay continuous and cross-fade cleanly on state changes.

interface ParticleVisualizerProps {
  variant: ParticleVariant;
  palette: OverlayPalette;
  level: number;
}

const CROSSFADE_S = 0.26;

function clamp01(value: number): number {
  if (!Number.isFinite(value)) return 0;
  if (value <= 0) return 0;
  if (value >= 1) return 1;
  return value;
}

function hash01(n: number): number {
  const s = Math.sin(n * 127.1 + 311.7) * 43758.5453;
  return s - Math.floor(s);
}

function makeSprite(color: string): HTMLCanvasElement {
  const sprite = document.createElement("canvas");
  sprite.width = 32;
  sprite.height = 32;
  const ctx = sprite.getContext("2d");
  if (ctx) {
    const gradient = ctx.createRadialGradient(16, 16, 0, 16, 16, 16);
    gradient.addColorStop(0, "#ffffff");
    gradient.addColorStop(0.22, color);
    gradient.addColorStop(1, "transparent");
    ctx.fillStyle = gradient;
    ctx.fillRect(0, 0, 32, 32);
  }
  return sprite;
}

interface RenderEnv {
  ctx: CanvasRenderingContext2D;
  W: number;
  H: number;
  t: number;
  since: number;
  level: number;
  alpha: number;
  palette: OverlayPalette;
  dot: (x: number, y: number, size: number, alpha: number) => void;
}

// 1. Listening: thin vertical pulses clustered middle-left, dissolving into
// sparse drifting dot rows toward the right.
function drawListening(env: RenderEnv) {
  const { ctx, W, H, t, level, alpha, palette } = env;
  const midY = H / 2;
  const barCount = 34;
  const barsW = W * 0.55;
  ctx.fillStyle = palette.main;
  for (let i = 0; i < barCount; i += 1) {
    const u = i / (barCount - 1);
    const x = 6 + u * (barsW - 12);
    const cluster = Math.exp(-(((u - 0.4) / 0.28) ** 2));
    const wiggle =
      0.45 + 0.55 * Math.abs(Math.sin(t * 8.4 + i * 2.13) * Math.sin(t * 5.1 + i * 1.31));
    const h = 1.1 + H * 0.45 * cluster * (0.16 + 0.84 * level) * wiggle;
    const a = (0.22 + 0.78 * cluster) * alpha;
    ctx.globalAlpha = a;
    ctx.fillRect(x, midY - h, 1.4, h * 2);
    if (h > H * 0.14) {
      env.dot(x + 0.7, midY - h, 4.5, a * 0.45);
      env.dot(x + 0.7, midY + h, 4.5, a * 0.45);
    }
  }
  ctx.globalAlpha = 1;

  // Dot rows fill the entire right half, dimming only near the far edge so
  // the pill reads as one continuous visualization at any voice level.
  const dotCount = 80;
  const start = W * 0.52;
  const span = W * 0.46;
  for (let j = 0; j < dotCount; j += 1) {
    const hx = hash01(j * 3 + 1);
    const hy = hash01(j * 5 + 2);
    const hs = hash01(j * 7 + 3);
    const x = start + ((hx * span + t * (4 + hs * 7)) % span);
    const row = Math.floor(hy * 6) - 2.5;
    const y = midY + row * H * 0.14 + (hash01(j * 11 + 5) - 0.5) * 3;
    const fade = 0.2 + 0.8 * (1 - (x - start) / span) ** 0.85;
    const flicker = 0.45 + 0.55 * Math.abs(Math.sin(t * (1.5 + hs * 2.4) + j * 2.9));
    env.dot(x, y, 2 + hs * 1.6, fade * flicker * (0.32 + 0.42 * level) * alpha);
  }
}

// 2. Transcribing: a flowing dotted ribbon of stacked strands, densest and
// brightest at the center, streaming left to right.
function drawTranscribing(env: RenderEnv) {
  const { W, H, t, alpha } = env;
  const midY = H / 2;
  for (let s = 0; s < 5; s += 1) {
    for (let k = 0; k < 34; k += 1) {
      const seed = s * 53 + k;
      const u = (hash01(seed) + t * 0.085 * (0.85 + 0.3 * hash01(seed * 3 + 7))) % 1;
      const x = u * W;
      const taper = Math.sin(Math.PI * u) ** 0.8;
      const wave = Math.sin(u * Math.PI * 2.1 - t * 1.7 + s * 0.35);
      const y = midY + (s - 2) * 2.7 + wave * H * 0.24 * taper;
      const bright = 0.26 + 0.74 * Math.exp(-(((u - 0.5) / 0.24) ** 2));
      const flicker = 0.5 + 0.5 * Math.abs(Math.sin(t * 2.3 + seed * 1.7));
      env.dot(x, y, 1.8 + 1.6 * bright, bright * flicker * alpha * 0.8);
    }
  }
  for (let j = 0; j < 14; j += 1) {
    const u = (hash01(j * 17 + 3) + t * 0.06) % 1;
    const x = u * W;
    const y = midY + (hash01(j * 13 + 9) - 0.5) * H * 0.8;
    env.dot(x, y, 1.6, 0.2 * Math.sin(Math.PI * u) * alpha);
  }
}

// 3. Speak an edit: a sweeping arc of stardust cresting mid-pill and
// resolving into a bright comet point near the right end.
function editCurve(u: number, H: number): number {
  return H * 0.12 - Math.exp(-(((u - 0.4) / 0.3) ** 2)) * H * 0.3;
}

function drawEdit(env: RenderEnv) {
  const { W, H, t, level, alpha } = env;
  const midY = H / 2;
  const cometU = 0.86;
  const energy = 0.45 + 0.55 * level;

  for (let i = 0; i < 66; i += 1) {
    const sp = 0.05 + 0.14 * hash01(i * 3 + 2);
    const u = ((hash01(i * 7 + 4) + t * sp) % 1) * cometU;
    const converge = 1 - (u / cometU) ** 1.6;
    const jitterY = (hash01(i * 11 + 6) - 0.5) * (2.5 + 9 * converge);
    const x = u * W;
    const y = midY + editCurve(u, H) + jitterY;
    const a = (0.16 + 0.84 * (u / cometU) ** 1.5) * energy * alpha;
    env.dot(x, y, 1.7 + 1.6 * (u / cometU), a);
  }
  for (let i = 0; i < 26; i += 1) {
    const u = hash01(i * 5 + 1) * cometU;
    const x = u * W;
    const y = midY + editCurve(u, H) + (hash01(i * 9 + 8) - 0.5) * H * 0.7;
    const twinkle = Math.max(0, Math.sin(t * (1.2 + hash01(i * 3) * 2.6) + i * 2.2));
    env.dot(x, y, 1.5, 0.2 * twinkle * energy * alpha);
  }

  const cx = cometU * W;
  const cy = midY + editCurve(cometU, H);
  const pulse = 1 + 0.22 * Math.sin(t * 5.2);
  env.dot(cx, cy, 8.5 * pulse, 0.95 * energy * alpha);
  env.dot(cx, cy, 16, 0.32 * energy * alpha);
  for (let j = 0; j < 4; j += 1) {
    const du = 0.02 + hash01(j * 21 + 2) * 0.06;
    env.dot((cometU + du) * W, cy + (hash01(j * 31 + 5) - 0.5) * 6, 1.6, 0.3 * energy * alpha);
  }
}

// 4. Rewriting: half a dozen parallel dotted waves — some bright, some broken —
// dipping through a central trough and rising again toward the right.
const TRACK_ALPHA = [0.95, 0.5, 0.75, 0.38, 0.85, 0.55];
const TRACK_GATE = [-1, -0.2, -0.6, 0.1, -0.8, -0.35];

function drawRewriting(env: RenderEnv) {
  const { W, H, t, alpha } = env;
  const midY = H / 2;
  for (let k = 0; k < 6; k += 1) {
    for (let d = 0; d < 30; d += 1) {
      const seed = k * 97 + d;
      const u = (hash01(seed) + t * 0.055 * (0.8 + 0.4 * hash01(seed * 3 + 5))) % 1;
      if (Math.sin(u * 26 + k * 7 + t * 0.6) < TRACK_GATE[k]) continue;
      const wave = Math.sin(u * Math.PI * 2.2 - t * 1.1 + k * 0.14);
      const x = u * W;
      const y = midY + (k - 2.5) * 3.4 + wave * H * 0.2;
      const bend = 0.3 + 0.7 * Math.exp(-(((u - 0.62) / 0.26) ** 2));
      env.dot(x, y, 1.7 + bend, TRACK_ALPHA[k] * bend * alpha * 0.85);
    }
  }
  for (let j = 0; j < 10; j += 1) {
    const u = (hash01(j * 19 + 7) + t * 0.04) % 1;
    const y = H / 2 + (hash01(j * 23 + 3) - 0.5) * H * 0.85;
    env.dot(u * W, y, 1.4, 0.18 * Math.sin(Math.PI * u) * alpha);
  }
}

// 5. Done / field: a calm, loosely gridded field of particles that settles
// and gently dims; "field" reuses it for warning/error palettes with jitter.
function drawField(env: RenderEnv, settle: boolean) {
  const { W, H, t, since, alpha } = env;
  const midY = H / 2;
  const cols = 18;
  for (let i = 0; i < 90; i += 1) {
    const col = i % cols;
    const row = Math.floor(i / cols);
    const jx = (hash01(i * 3 + 2) - 0.5) * 5;
    const jy = (hash01(i * 5 + 4) - 0.5) * 4;
    let x = W * 0.14 + col * ((W * 0.78) / (cols - 1)) + jx;
    const y = midY + (row - 2) * H * 0.16 + jy + Math.sin(t * 0.8 + i * 1.9) * 0.9;
    if (!settle) {
      x += Math.sin(t * 12 + i * 3.3) * 0.7;
    }
    const centerFade = Math.exp(-(((x / W - 0.55) / 0.33) ** 2));
    const shimmer = 0.32 + 0.2 * Math.sin(t * 1.2 + i * 2.7);
    const entry = settle ? Math.min(1, since * 1.6) * (1 + 0.7 * Math.exp(-since * 1.9)) : 1;
    env.dot(x, y, 1.9 + hash01(i * 7 + 1) * 1.4, centerFade * shimmer * entry * alpha);
  }
}

// Loading: a comet dash running along the center line while the mic arms.
function drawLoading(env: RenderEnv) {
  const { W, H, t, alpha } = env;
  const midY = H / 2;
  for (let g = 0; g < 20; g += 1) {
    const x = (g / 19) * W;
    env.dot(x, midY, 1.4, 0.09 * alpha);
  }
  const u = (t * 0.55) % 1.25;
  for (let j = 0; j < 15; j += 1) {
    const x = (u - j * 0.016) * W;
    if (x < 0 || x > W) continue;
    const a = (1 - j / 15) ** 1.6 * alpha;
    const y = midY + Math.sin(t * 6 + j * 0.7) * 1.2;
    env.dot(x, y, 4.6 - j * 0.22, a * 0.85);
  }
}

function drawVariant(variant: ParticleVariant, env: RenderEnv) {
  if (variant === "listening") drawListening(env);
  else if (variant === "transcribing") drawTranscribing(env);
  else if (variant === "edit") drawEdit(env);
  else if (variant === "rewriting") drawRewriting(env);
  else if (variant === "done") drawField(env, true);
  else if (variant === "loading") drawLoading(env);
  else drawField(env, false);
}

export function ParticleVisualizer({ variant, palette, level }: ParticleVisualizerProps) {
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const propsRef = useRef({ variant, palette, level });

  useEffect(() => {
    propsRef.current = { variant, palette, level };
  }, [variant, palette, level]);

  useEffect(() => {
    const canvas = canvasRef.current;
    const ctx = canvas?.getContext("2d");
    if (!canvas || !ctx) return;

    const sprites = new Map<string, HTMLCanvasElement>();
    const spriteFor = (color: string) => {
      let sprite = sprites.get(color);
      if (!sprite) {
        sprite = makeSprite(color);
        sprites.set(color, sprite);
      }
      return sprite;
    };

    let raf = 0;
    let last = performance.now();
    const envelope = { value: 0 };
    const active = {
      current: { variant: propsRef.current.variant, palette: propsRef.current.palette },
      prev: null as null | { variant: ParticleVariant; palette: OverlayPalette },
      switchedAt: performance.now() / 1000 - 10,
    };

    const makeEnv = (
      pal: OverlayPalette,
      alpha: number,
      t: number,
      since: number,
      W: number,
      H: number,
    ): RenderEnv => ({
      ctx,
      W,
      H,
      t,
      since,
      level: envelope.value,
      alpha,
      palette: pal,
      dot: (x, y, size, a) => {
        if (a <= 0.01 || size <= 0) return;
        ctx.globalAlpha = Math.min(1, a);
        ctx.drawImage(spriteFor(pal.main), x - size, y - size, size * 2, size * 2);
        ctx.globalAlpha = 1;
      },
    });

    const render = (now: number) => {
      raf = requestAnimationFrame(render);
      const dt = Math.min(0.05, (now - last) / 1000);
      last = now;
      const t = now / 1000;
      const { variant: nextVariant, palette: nextPalette, level: rawLevel } = propsRef.current;

      if (nextVariant !== active.current.variant) {
        active.prev = active.current;
        active.current = { variant: nextVariant, palette: nextPalette };
        active.switchedAt = t;
      } else {
        active.current.palette = nextPalette;
      }

      const dpr = window.devicePixelRatio || 1;
      const cssW = canvas.clientWidth;
      const cssH = canvas.clientHeight;
      if (cssW === 0 || cssH === 0) return;
      if (canvas.width !== Math.round(cssW * dpr) || canvas.height !== Math.round(cssH * dpr)) {
        canvas.width = Math.round(cssW * dpr);
        canvas.height = Math.round(cssH * dpr);
      }
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);

      const target = clamp01(rawLevel);
      const tau = target > envelope.value ? 0.05 : 0.24;
      envelope.value += (target - envelope.value) * (1 - Math.exp(-dt / tau));

      ctx.clearRect(0, 0, cssW, cssH);
      ctx.globalCompositeOperation = "lighter";

      const progress = clamp01((t - active.switchedAt) / CROSSFADE_S);
      if (active.prev && progress < 1) {
        drawVariant(
          active.prev.variant,
          makeEnv(active.prev.palette, (1 - progress) ** 1.2, t, 10, cssW, cssH),
        );
      } else {
        active.prev = null;
      }
      drawVariant(
        active.current.variant,
        makeEnv(active.current.palette, progress, t, t - active.switchedAt, cssW, cssH),
      );
    };

    raf = requestAnimationFrame(render);
    return () => cancelAnimationFrame(raf);
  }, []);

  return <canvas ref={canvasRef} className="h-full w-full" aria-hidden="true" />;
}
