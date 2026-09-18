"""Pinned, user-requested model installation and app-owned download progress."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import queue
import shutil
import sys
import tarfile
import tempfile
import threading
from dataclasses import dataclass
from contextlib import contextmanager

import requests

MODEL_ID = "orukeet-onnx-int8"
REVISION = "55a984d46f68323301837194ce647c702f55facc"


@dataclass(frozen=True)
class ModelFile:
    name: str
    size: int
    sha256: str


@dataclass(frozen=True)
class Model:
    id: str
    revision: str
    url: str
    archive_size: int
    archive_sha256: str
    archive_root: str
    files: tuple[ModelFile, ...]
    file_urls: tuple[str, ...] = ()
    label: str = ""
    manifest: ModelFile | None = None
    manifest_url: str = ""

    @property
    def extracted_size(self):
        return sum(file.size for file in self.files)


ORUKEET = Model(
    MODEL_ID, REVISION,
    f"https://huggingface.co/oruk/orukeet/resolve/{REVISION}/onnx/sherpa-onnx-orukeet-v0.1.0-int8.tar.bz2",
    486807585, "f9191f30178cc9122ce2f023bf9fefafc822028307b0efa4caff645ba3fe8d0a",
    "sherpa-onnx-orukeet-v0.1.0-int8",
    (
        ModelFile("encoder.int8.onnx", 653182378, "7b55f2a504a20a8e462899f5befd45f4a1784948d76ed0127902d9cf39405487"),
        ModelFile("decoder.int8.onnx", 11845332, "c185c2afb4c77c94bb1314807ecb3dc1623057a3dc540b83e10301af9bf4cfca"),
        ModelFile("joiner.int8.onnx", 6355335, "1a7e90abf7172d926dd7e2edac2a5d5035c24dfb641a15e131d57b6a5f63cdd3"),
        ModelFile("tokens.txt", 93939, "d58544679ea4bc6ac563d1f545eb7d474bd6cfa467f0a6e2c1dc1c7d37e3c35d"),
        ModelFile("bpe.vocab", 117408, "41d5e71b3591642eff088151efd7acd4e750124cc0054c8ba9fa3245187a4804"),
        ModelFile("LICENSE-WEIGHTS", 20137, "23ee78c8bae49cf08ea2f0c84945c66b987ebe4520881fb51b3dad4fb43d07c2"),
        ModelFile("NOTICE.md", 5271, "440361d963edd9621e744f251332b47f2c4de2e2594ecfe42b215e3f6223fa44"),
    ),
    manifest=ModelFile("manifest.json", 1867, "7e80f93f0e9b923c392424b0f85d28a717feee0a4d2a6aa9bfa723693868e727"),
    manifest_url=f"https://huggingface.co/oruk/orukeet/resolve/{REVISION}/onnx/manifest.json",
)
CATALOG = {MODEL_ID: ORUKEET}
BUSY_STAGES = {"Downloading", "Verifying", "Extracting"}
CHUNK_SIZE = 1024 * 1024


class ModelError(RuntimeError):
    pass


class DownloadCancelled(ModelError):
    pass


def model_root() -> Path:
    if sys.platform.startswith("linux"):
        return Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "ownkey" / "models"
    return Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / "Ownkey" / "models"


@dataclass(frozen=True)
class DownloadState:
    stage: str = "Not downloaded"
    completed: int = 0
    total: int = 0
    error: str = ""


class LocalModelManager:
    def __init__(self, root=None, model=ORUKEET, http_get=requests.get, *, directory=None):
        self.model = model
        self.root = Path(root) if root is not None else model_root()
        self.path = self.root / (model.id if model.file_urls else "orukeet") / model.revision
        if directory:
            self.path = Path(directory).expanduser().absolute()
            self.root = self.path.parent
        self._get = http_get
        self._lock = threading.RLock()
        self._state = DownloadState()
        self._subscribers = set()
        self._cancel = threading.Event()
        self._thread = None
        self._users = 0
        self._active = False

    def snapshot(self):
        with self._lock:
            return self._state

    def subscribe(self):
        updates = queue.Queue(maxsize=1)
        with self._lock:
            self._subscribers.add(updates)
            updates.put_nowait(self._state)
        return updates

    def unsubscribe(self, updates):
        with self._lock:
            self._subscribers.discard(updates)

    def _publish(self, stage, completed=0, total=0, error=""):
        with self._lock:
            self._state = DownloadState(stage, completed, total, error)
            for updates in self._subscribers:
                try:
                    updates.get_nowait()
                except queue.Empty:
                    pass
                updates.put_nowait(self._state)

    def set_active(self, active):
        with self._lock:
            self._active = active

    @contextmanager
    def use(self):
        """Keep removal and replacement out of a recognizer's file lifetime."""
        with self._lock:
            if self._state.stage in BUSY_STAGES:
                raise ModelError("Model installation is in progress. Wait for it to finish.")
            self._users += 1
        try:
            yield self.path
        finally:
            with self._lock:
                self._users -= 1

    def _check_cancel(self):
        if self._cancel.is_set():
            raise DownloadCancelled("Download cancelled.")

    def _hash(self, path, *, cancellable=False):
        digest = hashlib.sha256()
        with path.open("rb") as source:
            while block := source.read(CHUNK_SIZE):
                if cancellable:
                    self._check_cancel()
                digest.update(block)
        return digest.hexdigest()

    def files_present(self):
        try:
            return not self.path.is_symlink() and all(
                not (self.path / file.name).is_symlink()
                and (self.path / file.name).is_file()
                and (self.path / file.name).stat().st_size == file.size
                for file in self.model.files
            )
        except OSError:
            return False

    def validate(self, path=None, *, cancellable=False):
        directory = Path(path) if path is not None else self.path
        settings_tab = "Rewriting" if self.model.file_urls else "Audio"
        for file in self.model.files:
            target = directory / file.name
            if directory.is_symlink() or target.is_symlink() or not target.is_file():
                raise ModelError(f"Model missing: {file.name}. Download it in Settings > {settings_tab}.")
            if target.stat().st_size != file.size or self._hash(target, cancellable=cancellable) != file.sha256:
                raise ModelError(f"Model file is damaged: {file.name}. Remove the download after switching away, then download again.")
        return directory

    def check_installation(self):
        try:
            with self.use():
                self.validate()
                self._publish("Installed")
        except (ModelError, OSError) as exc:
            with self._lock:
                if self._state.stage in BUSY_STAGES:
                    return
                if self._active or self.path.exists():
                    self._publish("Model missing", error=str(exc))
                else:
                    self._publish("Not downloaded")

    def start_download(self):
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return False
            if self._users:
                raise ModelError("The model is in use. Try again when transcription has finished.")
            if self.files_present():
                # Never replace a potentially loaded model, even if its hash is bad.
                raise ModelError("The model is already downloaded. Switch away and remove it before downloading again.")
            self._check_replaceable()
            self._cancel.clear()
            self._publish("Downloading", total=self.model.archive_size)
            target = self._download_files if self.model.file_urls else self._download
            self._thread = threading.Thread(target=target, daemon=True, name="model-download")
            self._thread.start()
            return True

    def cancel(self):
        self._cancel.set()

    def close(self):
        self.cancel()
        if self._thread is not None:
            self._thread.join(timeout=15)

    def remove(self):
        with self._lock:
            if self._active or self._users or self._state.stage in BUSY_STAGES:
                raise ModelError("Switch to another audio provider and save before removing this model. Wait for pending transcription to finish.")
            # A chosen folder can also contain the user's own files.
            if self.path.is_symlink() or getattr(self.path, "is_junction", lambda: False)():
                raise ModelError("Choose a model folder without a symbolic link or junction.")
            for file in self.model.files:
                target = self.path / file.name
                if target.is_file() or target.is_symlink():
                    target.unlink()
            if self.path.exists() and not any(self.path.iterdir()):
                self.path.rmdir()
            self._publish("Not downloaded")

    def _check_replaceable(self):
        if self.path.is_symlink() or getattr(self.path, "is_junction", lambda: False)():
            raise ModelError("Choose a model folder without a symbolic link or junction.")
        if self.path.exists():
            owned = {file.name for file in self.model.files}
            if not self.path.is_dir() or any(p.name not in owned or not p.is_file() or p.is_symlink()
                                             for p in self.path.iterdir()):
                raise ModelError("This folder contains other files. Choose an empty folder for the download.")

    def _remove_path(self, path):
        # Every recursive removal must stay under this manager's model root.
        path = Path(path)
        if path.is_symlink() or not path.resolve().is_relative_to(self.root.resolve()) or path.resolve() == self.root.resolve():
            raise ModelError("Unsafe model storage path.")
        if path.exists():
            shutil.rmtree(path)

    def _extract(self, archive, staging):
        expected = {file.name: file for file in self.model.files}
        seen = set()
        written = 0
        self._publish("Extracting", total=self.model.extracted_size)
        with tarfile.open(archive, mode="r|bz2") as bundle:
            for member in bundle:
                self._check_cancel()
                # data_filter alone can normalize absolute paths and allow internal
                # links. Reject those explicitly, along with undeclared files.
                parts = PurePosixPath(member.name).parts
                if "\\" in member.name or ":" in member.name or ".." in parts or member.name.startswith("/"):
                    raise ModelError("Unsafe path in model archive.")
                tarfile.data_filter(member, str(staging))
                if member.isdir() and parts == (self.model.archive_root,):
                    continue
                if not member.isfile() or len(parts) != 2 or parts[0] != self.model.archive_root:
                    raise ModelError("Unexpected entry in model archive.")
                name = parts[1]
                if name not in expected or name in seen or member.size != expected[name].size:
                    raise ModelError(f"Unexpected model file: {name}")
                seen.add(name)
                # Apply the data filter above, then copy in chunks so cancellation
                # and progress also work inside the 653 MB encoder file.
                with bundle.extractfile(member) as source, (staging / name).open("xb") as target:
                    while block := source.read(CHUNK_SIZE):
                        self._check_cancel()
                        target.write(block)
                        written += len(block)
                        self._publish("Extracting", written, self.model.extracted_size)
        if seen != set(expected):
            raise ModelError("The model archive is incomplete.")

    def _archive_release(self):
        """Verify the publisher's pinned release before acquiring its archive."""
        spec = self.model.manifest
        if spec is None:
            return self.model.archive_size, self.model.archive_sha256
        data = bytearray()
        self._check_cancel()
        with self._get(self.model.manifest_url, stream=True, timeout=(10, 10)) as response:
            response.raise_for_status()
            for block in response.iter_content(CHUNK_SIZE):
                self._check_cancel()
                if len(data) + len(block) > spec.size:
                    raise ModelError("Model release manifest exceeds its pinned size.")
                data.extend(block)
        self._check_cancel()
        if len(data) != spec.size or hashlib.sha256(data).hexdigest() != spec.sha256:
            raise ModelError("Model release manifest checksum does not match.")
        manifest = json.loads(data)
        expected = {
            "archive": self.model.url.rsplit("/", 1)[-1],
            "archive_bytes": self.model.archive_size,
            "archive_sha256": self.model.archive_sha256,
            "extract_dir": self.model.archive_root,
            "files": [{"path": f.name, "bytes": f.size, "sha256": f.sha256}
                      for f in self.model.files],
        }
        if not isinstance(manifest, dict) or any(manifest.get(k) != v for k, v in expected.items()):
            raise ModelError("Model release manifest does not match the selected model.")
        return manifest["archive_bytes"], manifest["archive_sha256"]

    def _download(self):
        temporary = None
        backup = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            required = self.model.archive_size + self.model.extracted_size + 64 * 1024 * 1024
            if shutil.disk_usage(self.path.parent).free < required:
                raise ModelError("Not enough disk space. Free at least 1.2 GB for the download and extraction.")
            archive_size, archive_sha256 = self._archive_release()
            temporary = Path(tempfile.mkdtemp(prefix=".download-", dir=self.path.parent))
            archive = temporary / "model.tar.bz2"
            count = 0
            with self._get(self.model.url, stream=True, timeout=(10, 10)) as response:
                response.raise_for_status()
                with archive.open("xb") as target:
                    for block in response.iter_content(CHUNK_SIZE):
                        self._check_cancel()
                        count += len(block)
                        if count > archive_size:
                            raise ModelError("Model archive exceeds its pinned size.")
                        target.write(block)
                        self._publish("Downloading", count, archive_size)
            self._publish("Verifying")
            if count != archive_size or self._hash(archive, cancellable=True) != archive_sha256:
                raise ModelError("Model archive checksum does not match. Try downloading again.")
            staging = temporary / "staging"
            staging.mkdir()
            self._extract(archive, staging)
            self.validate(staging, cancellable=True)
            self._check_cancel()
            with self._lock:
                self._check_replaceable()
                if self.path.exists():
                    backup = temporary / "previous"
                    self.path.rename(backup)
                try:
                    staging.rename(self.path)
                except Exception:
                    if backup is not None:
                        backup.rename(self.path)
                    raise
            self._publish("Installed")
        except DownloadCancelled:
            self._publish("Cancelled")
        except Exception as exc:
            self._publish("Error", error=str(exc))
        finally:
            if temporary is not None:
                self._remove_path(temporary)

    def _download_files(self):
        """Install GGUF weights and their license as one verified directory."""
        temporary = None
        backup = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            if shutil.disk_usage(self.path.parent).free < self.model.extracted_size + 64 * 1024 * 1024:
                raise ModelError("Not enough disk space for this model download.")
            temporary = Path(tempfile.mkdtemp(prefix=".download-", dir=self.path.parent))
            staging = temporary / "staging"
            staging.mkdir()
            completed = 0
            for file, url in zip(self.model.files, self.model.file_urls, strict=True):
                count = 0
                with (staging / file.name).open("xb") as target:
                    while count < file.size:
                        self._check_cancel()
                        end = min(file.size - 1, count + 8 * CHUNK_SIZE - 1)
                        headers = {"Range": f"bytes={count}-{end}"} if file.size > 8 * CHUNK_SIZE else {}
                        with self._get(url, headers=headers, stream=True, timeout=(10, 10)) as response:
                            response.raise_for_status()
                            partial = getattr(response, "status_code", 200) == 206
                            if partial:
                                expected = f"bytes {count}-{end}/{file.size}"
                                if response.headers.get("Content-Range") != expected:
                                    raise ModelError("The download server returned the wrong file range.")
                            elif count:
                                raise ModelError("The download server did not honor the requested file range.")
                            limit = end + 1 if partial else file.size
                            for block in response.iter_content(CHUNK_SIZE):
                                self._check_cancel()
                                count += len(block)
                                if count > limit:
                                    raise ModelError("Model download exceeds its pinned size.")
                                target.write(block)
                                completed += len(block)
                                self._publish("Downloading", completed, self.model.extracted_size)
                            if count != limit:
                                raise ModelError("Model download is incomplete.")
                if count != file.size:
                    raise ModelError("Model download is incomplete.")
            self._publish("Verifying")
            self.validate(staging, cancellable=True)
            self._check_cancel()
            with self._lock:
                self._check_replaceable()
                if self.path.exists():
                    backup = temporary / "previous"
                    self.path.rename(backup)
                try:
                    staging.rename(self.path)
                except Exception:
                    if backup is not None:
                        backup.rename(self.path)
                    raise
            self._publish("Installed")
        except DownloadCancelled:
            self._publish("Cancelled")
        except Exception as exc:
            self._publish("Error", error=str(exc))
        finally:
            if temporary is not None:
                self._remove_path(temporary)
