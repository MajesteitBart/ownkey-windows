import type { OverlayState } from "@/types/overlay";

export type OverlayMode =
  | "idle"
  | "loading"
  | "listening_wait"
  | "listening_audio"
  | "processing"
  | "done"
  | "warning"
  | "error";

export interface OverlayPalette {
  main: string;
  soft: string;
  dim: string;
}

export interface WaveSet {
  main: string;
  soft: string;
  dim: string;
}

const LISTENING_AUDIO_THRESHOLD = 0.05;

function clamp01(value: number): number {
  if (!Number.isFinite(value)) return 0;
  if (value <= 0) return 0;
  if (value >= 1) return 1;
  return value;
}

export function deriveMode(state: OverlayState, filteredLevel: number): OverlayMode {
  if (state.listening === "error" || state.processing === "error") return "error";
  if (state.connection === "offline") return "error";
  if (state.target === "not_selected") return "warning";
  if (state.listening === "arming") return "loading";
  if (state.processing === "processing") return "processing";
  if (state.listening === "listening") {
    return filteredLevel >= LISTENING_AUDIO_THRESHOLD ? "listening_audio" : "listening_wait";
  }
  if (state.processing === "done") return "done";
  return "idle";
}

export function statusLabel(state: OverlayState, mode: OverlayMode): string {
  if (state.target === "not_selected") return "Select a text box";
  if (state.connection === "offline") return "No connection";
  if (mode === "error") return "Try again";
  if (state.activity === "rewrite" && state.listening === "listening") return "Speak an edit";
  if (mode === "loading") return "Starting";
  if (mode === "listening_wait" || mode === "listening_audio") return "Listening";
  if (mode === "processing") {
    return state.activity === "rewrite" ? "Rewriting" : "Transcribing";
  }
  if (mode === "done") return "Done";
  if (mode === "warning") return "Check input";
  if (state.message && state.message.trim()) return state.message.trim().replace(/\.{3}$/, "");
  return "Ready";
}

const REWRITE_PALETTE: OverlayPalette = {
  main: "#22e6e6",
  soft: "#8fffff",
  dim: "#00aebd",
};

const REWRITE_TINTED_MODES: ReadonlySet<OverlayMode> = new Set([
  "loading",
  "listening_wait",
  "listening_audio",
  "processing",
]);

export function activityPalette(mode: OverlayMode, state: OverlayState): OverlayPalette {
  if (state.activity === "rewrite" && REWRITE_TINTED_MODES.has(mode)) {
    return REWRITE_PALETTE;
  }
  return modePalette(mode);
}

export function modePalette(mode: OverlayMode): OverlayPalette {
  if (mode === "error") {
    return { main: "#ff7f98", soft: "#ffb3c1", dim: "#ff5d74" };
  }
  if (mode === "warning") {
    return { main: "#ffd778", soft: "#ffe6ab", dim: "#ffb84f" };
  }
  if (mode === "processing") {
    return { main: "#b45cff", soft: "#e0b1ff", dim: "#7720d8" };
  }
  if (mode === "loading") {
    return { main: "#b45cff", soft: "#e0b1ff", dim: "#7720d8" };
  }
  if (mode === "done") {
    return { main: "#22e6e6", soft: "#8fffff", dim: "#00aebd" };
  }
  return { main: "#b45cff", soft: "#e0b1ff", dim: "#7720d8" };
}

export type ParticleVariant =
  | "listening"
  | "transcribing"
  | "edit"
  | "rewriting"
  | "done"
  | "loading"
  | "field";

export function deriveVariant(mode: OverlayMode, state: OverlayState): ParticleVariant {
  if (mode === "loading") return "loading";
  if (mode === "listening_wait" || mode === "listening_audio") {
    return state.activity === "rewrite" ? "edit" : "listening";
  }
  if (mode === "processing") {
    return state.activity === "rewrite" ? "rewriting" : "transcribing";
  }
  if (mode === "done") return "done";
  return "field";
}

interface Point {
  x: number;
  y: number;
}

