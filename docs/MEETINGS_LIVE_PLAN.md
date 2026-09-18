# Semi-live meeting transcription and speaker activity

Implementation and validation status, 17 September 2026. The first working implementation is present in this checkout. This document records the design, completed checks and remaining limits. Private audio, recognition output, provider events and recording-specific reports stay outside the repository.

## Result

New meetings default to **Transcribe while recording**. Text arrives after pauses and, when live labels are enabled, suitable speaker changes. Stop processes the remaining audio and keeps completed passages and corrections. Disabling the option retains transcription after Stop.

**Show live speaker changes with pyannoteAI** is a separate, optional setting on the New meeting screen. It shows anonymous active speakers, including simultaneous speakers, and supplies turn boundaries to transcription. Users can name speakers. Call audio and shared microphones use separate connections; identities are never merged across sources or reconnects automatically.

Orukeet stays local. Cloud transcription uses the selected provider and asks for permission to upload during recording. Live speaker detection requires a separate permission to stream selected tracks to pyannoteAI. These choices are resolved before recording starts. Changing Settings does not change an active session's provider or key.

## Why transcription previously waited for Stop

Capture already wrote durable five-second audio files, but those files were a storage mechanism. `MeetingService.stop()` started the transcription job, which calculated windows over the complete saved track. There was no recording-time recognition coordinator or live diarization transport. The former pyannote integration uploaded complete tracks to the batch endpoint.

## Implemented architecture

```mermaid
flowchart LR
    A[Microphone and call audio] --> B[Aligned audio blocks]
    B --> C[Durable local chunks]
    B --> D[Optional paced speaker stream]
    C --> E[Bounded audio reader]
    B --> E
    E --> F[Pause and speaker boundary controller]
    D --> F
    D --> G[Active speaker display]
    F --> H[Selected speech recognizer]
    H --> I[Atomic window and passage commit]
    I --> J[Growing transcript]
```

| Area | Implementation |
|---|---|
| Capture | `capture.py` publishes aligned samples to bounded streaming queues and exposes its short unwritten tail. Pause flushes pre-pause audio; Stop while paused discards queued paused input. |
| Boundaries | `segmentation.py` makes causal decisions from available audio and speaker events. Long local dictation shares this controller. |
| Recognition | `live.py` runs outside the batch job queue. Each pass handles at most one window per source, using the shared serialized recognizer or selected cloud adapter. It yields between windows for dictation. |
| Durability | Capture flushes pending audio under its writer lock before a selected range is decoded. Model and network calls run outside that lock. Older ranges are read from intersecting WAV files. |
| Persistence | Schema version 3 adds session options, committed window ranges and speaker turns. One transaction saves passages and advances the contiguous retry cursor. Silence also advances progress. |
| Stable text | Passage IDs include source and owned start sample. Stop and retry do not renumber completed passages. Later batch labels preserve live passage IDs, words and corrections, filling only unambiguous source-labelled passages. |
| Streaming | `streaming.py` owns paced WebSocket transport, event reception, clock mapping and bounded reconnect attempts. `websockets==15.0.1` is included in runtime requirements and the Windows build. |
| Interface | The authenticated `/live` endpoint returns lightweight activity and progress approximately every 300 ms while visible and active. Ordinary detail refreshes remain once per second. Activity updates do not replace the editor. |

Recognition holds one short range at a time; accumulated work is represented by offsets into saved audio. Existing capture queues hold at most about 60 seconds per source, the durable writer tail normally holds less than five seconds, and each speaker queue holds at most four seconds. Meetings do not introduce a second full-recording memory buffer.

## Boundary policy

| Signal | Rule |
|---|---|
| Ordinary pause | Wait for 800 ms of low energy and cut inside the quiet interval. |
| Longer phrase | After 12 seconds, accept a 300 ms pause. |
| Speaker change | Consider different, non-overlapping speakers with 400 ms of following audio available. Reject cuts covered by another active speaker. |
| Continuous speech | Limit the owned range to 27 seconds; choose the quietest frame in its final eight seconds when no pause is available. |
| Context | Orukeet uses up to 600 ms of left context and 200 ms of right context. Input stays under 28 seconds. |
| Brief replies | Completed short turns can flush without a six-second minimum. |
| Silence | Advance the saved cursor and skip silent recognition input. |
| Pause and Stop | Flush the available final partial window. Stop waits for recognition and bounded speaker-event draining. |

The energy threshold uses only preceding available frames, up to ten seconds. Acoustic boundaries do not guarantee sentence completion. Adjacent unfinished passages for the same speaker can display as one paragraph while keeping separately editable IDs. Speaker changes, corrections and pause markers can still create display breaks.

Timed subword tokens are grouped into words before assigning ownership or speakers. Word-group midpoints determine the owning window; intentional repeated words are retained. Overlap protects context, but independently decoded hypotheses can disagree at a seam. This is a timing heuristic, not full hypothesis alignment or a guarantee against every missing or repeated word.

Cloud adapters currently return text without word times. They receive disjoint audio ranges without recognition overlap. Passages show approximate timing; mixed or overlapping speakers retain the source label. Continuous speech can still require a forced cut. No word-level timing precision is implied.

