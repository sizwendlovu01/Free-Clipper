"""FastAPI web app: pages + JSON API."""
from __future__ import annotations

import hmac
import json
import logging
import re
import shutil
import tempfile
import uuid
import zipfile
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.background import BackgroundTask
from starlette.concurrency import run_in_threadpool
from starlette.middleware.sessions import SessionMiddleware

from . import auth, config, db
from .pipeline import ingest, media
from .pipeline.render import ASPECTS
from .worker import Runner, find_source, job_dir

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("freeclipper")

db.init()
runner = Runner(config.MAX_CONCURRENT_JOBS)
FFMPEG_OK = bool(shutil.which(config.FFMPEG)) and bool(shutil.which(config.FFPROBE))


@asynccontextmanager
async def lifespan(_app: FastAPI):
    runner.start()
    log.info("ClipForge AI ready: %d job worker(s), %d render worker(s), data in %s, database %s",
             config.MAX_CONCURRENT_JOBS, config.RENDER_WORKERS, config.DATA_DIR, config.DB_PATH)
    log.info("Limits: upload %d MB, video %ds, %d clips/job, job timeout %ds; highlight provider: %s",
             config.MAX_UPLOAD_MB, config.MAX_VIDEO_SECONDS, config.MAX_CLIPS_PER_JOB,
             config.JOB_TIMEOUT_SECONDS, config.resolve_provider("auto"))
    if not FFMPEG_OK:
        log.error("ffmpeg/ffprobe not found: video processing will fail")
    if not config.SECRET_KEY_FROM_ENV:
        log.warning("SECRET_KEY is not set; using a generated key stored in %s", config.DATA_DIR)
    yield
    log.info("Shutting down: returning running jobs to the queue")
    await run_in_threadpool(runner.shutdown, config.SHUTDOWN_GRACE_SECONDS)


app = FastAPI(title="ClipForge AI", docs_url="/api/docs", redoc_url=None, lifespan=lifespan)
app.add_middleware(SessionMiddleware, secret_key=config.SECRET_KEY, session_cookie="fc_session",
                   same_site="lax", https_only=config.HTTPS_ONLY_COOKIES, max_age=60 * 60 * 24 * 30)
app.mount("/static", StaticFiles(directory=config.PACKAGE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=config.PACKAGE_DIR / "templates")

VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v", ".flv", ".wmv", ".mpg", ".mpeg", ".ts",
              ".3gp", ".3g2", ".mts", ".m2ts", ".mxf", ".ogv", ".hevc",
              ".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg", ".opus", ".amr", ".wma"}
# Phones sometimes send a file without an extension (or with a generic name); use its media type instead.
MIME_EXTS = {"video/mp4": ".mp4", "video/quicktime": ".mov", "video/webm": ".webm", "video/x-matroska": ".mkv",
             "video/3gpp": ".3gp", "video/3gpp2": ".3g2", "video/mp2t": ".ts", "video/x-msvideo": ".avi",
             "audio/mpeg": ".mp3", "audio/mp4": ".m4a", "audio/x-m4a": ".m4a", "audio/aac": ".aac",
             "audio/wav": ".wav", "audio/x-wav": ".wav", "audio/ogg": ".ogg", "audio/webm": ".webm",
             "audio/amr": ".amr", "audio/flac": ".flac"}


def upload_extension(filename: str, content_type: str | None) -> str | None:
    ext = Path(filename or "").suffix.lower()
    if ext in VIDEO_EXTS:
        return ext
    ctype = (content_type or "").split(";")[0].strip().lower()
    if ctype in MIME_EXTS:
        return MIME_EXTS[ctype]
    if ctype.startswith(("video/", "audio/")):
        return ".mp4" if ctype.startswith("video/") else ".m4a"  # ffprobe checks the real content anyway
    return None
CLIP_FILES = {"clip.mp4": "video/mp4", "thumb.jpg": "image/jpeg", "captions.srt": "application/x-subrip",
              "captions.vtt": "text/vtt", "captions.ass": "text/plain", "meta.json": "application/json"}
FILE_KEYS = {"clip.mp4": "video", "thumb.jpg": "thumb", "captions.srt": "srt", "captions.vtt": "vtt",
             "captions.ass": "ass", "meta.json": "meta"}


def as_int(value) -> int | None:
    try:
        return int(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


# ---------------- security helpers ----------------

SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "same-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
}


@app.middleware("http")
async def guard(request: Request, call_next):
    """Cross-site write protection, early rejection of oversized uploads, and security headers."""
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        if "authorization" not in request.headers:
            origin = request.headers.get("origin") or request.headers.get("referer")
            # Behind a proxy (Railway) the public host can arrive as X-Forwarded-Host instead of Host.
            # A browser can't forge either header on a cross-site request, so accepting both is safe.
            allowed = {request.headers.get("host", "").lower()}
            allowed.update(h.strip().lower() for h in request.headers.get("x-forwarded-host", "").split(",") if h.strip())
            if origin and urlparse(origin).netloc.lower() not in allowed:
                return JSONResponse({"detail": "Cross-site request blocked."}, status_code=403)
        length = request.headers.get("content-length")
        # Video uploads: MAX_UPLOAD_SIZE_MB. Everything else (logos, fonts, JSON): 50 MB.
        limit = (config.MAX_UPLOAD_MB + 1) * 1024 * 1024 if request.url.path == "/api/jobs" else 50 * 1024 * 1024
        if length and length.isdigit() and int(length) > limit:
            return JSONResponse({"detail": f"Upload is larger than the {config.MAX_UPLOAD_MB} MB limit."}
                                if request.url.path == "/api/jobs" else {"detail": "Request too large."},
                                status_code=413)
    response = await call_next(request)
    for key, value in SECURITY_HEADERS.items():
        response.headers.setdefault(key, value)
    if config.HTTPS_ONLY_COOKIES:
        response.headers.setdefault("Strict-Transport-Security", "max-age=15552000")
    return response


