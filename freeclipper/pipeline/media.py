"""Thin wrappers around ffmpeg / ffprobe."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

from .. import config


class MediaError(RuntimeError):
    pass


# Container formats that can make ffmpeg open other files or network streams (playlists, concat lists,
# session descriptions). A user upload must be a real media file, so these are refused.
_UNSAFE_FORMATS = {"hls", "applehttp", "concat", "ffconcat", "sdp", "rtp", "rtsp", "tee", "lavfi",
                   "data", "tty", "image2", "image2pipe", "ffmetadata", "subviewer", "webvtt", "srt", "ass"}


def run(cmd: list[str], cwd: Path | None = None, timeout: float | None = None) -> subprocess.CompletedProcess:
    try:
        proc = subprocess.run(
            cmd, cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise MediaError(f"{Path(cmd[0]).name} took longer than {timeout:.0f}s and was stopped.") from exc
    if proc.returncode != 0:
        tail = "\n".join((proc.stderr or "").strip().splitlines()[-15:])
        raise MediaError(f"{Path(cmd[0]).name} failed ({proc.returncode}):\n{tail}")
    return proc


def probe(path: Path) -> dict:
    proc = run([
        config.FFPROBE, "-v", "error", "-protocol_whitelist", "file", "-print_format", "json",
        "-show_format", "-show_streams", str(path),
    ], timeout=60)
    data = json.loads(proc.stdout or "{}")
    formats = set((data.get("format", {}).get("format_name") or "").split(","))
    if formats & _UNSAFE_FORMATS:
        raise MediaError("This file isn't a supported video or audio file.")
    video = next((s for s in data.get("streams", []) if s.get("codec_type") == "video"
                  and not s.get("disposition", {}).get("attached_pic")), None)
    audio = next((s for s in data.get("streams", []) if s.get("codec_type") == "audio"), None)
    duration = float(data.get("format", {}).get("duration") or 0)
    info = {"duration": duration, "has_audio": audio is not None, "has_video": video is not None,
            "width": 0, "height": 0, "fps": 30.0}
    if video:
        w, h = int(video.get("width") or 0), int(video.get("height") or 0)
        rotation = 0
        for sd in video.get("side_data_list", []) or []:
            if "rotation" in sd:
                rotation = int(float(sd["rotation"]))
        if abs(rotation) in (90, 270):
            w, h = h, w
        info["width"], info["height"] = w, h
        try:
            num, den = (video.get("avg_frame_rate") or "30/1").split("/")
            fps = float(num) / float(den) if float(den) else 30.0
            info["fps"] = fps if 1 <= fps <= 240 else 30.0
        except (ValueError, ZeroDivisionError):
            pass
    if duration <= 0:
        raise MediaError("Could not read the media duration. Is this a valid video or audio file?")
    return info


def thread_args() -> list[str]:
    return ["-threads", str(config.FFMPEG_THREADS)] if config.FFMPEG_THREADS else []


def extract_audio(src: Path, dest: Path) -> None:
    tmp = dest.with_suffix(".tmp.wav")
    run([config.FFMPEG, "-y", "-v", "error", "-protocol_whitelist", "file", "-i", str(src), "-vn",
         "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", *thread_args(), str(tmp)],
        timeout=config.JOB_TIMEOUT_SECONDS)
    tmp.replace(dest)  # never leave a half-written file that a retry would trust


def thumbnail(src: Path, dest: Path, at: float) -> None:
    run([config.FFMPEG, "-y", "-v", "error", "-ss", f"{max(0.0, at):.3f}", "-i", str(src),
         "-frames:v", "1", "-q:v", "3", str(dest)], timeout=120)
