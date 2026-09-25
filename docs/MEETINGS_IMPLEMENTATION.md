# Ownkey Meetings: what is built

The recording-time transcription and live speaker additions are documented in [MEETINGS_LIVE_PLAN.md](MEETINGS_LIVE_PLAN.md). That document supersedes the transcription-after-Stop behavior below when **Transcribe while recording** is enabled.

Status on 17 September 2026. This describes the code in `meetings/` and its
tray integration, against the proposal in `MEETINGS_SPEC.md`. It is a first
usable slice, not the whole spec.

## What works

- **Recording** from the tray: Meetings › New meeting opens the meeting
  window. Microphone, system audio or both, with device pickers, a retention
  choice and the "let everyone know you are recording" reminder. Nothing is
  written before Start.
- **Live input meter before Start.** The Microphone card on the New meeting
  sheet shows the level of the selected microphone, in the same style as
  the meters of a running session, so a wrong or muted device shows before
  the meeting does. The backend opens the microphone for this only
  (`GET /api/preview/mic?device=`), turns each block into one number and
  drops it: nothing is kept and nothing is written. The preview stops when
  the sheet is left, the window is hidden or closed, a recording starts, or
  no poll has arrived for 2.5 seconds. A microphone that cannot be opened
  shows its error in the card, and four seconds of silence shows a hint.
  Windows shows its microphone-in-use indicator while the sheet is open.
- **Ownkey's own window.** The meeting interface is a local page on 127.0.0.1
  with a per-process token, shown in a window of the overlay process (Tauri,
  WebView2): title bar "Ownkey Meetings", its own taskbar entry, no address
  bar, no browser extensions, no history. The backend asks for the window
  over the overlay's UDP channel (`{"meetings": {"url", "navigate"}}`) and
  gets a reply; "Open Meetings" focuses an open window without reloading it,
  "New meeting" navigates it. The window refuses anything but
  `http://127.0.0.1:<port>`, has no Tauri IPC permissions, and closing it
  leaves the pill and a running recording alone. When the overlay does not
  answer within six seconds (running from source without the overlay build,
  an older overlay), the same page opens in the default browser.
- **Durable capture.** Each source is its own mono 16 kHz track, written in
  five-second WAV chunks (`%LOCALAPPDATA%\Ownkey\meetings\<meeting>\audio\<source>`)
  that are flushed and renamed before their row is committed to `library.db`.
  Pause discards new audio and is stored as an event, so transcript times equal
  playback times. A silent loopback is padded from the timeline, an overrun
  becomes a visible gap event, a dead source or a write error ends capture and
  marks the meeting interrupted. A crash is reconciled on the next start: the
  meeting is marked interrupted, chunk rows without files are dropped, and
  Ownkey notifies you. Recording never resumes on its own.
- **Transcription with the same providers as dictation.** Settings › Meetings
  › Transcription offers "Same as dictation" (default), Orukeet on this PC,
  or any audio provider with its own key, endpoint and model (OpenAI,
  Mistral, Google, custom OpenAI-compatible endpoints). A cloud provider
  receives every recorded track as WAV in windows of up to 28 seconds and
  returns text per window, so those passages carry window timing (shown as
  "≈ time"); it asks before the first upload and "Don't ask again" stores
  `meetings_transcription_policy = allow`. A custom endpoint on localhost
  counts as local and never asks. After Stop, a cloud engine that still
  needs consent waits: the window asks when it is open, and the meeting
  shows "Not transcribed yet" with a Transcribe button otherwise.
- **Transcription on this PC** with the installed Orukeet model, after Stop.
  Orukeet has no hard clip limit, but it drops words when one decode window is
  long or mixes languages (a 29 s track with English and Dutch turns lost
  three of four sentences when decoded whole). So each track is decoded in
  windows: a window ends at the first pause of half a second after six
  seconds of audio, and never runs past 28 s (then it ends at the quietest
  moment). Every window returns per-token timestamps, offset by the window
  start, so passages (`p0001`, `p0002`, …) carry track time and keep their
  token timing for later speaker splitting. Passages appear while the job
  runs. If Orukeet is not installed the job fails with a message and the
  meeting keeps its audio; there is no download and no cloud transcription
  for meetings.
- **Speaker labels on this PC** with NVIDIA Nemotron 3 Diarization
  (`local_diarization.py`). "Add speaker labels" in the Transcript tab asks
  which tracks have several people: call audio only (default), call audio
  plus the microphone (several people around one PC), or the microphone
  only. New meeting has a "several people share this microphone" option
  that sets the default. Each chosen track goes through NeMo-Speech.cpp's
  diarizer with its 30.4-second "v3-offline" geometry, read in 30-second
  blocks. Where two speakers' segments overlap, the one with the higher
  probability in that 10 ms frame keeps it, so labels are exclusive. Every
  word, with its subword pieces and punctuation, gets the speaker whose
  segment overlaps it most; passages split where the speaker changes;
  speakers are numbered Speaker 1, Speaker 2, … across all labelled tracks
  (never two Speaker 1s) until you confirm names, and confirmed names
  survive relabelling. Nothing is uploaded, so there is no disclosure. The
  model is a 107 MB download in Settings › Meetings; without it, passages
  keep their source labels. The runtime DLLs ship in the installer under
  `nemo_speech/`, built by `scripts/Build-NemoSpeechRuntime.ps1` for CPUs
  with AVX2. Ownkey checks for AVX2 before loading them.
