"""Runtime configuration, read from environment variables (and an optional .env file)."""
from __future__ import annotations

import os
import secrets
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


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, default))
    except ValueError:
        return default


def _bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


DATA_DIR = Path(os.getenv("FC_DATA_DIR", BASE_DIR / "data")).resolve()
JOBS_DIR = DATA_DIR / "jobs"
BRAND_DIR = DATA_DIR / "branding"
FONTS_DIR = Path(os.getenv("FC_FONTS_DIR", DATA_DIR / "fonts")).resolve()
DB_PATH = DATA_DIR / "freeclipper.db"

for _d in (DATA_DIR, JOBS_DIR, BRAND_DIR, FONTS_DIR):
    _d.mkdir(parents=True, exist_ok=True)


def _secret_key() -> str:
    env = os.getenv("FC_SECRET_KEY")
    if env:
        return env
    path = DATA_DIR / "secret_key"
    if path.exists():
        return path.read_text(encoding="utf-8").strip()
    key = secrets.token_urlsafe(48)
    path.write_text(key, encoding="utf-8")
    return key


SECRET_KEY = _secret_key()
HOST = os.getenv("FC_HOST", "127.0.0.1")
# A host-assigned PORT (preview tools, PaaS) wins over FC_PORT.
PORT = _int("PORT", _int("FC_PORT", 8000))
HTTPS_ONLY_COOKIES = _bool("FC_HTTPS_ONLY_COOKIES", False)
ALLOW_SIGNUP = _bool("FC_ALLOW_SIGNUP", False)
MAX_UPLOAD_MB = _int("FC_MAX_UPLOAD_MB", 8192)

# How many videos are processed at the same time. Everything else waits in the queue.
MAX_CONCURRENT_JOBS = max(1, _int("FC_MAX_CONCURRENT_JOBS", max(1, (os.cpu_count() or 4) // 4)))

FFMPEG = os.getenv("FC_FFMPEG", "ffmpeg")
FFPROBE = os.getenv("FC_FFPROBE", "ffprobe")
X264_PRESET = os.getenv("FC_X264_PRESET", "veryfast")
X264_CRF = _int("FC_X264_CRF", 20)

# Transcription (faster-whisper, runs locally)
WHISPER_MODEL = os.getenv("FC_WHISPER_MODEL", "small")
WHISPER_DEVICE = os.getenv("FC_WHISPER_DEVICE", "auto")  # auto | cpu | cuda
WHISPER_COMPUTE_TYPE = os.getenv("FC_WHISPER_COMPUTE_TYPE", "auto")  # auto | int8 | float16 | ...
WHISPER_CPU_THREADS = _int("FC_WHISPER_CPU_THREADS", 0)
WHISPER_BATCH_SIZE = _int("FC_WHISPER_BATCH_SIZE", 0)  # 0 = auto (8 on CPU, 16 on GPU)
# Clips rendered at the same time within one project.
RENDER_WORKERS = max(1, _int("FC_RENDER_WORKERS", min(3, max(1, (os.cpu_count() or 4) // 4))))
WHISPER_MODELS = [
    "tiny", "base", "small", "medium", "large-v3", "turbo",
    "distil-large-v3", "tiny.en", "base.en", "small.en", "medium.en",
]

# Highlight detection
AI_PROVIDER = os.getenv("FC_AI_PROVIDER", "auto")  # auto | claude | ollama | heuristic
CLAUDE_MODEL = os.getenv("FC_CLAUDE_MODEL", "claude-opus-5-5")
CLAUDE_EFFORT = os.getenv("FC_CLAUDE_EFFORT", "medium")
OLLAMA_URL = os.getenv("FC_OLLAMA_URL", "http://localhost:11434").rstrip("/")
OLLAMA_MODEL = os.getenv("FC_OLLAMA_MODEL", "qwen2.5:3b")
OLLAMA_NUM_CTX = _int("FC_OLLAMA_NUM_CTX", 32768)


def has_claude_credentials() -> bool:
    return bool(os.getenv("ANTHROPIC_API_KEY") or os.getenv("ANTHROPIC_AUTH_TOKEN"))


def ollama_model_available() -> bool:
    """True when the Ollama server is up and the configured model has been pulled."""
    import httpx

    try:
        resp = httpx.get(f"{OLLAMA_URL}/api/tags", timeout=1.5)
        resp.raise_for_status()
        names = {m.get("name") for m in resp.json().get("models", [])}
    except (httpx.HTTPError, ValueError):
        return False
    wanted = OLLAMA_MODEL if ":" in OLLAMA_MODEL else f"{OLLAMA_MODEL}:latest"
    return wanted in names


def resolve_provider(requested: str | None) -> str:
    """auto = Claude if a key is set, else a local Ollama model if running, else the heuristic."""
    choice = (requested or "auto").lower()
    if choice == "auto":
        choice = (AI_PROVIDER or "auto").lower()
    if choice == "auto":
        if has_claude_credentials():
            return "claude"
        return "ollama" if ollama_model_available() else "heuristic"
    return choice
