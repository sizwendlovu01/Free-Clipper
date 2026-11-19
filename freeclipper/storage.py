"""Optional Cloudflare R2 (S3-compatible) storage for the big files: rendered clips, thumbnails and source
videos. Small files (captions, transcripts, metadata, the database) stay on the server's volume.

Turned on by setting R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY and R2_BUCKET. Without them
everything stays on the volume exactly as before.

Free-tier guard: every stored object is recorded with its size, and nothing is uploaded that would take the
total over R2_MAX_STORAGE_GB (default 9, under R2's 10 GB free allowance). A file that can't go to R2 simply
stays on the volume. Viewers get short-lived private links straight to R2, so playback and downloads don't
pass through (or cost) the server.
"""
from __future__ import annotations

import logging
import threading
from pathlib import Path

from . import config, db

log = logging.getLogger("freeclipper.storage")
_client = None
_client_lock = threading.Lock()
_reserve_lock = threading.Lock()
LINK_SECONDS = 6 * 3600  # private links last 6 hours; pages ask for fresh ones on every load


def enabled() -> bool:
    return bool(config.R2_BUCKET and config.R2_ACCESS_KEY_ID and config.R2_SECRET_ACCESS_KEY
                and (config.R2_ACCOUNT_ID or config.R2_ENDPOINT))


def endpoint() -> str:
    return config.R2_ENDPOINT or f"https://{config.R2_ACCOUNT_ID}.r2.cloudflarestorage.com"


def client():
    global _client
    with _client_lock:
        if _client is None:
            import boto3
            from botocore.config import Config
            _client = boto3.client(
                "s3", endpoint_url=endpoint(), region_name="auto",
                aws_access_key_id=config.R2_ACCESS_KEY_ID, aws_secret_access_key=config.R2_SECRET_ACCESS_KEY,
                config=Config(signature_version="s3v4", retries={"max_attempts": 5, "mode": "standard"},
                              connect_timeout=15, read_timeout=120, s3={"addressing_style": "path"}))
        return _client


def key(job_id: str, rel: str) -> str:
    return f"jobs/{job_id}/{rel}"


def cap_bytes() -> int:
    return int(config.R2_MAX_STORAGE_GB * 1024 ** 3)


def tracked_bytes() -> int:
    """What the app's own records say is stored."""
    return int(db.one("SELECT COALESCE(SUM(size), 0) AS n FROM remote_files")["n"])


def used_bytes() -> int:
    """Usage for every cap decision: the app's records plus anything the last real count of the bucket
    (verify()) found that the records don't cover. Deletions count straight away; unknown files are never
    ignored."""
    measured = db.get_setting("r2_measured", {}) or {}
    return tracked_bytes() + int(measured.get("unrecorded_bytes", 0))


def job_bytes(job_ids: list[str]) -> int:
    if not job_ids:
        return 0
    marks = ",".join("?" * len(job_ids))
    return int(db.one(f"SELECT COALESCE(SUM(size), 0) AS n FROM remote_files WHERE job_id IN ({marks})",
                      tuple(job_ids))["n"])


def is_remote(job_id: str, rel: str) -> bool:
    return db.one("SELECT 1 AS x FROM remote_files WHERE key = ?", (key(job_id, rel),)) is not None


def remote_source(job_id: str) -> str | None:
    """The stored name of a project's source video in R2 (e.g. "source.mp4"), if it's there."""
    row = db.one("SELECT key FROM remote_files WHERE job_id = ? AND key LIKE ?", (job_id, key(job_id, "source.%")))
    return row["key"].rsplit("/", 1)[1] if row else None


def offload(job_id: str, path: Path, rel: str) -> bool:
    """Move a local file to R2 (upload, record, delete the local copy). Returns False, keeping the file
    local, when R2 is off, the free-tier cap would be exceeded, or the upload fails."""
    if not enabled() or not path.exists():
        return False
    size = path.stat().st_size
    k = key(job_id, rel)
    with _reserve_lock:  # two uploads at once can't both squeeze under the cap
        current = used_bytes() - int((db.one("SELECT size FROM remote_files WHERE key = ?", (k,)) or {"size": 0})["size"])
        if current + size > cap_bytes():
            log.warning("R2 free-tier cap reached (%.2f of %.0f GB); keeping %s on the volume",
                        current / 1024 ** 3, config.R2_MAX_STORAGE_GB, rel)
            return False
        db.execute("INSERT OR REPLACE INTO remote_files(key, job_id, size, created_at) VALUES(?, ?, ?, ?)",
                   (k, job_id, size, db.now()))
    try:
        client().upload_file(str(path), config.R2_BUCKET, k)
    except Exception as exc:
        db.execute("DELETE FROM remote_files WHERE key = ?", (k,))
        log.error("R2 upload failed for %s: %s", rel, exc)
        return False
    path.unlink(missing_ok=True)
    return True


def fetch(job_id: str, rel: str, dest: Path) -> bool:
    """Download an R2 file back to the volume (e.g. the source video before a re-render)."""
    if not enabled() or not is_remote(job_id, rel):
        return False
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".download")
    try:
        client().download_file(config.R2_BUCKET, key(job_id, rel), str(tmp))
        tmp.replace(dest)
        return True
    except Exception as exc:
        tmp.unlink(missing_ok=True)
        log.error("R2 download failed for %s: %s", rel, exc)
        return False


