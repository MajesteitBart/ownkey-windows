"""Measure the pinned CPU loader with local mono 16-bit, 16 kHz WAV files.

Run from source or freeze with PyInstaller --onedir --collect-all sherpa_onnx.
Requires psutil only for this development benchmark. Never downloads a model.
"""

import argparse
import gc
import io
import json
import os
from pathlib import Path
import threading
import time
import wave

import numpy as np
import psutil


def load_recognizer(directory, threads=4):
    import sherpa_onnx

    directory = Path(directory)
    return sherpa_onnx.OfflineRecognizer.from_transducer(
        encoder=str(directory / "encoder.int8.onnx"),
        decoder=str(directory / "decoder.int8.onnx"),
        joiner=str(directory / "joiner.int8.onnx"),
        tokens=str(directory / "tokens.txt"),
        num_threads=threads, sample_rate=16000, feature_dim=128,
        decoding_method="greedy_search", provider="cpu", model_type="nemo_transducer",
    )


def samples_from_wav(data):
    with wave.open(io.BytesIO(data), "rb") as source:
        if (source.getnchannels(), source.getsampwidth(), source.getframerate()) != (1, 2, 16000):
            raise ValueError("Expected mono int16 WAV at 16 kHz")
        return np.frombuffer(source.readframes(source.getnframes()), dtype="<i2").astype(np.float32) / 32768.0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("model_dir", type=Path)
    parser.add_argument("clips", nargs="+", type=Path)
    parser.add_argument("--threads", type=int, nargs="+", default=[2, 4, os.cpu_count()])
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    process = psutil.Process()
    results = {"machine": os.environ.get("COMPUTERNAME"), "cpu_count": os.cpu_count(), "runs": []}
    for threads in args.threads:
        for cycle in ("cold", "warm"):
            baseline = process.memory_info().rss
            peak = [baseline]
            stop = threading.Event()

            def monitor():
                while not stop.wait(0.02):
                    peak[0] = max(peak[0], process.memory_info().rss)

            monitor_thread = threading.Thread(target=monitor, daemon=True)
            monitor_thread.start()
            started = time.perf_counter()
            recognizer = load_recognizer(args.model_dir, threads)
            load_seconds = time.perf_counter() - started
            run = {"threads": threads, "cycle": cycle, "baseline_rss": baseline,
                   "load_seconds": load_seconds, "loaded_rss": process.memory_info().rss, "clips": []}
            for clip in args.clips:
                samples = samples_from_wav(clip.read_bytes())
                started = time.perf_counter()
                stream = recognizer.create_stream()
                stream.accept_waveform(16000, samples)
                recognizer.decode_stream(stream)
                elapsed = time.perf_counter() - started
                run["clips"].append({"name": clip.name, "seconds": len(samples) / 16000,
                                     "decode_seconds": elapsed, "text": stream.result.text})
                del stream, samples
            run["peak_rss"] = peak[0]
            del recognizer
            gc.collect()
            time.sleep(1)
            run["unloaded_rss"] = process.memory_info().rss
            stop.set()
            monitor_thread.join()
            # Recording can proceed during load; report latency from a 5 s press.
            run["overlap_5s_first_transcript"] = max(5, load_seconds) + run["clips"][0]["decode_seconds"]
            results["runs"].append(run)
            print(json.dumps(run), flush=True)
            args.output.write_text(json.dumps(results, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
