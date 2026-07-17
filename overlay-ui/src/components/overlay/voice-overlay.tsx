import { useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";

import { DotGridVisualizer } from "@/components/ui/dot-grid-visualizer";
import { activityPalette, deriveMode, deriveVariant, statusLabel } from "@/lib/overlay";
import type { OverlayState } from "@/types/overlay";

const EXIT_MS = 240;
const STATE_TRANSITION_MS = 140;

interface OverlayPresentation {
  key: string;
  label: string;
  labelColor: string;
  palette: {
    main: string;
    soft: string;
    dim: string;
  };
}

interface VoiceOverlayProps {
  state: OverlayState;
}

function mixHex(base: string, tint: string, amount: number): string {
  const a = [1, 3, 5].map((i) => parseInt(base.slice(i, i + 2), 16));
  const b = [1, 3, 5].map((i) => parseInt(tint.slice(i, i + 2), 16));
  const mixed = a.map((v, i) => Math.round(v + (b[i] - v) * amount));
  return `#${mixed.map((v) => v.toString(16).padStart(2, "0")).join("")}`;
}

export function VoiceOverlay({ state }: VoiceOverlayProps) {
  const level = Math.max(0, Math.min(1, Number.isFinite(state.level) ? state.level : 0));
  const mode = deriveMode(state, level);
  const palette = activityPalette(mode, state);
  const variant = deriveVariant(mode, state);
  const label = statusLabel(state, mode);
  const labelColor = mixHex("#f4efff", palette.soft, 0.46);
  const presentation = useMemo<OverlayPresentation>(
    () => ({
      key: `${variant}:${label}:${palette.main}`,
      label,
      labelColor,
      palette: {
        main: palette.main,
        soft: palette.soft,
        dim: palette.dim,
      },
    }),
    [label, labelColor, palette.dim, palette.main, palette.soft, variant],
  );
  const previousPresentationRef = useRef(presentation);
  const [outgoingPresentation, setOutgoingPresentation] =
    useState<OverlayPresentation | null>(null);

  useLayoutEffect(() => {
    const previous = previousPresentationRef.current;
    if (previous.key === presentation.key) {
      previousPresentationRef.current = presentation;
      return;
    }

    previousPresentationRef.current = presentation;
    setOutgoingPresentation(previous);
    const timer = window.setTimeout(() => setOutgoingPresentation(null), STATE_TRANSITION_MS);
    return () => window.clearTimeout(timer);
  }, [presentation]);

  // Keep rendering briefly after visible flips off so the exit fade can play.
  const [mounted, setMounted] = useState(state.visible);
  if (state.visible && !mounted) {
    setMounted(true);
  }
  useEffect(() => {
    if (state.visible) return;
    const timer = window.setTimeout(() => setMounted(false), EXIT_MS);
    return () => window.clearTimeout(timer);
  }, [state.visible]);

  if (!state.visible && !mounted) return null;

  return (
    <div
      className="pointer-events-none relative select-none transition-[opacity,transform] duration-200 ease-out [animation:ownkey-overlay-in_0.22s_ease-out]"
      style={{
        width: 293,
        height: 117,
        opacity: state.visible ? 1 : 0,
        transform: state.visible ? "scale(1)" : "scale(0.96) translateY(5px)",
      }}
    >
      <div
        className="absolute inset-[15px] z-10 rounded-[15px]"
        style={{
          boxShadow: `0 0 8px ${palette.main}24, 0 3px 9px rgba(0,0,0,0.34)`,
        }}
      >
        <div
          className="absolute inset-0 overflow-hidden rounded-[15px]"
          style={{
            background:
              "linear-gradient(160deg, rgba(9,10,24,0.98) 0%, rgba(4,7,17,0.98) 72%, rgba(2,5,13,0.99) 100%)",
          }}
        >
          <div
            key={presentation.key}
            className={`absolute inset-0 ${outgoingPresentation ? "ownkey-state-in" : ""}`}
            style={{
              background: `radial-gradient(120% 90% at 50% 5%, ${presentation.palette.main}1c 0%, transparent 62%)`,
            }}
          />
          {outgoingPresentation ? (
            <div
              aria-hidden="true"
              className="ownkey-state-out absolute inset-0"
              style={{
                background: `radial-gradient(120% 90% at 50% 5%, ${outgoingPresentation.palette.main}1c 0%, transparent 62%)`,
              }}
            />
          ) : null}
          <div
            className="absolute inset-0"
            style={{
              background:
                "radial-gradient(173px 50px at 9% -8%, rgba(255,255,255,0.07), transparent 72%)",
            }}
          />
          <div className="absolute inset-x-[42px] top-0 h-px bg-gradient-to-r from-transparent via-white/30 to-transparent" />

          <div className="absolute left-[18px] right-[18px] top-[8px] h-[44px]">
            <DotGridVisualizer
              mode={mode}
              variant={variant}
              palette={palette}
              level={level}
            />
          </div>

          <span
            key={presentation.key}
            role="status"
            className={`absolute bottom-[10px] left-[18px] whitespace-nowrap text-[12px] font-normal tracking-[0.01em] ${outgoingPresentation ? "ownkey-state-in" : ""}`}
            style={{
              color: presentation.labelColor,
              fontFamily:
                '"Segoe UI Variable Display", "Segoe UI", ui-sans-serif, sans-serif',
              textShadow: `0 0 11px ${presentation.palette.main}55, 0 1px 8px rgba(0,0,0,0.75)`,
            }}
          >
            {presentation.label}
          </span>
          {outgoingPresentation ? (
            <span
              aria-hidden="true"
              className="ownkey-state-out absolute bottom-[10px] left-[18px] whitespace-nowrap text-[12px] font-normal tracking-[0.01em]"
              style={{
                color: outgoingPresentation.labelColor,
                fontFamily:
                  '"Segoe UI Variable Display", "Segoe UI", ui-sans-serif, sans-serif',
                textShadow: `0 0 11px ${outgoingPresentation.palette.main}55, 0 1px 8px rgba(0,0,0,0.75)`,
              }}
            >
              {outgoingPresentation.label}
            </span>
          ) : null}
        </div>

        <div
          key={presentation.key}
          className={`absolute inset-0 rounded-[15px] ${outgoingPresentation ? "ownkey-state-in" : ""}`}
          style={{
            padding: "1px",
            background: `linear-gradient(112deg, ${presentation.palette.soft}dd 0%, ${presentation.palette.main}a8 22%, ${presentation.palette.dim}88 62%, ${presentation.palette.main}e8 100%)`,
            WebkitMask: "linear-gradient(#fff 0 0) content-box, linear-gradient(#fff 0 0)",
            WebkitMaskComposite: "xor",
            maskComposite: "exclude",
          }}
        />
        {outgoingPresentation ? (
          <div
            aria-hidden="true"
            className="ownkey-state-out absolute inset-0 rounded-[15px]"
            style={{
              padding: "1px",
              background: `linear-gradient(112deg, ${outgoingPresentation.palette.soft}dd 0%, ${outgoingPresentation.palette.main}a8 22%, ${outgoingPresentation.palette.dim}88 62%, ${outgoingPresentation.palette.main}e8 100%)`,
              WebkitMask: "linear-gradient(#fff 0 0) content-box, linear-gradient(#fff 0 0)",
              WebkitMaskComposite: "xor",
              maskComposite: "exclude",
            }}
          />
        ) : null}
      </div>
    </div>
  );
}
