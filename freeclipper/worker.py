"""Background job queue. Worker threads pull queued jobs (and single-clip re-renders) from SQLite.

Pipeline per job (progress share in brackets):
  link:   metadata + length check -> platform captions or audio-only download [0-5%]
          -> transcription if needed [5-55%]   (the full video downloads in parallel)
  upload: audio extraction [0-5%] -> transcription [5-55%]
  then:   highlight finding [55-65%] -> wait for video if needed [65-70%] -> rendering [70-100%]

Reliability rules:
  * claiming is one atomic UPDATE, so no job is processed twice;
  * a job interrupted by a crash or out-of-memory kill is retried up to MAX_JOB_ATTEMPTS, then failed;
  * a graceful shutdown (redeploy) puts running jobs back in the queue without using up an attempt;
  * every job has a time limit (JOB_TIMEOUT_SECONDS) and every ffmpeg call its own timeout;
  * a janitor thread removes temporary files; rendered clips are never deleted by cleanup.
"""
from __future__ import annotations

import json
import logging
import re
import shutil
import threading
import time
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import config, db, monitor, storage
from .pipeline import captions, highlights, ingest, media, render, transcribe

log = logging.getLogger("freeclipper.worker")

_SOURCE_RE = re.compile(r"^source\.[A-Za-z0-9]+$")  # excludes yt-dlp partials like source.f137.mp4
# Intermediate files inside a project folder that are safe to delete once the job is not running.
_LEFTOVER_GLOBS = ("audio.wav", "audio.tmp.wav", "audiosrc.*", "captions.*.json3", "*.part", "*.ytdl",
                   "*.part-Frag*", "source.f*.*", "source.temp.*", "clips/*/clip.tmp.mp4")


def job_dir(job_id: str) -> Path:
    return config.JOBS_DIR / job_id


def find_source(job_id: str) -> Path | None:
    for p in sorted(job_dir(job_id).glob("source.*")):
        if _SOURCE_RE.match(p.name) and p.stat().st_size > 0:
            return p
    return None


def dir_size(path: Path) -> int:
    total = 0
    for f in path.rglob("*"):
        try:
            if f.is_file():
                total += f.stat().st_size
        except OSError:
            pass
    return total


def remove_temp_files(clip_ids: list[str]) -> None:
    """Delete in-progress render files for these clips from the temp folder."""
    for cid in clip_ids:
        for f in config.TEMP_DIR.glob(f"render-{cid}-*"):
            f.unlink(missing_ok=True)


def remove_clip_files(job_id: str, clip_id: str) -> int:
    """Permanently delete one clip's folder (video, thumbnail, captions, metadata). Returns bytes freed."""
    cdir = job_dir(job_id) / "clips" / clip_id
    freed = dir_size(cdir) if cdir.exists() else 0
    shutil.rmtree(cdir, ignore_errors=True)
    remove_temp_files([clip_id])
    return freed + storage.delete(job_id, f"clips/{clip_id}/")


def purge_job(job_id: str) -> int:
    """Permanently delete a project: its database rows (clips cascade) and every stored file.
    Returns the bytes freed. A worker still finishing this job removes anything it writes afterwards."""
    clip_ids = [c["id"] for c in db.query("SELECT id FROM clips WHERE job_id = ?", (job_id,))]
    db.execute("DELETE FROM jobs WHERE id = ?", (job_id,))
    jdir = job_dir(job_id)
    freed = dir_size(jdir) if jdir.exists() else 0
    shutil.rmtree(jdir, ignore_errors=True)
    remove_temp_files(clip_ids)
    return freed + storage.delete(job_id)


# Half-finished downloads and working files: useless after a restart, and the first thing to go when the
# volume is full (a full disk also stops the database from saving anything).
_PARTIAL_GLOBS = ("*.part", "*.part-Frag*", "*.ytdl", "source.f*.*", "source.temp.*", "*.temp.mp4",
                  "audiosrc.*", "audio.tmp.wav")


def emergency_free_space() -> int:
    """If the storage is (nearly) full, delete partial downloads and temp files. Touches only files, never
    the database, so it works even when SQLite can no longer write. Returns bytes freed."""
    if disk_free_mb() >= min_free_mb():
        return 0
    freed = 0
    targets = [p for d in config.JOBS_DIR.iterdir() if d.is_dir() for g in _PARTIAL_GLOBS for p in d.glob(g)]
    targets += [p for p in config.TEMP_DIR.rglob("*") if p.is_file()]
    for p in targets:
        try:
            if p.is_file():
                size = p.stat().st_size
                p.unlink()
                freed += size
        except OSError:
            pass
    log.warning("storage was full: removed %d partial/temporary file(s), %.0f MB freed, %d MB free now",
                len(targets), freed / 1048576, disk_free_mb())
    return freed


def source_available(job_id: str) -> bool:
    """The project's source video is kept, on the volume or in R2."""
    return find_source(job_id) is not None or storage.remote_source(job_id) is not None


def ensure_local_source(job_id: str) -> Path | None:
    """The source video on the volume, downloading it back from R2 if that's where it is."""
    src = find_source(job_id)
    if src:
        return src
    name = storage.remote_source(job_id)
    if name and storage.fetch(job_id, name, job_dir(job_id) / name):
        return job_dir(job_id) / name
    return None


def offload_clip(job_id: str, clip_id: str) -> None:
    """Move a finished clip's video and thumbnail to R2 (no-op when R2 is off or full)."""
    cdir = job_dir(job_id) / "clips" / clip_id
    for name in ("clip.mp4", "thumb.jpg"):
        storage.offload(job_id, cdir / name, f"clips/{clip_id}/{name}")


