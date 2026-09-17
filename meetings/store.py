"""SQLite library for meetings.

Audio chunks live as WAV files next to the database; every other record is a
row here. Chunk files become durable before their rows commit, so the file
system is the source of truth for audio and the database for everything else.
"""

from __future__ import annotations

import json
import os
import secrets
import sqlite3
import threading
import time
import wave
from pathlib import Path

SCHEMA_VERSION = 3

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS live_sessions (
    meeting_id TEXT PRIMARY KEY,
    options TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS transcription_windows (
    meeting_id TEXT NOT NULL,
    source TEXT NOT NULL,
    start_sample INTEGER NOT NULL,
    end_sample INTEGER NOT NULL,
    reason TEXT NOT NULL,
    PRIMARY KEY (meeting_id, source, start_sample)
);
CREATE TABLE IF NOT EXISTS speaker_turns (
    meeting_id TEXT NOT NULL,
    source TEXT NOT NULL,
    speaker_id TEXT NOT NULL,
    start REAL NOT NULL,
    end REAL,
    PRIMARY KEY (meeting_id, speaker_id, start)
);
CREATE TABLE IF NOT EXISTS meetings (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    state TEXT NOT NULL,
    elapsed REAL NOT NULL DEFAULT 0,
    sources TEXT NOT NULL DEFAULT '{}',
    retention TEXT NOT NULL DEFAULT 'days7',
    audio_state TEXT NOT NULL DEFAULT 'kept',
    transcript_rev INTEGER NOT NULL DEFAULT 0,
    transcribed_at REAL,
    engine TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS chunks (
    meeting_id TEXT NOT NULL,
    source TEXT NOT NULL,
    seq INTEGER NOT NULL,
    start_sample INTEGER NOT NULL,
    n_samples INTEGER NOT NULL,
    path TEXT NOT NULL,
    PRIMARY KEY (meeting_id, source, seq)
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    meeting_id TEXT NOT NULL,
    at REAL NOT NULL,
    wall REAL NOT NULL,
    kind TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS speakers (
    meeting_id TEXT NOT NULL,
    id TEXT NOT NULL,
    source TEXT NOT NULL,
    name TEXT NOT NULL,
    confirmed INTEGER NOT NULL DEFAULT 0,
    position INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (meeting_id, id)
);
CREATE TABLE IF NOT EXISTS passages (
    meeting_id TEXT NOT NULL,
    id TEXT NOT NULL,
    position INTEGER NOT NULL,
    source TEXT NOT NULL,
    speaker_id TEXT NOT NULL,
    start REAL NOT NULL,
    "end" REAL NOT NULL,
    text TEXT NOT NULL,
    corrected TEXT,
    quality TEXT NOT NULL DEFAULT '',
    rev INTEGER NOT NULL DEFAULT 1,
    tokens TEXT,
    PRIMARY KEY (meeting_id, id)
);
CREATE TABLE IF NOT EXISTS notes (
    meeting_id TEXT PRIMARY KEY,
    content TEXT NOT NULL DEFAULT '',
    rev INTEGER NOT NULL DEFAULT 0,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS analyses (
    id TEXT PRIMARY KEY,
    meeting_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    created_at REAL NOT NULL,
    provider TEXT NOT NULL DEFAULT '',
    model TEXT NOT NULL DEFAULT '',
    input_rev INTEGER NOT NULL DEFAULT 0,
    include_notes INTEGER NOT NULL DEFAULT 0,
    question TEXT NOT NULL DEFAULT '',
    content TEXT NOT NULL DEFAULT '',
    refs TEXT NOT NULL DEFAULT '[]',
    state TEXT NOT NULL DEFAULT 'done',
    error TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    meeting_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    state TEXT NOT NULL,
    progress REAL NOT NULL DEFAULT 0,
    detail TEXT NOT NULL DEFAULT '',
    error TEXT NOT NULL DEFAULT '',
    attempts INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_passages_meeting ON passages(meeting_id, position);
CREATE INDEX IF NOT EXISTS idx_events_meeting ON events(meeting_id, id);
CREATE INDEX IF NOT EXISTS idx_analyses_meeting ON analyses(meeting_id, created_at);
CREATE INDEX IF NOT EXISTS idx_jobs_meeting ON jobs(meeting_id, created_at);
"""

MEETING_STATES = ("recording", "paused", "stopped", "interrupted")
RETENTION_CHOICES = ("days7", "keep", "after_transcription")
RETENTION_DAYS = 7


def default_library_root() -> Path:
    """%LOCALAPPDATA%\Ownkey\meetings, or OWNKEY_MEETINGS_LIBRARY when set (tests and
    a second Ownkey run next to the installed one must not touch the real library)."""
    override = os.environ.get("OWNKEY_MEETINGS_LIBRARY", "").strip()
    if override:
        return Path(override)
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return Path(base) / "Ownkey" / "meetings"


def new_id(prefix: str) -> str:
    return f"{prefix}-{secrets.token_hex(6)}"


def _row(cursor_row) -> dict | None:
    return dict(cursor_row) if cursor_row is not None else None


def _tokens_json(tokens) -> str | None:
    """Token timing as compact JSON: [[text, start, end], ...]."""
    if not tokens:
        return None
    return json.dumps([[str(t[0]), round(float(t[1]), 2), round(float(t[2]), 2)] for t in tokens], ensure_ascii=False)


def _tokens_load(value) -> list | None:
    if not value:
        return None
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return None


class MeetingStore:
    """Thread-safe access to the meeting library.

    One connection guarded by a lock is enough: writes are small and the UI
    polls a few times per second at most.
    """

    def __init__(self, root: str | os.PathLike | None = None, *, clock=time.time):
        self.root = Path(root) if root is not None else default_library_root()
        self.root.mkdir(parents=True, exist_ok=True)
        self.db_path = self.root / "library.db"
        self._clock = clock
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        with self._lock:
            self._conn.executescript(SCHEMA)
            columns = {row["name"] for row in self._conn.execute("PRAGMA table_info(passages)")}
            if "tokens" not in columns:  # libraries created before speaker labels
                self._conn.execute("ALTER TABLE passages ADD COLUMN tokens TEXT")
            self._conn.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES ('schema_version', ?)", (str(SCHEMA_VERSION),)
            )

    # ── housekeeping ───────────────────────────────────────────────
    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def meeting_dir(self, meeting_id: str) -> Path:
        return self.root / meeting_id

    def audio_dir(self, meeting_id: str, source: str) -> Path:
        return self.meeting_dir(meeting_id) / "audio" / source

    def _tx(self):
        return _Transaction(self._conn, self._lock)

    # ── meetings ───────────────────────────────────────────────────
    def create_meeting(self, title: str, sources: dict, retention: str = "days7", engine: str = "") -> dict:
        if retention not in RETENTION_CHOICES:
            retention = "days7"
        meeting_id = new_id("m")
        now = self._clock()
        with self._tx():
            self._conn.execute(
                "INSERT INTO meetings(id, title, created_at, updated_at, state, elapsed, sources, retention, engine)"
                " VALUES (?, ?, ?, ?, 'recording', 0, ?, ?, ?)",
                (meeting_id, title.strip(), now, now, json.dumps(sources), retention, engine),
            )
            self._conn.execute(
                "INSERT INTO notes(meeting_id, content, rev, updated_at) VALUES (?, '', 0, ?)", (meeting_id, now)
            )
        self.meeting_dir(meeting_id).mkdir(parents=True, exist_ok=True)
        return self.get_meeting(meeting_id)

    def get_meeting(self, meeting_id: str) -> dict | None:
        with self._lock:
            row = _row(self._conn.execute("SELECT * FROM meetings WHERE id = ?", (meeting_id,)).fetchone())
        if row:
            row["sources"] = json.loads(row["sources"] or "{}")
        return row

    def list_meetings(self) -> list[dict]:
        with self._lock:
            rows = [dict(r) for r in self._conn.execute("SELECT * FROM meetings ORDER BY created_at DESC")]
        for row in rows:
            row["sources"] = json.loads(row["sources"] or "{}")
        return rows

    def update_meeting(self, meeting_id: str, **fields) -> None:
        if not fields:
            return
        if "sources" in fields:
            fields["sources"] = json.dumps(fields["sources"])
        if "state" in fields and fields["state"] not in MEETING_STATES:
            raise ValueError(f"Unknown meeting state {fields['state']!r}")
        fields["updated_at"] = self._clock()
        columns = ", ".join(f"{key} = ?" for key in fields)
        with self._tx():
            self._conn.execute(f"UPDATE meetings SET {columns} WHERE id = ?", (*fields.values(), meeting_id))

    def delete_meeting(self, meeting_id: str) -> None:
        with self._tx():
            for table in ("chunks", "events", "speakers", "passages", "notes", "analyses", "jobs",
                          "live_sessions", "transcription_windows", "speaker_turns"):
                self._conn.execute(f"DELETE FROM {table} WHERE meeting_id = ?", (meeting_id,))
            self._conn.execute("DELETE FROM meetings WHERE id = ?", (meeting_id,))

    # ── chunks and events ──────────────────────────────────────────
    def add_chunk(self, meeting_id: str, source: str, seq: int, start_sample: int, n_samples: int, path: str) -> None:
        with self._tx():
            self._conn.execute(
                "INSERT OR REPLACE INTO chunks(meeting_id, source, seq, start_sample, n_samples, path)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (meeting_id, source, seq, start_sample, n_samples, path),
            )

    def list_chunks(self, meeting_id: str, source: str | None = None) -> list[dict]:
        with self._lock:
            if source is None:
                rows = self._conn.execute(
                    "SELECT * FROM chunks WHERE meeting_id = ? ORDER BY source, seq", (meeting_id,)
                )
            else:
                rows = self._conn.execute(
                    "SELECT * FROM chunks WHERE meeting_id = ? AND source = ? ORDER BY seq", (meeting_id, source)
                )
            return [dict(r) for r in rows]

    def delete_chunks(self, meeting_id: str) -> None:
        with self._tx():
            self._conn.execute("DELETE FROM chunks WHERE meeting_id = ?", (meeting_id,))

    def read_audio(self, meeting_id: str, source: str, start: int, end: int):
        """Read a bounded sample range without concatenating a whole meeting."""
        import numpy as np

        if end <= start:
            return np.zeros(0, dtype=np.int16)
        with self._lock:
            rows = [dict(r) for r in self._conn.execute(
                "SELECT * FROM chunks WHERE meeting_id=? AND source=? AND start_sample < ? "
                "AND start_sample+n_samples > ? ORDER BY seq", (meeting_id, source, end, start))]
        result = np.zeros(end - start, dtype=np.int16)
        for row in rows:
            a, b = max(start, row['start_sample']), min(end, row['start_sample'] + row['n_samples'])
            with wave.open(row['path'], 'rb') as handle:
                handle.setpos(a - row['start_sample'])
                samples = np.frombuffer(handle.readframes(b - a), dtype='<i2')
                if samples.size != b - a:
                    raise OSError('A saved audio chunk is incomplete. The remaining audio is kept.')
                result[a - start:b - start] = samples
        return result

    def set_live_options(self, meeting_id: str, options: dict) -> None:
        with self._tx():
            self._conn.execute('INSERT OR REPLACE INTO live_sessions VALUES (?, ?)',
                               (meeting_id, json.dumps(options)))

    def live_options(self, meeting_id: str) -> dict | None:
        with self._lock:
            row = self._conn.execute('SELECT options FROM live_sessions WHERE meeting_id=?', (meeting_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def processed_samples(self, meeting_id: str, source: str) -> int:
        with self._lock:
            return int(self._conn.execute('SELECT COALESCE(MAX(end_sample),0) FROM transcription_windows '
                                          'WHERE meeting_id=? AND source=?', (meeting_id, source)).fetchone()[0])

    def commit_window(self, meeting_id: str, job_id: str, source: str, start: int, end: int,
                      reason: str, passages: list[dict]) -> bool:
        """One transaction owns the audio range, its text, and the retry cursor."""
        with self._tx():
            meeting = self._conn.execute('SELECT transcript_rev FROM meetings WHERE id=?', (meeting_id,)).fetchone()
            job = self._conn.execute('SELECT state FROM jobs WHERE id=?', (job_id,)).fetchone()
            if not meeting or not job or job[0] != 'running':
                return False
            if self.processed_samples(meeting_id, source) != start:
                return False
            self._conn.execute('INSERT INTO transcription_windows VALUES (?, ?, ?, ?, ?)',
                               (meeting_id, source, start, end, reason))
            position = self._conn.execute('SELECT COALESCE(MAX(position),-1)+1 FROM passages WHERE meeting_id=?',
                                          (meeting_id,)).fetchone()[0]
            rev = meeting[0] + 1
            for i, p in enumerate(passages):
                pid = f'{source}-{start:012d}-{i:03d}'
                self._conn.execute('INSERT INTO passages '
                    '(meeting_id,id,position,source,speaker_id,start,"end",text,quality,rev,tokens) '
                    'VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                    (meeting_id, pid, position+i, source, p['speaker_id'], p['start'], p['end'], p['text'],
                     p.get('quality','timed'), rev, _tokens_json(p.get('tokens'))))
            if passages:
                self._conn.execute('UPDATE meetings SET transcript_rev=?,updated_at=? WHERE id=?',
                                   (rev, self._clock(), meeting_id))
            return True

    def speaker_turn(self, meeting_id: str, source: str, speaker_id: str, at: float, starting: bool) -> None:
        with self._tx():
            if not self._conn.execute('SELECT 1 FROM meetings WHERE id=?', (meeting_id,)).fetchone():
                return
            if starting:
                self._conn.execute('INSERT OR IGNORE INTO speaker_turns VALUES (?,?,?,?,NULL)',
                                   (meeting_id, source, speaker_id, at))
            else:
                self._conn.execute('UPDATE speaker_turns SET end=MAX(start,?) '
                                   'WHERE meeting_id=? AND speaker_id=? AND end IS NULL', (at, meeting_id, speaker_id))

    def speaker_segments(self, meeting_id: str, source: str, start: float, end: float) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self._conn.execute(
                'SELECT speaker_id AS speaker,start,COALESCE(end,?) AS end FROM speaker_turns '
                'WHERE meeting_id=? AND source=? AND start < ? AND (end IS NULL OR end > ?) ORDER BY start',
                (end, meeting_id, source, end, start))]

    def add_event(self, meeting_id: str, at: float, kind: str, detail: str = "") -> None:
        with self._tx():
            self._conn.execute(
                "INSERT INTO events(meeting_id, at, wall, kind, detail) VALUES (?, ?, ?, ?, ?)",
                (meeting_id, float(at), self._clock(), kind, detail),
            )

    def list_events(self, meeting_id: str) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self._conn.execute(
                "SELECT * FROM events WHERE meeting_id = ? ORDER BY id", (meeting_id,))]

    # ── notes ──────────────────────────────────────────────────────
    def get_notes(self, meeting_id: str) -> dict:
        with self._lock:
            row = _row(self._conn.execute("SELECT * FROM notes WHERE meeting_id = ?", (meeting_id,)).fetchone())
        return row or {"meeting_id": meeting_id, "content": "", "rev": 0, "updated_at": 0.0}

    def save_notes(self, meeting_id: str, content: str) -> dict:
        now = self._clock()
        with self._tx():
            self._conn.execute(
                "INSERT INTO notes(meeting_id, content, rev, updated_at) VALUES (?, ?, 1, ?)"
                " ON CONFLICT(meeting_id) DO UPDATE SET content = excluded.content, rev = notes.rev + 1,"
                " updated_at = excluded.updated_at",
                (meeting_id, content, now),
            )
        return self.get_notes(meeting_id)

    # ── speakers ───────────────────────────────────────────────────
    def ensure_speaker(self, meeting_id: str, speaker_id: str, source: str, name: str, confirmed: bool = False) -> dict:
        with self._tx():
            if not self._conn.execute('SELECT 1 FROM meetings WHERE id=?', (meeting_id,)).fetchone():
                return None
            existing = self._conn.execute(
                "SELECT * FROM speakers WHERE meeting_id = ? AND id = ?", (meeting_id, speaker_id)).fetchone()
            if existing is None:
                position = self._conn.execute(
                    "SELECT COUNT(*) FROM speakers WHERE meeting_id = ?", (meeting_id,)).fetchone()[0]
                self._conn.execute(
                    "INSERT INTO speakers(meeting_id, id, source, name, confirmed, position) VALUES (?, ?, ?, ?, ?, ?)",
                    (meeting_id, speaker_id, source, name, int(confirmed), position),
                )
        return self.get_speaker(meeting_id, speaker_id)

    def get_speaker(self, meeting_id: str, speaker_id: str) -> dict | None:
        with self._lock:
            return _row(self._conn.execute(
                "SELECT * FROM speakers WHERE meeting_id = ? AND id = ?", (meeting_id, speaker_id)).fetchone())

    def order_speakers(self, meeting_id: str, ordered_ids: list[str]) -> None:
        """Puts the given speakers last, in the given order; the others keep their relative order."""
        with self._tx():
            rows = self._conn.execute(
                "SELECT id FROM speakers WHERE meeting_id = ? ORDER BY position", (meeting_id,)).fetchall()
            rest = [r["id"] for r in rows if r["id"] not in ordered_ids]
            for position, speaker_id in enumerate(rest + list(ordered_ids)):
                self._conn.execute("UPDATE speakers SET position = ? WHERE meeting_id = ? AND id = ?",
                                   (position, meeting_id, speaker_id))

    def list_speakers(self, meeting_id: str) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self._conn.execute(
                "SELECT * FROM speakers WHERE meeting_id = ? ORDER BY position", (meeting_id,))]

    def rename_speaker(self, meeting_id: str, speaker_id: str, name: str, confirmed: bool | None = None) -> dict | None:
        name = " ".join(str(name).split())
        if not name:
            raise ValueError("A speaker needs a name.")
        with self._tx():
            if confirmed is None:
                self._conn.execute("UPDATE speakers SET name = ? WHERE meeting_id = ? AND id = ?",
                                   (name, meeting_id, speaker_id))
            else:
                self._conn.execute("UPDATE speakers SET name = ?, confirmed = ? WHERE meeting_id = ? AND id = ?",
                                   (name, int(confirmed), meeting_id, speaker_id))
            self._conn.execute("UPDATE meetings SET updated_at = ? WHERE id = ?", (self._clock(), meeting_id))
        return self.get_speaker(meeting_id, speaker_id)

    # ── passages ───────────────────────────────────────────────────
    def replace_passages(self, meeting_id: str, passages: list[dict], rev: int) -> None:
        """Store recognition output as the base revision, keeping corrections by id."""
        with self._tx():
            existing, by_content = {}, {}
            for r in self._conn.execute("SELECT * FROM passages WHERE meeting_id = ?", (meeting_id,)):
                existing[r["id"]] = dict(r)
                by_content[(r["source"], round(float(r["start"]), 2), r["text"])] = dict(r)
            self._conn.execute("DELETE FROM passages WHERE meeting_id = ?", (meeting_id,))
            for position, passage in enumerate(passages):
                old = existing.get(passage["id"])
                if not (old and old.get("text") == passage["text"]):
                    # ids shift when passages are split; the same words at the
                    # same time still carry their correction and speaker
                    old = by_content.get((passage["source"], round(float(passage["start"]), 2), passage["text"]))
                same_text = bool(old and old.get("text") == passage["text"])
                corrected = old["corrected"] if same_text else None
                speaker_id = old["speaker_id"] if same_text and not passage.get("speaker_labelled") else passage["speaker_id"]
                self._conn.execute(
                    'INSERT INTO passages(meeting_id, id, position, source, speaker_id, start, "end", text,'
                    " corrected, quality, rev, tokens) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (meeting_id, passage["id"], position, passage["source"], speaker_id,
                     float(passage["start"]), float(passage["end"]), passage["text"], corrected,
                     passage.get("quality", ""), int(rev), _tokens_json(passage.get("tokens"))),
                )
            self._conn.execute("UPDATE meetings SET transcript_rev = ?, updated_at = ? WHERE id = ?",
                               (int(rev), self._clock(), meeting_id))

    def append_passages(self, meeting_id: str, passages: list[dict], rev: int) -> None:
        """Add passages as a transcription job produces them, in order."""
        with self._tx():
            position = self._conn.execute(
                "SELECT COALESCE(MAX(position), -1) + 1 FROM passages WHERE meeting_id = ?", (meeting_id,)).fetchone()[0]
            for offset, passage in enumerate(passages):
                self._conn.execute(
                    'INSERT OR REPLACE INTO passages(meeting_id, id, position, source, speaker_id, start, "end", text,'
                    " corrected, quality, rev, tokens) VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?)",
                    (meeting_id, passage["id"], position + offset, passage["source"], passage["speaker_id"],
                     float(passage["start"]), float(passage["end"]), passage["text"], passage.get("quality", ""), int(rev),
                     _tokens_json(passage.get("tokens"))),
                )

    def clear_passages(self, meeting_id: str) -> None:
        with self._tx():
            self._conn.execute("DELETE FROM passages WHERE meeting_id = ?", (meeting_id,))

    def list_passages(self, meeting_id: str) -> list[dict]:
        with self._lock:
            rows = [dict(r) for r in self._conn.execute(
                "SELECT * FROM passages WHERE meeting_id = ? ORDER BY start, position", (meeting_id,))]
        for row in rows:
            row["tokens"] = _tokens_load(row.get("tokens"))
        return rows

    def get_passage(self, meeting_id: str, passage_id: str) -> dict | None:
        with self._lock:
            return _row(self._conn.execute(
                "SELECT * FROM passages WHERE meeting_id = ? AND id = ?", (meeting_id, passage_id)).fetchone())

    def correct_passage(self, meeting_id: str, passage_id: str, corrected: str | None) -> dict | None:
        """Keep the recognition text; store the user's wording as a separate revision."""
        with self._tx():
            row = self._conn.execute(
                "SELECT * FROM passages WHERE meeting_id = ? AND id = ?", (meeting_id, passage_id)).fetchone()
            if row is None:
                return None
            value = None if corrected is None or corrected.strip() == row["text"].strip() else corrected.strip()
            rev = self._bump_rev(meeting_id)
            self._conn.execute("UPDATE passages SET corrected = ?, rev = ? WHERE meeting_id = ? AND id = ?",
                               (value, rev, meeting_id, passage_id))
        return self.get_passage(meeting_id, passage_id)

    def assign_passage_speaker(self, meeting_id: str, passage_id: str, speaker_id: str) -> dict | None:
        with self._tx():
            if self.get_speaker(meeting_id, speaker_id) is None:
                raise ValueError("Unknown speaker for this meeting.")
            rev = self._bump_rev(meeting_id)
            self._conn.execute("UPDATE passages SET speaker_id = ?, rev = ? WHERE meeting_id = ? AND id = ?",
                               (speaker_id, rev, meeting_id, passage_id))
        return self.get_passage(meeting_id, passage_id)

    def _bump_rev(self, meeting_id: str) -> int:
        rev = self._conn.execute("SELECT transcript_rev FROM meetings WHERE id = ?", (meeting_id,)).fetchone()[0] + 1
        self._conn.execute("UPDATE meetings SET transcript_rev = ?, updated_at = ? WHERE id = ?",
                           (rev, self._clock(), meeting_id))
        return rev

    def bump_transcript_rev(self, meeting_id: str) -> int:
        with self._tx():
            return self._bump_rev(meeting_id)

    # ── analyses ───────────────────────────────────────────────────
    def add_analysis(self, meeting_id: str, kind: str, *, provider: str, model: str, input_rev: int,
                     include_notes: bool, content: str, refs: list[str], question: str = "",
                     state: str = "done", error: str = "") -> dict:
        analysis_id = new_id("a")
        with self._tx():
            self._conn.execute(
                "INSERT INTO analyses(id, meeting_id, kind, created_at, provider, model, input_rev, include_notes,"
                " question, content, refs, state, error) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (analysis_id, meeting_id, kind, self._clock(), provider, model, int(input_rev), int(include_notes),
                 question, content, json.dumps(list(refs)), state, error),
            )
        return self.get_analysis(analysis_id)

    def get_analysis(self, analysis_id: str) -> dict | None:
        with self._lock:
            row = _row(self._conn.execute("SELECT * FROM analyses WHERE id = ?", (analysis_id,)).fetchone())
        if row:
            row["refs"] = json.loads(row["refs"] or "[]")
        return row

    def list_analyses(self, meeting_id: str, kind: str | None = None) -> list[dict]:
        with self._lock:
            if kind is None:
                rows = self._conn.execute(
                    "SELECT * FROM analyses WHERE meeting_id = ? ORDER BY created_at", (meeting_id,))
            else:
                rows = self._conn.execute(
                    "SELECT * FROM analyses WHERE meeting_id = ? AND kind = ? ORDER BY created_at", (meeting_id, kind))
            rows = [dict(r) for r in rows]
        for row in rows:
            row["refs"] = json.loads(row["refs"] or "[]")
        return rows

    def latest_analysis(self, meeting_id: str, kind: str) -> dict | None:
        rows = self.list_analyses(meeting_id, kind)
        return rows[-1] if rows else None

    def delete_analysis(self, analysis_id: str) -> None:
        with self._tx():
            self._conn.execute("DELETE FROM analyses WHERE id = ?", (analysis_id,))

    # ── jobs ───────────────────────────────────────────────────────
    def create_job(self, meeting_id: str, kind: str, detail: str = "") -> dict:
        job_id = new_id("j")
        now = self._clock()
        with self._tx():
            self._conn.execute(
                "INSERT INTO jobs(id, meeting_id, kind, state, progress, detail, error, attempts, created_at, updated_at)"
                " VALUES (?, ?, ?, 'queued', 0, ?, '', 0, ?, ?)",
                (job_id, meeting_id, kind, detail, now, now),
            )
        return self.get_job(job_id)

    def update_job(self, job_id: str, **fields) -> dict | None:
        fields["updated_at"] = self._clock()
        columns = ", ".join(f"{key} = ?" for key in fields)
        with self._tx():
            self._conn.execute(f"UPDATE jobs SET {columns} WHERE id = ?", (*fields.values(), job_id))
        return self.get_job(job_id)

    def get_job(self, job_id: str) -> dict | None:
        with self._lock:
            return _row(self._conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone())

    def list_jobs(self, meeting_id: str | None = None, states: tuple[str, ...] | None = None) -> list[dict]:
        query = "SELECT * FROM jobs"
        clauses, params = [], []
        if meeting_id is not None:
            clauses.append("meeting_id = ?")
            params.append(meeting_id)
        if states:
            clauses.append("state IN (%s)" % ",".join("?" * len(states)))
            params.extend(states)
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY created_at"
        with self._lock:
            return [dict(r) for r in self._conn.execute(query, params)]

    # ── recovery and retention ─────────────────────────────────────
    def reconcile_on_start(self) -> list[str]:
        """Mark meetings that were recording when the process died, and drop
        chunk rows whose files vanished. Returns the interrupted meeting ids."""
        interrupted = []
        with self._tx():
            for row in self._conn.execute("SELECT id FROM meetings WHERE state IN ('recording', 'paused')"):
                interrupted.append(row["id"])
                self._conn.execute("UPDATE meetings SET state = 'interrupted', updated_at = ? WHERE id = ?",
                                   (self._clock(), row["id"]))
                self._conn.execute(
                    "INSERT INTO events(meeting_id, at, wall, kind, detail) VALUES (?, ?, ?, 'interrupted',"
                    " 'Ownkey was not running when this meeting ended')",
                    (row["id"], self._elapsed(row["id"]), self._clock()),
                )
            for row in self._conn.execute("SELECT meeting_id, source, seq, path FROM chunks"):
                if not os.path.exists(row["path"]):
                    self._conn.execute("DELETE FROM chunks WHERE meeting_id = ? AND source = ? AND seq = ?",
                                       (row["meeting_id"], row["source"], row["seq"]))
            for row in self._conn.execute("SELECT id FROM jobs WHERE state IN ('queued', 'running')"):
                self._conn.execute("UPDATE jobs SET state = 'interrupted', updated_at = ? WHERE id = ?",
                                   (self._clock(), row["id"]))
        return interrupted

    def _elapsed(self, meeting_id: str) -> float:
        row = self._conn.execute("SELECT elapsed FROM meetings WHERE id = ?", (meeting_id,)).fetchone()
        return float(row[0]) if row else 0.0

    def audio_expired(self, meeting: dict, now: float | None = None) -> bool:
        """True when the retention policy says this meeting's audio can go."""
        if meeting.get("audio_state") != "kept":
            return False
        if meeting.get("retention") == "keep":
            return False
        if meeting.get("state") in ("recording", "paused", "interrupted"):
            return False
        transcribed_at = meeting.get("transcribed_at")
        if not transcribed_at:
            return False
        if meeting.get("retention") == "after_transcription":
            return True
        now = self._clock() if now is None else now
        return now - float(transcribed_at) >= RETENTION_DAYS * 86400


class _Transaction:
    def __init__(self, conn, lock):
        self._conn, self._lock = conn, lock

    def __enter__(self):
        self._lock.acquire()
        self._conn.execute("BEGIN IMMEDIATE")
        return self._conn

    def __exit__(self, exc_type, exc, tb):
        try:
            if exc_type is None:
                self._conn.execute("COMMIT")
            else:
                self._conn.execute("ROLLBACK")
        finally:
            self._lock.release()
        return False