- **Review**: My thoughts (autosaved notes, timestamp insertion), Transcript
  (search, playback of both tracks, inline corrections kept as a separate
  revision next to the recognition text, speaker rename and confirmation,
  per-passage reassignment), Summary (overview, decisions marked decided or
  suggested or deferred, action items with unassigned owners left unassigned,
  open questions, every item citing passages), Ask this meeting (answers cite
  passages that exist in the current revision, say when the record cannot
  answer, and use notes only when you tick "Include My thoughts"), Follow-up
  draft (editable, Copy only; there is no send action).
- **Remote disclosure.** Summary, questions and drafts use the rewrite
  provider from Settings. The first remote use shows the provider, model, what
  is sent and what is not; "Don't ask again" stores `meetings_remote_policy =
  allow`. A local endpoint (Ollama, localhost) never asks. Audio never leaves
  the PC.
- **Export** as Markdown (notes, summary, transcript, Q&A, draft as separate
  sections) or JSON (ids, timestamps, revisions, speakers, events, analyses).
- **Delete** cancels jobs, removes rows and the meeting directory, and tells you
  exported copies stay where you put them.
- **Retention**: keep 7 days after transcription (default), keep until deleted,
  or remove right after transcription. A sweep runs at start.
- **Tray, pill and hotkeys**: Meetings runs inside the same Ownkey process,
  with the same tray icon, Settings window, config file, provider adapters
  and Orukeet model manager as dictation. While a meeting records the tray
  icon shows recording, the Meetings submenu offers Pause/Resume and Stop,
  dictation hotkeys show "Meeting recording · mm:ss" in the pill instead of
  recording, and Quit asks whether to stop and save or keep recording. The
  pill announces meeting transitions for two seconds (recording, paused,
  resumed, saved, interrupted) and never interrupts a dictation in progress.
  Dictation comes first: a meeting cannot start while the dictation key is
  held, and meeting transcription pauses between windows while dictation is
  recording or has audio waiting for the decoder, so typing never waits on
  a meeting. There is no separate always-on-top recorder widget; the tray
  and the meeting window carry the controls.

## Config keys

| Key | Default | Meaning |
|---|---|---|
| `meetings_remote_policy` | `ask` | `allow` skips the disclosure for remote text models |
| `meetings_transcription_policy` | `ask` | `allow` skips the disclosure for sending audio to a cloud transcription provider |
| `meetings_audio_provider` | `same` | `same` follows the dictation provider; otherwise `orukeet`, `openai`, `mistral`, `google` or `custom` |
| `meetings_audio_api_key`, `meetings_audio_endpoint`, `meetings_audio_model` | empty | the meeting provider's own settings when it is not `same` |
| `meetings_auto_summary` | `false` | run a summary right after transcription (only when no disclosure is pending) |
| `meetings_auto_speakers` | `false` | label speakers right after transcription (needs the speaker model; skipped when live labels ran) |
| `meetings_retention` | `days7` | default retention for new meetings |

Loading an older config drops `pyannote_api_key`, `meetings_upload_policy` and
`meetings_live_speakers_policy`; the next save removes them from disk.

## Verified on this machine

- 228 automated tests (`py -m unittest discover -s tests`): store, chunk writer,
  capture session (timeline, pause, padding, overrun, dead source), windowing
  and passage building, analysis prompts and citation validation, export,
  service jobs, HTTP API, tray integration, config normalization.
- End to end with the development harness and synthetic speech clips as live
  sources (`py -m meetings --fixture mic=en.wav --fixture system=nl.wav`):
  start, pause, resume, stop, Orukeet transcription of both tracks with
  timestamps, notes, a correction, speaker confirmation, the 409 disclosure,
  a real summary, two questions (one answerable, one refused as not in the
  record), a draft, Markdown and JSON export.
- Orukeet timing: a 40 s clip decodes in about 3 s with per-token timestamps.
- Speaker labels end to end with the real model, the staged runtime and
  Orukeet, through `MeetingService`: the AMI fixture from NeMo-Speech.cpp
  (60 s of meeting EN2002d, CC BY 4.0, with a reference RTTM) played in real
  time as call audio. After Stop, labels took 2.0 s and 98% of transcript
  time went to the right speaker, with three speakers found; the fourth
  says a few words. Diarization error rate on the clip was 23.4% after Stop
  and 24.0% live, without a collar and with overlap included. Speaker
  confusion was 2.3%; the rest is segment timing against a word-aligned
  reference.