function wavePoints(mode: OverlayMode, depth: number, phase: number, phaseOffset = 0): Point[] {
  const width = 156;
  const baseline = 42;
  const points: Point[] = [];
  for (let px = 0; px <= width; px += 2) {
    const t = px / width;
    const x = px + 1;
    let y: number;

    if (mode === "listening_audio") {
      // Pulse centers drift and each pulse breathes on its own clock so the
      // wave keeps moving organically instead of scaling one fixed shape.
      const c1 = 0.18 + 0.02 * Math.sin(phase * 0.62 + phaseOffset * 1.7);
      const c2 = 0.5 + 0.024 * Math.sin(phase * 0.48 + 2.1 + phaseOffset);
      const c3 = 0.8 - 0.02 * Math.sin(phase * 0.55 + 4.2 + phaseOffset * 1.3);
      const leftPulse = Math.exp(-(((t - c1) / 0.10) ** 2));
      const centerPulse = Math.exp(-(((t - c2) / 0.17) ** 2));
      const rightPulse = Math.exp(-(((t - c3) / 0.10) ** 2));
      const shoulder = Math.exp(-(((t - 0.34) / 0.08) ** 2)) + Math.exp(-(((t - 0.66) / 0.09) ** 2));
      const w1 = 0.64 + 0.36 * Math.sin(phase * 2.3 + phaseOffset * 2.9);
      const w2 = 0.74 + 0.26 * Math.sin(phase * 1.7 + 1.9 + phaseOffset * 2.2);
      const w3 = 0.64 + 0.36 * Math.sin(phase * 2.6 + 3.7 + phaseOffset * 2.5);
      const wS = 0.55 + 0.45 * Math.sin(phase * 2.0 + 5.1 + phaseOffset * 1.8);
      const profile =
        0.84 * leftPulse * w1 + 1.24 * centerPulse * w2 + 0.8 * rightPulse * w3 + 0.4 * shoulder * wS;
      const shimmer = 1 + 0.07 * Math.sin(phase * 1.55 + t * 10.5 + phaseOffset);
      const ripple = 0.5 + 0.5 * Math.sin(t * 22 - phase * 1.35 + phaseOffset);
      const amp = (2.6 + 11.5 * depth) * profile * shimmer + ripple * (0.6 + depth * 1.6);
      y = baseline - amp;
    } else if (mode === "loading") {
      const arch = Math.sin(Math.PI * t) ** 0.92;
      const pulse = 1 + 0.05 * Math.sin(phase * 0.8 + phaseOffset);
      y = baseline - (5.6 + 1.8 * depth) * arch * pulse;
    } else if (mode === "processing") {
      const arch = Math.sin(Math.PI * t) ** 0.92;
      const pulse = 1 + 0.05 * Math.sin(phase * 0.6 + phaseOffset);
      y = baseline - (5.9 + 2.2 * depth) * arch * pulse;
    } else if (mode === "listening_wait") {
      const arch = Math.sin(Math.PI * t) ** 0.9;
      const skew = 0.82 + 0.18 * Math.cos((t - 0.5) * Math.PI);
      const breathe = 1 + 0.05 * Math.sin(phase * 0.55 + phaseOffset);
      y = baseline - (4.6 + 2.4 * depth) * arch * skew * breathe;
    } else if (mode === "done") {
      const arch = Math.sin(Math.PI * t);
      y = baseline - (6 + 0.8 * Math.sin(phase * 0.45 + phaseOffset)) * arch;
    } else if (mode === "warning") {
      const arch = Math.sin(Math.PI * t) ** 0.9;
      y = baseline - (6.2 + 0.8 * Math.sin(phase * 1 + phaseOffset)) * arch;
    } else if (mode === "error") {
      const arch = Math.sin(Math.PI * t) ** 0.9;
      y = baseline - (5.8 + 0.6 * Math.sin(phase * 1.7 + phaseOffset)) * arch;
    } else {
      const arch = Math.sin(Math.PI * t) ** 0.9;
      y = baseline - (5.8 + 0.6 * Math.sin(phase * 0.7 + phaseOffset)) * arch;
    }

    points.push({ x, y });
  }
  return points;
}

function smoothPath(points: Point[]): string {
  if (points.length === 0) return "";
  if (points.length === 1) return `M ${points[0].x} ${points[0].y}`;

  let d = `M ${points[0].x.toFixed(2)} ${points[0].y.toFixed(2)}`;
  for (let i = 1; i < points.length - 1; i += 1) {
    const current = points[i];
    const next = points[i + 1];
    const midX = (current.x + next.x) / 2;
    const midY = (current.y + next.y) / 2;
    d += ` Q ${current.x.toFixed(2)} ${current.y.toFixed(2)} ${midX.toFixed(2)} ${midY.toFixed(2)}`;
  }
  const last = points[points.length - 1];
  d += ` T ${last.x.toFixed(2)} ${last.y.toFixed(2)}`;
  return d;
}

export function buildWaves(mode: OverlayMode, level: number, phase: number): WaveSet {
  const clampedLevel = clamp01(level);
  const depth =
    mode === "listening_audio"
      ? 0.07 + Math.pow(clampedLevel, 1.15) * 0.88
      : 0.05 + clampedLevel * 0.42;
  return {
    dim: smoothPath(wavePoints(mode, depth * 0.55, phase, 0.85)),
    soft: smoothPath(wavePoints(mode, depth * 0.78, phase, 0.45)),
    main: smoothPath(wavePoints(mode, depth, phase, 0.1)),
  };
}
