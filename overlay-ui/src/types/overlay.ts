export type ConnectionState = "checking" | "online" | "offline";
export type ListeningState = "ready" | "arming" | "listening" | "error";
export type ProcessingState = "idle" | "processing" | "done" | "error";
export type TargetState = "unknown" | "selected" | "not_selected";
export type ActivityState = "dictate" | "rewrite";

export interface OverlayState {
  connection: ConnectionState;
  listening: ListeningState;
  processing: ProcessingState;
  target: TargetState;
  level: number;
  visible: boolean;
  message?: string | null;
  activity?: ActivityState;
}

export const defaultOverlayState: OverlayState = {
  connection: "checking",
  listening: "ready",
  processing: "idle",
  target: "unknown",
  level: 0,
  visible: false,
  message: null,
  activity: "dictate",
};
