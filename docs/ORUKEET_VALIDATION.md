# Orukeet implementation and validation

Validated on DESKTOP-ZOLDER, Windows 11 build 26200, Python 3.14.2 and
PyInstaller 6.19.0 on 2026-09-15. The CPU packages are pinned to
`sherpa-onnx==1.13.8` and `sherpa-onnx-core==1.13.8`.

The implementation now includes `origin/main` at `0a1ff3a`, including v0.4.0
OpenRouter, Custom providers, dictation fixes, and the subsequent Linux support.
The Windows backend and overlay were both rebuilt from this combined source.

## Runtime decision

The recognizer runs in-process with four CPU threads. Deleting the recognizer
and collecting garbage releases its model memory on this machine. A separate
worker process was therefore unnecessary.

The benchmark used local Windows speech synthesis: Zira for English and Frank
for Dutch. Short clips were 6.1 and 6.8 seconds; long clips were 36.6 and 40.6
seconds. Long clips repeat the same sentence six times. These measurements
demonstrate runtime operation, not accuracy on natural microphone recordings.
The Dutch long clip lost words near the end; the short clips matched their input.

| Threads | First / second load, seconds | Warm short EN / NL decode, seconds | Warm long EN / NL decode, seconds | Peak RSS, MiB | RSS after unload, MiB |
|---|---|---|---|---|---|
| 2 | 2.64 / 1.72 | 0.41 / 0.46 | 2.77 / 3.40 | 1,233 | 60 |
| 4 | 1.82 / 1.56 | 0.26 / 0.27 | 1.67 / 1.93 | 1,235 | 62 |
| 20 (`os.cpu_count()`) | 2.01 / 2.02 | 1.64 / 1.26 | 2.98 / 3.81 | 1,247 | 65 |

The first load in the process took 2.64 seconds. Subsequent trials reused the
OS file cache; these are not machine-reboot cold-start measurements. RSS before
importing the runtime was 33 MiB. About 30 MiB of runtime overhead remains after
unloading; the approximately 700 MiB loaded model and decode allocations are
released. RSS was sampled every 20 milliseconds.

An independent PyInstaller onedir benchmark also loaded and transcribed all four
clips. With four threads it peaked at 1,237 MiB and returned to 62 MiB after
unloading. First / second load took 1.91 / 2.05 seconds. These frozen timing
measurements overlapped other development work and should not be used to compare
source and frozen performance.

For a five-second recording overlapping the measured four-thread load, the
estimated first transcript arrives 5.26 seconds after pressing the hotkey.
This estimate uses `max(recording duration, load time) + decode time`; it is not
a measured microphone-to-paste latency.

## What was checked

- The real 486,807,585-byte archive downloaded into an ignored development folder.
  Its archive hash and all seven extracted file hashes matched the pinned catalog.
- Source and frozen CPU inference worked in Dutch and English without CUDA.
- The actual windowed `Ownkey.exe` supports `--local-smoke-test`. That path avoids
  constructing the app, writing user settings, registering startup, opening the
  microphone, or creating tray/overlay windows. It transcribes, expires the idle
  timer, reloads, and transcribes again with all Requests HTTP calls disabled.
- Settings was exercised with real model files and a temporary config. Selecting
  local hid API controls, disabled Language, showed Installed, and Save produced
  Active · Loaded. Both states were visually inspected.
- With the rebuilt Windows overlay enabled, 77 automated tests pass and eight
  Linux-specific tests are skipped on Windows. They cover downloads, archive attacks, disk-space failure,
  cancellation, removal guards, damaged files, atomic config failure, Save
  cancellation, live config rebasing, provider routing, dictation and spoken
  rewrite instructions, concurrent loads and decodes, idle expiry, disabled
  expiry, recording snapshots, Settings lifecycle, and shutdown. OpenRouter is
  verified in the rewrite menu, after Save/restart with local audio, and in the
  frozen executable's provider catalog. Windows overlay parent-exit and orphan
  cleanup integration tests also pass.
- The Windows signing-pipeline checks passed. The local build is unsigned and
  is intended for development testing.

## Package size

An unsigned Inno Setup build was compared with a fresh build of the pre-change
`origin/main` backend using the same Python environment, overlay binary, compression, and
installer assets. The backend folder grew from 88.6 MB to about 117.1 MB
(28.6 MB). The installer grew from 31.6 MB to about 37.3 MB (5.7 MB).
Sizes are decimal MB. No ONNX models or model archives were present in either
backend bundle or installer inputs.

## Reproduce

Install app dependencies plus the benchmark-only dependency:

```powershell
py -m pip install -r requirements.txt pyinstaller psutil
py -m unittest discover -s tests
powershell.exe -NoProfile -File scripts/New-OrukeetBenchmarkClips.ps1
```

Install the model through Settings > Transcription > Download, or explicitly run the
download manager against an isolated development directory. Never add models to
build inputs. Run the benchmark with the installed revision directory and WAV
paths:

```powershell
py scripts/benchmark_orukeet.py $modelDirectory $englishWav $dutchWav --output build/benchmark.json
py -m PyInstaller --noconfirm --onedir --collect-all sherpa_onnx --specpath build --workpath build/probe --distpath dist/probe scripts/benchmark_orukeet.py
```

Build Ownkey using `Ownkey.spec`. To exercise that actual executable, supply the
models root, which contains `orukeet/<revision>`, and mono int16 16 kHz clips:

```powershell
dist/Ownkey/Ownkey.exe --local-smoke-test --model-root $modelsRoot --wav $englishWav $dutchWav --output build/frozen-smoke.json
```

Read the JSON result after the process exits. A windowed executable does not
write diagnostics to a console. The smoke test never downloads a model.

The Settings widgets have their own check in the frozen runtime. It builds a
button, a toggle, a card, a dropdown and a scrollbar on a hidden window and
reports whether the rounded shapes are antialiased images (they fall back to
plain polygons when Pillow cannot render), whether the dropdown list builds,
and how wide the scrollbar is. It starts no tray icon and no hotkeys:

```powershell
dist/Ownkey/Ownkey.exe --ui-smoke-test --output build/frozen-ui.json
```

## Remaining manual release checks

The follow-up implementation adds editable model folders and an idle timeout
control in Audio settings. Tests cover loading an existing folder, preserving
an active recording while switching folders, rolling back a failed Save, and
keeping unrelated files when removing a model. Custom folders are retained by
the uninstaller. The experimental built-in local rewrite LLM has been removed;
Orukeet transcription and the existing cloud, Custom and Ollama rewrite providers remain.

- Run the installer and frozen smoke test on a clean Windows machine with no
  development runtime. Zolder has Python and development tools installed.
- Record natural Dutch and English speech of about five and thirty seconds.
  Check dictation and spoken rewrite commands through the physical hotkeys,
  including rapid consecutive utterances and capture during model loading.
- Exercise the uninstall prompt interactively: No is the default and preserves
  files; Yes removes the current user's model directory. Confirm both choices
  and silent uninstall on a disposable Windows installation.

## Upstream pins

The catalog follows the pinned
[Hugging Face manifest](https://huggingface.co/oruk/orukeet/blob/55a984d46f68323301837194ce647c702f55facc/onnx/manifest.json)
and archive hash, not the differing README archive metadata at that revision.
The loader uses the standard offline transducer parameters documented by
[Oruk AI](https://github.com/Oruk-AI/orukeet/blob/main/export/onnx/README.md).
The installed archive retains the weight license and notice, including Orukeet
and NVIDIA Parakeet TDT 0.6B v3 attribution.
