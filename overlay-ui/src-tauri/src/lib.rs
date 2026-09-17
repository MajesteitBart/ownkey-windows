use std::{
    net::UdpSocket,
    sync::{mpsc, Arc, Mutex},
    thread,
    time::Duration,
};

use serde::{Deserialize, Serialize};
use tauri::{
    AppHandle, Emitter, Manager, PhysicalPosition, Position, State, Url, WebviewUrl, WebviewWindow,
    WebviewWindowBuilder,
};

const DEFAULT_UDP_ADDR: &str = "127.0.0.1:38485";
const MEETINGS_WINDOW: &str = "meetings";

// A development copy can run next to an installed Ownkey on another port.
fn udp_addr() -> String {
    std::env::var("OWNKEY_OVERLAY_UDP")
        .ok()
        .map(|value| value.trim().to_string())
        .filter(|value| !value.is_empty())
        .unwrap_or_else(|| DEFAULT_UDP_ADDR.to_string())
}
// The window includes ~44px of bottom padding for the pill's drop shadow,
// so the visible pill still floats the same distance above the taskbar.
const TASKBAR_MARGIN_PX: i32 = 40;
// Keep the OS window up long enough for the webview's exit fade to play.
const EXIT_FADE_MS: u64 = 280;

// A process handle stays tied to this parent even if Windows later reuses its PID.
// Wait on a worker thread so the window's event loop remains responsive.
#[cfg(windows)]
fn watch_parent() {
    use std::ffi::c_void;

    #[link(name = "kernel32")]
    extern "system" {
        fn OpenProcess(access: u32, inherit: i32, pid: u32) -> *mut c_void;
        fn WaitForSingleObject(handle: *mut c_void, milliseconds: u32) -> u32;
        fn CloseHandle(handle: *mut c_void) -> i32;
    }

    let Ok(value) = std::env::var("OWNKEY_PARENT_PID") else {
        return; // Standalone overlay development has no owning backend.
    };
    let pid = value.parse::<u32>().expect("invalid OWNKEY_PARENT_PID");
    thread::spawn(move || unsafe {
        let parent = OpenProcess(0x0010_0000, 0, pid); // SYNCHRONIZE
        if parent.is_null() {
            std::process::exit(0); // The backend may have exited during startup.
        }
        let result = WaitForSingleObject(parent, u32::MAX);
        CloseHandle(parent);
        std::process::exit(if result == 0 { 0 } else { 1 });
    });
}

// Track the parent's process start time as well as its PID so a reused PID
// cannot keep an orphaned overlay alive.
#[cfg(target_os = "linux")]
fn watch_parent() {
    let Ok(value) = std::env::var("OWNKEY_PARENT_PID") else {
        return;
    };
    let pid = value.parse::<u32>().expect("invalid OWNKEY_PARENT_PID");
    let identity = move || -> Option<String> {
        let stat = std::fs::read_to_string(format!("/proc/{pid}/stat")).ok()?;
        // comm can contain spaces and parentheses; field 22 is starttime.
        stat.rsplit_once(')')?.1.split_whitespace().nth(19).map(str::to_owned)
    };
    let Some(start_time) = identity() else {
        std::process::exit(0);
    };
    thread::spawn(move || loop {
        thread::sleep(Duration::from_millis(250));
        if identity().as_ref() != Some(&start_time) {
            std::process::exit(0);
        }
    });
}

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

// `{"meetings": {"url": "http://127.0.0.1:PORT/?token=...", "navigate": false}}`
// asks for Ownkey's meeting window. The sender gets a JSON reply on the same
// socket, so the backend can fall back to the browser when nothing answers.
#[derive(Debug, Deserialize)]
struct MeetingsRequest {
    meetings: MeetingsCommand,
}

#[derive(Debug, Deserialize)]
struct MeetingsCommand {
    url: String,
    #[serde(default)]
    navigate: bool,
}

fn is_local_meetings_url(url: &Url) -> bool {
    url.scheme() == "http" && url.host_str() == Some("127.0.0.1") && url.port().is_some()
}

// The meeting window only ever shows Ownkey's own server on this PC.
fn local_meetings_url(raw: &str) -> Result<Url, String> {
    let url = Url::parse(raw).map_err(|error| format!("invalid url: {error}"))?;
    if is_local_meetings_url(&url) {
        Ok(url)
    } else {
        Err("only Ownkey's local meeting server can be shown".to_string())
    }
}

