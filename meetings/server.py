"""Local HTTP API and static UI for the meeting window.

Binds to 127.0.0.1 on a random port with a per-process token. The page is
opened with the token in the URL once; it is kept in a cookie so the audio
element and every API call carry it. Nothing here is reachable from other
machines, and there is no UDP: every command is a request with a response.
"""

from __future__ import annotations

import json
import mimetypes
import os
import re
import secrets
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .service import ConsentRequired, MeetingError

UI_DIR = Path(__file__).resolve().parent / "ui"
COOKIE = "ownkey_meetings"


class MeetingServer:
    def __init__(self, service, *, host: str = "127.0.0.1", port: int = 0, token: str | None = None):
        self.service = service
        self.host = host
        self.token = token or secrets.token_urlsafe(24)
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self.port = port

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    @property
    def url(self) -> str:
        return f"{self.base_url}/?token={self.token}"

    def start(self) -> str:
        if self._httpd is not None:
            return self.url
        server = self

        class Handler(_Handler):
            owner = server

        self._httpd = ThreadingHTTPServer((self.host, self.port), Handler)
        self._httpd.daemon_threads = True
        self.port = self._httpd.server_address[1]
        self._thread = threading.Thread(target=self._httpd.serve_forever, kwargs={"poll_interval": 0.25},
                                        daemon=True, name="meeting-http")
        self._thread.start()
        return self.url

    def stop(self) -> None:
        httpd, self._httpd = self._httpd, None
        if httpd is not None:
            httpd.shutdown()
            httpd.server_close()


ROUTES = [
    ("GET", r"/api/status", "status"),
    ("GET", r"/api/devices", "devices"),
    ("GET", r"/api/meetings", "list_meetings"),
    ("POST", r"/api/meetings", "create_meeting"),
    ("GET", r"/api/meetings/(?P<mid>[\w-]+)", "meeting"),
    ("PUT", r"/api/meetings/(?P<mid>[\w-]+)", "rename"),
    ("DELETE", r"/api/meetings/(?P<mid>[\w-]+)", "delete"),
    ("POST", r"/api/meetings/(?P<mid>[\w-]+)/(?P<action>pause|resume|stop|transcribe|speakers|summary|draft|ask|remove-audio)", "action"),
    ("PUT", r"/api/meetings/(?P<mid>[\w-]+)/notes", "notes"),
    ("PUT", r"/api/meetings/(?P<mid>[\w-]+)/passages/(?P<pid>[\w-]+)", "passage"),
    ("PUT", r"/api/meetings/(?P<mid>[\w-]+)/speakers/(?P<sid>[\w-]+)", "speaker"),
    ("GET", r"/api/meetings/(?P<mid>[\w-]+)/export", "export"),
    ("GET", r"/api/meetings/(?P<mid>[\w-]+)/audio/(?P<source>\w+)\.wav", "audio"),
    ("POST", r"/api/policy", "policy"),
    ("POST", r"/api/settings/open", "open_settings"),
]
COMPILED = [(method, re.compile("^" + pattern + "$"), name) for method, pattern, name in ROUTES]


