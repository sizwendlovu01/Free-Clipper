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

from . import config, db
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
        self._active_lock = threading.Lock()
        self._stopping = False
        self._threads: list[threading.Thread] = []

    # ---- lifecycle ----
    def start(self) -> None:
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
            self._update(job_id, status="failed", stage="Failed",
                         error=f"Processing took longer than the {fmt_duration(config.JOB_TIMEOUT_SECONDS)} "
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
        self._deadline[job_id] = time.monotonic() + config.JOB_TIMEOUT_SECONDS
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
                video["path"], video["title"] = ingest.download(
                    job["source"], jdir, lambda p: video.__setitem__("pct", p), stop)
            except BaseException as exc:  # reported when the video is needed
                video["error"] = exc

        try:
            audio = jdir / "audio.wav"
            src = find_source(job_id)
            title = None
            tpath = jdir / "transcript.json"
            want_model = opts.get("whisper_model") or config.WHISPER_MODEL
            if want_model not in config.WHISPER_MODELS:
                want_model = config.WHISPER_MODEL
            want_lang = opts.get("language") or None
            want_task = "translate" if opts.get("translate") else "transcribe"
            use_captions = opts.get("transcript_source", "auto") == "auto" and job["source_type"] == "url"
            transcript = json.loads(tpath.read_text(encoding="utf-8")) if tpath.exists() else None
            if transcript:
                from_captions = transcript.get("model") == transcribe.CAPTIONS_MODEL
                if (from_captions and not use_captions) or (not from_captions and (
                        transcript.get("model") != want_model or transcript.get("task") != want_task
                        or (want_lang and transcript.get("language") != want_lang))):
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
                transcript = transcribe.transcribe(audio, want_model, want_lang, want_task, on_transcribe, stop)
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
                src = find_source(job_id)
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
                raise RuntimeError("Every clip failed to render; see the clip errors for details.")
            if failures:
                notes.append(f"{failures} of {len(clip_rows)} clips failed to render; see their errors below.")
            total = time.monotonic() - started
            log.info("job %s done in %.0fs %s", job_id, total, {k: round(v) for k, v in timings.items()})
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
                log.error("job %s failed: %s", job_id, traceback.format_exc())
                self._update(job_id, status="failed", stage="Failed", error=friendly_error(exc),
                             notes="\n".join(notes) or None)
        finally:
            if video_thread and video_thread.is_alive():
                self._cancel.add(job_id)  # stop the download if the job ended early
                video_thread.join(timeout=30)
            if not requeued:  # keep partial downloads when the job will resume after a restart
                self._clean_leftovers(jdir)
            self._cancel.discard(job_id)
            self._deadline.pop(job_id, None)
            with self._active_lock:
                self._active.discard(job_id)
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

        def work(c: dict) -> None:
            if self._stopped(job_id):
                raise Cancelled()
            db.execute("UPDATE clips SET status = 'rendering', updated_at = ? WHERE id = ?", (db.now(), c["id"]))
            render.render_clip(jdir, src, info, c, transcript, opts, style, b)

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
                    log.exception("render failed for clip %s", c["id"])
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
        try:
            jdir = job_dir(job["id"])
            src = find_source(job["id"])
            if not src:
                raise RuntimeError("The source video is no longer stored, so this clip can't be re-rendered.")
            probe_path = jdir / "probe.json"
            info = json.loads(probe_path.read_text(encoding="utf-8")) if probe_path.exists() else media.probe(src)
            transcript = json.loads((jdir / "transcript.json").read_text(encoding="utf-8"))
            opts = json.loads(job["options"] or "{}")
            clip["hashtags"] = json.loads(clip["hashtags"] or "[]")
            render.render_clip(jdir, src, info, clip, transcript, opts,
                               template_style(clip["template_id"] or opts.get("template_id")), brand())
            db.execute("UPDATE clips SET status = 'done', error = NULL, updated_at = ? WHERE id = ?",
                       (db.now(), clip_id))
        except Exception as exc:
            log.exception("re-render failed for %s", clip_id)
            db.execute("UPDATE clips SET status = 'failed', error = ?, updated_at = ? WHERE id = ?",
                       (str(exc)[-2000:], db.now(), clip_id))

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
                time.sleep(1)

    def cleanup(self) -> dict:
        """Remove expired temporary files, leftovers and orphaned folders. Never touches rendered clips,
        captions or transcripts. Old source videos are removed only if SOURCE_RETENTION_DAYS is set."""
        stats = {"temp_files": 0, "orphans": 0, "leftover_dirs": 0, "sources": 0}
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
                if d.stat().st_mtime < time.time() - 3600:  # not a project being created right now
                    shutil.rmtree(d, ignore_errors=True)
                    stats["orphans"] += 1
                continue
            if job["status"] in ("queued", "running"):
                continue
            self._clean_leftovers(d)
            stats["leftover_dirs"] += 1
            if config.SOURCE_RETENTION_DAYS:
                updated = datetime.fromisoformat(job["updated_at"])
                if updated < datetime.now(timezone.utc) - timedelta(days=config.SOURCE_RETENTION_DAYS):
                    src = find_source(d.name)
                    if src:
                        src.unlink(missing_ok=True)
                        stats["sources"] += 1
        stats["disk_free_mb"] = disk_free_mb()
        if stats["disk_free_mb"] < min_free_mb():
            log.warning("low disk space: %d MB free in %s", stats["disk_free_mb"], config.JOBS_DIR)
        log.info("cleanup: %s", stats)
        return stats
