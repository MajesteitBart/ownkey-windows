import { useEffect, useMemo, useRef, useState, type HTMLAttributes } from "react";

import type { OverlayMode, OverlayPalette, ParticleVariant } from "@/lib/overlay";
import { cn } from "@/lib/utils";

// Animation model adapted from LiveKit Agents UI's AgentAudioVisualizerGrid
// (Apache-2.0). Chase states highlight a single dot along a precomputed path,
// with the comet trail coming from asymmetric CSS transitions (fast highlight,
// ~9x slower fade). Band states light whole columns from the middle row
// outward, driven by the live voice level.

interface DotGridVisualizerProps extends HTMLAttributes<HTMLDivElement> {
  mode: OverlayMode;
  variant: ParticleVariant;
  palette: OverlayPalette;
  level: number;
}

const COLS = 31;
const ROWS = 9;
const DOT_PX = 1.875;
const COLUMN_GAP_PX = 5.25;
const ROW_GAP_PX = 2.4;
const STEP_MS = 50;

function clamp01(value: number): number {
  if (!Number.isFinite(value)) return 0;
  if (value <= 0) return 0;
  if (value >= 1) return 1;
  return value;
}

function gaussian(value: number, center: number, spread: number): number {
  return Math.exp(-(((value - center) / spread) ** 2));
}

function smoothstep(edge0: number, edge1: number, value: number): number {
  const width = Math.max(0.0001, edge1 - edge0);
  const t = clamp01((value - edge0) / width);
  return t * t * (3 - 2 * t);
}

function alphaHex(value: number): string {
  return Math.round(clamp01(value) * 255)
    .toString(16)
    .padStart(2, "0");
}

function mixHex(base: string, tint: string, amount: number): string {
  const a = [1, 3, 5].map((i) => parseInt(base.slice(i, i + 2), 16));
  const b = [1, 3, 5].map((i) => parseInt(tint.slice(i, i + 2), 16));
  const mixed = a.map((value, i) => Math.round(value + (b[i] - value) * amount));
  return `#${mixed.map((value) => value.toString(16).padStart(2, "0")).join("")}`;
}

function cellIntensity(
  mode: OverlayMode,
  variant: ParticleVariant,
  level: number,
  x: number,
  y: number,
  step: number,
  history: number[],
): number {
  const u = x / (COLS - 1);
  const v = y / (ROWS - 1);
  const t = (step * STEP_MS) / 1000;
  const edgeFade = 0.38 + 0.62 * Math.sin(Math.PI * u) ** 0.42;
  let energy = 0;

  if (variant === "listening") {
    // A scrolling waveform built from recent normalized dBFS samples. Each
    // column is one moment in time and lights symmetrically from the middle.
    const sample = clamp01(history[x] ?? level);
    const motion =
      0.08 +
      0.56 * Math.abs(Math.sin(x * 0.55 - t * 2.1)) +
      0.36 * Math.abs(Math.sin(x * 0.23 + t * 1.3));
    const amplitude = clamp01(sample * Math.pow(motion, 1.45));
    const distanceFromCenter = Math.abs(v - 0.5) * 2;
    const waveform = 1 - smoothstep(amplitude, amplitude + 0.22, distanceFromCenter);
    const strength = 0.28 + sample * 0.72;
    energy = waveform * strength * edgeFade;
  } else if (variant === "transcribing") {
    const center = 0.5 + 0.018 * Math.sin(u * Math.PI * 3.2 - t * 1.1);
    const band = gaussian(v, center, 0.13);
    const shimmer = 0.84 + 0.16 * Math.sin(t * 3.1 + x * 0.64);
    energy = band * edgeFade * shimmer;
  } else if (variant === "edit") {
    const center =
      0.52 + 0.095 * Math.sin(u * Math.PI * 3.5 - t * 1.15) + 0.035 * Math.sin(u * 17 + t);
    const band = gaussian(v, center, 0.19);
    const shimmer = 0.82 + 0.18 * Math.sin(t * 3.6 + x * 0.81);
    energy = band * edgeFade * shimmer;
  } else if (variant === "rewriting") {
    const center =
      0.5 -
      0.075 * gaussian(u, 0.3 + 0.025 * Math.sin(t), 0.16) +
      0.08 * gaussian(u, 0.72 + 0.02 * Math.cos(t * 0.8), 0.18);
    const band = gaussian(v, center, 0.18);
    const shimmer = 0.84 + 0.16 * Math.sin(t * 3 + x * 0.57 + y * 0.3);
    energy = band * edgeFade * shimmer;
  } else if (variant === "done") {
    const band = gaussian(v, 0.5, 0.23);
    const body = 0.62 + 0.38 * gaussian(u, 0.5, 0.42);
    energy = band * body * (0.94 + 0.06 * Math.sin(t * 1.4 + x * 0.32));
  } else if (variant === "loading") {
    const perimeter = 2 * (COLS + ROWS) - 4;
    const cursor = step % perimeter;
    let index: number;
    if (y === 0) index = x;
    else if (x === COLS - 1) index = COLS - 1 + y;
    else if (y === ROWS - 1) index = COLS - 1 + ROWS - 1 + (COLS - 1 - x);
    else if (x === 0) index = 2 * (COLS - 1) + ROWS - 1 + (ROWS - 1 - y);
    else index = -100;
    const distance = Math.min(
      Math.abs(index - cursor),
      perimeter - Math.abs(index - cursor),
    );
    energy = index >= 0 ? Math.max(0, 1 - distance / 5) : 0;
  } else {
    const pulse = 0.62 + 0.38 * Math.sin(t * (mode === "error" ? 4.2 : 1.6));
    energy = gaussian(u, 0.5, 0.16) * gaussian(v, 0.5, 0.24) * pulse;
  }

  return clamp01(energy);
}