def client_ip(request: Request) -> str:
    if config.CLIENT_IP_HEADER:
        value = request.headers.get(config.CLIENT_IP_HEADER, "").split(",")[0].strip()
        if value:
            return value
    return request.client.host if request.client else "unknown"


def current_user(request: Request) -> dict | None:
    header = request.headers.get("authorization", "")
    if header.lower().startswith("bearer "):
        return auth.user_by_token(header[7:].strip())
    uid = request.session.get("uid")
    return db.one("SELECT * FROM users WHERE id = ?", (uid,)) if uid else None


def require_user(request: Request) -> dict:
    user = current_user(request)
    if not user:
        raise HTTPException(401, "Sign in required.")
    return user


def require_admin(user: dict = Depends(require_user)) -> dict:
    if not user["is_admin"]:
        raise HTTPException(403, "Admins only.")
    return user


def brand() -> dict:
    b = db.get_setting("brand", {}) or {}
    return {
        "app_name": b.get("app_name") or "ClipForge AI",
        "accent": b.get("accent") or "#7c5cff",
        "has_logo": (config.BRAND_DIR / "logo.png").exists(),
        "watermark": {"enabled": False, "position": "top-right", "opacity": 0.8, "size": 0.18,
                      **(b.get("watermark") or {})},
    }


def own_job(job_id: str, user: dict) -> dict:
    job = db.one("SELECT * FROM jobs WHERE id = ?", (job_id,))
    if not job or job["user_id"] != user["id"]:
        raise HTTPException(404, "Job not found.")
    return job


def own_clip(clip_id: str, user: dict) -> tuple[dict, dict]:
    clip = db.one("SELECT * FROM clips WHERE id = ?", (clip_id,))
    if not clip:
        raise HTTPException(404, "Clip not found.")
    return clip, own_job(clip["job_id"], user)


def slug(text: str) -> str:
    s = re.sub(r"[^\w\-]+", "-", text, flags=re.UNICODE).strip("-")
    return (s or "clip")[:60]


# ---------------- serializers ----------------

def clip_json(c: dict) -> dict:
    base = f"/files/{c['id']}"
    version = re.sub(r"\D", "", c["updated_at"] or "")
    return {
        "id": c["id"], "job_id": c["job_id"], "index": c["idx"], "start": c["start"], "end": c["end"],
        "duration": round(c["end"] - c["start"], 2), "title": c["title"], "hook": c["hook"],
        "reason": c["reason"], "score": c["score"], "hashtags": json.loads(c["hashtags"] or "[]"),
        "template_id": c["template_id"], "status": c["status"], "error": c["error"],
        "updated_at": c["updated_at"],
        "files": {key: f"{base}/{name}?v={version}" for name, key in FILE_KEYS.items()}
        if c["status"] == "done" else {},
    }


def job_json(j: dict, with_clips: bool = False) -> dict:
    out = {k: j[k] for k in ("id", "title", "source_type", "status", "stage", "progress", "error", "notes",
                             "language", "duration", "created_at", "updated_at")}
    out["source"] = j["source"] if j["source_type"] == "url" else Path(j["source"]).name
    out["options"] = json.loads(j["options"] or "{}")
    counts = db.one("SELECT COUNT(*) AS n, SUM(status = 'done') AS done FROM clips WHERE job_id = ?", (j["id"],))
    out["clip_count"], out["clips_done"] = counts["n"] or 0, counts["done"] or 0
    thumb = db.one("SELECT id FROM clips WHERE job_id = ? AND status = 'done' ORDER BY score DESC LIMIT 1",
                   (j["id"],))
    out["thumb"] = f"/files/{thumb['id']}/thumb.jpg" if thumb else None
    out["source_available"] = find_source(j["id"]) is not None
    if with_clips:
        out["clips"] = [clip_json(c) for c in db.query(
            "SELECT * FROM clips WHERE job_id = ? ORDER BY score DESC, idx", (j["id"],))]
    return out


def user_storage_mb(user_id: int) -> int:
    total = 0
    for j in db.query("SELECT id FROM jobs WHERE user_id = ?", (user_id,)):
        for p in job_dir(j["id"]).rglob("*"):
            try:
                if p.is_file():
                    total += p.stat().st_size
            except OSError:
                pass
    return int(total / 1024 / 1024)


def usage(user: dict) -> dict:
    active = db.one("SELECT COUNT(*) AS n FROM jobs WHERE user_id = ? AND status IN ('queued', 'running')",
                    (user["id"],))["n"]
    since = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat(timespec="seconds")
    today = db.one("SELECT COUNT(*) AS n FROM jobs WHERE user_id = ? AND created_at >= ?",
                   (user["id"], since))["n"]
    return {"active_jobs": active, "jobs_last_24h": today, "storage_mb": user_storage_mb(user["id"])}


def check_capacity(user: dict, new_job: bool = True) -> None:
    """Cost protection: server-wide queue and disk limits, plus per-user quotas (admins exempt)."""
    from .worker import disk_free_mb, min_free_mb

    queued = db.one("SELECT COUNT(*) AS n FROM jobs WHERE status = 'queued'")["n"]
    if queued >= config.MAX_QUEUED_JOBS:
        raise HTTPException(503, "The processing queue is full right now. Please try again in a few minutes.")
    if disk_free_mb() < min_free_mb():
        raise HTTPException(507, "The server's storage is nearly full. Please try again later.")
    if user["is_admin"]:
        return
    u = usage(user)
    if u["active_jobs"] >= config.USER_MAX_ACTIVE_JOBS:
        raise HTTPException(429, f"You already have {u['active_jobs']} video(s) processing. Wait for one to finish.")
    if new_job and u["jobs_last_24h"] >= config.USER_JOBS_PER_DAY:
        raise HTTPException(429, f"Daily limit reached ({config.USER_JOBS_PER_DAY} videos per 24 hours).")
    if u["storage_mb"] >= config.USER_MAX_STORAGE_MB:
        raise HTTPException(507, f"Your storage is full ({u['storage_mb']} of {config.USER_MAX_STORAGE_MB} MB). "
                                 "Delete old projects to make room.")


