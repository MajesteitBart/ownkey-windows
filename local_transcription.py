"""Serial CPU recognition, recording leases, and idle memory release."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import gc
import io
import threading
import time
import wave

from local_models import MODEL_ID, ModelError

SAMPLE_RATE = 16000
# Zolder measurements: four threads outperform two and os.cpu_count().
NUM_THREADS = 4


def load_recognizer(directory):
    # Keep sherpa optional for config/provider tests and cloud-only use.
    import sherpa_onnx

    # modified_beam_search with the BPE vocabulary lets each recording pass
    # dictionary terms as hotwords. build/orukeet-hotwords-probe.json measured
    # it at the same speed as greedy search on this model.
    return sherpa_onnx.OfflineRecognizer.from_transducer(
        encoder=str(directory / "encoder.int8.onnx"),
        decoder=str(directory / "decoder.int8.onnx"),
        joiner=str(directory / "joiner.int8.onnx"),
        tokens=str(directory / "tokens.txt"),
        num_threads=NUM_THREADS, sample_rate=SAMPLE_RATE, feature_dim=128,
        decoding_method="modified_beam_search", max_active_paths=4,
        modeling_unit="bpe", bpe_vocab=str(directory / "bpe.vocab"), hotwords_score=1.5,
        provider="cpu", model_type="nemo_transducer",
    )


def hotwords_string(vocabulary):
    """Join dictionary terms into sherpa-onnx's slash-separated hotwords string."""
    terms = [" ".join(str(term).split()) for term in vocabulary or ()]
    return "/".join(term.replace("/", " ") for term in terms if term)


def wav_samples(wav_bytes):
    import numpy as np

    with wave.open(io.BytesIO(wav_bytes), "rb") as source:
        if (source.getnchannels(), source.getsampwidth(), source.getframerate(), source.getcomptype()) != (1, 2, SAMPLE_RATE, "NONE"):
            raise ModelError("Local transcription requires mono 16-bit WAV at 16 kHz.")
        data = source.readframes(source.getnframes())
    return np.frombuffer(data, dtype="<i2").astype(np.float32) / 32768.0


class LocalAttempt:
    """A recording owns its model until processing finishes, even after a switch."""

    def __init__(self, service, lease, ready):
        self.service = service
        self._lease = lease
        self.ready = ready
        self._closed = False

    def transcribe(self, wav_bytes, on_loading=None, vocabulary=()):
        if self._closed:
            raise ModelError("This local recording has already finished.")
        if not self.ready.done() and on_loading is not None:
            on_loading()
        return self.service._executor.submit(self._decode_when_ready, wav_bytes, vocabulary).result()

    def _decode_when_ready(self, wav_bytes, vocabulary):
        self.ready.result()
        return self.service._decode(wav_bytes, vocabulary)

    def close(self):
        with self.service._lock:
            if self._closed:
                return
            self._closed = True
            self._lease.__exit__(None, None, None)
            self.service._attempts -= 1
            self.service._last_used = self.service._clock()
            self.service._maybe_unload()


