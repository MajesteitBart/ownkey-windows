# Ownkey for Windows 🎙️

**Push-to-talk voice keyboard for Windows with bring-your-own-key AI providers.**

Hold a hotkey → speak → release → your words are typed anywhere on screen.

Ownkey is a keyboard you actually own: modern AI input without sending your words to someone else's servers. You bring your own API key; your keystrokes stay yours.

Part of the Ownkey family:

- **Ownkey for Windows** — this repository
- **[Ownkey Keyboard (Android)](https://github.com/MajesteitBart/ownkey-keyboard)**
- Website: [ownkey.bvdm.ai](https://ownkey.bvdm.ai) *(planned)*

---

## Features

- 🎤 **Push-to-talk** — hold your hotkey, speak, release; text appears instantly
- 🔔 **Ready ping** — a short chime plays when the microphone is armed, so you know exactly when to start speaking
- ⌨️ **Types anywhere** — works in any app: browser, Word, Slack, VS Code, etc.
- 🔔 **System tray** — runs quietly in the background, coloured icon shows state
- ⚙️ **Configurable** — hotkey, language, paste mode, model, API endpoint
- 🚀 **Windows startup** — optionally auto-starts with Windows

## End-user install status

Ownkey is **not yet shipped as a one-click installer** (`.msi`/setup) or a true **single self-contained executable**.

Current packaging is a **portable folder build** created with PyInstaller `--onedir`:

- `dist\Ownkey\Ownkey.exe`
- plus required runtime files in the same folder

That means users must keep the full `dist\Ownkey` folder together.

For complete user-facing installation steps, see **[`docs/USER_INSTALLATION.md`](docs/USER_INSTALLATION.md)**.

## Quick Start (run from source)

**1. Install dependencies**

```bash
pip install -r requirements.txt
```

**2. Configure providers in Settings**

Start the app, then right-click the tray icon and open **Settings**.
Choose separate providers for **Audio** and **Rewriting**, enter their API keys,
click **Refresh** to retrieve available models, then click **Save**.

**3. Run**

```bash
python ownkey.py
```

The app starts in the system tray (bottom-right). Right-click → **Settings** to configure your providers.

---

## Usage

| Action | How |
|--------|-----|
| Start recording | Hold **Right Alt**, wait for the short ping, then speak |
| Stop & transcribe | Release the hotkey |
| Rewrite selected text | Highlight text, hold the rewrite hotkey (**Right Ctrl** by default), speak an instruction ("make this more formal", "translate to English", "turn into bullet points"), release |
| Open Settings | Right-click tray icon → **Settings** |
| Quit | Right-click tray icon → **Quit** |

### AI rewrite

Two Wispr Flow-style features, powered by the provider configured in the
**Rewriting** tab:

- **Auto-rewrite dictation** — when enabled in Settings, every transcript is cleaned up before it is typed: filler words removed, self-corrections applied, punctuation fixed, tone and formatting applied per your settings. If the rewrite call fails, the raw transcript is inserted instead.
- **Rewrite selected text** — highlight text anywhere, hold the rewrite hotkey, and speak what should happen to it. The selection is replaced in place with the rewritten version.

### Tray icon states

| Color | State |
|-------|-------|
| 🔘 Dark gray | Idle — ready |
| 🔴 Red | Recording |
| 🟠 Orange | Transcribing |

## Configuration

Settings are stored in `%APPDATA%\Ownkey\config.json` and managed through the Settings window.

Audio and rewriting have independent provider presets, credentials, endpoints,
and models. Model names are retrieved live from the selected provider API; the
model field remains editable for compatible aliases or custom endpoints.

| Provider | Audio transcription | Rewriting | Model discovery |
|----------|---------------------|-----------|-----------------|
| OpenAI | Yes | Yes | `GET /v1/models` |
| Anthropic | No official audio transcription API | Yes | `GET /v1/models` |
| Google Gemini | Yes | Yes | `GET /v1beta/models` |
| Mistral | Yes | Yes | `GET /v1/models` |
| Ollama | No official audio transcription API | Yes, local or cloud | `GET /api/tags` |

Ollama presets include `http://localhost:11434/api/chat` and
`https://ollama.com/api/chat`. Ownkey does not bundle or assume any Ollama model;
use **Refresh** to list the models exposed by the selected endpoint.

| Setting | Default | Description |
|---------|---------|-------------|
| `audio_provider` | `mistral` | Provider used for transcription |
| `audio_api_key` | *(empty)* | API key used only for transcription |
| `audio_endpoint` | `https://api.mistral.ai/v1/audio/transcriptions` | Transcription endpoint |
| `audio_model` | `voxtral-mini-latest` | Transcription model |
| `hotkey` | `right alt` | Push-to-talk key |
| `language` | `auto` | Transcription language (`auto`, `en`, `nl`, `de`, `fr`, …) |
| `paste_mode` | `true` | Clipboard paste (faster) vs. keystroke-by-keystroke |
| `sample_rate` | `16000` | Microphone sample rate (Hz) |
| `auto_rewrite` | `false` | Clean up every dictation with AI before typing it |
| `rewrite_tone` | `auto` | Tone for auto-rewrite: `auto`, `professional`, `casual`, `friendly`, `concise` |
| `rewrite_formatting` | `true` | Let auto-rewrite structure paragraphs and bullet lists |
| `rewrite_custom_instructions` | *(empty)* | Extra free-form instructions applied to auto-rewrite |
| `rewrite_hotkey` | `right ctrl` | Hold-to-talk key for rewriting the selected text (`off` to disable) |
| `rewrite_provider` | `mistral` | Provider used for text rewriting |
| `rewrite_api_key` | *(empty)* | API key used only for rewriting |
| `rewrite_model` | `mistral-small-latest` | Chat model used for rewrites |
| `rewrite_endpoint` | `https://api.mistral.ai/v1/chat/completions` | Endpoint used for rewrites |

Existing configs using the former shared `api_key`, `endpoint`, `model`, and
`chat_endpoint` keys are migrated automatically when loaded.

---

## Build portable EXE bundle (developer)

```bat
build.bat
```

Output: `dist\Ownkey\Ownkey.exe`.

Important: this is a **folder-based bundle**, not a single self-contained executable. Share the full `dist\Ownkey` directory with users.

Requires PyInstaller (`pip install pyinstaller`). The build script installs it automatically.

### Full Windows build (backend + overlay)

If you want the Python backend and Tauri overlay both buildable on Windows:

1. Install prerequisites on Windows:
   - Python 3.11+
   - Node.js + pnpm
   - Rust toolchain (`rustup`) with MSVC
   - Visual Studio Build Tools (Desktop development with C++)
2. Build backend:

```bat
build.bat
```

3. Build overlay app:

```bat
cd overlay-ui
pnpm install
pnpm build
```

Overlay artifacts are produced in `overlay-ui/src-tauri/target/release/bundle/`.

---

## Tauri Overlay UI (React + Tailwind/shadcn-style)

An experimental desktop overlay UI is available in `overlay-ui/`.

Run it:

```bash
cd overlay-ui
pnpm install
pnpm dev
```

It listens for overlay state payloads on UDP `127.0.0.1:38485`.
See `overlay-ui/README.md` for payload format and bridge details.

Set `OWNKEY_TAURI_OVERLAY_ONLY=1` to disable Tkinter overlay and use the Tauri overlay only.

---

## Dependencies

| Package | Purpose |
|---------|---------|
| `sounddevice` | Microphone capture |
| `numpy` | Audio buffer |
| `requests` | API calls |
| `pynput` | Global hotkey detection |
| `keyboard` | Text output |
| `pyperclip` | Clipboard (paste mode) |
| `pystray` | System tray icon |
| `Pillow` | Icon rendering |

---

## Troubleshooting

**Hotkey not working?**
Run as Administrator — some elevated apps block non-admin global hotkeys.

**No audio?**
Check Windows microphone permissions: Settings → Privacy → Microphone.

**Paste mode not working?**
Some apps block programmatic `Ctrl+V`. Disable paste mode in Settings.

**API errors?**
Verify the provider, activity-specific API key, endpoint, and selected model in
Settings. Use **Refresh** to confirm that the provider returns models for the key.

---

## License

MIT