def offload_source(job_id: str) -> None:
    """Keep the source video in R2 rather than on the volume once a job no longer needs it locally."""
    src = find_source(job_id)
    if not src:
        return
    if storage.is_remote(job_id, src.name):
        src.unlink(missing_ok=True)  # R2 already has it (fetched back for a re-render)
    else:
        storage.offload(job_id, src, src.name)


def template_style(template_id: int | None) -> dict:
    row = None
    if template_id:
        row = db.one("SELECT style FROM templates WHERE id = ?", (template_id,))
    if not row:
        row = db.one("SELECT style FROM templates WHERE user_id IS NULL ORDER BY id LIMIT 1")
    return json.loads(row["style"]) if row else dict(db.DEFAULT_TEMPLATES[0][1])


def brand() -> dict:
    return db.get_setting("brand", {}) or {}


def fmt_duration(seconds: float) -> str:
    seconds = int(seconds)
    return f"{seconds // 3600}h {seconds % 3600 // 60}m" if seconds >= 3600 else f"{seconds // 60}m {seconds % 60}s"


def disk_free_mb() -> int:
    return int(shutil.disk_usage(config.JOBS_DIR).free / 1024 / 1024)


def disk_total_mb() -> int:
    return int(shutil.disk_usage(config.JOBS_DIR).total / 1024 / 1024)


