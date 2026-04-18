"""DJ audio engine.

Loads tracks from a directory, maintains a queue, and streams stereo audio
to a chosen output device (e.g. VB-Audio Virtual Cable "CABLE Input").
Supports crossfade between tracks, skip, pause, volume control.

The engine runs a background thread that pushes audio frames to a
sounddevice OutputStream. All queue mutations are protected by a lock.
"""

from __future__ import annotations

import random
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

import numpy as np
import sounddevice as sd
from pydub import AudioSegment


AUDIO_EXTS = {".mp3", ".wav", ".flac", ".m4a", ".ogg", ".aac"}


@dataclass
class Track:
    path: Path
    samples: np.ndarray  # shape (frames, 2), float32, range [-1, 1]
    sample_rate: int

    @property
    def name(self) -> str:
        return self.path.stem


@dataclass
class EngineState:
    current: Optional[Track] = None
    next_up: Optional[Track] = None
    queue: list[Path] = field(default_factory=list)
    volume: float = 0.9
    paused: bool = False


def find_output_devices(name_substring: str) -> list[int]:
    """Return all device indices whose name matches, ranked by host-API preference.

    WASAPI shared mode is tried first (low latency, clean 2-channel), then
    DirectSound, then MME. We return the full ranked list so the caller
    can fall back if one variant refuses to open.
    """
    ns = name_substring.lower()
    hostapis = sd.query_hostapis()

    def rank(hostapi_name: str) -> int:
        n = hostapi_name.lower()
        if "wasapi" in n:
            return 0
        if "directsound" in n:
            return 1
        if "mme" in n:
            return 2
        return 3

    matches: list[tuple[int, int]] = []
    for idx, dev in enumerate(sd.query_devices()):
        if dev["max_output_channels"] > 0 and ns in dev["name"].lower():
            matches.append((rank(hostapis[dev["hostapi"]]["name"]), idx))

    if not matches:
        available = [d["name"] for d in sd.query_devices() if d["max_output_channels"] > 0]
        raise RuntimeError(
            f"No output device matching '{name_substring}'. "
            f"Available outputs: {available}"
        )
    matches.sort()
    return [idx for _, idx in matches]


def open_output_stream(
    candidates: list[int], preferred_sample_rate: int, blocksize: int
) -> tuple[sd.OutputStream, int, int]:
    """Try each candidate device until one opens. Returns (stream, idx, sr).

    Strategy: for each device, try (native_rate, 48000, 44100), with WASAPI's
    auto_convert enabled so the driver reconciles any format mismatch.
    blocksize=0 lets PortAudio pick — more reliable across host APIs than a
    hard-coded 2048.
    """
    hostapis = sd.query_hostapis()
    errors: list[str] = []

    for idx in candidates:
        dev = sd.query_devices(idx)
        hostapi_name = hostapis[dev["hostapi"]]["name"].lower()
        native = int(dev.get("default_samplerate") or preferred_sample_rate)
        rates = list(dict.fromkeys([native, 48000, 44100, preferred_sample_rate]))

        extra = None
        if "wasapi" in hostapi_name:
            try:
                extra = sd.WasapiSettings(auto_convert=True)
            except Exception:
                extra = None

        for sr in rates:
            for bs in (0, blocksize):
                try:
                    stream = sd.OutputStream(
                        samplerate=sr,
                        channels=2,
                        dtype="float32",
                        device=idx,
                        blocksize=bs,
                        extra_settings=extra,
                    )
                    stream.start()
                    print(
                        f"[dj] opened device {idx} '{dev['name']}' "
                        f"@ {sr}Hz (host={hostapi_name}, blocksize={bs})"
                    )
                    return stream, idx, sr
                except Exception as e:
                    errors.append(f"dev {idx} [{hostapi_name}] {sr}Hz bs={bs}: {e}")
                    continue

    raise RuntimeError(
        "Could not open any output device. Tried:\n  " + "\n  ".join(errors)
    )