def check_render_rate(user: dict) -> None:
    if not user["is_admin"] and not auth.limiter.allow(f"render:{user['id']}", config.USER_RENDERS_PER_HOUR, 3600):
        raise HTTPException(429, f"Re-render limit reached ({config.USER_RENDERS_PER_HOUR} per hour). Try again later.")


def clean_options(raw: dict, user: dict) -> dict:
    def num(key, default, lo, hi):
        try:
            return max(lo, min(hi, int(raw.get(key, default))))
        except (TypeError, ValueError):
            return default

    min_len = num("min_len", 20, 5, 180)
    opts = {
        "max_clips": num("max_clips", min(6, config.MAX_CLIPS_PER_JOB), 1, config.MAX_CLIPS_PER_JOB),
        "min_len": min_len,
        "max_len": max(min_len + 5, num("max_len", 60, 10, 300)),
        "aspect": raw.get("aspect") if raw.get("aspect") in (*ASPECTS, "original") else "9:16",
        "reframe": raw.get("reframe") if raw.get("reframe") in ("auto", "center", "fit_blur", "fit_black") else "auto",
        "captions": bool(raw.get("captions", True)),
        "loudnorm": bool(raw.get("loudnorm", True)),
        "translate": bool(raw.get("translate", False)),
        "language": re.sub(r"[^a-zA-Z\-]", "", str(raw.get("language") or ""))[:10].lower(),
        "whisper_model": raw.get("whisper_model") if raw.get("whisper_model") in config.WHISPER_MODELS
        else config.WHISPER_MODEL,
        "transcript_source": raw.get("transcript_source") if raw.get("transcript_source") in ("auto", "whisper")
        else "auto",
        "provider": raw.get("provider") if raw.get("provider") in ("auto", "claude", "ollama", "heuristic") else "auto",
        "prompt": str(raw.get("prompt") or "")[:1000],
        "template_id": None,
    }
    tid = as_int(raw.get("template_id"))
    if tid:
        t = db.one("SELECT id FROM templates WHERE id = ? AND (user_id IS NULL OR user_id = ?)", (tid, user["id"]))
        opts["template_id"] = t["id"] if t else None
    return opts


# ---------------- pages ----------------

def _asset_version() -> str:
    """Fingerprint of the static files, appended to their URLs so browsers fetch new CSS/JS right after a
    deploy instead of reusing stale cached copies."""
    import hashlib

    digest = hashlib.sha256()
    for p in sorted((config.PACKAGE_DIR / "static").glob("*")):
        if p.is_file():
            digest.update(p.name.encode())
            digest.update(p.read_bytes())
    return digest.hexdigest()[:10]


ASSET_VERSION = _asset_version()


def page(request: Request, name: str, user: dict | None, **ctx):
    return templates.TemplateResponse(
        request, name, {"brand": brand(), "user": user, "allow_signup": config.ALLOW_SIGNUP,
                        "v": ASSET_VERSION, **ctx})


def page_user(request: Request) -> dict | RedirectResponse:
    if not db.one("SELECT id FROM users LIMIT 1"):
        return RedirectResponse("/setup", 303)
    user = current_user(request)
    return user if user else RedirectResponse("/login", 303)