class LocalTranscriber:
    def __init__(self, models, *, loader=load_recognizer, clock=time.monotonic, start_timer=True):
        self.models = models
        self._loader = loader
        self._clock = clock
        self._lock = threading.RLock()
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="local-recognizer")
        self._recognizer = None
        self._ready = None
        self._attempts = 0
        self._active = False
        self._closed = False
        self._loading = False
        self._decoding = False
        self._error = ""
        self._last_used = clock()
        self._timeout = 20 * 60
        self._stop = threading.Event()
        if start_timer:
            threading.Thread(target=self._idle_loop, daemon=True, name="local-idle").start()

    def configure(self, *, active, idle_minutes=20):
        with self._lock:
            self._active = active
            self._timeout = max(0, float(idle_minutes)) * 60
            self._maybe_unload()
            self.models.set_active(active)

    def snapshot(self):
        with self._lock:
            if self._loading:
                state = "Loading"
            elif self._error:
                state = "Error"
            elif self._recognizer is not None:
                state = "Loaded"
            else:
                state = "Unloaded"
            return {"state": state, "error": self._error, "busy": bool(self._attempts), "decoding": self._decoding}

    def begin_attempt(self, model_id=MODEL_ID, *, validate=False):
        with self._lock:
            if self._closed:
                raise ModelError("Local transcription is shutting down.")
            if model_id != self.models.model.id:
                raise ModelError("Unknown local model. Select Local (Orukeet) in Settings and save.")
            lease = self.models.use()
            lease.__enter__()
            self._attempts += 1
            # Exactly one load is scheduled while several hotkeys or Save overlap.
            if self._ready is None or (self._ready.done() and self._recognizer is None):
                self._loading = True
                self._error = ""
                self._ready = self._executor.submit(self._load)
            ready = self._ready
            if validate:
                ready = self._executor.submit(self._validate_ready, ready)
            return LocalAttempt(self, lease, ready)

    def _validate_ready(self, ready):
        ready.result()
        self.models.validate()

    def _load(self):
        try:
            with self.models.use():
                directory = self.models.validate()
                recognizer = self._loader(directory)
                with self._lock:
                    self._recognizer = recognizer
                    self._error = ""
                del recognizer
        except Exception as exc:
            with self._lock:
                self._error = str(exc)
            raise ModelError(f"Could not load Orukeet: {exc}. Try again in Settings > Transcription.") from exc
        finally:
            with self._lock:
                self._loading = False
                self._last_used = self._clock()
                self._maybe_unload()

    def _decode(self, wav_bytes, vocabulary=()):
        with self._lock:
            if self._closed:
                raise ModelError("Local transcription is shutting down.")
            self._decoding = True
        try:
            with self.models.use():
                samples = wav_samples(wav_bytes)
                # Detect removed files even while the recognizer is still resident.
                if not self.models.files_present():
                    raise ModelError("Model missing. Download it in Settings > Transcription.")
                hotwords = hotwords_string(vocabulary)
                stream = self._recognizer.create_stream(hotwords) if hotwords else self._recognizer.create_stream()
                try:
                    stream.accept_waveform(SAMPLE_RATE, samples)
                    self._recognizer.decode_stream(stream)
                    return stream.result.text.strip()
                finally:
                    del stream
        finally:
            with self._lock:
                self._decoding = False
                self._last_used = self._clock()
                self._maybe_unload()

    def _maybe_unload(self):
        if self._attempts or self._loading or self._decoding:
            return False
        expired = self._timeout > 0 and self._clock() - self._last_used >= self._timeout
        if self._recognizer is not None and (not self._active or expired or self._closed):
            self._recognizer = None
            self._ready = None
            gc.collect()
            return True
        return False

    def check_idle(self):
        with self._lock:
            return self._maybe_unload()

    def _idle_loop(self):
        while not self._stop.wait(1):
            self.check_idle()

    def close(self, *, wait=True):
        with self._lock:
            self._closed = True
            self._active = False
            self._maybe_unload()
        self._stop.set()
        self._executor.shutdown(wait=wait, cancel_futures=True)
        if wait:
            with self._lock:
                self._recognizer = None
                self._ready = None
                gc.collect()


def run_smoke_test(argv):
    """Exercise the actual frozen runtime without tray, startup, or user config."""
    import argparse
    import json
    from pathlib import Path
    import requests
    from local_models import LocalModelManager
    from providers import REWRITE_PROVIDER_IDS

    parser = argparse.ArgumentParser()
    parser.add_argument("--model-root", required=True)
    parser.add_argument("--wav", required=True, nargs="+")
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    now = [0]
    service = LocalTranscriber(LocalModelManager(args.model_root), clock=lambda: now[0], start_timer=False)
    service.configure(active=True, idle_minutes=20)
    original_request = requests.sessions.Session.request

    def network_disabled(*_args, **_kwargs):
        raise RuntimeError("Network access is disabled during the local smoke test.")

    requests.sessions.Session.request = network_disabled
    report = {"success": False, "transcripts": [], "rewrite_providers": list(REWRITE_PROVIDER_IDS)}
    try:
        assert service.snapshot()["state"] == "Unloaded"
        for clip in args.wav:
            attempt = service.begin_attempt()
            try:
                text = attempt.transcribe(Path(clip).read_bytes())
                assert text, "The clip returned an empty transcript."
                report["transcripts"].append({"clip": Path(clip).name, "text": text})
            finally:
                attempt.close()
        now[0] = 1200
        assert service.check_idle(), "Idle expiry did not unload the model."
        attempt = service.begin_attempt()
        try:
            assert attempt.transcribe(Path(args.wav[0]).read_bytes()) == report["transcripts"][0]["text"]
        finally:
            attempt.close()
        report.update(success=True, idle_unload=True, reload=True, http_disabled=True)
    except Exception as exc:
        report["error"] = str(exc)
    finally:
        requests.sessions.Session.request = original_request
        service.close()
    Path(args.output).write_text(json.dumps(report, indent=2), encoding="utf-8")
    return 0 if report["success"] else 1
