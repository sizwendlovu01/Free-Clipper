"""Error monitoring: every failure is stored (admin dashboard) and, optionally, sent to a chat webhook
so the admin hears about it before users report it.

Sources of events:
  * explicit calls to record() (failed projects, failed clips, server errors, low disk);
  * any ERROR logged by the app's own loggers (crashes inside the worker, cleanup, etc.).

Alerts go to ALERT_WEBHOOK_URL (a Discord or Slack incoming webhook, or any URL accepting JSON), are
sent in the background, and are throttled: the same kind of problem alerts at most once per
ALERT_COOLDOWN_MINUTES, with a count of how many more happened meanwhile.
"""
from __future__ import annotations

import logging
import re
import threading
import time
import traceback

from . import config, db

log = logging.getLogger("freeclipper.monitor")
_local = threading.local()
_lock = threading.Lock()
_last_alert: dict[str, float] = {}
_suppressed: dict[str, int] = {}
_sent_times: list[float] = []
MAX_ALERTS_PER_HOUR = 30


def public_url(path: str = "") -> str:
    domain = config.PUBLIC_DOMAIN
    return f"https://{domain}{path}" if domain else path


def _fingerprint(source: str, message: str) -> str:
    """Group similar problems: ids, numbers and paths don't make a new kind of problem."""
    m = re.sub(r"[0-9a-f]{8,}", "#", message.lower())
    m = re.sub(r"\d+", "#", m)
    return f"{source}:{m[:120]}"


def record(level: str, source: str, message: str, details: str = "", job_id: str | None = None,
           user_id: int | None = None, alert: bool = True) -> None:
    """Store an event and send an alert for it (errors only). Never raises."""
    if getattr(_local, "busy", False):  # an error while recording an error: don't loop
        return
    _local.busy = True
    try:
        db.execute("INSERT INTO events(level, source, message, details, job_id, user_id, created_at) "
                   "VALUES(?, ?, ?, ?, ?, ?, ?)",
                   (level, source, message[:1000], (details or "")[-6000:], job_id, user_id, db.now()))
        if alert and level in ("error", "critical") and config.ALERT_WEBHOOK_URL:
            _maybe_alert(level, source, message, job_id)
    except Exception:  # monitoring must never break the app
        pass
    finally:
        _local.busy = False


def _maybe_alert(level: str, source: str, message: str, job_id: str | None) -> None:
    key = _fingerprint(source, message)
    now = time.time()
    with _lock:
        last = _last_alert.get(key, 0)
        if now - last < config.ALERT_COOLDOWN_MINUTES * 60:
            _suppressed[key] = _suppressed.get(key, 0) + 1
            return
        _sent_times[:] = [t for t in _sent_times if now - t < 3600]
        if len(_sent_times) >= MAX_ALERTS_PER_HOUR:
            _suppressed[key] = _suppressed.get(key, 0) + 1
            return
        _last_alert[key] = now
        _sent_times.append(now)
        repeats = _suppressed.pop(key, 0)
    lines = [f"**{config_name()} {level.upper()}** ({source})", message[:900]]
    if repeats:
        lines.append(f"(+{repeats} similar since the last alert)")
    if job_id:
        lines.append(public_url(f"/jobs/{job_id}"))
    lines.append(public_url("/admin") or "Open Admin in the app for details.")
    send_alert("\n".join(lines))


def config_name() -> str:
    try:
        from .main import brand
        return brand().get("app_name") or "ClipForge AI"
    except Exception:
        return "ClipForge AI"


def send_alert(text: str, wait: bool = False) -> bool:
    """POST the text to the webhook in the background. Works with Discord ("content") and Slack
    ("text") incoming webhooks. With wait=True, returns whether it was delivered."""
    url = config.ALERT_WEBHOOK_URL
    if not url:
        return False
    result = {"ok": False}

    def post() -> None:
        try:
            import httpx
            r = httpx.post(url, json={"content": text[:1900], "text": text}, timeout=10)
            result["ok"] = r.status_code < 300
            if not result["ok"]:
                log.warning("alert webhook returned %s", r.status_code)
        except Exception as exc:
            log.warning("alert webhook failed: %s", type(exc).__name__)

    t = threading.Thread(target=post, daemon=True)
    t.start()
    if wait:
        t.join(timeout=12)
    return result["ok"]


class _ErrorLogHandler(logging.Handler):
    """Turns ERROR logs from the app's own code into events (unless already recorded explicitly)."""

    def emit(self, rec: logging.LogRecord) -> None:
        if getattr(rec, "monitored", False) or rec.name.startswith("freeclipper.monitor"):
            return
        try:
            message = rec.getMessage().splitlines()[0][:500]
            details = "".join(traceback.format_exception(*rec.exc_info)) if rec.exc_info else ""
            m = re.search(r"\bjob ([0-9a-f]{16})\b", rec.getMessage())
            level = "critical" if rec.levelno >= logging.CRITICAL else "error"
            source = rec.name.replace("freeclipper.", "") or "app"
            record(level, source, message, details, job_id=m.group(1) if m else None)
        except Exception:
            pass


_installed = False


def install() -> None:
    global _installed
    if _installed:
        return
    handler = _ErrorLogHandler(level=logging.ERROR)
    logging.getLogger("freeclipper").addHandler(handler)
    _installed = True


def prune(days: int = 30) -> int:
    from datetime import datetime, timedelta, timezone
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
    before = db.one("SELECT COUNT(*) AS n FROM events")["n"]
    db.execute("DELETE FROM events WHERE created_at < ?", (cutoff,))
    # Hard cap so a crash loop can't fill the database.
    db.execute("DELETE FROM events WHERE id NOT IN (SELECT id FROM events ORDER BY id DESC LIMIT 5000)")
    return before - db.one("SELECT COUNT(*) AS n FROM events")["n"]
