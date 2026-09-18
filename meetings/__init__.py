"""Ownkey Meetings: local recording, transcription and analysis of conversations.

Modules:
    store          SQLite library for meetings, chunks, passages, notes, analyses, jobs
    audio          WAV helpers, resampling, the durable chunk writer
    capture        Microphone and system-audio capture with pause, stop and recovery
    transcription  Orukeet decoding into timed passages
    analysis       Summary, questions and follow-up drafts with passage citations
    export         Markdown and JSON export
    service        The meeting service that ties capture, jobs and the store together
    server         The local HTTP API and static UI used by the meeting window
"""
