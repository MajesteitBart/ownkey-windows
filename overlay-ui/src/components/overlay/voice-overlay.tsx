import { useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";

import { BrandWaveform, usePrefersReducedMotion } from "@/components/ui/brand-waveform";
import { BRAND, deriveMode, modePresentation, statusLabel } from "@/lib/overlay";
import type { OverlayState } from "@/types/overlay";

const EXIT_MS = 240;
const STATE_TRANSITION_MS = 140;

const LABEL_FONT = '"JetBrains Mono", "Cascadia Mono", Consolas, monospace';

interface PillPresentation {
  key: string;
  label: string;
  labelColor: string;
  barTint: string | null;
}

interface VoiceOverlayProps {
  state: OverlayState;
}

export function VoiceOverlay({ state }: VoiceOverlayProps) {
  const level = Math.max(0, Math.min(1, Number.isFinite(state.level) ? state.level : 0));
  const mode = deriveMode(state, level);
  const pres = modePresentation(mode);
  const label = statusLabel(state, mode);
  const presentation = useMemo<PillPresentation>(
    () => ({
      key: `${label}:${pres.label}:${pres.bar ?? "voice"}`,
      label,
      labelColor: pres.label,
      barTint: pres.bar,
    }),
    [label, pres.bar, pres.label],
  );
  const previousPresentationRef = useRef(presentation);
  const [outgoingPresentation, setOutgoingPresentation] =
    useState<PillPresentation | null>(null);

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

  // The pill hugs its content; animate the label slot's width so state
  // changes ("Listening" -> "Transcribing") resize the pill fluidly instead
  // of snapping. The hidden measurer span renders the incoming label
  // off-screen so the target width is known before the transition starts.
  const reducedMotion = usePrefersReducedMotion();
  const measureRef = useRef<HTMLSpanElement | null>(null);
  const [labelWidth, setLabelWidth] = useState<number | null>(null);
  // The label font loads lazily on first use, so the first pill can be
  // measured with fallback-font metrics and end up a few pixels short.
  // Preload the exact face and re-measure whenever any font finishes loading.
  const [fontEpoch, setFontEpoch] = useState(0);
  useEffect(() => {
    let disposed = false;
    const bump = () => {
      if (!disposed) setFontEpoch((n) => n + 1);
    };
    document.fonts?.load('500 11px "JetBrains Mono"').then(bump).catch(() => {});
    document.fonts?.addEventListener?.("loadingdone", bump);
    return () => {
      disposed = true;
      document.fonts?.removeEventListener?.("loadingdone", bump);
    };
  }, []);
  useLayoutEffect(() => {
    const el = measureRef.current;
    if (el) {
      // offsetWidth rounds to whole pixels, which can round *down* and shave
      // a fraction off the slot — enough to trigger the ellipsis. Measure
      // fractionally, round up, and keep a 1px cushion.
      setLabelWidth(Math.min(210, Math.ceil(el.getBoundingClientRect().width) + 1));
    }
  }, [presentation.label, fontEpoch]);

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

  const labelStyle = {
    fontFamily: LABEL_FONT,
    fontSize: 11,
    fontWeight: 500,
    letterSpacing: "0.14em",
    textTransform: "uppercase",
    whiteSpace: "nowrap",
    maxWidth: 210,
    overflow: "hidden",
    textOverflow: "ellipsis",
  } as const;

  return (
    <>
      {/* Label measurer. Lives outside the animated subtree: the pill's
          entry animation scales the overlay, and a transformed ancestor
          would shrink getBoundingClientRect and truncate the first label. */}
      <span
        aria-hidden="true"
        ref={measureRef}
        className="pointer-events-none invisible fixed left-0 top-0"
        style={{ ...labelStyle, maxWidth: "none" }}
      >
        {presentation.label}
      </span>
      <div
        className="pointer-events-none select-none transition-[opacity,transform] duration-200 ease-out [animation:ownkey-overlay-in_0.22s_ease-out]"
        style={{
          opacity: state.visible ? 1 : 0,
          transform: state.visible ? "scale(1)" : "scale(0.96) translateY(5px)",
        }}
      >
        <div
          className="inline-flex items-center gap-[14px] rounded-full border px-5 py-3"
          style={{
            background: BRAND.key,
            borderColor: BRAND.hairline,
            boxShadow:
              "0 24px 50px -12px rgba(0,0,0,0.9), 0 0 40px -18px rgba(222,95,20,0.55)",
          }}
        >
          <BrandWaveform mode={mode} level={level} tint={presentation.barTint} />
          <span
            className="relative inline-flex items-center overflow-hidden"
            style={{
              width: labelWidth ?? "auto",
              transition: reducedMotion ? "none" : "width 160ms cubic-bezier(0, 0, 0.2, 1)",
            }}
          >
            <span
              key={presentation.key}
              role="status"
              className={outgoingPresentation ? "ownkey-state-in" : undefined}
              style={{ ...labelStyle, color: presentation.labelColor }}
            >
              {presentation.label}
            </span>
            {outgoingPresentation ? (
              <span
                aria-hidden="true"
                className="ownkey-state-out absolute left-0 top-1/2 -translate-y-1/2"
                style={{ ...labelStyle, color: outgoingPresentation.labelColor }}
              >
                {outgoingPresentation.label}
              </span>
            ) : null}
          </span>
        </div>
      </div>
    </>
  );
}
