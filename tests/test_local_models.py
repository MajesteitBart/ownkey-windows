import hashlib
import io
import json
from pathlib import Path
import tarfile
import tempfile
import threading
import unittest
from dataclasses import replace
from unittest.mock import patch

from local_models import LocalModelManager, Model, ModelError, ModelFile


def fixture(entries=None):
    payload = b"a tiny model and its license"
    member = ModelFile("tokens.txt", len(payload), hashlib.sha256(payload).hexdigest())
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:bz2") as bundle:
        for name, kind in entries or [("model/tokens.txt", tarfile.REGTYPE)]:
            info = tarfile.TarInfo(name)
            info.type = kind
            if kind == tarfile.REGTYPE:
                info.size = len(payload)
                bundle.addfile(info, io.BytesIO(payload))
            else:
                info.linkname = "tokens.txt"
                bundle.addfile(info)
    archive = output.getvalue()
    model = Model("test-model", "test-revision", "https://test.invalid/model", len(archive),
                  hashlib.sha256(archive).hexdigest(), "model", (member,))
    return model, archive, payload


class Response:
    def __init__(self, data):
        self.data = data

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass

    def raise_for_status(self):
        pass

    def iter_content(self, _):
        yield self.data


class DownloadTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)

    def manager(self, model=None, archive=None):
        default_model, default_archive, _ = fixture()
        manager = LocalModelManager(self.directory.name, model or default_model,
                                    http_get=lambda *a, **k: Response(archive or default_archive))
        self.addCleanup(manager.close)
        return manager

    def finish(self, manager):
        manager.start_download()
        manager._thread.join(5)
        self.assertFalse(manager._thread.is_alive())

    def test_download_installs_valid_files_and_keeps_provider_inactive(self):
        manager = self.manager()
        self.finish(manager)
        self.assertEqual(manager.snapshot().stage, "Installed")
        self.assertEqual(manager.validate(), manager.path)
        self.assertFalse(manager._active)
        self.assertEqual(list(manager.path.parent.iterdir()), [manager.path])

    def test_corrupt_hash_does_not_install(self):
        model, archive, _ = fixture()
        manager = self.manager(replace(model, archive_sha256="0" * 64), archive)
        self.finish(manager)
        self.assertEqual(manager.snapshot().stage, "Error")
        self.assertFalse(manager.path.exists())
        self.assertEqual(list(manager.path.parent.iterdir()), [])

    def release_manager(self, *, metadata=None, mutate=None):
        model, archive, _ = fixture()
        release = {
            "archive": "model", "archive_bytes": model.archive_size,
            "archive_sha256": model.archive_sha256, "extract_dir": model.archive_root,
            "files": [{"path": f.name, "bytes": f.size, "sha256": f.sha256}
                      for f in model.files],
        }
        release.update(metadata or {})
        data = json.dumps(release).encode()
        model = replace(model, manifest=ModelFile("manifest.json", len(data),
                        hashlib.sha256(data).hexdigest()),
                        manifest_url="https://test.invalid/manifest.json")
        requests = []
        def get(url, **_kwargs):
            requests.append(url)
            if url == model.manifest_url:
                return Response(mutate(data) if mutate else data)
            self.assertEqual(url, model.url)
            return Response(archive)
        manager = LocalModelManager(self.directory.name, model, http_get=get)
        self.addCleanup(manager.close)
        return manager, requests

    def test_release_manifest_is_verified_before_archive_and_not_needed_offline(self):
        manager, requests = self.release_manager()
        self.finish(manager)
        self.assertEqual(manager.snapshot().stage, "Installed")
        self.assertEqual(requests, [manager.model.manifest_url, manager.model.url])
        with patch.object(manager, "_get") as get:
            manager.check_installation()
            manager.validate()
            with self.assertRaisesRegex(ModelError, "already downloaded"):
                manager.start_download()
        get.assert_not_called()
        self.assertEqual(manager.snapshot().stage, "Installed")

    def test_corrupt_or_incomplete_manifest_never_downloads_archive(self):
        for mutate in [lambda data: b"x" + data[1:], lambda data: data[:-1],
                       lambda data: data + b"x"]:
            with self.subTest(mutate=mutate):
                manager, requests = self.release_manager(mutate=mutate)
                self.finish(manager)
                self.assertEqual(manager.snapshot().stage, "Error")
                self.assertIn("manifest", manager.snapshot().error)
                self.assertEqual(requests, [manager.model.manifest_url])
                self.assertFalse(manager.path.exists())
                self.assertEqual(list(manager.path.parent.iterdir()), [])

    def test_manifest_must_describe_the_selected_archive_and_files(self):
        for metadata in [{"archive": "other.tar.bz2"}, {"archive_bytes": 1},
                         {"archive_sha256": "0" * 64}, {"extract_dir": "other"},
                         {"files": []}]:
            with self.subTest(metadata=metadata):
                manager, requests = self.release_manager(metadata=metadata)
                self.finish(manager)
                self.assertEqual(manager.snapshot().stage, "Error")
                self.assertIn("selected model", manager.snapshot().error)
                self.assertEqual(requests, [manager.model.manifest_url])
                self.assertFalse(manager.path.exists())

    def test_cancel_during_manifest_does_not_start_archive(self):
        manager, requests = self.release_manager()
        original_get = manager._get
        def get(*args, **kwargs):
            response = original_get(*args, **kwargs)
            manager.cancel()
            return response
        manager._get = get
        self.finish(manager)
        self.assertEqual(manager.snapshot().stage, "Cancelled")
        self.assertEqual(requests, [manager.model.manifest_url])
        self.assertFalse(manager.path.exists())

    def test_manifest_network_error_does_not_start_archive(self):
        manager, requests = self.release_manager()
        def get(url, **_kwargs):
            requests.append(url)
            raise OSError("manifest endpoint unavailable")
        manager._get = get
        self.finish(manager)
        self.assertEqual(manager.snapshot().stage, "Error")
        self.assertEqual(requests, [manager.model.manifest_url])
        self.assertFalse(manager.path.exists())

    def test_file_hash_is_also_checked(self):
        model, archive, _ = fixture()
        model = replace(model, files=(replace(model.files[0], sha256="0" * 64),))
        manager = self.manager(model, archive)
        self.finish(manager)
        self.assertEqual(manager.snapshot().stage, "Error")
        self.assertFalse(manager.path.exists())

    def test_rejects_paths_links_devices_and_duplicate_files(self):
        for name, kind in [("../tokens.txt", tarfile.REGTYPE), ("/model/tokens.txt", tarfile.REGTYPE),
                           ("model/../tokens.txt", tarfile.REGTYPE), ("model\\tokens.txt", tarfile.REGTYPE),
                           ("model/C:tokens.txt", tarfile.REGTYPE), ("model/tokens.txt", tarfile.SYMTYPE),
                           ("model/tokens.txt", tarfile.LNKTYPE), ("model/tokens.txt", tarfile.CHRTYPE),
                           ("other/tokens.txt", tarfile.REGTYPE)]:
            with self.subTest(name=name, kind=kind):
                model, archive, _ = fixture([(name, kind)])
                manager = self.manager(model, archive)
                self.finish(manager)
                self.assertEqual(manager.snapshot().stage, "Error")
                self.assertFalse(manager.path.exists())
        model, archive, _ = fixture([("model/tokens.txt", tarfile.REGTYPE)] * 2)
        manager = self.manager(model, archive)
        self.finish(manager)
        self.assertEqual(manager.snapshot().stage, "Error")

    def test_disk_space_is_checked_before_http(self):
        manager = self.manager()
        with patch("local_models.shutil.disk_usage") as disk, patch.object(manager, "_get") as get:
            disk.return_value.free = 0
            self.finish(manager)
        get.assert_not_called()
        self.assertIn("disk space", manager.snapshot().error)

    def test_cancel_during_extraction_cleans_partial_data(self):
        manager = self.manager()
        publish = manager._publish

        def cancel_on_extract(stage, *args, **kwargs):
            publish(stage, *args, **kwargs)
            if stage == "Extracting":
                manager.cancel()

        with patch.object(manager, "_publish", side_effect=cancel_on_extract):
            self.finish(manager)
        self.assertEqual(manager.snapshot().stage, "Cancelled")
        self.assertEqual(list(manager.path.parent.iterdir()), [])

    def test_subscribers_can_detach_and_reattach_during_download(self):
        manager = self.manager()
        started, release = threading.Event(), threading.Event()
        original_get = manager._get

        def delayed_get(*args, **kwargs):
            started.set()
            release.wait(5)
            return original_get(*args, **kwargs)

        manager._get = delayed_get
        updates = manager.subscribe()
        manager.start_download()
        self.assertTrue(started.wait(2))
        self.assertFalse(manager.start_download())
        manager.unsubscribe(updates)
        reopened = manager.subscribe()
        self.assertEqual(reopened.get_nowait().stage, "Downloading")
        release.set()
        manager._thread.join(5)
        self.assertEqual(reopened.get_nowait().stage, "Installed")
        self.assertFalse(manager._active)

    def test_removal_requires_inactive_and_no_recordings(self):
        manager = self.manager()
        self.finish(manager)
        manager.set_active(True)
        with self.assertRaises(ModelError):
            manager.remove()
        manager.set_active(False)
        with manager.use(), self.assertRaises(ModelError):
            manager.remove()
        manager.remove()
        self.assertFalse(manager.path.exists())

    def test_startup_validates_hash_and_reports_missing_without_network(self):
        manager = self.manager()
        self.finish(manager)
        target = manager.path / "tokens.txt"
        target.write_bytes(b"x" * target.stat().st_size)
        manager.set_active(True)
        with patch.object(manager, "_get") as get:
            manager.check_installation()
        get.assert_not_called()
        self.assertEqual(manager.snapshot().stage, "Model missing")

    def test_failed_install_rename_restores_previous_files(self):
        manager = self.manager()
        manager.path.mkdir(parents=True)
        (manager.path / "tokens.txt").write_text("previous model")
        original = Path.rename

        def fail_staging(path, target):
            if path.name == "staging":
                raise OSError("simulated rename failure")
            return original(path, target)

        with patch.object(Path, "rename", fail_staging):
            self.finish(manager)
        self.assertEqual(manager.snapshot().stage, "Error")
        self.assertEqual((manager.path / "tokens.txt").read_text(), "previous model")

    def test_existing_folder_can_be_selected_and_removal_keeps_other_files(self):
        model, _, payload = fixture()
        folder = Path(self.directory.name) / "existing"
        folder.mkdir()
        (folder / "tokens.txt").write_bytes(payload)
        (folder / "my-notes.txt").write_text("keep")
        manager = LocalModelManager(model=model, directory=folder)
        manager.check_installation()
        self.assertEqual(manager.snapshot().stage, "Installed")
        manager.remove()
        self.assertEqual((folder / "my-notes.txt").read_text(), "keep")
        self.assertFalse((folder / "tokens.txt").exists())

    def test_download_refuses_to_replace_folder_containing_unrelated_files(self):
        manager = self.manager()
        manager.path.mkdir(parents=True)
        (manager.path / "my-notes.txt").write_text("keep")
        with self.assertRaisesRegex(ModelError, "other files"):
            manager.start_download()
        self.assertEqual((manager.path / "my-notes.txt").read_text(), "keep")

    def test_separate_file_download_verifies_weights_and_license_together(self):
        model, _, payload = fixture()
        model = replace(model, file_urls=("https://test.invalid/tokens",), archive_size=len(payload))
        manager = self.manager(model, payload)
        self.finish(manager)
        self.assertEqual(manager.snapshot().stage, "Installed")
        self.assertEqual(manager.validate(), manager.path)

    def test_separate_file_download_rejects_wrong_range_without_installing(self):
        model, _, payload = fixture()
        model = replace(model, file_urls=("https://test.invalid/tokens",), archive_size=len(payload))
        manager = self.manager(model, payload)
        response = Response(payload)
        response.status_code = 206
        response.headers = {"Content-Range": "bytes 5-10/20"}
        manager._get = lambda *a, **kw: response
        self.finish(manager)
        self.assertEqual(manager.snapshot().stage, "Error")
        self.assertFalse(manager.path.exists())

    def test_range_download_assembles_exact_file_and_checks_its_hash(self):
        model, _, payload = fixture()
        model = replace(model, file_urls=("https://test.invalid/tokens",), archive_size=len(payload))
        manager = self.manager(model, payload)
        ranges = []
        def get(_url, *, headers, **_kwargs):
            start, end = map(int, headers["Range"].removeprefix("bytes=").split("-"))
            ranges.append((start, end))
            response = Response(payload[start:end + 1])
            response.status_code = 206
            response.headers = {"Content-Range": f"bytes {start}-{end}/{len(payload)}"}
            return response
        manager._get = get
        with patch("local_models.CHUNK_SIZE", 1):
            self.finish(manager)
        self.assertEqual(manager.snapshot().stage, "Installed")
        self.assertEqual((manager.path / "tokens.txt").read_bytes(), payload)
        self.assertGreater(len(ranges), 1)
        self.assertEqual(ranges[0][0], 0)
        self.assertEqual(ranges[-1][1], len(payload) - 1)


if __name__ == "__main__":
    unittest.main()
