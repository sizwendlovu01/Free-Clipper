"""Fetch media from a link with yt-dlp.

The audio track is fetched on its own first (a few MB per minute) so transcription can start
while the full video keeps downloading in the background.
"""
from __future__ import annotations

import ipaddress
import shutil
import socket
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse

from .. import config


def _is_public_host(host: str) -> bool:
    """False for localhost, private networks, link-local (cloud metadata) and other internal addresses."""
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return False
    for info in infos:
        ip = ipaddress.ip_address(info[4][0].split("%")[0])
        if (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_reserved
                or ip.is_unspecified or (ip.version == 6 and ip.ipv4_mapped and not ip.ipv4_mapped.is_global)):
            return False
    return True


def validate_url(url: str) -> str:
    """Accept only public http(s) links, so the server can't be used to reach internal services."""
    url = url.strip()
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname or len(url) > 2000:
        raise ValueError("Enter a full http(s) link to the video.")
    if not _is_public_host(parsed.hostname):
        raise ValueError("That link points to a private or unreachable address.")
    return url


@contextmanager
def _access_opts():
    """yt-dlp options for getting past YouTube's bot check from cloud servers: a private copy of the
    admin's cookies file (yt-dlp rewrites it, so parallel downloads must not share one) and a proxy."""
    opts: dict = {}
    copy = None
    if config.YTDLP_PROXY:
        opts["proxy"] = config.YTDLP_PROXY
    if config.YTDLP_COOKIES_FILE.is_file():
        copy = config.TEMP_DIR / f"cookies-{uuid.uuid4().hex}.txt"
        shutil.copyfile(config.YTDLP_COOKIES_FILE, copy)
        opts["cookiefile"] = str(copy)
    try:
        yield opts
    finally:
        if copy:
            copy.unlink(missing_ok=True)


def download_limit_mb() -> int:
    free = int(shutil.disk_usage(config.JOBS_DIR).free / 1024 / 1024)
    return max(1, min(config.MAX_DOWNLOAD_MB, free - 50))


def video_info(url: str) -> dict:
    """Metadata only (title, duration), without downloading media."""
    import yt_dlp

    with _access_opts() as access, yt_dlp.YoutubeDL({
            "quiet": True, "no_warnings": True, "noplaylist": True, "skip_download": True,
            "socket_timeout": 30, "extractor_retries": 3, **access}) as ydl:
        info = ydl.extract_info(url, download=False)
    if info.get("_type") == "playlist":
        raise ValueError("Playlists are not supported; link to a single video.")
    if info.get("is_live"):
        raise ValueError("Live streams can't be clipped; wait until the stream has ended.")
    return {"title": info.get("title"), "duration": info.get("duration")}


def _base_opts(progress: Callable[[float], None], should_stop: Callable[[], bool]) -> dict:
    import yt_dlp

    # Video and audio are separate downloads (each 0-100%); combine them so progress only moves forward.
    state = {"done": 0, "best": 0.0}

    def hook(d: dict) -> None:
        if should_stop():
            raise yt_dlp.utils.DownloadCancelled("Cancelled")
        parts = len((d.get("info_dict") or {}).get("requested_formats") or []) or 1
        if d.get("status") == "finished":
            state["done"] += 1
            fraction = state["done"] / parts
        elif d.get("status") == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            if not total:
                return
            fraction = (state["done"] + min(1.0, d.get("downloaded_bytes", 0) / total)) / parts
        else:
            return
        state["best"] = max(state["best"], min(1.0, fraction))
        progress(state["best"])

    opts = {
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "progress_hooks": [hook],
        "restrictfilenames": True,
        "retries": 10,
        "fragment_retries": 10,
        "extractor_retries": 3,
        "socket_timeout": 30,
        "concurrent_fragment_downloads": 4,
        # Never download more than the volume can hold (keeping 50 MB spare for clips and audio).
        "max_filesize": download_limit_mb() * 1024 * 1024,
    }
    if config.FFMPEG != "ffmpeg":
        opts["ffmpeg_location"] = config.FFMPEG
    return opts


def _run(url: str, opts: dict, dest_dir: Path, stem: str) -> tuple[Path, str]:
    import yt_dlp

    with _access_opts() as access, yt_dlp.YoutubeDL({**opts, **access}) as ydl:
        info = ydl.extract_info(url, download=True)
        if info.get("_type") == "playlist":
            raise ValueError("Playlists are not supported; link to a single video.")
        downloads = info.get("requested_downloads") or []
        path = Path(downloads[0]["filepath"]) if downloads and downloads[0].get("filepath") else None
        if not path or not path.exists():
            candidates = [p for p in sorted(dest_dir.glob(f"{stem}.*")) if not p.name.endswith((".part", ".ytdl"))]
            if not candidates:
                raise RuntimeError(f"No file was downloaded. The video is probably larger than the "
                                   f"{download_limit_mb()} MB this server can store right now. Try a "
                                   f"shorter video, or free up storage.")
            path = candidates[0]
        return path, info.get("title") or url


def download(url: str, dest_dir: Path, progress: Callable[[float], None],
             should_stop: Callable[[], bool]) -> tuple[Path, str]:
    """Full video (best <=1080p) with audio, merged to MP4."""
    opts = _base_opts(progress, should_stop) | {
        "format": "bv*[height<=1080]+ba/b[height<=1080]/bv*+ba/b",
        "merge_output_format": "mp4",
        "outtmpl": str(dest_dir / "source.%(ext)s"),
    }
    return _run(url, opts, dest_dir, "source")


def download_captions(url: str, dest_dir: Path, should_stop: Callable[[], bool]) -> tuple[Path | None, dict]:
    """Fetch the platform's original-language speech-recognition captions (YouTube "<lang>-orig"
    track, json3 format with per-word timing). Seconds instead of a local transcription pass.
    Returns (path or None, info) where info has title, duration and language."""
    import yt_dlp

    opts = _base_opts(lambda _p: None, should_stop) | {
        "skip_download": True,
        "writeautomaticsub": True,
        "subtitleslangs": [r".*-orig"],
        "subtitlesformat": "json3",
        "outtmpl": str(dest_dir / "captions.%(ext)s"),
    }
    with _access_opts() as access, yt_dlp.YoutubeDL({**opts, **access}) as ydl:
        info = ydl.extract_info(url, download=True)
    meta = {"title": info.get("title"), "duration": info.get("duration"), "language": info.get("language")}
    for lang, sub in (info.get("requested_subtitles") or {}).items():
        path = Path(sub.get("filepath") or "")
        if lang.endswith("-orig") and path.suffix == ".json3" and path.exists():
            meta["caption_lang"] = lang[: -len("-orig")]
            return path, meta
    return None, meta


def download_audio(url: str, dest_dir: Path, progress: Callable[[float], None],
                   should_stop: Callable[[], bool]) -> tuple[Path, str]:
    """Audio-only stream. Raises if the site offers no separate audio stream."""
    opts = _base_opts(progress, should_stop) | {
        "format": "ba",
        "outtmpl": str(dest_dir / "audiosrc.%(ext)s"),
    }
    return _run(url, opts, dest_dir, "audiosrc")
