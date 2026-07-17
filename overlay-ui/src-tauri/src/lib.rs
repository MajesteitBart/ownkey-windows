use std::{
    net::UdpSocket,
    sync::{Arc, Mutex},
    thread,
    time::Duration,
};

use serde::{Deserialize, Serialize};
use tauri::{AppHandle, Emitter, Manager, PhysicalPosition, Position, State, WebviewWindow};

const UDP_ADDR: &str = "127.0.0.1:38485";
// The window includes ~44px of bottom padding for the pill's drop shadow,
// so the visible pill still floats the same distance above the taskbar.
const TASKBAR_MARGIN_PX: i32 = 40;
// Keep the OS window up long enough for the webview's exit fade to play.
const EXIT_FADE_MS: u64 = 280;

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
struct OverlayState {
    connection: String,
    listening: String,
    processing: String,
    target: String,
    level: f64,
    visible: bool,
    #[serde(default)]
    message: Option<String>,
    #[serde(default = "default_activity")]
    activity: String,
}

fn default_activity() -> String {
    "dictate".to_string()
}

impl Default for OverlayState {
    fn default() -> Self {
        Self {
            connection: "checking".to_string(),
            listening: "ready".to_string(),
            processing: "idle".to_string(),
            target: "unknown".to_string(),
            level: 0.0,
            visible: false,
            message: None,
            activity: default_activity(),
        }
    }
}

#[derive(Debug, Clone, Deserialize, Default)]
#[serde(default, rename_all = "snake_case")]
struct OverlayPatch {
    connection: Option<String>,
    listening: Option<String>,
    processing: Option<String>,
    target: Option<String>,
    level: Option<f64>,
    visible: Option<bool>,
    message: Option<String>,
    activity: Option<String>,
}

impl OverlayPatch {
    fn has_updates(&self) -> bool {
        self.connection.is_some()
            || self.listening.is_some()
            || self.processing.is_some()
            || self.target.is_some()
            || self.level.is_some()
            || self.visible.is_some()
            || self.message.is_some()
            || self.activity.is_some()
    }

    fn apply(self, state: &mut OverlayState) {
        if let Some(value) = self.connection {
            state.connection = value;
        }
        if let Some(value) = self.listening {
            state.listening = value;
        }
        if let Some(value) = self.processing {
            state.processing = value;
        }
        if let Some(value) = self.target {
            state.target = value;
        }
        if let Some(value) = self.level {
            state.level = value.clamp(0.0, 1.0);
        }
        if let Some(value) = self.visible {
            state.visible = value;
        }
        if let Some(value) = self.message {
            state.message = if value.trim().is_empty() {
                None
            } else {
                Some(value)
            };
        }
        if let Some(value) = self.activity {
            state.activity = value;
        }
    }
}

#[derive(Default)]
struct SharedOverlayState {
    current: Mutex<OverlayState>,
}

fn emit_overlay_state(app: &AppHandle, state: &OverlayState) {
    let _ = app.emit("overlay://state", state);
}

fn sync_overlay_window(app: &AppHandle, shared: &Arc<SharedOverlayState>, state: &OverlayState) {
    if state.visible {
        let app_handle = app.clone();
        let _ = app.run_on_main_thread(move || {
            let Some(window) = app_handle.get_webview_window("main") else {
                return;
            };
            if !window.is_visible().unwrap_or(false) {
                log::info!("restoring overlay window");
                let _ = position_overlay_window(&window);
                let _ = window.show();
            }
        });
        return;
    }

    // Delay the hide so the webview's exit fade can play; abort when the
    // overlay was re-shown in the meantime.
    let app_handle = app.clone();
    let shared = shared.clone();
    thread::spawn(move || {
        thread::sleep(Duration::from_millis(EXIT_FADE_MS));
        let still_hidden = shared
            .current
            .lock()
            .map(|state| !state.visible)
            .unwrap_or(false);
        if !still_hidden {
            return;
        }
        let handle = app_handle.clone();
        let _ = app_handle.run_on_main_thread(move || {
            let Some(window) = handle.get_webview_window("main") else {
                return;
            };
            if window.is_visible().unwrap_or(false) {
                let _ = window.hide();
            }
        });
    });
}

fn lock_state(shared: &Arc<SharedOverlayState>) -> Result<std::sync::MutexGuard<'_, OverlayState>, String> {
    shared
        .current
        .lock()
        .map_err(|_| "overlay state lock poisoned".to_string())
}

#[tauri::command]
fn get_overlay_state(shared: State<'_, Arc<SharedOverlayState>>) -> Result<OverlayState, String> {
    Ok(lock_state(shared.inner())?.clone())
}

