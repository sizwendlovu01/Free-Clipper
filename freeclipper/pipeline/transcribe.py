"""Local speech-to-text with word timestamps using faster-whisper (99 languages).

Uses faster-whisper's batched pipeline: audio is split on speech (VAD) into <=30 s chunks and
decoded in batches. Memory stays bounded for multi-hour videos (the sequential mode builds one
spectrogram for the whole file, ~1 GB per hour) and it is several times faster.
"""
from __future__ import annotations

import threading
import wave
from pathlib import Path
from typing import Callable

import numpy as np

from .. import config
from .captions import joiner

_models: dict[tuple[str, str, str], object] = {}
_lock = threading.Lock()


def _device() -> tuple[str, str]:
    device = config.WHISPER_DEVICE
    if device == "auto":
        try:
            import ctranslate2
            device = "cuda" if ctranslate2.get_cuda_device_count() > 0 else "cpu"
        except Exception:
            device = "cpu"
    compute = config.WHISPER_COMPUTE_TYPE
    if compute == "auto":
        compute = "float16" if device == "cuda" else "int8"
    return device, compute


def get_model(size: str):
    """Shared model instance. Only one model size stays loaded: switching sizes releases the previous
    one (a job still using it keeps its own reference until it finishes), so RAM holds one model."""
    from faster_whisper import WhisperModel

    if size not in config.WHISPER_MODELS:
        size = config.WHISPER_MODEL
    device, compute = _device()
    key = (size, device, compute)
    with _lock:
        if key not in _models:
            _models.clear()
            _models[key] = WhisperModel(
                size, device=device, compute_type=compute,
                cpu_threads=config.WHISPER_CPU_THREADS,
                num_workers=config.MAX_CONCURRENT_JOBS,
                download_root=str(config.MODEL_DIR),
            )
        return _models[key]


def model_cached(size: str) -> bool:
    """True if the Whisper model is already on disk (otherwise first use downloads it)."""
    try:
        from faster_whisper.utils import _MODELS
        repo = _MODELS.get(size, size)
    except ImportError:
        return True
    folder = config.MODEL_DIR / ("models--" + repo.replace("/", "--"))
    return folder.exists() and any(folder.rglob("model.bin"))


def preload(size: str) -> None:
    """Download a model's files into MODEL_DIR without loading it into memory."""
    from faster_whisper.utils import download_model

    if not model_cached(size):
        download_model(size, cache_dir=str(config.MODEL_DIR))


def load_wav(audio: Path) -> np.ndarray:
    with wave.open(str(audio), "rb") as wf:
        if wf.getframerate() != 16000 or wf.getnchannels() != 1 or wf.getsampwidth() != 2:
            raise ValueError("Expected 16 kHz mono 16-bit WAV audio.")
        return np.frombuffer(wf.readframes(wf.getnframes()), dtype=np.int16).astype(np.float32) / 32768.0


CAPTIONS_MODEL = "platform-captions"


def from_json3(path: Path, language: str, duration: float | None) -> dict:
    """Convert YouTube json3 speech-recognition captions (per-word offsets) to our transcript format."""
    import json

    data = json.loads(path.read_text(encoding="utf-8"))
    raw: list[tuple[float, float, list[tuple[float, str]]]] = []
    for ev in data.get("events", []):
        segs = ev.get("segs") or []
        start = ev.get("tStartMs", 0) / 1000
        words = [(start + s.get("tOffsetMs", 0) / 1000, s.get("utf8", "").strip()) for s in segs]
        words = [(t, w) for t, w in words if w and w != "[Music]"]
        if words:
            raw.append((start, start + ev.get("dDurationMs", 0) / 1000, words))
    all_words: list[dict] = []
    for i, (start, end, words) in enumerate(raw):
        next_start = raw[i + 1][2][0][0] if i + 1 < len(raw) else end
        limit = min(end, next_start) if next_start > words[-1][0] else end
        for j, (t, w) in enumerate(words):
            nxt = words[j + 1][0] if j + 1 < len(words) else (limit if limit > t + 0.05 else t + 0.25)
            # json3 gives word starts only; estimate how long the word is spoken so real pauses show.
            spoken = 0.12 + 0.065 * len(w)
            all_words.append({"start": round(t, 3), "end": round(max(t + 0.05, min(nxt, t + spoken)), 3),
                              "word": w, "p": 1.0})
    all_words.sort(key=lambda w: w["start"])
    # Caption lines break wherever the line is full, mid-sentence. Rebuild phrases from speech pauses
    # (auto captions have little punctuation), so clip boundaries land where the speaker pauses.
    segments = phrases(all_words, joiner(language))
    total = float(duration or (segments[-1]["end"] if segments else 0))
    return {"language": language, "language_probability": 1.0, "duration": total,
            "model": CAPTIONS_MODEL, "task": "transcribe", "segments": segments}