def min_free_mb() -> int:
    """Free space to keep in reserve before refusing new videos: MIN_FREE_DISK_MB, but never more than
    10% of the volume (at least 100 MB), so a small volume isn't permanently locked."""
    return min(config.MIN_FREE_DISK_MB, max(100, disk_total_mb() // 10))


def friendly_error(exc: BaseException) -> str:
    text = str(exc) or type(exc).__name__
    low = text.lower()
    if "sign in to confirm" in low or "not a bot" in low:
        return ("YouTube asked this server for a sign-in check. Upload the video file instead, or try again "
                "later. (Admins: add YouTube cookies in Settings, or set YTDLP_PROXY, to fix this.)")
    if any(k in low for k in ("private video", "is unavailable", "video unavailable", "members-only",
                              "has been removed", "age-restricted", "not available in your country")):
        return "This video is private, removed, region-locked or otherwise restricted, so it can't be downloaded."
    if "unsupported url" in low:
        return "This link isn't a supported video page."
    if "http error 403" in low or "http error 429" in low:
        return f"The site refused the download ({text[:160]}). Try again in a few minutes."
    if isinstance(exc, MemoryError) or "unable to allocate" in low:
        return "Ran out of memory. Try a smaller transcription model or a shorter video."
    if isinstance(exc, OSError) and getattr(exc, "errno", None) == 28:
        return "The server ran out of disk space while processing this video."
    return text[-3000:]


class Cancelled(Exception):
    pass


class JobTimeout(Exception):
    pass


class Progress:
    """Throttled progress writer so fast callbacks don't hammer SQLite."""

    def __init__(self, runner: "Runner", job_id: str):
        self.runner, self.job_id = runner, job_id
        self._last = 0.0
        self._lock = threading.Lock()

    def set(self, value: float, stage: str | None = None, force: bool = False) -> None:
        now = time.monotonic()
        with self._lock:
            if not force and stage is None and now - self._last < 0.7:
                return
            self._last = now
        fields = {"progress": round(max(0.0, min(1.0, value)), 4)}
        if stage is not None:
            fields["stage"] = stage
        self.runner._update(self.job_id, **fields)


class Runner:
    def __init__(self, workers: int):
        self.workers = workers
        self._wake = threading.Event()
        self._cancel: set[str] = set()
        self._deadline: dict[str, float] = {}
        self._active: set[str] = set()
        self._started: dict[str, float] = {}
        self._rerendering: set[str] = set()
        self._active_lock = threading.Lock()
        self._stopping = False
        self._threads: list[threading.Thread] = []

    # ---- lifecycle ----
    def start(self) -> None:
        emergency_free_space()  # before anything writes to the database
        self._recover()
        for i in range(self.workers):
            self._spawn(self._loop, f"clip-worker-{i}")
        self._spawn(self._janitor, "janitor")
        if config.PRELOAD_WHISPER:
            self._spawn(self._preload, "model-preload")

    def _spawn(self, target, name: str) -> None:
        t = threading.Thread(target=target, name=name, daemon=True)
        t.start()
        self._threads.append(t)

    def _recover(self) -> None:
        """Jobs left 'running' by a crash: retry them, unless they already crashed too often."""
        for j in db.query("SELECT id, attempts FROM jobs WHERE status = 'running'"):
            if j["attempts"] >= config.MAX_JOB_ATTEMPTS:
                db.execute(
                    "UPDATE jobs SET status = 'failed', stage = 'Failed', error = ?, updated_at = ? WHERE id = ?",
                    (f"Processing stopped unexpectedly {j['attempts']} times (often the server running out of "
                     "memory). Try a smaller transcription model, fewer clips or a shorter video.",
                     db.now(), j["id"]))
                log.warning("job %s failed after %d interrupted attempts", j["id"], j["attempts"])
            else:
                db.execute("UPDATE jobs SET status = 'queued', stage = 'Waiting (server restarted)', "
                           "updated_at = ? WHERE id = ?", (db.now(), j["id"]))
        db.execute("UPDATE clips SET status = 'queued' WHERE status = 'rendering'")

    def shutdown(self, grace: float) -> None:
        """Graceful stop: running jobs return to the queue and resume after the restart."""
        self._stopping = True
        with self._active_lock:
            self._cancel.update(self._active)
        self._wake.set()
        end = time.monotonic() + grace
        while time.monotonic() < end:
            with self._active_lock:
                if not self._active:
                    break
            time.sleep(0.25)
        with self._active_lock:
            remaining = list(self._active)
        for job_id in remaining:  # still busy (e.g. a long ffmpeg call): requeue it ourselves
            self._requeue(job_id)

    def notify(self) -> None:
        self._wake.set()

    def cancel(self, job_id: str) -> None:
        self._cancel.add(job_id)
        media.kill_job(job_id)  # stop a long ffmpeg call now rather than when it finishes

    def is_active(self, job_id: str) -> bool:
        with self._active_lock:
            return job_id in self._active

    def stop_and_wait(self, job_id: str, timeout: float = 20.0) -> bool:
        """Cancel a running job and wait for its worker to let go of its files."""
        self.cancel(job_id)
        self._wake.set()
        end = time.monotonic() + timeout
        while self.is_active(job_id) and time.monotonic() < end:
            time.sleep(0.2)
        return not self.is_active(job_id)

    def _loop(self) -> None:
        while not self._stopping:
            try:
                job_id = db.claim_job()
                if job_id:
                    self._run_job(job_id)
                    continue
                clip_id = db.claim_clip()
                if clip_id:
                    self._rerender(clip_id)
                    continue
            except Exception:
                log.exception("worker loop error")
                time.sleep(2)
            self._wake.wait(timeout=3)
            self._wake.clear()

    def _preload(self) -> None:
        try:
            if not transcribe.model_cached(config.WHISPER_MODEL):
                log.info("downloading the %s transcription model in the background", config.WHISPER_MODEL)
                transcribe.preload(config.WHISPER_MODEL)
        except Exception as exc:
            log.warning("model preload failed (it will be retried on first use): %s", exc)

    # ---- helpers ----
    def _update(self, job_id: str, **fields) -> None:
        fields["updated_at"] = db.now()
        cols = ", ".join(f"{k} = ?" for k in fields)
        db.execute(f"UPDATE jobs SET {cols} WHERE id = ?", (*fields.values(), job_id))

    def _stopped(self, job_id: str) -> bool:
        return job_id in self._cancel or time.monotonic() > self._deadline.get(job_id, float("inf"))

    def _check(self, job_id: str) -> None:
        if job_id in self._cancel:
            raise Cancelled()
        if time.monotonic() > self._deadline.get(job_id, float("inf")):
            raise JobTimeout()

    def _requeue(self, job_id: str) -> None:
        db.execute("UPDATE jobs SET status = 'queued', stage = 'Waiting (server restarting)', "
                   "attempts = MAX(attempts - 1, 0), updated_at = ? WHERE id = ? AND status = 'running'",
                   (db.now(), job_id))

    def _interrupted(self, job_id: str) -> None:
        """Decide what an interruption means: redeploy, time limit, or the user cancelling."""
        if self._stopping:
            self._requeue(job_id)
        elif time.monotonic() > self._deadline.get(job_id, float("inf")):
            limit = self._deadline[job_id] - self._started.get(job_id, self._deadline[job_id])
            self._update(job_id, status="failed", stage="Failed",
                         error=f"Processing took longer than the {fmt_duration(limit)} "
                               "limit and was stopped. Try a shorter video or fewer clips.")
        else:
            self._update(job_id, status="cancelled", stage="Cancelled")

    @staticmethod
    def _clean_leftovers(jdir: Path) -> None:
        for pattern in _LEFTOVER_GLOBS:
            for p in jdir.glob(pattern):
                if p.is_file() and not _SOURCE_RE.match(p.name):
                    p.unlink(missing_ok=True)

    # ---- the pipeline ----
    def _run_job(self, job_id: str) -> None:
        job = db.one("SELECT * FROM jobs WHERE id = ?", (job_id,))
        if not job:
            return
        with self._active_lock:
            self._active.add(job_id)
        self._started[job_id] = time.monotonic()
        self._deadline[job_id] = self._started[job_id] + config.job_time_limit(job["duration"])
        media.set_job(job_id)
        opts = json.loads(job["options"] or "{}")
        jdir = job_dir(job_id)
        jdir.mkdir(parents=True, exist_ok=True)
        notes: list[str] = []
        stop = lambda: self._stopped(job_id)  # noqa: E731
        prog = Progress(self, job_id)
        started = time.monotonic()
        timings: dict[str, float] = {}
        requeued = False

        # Shared state for the background video download.
        video: dict = {"path": None, "title": None, "error": None, "pct": 0.0}
        video_thread: threading.Thread | None = None

        def fetch_video() -> None:
            try:
                # Leave room for the rendered clips (~15 MB each, with margin) so rendering can't run out of space.
                clip_space = 40 + 15 * max(1, min(config.MAX_CLIPS_PER_JOB, int(opts.get("max_clips", 6))))
                video["path"], video["title"] = ingest.download(
                    job["source"], jdir, lambda p: video.__setitem__("pct", p), stop, reserve_mb=clip_space)
            except BaseException as exc:  # reported when the video is needed
                video["error"] = exc

        try:
            audio = jdir / "audio.wav"
            src = ensure_local_source(job_id)
            title = None
            tpath = jdir / "transcript.json"
            want_model = opts.get("whisper_model") or config.WHISPER_MODEL
            if want_model not in config.WHISPER_MODELS:
                want_model = config.WHISPER_MODEL
            want_lang = opts.get("language") or None
            want_task = "translate" if opts.get("translate") else "transcribe"
            english_only = config.ENGLISH_ONLY
            use_captions = opts.get("transcript_source", "auto") == "auto" and job["source_type"] == "url"
            transcript = json.loads(tpath.read_text(encoding="utf-8")) if tpath.exists() else None
            if transcript:
                from_captions = transcript.get("model") == transcribe.CAPTIONS_MODEL
                if english_only:  # a saved transcript is reused only if it's English
                    stale = transcript.get("language") != "en" or not transcribe.is_english(
                        transcript.get("segments") or []) or (
                        not from_captions and transcript.get("model") != want_model) or (from_captions and not use_captions)
                elif (from_captions and not use_captions) or (not from_captions and (
                        transcript.get("model") != want_model or transcript.get("task") != want_task
                        or (want_lang and transcript.get("language") != want_lang))):
                    stale = True
                else:
                    stale = False
                if stale:
                    transcript = None
            duration = float(transcript["duration"]) if transcript and transcript.get("duration") else None

            # 1. Media in
            t0 = time.monotonic()
            if not src and job["source_type"] != "url":
                raise RuntimeError("The uploaded file is missing.")
            if not src:
                if disk_free_mb() < min_free_mb():
                    raise RuntimeError("The server's storage is nearly full, so new videos can't be downloaded "
                                       "right now. Delete old projects or try again later.")
                prog.set(0.01, "Checking the video", force=True)
                meta = ingest.video_info(job["source"])
                title = meta.get("title")
                if meta.get("duration") and meta["duration"] > config.MAX_VIDEO_SECONDS + 5:
                    raise RuntimeError(f"This video is {fmt_duration(meta['duration'])} long; the limit is "
                                       f"{fmt_duration(config.MAX_VIDEO_SECONDS)}.")
                video_thread = threading.Thread(target=fetch_video, name=f"dl-{job_id}", daemon=True)
                video_thread.start()
            if transcript is None and use_captions:
                prog.set(0.01, "Fetching captions", force=True)
                try:
                    cpath, meta = ingest.download_captions(job["source"], jdir, stop)
                    title = title or meta.get("title")
                    lang = meta.get("caption_lang") or meta.get("language")
                    if cpath and english_only and lang != "en":
                        cpath = None  # captions in another language: transcribe and translate locally
                        notes.append("This video isn't in English; its speech was translated to English.")
                    if cpath and not (want_task == "translate" and lang != "en"):
                        transcript = transcribe.from_json3(cpath, lang, meta.get("duration"))
                        duration = transcript["duration"]
                        self._save_transcript(jdir, transcript)
                    elif not cpath:
                        notes.append("No platform captions for this video; transcribed it locally instead.")
                except Exception as exc:
                    if stop():
                        raise Cancelled() from exc
                    # If the video itself can't be reached, the download will fail too: stop now.
                    if friendly_error(exc) != (str(exc) or type(exc).__name__)[-3000:]:
                        raise
                    log.warning("caption fetch failed for %s: %s", job_id, exc)
                    notes.append("Couldn't fetch platform captions; transcribing locally instead.")
            if transcript is None:
                if src:
                    if not audio.exists():
                        prog.set(0.01, "Extracting audio", force=True)
                        media.extract_audio(src, audio)
                elif not audio.exists():
                    prog.set(0.01, "Downloading audio", force=True)
                    try:
                        audio_src, dl_title = ingest.download_audio(
                            job["source"], jdir, lambda p: prog.set(0.01 + 0.04 * p), stop)
                        title = title or dl_title
                        media.extract_audio(audio_src, audio)
                        audio_src.unlink(missing_ok=True)
                    except Exception as exc:
                        if stop():
                            raise Cancelled() from exc
                        # No separate audio stream on this site: wait for the full video instead.
                        log.info("audio-only download unavailable (%s); waiting for full video", exc)
                        self._wait_video(job_id, video_thread, video, prog, 0.01, 0.05, "Downloading video")
                        src = video["path"]
                        media.extract_audio(src, audio)
                duration = media.probe(audio)["duration"]
            if not duration and src:
                duration = media.probe(src)["duration"]
            if duration and duration > config.MAX_VIDEO_SECONDS + 5:
                raise RuntimeError(f"This video is {fmt_duration(duration)} long; the limit is "
                                   f"{fmt_duration(config.MAX_VIDEO_SECONDS)}.")
            if title and job["title"] == job["source"]:
                self._update(job_id, title=title[:200])
            self._check(job_id)
            self._update(job_id, duration=duration)
            # Long videos get a longer time limit once their length is known.
            self._deadline[job_id] = self._started[job_id] + config.job_time_limit(duration)
            timings["media"] = time.monotonic() - t0

            # 2. Transcript (local transcription only when no usable one exists yet)
            t0 = time.monotonic()
            if transcript is None:
                label = f"Transcribing ({want_model} model)"

                def on_transcribe(p: float) -> None:
                    extra = ""
                    if video_thread and video_thread.is_alive():
                        extra = f", video downloading {video['pct'] * 100:.0f}%"
                    prog.set(0.05 + 0.5 * p, f"{label} {p * 100:.0f}%{extra}")

                if not transcribe.model_cached(want_model):
                    prog.set(0.05, f"Downloading the {want_model} transcription model (first use only)", force=True)
                    transcribe.get_model(want_model)
                prog.set(0.05, label, force=True)
                try:
                    transcript = transcribe.transcribe(audio, want_model, want_lang, want_task, on_transcribe, stop,
                                                       english_only=english_only)
                    if english_only and transcript.get("spoken_language", "en") != "en":
                        note = "This video isn't in English; its speech was translated to English."
                        if note not in notes:
                            notes.append(note)
                finally:
                    transcribe.release()  # give the memory back before rendering
                self._save_transcript(jdir, transcript)
            self._update(job_id, language=transcript.get("language"))
            prog.set(0.55, force=True)
            timings["transcribe"] = time.monotonic() - t0
            self._check(job_id)

            # 3. Highlights
            t0 = time.monotonic()
            requested = (opts.get("provider") or "auto").lower()
            if requested == "auto":
                requested = config.AI_PROVIDER
            provider = config.resolve_provider(opts.get("provider"))
            names = {"claude": "Claude", "ollama": "local AI", "heuristic": "built-in finder"}
            if requested in ("claude", "ollama") and provider != requested:
                notes.append(f"{names[requested]} isn't available on this server; used {names[provider]} instead.")
            hl_label = ("Finding highlights (local AI is reviewing the strongest moments)" if provider == "ollama"
                        else f"Finding highlights ({names.get(provider, provider)})")
            prog.set(0.55, hl_label, force=True)
            max_clips = max(1, min(config.MAX_CLIPS_PER_JOB, int(opts.get("max_clips", 6))))
            found, used, note = highlights.find_highlights(
                transcript, duration, max_clips,
                int(opts.get("min_len", 20)), int(opts.get("max_len", 60)), provider,
                audio if audio.exists() else None, opts.get("prompt", ""),
                progress=lambda p: prog.set(0.55 + 0.1 * p, f"{hl_label} {p * 100:.0f}%"))
            if note:
                notes.append(note)
            if not found:
                raise RuntimeError("No usable clips were found in this video.")
            if len(found) < max_clips:
                notes.append(
                    f"You asked for {max_clips} clips; {len(found)} distinct, non-overlapping moments of at least "
                    f"{int(opts.get('min_len', 20))} s fit in this {fmt_duration(duration)} video. Lower the minimum "
                    "length or use a longer video to get more.")
            timings["highlights"] = time.monotonic() - t0
            self._check(job_id)

            # 4. Make sure the video is here
            if not src:
                src = ensure_local_source(job_id)
            if not src:
                self._wait_video(job_id, video_thread, video, prog, 0.65, 0.70, "Finishing video download")
                src = video["path"]
            info = media.probe(src)
            (jdir / "probe.json").write_text(json.dumps(info), encoding="utf-8")

            # Replace previous clips (re-analysis)
            for old in db.query("SELECT id FROM clips WHERE job_id = ?", (job_id,)):
                shutil.rmtree(jdir / "clips" / old["id"], ignore_errors=True)
            db.execute("DELETE FROM clips WHERE job_id = ?", (job_id,))
            clip_rows = []
            for i, c in enumerate(found):
                cid = uuid.uuid4().hex[:12]
                db.execute(
                    "INSERT INTO clips(id, job_id, idx, start, \"end\", title, hook, reason, score, hashtags, "
                    "template_id, status, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (cid, job_id, i, c["start"], c["end"], c["title"], c["hook"], c["reason"], c["score"],
                     json.dumps(c["hashtags"], ensure_ascii=False), opts.get("template_id"), "pending",
                     db.now(), db.now()))
                clip_rows.append({**c, "id": cid})

            # 5. Render clips (MAX_WORKERS at a time)
            t0 = time.monotonic()
            failures = self._render_all(job_id, jdir, src, info, clip_rows, transcript, opts, prog)
            timings["render"] = time.monotonic() - t0
            if failures == len(clip_rows):
                first = db.one("SELECT error FROM clips WHERE job_id = ? AND error IS NOT NULL LIMIT 1", (job_id,))
                reason = (first or {}).get("error") or ""
                raise RuntimeError("Every clip failed to render. " + (reason.splitlines()[0][:300] if reason else
                                   "See the clip errors for details."))
            if failures:
                notes.append(f"{failures} of {len(clip_rows)} clips failed to render; see their errors below.")
            total = time.monotonic() - started
            log.info("job %s done in %.0fs %s", job_id, total, {k: round(v) for k, v in timings.items()})
            offload_source(job_id)  # the volume keeps only small files when R2 is on
            monitor.record("info", "project finished",
                           f"{job['title'][:80]}: {len(clip_rows) - failures} clip(s) in {total / 60:.1f} min",
                           job_id=job_id, user_id=job["user_id"])
            self._update(job_id, status="done", progress=1.0, error=None, notes="\n".join(notes) or None,
                         stage=f"Done in {total / 60:.1f} min via {names.get(used, used)} "
                               f"({len(clip_rows) - failures} clips)")
        except (Cancelled, JobTimeout, InterruptedError):
            requeued = self._stopping
            self._interrupted(job_id)
        except Exception as exc:
            if type(exc).__name__ == "DownloadCancelled" or stop():
                requeued = self._stopping
                self._interrupted(job_id)
            else:
                tb = traceback.format_exc()
                log.error("job %s failed: %s", job_id, tb, extra={"monitored": True})
                self._update(job_id, status="failed", stage="Failed", error=friendly_error(exc),
                             notes="\n".join(notes) or None)
                monitor.record("error", "project failed", f"{job['title'][:80]}: {friendly_error(exc)}"[:900],
                               tb, job_id=job_id, user_id=job["user_id"])
        finally:
            if video_thread and video_thread.is_alive():
                self._cancel.add(job_id)  # stop the download if the job ended early
                video_thread.join(timeout=30)
            if storage.remote_source(job_id) and find_source(job_id):
                find_source(job_id).unlink(missing_ok=True)  # R2 has it; don't keep a second copy here
            if not db.one("SELECT id FROM jobs WHERE id = ?", (job_id,)):
                shutil.rmtree(jdir, ignore_errors=True)  # deleted while running: leave nothing behind
            elif not requeued:  # keep partial downloads when the job will resume after a restart
                self._clean_leftovers(jdir)
            self._cancel.discard(job_id)
            self._deadline.pop(job_id, None)
            self._started.pop(job_id, None)
            media.forget_job(job_id)
            media.set_job(None)
            with self._active_lock:
                self._active.discard(job_id)
            transcribe.trim_memory()  # hand the job's memory back to the OS
            self.notify()

    @staticmethod
    def _save_transcript(jdir: Path, transcript: dict) -> None:
        tmp = jdir / "transcript.json.tmp"
        tmp.write_text(json.dumps(transcript, ensure_ascii=False), encoding="utf-8")
        tmp.replace(jdir / "transcript.json")  # atomic: a crash never leaves a half-written transcript
        cues = captions.segment_cues(transcript)
        (jdir / "transcript.srt").write_text(captions.to_srt(cues), encoding="utf-8")
        (jdir / "transcript.vtt").write_text(captions.to_vtt(cues), encoding="utf-8")

    def _wait_video(self, job_id: str, thread: threading.Thread | None, video: dict, prog: Progress,
                    lo: float, hi: float, label: str) -> None:
        while thread and thread.is_alive():
            self._check(job_id)
            prog.set(lo + (hi - lo) * video["pct"], f"{label} {video['pct'] * 100:.0f}%")
            thread.join(timeout=1.0)
        if video["error"] is not None:
            raise video["error"]
        if not video["path"]:
            raise RuntimeError("The video download did not produce a file.")

    def _render_all(self, job_id: str, jdir: Path, src: Path, info: dict, clips: list[dict],
                    transcript: dict, opts: dict, prog: Progress) -> int:
        style = template_style(opts.get("template_id"))
        b = brand()
        total = len(clips)
        done = failures = 0
        lock = threading.Lock()
        prog.set(0.70, f"Rendering clips (0 of {total} done)", force=True)
        transcribe.trim_memory()

        def work(c: dict) -> None:
            media.set_job(job_id)  # pool threads: tag their ffmpeg processes with this job
            if self._stopped(job_id):
                raise Cancelled()
            db.execute("UPDATE clips SET status = 'rendering', updated_at = ? WHERE id = ?", (db.now(), c["id"]))
            try:
                render.render_clip(jdir, src, info, c, transcript, opts, style, b)
                offload_clip(job_id, c["id"])
            finally:
                transcribe.trim_memory()  # face tracking frees frames; return them to the OS
                if not db.one("SELECT id FROM clips WHERE id = ?", (c["id"],)):
                    remove_clip_files(job_id, c["id"])  # deleted while rendering

        with ThreadPoolExecutor(max_workers=min(config.RENDER_WORKERS, total)) as pool:
            futures = {pool.submit(work, c): c for c in clips}
            for fut in as_completed(futures):
                c = futures[fut]
                try:
                    fut.result()
                    db.execute("UPDATE clips SET status = 'done', error = NULL, updated_at = ? WHERE id = ?",
                               (db.now(), c["id"]))
                except Cancelled:
                    continue
                except Exception as exc:
                    log.exception("render failed for clip %s", c["id"], extra={"monitored": True})
                    monitor.record("error", "clip failed", f"Clip {c['id']} of {job_id}: {str(exc).splitlines()[0][:300]}",
                                   traceback.format_exc(), job_id=job_id, alert=False)
                    db.execute("UPDATE clips SET status = 'failed', error = ?, updated_at = ? WHERE id = ?",
                               (str(exc)[-2000:], db.now(), c["id"]))
                    with lock:
                        failures += 1
                with lock:
                    done += 1
                    prog.set(0.70 + 0.30 * done / total, f"Rendering clips ({done} of {total} done)", force=True)
        self._check(job_id)
        return failures

    def _rerender(self, clip_id: str) -> None:
        clip = db.one("SELECT * FROM clips WHERE id = ?", (clip_id,))
        if not clip:
            return
        job = db.one("SELECT * FROM jobs WHERE id = ?", (clip["job_id"],))
        if not job:
            return
        self._rerendering.add(clip_id)
        media.set_job(job["id"])
        try:
            jdir = job_dir(job["id"])
            src = ensure_local_source(job["id"])
            if not src:
                raise RuntimeError("The source video is no longer stored, so this clip can't be re-rendered.")
            probe_path = jdir / "probe.json"
            info = json.loads(probe_path.read_text(encoding="utf-8")) if probe_path.exists() else media.probe(src)
            transcript = json.loads((jdir / "transcript.json").read_text(encoding="utf-8"))
            opts = json.loads(job["options"] or "{}")
            clip["hashtags"] = json.loads(clip["hashtags"] or "[]")
            try:
                render.render_clip(jdir, src, info, clip, transcript, opts,
                                   template_style(clip["template_id"] or opts.get("template_id")), brand())
            finally:
                if not db.one("SELECT id FROM clips WHERE id = ?", (clip_id,)):
                    remove_clip_files(job["id"], clip_id)  # deleted while re-rendering
                    return
            offload_clip(job["id"], clip_id)
            db.execute("UPDATE clips SET status = 'done', error = NULL, updated_at = ? WHERE id = ?",
                       (db.now(), clip_id))
        except Exception as exc:
            log.exception("re-render failed for %s", clip_id)
            db.execute("UPDATE clips SET status = 'failed', error = ?, updated_at = ? WHERE id = ?",
                       (str(exc)[-2000:], db.now(), clip_id))
        finally:
            self._rerendering.discard(clip_id)
            media.set_job(None)
            if not self.is_active(clip["job_id"]):
                offload_source(clip["job_id"])  # it was only needed for this render
            transcribe.trim_memory()

    # ---- housekeeping ----
    def _janitor(self) -> None:
        time.sleep(30)  # let startup finish first
        while not self._stopping:
            try:
                self.cleanup()
            except Exception:
                log.exception("cleanup failed")
            for _ in range(config.CLEANUP_INTERVAL_MINUTES * 60):
                if self._stopping:
                    return
                self._watchdog()
                time.sleep(1)

    def purge_all(self) -> dict:
        """Admin "delete everything": stop all processing, then permanently delete every project, clip,
        upload and temporary file for all users. Accounts, styles, branding, cookies and the speech
        models are kept."""
        before = disk_free_mb()
        # Nothing new may start: queued projects and queued re-renders are cancelled first.
        db.execute("UPDATE jobs SET status = 'cancelled', stage = 'Cancelled' WHERE status = 'queued'")
        db.execute("UPDATE clips SET status = 'failed' WHERE status = 'queued'")
        with self._active_lock:
            active = set(self._active)
        active.update(j["id"] for j in db.query("SELECT id FROM jobs WHERE status = 'running'"))
        for job_id in active:
            self.stop_and_wait(job_id, timeout=30)
        projects = freed = 0
        for j in db.query("SELECT id FROM jobs"):
            media.kill_job(j["id"])  # a re-render in progress
            freed += purge_job(j["id"])
            projects += 1
        # R2 files whose project is gone, unfinished uploads, anything left on disk, and all temp files.
        for r in db.query("SELECT DISTINCT job_id FROM remote_files"):
            freed += storage.delete(r["job_id"])
        db.execute("DELETE FROM uploads")
        for d in list(config.UPLOADS_DIR.iterdir()) + list(config.JOBS_DIR.iterdir()):
            freed += dir_size(d) if d.is_dir() else 0
            shutil.rmtree(d, ignore_errors=True) if d.is_dir() else d.unlink(missing_ok=True)
        for f in config.TEMP_DIR.rglob("*"):
            try:
                if f.is_file():
                    freed += f.stat().st_size
                    f.unlink()
            except OSError:
                pass
        transcribe.trim_memory()
        after = disk_free_mb()
        log.warning("admin deleted all projects: %d project(s), %.0f MB freed", projects, freed / 1048576)
        return {"projects_deleted": projects, "freed_mb": round(freed / 1048576, 1),
                "disk_free_before_mb": before, "disk_free_mb": after}

    def _watchdog(self) -> None:
        """Stop the processes of a job that ran past its time limit, so a hung ffmpeg call can't keep a
        worker busy forever; the job then fails with a clear message."""
        now = time.monotonic()
        for job_id, deadline in list(self._deadline.items()):
            if now > deadline and job_id not in self._cancel:
                log.warning("job %s passed its time limit; stopping it", job_id)
                media.kill_job(job_id)

    def _expire_projects(self, stats: dict) -> None:
        """Permanently delete finished projects older than PROJECT_RETENTION_HOURS, for every user.
        Never touches queued or processing projects, or ones with a clip being re-rendered."""
        if not config.PROJECT_RETENTION_HOURS:
            return
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=config.PROJECT_RETENTION_HOURS)).isoformat(
            timespec="seconds")
        rows = db.query(
            "SELECT id FROM jobs WHERE status IN ('done', 'failed', 'cancelled') AND updated_at < ? "
            "AND NOT EXISTS (SELECT 1 FROM clips WHERE clips.job_id = jobs.id "
            "AND (clips.status IN ('queued', 'rendering') OR clips.updated_at >= ?))", (cutoff, cutoff))
        for row in rows:
            if self.is_active(row["id"]):
                continue
            stats["expired_bytes"] += purge_job(row["id"])
            stats["expired_projects"] += 1
        if stats["expired_projects"]:
            log.info("expired %d project(s), freed %.0f MB", stats["expired_projects"],
                     stats["expired_bytes"] / 1048576)

    def _expire_uploads(self, stats: dict) -> None:
        """Delete unfinished uploads with no new piece for UPLOAD_EXPIRY_HOURS, and upload folders that
        have no database row (e.g. their user was deleted)."""
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=config.UPLOAD_EXPIRY_HOURS)).isoformat(
            timespec="seconds")
        for row in db.query("SELECT id FROM uploads WHERE updated_at < ?", (cutoff,)):
            db.execute("DELETE FROM uploads WHERE id = ?", (row["id"],))
            shutil.rmtree(config.UPLOADS_DIR / row["id"], ignore_errors=True)
            stats["expired_uploads"] += 1
        known = {r["id"] for r in db.query("SELECT id FROM uploads")}
        for d in config.UPLOADS_DIR.iterdir():
            try:
                if d.name not in known and d.stat().st_mtime < time.time() - 3600:
                    shutil.rmtree(d, ignore_errors=True) if d.is_dir() else d.unlink(missing_ok=True)
                    stats["expired_uploads"] += 1
            except OSError:
                pass

    def _recover_abandoned(self, stats: dict) -> None:
        """A job marked 'running' that no worker is processing (a crashed thread) goes back to the queue,
        or fails after MAX_JOB_ATTEMPTS; a clip stuck in 'rendering' is queued again."""
        if self._stopping:
            return
        stale = (datetime.now(timezone.utc) - timedelta(minutes=15)).isoformat(timespec="seconds")
        for j in db.query("SELECT id, attempts FROM jobs WHERE status = 'running' AND updated_at < ?", (stale,)):
            if self.is_active(j["id"]):
                continue
            if j["attempts"] >= config.MAX_JOB_ATTEMPTS:
                db.execute("UPDATE jobs SET status = 'failed', stage = 'Failed', error = ?, updated_at = ? "
                           "WHERE id = ? AND status = 'running'",
                           ("Processing stopped unexpectedly. Retry the project, or try fewer clips.",
                            db.now(), j["id"]))
            else:
                db.execute("UPDATE jobs SET status = 'queued', stage = 'Waiting (worker restarted)', "
                           "updated_at = ? WHERE id = ? AND status = 'running'", (db.now(), j["id"]))
            stats["abandoned_jobs"] += 1
        for c in db.query("SELECT id, job_id FROM clips WHERE status = 'rendering' AND updated_at < ?", (stale,)):
            if c["id"] not in self._rerendering and not self.is_active(c["job_id"]):
                db.execute("UPDATE clips SET status = 'queued' WHERE id = ? AND status = 'rendering'", (c["id"],))
                stats["abandoned_jobs"] += 1
        if stats["abandoned_jobs"]:
            self.notify()

    def cleanup(self) -> dict:
        """Remove expired temporary files, leftovers and orphaned folders. Never touches rendered clips,
        captions or transcripts. Old source videos are removed only if SOURCE_RETENTION_DAYS is set."""
        stats = {"expired_projects": 0, "expired_bytes": 0, "expired_uploads": 0, "abandoned_jobs": 0, "temp_files": 0, "orphans": 0,
                 "orphan_clips": 0, "leftover_dirs": 0, "sources": 0}
        self._expire_projects(stats)
        self._expire_uploads(stats)
        self._recover_abandoned(stats)
        if storage.enabled():
            try:  # third free-tier guard: count what the bucket really holds
                r2 = storage.verify()
                stats["r2_mb"] = round(r2["bytes"] / 1048576, 1)
                stats["r2_strays_removed"] = r2["strays_removed"]
            except Exception as exc:
                log.error("R2 usage check failed: %s", exc)
        cutoff = time.time() - config.TEMP_FILE_MAX_AGE_HOURS * 3600
        for p in sorted(config.TEMP_DIR.rglob("*"), key=lambda x: len(x.parts), reverse=True):
            try:
                if p.is_file() and p.stat().st_mtime < cutoff:
                    p.unlink()
                    stats["temp_files"] += 1
                elif p.is_dir() and not any(p.iterdir()):
                    p.rmdir()
            except OSError:
                pass
        rows = {j["id"]: j for j in db.query("SELECT id, status, updated_at FROM jobs")}
        for d in config.JOBS_DIR.iterdir():
            if not d.is_dir():
                continue
            job = rows.get(d.name)
            if job is None:
                # Not a project being created right now (a long upload keeps writing its source file).
                newest = max([d.stat().st_mtime] + [f.stat().st_mtime for f in d.rglob("*") if f.exists()])
                if newest < time.time() - 3600:
                    shutil.rmtree(d, ignore_errors=True)
                    stats["orphans"] += 1
                continue
            if job["status"] in ("queued", "running"):
                continue
            known = {c["id"] for c in db.query("SELECT id FROM clips WHERE job_id = ?", (d.name,))}
            clips_dir = d / "clips"
            if clips_dir.is_dir():
                for cd in clips_dir.iterdir():
                    if cd.is_dir() and cd.name not in known and cd.stat().st_mtime < time.time() - 600:
                        shutil.rmtree(cd, ignore_errors=True)
                        stats["orphan_clips"] += 1
            self._clean_leftovers(d)
            stats["leftover_dirs"] += 1
            if config.SOURCE_RETENTION_DAYS:
                updated = datetime.fromisoformat(job["updated_at"])
                if updated < datetime.now(timezone.utc) - timedelta(days=config.SOURCE_RETENTION_DAYS):
                    src = find_source(d.name)
                    if src:
                        src.unlink(missing_ok=True)
                        stats["sources"] += 1
                    if storage.delete(d.name, "source."):
                        stats["sources"] += 1
        stats["disk_free_mb"] = disk_free_mb()
        if stats["disk_free_mb"] < min_free_mb():
            log.warning("low disk space: %d MB free in %s", stats["disk_free_mb"], config.JOBS_DIR)
            monitor.record("error", "storage", f"Storage almost full: {stats['disk_free_mb']} MB free of "
                           f"{disk_total_mb()} MB. New uploads are being refused.")
        stats["events_pruned"] = monitor.prune()
        log.info("cleanup: %s", stats)
        return stats
