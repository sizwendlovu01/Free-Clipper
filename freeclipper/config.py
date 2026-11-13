"""Runtime configuration, read from environment variables (and an optional .env file).

Every setting has a plain name (e.g. MAX_UPLOAD_SIZE_MB) and the older FC_-prefixed name
(e.g. FC_MAX_UPLOAD_MB); the plain name wins when both are set. See .env.example.
"""
from __future__ import annotations

import os
import secrets
import tempfile
import threading
import time
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
PACKAGE_DIR = Path(__file__).resolve().parent


def _load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip()
        if value[:1] in ('"', "'") and value[1:].find(value[0]) >= 0:
            value = value[1:1 + value[1:].find(value[0])]
        else:
            value = value.split(" #", 1)[0].strip()
        if value:
            os.environ.setdefault(key.strip(), value)


_load_env_file(BASE_DIR / ".env")
# Windows without Developer Mode cannot symlink; the model cache still works, so skip the noisy warning.
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")


def _env(*names: str, default: str | None = None) -> str | None:
    for name in names:
        value = os.getenv(name)
        if value not in (None, ""):
            return value
    return default


def _int(*names: str, default: int) -> int:
    try:
        return int(_env(*names, default=str(default)))
    except ValueError:
        return default


def _bool(*names: str, default: bool) -> bool:
    value = _env(*names)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _path(*names: str, default: Path) -> Path:
    return Path(_env(*names, default=str(default))).resolve()


# ---------------- storage ----------------
# STORAGE_ROOT holds everything that must survive restarts. On Railway it defaults to the attached
# volume's mount path, which Railway exposes as RAILWAY_VOLUME_MOUNT_PATH.
DATA_DIR = _path("STORAGE_ROOT", "FC_DATA_DIR", "RAILWAY_VOLUME_MOUNT_PATH", default=BASE_DIR / "data")
DB_PATH = _path("DATABASE_PATH", default=DATA_DIR / "freeclipper.db")
# One folder per project: its source video, transcript, and every rendered clip (kept together so a
# project can be exported or deleted as a unit).
JOBS_DIR = _path("PROJECTS_DIR", "OUTPUT_DIR", default=DATA_DIR / "jobs")
MODEL_DIR = _path("MODEL_DIR", default=DATA_DIR / "models")
BRAND_DIR = DATA_DIR / "branding"
FONTS_DIR = _path("FC_FONTS_DIR", default=DATA_DIR / "fonts")
# Scratch space for in-flight uploads and ZIP exports. Defaults to the container's own disk (large on
# Railway) rather than the size-limited volume; files here are short-lived and cleaned automatically.
TEMP_DIR = _path("TEMP_DIR", default=Path(tempfile.gettempdir()) / "clipforge")

for _d in (DATA_DIR, DB_PATH.parent, JOBS_DIR, MODEL_DIR, BRAND_DIR, FONTS_DIR, TEMP_DIR):
    _d.mkdir(parents=True, exist_ok=True)
# Make every tempfile (including the web server's upload spooling) use TEMP_DIR.
tempfile.tempdir = str(TEMP_DIR)