export function DotGridVisualizer({
  mode,
  variant,
  palette,
  level,
  className,
  ...props
}: DotGridVisualizerProps) {
  const [tick, setTick] = useState({
    step: 0,
    displayLevel: 0,
    history: Array.from({ length: COLS }, () => 0),
  });
  const levelRef = useRef(0);
  const cells = useMemo(() => Array.from({ length: COLS * ROWS }, (_, index) => index), []);

  useEffect(() => {
    levelRef.current = clamp01(level);
  }, [level]);

  useEffect(() => {
    const id = setInterval(() => {
      setTick(({ step, displayLevel, history }) => {
        // Fast attack, slower release so the blob pulses with syllables.
        const target = levelRef.current;
        const blend = target > displayLevel ? 0.6 : 0.25;
        const nextLevel = displayLevel + (target - displayLevel) * blend;
        return {
          step: step + 1,
          displayLevel: nextLevel,
          history: [...history.slice(1), nextLevel],
        };
      });
    }, STEP_MS);
    return () => clearInterval(id);
  }, []);

  return (
    <div
      className={cn("relative flex h-full w-full items-center justify-center", className)}
      aria-label="Voice status"
      role="img"
      {...props}
    >
      <div
        className="absolute inset-x-8 inset-y-1"
        style={{
          background: `radial-gradient(ellipse at 50% 50%, ${palette.soft}12 0%, ${palette.main}09 48%, transparent 78%)`,
        }}
      />

      <div
        className="relative grid"
        style={{
          gridTemplateColumns: `repeat(${COLS}, ${DOT_PX}px)`,
          columnGap: `${COLUMN_GAP_PX}px`,
          rowGap: `${ROW_GAP_PX}px`,
        }}
      >
        {cells.map((index) => {
          const x = index % COLS;
          const y = Math.floor(index / COLS);
          const intensity = cellIntensity(
            mode,
            variant,
            tick.displayLevel,
            x,
            y,
            tick.step,
            tick.history,
          );
          const opacity = 0.08 + intensity * 0.92;
          return (
            <div
              key={index}
              className="rounded-full ease-out"
              style={{
                width: DOT_PX,
                height: DOT_PX,
                backgroundColor: mixHex(palette.main, "#ffffff", intensity * 0.65),
                opacity,
                boxShadow:
                  intensity > 0.28
                    ? `0 0 ${1.5 + intensity * 6}px ${palette.main}${alphaHex(0.34 + intensity * 0.58)}`
                    : "none",
                transform: `scale(${0.86 + intensity * 0.38})`,
                transitionProperty: "opacity, box-shadow, transform",
                transitionDuration: `${STEP_MS * 1.6}ms`,
              }}
            />
          );
        })}
      </div>
    </div>
  );
}
