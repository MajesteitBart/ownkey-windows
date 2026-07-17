import { useEffect, useRef, useState } from "react";

import { BRAND, type OverlayMode } from "@/lib/overlay";

// Rest heights trace the logo's waveform muzzle (docs/OVERLAY_DESIGN_SPEC.md).
const REST_HEIGHTS = [0.4, 0.75, 1, 0.65, 0.45];
// Per-bar phase offsets so level-driven bars never move in lockstep.
const BAR_PHASES = [0.0, 2.1, 4.4, 1.3, 3.2];
// Site bounce stagger, mirrored from the website's pill.
const BOUNCE_DELAYS = ["0.05s", "0.2s", "0s", "0.3s", "0.12s"];
const ATTACK_MS = 90;
const RELEASE_MS = 160;

const MOTION_CLASS: Partial<Record<OverlayMode, string>> = {
  loading: "ok-bar-ripple",
  listening_wait: "ok-bar-breathe",
  processing: "ok-bar-bounce",
  idle: "ok-bar-breathe",
};

interface BrandWaveformProps {
  mode: OverlayMode;
  level: number;
  tint: string | null;
}

function usePrefersReducedMotion(): boolean {
  const [reduced, setReduced] = useState(
    () => window.matchMedia("(prefers-reduced-motion: reduce)").matches,
  );
  useEffect(() => {
    const query = window.matchMedia("(prefers-reduced-motion: reduce)");
    const onChange = (event: MediaQueryListEvent) => setReduced(event.matches);
    query.addEventListener("change", onChange);
    return () => query.removeEventListener("change", onChange);
  }, []);
  return reduced;
}

export function BrandWaveform({ mode, level, tint }: BrandWaveformProps) {
  const barsRef = useRef<Array<HTMLSpanElement | null>>([]);
  const levelRef = useRef(level);
  levelRef.current = level;
  const reducedMotion = usePrefersReducedMotion();

  const audioDriven = mode === "listening_audio" && !reducedMotion;

  useEffect(() => {
    if (!audioDriven) {
      for (const bar of barsRef.current) {
        if (bar) bar.style.transform = "";
      }
      return;
    }

    let frame = 0;
    let last = performance.now();
    const scales = REST_HEIGHTS.map(() => 0.35);
    const tick = (now: number) => {
      const dt = Math.min(64, now - last);
      last = now;
      const eased = Math.pow(Math.max(0, Math.min(1, levelRef.current)), 0.8);
      const t = now / 1000;
      for (let i = 0; i < REST_HEIGHTS.length; i += 1) {
        const wobble = 1 + 0.14 * eased * Math.sin(t * 7.3 + BAR_PHASES[i]);
        const target = (0.35 + 0.65 * eased) * wobble;
        const tau = target > scales[i] ? ATTACK_MS : RELEASE_MS;
        scales[i] += (target - scales[i]) * Math.min(1, dt / tau);
        const bar = barsRef.current[i];
        if (bar) {
          const clamped = Math.max(0.2, Math.min(1.12, scales[i]));
          bar.style.transform = `scaleY(${clamped.toFixed(3)})`;
        }
      }
      frame = requestAnimationFrame(tick);
    };
    frame = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(frame);
  }, [audioDriven]);

  const background = tint ?? `linear-gradient(180deg, ${BRAND.amber}, ${BRAND.orange})`;
  const motionClass = reducedMotion || audioDriven ? undefined : MOTION_CLASS[mode];
  // Errors hold the bars visibly low; other static modes rest at full profile.
  const staticScale = mode === "error" ? 0.3 : undefined;

  return (
    <div aria-hidden="true" className="flex h-[22px] items-center gap-[3px]">
      {REST_HEIGHTS.map((rest, index) => (
        <span
          key={index}
          ref={(el) => {
            barsRef.current[index] = el;
          }}
          className={motionClass}
          style={{
            width: 4,
            height: `${rest * 100}%`,
            borderRadius: 3,
            background,
            transformOrigin: "center",
            transform: staticScale !== undefined ? `scaleY(${staticScale})` : undefined,
            animationDelay:
              motionClass === "ok-bar-ripple"
                ? `${index * 0.1}s`
                : motionClass === "ok-bar-breathe"
                  ? `${-BAR_PHASES[index] * 0.4}s`
                  : motionClass === "ok-bar-bounce"
                    ? BOUNCE_DELAYS[index]
                    : undefined,
          }}
        />
      ))}
    </div>
  );
}