def load_track(path: Path, target_sr: int) -> Track:
    seg = AudioSegment.from_file(path)
    seg = seg.set_channels(2).set_frame_rate(target_sr).set_sample_width(2)
    raw = np.frombuffer(seg.raw_data, dtype=np.int16).astype(np.float32) / 32768.0
    samples = raw.reshape(-1, 2)
    return Track(path=path, samples=samples, sample_rate=target_sr)


def scan_library(music_dir: Path) -> list[Path]:
    if not music_dir.exists():
        return []
    return sorted(p for p in music_dir.rglob("*") if p.suffix.lower() in AUDIO_EXTS)


class DJEngine:
    def __init__(
        self,
        music_dir: str,
        output_device_substring: str,
        sample_rate: int = 44100,
        crossfade_seconds: float = 4.0,
        default_volume: float = 0.9,
        shuffle: bool = True,
        on_track_change: Optional[Callable[[Track], None]] = None,
        extra_library_dirs: Optional[list[str]] = None,
    ):
        self.music_dir = Path(music_dir)
        self.extra_library_dirs = [Path(p) for p in (extra_library_dirs or [])]
        # Rank candidates now; pick the actual device + rate when we open
        # the stream (see _run) because some WASAPI devices only reveal
        # their constraints at open time.
        self.device_candidates = find_output_devices(output_device_substring)
        self.preferred_sample_rate = sample_rate
        self.crossfade_seconds = crossfade_seconds
        # sample_rate / crossfade_frames are finalized once the stream opens.
        self.sample_rate = sample_rate
        self.crossfade_frames = int(crossfade_seconds * sample_rate)
        self.state = EngineState(volume=default_volume)
        self.shuffle = shuffle
        self.on_track_change = on_track_change

        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._skip_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

        # Populate initial queue from music/ + any extra dirs (e.g. yt cache).
        library = self._full_library()
        if shuffle:
            random.shuffle(library)
        self.state.queue = library

    def _full_library(self) -> list[Path]:
        """Union of music_dir + every extra dir, deduplicated."""
        paths = scan_library(self.music_dir)
        for extra in self.extra_library_dirs:
            if extra.exists():
                paths.extend(scan_library(extra))
        seen: set[Path] = set()
        uniq: list[Path] = []
        for p in paths:
            key = p.resolve()
            if key in seen:
                continue
            seen.add(key)
            uniq.append(p)
        return uniq

    # ---------- public controls ----------

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, name="dj-engine", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        self._skip_event.set()
        if self._thread:
            self._thread.join(timeout=5)

    def skip(self) -> None:
        self._skip_event.set()

    def pause(self) -> None:
        with self._lock:
            self.state.paused = True

    def resume(self) -> None:
        with self._lock:
            self.state.paused = False

    def set_volume(self, vol: float) -> None:
        with self._lock:
            self.state.volume = max(0.0, min(1.5, vol))

    def enqueue(self, query: str) -> Optional[Path]:
        """Match a file in the library by substring and append to queue."""
        q = query.lower().strip()
        for path in scan_library(self.music_dir):
            if q in path.stem.lower():
                with self._lock:
                    self.state.queue.append(path)
                return path
        return None

    def enqueue_path(self, path: Path) -> Path:
        """Append an already-resolved absolute path to the queue."""
        with self._lock:
            self.state.queue.append(path)
        return path

    def shuffle_queue(self) -> None:
        with self._lock:
            random.shuffle(self.state.queue)

    def now_playing(self) -> Optional[str]:
        with self._lock:
            return self.state.current.name if self.state.current else None

    def list_queue(self, limit: int = 10) -> list[str]:
        with self._lock:
            return [p.stem for p in self.state.queue[:limit]]

    # ---------- internal playback loop ----------

    def _next_track(self) -> Optional[Track]:
        with self._lock:
            if not self.state.queue:
                # Refill from music/ + yt cache. Everything ever downloaded
                # stays available, so the set loops instead of dying.
                library = self._full_library()
                if not library:
                    return None
                if self.shuffle:
                    random.shuffle(library)
                self.state.queue = library
                print(f"[dj] looping library ({len(library)} tracks)")
            path = self.state.queue.pop(0)
        try:
            return load_track(path, self.sample_rate)
        except Exception as e:
            print(f"[dj] failed to load {path}: {e}")
            return self._next_track()

    def _run(self) -> None:
        blocksize = 2048
        stream, _, actual_sr = open_output_stream(
            self.device_candidates, self.preferred_sample_rate, blocksize
        )
        self.sample_rate = actual_sr
        self.crossfade_frames = int(self.crossfade_seconds * actual_sr)
        silence = np.zeros((blocksize, 2), dtype=np.float32)
        warned_empty = False
        try:
            current: Optional[Track] = None
            cursor = 0

            while not self._stop_event.is_set():
                # Acquire a track if we don't have one. Idle with silence
                # (keeps the stream alive) instead of exiting — an empty
                # music/ dir is recoverable once `!yt` adds something.
                if current is None:
                    current = self._next_track()
                    if current is None:
                        if not warned_empty:
                            print("[dj] queue empty — waiting for tracks (use !yt or drop files in music/)")
                            warned_empty = True
                        stream.write(silence)
                        time.sleep(0.1)
                        continue
                    warned_empty = False
                    self._announce(current)
                    cursor = 0

                if self.state.paused:
                    stream.write(silence)
                    time.sleep(0.01)
                    continue

                remaining = len(current.samples) - cursor

                # Time to transition: either we're near the end, or a skip was requested.
                if remaining <= self.crossfade_frames or self._skip_event.is_set():
                    next_track = self._next_track()
                    if next_track is None:
                        # Nothing queued — finish current track cleanly, then idle.
                        if self._skip_event.is_set() or remaining <= 0:
                            current = None
                            self._skip_event.clear()
                            continue
                        chunk = current.samples[cursor:] * self.state.volume
                        stream.write(chunk.astype(np.float32))
                        current = None
                        continue
                    current, cursor = self._crossfade(stream, current, cursor, next_track)
                    self._announce(current)
                    self._skip_event.clear()
                    continue

                end = min(cursor + blocksize, len(current.samples) - self.crossfade_frames)
                chunk = current.samples[cursor:end] * self.state.volume
                stream.write(chunk.astype(np.float32))
                cursor = end
        finally:
            stream.stop()
            stream.close()

    def _crossfade(
        self,
        stream: sd.OutputStream,
        a: Track,
        cursor: int,
        b: Track,
    ) -> tuple[Track, int]:
        """Linear equal-power crossfade from tail of `a` into head of `b`.

        Returns (new_current, new_cursor_into_b).
        """
        n = self.crossfade_frames
        a_tail = a.samples[cursor : cursor + n]
        # Pad if track ended early.
        if len(a_tail) < n:
            pad = np.zeros((n - len(a_tail), 2), dtype=np.float32)
            a_tail = np.vstack([a_tail, pad])
        b_head = b.samples[:n]
        if len(b_head) < n:
            n = len(b_head)
            a_tail = a_tail[:n]

        # Equal-power curve: cos/sin of quarter circle.
        t = np.linspace(0.0, np.pi / 2, n, dtype=np.float32)
        fade_out = np.cos(t)[:, None]
        fade_in = np.sin(t)[:, None]
        mixed = (a_tail * fade_out + b_head * fade_in) * self.state.volume

        # Write in blocksize-ish chunks so volume/skip changes remain responsive.
        step = 2048
        for i in range(0, n, step):
            stream.write(mixed[i : i + step].astype(np.float32))
        return b, n

    def _announce(self, track: Track) -> None:
        with self._lock:
            self.state.current = track
        if self.on_track_change:
            try:
                self.on_track_change(track)
            except Exception as e:
                print(f"[dj] on_track_change error: {e}")
        print(f"[dj] now playing: {track.name}")
