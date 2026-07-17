import { useEffect, useId, useRef, useState, type HTMLAttributes } from "react";

import { buildWaves, type OverlayMode, type OverlayPalette } from "@/lib/overlay";
import { cn } from "@/lib/utils";

interface NeonWaveformProps extends HTMLAttributes<HTMLDivElement> {
  mode: OverlayMode;
  palette: OverlayPalette;
  level: number;
}

const VIEWBOX_WIDTH = 160;
const VIEWBOX_HEIGHT = 84;

function clamp01(value: number): number {
  if (!Number.isFinite(value)) return 0;
  if (value <= 0) return 0;
  if (value >= 1) return 1;
  return value;
}

function mapWaveLevel(mode: OverlayMode, level: number): number {
  const safeLevel = clamp01(level);
  if (mode === "listening_audio") {
    return 0.06 + Math.pow(safeLevel, 1.2) * 0.82;
  }
  if (mode === "processing") {
    return 0.24;
  }
  if (mode === "loading") {
    return 0.18;
  }
  if (mode === "listening_wait") {
    return 0.12;
  }
  if (mode === "done") {
    return 0.16;
  }
  if (mode === "warning") {
    return 0.14;
  }
  if (mode === "error") {
    return 0.13;
  }
  return 0.08;
}

function phaseSpeed(mode: OverlayMode, level: number): number {
  if (mode === "listening_audio") {
    // Wave motion picks up with the voice so loud passages feel energetic.
    return 0.004 + clamp01(level) * 0.0055;
  }
  if (mode === "processing") return 0.0042;
  if (mode === "loading") return 0.0036;
  if (mode === "listening_wait") return 0.0024;
  return 0.0018;
}

function MirroredPath({
  d,
  color,
  strokeWidth,
  opacity,
  glowFilter,
}: {
  d: string;
  color: string;
  strokeWidth: number;
  opacity: number;
  glowFilter?: string;
}) {
  return (
    <>
      <path
        d={d}
        fill="none"
        stroke={color}
        strokeLinecap="round"
        strokeLinejoin="round"
        strokeWidth={strokeWidth}
        opacity={opacity}
        filter={glowFilter}
      />
      <path
        d={d}
        fill="none"
        stroke={color}
        strokeLinecap="round"
        strokeLinejoin="round"
        strokeWidth={strokeWidth}
        opacity={opacity}
        filter={glowFilter}
        transform={`translate(0 ${VIEWBOX_HEIGHT}) scale(1 -1)`}
      />
    </>
  );
}

export function NeonWaveform({
  mode,
  palette,
  level,
  className,
  ...props
}: NeonWaveformProps) {
  const [anim, setAnim] = useState({ phase: 0, displayLevel: 0 });
  const glowId = useId().replace(/:/g, "");
  const levelRef = useRef(0);
  const surfaceLevel = mapWaveLevel(mode, anim.displayLevel);
  const waves = buildWaves(mode, surfaceLevel, anim.phase);

  useEffect(() => {
    levelRef.current = clamp01(level);
  }, [level]);

  useEffect(() => {
    let raf = 0;
    let lastTime = 0;

    const animate = (time: number) => {
      const delta = Math.min(40, time - lastTime || 16);
      lastTime = time;
      setAnim(({ phase, displayLevel }) => {
        // Frame-rate envelope follower: fast attack so syllables pop, slow
        // release so the wave settles fluidly between level pushes.
        const target = levelRef.current;
        const tau = target > displayLevel ? 45 : 260;
        const nextLevel = displayLevel + (target - displayLevel) * (1 - Math.exp(-delta / tau));
        return {
          phase: phase + delta * phaseSpeed(mode, nextLevel),
          displayLevel: nextLevel,
        };
      });
      raf = requestAnimationFrame(animate);
    };

    raf = requestAnimationFrame(animate);
    return () => cancelAnimationFrame(raf);
  }, [mode]);

  const centerLineOpacity =
    mode === "listening_audio" ? 0.22 : mode === "processing" || mode === "loading" ? 0.18 : 0.12;

  return (
    <div
      className={cn("relative h-full w-full", className)}
      aria-label="Voice waveform"
      role="img"
      {...props}
    >
      <div
        className="absolute inset-x-6 top-1/2 h-[1px] -translate-y-1/2 rounded-full"
        style={{
          background: `linear-gradient(90deg, transparent 0%, ${palette.dim} 18%, ${palette.main} 50%, ${palette.dim} 82%, transparent 100%)`,
          opacity: centerLineOpacity,
          boxShadow: `0 0 12px ${palette.main}55`,
        }}
      />

      <div
        className="absolute inset-x-8 inset-y-4 rounded-full blur-2xl"
        style={{
          background: `radial-gradient(circle at 50% 50%, ${palette.soft}22 0%, ${palette.main}12 34%, transparent 72%)`,
        }}
      />

      <svg
        viewBox={`0 0 ${VIEWBOX_WIDTH} ${VIEWBOX_HEIGHT}`}
        className="absolute inset-0 h-full w-full overflow-visible"
        aria-hidden="true"
      >
        <defs>
          <filter id={`${glowId}-bloom`} x="-20%" y="-40%" width="140%" height="180%">
            <feGaussianBlur stdDeviation="4.2" result="blur" />
            <feColorMatrix
              in="blur"
              type="matrix"
              values="1 0 0 0 0
                      0 1 0 0 0
                      0 0 1 0 0
                      0 0 0 1.4 0"
            />
          </filter>
          <filter id={`${glowId}-soft`} x="-20%" y="-40%" width="140%" height="180%">
            <feGaussianBlur stdDeviation="2.4" />
          </filter>
        </defs>

        <MirroredPath
          d={waves.dim}
          color={palette.dim}
          strokeWidth={18}
          opacity={0.16}
          glowFilter={`url(#${glowId}-bloom)`}
        />
        <MirroredPath
          d={waves.soft}
          color={palette.soft}
          strokeWidth={11}
          opacity={0.28}
          glowFilter={`url(#${glowId}-soft)`}
        />
        <MirroredPath d={waves.main} color={palette.main} strokeWidth={4.8} opacity={0.92} />

        <circle cx="9" cy={VIEWBOX_HEIGHT / 2} r="1.7" fill={palette.main} opacity="0.52" />
        <circle cx={VIEWBOX_WIDTH - 9} cy={VIEWBOX_HEIGHT / 2} r="1.7" fill={palette.main} opacity="0.52" />
      </svg>
    </div>
  );
}