@app.get("/")
def dashboard(request: Request):
    """Signed-in users get their dashboard; visitors get the public landing page."""
    if not db.one("SELECT id FROM users LIMIT 1"):
        return RedirectResponse("/setup", 303)
    user = current_user(request)
    if not user:
        return page(request, "landing.html", None)
    return page(request, "dashboard.html", user, aspects=list(ASPECTS), whisper_models=config.WHISPER_MODELS,
                default_model=config.WHISPER_MODEL, max_upload_mb=config.MAX_UPLOAD_MB,
                max_video_minutes=config.MAX_VIDEO_SECONDS // 60, max_clips=config.MAX_CLIPS_PER_JOB)


@app.get("/jobs/{job_id}")
def job_page(request: Request, job_id: str):
    user = page_user(request)
    if isinstance(user, Response):
        return user
    own_job(job_id, user)
    return page(request, "job.html", user, job_id=job_id)


@app.get("/clips")
def clips_page(request: Request):
    user = page_user(request)
    if isinstance(user, Response):
        return user
    return page(request, "clips.html", user)


@app.get("/templates")
def templates_page(request: Request):
    user = page_user(request)
    if isinstance(user, Response):
        return user
    return page(request, "templates.html", user)


@app.get("/settings")
def settings_page(request: Request):
    user = page_user(request)
    if isinstance(user, Response):
        return user
    return page(request, "settings.html", user)


@app.get("/login")
def login_page(request: Request):
    if not db.one("SELECT id FROM users LIMIT 1"):
        return RedirectResponse("/setup", 303)
    if current_user(request):
        return RedirectResponse("/", 303)
    return page(request, "login.html", None)


@app.get("/signup")
def signup_page(request: Request):
    if not db.one("SELECT id FROM users LIMIT 1"):
        return RedirectResponse("/setup", 303)
    if current_user(request):
        return RedirectResponse("/", 303)
    return page(request, "signup.html", None)


@app.get("/setup")
def setup_page(request: Request):
    if db.one("SELECT id FROM users LIMIT 1"):
        return RedirectResponse("/login", 303)
    return page(request, "setup.html", None, needs_token=bool(config.SETUP_TOKEN))


@app.post("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/", 303)


# ---------------- auth API ----------------

@app.post("/api/setup")
async def api_setup(request: Request):
    body = await request.json()
    if db.one("SELECT id FROM users LIMIT 1"):
        raise HTTPException(400, "Setup is already complete.")
    if config.SETUP_TOKEN and not hmac.compare_digest(str(body.get("setup_token", "")), config.SETUP_TOKEN):
        raise HTTPException(403, "Wrong setup code (FC_SETUP_TOKEN).")
    try:
        uid = auth.create_user(body.get("username", ""), body.get("password", ""), is_admin=True)
    except ValueError as e:
        raise HTTPException(400, str(e))
    request.session["uid"] = uid
    return {"ok": True}


@app.post("/api/login")
async def api_login(request: Request):
    ip = client_ip(request)
    if auth.throttle.blocked(ip):
        raise HTTPException(429, "Too many failed attempts. Try again in a few minutes.")
    body = await request.json()
    user = db.one("SELECT * FROM users WHERE username = ?", (str(body.get("username", "")).strip(),))
    if not user or not auth.verify_password(str(body.get("password", "")), user["password_hash"]):
        auth.throttle.fail(ip)
        raise HTTPException(401, "Wrong username or password.")
    auth.throttle.reset(ip)
    request.session["uid"] = user["id"]
    return {"ok": True}


@app.post("/api/signup")
async def api_signup(request: Request):
    if not config.ALLOW_SIGNUP:
        raise HTTPException(403, "Sign-up is disabled. Ask an admin for an account.")
    body = await request.json()
    ip = client_ip(request)
    if not auth.limiter.allow(f"signup:{ip}", config.SIGNUPS_PER_IP_PER_DAY, 86400):
        raise HTTPException(429, "Too many new accounts from this network today. Try again tomorrow.")
    try:
        uid = auth.create_user(body.get("username", ""), body.get("password", ""))
    except ValueError as e:
        raise HTTPException(400, str(e))
    request.session["uid"] = uid
    return {"ok": True}


@app.get("/api/me")
def api_me(user: dict = Depends(require_user)):
    return {"id": user["id"], "username": user["username"], "is_admin": bool(user["is_admin"]),
            "has_api_token": bool(user["api_token_hash"])}


@app.post("/api/me/password")
async def api_password(request: Request, user: dict = Depends(require_user)):
    body = await request.json()
    if not auth.verify_password(str(body.get("current", "")), user["password_hash"]):
        raise HTTPException(400, "Current password is wrong.")
    if len(str(body.get("new", ""))) < 8:
        raise HTTPException(400, "New password must be at least 8 characters.")
    db.execute("UPDATE users SET password_hash = ? WHERE id = ?", (auth.hash_password(body["new"]), user["id"]))
    return {"ok": True}


@app.post("/api/me/token")
def api_token(user: dict = Depends(require_user)):
    return {"token": auth.new_api_token(user["id"])}


@app.delete("/api/me/token")
def api_token_revoke(user: dict = Depends(require_user)):
    db.execute("UPDATE users SET api_token_hash = NULL WHERE id = ?", (user["id"],))
    return {"ok": True}


# ---------------- users (admin) ----------------

@app.get("/api/users")
def api_users(admin: dict = Depends(require_admin)):
    return [{"id": u["id"], "username": u["username"], "is_admin": bool(u["is_admin"]), "created_at": u["created_at"]}
            for u in db.query("SELECT * FROM users ORDER BY id")]


@app.post("/api/users")
async def api_user_create(request: Request, admin: dict = Depends(require_admin)):
    body = await request.json()
    try:
        uid = auth.create_user(body.get("username", ""), body.get("password", ""), bool(body.get("is_admin")))
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"id": uid}


@app.delete("/api/users/{uid}")
def api_user_delete(uid: int, admin: dict = Depends(require_admin)):
    if uid == admin["id"]:
        raise HTTPException(400, "You cannot delete yourself.")
    for j in db.query("SELECT id, status FROM jobs WHERE user_id = ?", (uid,)):
        if j["status"] == "running":
            raise HTTPException(409, "That user has a job running. Cancel it first.")
    for j in db.query("SELECT id FROM jobs WHERE user_id = ?", (uid,)):
        shutil.rmtree(job_dir(j["id"]), ignore_errors=True)
    db.execute("DELETE FROM users WHERE id = ?", (uid,))
    return {"ok": True}


# ---------------- system / branding ----------------

@app.get("/api/system")
def api_system(user: dict = Depends(require_user)):
    from .worker import disk_free_mb, disk_total_mb

    queue = db.one("SELECT SUM(status = 'queued') AS queued, SUM(status = 'running') AS running FROM jobs")
    info = {
        "workers": config.MAX_CONCURRENT_JOBS,
        "queued": queue["queued"] or 0, "running": queue["running"] or 0,
        "default_provider": config.resolve_provider("auto"),
        "claude_configured": config.has_claude_credentials(), "claude_model": config.CLAUDE_MODEL,
        "ollama_model": config.OLLAMA_MODEL,
        "whisper_default": config.WHISPER_MODEL, "whisper_models": config.WHISPER_MODELS,
        "fonts": sorted(p.name for p in config.FONTS_DIR.iterdir() if p.suffix.lower() in (".ttf", ".otf")),
        "limits": {
            "max_upload_mb": config.MAX_UPLOAD_MB, "max_video_seconds": config.MAX_VIDEO_SECONDS,
            "max_clips_per_job": config.MAX_CLIPS_PER_JOB, "user_max_active_jobs": config.USER_MAX_ACTIVE_JOBS,
            "user_jobs_per_day": config.USER_JOBS_PER_DAY, "user_max_storage_mb": config.USER_MAX_STORAGE_MB,
            "quotas_apply": not user["is_admin"],
        },
        "usage": usage(user),
    }
    if user["is_admin"]:  # server details only for admins
        info.update(ollama_url=config.OLLAMA_URL, data_dir=str(config.DATA_DIR), disk_free_mb=disk_free_mb(),
                    disk_total_mb=disk_total_mb(),
                    render_workers=config.RENDER_WORKERS,
                    youtube_cookies=config.YTDLP_COOKIES_FILE.is_file(), youtube_proxy=bool(config.YTDLP_PROXY))
    return info


