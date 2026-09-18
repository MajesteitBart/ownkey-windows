"""WAV helpers, resampling and the durable chunk writer.

Every track is stored as mono 16-bit 16 kHz WAV chunks. Chunks are written
to a temporary file, flushed to disk and renamed before their row is
committed, so a crash leaves at most one partial chunk behind and never a
committed row without audio.
"""

from __future__ import annotations

import io
import math
import os
import struct
import wave
from pathlib import Path

import numpy as np

SAMPLE_RATE = 16000
CHUNK_SECONDS = 5.0
CHUNK_SAMPLES = int(SAMPLE_RATE * CHUNK_SECONDS)

# Same meter mapping as dictation: -52 dBFS reads 0, -11 dBFS reads 1.
LEVEL_DB_FLOOR = -52.0
LEVEL_DB_CEILING = -11.0


def to_mono_float(block: np.ndarray) -> np.ndarray:
    """Average channels into float32 mono in the range -1..1."""
    data = np.asarray(block)
    if data.ndim == 2:
        data = data.mean(axis=1)
    if data.dtype == np.int16:
        return data.astype(np.float32) / 32768.0
    if data.dtype == np.int32:
        return data.astype(np.float32) / 2147483648.0
    return data.astype(np.float32)


def resample(samples: np.ndarray, rate_in: int, rate_out: int = SAMPLE_RATE) -> np.ndarray:
    """Resample float32 mono audio. Exact decimation averages whole groups of
    samples; other ratios use linear interpolation, which is enough for speech
    recognition at 16 kHz."""
    samples = np.asarray(samples, dtype=np.float32)
    if rate_in == rate_out or samples.size == 0:
        return samples
    if rate_in > rate_out and rate_in % rate_out == 0:
        factor = rate_in // rate_out
        usable = (samples.size // factor) * factor
        return samples[:usable].reshape(-1, factor).mean(axis=1).astype(np.float32)
    count = int(round(samples.size * rate_out / rate_in))
    if count <= 0:
        return np.zeros(0, dtype=np.float32)
    positions = np.linspace(0, samples.size - 1, count)
    return np.interp(positions, np.arange(samples.size), samples).astype(np.float32)


def float_to_int16(samples: np.ndarray) -> np.ndarray:
    clipped = np.clip(np.asarray(samples, dtype=np.float32), -1.0, 1.0)
    return (clipped * 32767.0).astype(np.int16)


def rms_db(samples: np.ndarray) -> float:
    """dBFS of an int16 or float block; silence reads -120."""
    data = to_mono_float(samples)
    if data.size == 0:
        return -120.0
    rms = float(np.sqrt(np.mean(data.astype(np.float64) ** 2)))
    return 20.0 * math.log10(max(rms, 1e-6))


def meter_level(samples: np.ndarray) -> float:
    """0..1 meter reading with the dictation meter's floor and ceiling."""
    db = rms_db(samples)
    return max(0.0, min(1.0, (db - LEVEL_DB_FLOOR) / (LEVEL_DB_CEILING - LEVEL_DB_FLOOR)))


def wav_bytes(samples: np.ndarray, rate: int = SAMPLE_RATE) -> bytes:
    output = io.BytesIO()
    with wave.open(output, "wb") as target:
        target.setnchannels(1)
        target.setsampwidth(2)
        target.setframerate(rate)
        target.writeframes(np.asarray(samples, dtype=np.int16).tobytes())
    return output.getvalue()


def write_wav(path: str | os.PathLike, samples: np.ndarray, rate: int = SAMPLE_RATE) -> None:
    """Write a WAV file durably: temp file, fsync, rename."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".part")
    with open(temporary, "wb") as handle:
        handle.write(wav_bytes(samples, rate))
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def read_wav(path: str | os.PathLike) -> tuple[np.ndarray, int]:
    with wave.open(str(path), "rb") as source:
        rate = source.getframerate()
        channels = source.getnchannels()
        width = source.getsampwidth()
        frames = source.readframes(source.getnframes())
    if width != 2:
        raise ValueError("Only 16-bit WAV is supported.")
    data = np.frombuffer(frames, dtype="<i2")
    if channels > 1:
        data = float_to_int16(to_mono_float(data.reshape(-1, channels)))
    return data, rate


def read_wav_16k(path: str | os.PathLike) -> np.ndarray:
    data, rate = read_wav(path)
    if rate == SAMPLE_RATE:
        return data
    return float_to_int16(resample(to_mono_float(data), rate, SAMPLE_RATE))


def concat_chunks(chunks: list[dict]) -> np.ndarray:
    """Join committed chunk files into one int16 track. Missing samples between
    chunks (a chunk lost to a crash) are filled with silence so timestamps
    keep their meaning."""
    parts: list[np.ndarray] = []
    position = 0
    for chunk in sorted(chunks, key=lambda item: item["seq"]):
        start = int(chunk["start_sample"])
        if start > position:
            parts.append(np.zeros(start - position, dtype=np.int16))
            position = start
        data = read_wav_16k(chunk["path"])
        parts.append(data)
        position = start + data.size
    if not parts:
        return np.zeros(0, dtype=np.int16)
    return np.concatenate(parts)


class ChunkedWav:
    """A seekable WAV representation of committed chunks without joining tracks.

    Ranges are byte offsets, including the 44-byte PCM header. Gaps retain their
    original duration as silence. Each read holds at most one bounded block.
    """

    BLOCK_BYTES = 64 * 1024

    def __init__(self, chunks: list[dict]):
        self.chunks = sorted((dict(c) for c in chunks), key=lambda c: c['start_sample'])
        samples = max((int(c['start_sample']) + int(c['n_samples']) for c in self.chunks), default=0)
        data_size = samples * 2
        if data_size > 0xffffffff - 36:
            raise ValueError('Recording exceeds the WAV size limit.')
        self.header = struct.pack('<4sI4s4sIHHIIHH4sI', b'RIFF', data_size + 36, b'WAVE',
                                  b'fmt ', 16, 1, 1, SAMPLE_RATE, SAMPLE_RATE * 2, 2, 16, b'data', data_size)
        self.size = len(self.header) + data_size

    def iter_range(self, start: int = 0, end: int | None = None):
        """Yield an inclusive byte range; support unaligned browser seeks."""
        end = self.size - 1 if end is None else min(end, self.size - 1)
        if start < 0 or start > end:
            raise ValueError('Invalid audio byte range.')
        if start < len(self.header):
            yield self.header[start:min(end + 1, len(self.header))]
        position = max(start, len(self.header)) - len(self.header)
        stop = end + 1 - len(self.header)
        for chunk in self.chunks:
            if position >= stop:
                return
            chunk_start = int(chunk['start_sample']) * 2
            chunk_end = chunk_start + int(chunk['n_samples']) * 2
            if chunk_end <= position:
                continue
            while position < min(chunk_start, stop):
                count = min(self.BLOCK_BYTES, chunk_start - position, stop - position)
                yield bytes(count)
                position += count
            if position >= stop:
                return
            with wave.open(str(chunk['path']), 'rb') as source:
                if (source.getnchannels(), source.getsampwidth(), source.getframerate()) != (1, 2, SAMPLE_RATE):
                    raise ValueError('Playback requires mono 16-bit 16 kHz chunks.')
                while position < min(chunk_end, stop):
                    offset = position - chunk_start
                    skip = offset % 2
                    count = min(self.BLOCK_BYTES - skip, chunk_end - position, stop - position)
                    source.setpos(offset // 2)
                    data = source.readframes((skip + count + 1) // 2)[skip:skip + count]
                    if len(data) != count:
                        raise OSError('A recorded audio chunk is incomplete.')
                    yield data
                    position += count


class ChunkWriter:
    """Accumulate one track's samples and persist them in fixed-size chunks.

    ``on_commit(seq, start_sample, n_samples, path)`` runs after each file is
    durable; the caller records the row. ``flush`` writes the remainder.
    """

    def __init__(self, directory: str | os.PathLike, on_commit, chunk_samples: int = CHUNK_SAMPLES):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.on_commit = on_commit
        self.chunk_samples = int(chunk_samples)
        self.seq = 0
        self.committed_samples = 0
        self._pending: list[np.ndarray] = []
        self._pending_count = 0

    @property
    def total_samples(self) -> int:
        return self.committed_samples + self._pending_count

    def pending_samples(self) -> np.ndarray:
        """Copy the unwritten tail while the caller holds the capture lock."""
        return np.concatenate(self._pending) if self._pending else np.zeros(0, dtype=np.int16)

    def append(self, samples: np.ndarray) -> int:
        """Buffer samples; write every full chunk. Returns chunks written."""
        data = np.asarray(samples, dtype=np.int16)
        if data.size == 0:
            return 0
        self._pending.append(data)
        self._pending_count += data.size
        written = 0
        while self._pending_count >= self.chunk_samples:
            self._write(self.chunk_samples)
            written += 1
        return written

    def flush(self) -> bool:
        """Write whatever is buffered as a final, shorter chunk."""
        if self._pending_count == 0:
            return False
        self._write(self._pending_count)
        return True

    def _write(self, count: int) -> None:
        buffer = np.concatenate(self._pending) if len(self._pending) > 1 else self._pending[0]
        chunk, rest = buffer[:count], buffer[count:]
        self._pending = [rest] if rest.size else []
        self._pending_count = int(rest.size)
        path = self.directory / f"chunk-{self.seq:06d}.wav"
        write_wav(path, chunk)
        start = self.committed_samples
        self.seq += 1
        self.committed_samples += int(chunk.size)
        self.on_commit(self.seq - 1, start, int(chunk.size), str(path))
