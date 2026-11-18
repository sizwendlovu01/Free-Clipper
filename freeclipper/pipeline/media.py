"""Thin wrappers around ffmpeg / ffprobe."""
from __future__ import annotations

import json
import subprocess
import threading
from pathlib import Path

from .. import config


class MediaError(RuntimeError):
    pass


# Container formats that can make ffmpeg open other files or network streams (playlists, concat lists,
# session descriptions). A user upload must be a real media file, so these are refused.
_UNSAFE_FORMATS = {"hls", "applehttp", "concat", "ffconcat", "sdp", "rtp", "rtsp", "tee", "lavfi",
                   "data", "tty", "image2", "image2pipe", "ffmetadata", "subviewer", "webvtt", "srt", "ass"}


# Every ffmpeg/ffprobe process is registered under the job it works for, so cancelling or deleting a job
# stops its processes at once instead of letting a long render run to the end.
_ctx = threading.local()
_procs: dict[str, set[subprocess.Popen]] = {}
_procs_lock = threading.Lock()
_killed: set[str] = set()


def set_job(job_id: str | None) -> None:
    """Tag the media processes started by this thread with a job id (None to clear)."""
    _ctx.job = job_id


def kill_job(job_id: str) -> int:
    """Terminate every running media process of a job. Returns how many were stopped."""
    with _procs_lock:
        procs = list(_procs.get(job_id, ()))
        if procs:
            _killed.add(job_id)
    for proc in procs:
        try:
            proc.kill()
        except OSError:
            pass
    return len(procs)


def forget_job(job_id: str) -> None:
    with _procs_lock:
        _procs.pop(job_id, None)
        _killed.discard(job_id)


class stream:
    """Popen for reading a process's output as it runs, registered under the current job so cancelling
    or deleting the job stops it. Use as a context manager; the process is always reaped."""

    def __init__(self, cmd: list[str]):
        self.job = getattr(_ctx, "job", None)
        self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        if self.job:
            with _procs_lock:
                _procs.setdefault(self.job, set()).add(self.proc)

    def __enter__(self) -> subprocess.Popen:
        return self.proc

    def __exit__(self, *exc) -> None:
        if self.proc.poll() is None:
            self.proc.kill()
        self.proc.stdout.close()
        self.proc.wait()
        if self.job:
            with _procs_lock:
                _procs.get(self.job, set()).discard(self.proc)


def run(cmd: list[str], cwd: Path | None = None, timeout: float | None = None) -> subprocess.CompletedProcess:
    job = getattr(_ctx, "job", None)
    popen = subprocess.Popen(cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                             encoding="utf-8", errors="replace")
    if job:
        with _procs_lock:
            _procs.setdefault(job, set()).add(popen)
    try:
        try:
            out, err = popen.communicate(timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            popen.kill()
            popen.communicate()
            raise MediaError(f"{Path(cmd[0]).name} took longer than {timeout:.0f}s and was stopped.") from exc
    finally:
        if job:
            with _procs_lock:
                _procs.get(job, set()).discard(popen)
    proc = subprocess.CompletedProcess(cmd, popen.returncode, out, err)
    if proc.returncode != 0 and job and job in _killed:
        raise MediaError("Stopped because the project was cancelled or deleted.")
    if proc.returncode != 0:
        stderr = proc.stderr or ""
        if "No space left on device" in stderr:
            raise MediaError("The server ran out of storage space. Delete old projects, choose fewer clips, "
                             "or ask the admin to enlarge the storage volume.")
        if proc.returncode in (-9, 137) or "Killed" in stderr[-200:]:
            raise MediaError("The server ran out of memory while processing. Try fewer clips or a shorter "
                             "video, or ask the admin to give the server more memory.")
        tail = "\n".join(stderr.strip().splitlines()[-15:])
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
