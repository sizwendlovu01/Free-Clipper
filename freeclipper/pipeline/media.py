"""Thin wrappers around ffmpeg / ffprobe."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

from .. import config


class MediaError(RuntimeError):
    pass


def run(cmd: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess:
    proc = subprocess.run(
        cmd, cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if proc.returncode != 0:
        tail = "\n".join((proc.stderr or "").strip().splitlines()[-15:])
        raise MediaError(f"{Path(cmd[0]).name} failed ({proc.returncode}):\n{tail}")
    return proc


def probe(path: Path) -> dict:
    proc = run([
        config.FFPROBE, "-v", "error", "-print_format", "json",
        "-show_format", "-show_streams", str(path),
    ])
    data = json.loads(proc.stdout or "{}")
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


def extract_audio(src: Path, dest: Path) -> None:
    run([config.FFMPEG, "-y", "-v", "error", "-i", str(src), "-vn", "-ac", "1", "-ar", "16000",
         "-c:a", "pcm_s16le", str(dest)])


def thumbnail(src: Path, dest: Path, at: float) -> None:
    run([config.FFMPEG, "-y", "-v", "error", "-ss", f"{max(0.0, at):.3f}", "-i", str(src),
         "-frames:v", "1", "-q:v", "3", str(dest)])