@app.get("/health")
@app.get("/healthz")
def health():
    """Cheap readiness check for hosting platforms: database reachable, ffmpeg installed. Never touches
    video processing."""
    from .worker import disk_free_mb, min_free_mb

    try:
        db.one("SELECT 1 AS ok")
    except Exception:
        log.exception("health check: database error")
        return JSONResponse({"ok": False, "detail": "database unavailable"}, status_code=503)
    if not FFMPEG_OK:
        return JSONResponse({"ok": False, "detail": "ffmpeg not found"}, status_code=503)
    free = disk_free_mb()
    return {"ok": True, "disk_low": free < min_free_mb()}


@app.get("/api/admin/backup")
async def api_backup(admin: dict = Depends(require_admin)):
    """Consistent snapshot of the SQLite database, safe to take while the app is running."""
    fd, path = tempfile.mkstemp(suffix=".db")
    import os
    os.close(fd)
    await run_in_threadpool(db.backup_to, path)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return FileResponse(path, filename=f"clipforge-{stamp}.db", media_type="application/octet-stream",
                        background=BackgroundTask(lambda: Path(path).unlink(missing_ok=True)))


@app.post("/api/admin/cleanup")
async def api_cleanup(admin: dict = Depends(require_admin)):
    """Run the periodic cleanup now (also runs automatically every CLEANUP_INTERVAL_MINUTES)."""
    return await run_in_threadpool(runner.cleanup)


@app.post("/api/admin/youtube-cookies")
async def api_youtube_cookies(file: UploadFile = File(...), admin: dict = Depends(require_admin)):
    """Store a Netscape-format cookies.txt for YouTube downloads. Never served back to anyone."""
    data = await file.read(1024 * 1024 + 1)
    if len(data) > 1024 * 1024:
        raise HTTPException(400, "That file is too large for a cookies.txt file.")
    text = data.decode("utf-8", errors="replace")
    rows = [line for line in text.splitlines() if line.strip() and not line.startswith("#")]
    if not rows or not all(len(line.split("\t")) >= 7 for line in rows[:20]):
        raise HTTPException(400, "This isn't a cookies.txt file. Export one in Netscape format with a "
                                 "'Get cookies.txt' browser extension while signed in to YouTube.")
    if not any("youtube.com" in line or "google.com" in line for line in rows):
        raise HTTPException(400, "The file has no YouTube or Google cookies. Export it while on youtube.com.")
    target = config.YTDLP_COOKIES_FILE
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(".tmp")
    tmp.write_text(text, encoding="utf-8")
    try:
        tmp.chmod(0o600)
    except OSError:
        pass
    tmp.replace(target)
    return {"ok": True, "cookies": len(rows)}


@app.delete("/api/admin/youtube-cookies")
def api_youtube_cookies_delete(admin: dict = Depends(require_admin)):
    config.YTDLP_COOKIES_FILE.unlink(missing_ok=True)
    return {"ok": True}


@app.get("/api/branding")
def api_brand_get():
    return brand()


@app.put("/api/branding")
async def api_brand_put(request: Request, admin: dict = Depends(require_admin)):
    body = await request.json()
    current = db.get_setting("brand", {}) or {}
    name = str(body.get("app_name", current.get("app_name", "ClipForge AI")))[:40].strip() or "ClipForge AI"
    accent = str(body.get("accent", current.get("accent", "#7c5cff")))
    if not re.fullmatch(r"#[0-9a-fA-F]{6}", accent):
        raise HTTPException(400, "Accent must be a hex color like #7c5cff.")
    wm_in = body.get("watermark") or {}
    wm = {
        "enabled": bool(wm_in.get("enabled")),
        "position": wm_in.get("position") if wm_in.get("position") in
        ("top-left", "top-right", "bottom-left", "bottom-right") else "top-right",
        "opacity": max(0.05, min(1.0, float(wm_in.get("opacity", 0.8)))),
        "size": max(0.05, min(0.5, float(wm_in.get("size", 0.18)))),
    }
    db.set_setting("brand", {"app_name": name, "accent": accent, "watermark": wm})
    return brand()


@app.post("/api/branding/logo")
async def api_logo(file: UploadFile = File(...), admin: dict = Depends(require_admin)):
    with tempfile.TemporaryDirectory() as tmp:
        raw = Path(tmp) / ("upload" + Path(file.filename or "x.png").suffix.lower()[:6])
        with raw.open("wb") as f:
            shutil.copyfileobj(file.file, f)
        try:
            await run_in_threadpool(media.run, [config.FFMPEG, "-y", "-v", "error", "-i", str(raw), "-frames:v", "1",
                                                "-vf", "scale='min(800,iw)':-2", str(config.BRAND_DIR / "logo.png")])
        except media.MediaError:
            raise HTTPException(400, "Could not read that image.")
    return brand()


@app.delete("/api/branding/logo")
def api_logo_delete(admin: dict = Depends(require_admin)):
    (config.BRAND_DIR / "logo.png").unlink(missing_ok=True)
    return brand()


@app.get("/branding/logo.png")
def logo_file():
    path = config.BRAND_DIR / "logo.png"
    if not path.exists():
        raise HTTPException(404)
    return FileResponse(path, media_type="image/png", headers={"Cache-Control": "no-cache"})


@app.post("/api/fonts")
async def api_font(file: UploadFile = File(...), admin: dict = Depends(require_admin)):
    name = Path(file.filename or "").name
    if Path(name).suffix.lower() not in (".ttf", ".otf") or not re.fullmatch(r"[\w .\-]+", name):
        raise HTTPException(400, "Upload a .ttf or .otf font file.")
    with (config.FONTS_DIR / name).open("wb") as f:
        shutil.copyfileobj(file.file, f)
    return {"ok": True, "name": name}