class _Handler(BaseHTTPRequestHandler):
    owner: MeetingServer
    protocol_version = "HTTP/1.1"

    # ── plumbing ───────────────────────────────────────────────────
    def log_message(self, format, *args):  # noqa: A002 - BaseHTTPRequestHandler signature
        if os.environ.get("OWNKEY_DEBUG_MEETINGS"):
            super().log_message(format, *args)

    def _json(self, status: int, payload) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _bytes(self, status: int, body: bytes, content_type: str, extra: dict | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8") or "{}")
        except ValueError as exc:
            raise MeetingError("Malformed JSON body.") from exc
        return data if isinstance(data, dict) else {}

    def _cookie_token(self) -> str:
        header = self.headers.get("Cookie") or ""
        for part in header.split(";"):
            name, _, value = part.strip().partition("=")
            if name == COOKIE:
                return value.strip()
        return ""

    def _authorized(self, query: dict) -> bool:
        token = self.owner.token
        supplied = self.headers.get("X-Ownkey-Token") or self._cookie_token() or (query.get("token") or [""])[0]
        return secrets.compare_digest(supplied or "", token)

    def _dispatch(self) -> None:
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        path = parsed.path
        if not path.startswith("/api/"):
            return self._static(path, query)
        if not self._authorized(query):
            return self._json(HTTPStatus.UNAUTHORIZED, {"error": "This page was opened without Ownkey's token. Open Meetings from the tray again."})
        for method, pattern, name in COMPILED:
            match = pattern.match(path)
            if match and method == self.command:
                try:
                    return getattr(self, "h_" + name)(query, **match.groupdict())
                except ConsentRequired as exc:
                    return self._json(HTTPStatus.CONFLICT, {"error": str(exc), "consent": exc.disclosure})
                except MeetingError as exc:
                    return self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                except Exception as exc:  # keep the window alive on unexpected errors
                    return self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": f"{exc.__class__.__name__}: {exc}"})
        for method, pattern, _name in COMPILED:
            if pattern.match(path):
                return self._json(HTTPStatus.METHOD_NOT_ALLOWED, {"error": "Method not allowed."})
        return self._json(HTTPStatus.NOT_FOUND, {"error": "Not found."})

    do_GET = do_POST = do_PUT = do_DELETE = _dispatch

    def _static(self, path: str, query: dict) -> None:
        if path in ("/", "/index.html"):
            if not self._authorized(query):
                body = b"<!doctype html><meta charset=utf-8><title>Ownkey Meetings</title><p style='font:14px system-ui;padding:24px'>Open Meetings from the Ownkey tray icon.</p>"
                return self._bytes(HTTPStatus.UNAUTHORIZED, body, "text/html; charset=utf-8")
            extra = {"Set-Cookie": f"{COOKIE}={self.owner.token}; Path=/; SameSite=Strict; HttpOnly"}
            return self._file(UI_DIR / "index.html", extra)
        name = path.lstrip("/")
        if not name or "/" in name or name.startswith("."):
            return self._json(HTTPStatus.NOT_FOUND, {"error": "Not found."})
        target = UI_DIR / name
        if not target.is_file():
            return self._json(HTTPStatus.NOT_FOUND, {"error": "Not found."})
        return self._file(target)

    def _file(self, target: Path, extra: dict | None = None) -> None:
        content_type = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        if content_type.startswith("text/") or content_type in ("application/javascript", "application/json"):
            content_type += "; charset=utf-8"
        self._bytes(HTTPStatus.OK, target.read_bytes(), content_type, extra)

    # ── handlers ───────────────────────────────────────────────────
    def h_status(self, query):
        self._json(HTTPStatus.OK, self.owner.service.status())

    def h_devices(self, query):
        from .capture import list_devices

        self._json(HTTPStatus.OK, list_devices())

    def h_list_meetings(self, query):
        self._json(HTTPStatus.OK, {"meetings": self.owner.service.list_meetings()})

    def h_create_meeting(self, query):
        body = self._body()
        meeting = self.owner.service.start_meeting(
            str(body.get("title", "")), mic=bool(body.get("mic", True)), system=bool(body.get("system", True)),
            mic_device=body.get("mic_device"), system_device=body.get("system_device"),
            retention=body.get("retention"), mic_shared=bool(body.get("mic_shared")))
        self._json(HTTPStatus.CREATED, {"meeting": meeting})

    def h_meeting(self, query, mid):
        self._json(HTTPStatus.OK, self.owner.service.meeting_detail(mid))

    def h_rename(self, query, mid):
        body = self._body()
        service = self.owner.service
        meeting = None
        if "title" in body:
            meeting = service.rename(mid, str(body.get("title", "")))
        if "mic_shared" in body:
            meeting = service.set_mic_shared(mid, bool(body.get("mic_shared")))
        if meeting is None:
            raise MeetingError("Nothing to change.")
        self._json(HTTPStatus.OK, {"meeting": meeting})

    def h_delete(self, query, mid):
        self.owner.service.delete_meeting(mid)
        self._json(HTTPStatus.OK, {"deleted": mid})

    def h_action(self, query, mid, action):
        service = self.owner.service
        body = self._body() if action in ("summary", "draft", "ask", "speakers", "transcribe") else {}
        remote_ok = bool(body.get("remote_ok"))
        if action == "transcribe":
            return self._json(HTTPStatus.ACCEPTED, {"job": service.transcribe(mid, remote_ok=remote_ok)})
        if action == "speakers":
            tracks = body.get("tracks")
            tracks = [str(t) for t in tracks] if isinstance(tracks, list) else None
            return self._json(HTTPStatus.ACCEPTED, {"job": service.label_speakers(mid, remote_ok=remote_ok, tracks=tracks)})
        if action == "pause":
            return self._json(HTTPStatus.OK, {"capture": service.pause()})
        if action == "resume":
            return self._json(HTTPStatus.OK, {"capture": service.resume()})
        if action == "stop":
            return self._json(HTTPStatus.OK, {"capture": service.stop()})
        if action == "summary":
            return self._json(HTTPStatus.ACCEPTED, {"job": service.summarize(
                mid, include_notes=bool(body.get("include_notes")), remote_ok=remote_ok)})
        if action == "draft":
            return self._json(HTTPStatus.ACCEPTED, {"job": service.draft(mid, remote_ok=remote_ok)})
        if action == "ask":
            return self._json(HTTPStatus.OK, {"answer": service.ask(
                mid, str(body.get("question", "")), include_notes=bool(body.get("include_notes")), remote_ok=remote_ok)})
        if action == "remove-audio":
            return self._json(HTTPStatus.OK, {"meeting": service.remove_audio(mid)})
        self._json(HTTPStatus.NOT_FOUND, {"error": "Unknown action."})

    def h_notes(self, query, mid):
        body = self._body()
        self._json(HTTPStatus.OK, {"notes": self.owner.service.save_notes(mid, str(body.get("content", "")))})

    def h_passage(self, query, mid, pid):
        body = self._body()
        service = self.owner.service
        if "speaker_id" in body:
            row = service.assign_speaker(mid, pid, str(body["speaker_id"]))
        else:
            corrected = body.get("corrected")
            row = service.correct_passage(mid, pid, None if corrected is None else str(corrected))
        self._json(HTTPStatus.OK, {"passage": row})

    def h_speaker(self, query, mid, sid):
        body = self._body()
        confirmed = body.get("confirmed")
        row = self.owner.service.rename_speaker(mid, sid, str(body.get("name", "")),
                                                None if confirmed is None else bool(confirmed))
        self._json(HTTPStatus.OK, {"speaker": row})

    def h_export(self, query, mid):
        fmt = (query.get("format") or ["md"])[0]
        filename, body = self.owner.service.export(mid, "json" if fmt == "json" else "md")
        content_type = "application/json; charset=utf-8" if fmt == "json" else "text/markdown; charset=utf-8"
        self._bytes(HTTPStatus.OK, body, content_type, {"Content-Disposition": f'attachment; filename="{filename}"'})

    def h_audio(self, query, mid, source):
        data = self.owner.service.audio_wav(mid, source)
        header = self.headers.get("Range")
        if header and header.startswith("bytes="):
            start_text, _, end_text = header[6:].partition("-")
            start = int(start_text or 0)
            end = int(end_text) if end_text else len(data) - 1
            end = min(end, len(data) - 1)
            if start > end:
                return self._bytes(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE, b"", "audio/wav", {"Content-Range": f"bytes */{len(data)}"})
            chunk = data[start:end + 1]
            return self._bytes(HTTPStatus.PARTIAL_CONTENT, chunk, "audio/wav",
                               {"Content-Range": f"bytes {start}-{end}/{len(data)}", "Accept-Ranges": "bytes"})
        self._bytes(HTTPStatus.OK, data, "audio/wav", {"Accept-Ranges": "bytes"})

    def h_policy(self, query):
        body = self._body()
        service = self.owner.service
        for kind in ("remote", "upload", "transcription"):
            if kind in body:
                service.set_policy(kind, str(body.get(kind)))
        self._json(HTTPStatus.OK, {"remote_policy": service.remote_policy(), "upload_policy": service.upload_policy(),
                                   "transcription_policy": service.transcription_policy()})

    def h_open_settings(self, query):
        self.owner.service.open_settings()
        self._json(HTTPStatus.OK, {"ok": True})