def _secret_key() -> str:
    env = _env("SECRET_KEY", "FC_SECRET_KEY")
    if env:
        return env
    path = DATA_DIR / "secret_key"
    if path.exists():
        return path.read_text(encoding="utf-8").strip()
    key = secrets.token_urlsafe(48)
    path.write_text(key, encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return key


SECRET_KEY = _secret_key()
SECRET_KEY_FROM_ENV = bool(_env("SECRET_KEY", "FC_SECRET_KEY"))

# ---------------- web server ----------------
HOST = _env("FC_HOST", "HOST", default="127.0.0.1")
# A host-assigned PORT (Railway, preview tools, PaaS) wins over FC_PORT.
PORT = _int("PORT", "FC_PORT", default=8000)
HTTPS_ONLY_COOKIES = _bool("SESSION_HTTPS_ONLY", "FC_HTTPS_ONLY_COOKIES", default=False)
ALLOW_SIGNUP = _bool("ALLOW_SIGNUP", "FC_ALLOW_SIGNUP", default=False)
# When set, creating the first (admin) account requires this code: protects a fresh public deployment.
SETUP_TOKEN = _env("SETUP_TOKEN", "FC_SETUP_TOKEN", default="") or ""
# Header that carries the real client IP behind a trusted proxy (Railway: X-Real-IP). Leave empty when
# the app is reached directly, otherwise clients could spoof it.
CLIENT_IP_HEADER = (_env("CLIENT_IP_HEADER", default="") or "").strip()
SHUTDOWN_GRACE_SECONDS = _int("SHUTDOWN_GRACE_SECONDS", default=20)

# ---------------- capacity (safe beta defaults; raise them as resources allow) ----------------
# Videos processed at the same time. Never derived from the CPU count: inside containers that reports
# the host's CPUs, not the service's allocation.
MAX_CONCURRENT_JOBS = max(1, _int("MAX_CONCURRENT_JOBS", "FC_MAX_CONCURRENT_JOBS", default=1))
# Clips rendered in parallel within one project (each is one ffmpeg process).
RENDER_WORKERS = max(1, _int("MAX_WORKERS", "FC_RENDER_WORKERS", default=1))
FFMPEG_THREADS = max(0, _int("FFMPEG_THREADS", default=0))  # 0 = ffmpeg decides
MAX_UPLOAD_MB = max(1, _int("MAX_UPLOAD_SIZE_MB", "FC_MAX_UPLOAD_MB", default=500))
MAX_DOWNLOAD_MB = max(50, _int("MAX_DOWNLOAD_SIZE_MB", default=2048))
MAX_VIDEO_SECONDS = max(30, _int("MAX_VIDEO_DURATION", default=1800))
MAX_CLIPS_PER_JOB = max(1, _int("MAX_CLIPS_PER_JOB", default=10))
JOB_TIMEOUT_SECONDS = max(300, _int("JOB_TIMEOUT_SECONDS", default=5400))
MAX_JOB_ATTEMPTS = max(1, _int("MAX_JOB_ATTEMPTS", default=2))
MAX_QUEUED_JOBS = max(1, _int("MAX_QUEUED_JOBS", default=20))
MIN_FREE_DISK_MB = max(0, _int("MIN_FREE_DISK_MB", default=1024))

# ---------------- per-user quotas (admins are exempt) ----------------
USER_MAX_ACTIVE_JOBS = max(1, _int("USER_MAX_ACTIVE_JOBS", default=2))
USER_JOBS_PER_DAY = max(1, _int("USER_JOBS_PER_DAY", default=10))
USER_RENDERS_PER_HOUR = max(1, _int("USER_RENDERS_PER_HOUR", default=30))
USER_MAX_STORAGE_MB = max(100, _int("USER_MAX_STORAGE_MB", default=2048))
SIGNUPS_PER_IP_PER_DAY = max(1, _int("SIGNUPS_PER_IP_PER_DAY", default=5))

# ---------------- cleanup ----------------
CLEANUP_INTERVAL_MINUTES = max(5, _int("CLEANUP_INTERVAL_MINUTES", default=60))
TEMP_FILE_MAX_AGE_HOURS = max(1, _int("TEMP_FILE_MAX_AGE_HOURS", default=6))
# Delete a project's *source* video this many days after processing (0 = keep forever). Rendered clips,
# captions and transcripts are never deleted by cleanup; without the source, clips can't be re-rendered.
SOURCE_RETENTION_DAYS = max(0, _int("SOURCE_RETENTION_DAYS", default=0))

# YouTube sometimes asks cloud servers to "confirm you're not a bot". Two standard yt-dlp workarounds:
# a cookies.txt exported from a signed-in browser (uploaded by an admin in Settings, or a path), and a proxy.
YTDLP_COOKIES_FILE = _path("YTDLP_COOKIES_FILE", default=DATA_DIR / "youtube_cookies.txt")
YTDLP_PROXY = _env("YTDLP_PROXY", default="") or ""

FFMPEG = _env("FC_FFMPEG", default="ffmpeg")
FFPROBE = _env("FC_FFPROBE", default="ffprobe")
X264_PRESET = _env("FC_X264_PRESET", default="veryfast")
X264_CRF = _int("FC_X264_CRF", default=20)

# ---------------- transcription (faster-whisper, runs locally) ----------------
WHISPER_MODEL = _env("WHISPER_MODEL", "FC_WHISPER_MODEL", default="small")
WHISPER_DEVICE = _env("FC_WHISPER_DEVICE", default="auto")  # auto | cpu | cuda
WHISPER_COMPUTE_TYPE = _env("FC_WHISPER_COMPUTE_TYPE", default="auto")  # auto | int8 | float16 | ...
WHISPER_CPU_THREADS = _int("WHISPER_CPU_THREADS", "FC_WHISPER_CPU_THREADS", default=0)
WHISPER_BATCH_SIZE = _int("WHISPER_BATCH_SIZE", "FC_WHISPER_BATCH_SIZE", default=0)  # 0 = auto (8 CPU, 16 GPU)
_ALL_WHISPER_MODELS = [
    "tiny", "base", "small", "medium", "large-v3", "turbo",
    "distil-large-v3", "tiny.en", "base.en", "small.en", "medium.en",
]
# Which models users may pick. Large models need several GB of RAM each; restrict them on small servers.
_allowed = [m.strip() for m in (_env("WHISPER_ALLOWED_MODELS", default="") or "").split(",") if m.strip()]
WHISPER_MODELS = [m for m in _ALL_WHISPER_MODELS if not _allowed or m in _allowed] or ["small"]
if WHISPER_MODEL not in WHISPER_MODELS:
    WHISPER_MODEL = WHISPER_MODELS[0]
# Download the default model at startup so the first job doesn't wait for it.
PRELOAD_WHISPER = _bool("PRELOAD_WHISPER", default=True)

# Face detector model: baked into the Docker image, otherwise downloaded once into MODEL_DIR.
YUNET_MODEL = _path("YUNET_MODEL_PATH", default=MODEL_DIR / "face_detection_yunet_2023mar.onnx")

# ---------------- highlight detection ----------------
AI_PROVIDER = (_env("AI_PROVIDER", "FC_AI_PROVIDER", default="auto") or "auto").lower()  # auto|claude|ollama|heuristic
CLAUDE_MODEL = _env("CLAUDE_MODEL", "FC_CLAUDE_MODEL", default="claude-opus-5-5")
CLAUDE_EFFORT = _env("CLAUDE_EFFORT", "FC_CLAUDE_EFFORT", default="medium")
# Ollama is optional and off unless OLLAMA_URL is set (e.g. http://localhost:11434). Railway doesn't
# provide Ollama, so leaving it unset avoids pointless network probes there.
OLLAMA_URL = (_env("OLLAMA_URL", "FC_OLLAMA_URL", default="") or "").rstrip("/")
OLLAMA_MODEL = _env("OLLAMA_MODEL", "FC_OLLAMA_MODEL", default="qwen2.5:3b")
OLLAMA_NUM_CTX = _int("OLLAMA_NUM_CTX", "FC_OLLAMA_NUM_CTX", default=32768)


def has_claude_credentials() -> bool:
    return bool(os.getenv("ANTHROPIC_API_KEY") or os.getenv("ANTHROPIC_AUTH_TOKEN"))


_ollama_cache: dict[str, float | bool] = {"at": 0.0, "ok": False}
_ollama_lock = threading.Lock()


def ollama_model_available() -> bool:
    """True when the Ollama server is up and the configured model has been pulled (cached for 60 s)."""
    if not OLLAMA_URL:
        return False
    with _ollama_lock:
        if time.monotonic() - float(_ollama_cache["at"]) < 60:
            return bool(_ollama_cache["ok"])
    import httpx

    try:
        resp = httpx.get(f"{OLLAMA_URL}/api/tags", timeout=1.5)
        resp.raise_for_status()
        names = {m.get("name") for m in resp.json().get("models", [])}
        wanted = OLLAMA_MODEL if ":" in OLLAMA_MODEL else f"{OLLAMA_MODEL}:latest"
        ok = wanted in names
    except (httpx.HTTPError, ValueError):
        ok = False
    with _ollama_lock:
        _ollama_cache.update(at=time.monotonic(), ok=ok)
    return ok


def resolve_provider(requested: str | None) -> str:
    """auto = Claude if a key is set, else a local Ollama model if reachable, else the built-in scorer.
    An explicitly chosen provider that isn't usable falls back the same way."""
    choice = (requested or "auto").lower()
    if choice == "auto":
        choice = AI_PROVIDER
    if choice == "claude" and has_claude_credentials():
        return "claude"
    if choice == "ollama" and ollama_model_available():
        return "ollama"
    if choice == "heuristic":
        return "heuristic"
    if has_claude_credentials():
        return "claude"
    return "ollama" if ollama_model_available() else "heuristic"