# ---------------- caption templates ----------------

STYLE_FIELDS = {
    "font": str, "font_size": int, "bold": bool, "uppercase": bool, "text_color": str,
    "highlight_color": str, "outline_color": str, "outline": float, "shadow": float, "position": str,
    "margin_v": int, "words_per_line": int, "highlight_mode": str, "animation": str, "box": bool,
    "box_color": str, "box_opacity": float,
}


def clean_style(raw: dict) -> dict:
    style = dict(db.DEFAULT_TEMPLATES[0][1])
    for key, typ in STYLE_FIELDS.items():
        if key in raw:
            try:
                style[key] = typ(raw[key]) if typ is not bool else bool(raw[key])
            except (TypeError, ValueError):
                raise HTTPException(400, f"Bad value for {key}.")
    for key in ("text_color", "highlight_color", "outline_color", "box_color"):
        if not re.fullmatch(r"#[0-9a-fA-F]{6}", style[key]):
            raise HTTPException(400, f"{key} must be a hex color.")
    style["font"] = re.sub(r"[^\w \-]", "", style["font"])[:60] or "Arial"
    style["font_size"] = max(20, min(200, style["font_size"]))
    style["words_per_line"] = max(1, min(12, style["words_per_line"]))
    style["margin_v"] = max(0, min(900, style["margin_v"]))
    style["outline"] = max(0, min(20, style["outline"]))
    style["shadow"] = max(0, min(20, style["shadow"]))
    style["box_opacity"] = max(0, min(1, style["box_opacity"]))
    if style["position"] not in ("top", "middle", "bottom"):
        style["position"] = "bottom"
    if style["highlight_mode"] not in ("word", "none"):
        style["highlight_mode"] = "word"
    if style["animation"] not in ("pop", "none"):
        style["animation"] = "none"
    return style


def template_json(t: dict) -> dict:
    return {"id": t["id"], "name": t["name"], "style": json.loads(t["style"]), "builtin": t["user_id"] is None}


@app.get("/api/templates")
def api_templates(user: dict = Depends(require_user)):
    rows = db.query("SELECT * FROM templates WHERE user_id IS NULL OR user_id = ? ORDER BY user_id IS NOT NULL, id",
                    (user["id"],))
    return [template_json(t) for t in rows]


@app.post("/api/templates")
async def api_template_create(request: Request, user: dict = Depends(require_user)):
    body = await request.json()
    name = str(body.get("name") or "My template")[:60]
    tid = db.execute("INSERT INTO templates(user_id, name, style, created_at) VALUES(?, ?, ?, ?)",
                     (user["id"], name, json.dumps(clean_style(body.get("style") or {})), db.now()))
    return template_json(db.one("SELECT * FROM templates WHERE id = ?", (tid,)))


def editable_template(tid: int, user: dict) -> dict:
    t = db.one("SELECT * FROM templates WHERE id = ?", (tid,))
    if not t or (t["user_id"] is None and not user["is_admin"]) or (t["user_id"] not in (None, user["id"])):
        raise HTTPException(404, "Template not found or not editable.")
    return t


@app.put("/api/templates/{tid}")
async def api_template_update(tid: int, request: Request, user: dict = Depends(require_user)):
    editable_template(tid, user)
    body = await request.json()
    db.execute("UPDATE templates SET name = ?, style = ? WHERE id = ?",
               (str(body.get("name") or "Template")[:60], json.dumps(clean_style(body.get("style") or {})), tid))
    return template_json(db.one("SELECT * FROM templates WHERE id = ?", (tid,)))


@app.delete("/api/templates/{tid}")
def api_template_delete(tid: int, user: dict = Depends(require_user)):
    editable_template(tid, user)
    if db.one("SELECT COUNT(*) AS n FROM templates WHERE user_id IS NULL")["n"] <= 1 and \
            db.one("SELECT user_id FROM templates WHERE id = ?", (tid,))["user_id"] is None:
        raise HTTPException(400, "Keep at least one built-in template.")
    db.execute("DELETE FROM templates WHERE id = ?", (tid,))
    return {"ok": True}


# ---------------- jobs ----------------

@app.get("/api/jobs")
def api_jobs(user: dict = Depends(require_user)):
    return [job_json(j) for j in db.query("SELECT * FROM jobs WHERE user_id = ? ORDER BY created_at DESC",
                                          (user["id"],))]


@app.post("/api/jobs")
async def api_job_create(request: Request, file: UploadFile | None = File(None), url: str = Form(""),
                         options: str = Form("{}"), title: str = Form(""),
                         user: dict = Depends(require_user)):
    try:
        opts = clean_options(json.loads(options or "{}"), user)
    except json.JSONDecodeError:
        raise HTTPException(400, "options must be JSON.")
    await run_in_threadpool(check_capacity, user)
    job_id = uuid.uuid4().hex[:16]
    jdir = job_dir(job_id)
    if file is not None and file.filename:
        ext = upload_extension(file.filename, file.content_type)
        if not ext:
            raise HTTPException(400, f"Unsupported file type {Path(file.filename).suffix or '(none)'}. "
                                     "Upload a video or audio file such as MP4, MOV, WEBM or MP3.")
        jdir.mkdir(parents=True, exist_ok=True)
        dest = jdir / f"source{ext}"
        limit = config.MAX_UPLOAD_MB * 1024 * 1024

        def save() -> int:
            written = 0
            with dest.open("wb") as f:
                while chunk := file.file.read(4 * 1024 * 1024):
                    written += len(chunk)
                    if written > limit:
                        raise ValueError
                    f.write(chunk)
            return written

        try:
            await run_in_threadpool(save)
        except ValueError:
            shutil.rmtree(jdir, ignore_errors=True)
            raise HTTPException(413, f"File is larger than {config.MAX_UPLOAD_MB} MB.")
        # Reject anything that isn't real audio/video, or is too long, before it takes a queue slot.
        try:
            info = await run_in_threadpool(media.probe, dest)
        except media.MediaError as e:
            shutil.rmtree(jdir, ignore_errors=True)
            raise HTTPException(400, str(e) if "supported" in str(e) else
                                "This file couldn't be read as video or audio.")
        if info["duration"] > config.MAX_VIDEO_SECONDS + 5:
            shutil.rmtree(jdir, ignore_errors=True)
            raise HTTPException(400, f"This video is {info['duration'] / 60:.0f} minutes long; the limit is "
                                     f"{config.MAX_VIDEO_SECONDS // 60} minutes.")
        source_type, source = "upload", Path(file.filename).name[:200]
        job_title = title.strip() or Path(file.filename).stem
    elif url.strip():
        try:
            source = await run_in_threadpool(ingest.validate_url, url)
        except ValueError as e:
            raise HTTPException(400, str(e))
        source_type, job_title = "url", title.strip() or source
    else:
        raise HTTPException(400, "Upload a file or paste a link.")
    db.execute(
        "INSERT INTO jobs(id, user_id, title, source_type, source, status, stage, options, created_at, updated_at) "
        "VALUES(?, ?, ?, ?, ?, 'queued', 'Waiting in queue', ?, ?, ?)",
        (job_id, user["id"], job_title[:200], source_type, source, json.dumps(opts), db.now(), db.now()))
    runner.notify()
    return job_json(db.one("SELECT * FROM jobs WHERE id = ?", (job_id,)))