fn show_meetings_window(app: &AppHandle, url: Url, navigate: bool) -> Result<&'static str, String> {
    if let Some(window) = app.get_webview_window(MEETINGS_WINDOW) {
        let _ = window.unminimize();
        let _ = window.show();
        let _ = window.set_focus();
        if navigate {
            window.navigate(url).map_err(|error| error.to_string())?;
        }
        return Ok("focused");
    }
    let port = url.port();
    WebviewWindowBuilder::new(app, MEETINGS_WINDOW, WebviewUrl::External(url))
        .title("Ownkey Meetings")
        .inner_size(1280.0, 860.0)
        .min_inner_size(920.0, 600.0)
        .center()
        .focused(true)
        // The page is always dark, like the rest of Ownkey: match the title bar
        // and do not flash white while the page loads.
        .theme(Some(tauri::Theme::Dark))
        .background_color(tauri::window::Color(14, 14, 14, 255))
        // Exports download; nothing else may take this window off the local server.
        .on_navigation(move |next| is_local_meetings_url(next) && next.port() == port)
        .build()
        .map_err(|error| error.to_string())?;
    Ok("opened")
}

fn handle_meetings_request(app: &AppHandle, command: MeetingsCommand) -> serde_json::Value {
    let url = match local_meetings_url(&command.url) {
        Ok(url) => url,
        Err(error) => return serde_json::json!({ "meetings": "refused", "error": error }),
    };
    // Windows are created on the event loop; this thread only waits for the result.
    let (sender, receiver) = mpsc::channel();
    let handle = app.clone();
    let navigate = command.navigate;
    let scheduled = app.run_on_main_thread(move || {
        let _ = sender.send(show_meetings_window(&handle, url, navigate));
    });
    if scheduled.is_err() {
        return serde_json::json!({ "meetings": "failed", "error": "event loop unavailable" });
    }
    match receiver.recv_timeout(Duration::from_secs(8)) {
        Ok(Ok(status)) => serde_json::json!({ "meetings": status }),
        Ok(Err(error)) => serde_json::json!({ "meetings": "failed", "error": error }),
        Err(_) => serde_json::json!({ "meetings": "failed", "error": "timed out" }),
    }
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
        let address = udp_addr();
        let socket = match UdpSocket::bind(&address) {
            Ok(socket) => socket,
            Err(error) => {
                log::error!("failed to bind UDP bridge at {}: {}", address, error);
                return;
            }
        };
        let _ = socket.set_read_timeout(Some(Duration::from_millis(250)));
        log::info!("overlay UDP bridge listening on {}", address);

        let mut buffer = [0_u8; 8192];
        loop {
            match socket.recv_from(&mut buffer) {
                Ok((count, sender)) => {
                    let payload = match std::str::from_utf8(&buffer[..count]) {
                        Ok(text) => text,
                        Err(error) => {
                            log::warn!("invalid UTF-8 UDP payload: {}", error);
                            continue;
                        }
                    };
                    if let Ok(request) = serde_json::from_str::<MeetingsRequest>(payload) {
                        let reply = handle_meetings_request(&app, request.meetings);
                        let _ = socket.send_to(reply.to_string().as_bytes(), sender);
                        continue;
                    }
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
    // GNOME Wayland does not expose absolute positioning/always-on-top for
    // ordinary clients. Use XWayland for this non-interactive overlay only.
    #[cfg(target_os = "linux")]
    if std::env::var_os("DISPLAY").is_some() {
        std::env::set_var("GDK_BACKEND", "x11");
    }
    #[cfg(any(windows, target_os = "linux"))]
    watch_parent();
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
                // Tao's click-through implementation requires a native GDK
                // window even though we start hidden (before the first show).
                #[cfg(target_os = "linux")]
                {
                    use gtk::prelude::WidgetExt;
                    window.gtk_window()?.realize();
                }
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

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn meeting_window_only_accepts_the_local_server() {
        assert!(local_meetings_url("http://127.0.0.1:51234/?token=abc&view=new").is_ok());
        for refused in [
            "https://127.0.0.1:51234/",
            "http://localhost:51234/",
            "http://127.0.0.1/",
            "http://example.com:80/",
            "file:///C:/Windows/win.ini",
            "not a url",
        ] {
            assert!(local_meetings_url(refused).is_err(), "{refused}");
        }
    }

    #[test]
    fn meetings_request_is_not_mistaken_for_overlay_state() {
        let request = r#"{"meetings":{"url":"http://127.0.0.1:1/?token=t"}}"#;
        let parsed = serde_json::from_str::<MeetingsRequest>(request).expect("request parses");
        assert!(!parsed.meetings.navigate);
        assert!(serde_json::from_str::<MeetingsRequest>(r#"{"visible":true}"#).is_err());
        assert!(serde_json::from_str::<OverlayState>(request).is_err());
    }
}
