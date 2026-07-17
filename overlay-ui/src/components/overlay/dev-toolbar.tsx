import type { OverlayState } from "@/types/overlay";
import { Button } from "@/components/ui/button";

export type DevBackground = "light" | "dark" | "image";

interface DevToolbarProps {
  onSetState: (next: OverlayState) => void;
  onSetBackground: (background: DevBackground) => void;
}

const states: Array<{ label: string; value: OverlayState }> = [
  {
    label: "Listening (quiet)",
    value: {
      connection: "online",
      listening: "listening",
      processing: "idle",
      target: "selected",
      level: 0.03,
      visible: true,
      message: "Listening...",
      activity: "dictate",
    },
  },
  {
    label: "Listening (audio)",
    value: {
      connection: "online",
      listening: "listening",
      processing: "idle",
      target: "selected",
      level: 0.74,
      visible: true,
      message: null,
      activity: "dictate",
    },
  },
  {
    label: "Loading (arming)",
    value: {
      connection: "online",
      listening: "arming",
      processing: "idle",
      target: "selected",
      level: 0,
      visible: true,
      message: "Starting...",
      activity: "dictate",
    },
  },
  {
    label: "Transcribing",
    value: {
      connection: "online",
      listening: "ready",
      processing: "processing",
      target: "selected",
      level: 0,
      visible: true,
      message: "Transcribing...",
      activity: "dictate",
    },
  },
  {
    label: "Rewrite (listening)",
    value: {
      connection: "online",
      listening: "listening",
      processing: "idle",
      target: "selected",
      level: 0.6,
      visible: true,
      message: "Speak an edit...",
      activity: "rewrite",
    },
  },
  {
    label: "Rewriting",
    value: {
      connection: "online",
      listening: "ready",
      processing: "processing",
      target: "selected",
      level: 0,
      visible: true,
      message: "Rewriting...",
      activity: "rewrite",
    },
  },
  {
    label: "Done",
    value: {
      connection: "online",
      listening: "ready",
      processing: "done",
      target: "selected",
      level: 0,
      visible: true,
      message: null,
      activity: "dictate",
    },
  },
  {
    label: "No target",
    value: {
      connection: "online",
      listening: "ready",
      processing: "idle",
      target: "not_selected",
      level: 0,
      visible: true,
      message: null,
      activity: "dictate",
    },
  },
  {
    label: "Ready (hidden)",
    value: {
      connection: "online",
      listening: "ready",
      processing: "idle",
      target: "selected",
      level: 0,
      visible: false,
      message: null,
      activity: "dictate",
    },
  },
];

const backgrounds: DevBackground[] = ["light", "dark", "image"];

export function DevToolbar({ onSetState, onSetBackground }: DevToolbarProps) {
  return (
    <div className="pointer-events-auto absolute bottom-3 left-3 right-3 z-10 flex flex-wrap gap-2 rounded-lg border border-border/60 bg-card/85 p-2 shadow-lg backdrop-blur-sm">
      {states.map((item) => (
        <Button key={item.label} size="sm" variant="secondary" onClick={() => onSetState(item.value)}>
          {item.label}
        </Button>
      ))}
      <span className="mx-1 self-center text-xs text-muted-foreground">Background:</span>
      {backgrounds.map((background) => (
        <Button
          key={background}
          size="sm"
          variant="default"
          onClick={() => onSetBackground(background)}
        >
          {background}
        </Button>
      ))}
    </div>
  );
}