## Speaker transport and Pause

The client requests `/v1/live` and streams mono 16 kHz float32 PCM in paced 100 ms frames. Events update an active set and persist meeting-time turns. Labels are namespaced by source and connection epoch, so reconnecting never silently assigns an old name to a new label. See the [streaming guide](https://docs.pyannote.ai/tutorials/streaming-real-time) and [event schema](https://docs.pyannote.ai/api-reference/streaming).

Pause discards captured microphone and call audio. Open speaker sockets receive generated silence to preserve identities; paused time still counts as streaming usage. The start disclosure explains this. A piecewise clock maps stream time to saved audio time across multiple pauses. Activity is hidden while paused.

A stalled or overflowing stream closes and may reconnect at the current position with fresh labels, with at most three connection attempts per source. Disconnection clears activity and shows an error while pause-based transcription continues. Displayed errors exclude credential-bearing socket URLs. The provider documents eight speakers, a five-second idle limit and a five-hour connection limit. Reconnection creates new identities; it does not guarantee identity continuity across that limit. See [billing](https://docs.pyannote.ai/administration/billing).

Events available when a window finishes determine attribution. Late events do not rewrite committed words or labels automatically. After Stop, optional batch labels can fill an unambiguous source label; they do not reconcile anonymous identities or split existing live passages.

## Recovery, editing and retention

Recognition failures preserve audio, completed passages and a durable retry cursor. Retry after Stop continues at the first uncommitted range. A crash marks capture Interrupted; reopening never starts capture or uploads automatically. Explicit retry uses the currently selected provider and applicable upload permission.

Delete and shutdown cancel live work before releasing resources. Committing checks that the meeting and running job still exist, preventing late results from recreating a deleted meeting. Model attempts close on completion and failure. Audio removal is blocked while transcription or speaker processing still needs it. Automatic removal follows successful processing, including a pending automatic batch speaker pass.

Retention is checked at startup, after audio jobs finish and every minute while Ownkey runs. A busy meeting does not prevent other expired recordings from being removed. Deletion rejects late writes from batch transcription and text analysis as well as live processing. New local recognition jobs follow the model directory selected in Settings; existing attempts keep their current model until they finish.

Notes and corrections remain usable during recording. Polling defers full rendering while an input is focused, preserves earlier scroll positions, and follows new text only when already near the bottom. Status distinguishes listening, transcription, catching up, pause, finalization and failure. Recording does not show a misleading completion percentage.

HTTP control actions consume their JSON bodies before connection reuse. This fixes malformed polling requests found during Pause/Resume browser testing. A new token-bearing meeting URL also takes precedence over a stale cookie from an earlier process.

## Long local dictation

Local dictation longer than 15 seconds uses the same bounded controller and word ownership. Short dictation retains a single decode. The caller receives one complete string for the existing rewrite and insertion flow; no multiple clipboard insertions are introduced. Cloud dictation is unchanged.

## Validation

Repository tests use generated PCM, invented text and fake events. They cover pauses, brief replies, limits, sample coverage, whole-word grouping, overlap uncertainty, multiple-pause clocks, paced float32 transport, final-event draining, live passage arrival, stable corrections, retry, deletion during decoding, separate pre-capture consent, cloud windows, retention and HTTP connection reuse.

Private validation used the supplied recording without adding its name, audio, text, screenshots or provider logs to the repository:

- The actual coordinator processed the complete decoded recording in about 3 minutes 32 seconds, using 402 speech windows. The speech source and an equally long synthetic silent second source had exact contiguous sample coverage. No forced cut was required; the longest owned window was 21.8 seconds. This was accelerated saved-audio processing, not a one-hour real-time hardware soak.
- A real-time two-minute replay through capture, Orukeet and pyannoteAI produced text before Stop, observed two active labels, retained unique IDs and reached the exact final offset on both tracks. It included an eight-second Pause/Resume interval and finalized the speaker socket successfully.
- Browser checks with synthetic sources exercised consent and cancellation, text arrival, speaker activity, note focus and persistence while text arrived, live corrections, Pause/Resume, Stop and playback. Expected consent responses were distinguished from unexpected server errors.
- A Windows PyInstaller build succeeded. Its offline smoke test transcribed a private 60-second clip through the new dictation path, unloaded the idle model and reloaded it successfully. Network requests were disabled for that smoke test.

These checks establish pipeline behavior, bounded recognition input and recoverable audio ownership. They do not establish word error rate or true speaker identity. Word counts and recognition disagreement are not substitutes for a manually checked reference.

## Remaining evaluation

Before claiming an accuracy improvement or a latency service level, create a private human-checked reference covering soft speech, noise, music, overlap, proper nouns, negation and sentence continuations. The energy detector can mistake noise for speech or soft speech for silence. Check forced seams against that reference and consider a speech model or richer hypothesis alignment if the results justify it.

An hour-long real-time replay with two speaking hardware sources, deliberate network faults, native window interaction and resource monitoring remains a release-hardening check. Existing gap placement still uses the original capture queue/padding behavior; this change does not establish hardware clock synchronization. Multi-hour speaker rotation, cross-connection identity reconciliation, passage-delta polling and automatic late-event relabelling remain follow-up work.
