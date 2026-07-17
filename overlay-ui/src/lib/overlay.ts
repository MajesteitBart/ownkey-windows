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

// Brand tokens (docs/OVERLAY_DESIGN_SPEC.md).
export const BRAND = {
  key: "#0E0E0E",
  slate: "#202020",
  hairline: "#3A3A3A",
  bone: "#F3F1EC",
  ash: "#8E8A7F",
  orange: "#DE5F14",
  amber: "#F4A23C",
  green: "#9DCB3B",
  red: "#E2574B",
} as const;

export interface ModePresentation {
  // Flat bar tint; null keeps the amber→orange voice gradient.
  bar: string | null;
  label: string;
}

const LISTENING_AUDIO_THRESHOLD = 0.05;

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

// Orange is the one accent that means voice/AI; rewrite is distinguished by
// its label, not a color. Non-orange tints are flat fills — the gradient is
// reserved for the voice state.
export function modePresentation(mode: OverlayMode): ModePresentation {
  if (mode === "error") return { bar: BRAND.red, label: BRAND.red };
  if (mode === "warning") return { bar: BRAND.amber, label: BRAND.bone };
  if (mode === "done") return { bar: BRAND.green, label: BRAND.ash };
  return { bar: null, label: BRAND.ash };
}