- The model download ran through `LocalModelManager` from Hugging Face and
  passed the pinned size and SHA-256 check.

## Not verified here

- **System audio (WASAPI loopback)** could not be exercised in this session:
  the process had no default render endpoint, so `soundcard` could not open a
  loopback device. The code path is written (`SystemAudioSource`) and fails
  with a clear message when no output device is available. Test it on a
  machine with a normal audio session before relying on it; the loopback
  stall-in-silence behaviour is handled by timeline padding, not yet measured.
- Real microphone capture through the meeting window (the harness used
  fixtures so no microphone was opened without you).
- Long meetings (60 to 120 minutes), Bluetooth routing, echo, clock drift
  figures. The spec's validation list still applies.
- Cloud transcription of meetings was exercised with a fake provider in the
  tests only; the real request goes through the same `transcribe_audio`
  adapter dictation uses every day, one call per window.
- Speaker labels were measured on one 60-second English meeting excerpt.
  Long meetings, Dutch speech, more than four speakers, echo between the
  microphone and call audio, and slower laptops have not been measured. The
  runtime keeps frame probabilities for about 20 minutes and folds older
  ones into segments. A 25-minute track, the fixture repeated, went past
  that point in 51 s and kept the same three speakers across all 25
  repeats; a real one-hour meeting has not been tried.
- Labels after Stop for one meeting while another records with live
  speakers share the runtime's compute lock with the live stream. On a slow
  PC that can delay live labels or stop them with the "could not keep up"
  error; recording and transcription continue.
- Linux builds do not include the runtime yet, so speaker labels report it
  as missing there. `OWNKEY_NEMO_SPEECH_DIR` can point at a NeMo-Speech.cpp
  build from commit 97a15af or later.
- The meeting window was exercised with a test copy of the overlay on its
  own UDP port next to the development harness: open, focus, navigate, close
  and reopen, a refused non-local URL, and a Markdown export that landed in
  Downloads. Opening it from the tray of an installed Ownkey, audio playback
  inside it and the focus hand-over from the tray click have not been tried.
- Code-switching without a pause inside one window can still lose words;
  the pause-based windowing only helps when speakers pause between turns.
- Two Ownkey processes on one library. Ownkey has no single-instance guard
  on Windows, and a second process reconciles the library at start: a
  meeting the first process is still recording would be marked interrupted.
  Run a development copy with `OWNKEY_MEETINGS_LIBRARY` set to another
  directory while the installed Ownkey runs.
- Packaging: `Ownkey.spec` bundles `meetings/ui` and the `soundcard` data
  files. An unsigned development installer (0.6.0) was built and its frozen
  `Ownkey.exe --local-smoke-test` passed, but the installer itself was not
  run on a clean machine. The spec also bundles the NeMo-Speech.cpp runtime
  as `nemo_speech/` with its licenses. From the frozen backend,
  `Ownkey.exe --diarization-smoke-test --model-dir … --wav … --output …`
  labelled the AMI fixture with three speakers in 1.9 s. The runtime has not
  been loaded on a CPU without AVX2 or on a PC without the Visual C++
  runtime installed.

## Screenshots

Taken from the real interface in `assets/readme/`, with the development
harness feeding synthetic speech clips as the microphone and call audio, so
the transcripts repeat two test sentences. Everything else in them is what
the app does today. The transcript and New meeting images are the native
window; the other two are the same page captured without a window frame.

| File | What it shows |
|---|---|
| `meetings-recording.png` | A meeting recording both tracks with live meters, Pause and Stop, and notes in My thoughts |
| `meetings-transcript.png` | The Ownkey Meetings window itself, captured from the overlay process: a transcript after speaker labels on a shared microphone and call audio, Speaker 1 to 3 to confirm, search, playback, retention |
| `meetings-summary.png` | Summary with the remote-model line, two questions (one refused for lack of a passage) and a follow-up draft |
| `meetings-new.png` | New meeting in the Ownkey Meetings window: sources, devices with the live input meter, the shared-microphone option, readiness rows and retention |

## Development harness

```powershell
py -m meetings                                   # real devices
py -m meetings --fixture mic=a.wav --fixture system=b.wav --library C:\tmp\lib
```

Prints the window URL. `--no-browser` keeps it headless for API tests.
Set `OWNKEY_DEBUG_MEETINGS=1` to log HTTP requests. `OWNKEY_MEETINGS_LIBRARY`
moves the library for a whole Ownkey process (the app-level tests set it, so
they never open the real library). `OWNKEY_OVERLAY_UDP=127.0.0.1:38499` moves
the channel between Ownkey and its overlay process, so a development copy
can run next to an installed Ownkey; both sides read it. The harness itself
still opens the page in the browser.