def link(job_id: str, rel: str, filename: str | None = None, content_type: str | None = None) -> str:
    """A private link to an R2 file, valid for LINK_SECONDS. With a filename it downloads as that name."""
    params = {"Bucket": config.R2_BUCKET, "Key": key(job_id, rel)}
    if filename:
        params["ResponseContentDisposition"] = f'attachment; filename="{filename}"'
    if content_type:
        params["ResponseContentType"] = content_type
    return client().generate_presigned_url("get_object", Params=params, ExpiresIn=LINK_SECONDS)


def delete(job_id: str, rel_prefix: str = "") -> int:
    """Delete a project's R2 files (all of them, or those under rel_prefix). Returns bytes freed."""
    like = key(job_id, rel_prefix) + "%"
    rows = db.query("SELECT key, size FROM remote_files WHERE job_id = ? AND key LIKE ?", (job_id, like))
    freed = 0
    for r in rows:
        try:
            if enabled():
                client().delete_object(Bucket=config.R2_BUCKET, Key=r["key"])  # deletes are free on R2
            db.execute("DELETE FROM remote_files WHERE key = ?", (r["key"],))
            freed += r["size"]
        except Exception as exc:  # keep the record so a later cleanup retries it
            log.error("R2 delete failed for %s: %s", r["key"], exc)
    return freed


def verify(stray_age_hours: float = 1.0) -> dict:
    """Third free-tier guard: count what the bucket really holds and fix any differences.

    Lists every object (a few requests per run), removes stray objects that no project owns (older than an
    hour, so in-flight uploads are safe), forgets records whose object is gone, and stores the real total,
    which used_bytes() then respects. Runs with each cleanup and from Admin."""
    from datetime import datetime, timedelta, timezone

    if not enabled():
        return {"enabled": False}
    known = {r["key"]: r["size"] for r in db.query("SELECT key, size FROM remote_files")}
    seen, actual, removed_bytes, removed, unrecorded = set(), 0, 0, 0, 0
    cutoff = datetime.now(timezone.utc) - timedelta(hours=stray_age_hours)
    token = None
    while True:
        kwargs = {"Bucket": config.R2_BUCKET, "MaxKeys": 1000}
        if token:
            kwargs["ContinuationToken"] = token
        page = client().list_objects_v2(**kwargs)
        for obj in page.get("Contents", []) or []:
            k, size = obj["Key"], int(obj["Size"])
            if k in known:
                seen.add(k)
                actual += size
                if size != known[k]:  # keep the record honest
                    db.execute("UPDATE remote_files SET size = ? WHERE key = ?", (size, k))
            elif obj["LastModified"] < cutoff:
                client().delete_object(Bucket=config.R2_BUCKET, Key=k)  # stray: nothing in the app uses it
                removed += 1
                removed_bytes += size
            else:
                actual += size  # brand new and not yet recorded: count it to be safe
                unrecorded += size
        if not page.get("IsTruncated"):
            break
        token = page.get("NextContinuationToken")
    missing = [k for k in known if k not in seen]
    for k in missing:  # recorded but not in the bucket (e.g. deleted by hand)
        db.execute("DELETE FROM remote_files WHERE key = ?", (k,))
    result = {"enabled": True, "bytes": actual, "unrecorded_bytes": unrecorded, "checked_at": db.now(),
              "strays_removed": removed,
              "strays_bytes": removed_bytes, "missing_records": len(missing), "cap_bytes": cap_bytes()}
    db.set_setting("r2_measured", result)
    pct = 100 * actual / cap_bytes()
    from . import monitor
    if pct >= 95:
        monitor.record("error", "storage", f"R2 is {pct:.0f}% of the {config.R2_MAX_STORAGE_GB:g} GB free-tier cap "
                       f"({actual / 1024 ** 3:.2f} GB). New videos are refused until space frees up.")
    elif pct >= 80:
        monitor.record("error", "storage", f"R2 is {pct:.0f}% of the {config.R2_MAX_STORAGE_GB:g} GB free-tier cap "
                       f"({actual / 1024 ** 3:.2f} GB).")
    if removed or missing:
        log.warning("R2 check: removed %d stray object(s) (%.1f MB), dropped %d stale record(s)",
                    removed, removed_bytes / 1048576, len(missing))
    return result


def self_test() -> dict:
    """Write, read back through a private link, and delete a small object: proves the settings work."""
    import time
    import httpx

    if not enabled():
        return {"ok": False, "error": "R2 is not configured (set R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, "
                                      "R2_SECRET_ACCESS_KEY and R2_BUCKET)."}
    k = f"_selftest/{int(time.time() * 1000)}.txt"
    body = b"clipforge r2 self-test"
    t0 = time.monotonic()
    try:
        client().put_object(Bucket=config.R2_BUCKET, Key=k, Body=body, ContentType="text/plain")
        url = client().generate_presigned_url(
            "get_object", ExpiresIn=120,
            Params={"Bucket": config.R2_BUCKET, "Key": k,
                    "ResponseContentDisposition": 'attachment; filename="test.txt"'})
        r = httpx.get(url, timeout=20)
        client().delete_object(Bucket=config.R2_BUCKET, Key=k)
        ok = r.status_code == 200 and r.content == body
        return {"ok": ok, "status": r.status_code, "download_name_works": "test.txt" in r.headers.get(
            "content-disposition", ""), "ms": int((time.monotonic() - t0) * 1000), "bucket": config.R2_BUCKET,
            "used_gb": round(used_bytes() / 1024 ** 3, 3), "cap_gb": config.R2_MAX_STORAGE_GB}
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:300]}"}