@app.get("/api/jobs/{job_id}")
def api_job(job_id: str, user: dict = Depends(require_user)):
    return job_json(own_job(job_id, user), with_clips=True)


@app.post("/api/jobs/{job_id}/cancel")
def api_job_cancel(job_id: str, user: dict = Depends(require_user)):
    job = own_job(job_id, user)
    if job["status"] == "queued":
        db.execute("UPDATE jobs SET status = 'cancelled', stage = 'Cancelled' WHERE id = ?", (job_id,))
    elif job["status"] == "running":
        runner.cancel(job_id)
    return {"ok": True}


@app.post("/api/jobs/{job_id}/reanalyze")
async def api_job_reanalyze(job_id: str, request: Request, user: dict = Depends(require_user)):
    job = own_job(job_id, user)
    if job["status"] in ("queued", "running"):
        raise HTTPException(409, "This job is already in progress.")
    await run_in_threadpool(check_capacity, user, False)
    if not user["is_admin"] and not auth.limiter.allow(f"reanalyze:{user['id']}", config.USER_JOBS_PER_DAY, 86400):
        raise HTTPException(429, f"Limit reached ({config.USER_JOBS_PER_DAY} re-runs per 24 hours).")
    if job["source_type"] == "upload" and find_source(job_id) is None:
        raise HTTPException(409, "The source video is no longer stored, so this project can't be processed again.")
    body = await request.json() if request.headers.get("content-length", "0") != "0" else {}
    merged = {**json.loads(job["options"] or "{}"), **(body.get("options") or {})}
    db.execute("UPDATE jobs SET status = 'queued', stage = 'Waiting in queue', progress = 0, error = NULL, "
               "notes = NULL, options = ?, updated_at = ? WHERE id = ?",
               (json.dumps(clean_options(merged, user)), db.now(), job_id))
    runner.notify()
    return {"ok": True}


@app.delete("/api/jobs/{job_id}")
def api_job_delete(job_id: str, user: dict = Depends(require_user)):
    job = own_job(job_id, user)
    if job["status"] == "running":
        raise HTTPException(409, "Cancel the job before deleting it.")
    db.execute("DELETE FROM jobs WHERE id = ?", (job_id,))
    shutil.rmtree(job_dir(job_id), ignore_errors=True)
    return {"ok": True}


@app.get("/api/jobs/{job_id}/transcript.{fmt}")
def api_transcript(job_id: str, fmt: str, user: dict = Depends(require_user)):
    job = own_job(job_id, user)
    jdir = job_dir(job_id)
    name = slug(job["title"])
    if fmt in ("srt", "vtt", "json"):
        path = jdir / f"transcript.{fmt}"
        if path.exists():
            return FileResponse(path, filename=f"{name}.{fmt}")
    if fmt == "txt" and (jdir / "transcript.json").exists():
        data = json.loads((jdir / "transcript.json").read_text(encoding="utf-8"))
        text = "\n".join(s["text"] for s in data.get("segments", []))
        return Response(text, media_type="text/plain; charset=utf-8",
                        headers={"Content-Disposition": f'attachment; filename="{name}.txt"'})
    raise HTTPException(404, "Transcript not ready.")


@app.get("/api/jobs/{job_id}/source")
def api_source(job_id: str, user: dict = Depends(require_user)):
    own_job(job_id, user)
    src = find_source(job_id)
    if not src:
        raise HTTPException(404)
    return FileResponse(src)


@app.get("/api/jobs/{job_id}/export.zip")
async def api_export(job_id: str, user: dict = Depends(require_user)):
    job = own_job(job_id, user)
    jdir = job_dir(job_id)
    clips = db.query("SELECT * FROM clips WHERE job_id = ? AND status = 'done' ORDER BY score DESC", (job_id,))
    if not clips:
        raise HTTPException(404, "No finished clips yet.")

    def build() -> str:
        fd, path = tempfile.mkstemp(suffix=".zip")
        with zipfile.ZipFile(open(fd, "wb", closefd=True), "w") as zf:
            for f in ("transcript.json", "transcript.srt", "transcript.vtt"):
                if (jdir / f).exists():
                    zf.write(jdir / f, f, compress_type=zipfile.ZIP_DEFLATED)
            index = []
            for rank, c in enumerate(clips, 1):
                folder = f"{rank:02d}-{slug(c['title'])}"
                cdir = jdir / "clips" / c["id"]
                for name in CLIP_FILES:
                    if (cdir / name).exists():
                        zf.write(cdir / name, f"{folder}/{name}",
                                 compress_type=zipfile.ZIP_STORED if name.endswith((".mp4", ".jpg"))
                                 else zipfile.ZIP_DEFLATED)
                index.append({**clip_json(c), "folder": folder})
            zf.writestr("clips.json", json.dumps({"job": job_json(job), "clips": index}, ensure_ascii=False, indent=2))
        return path

    path = await run_in_threadpool(build)
    return FileResponse(path, filename=f"{slug(job['title'])}-clips.zip", media_type="application/zip",
                        background=BackgroundTask(lambda: Path(path).unlink(missing_ok=True)))


