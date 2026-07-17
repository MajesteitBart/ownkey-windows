import { useCallback, useEffect, useMemo, useState } from "react";
import { listen } from "@tauri-apps/api/event";
import { invoke } from "@tauri-apps/api/core";

import { DevToolbar, type DevBackground } from "@/components/overlay/dev-toolbar";
import { VoiceOverlay } from "@/components/overlay/voice-overlay";
import { defaultOverlayState, type OverlayState } from "@/types/overlay";

const DEV_MODE = import.meta.env.DEV;
const IS_TAURI_RUNTIME = typeof window !== "undefined" && "__TAURI_INTERNALS__" in window;

function isOverlayState(value: unknown): value is OverlayState {
  if (!value || typeof value !== "object") return false;
  const state = value as Partial<OverlayState>;
  return (
    typeof state.connection === "string" &&
    typeof state.listening === "string" &&
    typeof state.processing === "string" &&
    typeof state.target === "string" &&
    typeof state.visible === "boolean"
  );
}

// Design-mode backdrops to verify overlay readability on any desktop.
const DEV_BACKGROUNDS: Record<DevBackground, string> = {
  light: "bg-[radial-gradient(circle_at_top,#ffffff,#eef1f5_55%,#dde3ea)]",
  dark: "bg-[radial-gradient(circle_at_top,#1a1d26,#0b0d12_60%,#06070a)]",
  image:
    "bg-[linear-gradient(135deg,#f6d365_0%,#fda085_30%,#c471ed_65%,#4a90d9_100%)]",
};

export default function App() {
  const [state, setState] = useState<OverlayState>(defaultOverlayState);
  const [background, setBackground] = useState<DevBackground>("light");
  const showDevToolbar = DEV_MODE && !IS_TAURI_RUNTIME;

  useEffect(() => {
    let disposed = false;
    invoke<OverlayState>("get_overlay_state")
      .then((current) => {
        if (!disposed && isOverlayState(current)) {
          setState(current);
        }
      })
      .catch(() => {
        // Keep default state in design mode when backend is unavailable.
      });

    let unlisten: (() => void) | undefined;
    listen<OverlayState>("overlay://state", (event) => {
      if (isOverlayState(event.payload)) {
        setState(event.payload);
      }
    })
      .then((dispose) => {
        unlisten = dispose;
    })
      .catch(() => {
        // In browser-only mode there is no Tauri event bridge.
      });

    const poll = window.setInterval(() => {
      invoke<OverlayState>("get_overlay_state")
        .then((current) => {
          if (!disposed && isOverlayState(current)) {
            setState(current);
          }
        })
        .catch(() => {
          // Browser-only mode fallback.
        });
    }, 110);

    return () => {
      disposed = true;
      clearInterval(poll);
      if (unlisten) {
        unlisten();
      }
    };
  }, []);

  const applyState = useCallback((next: OverlayState) => {
    setState(next);
    invoke("set_overlay_state", { next }).catch(() => {
      // Browser-only mode fallback.
    });
  }, []);

  const rootClassName = useMemo(() => {
    if (showDevToolbar) {
      return `relative flex h-full w-full items-center justify-center ${DEV_BACKGROUNDS[background]}`;
    }
    return "relative flex h-full w-full items-center justify-center bg-transparent";
  }, [showDevToolbar, background]);

  return (
    <main className={rootClassName}>
      <VoiceOverlay state={state} />
      {showDevToolbar ? <DevToolbar onSetState={applyState} onSetBackground={setBackground} /> : null}
    </main>
  );
}
