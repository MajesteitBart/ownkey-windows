"""Development harness: run the meeting service and window without the tray.

    py -m meetings                       real microphone and system audio
    py -m meetings --fixture mic=a.wav --fixture system=b.wav
                                         feed WAV files as if they were live
                                         sources (no microphone is opened)

Uses the normal Ownkey config and model directory. Prints the window URL.
"""

from __future__ import annotations

import argparse
import sys
import threading
import time
import webbrowser

import numpy as np


def _fixture_source(label: str, path: str):
    from . import audio
    from .capture import ArraySource

    samples = audio.read_wav_16k(path)

    class FixtureSource(ArraySource):
        def start(self):
            super().start()
            self._stop = threading.Event()
            threading.Thread(target=self._run, daemon=True, name=f"fixture-{label}").start()

        def _run(self):
            block = int(audio.SAMPLE_RATE * 0.1)
            started = time.monotonic()
            position = 0
            while not self._stop.is_set() and position < samples.size:
                target = started + (position / audio.SAMPLE_RATE)
                delay = target - time.monotonic()
                if delay > 0:
                    time.sleep(delay)
                self.push(samples[position:position + block])
                position += block
            # keep delivering silence so the session sees a live source
            while not self._stop.is_set():
                time.sleep(0.1)
                self.push(np.zeros(block, dtype=np.int16))

        def stop(self):
            self._stop.set()
            super().stop()

    return FixtureSource(label)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--fixture", action="append", default=[], metavar="LABEL=WAV",
                        help="use a WAV file instead of a live source (mic=… or system=…)")
    parser.add_argument("--library", help="library directory (default: the real Ownkey library)")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args(argv)

    import ownkey
    from local_models import LocalModelManager
    from local_transcription import LocalTranscriber
    from .server import MeetingServer
    from .service import MeetingService
    from .store import MeetingStore

    cfg = ownkey.load_config()
    fixtures = dict(item.split("=", 1) for item in args.fixture)
    models = LocalModelManager(directory=cfg.get("local_model_directory"))
    transcriber = LocalTranscriber(models)
    transcriber.configure(active=True, idle_minutes=cfg.get("local_model_idle_timeout_minutes", 20))

    def source_factory(wants):
        if not fixtures:
            return MeetingService._default_sources(None, wants)
        return [_fixture_source(label, path) for label, path in fixtures.items()
                if wants.get(label, True)]

    def set_config(changes):
        cfg.update(changes)
        ownkey.save_config(cfg)

    store = MeetingStore(args.library)
    service = MeetingService(store, get_config=lambda: cfg, set_config=set_config, local_models=models,
                             local_transcriber=transcriber, get_rewrite_key=ownkey.get_rewrite_api_key,
                             notify=lambda message: print("[notify]", message), source_factory=source_factory)
    server = MeetingServer(service, port=args.port)
    url = server.start()
    print(url, flush=True)
    if not args.no_browser:
        webbrowser.open(url)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        server.stop()
        service.close()
        transcriber.close()
        store.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
