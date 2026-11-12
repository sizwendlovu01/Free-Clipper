"""Background job queue. N worker threads pull queued jobs (and single-clip re-renders) from SQLite.

Pipeline per job (progress share in brackets):
  link:   audio-only download [0-5%] -> transcription [5-55%]   (full video downloads in parallel)
  upload: audio extraction    [0-5%] -> transcription [5-55%]
  then:   highlight finding [55-65%] -> wait for video if needed [65-70%] -> parallel rendering [70-100%]
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
from pathlib import Path

from . import config, db
from .pipeline import captions, highlights, ingest, media, render, transcribe

log = logging.getLogger("freeclipper.worker")

_SOURCE_RE = re.compile(r"^source\.[A-Za-z0-9]+$")  # excludes yt-dlp partials like source.f137.mp4


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


def friendly_error(exc: BaseException) -> str:
    text = str(exc) or type(exc).__name__
    low = text.lower()
    if "sign in to confirm" in low or "not a bot" in low:
        return ("YouTube asked for a sign-in check for this video. Try again later, download the video "
                "yourself and upload the file, or try another link.")
    if any(k in low for k in ("private video", "is unavailable", "video unavailable", "members-only",
                              "has been removed", "age-restricted", "not available in your country")):
        return "This video is private, removed, region-locked or otherwise restricted, so it can't be downloaded."
    if "unsupported url" in low:
        return "This link isn't a supported video page."
    if "http error 403" in low or "http error 429" in low:
        return f"The site refused the download ({text[:160]}). Try again in a few minutes."
    if isinstance(exc, MemoryError) or "unable to allocate" in low:
        return "Ran out of memory. Close other apps or choose a smaller transcription model."
    return text[-3000:]


class Cancelled(Exception):
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
        self._claim_lock = threading.Lock()
        self._cancel: set[str] = set()
        self._threads: list[threading.Thread] = []

    def start(self) -> None:
        db.execute("UPDATE jobs SET status = 'queued', stage = 'Waiting (restarted)' WHERE status = 'running'")
        db.execute("UPDATE clips SET status = 'queued' WHERE status = 'rendering'")
        for i in range(self.workers):
            t = threading.Thread(target=self._loop, name=f"clip-worker-{i}", daemon=True)
            t.start()
            self._threads.append(t)

    def notify(self) -> None:
        self._wake.set()

    def cancel(self, job_id: str) -> None:
        self._cancel.add(job_id)

    # ---- claiming ----
    def _claim(self) -> tuple[str, str] | None:
        with self._claim_lock:
            job = db.one("SELECT id FROM jobs WHERE status = 'queued' ORDER BY created_at LIMIT 1")
            if job:
                db.execute("UPDATE jobs SET status = 'running', updated_at = ? WHERE id = ?", (db.now(), job["id"]))
                return "job", job["id"]
            clip = db.one(
                "SELECT c.id FROM clips c JOIN jobs j ON j.id = c.job_id "
                "WHERE c.status = 'queued' AND j.status IN ('done', 'failed') ORDER BY c.updated_at LIMIT 1")
            if clip:
                db.execute("UPDATE clips SET status = 'rendering', updated_at = ? WHERE id = ?",
                           (db.now(), clip["id"]))
                return "clip", clip["id"]
        return None

    def _loop(self) -> None:
        while True:
            task = self._claim()
            if not task:
                self._wake.wait(timeout=3)
                self._wake.clear()
                continue
            kind, ident = task
            try:
                if kind == "job":
                    self._run_job(ident)
                else:
                    self._rerender(ident)
            except Exception:
                log.exception("worker crashed on %s %s", kind, ident)

    # ---- helpers ----
    def _update(self, job_id: str, **fields) -> None:
        fields["updated_at"] = db.now()
        cols = ", ".join(f"{k} = ?" for k in fields)
        db.execute(f"UPDATE jobs SET {cols} WHERE id = ?", (*fields.values(), job_id))

    def _check(self, job_id: str) -> None:
        if job_id in self._cancel:
            raise Cancelled()

    # ---- the pipeline ----
    def _run_job(self, job_id: str) -> None:
        job = db.one("SELECT * FROM jobs WHERE id = ?", (job_id,))
        if not job:
            return
        opts = json.loads(job["options"] or "{}")
        jdir = job_dir(job_id)
        jdir.mkdir(parents=True, exist_ok=True)
        notes: list[str] = []
        stop = lambda: job_id in self._cancel  # noqa: E731
        prog = Progress(self, job_id)
        started = time.monotonic()
        timings: dict[str, float] = {}

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
                video_thread = threading.Thread(target=fetch_video, name=f"dl-{job_id}", daemon=True)
                video_thread.start()
            if transcript is None and use_captions:
                prog.set(0.01, "Fetching captions", force=True)
                try:
                    cpath, meta = ingest.download_captions(job["source"], jdir, stop)
                    title = meta.get("title")
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
                        audio_src, title = ingest.download_audio(
                            job["source"], jdir, lambda p: prog.set(0.01 + 0.04 * p), stop)
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
            provider = config.resolve_provider(opts.get("provider"))
            names = {"claude": "Claude", "ollama": "local AI", "heuristic": "built-in finder"}
            hl_label = ("Finding highlights (local AI is reviewing the strongest moments)" if provider == "ollama"
                        else f"Finding highlights ({names.get(provider, provider)})")
            prog.set(0.55, hl_label, force=True)
            found, used, note = highlights.find_highlights(
                transcript, duration, int(opts.get("max_clips", 6)),
                int(opts.get("min_len", 20)), int(opts.get("max_len", 60)), provider,
                audio, opts.get("prompt", ""),
                progress=lambda p: prog.set(0.55 + 0.1 * p, f"{hl_label} {p * 100:.0f}%"))
            if note:
                notes.append(note)
            if not found:
                raise RuntimeError("No usable clips were found in this video.")
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

            # 5. Render clips in parallel
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
        except (Cancelled, InterruptedError):
            self._update(job_id, status="cancelled", stage="Cancelled")
        except Exception as exc:
            if type(exc).__name__ == "DownloadCancelled" or stop():
                self._update(job_id, status="cancelled", stage="Cancelled")
            else:
                log.error("job %s failed: %s", job_id, traceback.format_exc())
                self._update(job_id, status="failed", stage="Failed", error=friendly_error(exc),
                             notes="\n".join(notes) or None)
        finally:
            if video_thread and video_thread.is_alive():
                self._cancel.add(job_id)  # stop the download if the job ended early
                video_thread.join(timeout=30)
            self._cancel.discard(job_id)
            self.notify()

    @staticmethod
    def _save_transcript(jdir: Path, transcript: dict) -> None:
        (jdir / "transcript.json").write_text(json.dumps(transcript, ensure_ascii=False), encoding="utf-8")
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
            if job_id in self._cancel:
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
                raise RuntimeError("Source video was deleted.")
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
