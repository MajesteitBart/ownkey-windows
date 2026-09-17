import json
import tempfile
import time
import unittest
import urllib.request

import numpy as np

from meetings import audio
from meetings.capture import ArraySource, SourceError
from meetings.preview import MicPreview
from meetings.server import MeetingServer
from meetings.service import MeetingService
from meetings.store import MeetingStore


def wait_for(condition, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.02)
    return False


def tone(seconds=0.1, amplitude=9000):
    t = np.arange(int(audio.SAMPLE_RATE * seconds)) / audio.SAMPLE_RATE
    return (np.sin(2 * np.pi * 220 * t) * amplitude).astype(np.int16)


class Recorder:
    """Builds ArraySources and remembers them, like the service's source factory."""

    def __init__(self):
        self.made = []

    def __call__(self, device):
        source = ArraySource()
        source.device = device
        self.made.append(source)
        return source


class MicPreviewTests(unittest.TestCase):
    def setUp(self):
        self.sources = Recorder()
        self.preview = MicPreview(self.sources)
        self.addCleanup(self.preview.stop)

    def test_level_follows_the_input_and_falls_back(self):
        first = self.preview.read(None)
        self.assertEqual((first["active"], first["level"], first["error"]), (True, 0.0, ""))
        source = self.sources.made[0]
        self.assertTrue(source.alive)
        source.push(tone())
        self.assertTrue(wait_for(lambda: self.preview.read(None)["level"] > 0.3))
        self.assertTrue(source.blocks.empty(), "blocks are consumed for the meter, not collected")
        self.assertTrue(wait_for(lambda: self.preview.read(None)["level"] < 0.05), "silence lets the meter fall")
        self.assertEqual(len(self.sources.made), 1, "polling must not reopen the microphone")

    def test_the_microphone_is_released_when_nobody_asks(self):
        self.preview.IDLE_SECONDS = 0.2
        self.preview.read(None)
        source = self.sources.made[0]
        self.assertTrue(wait_for(lambda: not source.alive), "an abandoned preview keeps the microphone open")
        self.assertFalse(self.preview.active)
        self.assertTrue(self.preview.read(None)["active"], "asking again starts it again")
        self.assertEqual(len(self.sources.made), 2)

    def test_another_device_replaces_the_stream(self):
        self.preview.read(None)
        self.preview.read(3)
        first, second = self.sources.made
        self.assertFalse(first.alive)
        self.assertTrue(second.alive)
        self.assertEqual(second.device, 3)
        self.preview.stop()
        self.assertFalse(second.alive)

    def test_a_microphone_that_cannot_open_is_reported_and_not_hammered(self):
        attempts = []

        def failing(device):
            attempts.append(device)
            raise SourceError("Microphone could not start: device unavailable")

        preview = MicPreview(failing)
        for _ in range(5):
            result = preview.read(None)
        self.assertEqual((result["active"], result["level"]), (False, 0.0))
        self.assertIn("device unavailable", result["error"])
        self.assertEqual(len(attempts), 1, "the poll runs eight times a second; the device is retried every two")

    def test_a_source_that_dies_reports_its_error(self):
        self.preview.read(None)
        self.sources.made[0].fail("Microphone stopped delivering audio (device removed or sleep).")
        self.assertTrue(wait_for(lambda: "device removed" in self.preview.read(None)["error"]))


class ServicePreviewTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.made = []

        def factory(wants):
            sources = []
            for label in ("mic", "system"):
                if wants.get(label, True):
                    source = ArraySource(label)
                    source.wants = dict(wants)
                    sources.append(source)
            self.made.extend(sources)
            return sources

        self.service = MeetingService(MeetingStore(self.directory.name), get_config=lambda: {}, source_factory=factory)
        self.addCleanup(self.service.close)

    def test_preview_uses_only_the_microphone_and_hands_over_to_the_recording(self):
        self.assertTrue(self.service.mic_preview(2)["active"])
        self.assertEqual([s.label for s in self.made], ["mic"], "the preview never opens system audio")
        self.assertEqual(self.made[0].wants["mic_device"], 2)
        preview_source = self.made[0]
        self.service.start_meeting("Preview hand-over", mic=True, system=False)
        self.assertFalse(preview_source.alive, "the recording opens the microphone itself")
        self.assertEqual(self.service.mic_preview(2), {"active": False, "level": 0.0, "error": ""})
        self.assertEqual(len(self.made), 2, "no preview stream while a meeting records")
        self.service.stop()

    def test_nothing_is_written_while_previewing(self):
        self.service.mic_preview(None)
        self.made[0].push(tone(0.5))
        self.assertTrue(wait_for(lambda: self.service.mic_preview(None)["level"] > 0.3))
        self.service.stop_mic_preview()
        self.assertEqual(self.service.list_meetings(), [])
        self.assertEqual([p.name for p in self.service.store.root.iterdir() if p.is_dir()], [])

    def test_http_routes(self):
        server = MeetingServer(self.service)
        server.start()
        self.addCleanup(server.stop)

        def call(method, path):
            request = urllib.request.Request(server.base_url + path, method=method,
                                             headers={"X-Ownkey-Token": server.token})
            with urllib.request.urlopen(request, timeout=5) as response:
                return json.loads(response.read().decode("utf-8"))

        self.assertTrue(call("GET", "/api/preview/mic?device=4")["active"])
        self.assertEqual(self.made[0].wants["mic_device"], 4)
        self.assertTrue(call("GET", "/api/preview/mic?device=")["active"])
        self.assertIsNone(self.made[1].wants["mic_device"])
        self.assertEqual(call("DELETE", "/api/preview/mic"), {"active": False})
        self.assertFalse(self.made[1].alive)
        with self.assertRaises(urllib.error.HTTPError) as denied:
            urllib.request.urlopen(server.base_url + "/api/preview/mic", timeout=5)
        self.assertEqual(denied.exception.code, 401)
        denied.exception.close()


if __name__ == "__main__":
    unittest.main()
