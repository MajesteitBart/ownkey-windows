# Optional local Orukeet transcription

Plan only. Researched against Ownkey's current code and upstream documentation on 2026-09-15, and revised the same day after review. Upstream facts (archive hash, size, license, loader arguments, wheel availability) were verified against the Hugging Face API, the pinned `onnx/manifest.json`, and PyPI.

## Recommendation

Add **Local (Orukeet)** to Settings > Audio, using ONNX INT8 through Python's `sherpa-onnx` CPU runtime. Orukeet is an offline speech-to-text model with automatic language detection across 25 European languages, including Dutch and English, which fits Ownkey's dictation flow. Its model download is 486,807,585 bytes and extracts to 671,619,800 bytes. The weights carry CC BY-SA 4.0; the archive includes `LICENSE-WEIGHTS` and `NOTICE.md`, which must be retained. [Model card](https://huggingface.co/oruk/orukeet)

Bundle a tested, pinned CPU runtime with Ownkey, but **never bundle or automatically download the model**. Users need neither Python nor CUDA installed. On Windows the runtime is two wheels: `sherpa-onnx` holds only the extension module, and `sherpa-onnx-core` holds `onnxruntime.dll` and the C/C++ API DLLs under `sherpa_onnx/lib/`. Together they add about 19 MB compressed and 28 MB on disk. Prebuilt CPU wheels exist for Python 3.10 through 3.14. [Runtime installation](https://k2-fsa.github.io/sherpa/onnx/python/install.html)

Pin the model **by archive hash only**. The `onnx/README.md` inside the upstream repository at the pinned revision advertises a different build with a different hash and size; the manifest and the stored blob agree with this plan. Never resolve "latest".

## Design decisions

- **One commit path.** Download only installs files. Save validates, loads the recognizer, and only then persists and applies the selection. There is no separate "Download and activate" action.
- **Download is owned by the app, not the Settings window.** Closing Settings does not cancel a download; reopening re-attaches to its progress.
- **Load on demand.** The model loads when Save validates it and, after a restart or an idle unload, on the next hotkey press. Startup does not load the model eagerly.
- **Process boundary is decided by measurement.** Step 1 measures whether deleting the recognizer in-process returns memory to the OS. If it does, the recognizer runs in-process. If it does not, a worker process is used.
- **Idle unload ships without UI.** A config key with a 20-minute default controls it. Settings controls can follow later.
- **Config stores the model id only.** An in-code catalog maps id to revision, URL, hash, and file manifest. Thread count is a code constant chosen in step 1. No device field is stored while CPU is the only option.
- **16 kHz is forced** when Orukeet is active. No resampler dependency.
- **Language is automatic.** The Language control is disabled and shown as `auto` for Orukeet; the stored value is ignored on the local path.

## User flow

1. Select **Local (Orukeet)** in Audio. Show size, local storage location, model source, license note, and **Not downloaded**. Hide API key, endpoint, and model refresh controls. Disable Language and show `auto`. Merely selecting the provider downloads nothing and changes nothing until Save.
2. Click **Download**. Show progress and Cancel through the stages **Downloading**, **Verifying**, **Extracting**. The download runs in the app; closing Settings leaves it running and reopening shows current progress. Completion with the window closed leaves the model installed and the previous provider active.
3. When installed, show **Installed · Not active**. Click **Save**. Save checks the installed files against the manifest, loads the recognizer, shows **Loading**, and only on success persists the audio selection and applies it. Show **Active · Loaded**. On failure show a retryable error and keep the previous configuration.
4. Restarting Ownkey restores the saved provider, verifies installed files, and shows **Active · Unloaded** until the first hotkey press loads the model.
5. An installed model that is not active offers **Remove download**; require switching away and saving before removing the active model. If saved files disappear, show **Model missing** with a download action; never download silently and never send audio to a cloud provider.

Keep the previous active configuration until validation, loading, and persistence all succeed. Cancellation, a corrupt download, loading failure, or config-write failure must leave it usable. A completed download may remain installed without being active. If the user changes the provider selection while a download runs, the download still completes as **Installed** and does not switch providers.

## Implementation steps

### 1. Prove the Windows runtime

Create a small local inference script and run it on Zolder before wiring the UI. Use the standard offline transducer loader with `model_type="nemo_transducer"`, `sample_rate=16000`, `feature_dim=128`, `decoding_method="greedy_search"`, and `provider="cpu"`. Load `encoder.int8.onnx`, `decoder.int8.onnx`, `joiner.int8.onnx`, and `tokens.txt`. Parse WAV with the standard library and NumPy; do not add `soundfile`. [Loader instructions](https://github.com/Oruk-AI/orukeet/blob/main/export/onnx/README.md)

Prototype on `sherpa-onnx==1.13.8`. Upstream pins 1.13.4 and notes that TDT detection depends on the encoder's `url` metadata; if 1.13.8 misidentifies the model, drop to 1.13.4. Pin whichever passes.

Measure, with short Dutch and English recordings of about 5 and 30 seconds:

- Cold and warm load time.
- Warm transcription latency per clip.
- Peak RSS during load and decode.
- RSS after deleting the recognizer and collecting garbage. This decides the process boundary.
- Time to first transcript when loading overlaps a 5-second recording.
- Thread counts 2, 4, and `os.cpu_count()`.

Then build the same script with PyInstaller onedir, collecting `sherpa_onnx` dynamic libraries, and run it from the frozen output. Frozen validation belongs here, not in step 4.

### 2. Add model storage and downloads

Create `local_models.py` with a fixed catalog and an app-owned download manager. Store files under `%LOCALAPPDATA%\Ownkey\models\orukeet\<revision>`, separate from the installer and roaming settings.

The catalog entry for `orukeet-onnx-int8` pins this [archive](https://huggingface.co/oruk/orukeet/resolve/55a984d46f68323301837194ce647c702f55facc/onnx/sherpa-onnx-orukeet-v0.1.0-int8.tar.bz2), revision `55a984d46f68323301837194ce647c702f55facc`, 486,807,585 bytes, SHA-256 `f9191f30178cc9122ce2f023bf9fefafc822028307b0efa4caff645ba3fe8d0a`, and the full seven-file manifest with per-file sizes and hashes: `encoder.int8.onnx`, `decoder.int8.onnx`, `joiner.int8.onnx`, `tokens.txt`, `bpe.vocab`, `LICENSE-WEIGHTS`, `NOTICE.md`. The loader needs only the first four; the others are validated for integrity and attribution. Updates are explicit catalog changes with a new revision, surfaced as a download action, never a config migration.

Stream to a temporary file, check free space for both archive and extraction, verify the archive hash, then extract into a staging folder with `tarfile` and `filter="data"`, which rejects traversal, absolute paths, links, and devices. Bzip2 decompression of this archive takes tens of seconds, so **Extracting** has its own progress stage. Validate every manifest file by size and hash, then rename the staging folder into place. Prevent duplicate downloads and clean partial data on cancel or failure.

The manager lives on `OwnkeyApp`, exposes a state snapshot and a subscription for progress, and survives the Settings window. Settings polls it with the same queue-plus-`after` pattern used by the model refresh button.

### 3. Route transcription locally

Create `local_transcription.py` to own the recognizer lifecycle. In `ownkey.py:transcribe()`, dispatch local requests before the existing HTTP adapter. Parse Ownkey's mono int16 WAV into float32 samples, create a stream, decode, and return text through the existing paste/rewrite flow. Both dictation and spoken rewrite instructions use this path.

**Recognizer hosting.** If step 1 shows in-process unload returns memory, run the recognizer in-process behind a lock. Otherwise run a worker process:

- Launch `sys.executable` with `--recognizer-worker`. In the frozen bundle that is `Ownkey.exe`, so `ownkey.py` must dispatch on that flag before constructing `OwnkeyApp`. Today the entry point unconditionally builds the app, which registers Windows startup, launches the overlay, and adds a tray icon. Place the dispatch before the heavy imports or measure the import cost.
- Use explicit pipes with length-prefixed messages and `CREATE_NO_WINDOW`. Verify in the frozen build that the child's standard handles are valid; a windowed executable may not provide them. Fall back to a localhost socket if needed.
- Terminate the worker explicitly in `_quit`, which ends with `os._exit` and skips cleanup.

**Serialization.** Recording can restart while the previous transcription thread is still running, so local requests must queue. Serialize decoding, protect it against removal or provider changes while busy, and never let a **Loading model...** message clobber a decode in progress. Capture settings per recording so a settings change cannot reroute an in-flight utterance. Release model memory when switching away or quitting.

**Sample rate.** Force the microphone stream to 16 kHz while Orukeet is active, regardless of the `sample_rate` config value.

#### Automatic unloading

Persist `local_model_idle_timeout_minutes: 20` in the existing config. Zero disables unloading. No Settings controls in this version.

Start the idle timer after loading finishes and reset it after every transcription attempt completes, including failures and cancelled recordings. Use a monotonic clock and guard lifecycle transitions so the timer never unloads during loading, recording, or decoding. At expiry, delete the recognizer or close the worker, keep downloaded files and the saved provider unchanged, and show **Active · Unloaded** rather than a connection error.

On the next hotkey press, start loading while recording audio normally. If loading has not finished on release, retain the recording, show **Loading model...**, and decode once ready. Serialize concurrent load requests. Loading failure must show a retryable error without a download or cloud fallback.

### 4. Integrate settings, config, and packaging

- `providers.py`: add an explicit local audio capability. `AUDIO_PROVIDER_IDS` currently derives from nonempty endpoints and `REWRITE_PROVIDER_IDS` includes every preset; restructure both so Orukeet needs no endpoint or key and never appears under Rewriting. `provider_requires_key` returns true for everything except Ollama and gates the first-run prompt, the audio-key check before transcription, and the rewrite-key check; it must return false for Orukeet. `tests/test_providers.py` pins the exact capability tuples and needs updating.
- `ownkey.py`: `load_config` resets unknown audio providers to Mistral and must accept Orukeet. Extend `SettingsWindow`, Save validation, startup checks, and connection status. Local readiness depends on the recognizer, not an HTTP probe; the connection monitor must not report offline for the local provider. Save rebases on the live `self.app.cfg` rather than the snapshot taken at window open, so keys written outside the window are never dropped.
- Keep `%APPDATA%\Ownkey\config.json`. Persist `audio_provider: "orukeet"` and `audio_model: "orukeet-onnx-int8"`. Derive the cache path from the catalog; verify installed files instead of trusting a saved flag. Write config atomically.
- `requirements.txt` and `Ownkey.spec`: pin `sherpa-onnx` and `sherpa-onnx-core`, collect the package's dynamic libraries, and keep the import lazy so tests run without the runtime installed. Measure installer growth and smoke-test the frozen executable on a clean Windows machine. Exclude model files from all build artifacts.
- `installer/Ownkey.iss`: the model lives outside the install directory, so add an uninstall prompt offering to delete `%LOCALAPPDATA%\Ownkey\models`. Default to keeping it.

### 5. Update documentation

- README provider table: add Orukeet with local audio transcription and no rewriting.
- README privacy statement: audio stays on the PC when the local provider is active; rewriting still sends text to the configured provider.
- Audio tab hint text: it currently says presets are limited to APIs with official transcription support.
- Add attribution for Orukeet and NVIDIA Parakeet TDT 0.6B v3 where the local option is described.

## How it works locally

**Microphone → buffered audio → release hotkey → CPU recognition → transcript → existing text insertion.** After installation, transcription needs no internet or API key. Keep audio in memory.

Rewriting remains a separate feature: auto-rewrite and selection rewriting can still send text to the configured cloud provider. Explain this next to the local option. Fully offline use requires disabling rewriting or configuring a local rewrite provider.

## Acceptance checks

- Fresh installation downloads no model; existing provider settings still work; updated provider tests pass.
- Download produces **Installed** without changing the active provider. Save loads the model, produces a real transcript, and survives restart.
- Installed-model dictation and spoken rewrite instructions work with networking disabled and no audio API key.
- Cancel, bad hash, unsafe archive, insufficient disk space, model-load failure, and failed config writes preserve the prior configuration.
- Closing Settings mid-download does not cancel it; reopening shows progress; completion while closed leaves **Installed**.
- Missing files show **Model missing** with a download action and never trigger a silent download or cloud fallback.
- Idle unload defaults to 20 minutes and survives restart. Test expiry with a controllable clock, zero disables it, and the timer resets after every attempt. Expiry releases memory by the step-1 measurement without deleting files or clearing selection.
- The next hotkey press after unload or restart reloads once, preserves audio captured during loading, and transcribes offline. Loading, recording, and decoding cannot race unloading, and overlapping utterances queue.
- Uninstall offers to delete the model directory and keeps it by default.
- The frozen build runs on a clean Windows machine; installer growth is measured and recorded.
- Documentation changes from step 5 are in place.