#[tauri::command]
fn set_overlay_state(
    next: OverlayState,
    app: AppHandle,
    shared: State<'_, Arc<SharedOverlayState>>,
) -> Result<(), String> {
    let snapshot = {
        let mut state = lock_state(shared.inner())?;
        *state = OverlayState {
            level: next.level.clamp(0.0, 1.0),
            ..next
        };
        state.clone()
    };
    emit_overlay_state(&app, &snapshot);
    sync_overlay_window(&app, shared.inner(), &snapshot);
    Ok(())
}

fn start_udp_bridge(app: AppHandle, shared: Arc<SharedOverlayState>) {
    thread::spawn(move || {
        let socket = match UdpSocket::bind(UDP_ADDR) {
            Ok(socket) => socket,
            Err(error) => {
                log::error!("failed to bind UDP bridge at {}: {}", UDP_ADDR, error);
                return;
            }
        };
        let _ = socket.set_read_timeout(Some(Duration::from_millis(250)));
        log::info!("overlay UDP bridge listening on {}", UDP_ADDR);

        let mut buffer = [0_u8; 8192];
        loop {
            match socket.recv_from(&mut buffer) {
                Ok((count, _)) => {
                    let payload = match std::str::from_utf8(&buffer[..count]) {
                        Ok(text) => text,
                        Err(error) => {
                            log::warn!("invalid UTF-8 UDP payload: {}", error);
                            continue;
                        }
                    };
                    if let Ok(next) = serde_json::from_str::<OverlayState>(payload) {
                        if let Ok(snapshot) = lock_state(&shared).map(|mut state| {
                            *state = OverlayState {
                                level: next.level.clamp(0.0, 1.0),
                                ..next
                            };
                            state.clone()
                        }) {
                            emit_overlay_state(&app, &snapshot);
                            sync_overlay_window(&app, &shared, &snapshot);
                        }
                        continue;
                    }
                    if let Ok(patch) = serde_json::from_str::<OverlayPatch>(payload) {
                        if patch.has_updates() {
                            let should_sync_window = patch.visible.is_some();
                            if let Ok(snapshot) = lock_state(&shared).map(|mut state| {
                                patch.apply(&mut state);
                                state.clone()
                            }) {
                                emit_overlay_state(&app, &snapshot);
                                if should_sync_window {
                                    sync_overlay_window(&app, &shared, &snapshot);
                                }
                            }
                            continue;
                        }
                    }
                    log::warn!("ignored UDP payload (invalid JSON shape): {}", payload);
                }
                Err(error)
                    if error.kind() == std::io::ErrorKind::WouldBlock
                        || error.kind() == std::io::ErrorKind::TimedOut =>
                {
                    continue;
                }
                Err(error) => {
                    log::error!("overlay UDP bridge stopped: {}", error);
                    break;
                }
            }
        }
    });
}

fn position_overlay_window(window: &WebviewWindow) -> tauri::Result<()> {
    let monitor = match window.current_monitor()? {
        Some(current) => Some(current),
        None => window.primary_monitor()?,
    };
    if let Some(monitor) = monitor {
        let monitor_size = monitor.size();
        let monitor_pos = monitor.position();
        let window_size = window.outer_size()?;
        let margin = (TASKBAR_MARGIN_PX as f64 * monitor.scale_factor()) as i32;
        let x = monitor_pos.x + ((monitor_size.width as i32 - window_size.width as i32) / 2).max(0);
        let y = monitor_pos.y + (monitor_size.height as i32 - window_size.height as i32 - margin).max(0);
        window.set_position(Position::Physical(PhysicalPosition::new(x, y)))?;
    }
    Ok(())
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    let shared = Arc::new(SharedOverlayState::default());
    let state_for_setup = shared.clone();

    tauri::Builder::default()
        .manage(shared)
        .invoke_handler(tauri::generate_handler![get_overlay_state, set_overlay_state])
        .setup(move |app| {
            if cfg!(debug_assertions) {
                app.handle().plugin(
                    tauri_plugin_log::Builder::default()
                        .level(log::LevelFilter::Info)
                        .build(),
                )?;
            }

            if let Some(window) = app.get_webview_window("main") {
                let _ = window.set_ignore_cursor_events(true);
                let _ = window.set_focusable(false);
                let _ = position_overlay_window(&window);
            }

            if let Ok(initial) = lock_state(&state_for_setup) {
                let handle = app.handle().clone();
                emit_overlay_state(&handle, &initial);
                let snapshot = initial.clone();
                drop(initial);
                sync_overlay_window(&handle, &state_for_setup, &snapshot);
            }

            start_udp_bridge(app.handle().clone(), state_for_setup.clone());
            Ok(())
        })
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}
