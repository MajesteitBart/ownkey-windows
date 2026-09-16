# Ownkey Meetings: what is built

Status on 16 September 2026. This describes the code in `meetings/` and its
tray integration, against the proposal in `MEETINGS_SPEC.md`. It is a first
usable slice, not the whole spec.

## What works

- **Recording** from the tray: Meetings › New meeting opens the meeting window
  in the default browser (a local page on 127.0.0.1 with a per-process token).
  Microphone, system audio or both, with device pickers, a retention choice and
  the "let everyone know you are recording" reminder. Nothing is written before
  Start.
- **Durable capture.** Each source is its own mono 16 kHz track, written in
  five-second WAV chunks (`%LOCALAPPDATA%\Ownkey\meetings\<meeting>\audio\<source>`)
  that are flushed and renamed before their row is committed to `library.db`.
  Pause discards new audio and is stored as an event, so transcript times equal
  playback times. A silent loopback is padded from the timeline, an overrun
  becomes a visible gap event, a dead source or a write error ends capture and
  marks the meeting interrupted. A crash is reconciled on the next start: the
  meeting is marked interrupted, chunk rows without files are dropped, and
  Ownkey notifies you. Recording never resumes on its own.
- **Transcription on this PC** with the installed Orukeet model, after Stop.
  Tracks are decoded in windows that end at the quietest moment before 28 s;
  token timestamps become passages (`p0001`, `p0002`, …) with source labels
  Microphone and Call audio. Passages appear while the job runs. If Orukeet is
  not installed the job fails with a message and the meeting keeps its audio;
  there is no download and no cloud transcription for meetings.
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
- **Tray and hotkeys**: while a meeting records the tray icon shows recording,
  the Meetings submenu offers Pause/Resume and Stop, dictation hotkeys show
  "Meeting recording · mm:ss" in the pill instead of recording, and Quit asks
  whether to stop and save or keep recording.

## Config keys

| Key | Default | Meaning |
|---|---|---|
| `meetings_remote_policy` | `ask` | `allow` skips the disclosure for remote text models |
| `meetings_auto_summary` | `false` | run a summary right after transcription (only when no disclosure is pending) |
| `meetings_retention` | `days7` | default retention for new meetings |

## Verified on this machine

- 190 automated tests (`py -m unittest discover -s tests`): store, chunk writer,
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
- Automatic speaker labels (diarization) are not implemented; labels are
  Microphone and Call audio, renamable and confirmable.
- Packaging: `Ownkey.spec` now bundles `meetings/ui` and the `soundcard`
  data files, but no installer was built in this session.

## Development harness

```powershell
py -m meetings                                   # real devices
py -m meetings --fixture mic=a.wav --fixture system=b.wav --library C:\tmp\lib
```

Prints the window URL. `--no-browser` keeps it headless for API tests.
Set `OWNKEY_DEBUG_MEETINGS=1` to log HTTP requests.