_SENTENCE_END = (".", "?", "!", "。", "？", "！", "…")


def phrases(words: list[dict], sep: str = " ") -> list[dict]:
    """Group timed words into sentence/phrase segments: split after sentence punctuation, at speech
    pauses, and before a phrase grows too long. These are the boundaries clips start and end on."""
    groups: list[list[dict]] = []
    cur: list[dict] = []
    for w in words:
        if cur:
            gap = w["start"] - cur[-1]["end"]
            length = w["end"] - cur[0]["start"]
            sentence_done = cur[-1]["word"].endswith(_SENTENCE_END) and cur[-1]["end"] - cur[0]["start"] >= 1.5
            if (sentence_done or gap >= 0.4 or (length > 8 and gap >= 0.2) or (length > 14 and gap >= 0.08)
                    or length > 20):
                groups.append(cur)
                cur = []
        cur.append(w)
    if cur:
        groups.append(cur)
    return [{"start": g[0]["start"], "end": g[-1]["end"], "text": sep.join(w["word"] for w in g).strip(),
             "words": g} for g in groups]


def transcribe(audio: Path, model_size: str, language: str | None, task: str,
               progress: Callable[[float], None], should_stop: Callable[[], bool]) -> dict:
    from faster_whisper import BatchedInferencePipeline

    model = get_model(model_size)
    samples = load_wav(audio)
    device, _ = _device()
    pipeline = BatchedInferencePipeline(model)
    try:
        segments, info = pipeline.transcribe(
            samples,
            language=language or None,
            task=task,
            batch_size=config.WHISPER_BATCH_SIZE or (16 if device == "cuda" else 8),
            word_timestamps=True,
            vad_filter=True,
            beam_size=5,
            condition_on_previous_text=False,
        )
        total = float(info.duration or 0) or 1.0
        out = []
        for seg in segments:
            if should_stop():
                raise InterruptedError("Cancelled")
            words = [
                {"start": round(w.start, 3), "end": round(w.end, 3), "word": w.word.strip(),
                 "p": round(w.probability, 3)}
                for w in (seg.words or []) if w.word.strip()
            ]
            text = seg.text.strip()
            if text:
                out.append({"start": round(seg.start, 3), "end": round(seg.end, 3), "text": text, "words": words})
            progress(min(1.0, seg.end / total))
    except MemoryError as exc:
        raise RuntimeError("Ran out of memory while transcribing. Close other apps, or pick a smaller "
                           "transcription model or batch size (FC_WHISPER_BATCH_SIZE).") from exc
    except Exception as exc:
        if "Unable to allocate" in str(exc):
            raise RuntimeError("Ran out of memory while transcribing. Close other apps, or pick a smaller "
                               "transcription model or batch size (FC_WHISPER_BATCH_SIZE).") from exc
        raise
    out.sort(key=lambda s: s["start"])
    # The batched pipeline returns ~30 s chunks; rebuild sentence/phrase segments from the word timings
    # so clips can start and end on real sentence boundaries. Chunks without word timings are kept.
    if out and all(s["words"] for s in out):
        out = phrases([w for s in out for w in s["words"]], joiner(info.language))
    return {
        "language": info.language,
        "language_probability": round(float(info.language_probability or 0), 3),
        "duration": float(info.duration or 0),
        "model": model_size,
        "task": task,
        "segments": out,
    }