# ---------------- clips ----------------

@app.get("/api/clips")
def api_all_clips(user: dict = Depends(require_user)):
    rows = db.query("SELECT c.*, j.title AS job_title FROM clips c JOIN jobs j ON j.id = c.job_id "
                    "WHERE j.user_id = ? AND c.status = 'done' ORDER BY c.updated_at DESC", (user["id"],))
    return [{**clip_json(r), "job_title": r["job_title"]} for r in rows]


@app.post("/api/jobs/{job_id}/clips")
async def api_clip_add(job_id: str, request: Request, user: dict = Depends(require_user)):
    job = own_job(job_id, user)
    if job["status"] not in ("done", "failed") or not (job_dir(job_id) / "transcript.json").exists():
        raise HTTPException(409, "Wait for the job to finish first.")
    if find_source(job_id) is None:
        raise HTTPException(409, "The source video is no longer stored, so new clips can't be made.")
    body = await request.json()
    start, end = _bounds(body, job)
    check_render_rate(user)
    idx = (db.one("SELECT MAX(idx) AS m FROM clips WHERE job_id = ?", (job_id,))["m"] or 0) + 1
    cid = uuid.uuid4().hex[:12]
    db.execute("INSERT INTO clips(id, job_id, idx, start, \"end\", title, score, template_id, status, created_at, "
               "updated_at) VALUES(?,?,?,?,?,?,?,?, 'queued', ?, ?)",
               (cid, job_id, idx, start, end, str(body.get("title") or "Custom clip")[:120], 50,
                as_int(body.get("template_id")), db.now(), db.now()))
    runner.notify()
    return clip_json(db.one("SELECT * FROM clips WHERE id = ?", (cid,)))


def _bounds(body: dict, job: dict) -> tuple[float, float]:
    try:
        start, end = float(body["start"]), float(body["end"])
    except (KeyError, TypeError, ValueError):
        raise HTTPException(400, "start and end (seconds) are required.")
    duration = float(job["duration"] or 0)
    if start < 0 or end <= start + 1 or (duration and end > duration + 0.5) or end - start > 600:
        raise HTTPException(400, "Clip must be 1s to 10min long and inside the video.")
    return round(start, 3), round(min(end, duration or end), 3)


@app.patch("/api/clips/{clip_id}")
async def api_clip_update(clip_id: str, request: Request, user: dict = Depends(require_user)):
    clip, job = own_clip(clip_id, user)
    body = await request.json()
    title = str(body.get("title", clip["title"]))[:120]
    hashtags = body.get("hashtags", json.loads(clip["hashtags"] or "[]"))
    if not isinstance(hashtags, list):
        raise HTTPException(400, "hashtags must be a list.")
    start, end = _bounds({"start": body.get("start", clip["start"]), "end": body.get("end", clip["end"])}, job)
    template_id = as_int(body.get("template_id", clip["template_id"]))
    if template_id and not db.one("SELECT id FROM templates WHERE id = ? AND (user_id IS NULL OR user_id = ?)",
                                  (template_id, user["id"])):
        raise HTTPException(400, "Unknown template.")
    needs_render = (start, end) != (clip["start"], clip["end"]) or template_id != clip["template_id"] \
        or bool(body.get("rerender"))
    if needs_render and clip["status"] == "rendering":
        raise HTTPException(409, "This clip is rendering right now.")
    if needs_render:
        check_render_rate(user)
    db.execute("UPDATE clips SET title = ?, hashtags = ?, start = ?, \"end\" = ?, template_id = ?, status = ?, "
               "updated_at = ? WHERE id = ?",
               (title, json.dumps([str(h)[:40] for h in hashtags][:15], ensure_ascii=False), start, end,
                template_id, "queued" if needs_render else clip["status"], db.now(), clip_id))
    if needs_render:
        runner.notify()
    return clip_json(db.one("SELECT * FROM clips WHERE id = ?", (clip_id,)))


@app.delete("/api/clips/{clip_id}")
def api_clip_delete(clip_id: str, user: dict = Depends(require_user)):
    clip, job = own_clip(clip_id, user)
    if clip["status"] == "rendering":
        raise HTTPException(409, "This clip is rendering right now.")
    db.execute("DELETE FROM clips WHERE id = ?", (clip_id,))
    shutil.rmtree(job_dir(job["id"]) / "clips" / clip_id, ignore_errors=True)
    return {"ok": True}


@app.get("/files/{clip_id}/{name}")
def clip_file(clip_id: str, name: str, request: Request, download: int = 0):
    user = require_user(request)
    if name not in CLIP_FILES:
        raise HTTPException(404)
    clip, job = own_clip(clip_id, user)
    path = job_dir(job["id"]) / "clips" / clip_id / name
    if not path.exists():
        raise HTTPException(404)
    kwargs = {"filename": f"{slug(clip['title'])}{Path(name).suffix}"} if download else {}
    return FileResponse(path, media_type=CLIP_FILES[name], headers={"Cache-Control": "private, max-age=60"},
                        **kwargs)


@app.exception_handler(HTTPException)
async def http_error(request: Request, exc: HTTPException):
    if exc.status_code == 401 and not request.url.path.startswith(("/api", "/files")):
        return RedirectResponse("/login", 303)
    return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
