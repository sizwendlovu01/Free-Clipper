"""Resumable upload test: pieces in any order, several at once, retries, bad pieces, old-style uploads.

    python tests/upload_test.py

Exit code 0 only if every check passed. Uses its own temporary data folder.
"""
from __future__ import annotations

import hashlib
import os
import random
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
TMP = Path(tempfile.mkdtemp(prefix="upload-test-"))
os.environ.update({"STORAGE_ROOT": str(TMP), "SECRET_KEY": "upload-test-secret-0123456789", "UPLOAD_CHUNK_MB": "1",
                   "AI_PROVIDER": "heuristic", "PRELOAD_WHISPER": "false", "SESSION_HTTPS_ONLY": "false"})
for k in ("SETUP_TOKEN", "FC_SETUP_TOKEN", "R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_BUCKET",
          "ALERT_WEBHOOK_URL", "ANTHROPIC_API_KEY", "FC_DATA_DIR", "FC_HTTPS_ONLY_COOKIES"):
    os.environ[k] = ""

from fastapi.testclient import TestClient  # noqa: E402

from freeclipper import config, db, main  # noqa: E402
from freeclipper.worker import job_dir  # noqa: E402

results: list[bool] = []


def check(name: str, ok, detail: str = "") -> None:
    results.append(bool(ok))
    print(("PASS " if ok else "FAIL ") + name + ("" if ok else f"  [{detail}]"))


client = TestClient(main.app)
client.post("/api/setup", json={"username": "owner", "password": "owner-pass-123"})

# A real ~5.5 MB video so the finished upload can become a project.
video = TMP / "in.mp4"
subprocess.run([config.FFMPEG, "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=30:duration=8",
                "-f", "lavfi", "-i", "sine=frequency=440:duration=8", "-c:v", "libx264", "-preset", "ultrafast",
                "-b:v", "5M", "-c:a", "aac", "-shortest", str(video)], check=True)
data = video.read_bytes()
size = len(data)


def start() -> dict:
    r = client.post("/api/uploads", json={"filename": "match.mp4", "size": size, "content_type": "video/mp4"})
    assert r.status_code == 200, r.text
    return r.json()


def put(up: dict, i: int, body: bytes | None = None):
    off = i * up["chunk_size"]
    piece = data[off:off + up["chunk_size"]] if body is None else body
    return client.put(f"/api/uploads/{up['id']}", params={"offset": off}, content=piece,
                      headers={"Content-Type": "application/octet-stream"})


up = start()
n = up["piece_count"]
check("upload starts with piece list and parallel hint",
      n == -(-size // (1024 * 1024)) and up["pieces"] == [] and up["parallel"] == config.UPLOAD_PARALLEL, up)

# Out of order, several at once, with duplicates
order = list(range(n))
random.seed(3)
random.shuffle(order)
order += order[:3]  # a few pieces sent twice (a retry after a lost reply)
with ThreadPoolExecutor(4) as pool:
    codes = list(pool.map(lambda i: put(up, i).status_code, order))
check("pieces accepted in any order and in parallel", all(c == 200 for c in codes), codes)
st = client.get(f"/api/uploads/{up['id']}").json()
check("server counts every piece once", st["received"] == size and st["pieces"] == list(range(n)), st)

r = put(up, 0, data[:100])
check("a cut-off piece is refused", r.status_code == 400, r.text)
r = client.put(f"/api/uploads/{up['id']}", params={"offset": 12345}, content=b"x" * 10)
check("an offset that isn't a piece start is refused", r.status_code == 409, r.text)
r = put(up, 1, data[up["chunk_size"]:up["chunk_size"] * 2] + b"extra")
check("an oversized piece is refused", r.status_code == 413, r.text)
check("bad pieces didn't change the stored file",
      (config.UPLOADS_DIR / up["id"] / "data").read_bytes() == data)

r = client.post("/api/jobs", data={"upload_id": up["id"], "options": "{}"})
check("finished upload becomes a project", r.status_code == 200, r.text)
jid = r.json().get("id")
src = next(job_dir(jid).glob("source.*"), None) if jid else None
check("project's video is byte-for-byte the uploaded file",
      src is not None and hashlib.sha256(src.read_bytes()).digest() == hashlib.sha256(data).digest())
check("upload record removed after use", not db.one("SELECT id FROM uploads WHERE id = ?", (up["id"],)))
if jid:
    client.delete(f"/api/jobs/{jid}")

# Unfinished upload can't become a project; resuming sends only what's missing
up2 = start()
for i in range(0, up2["piece_count"], 2):
    put(up2, i)
r = client.post("/api/jobs", data={"upload_id": up2["id"], "options": "{}"})
check("an unfinished upload can't start a project", r.status_code == 409, r.text)
st = client.get(f"/api/uploads/{up2['id']}").json()
missing = [i for i in range(st["piece_count"]) if i not in st["pieces"]]
check("status lists exactly the missing pieces", missing == list(range(1, st["piece_count"], 2)), st["pieces"])
for i in missing:
    put(up2, i)
r = client.post("/api/jobs", data={"upload_id": up2["id"], "options": "{}"})
check("after sending the missing pieces it completes", r.status_code == 200, r.text)
if r.status_code == 200:
    client.delete(f"/api/jobs/{r.json()['id']}")

# An upload started before this change (in order, no piece list) keeps working
up3 = start()
(config.UPLOADS_DIR / up3["id"] / "pieces").unlink()
part = config.UPLOADS_DIR / up3["id"] / "data"
part.write_bytes(data[:3 * 1024 * 1024])  # three pieces stored the old way
st = client.get(f"/api/uploads/{up3['id']}").json()
check("old in-order upload: its pieces are recognised", st["pieces"] == [0, 1, 2] and st["received"] == 3 * 1024 * 1024, st)
for i in range(3, st["piece_count"]):
    assert put(up3, i).status_code == 200
r = client.post("/api/jobs", data={"upload_id": up3["id"], "options": "{}"})
check("old in-order upload can be finished and used", r.status_code == 200, r.text)
if r.status_code == 200:
    client.delete(f"/api/jobs/{r.json()['id']}")

# Another user can't touch someone's upload
up4 = start()
client.post("/api/users", json={"username": "other", "password": "other-pass-123", "is_admin": False})
other = TestClient(main.app)
other.post("/api/login", json={"username": "other", "password": "other-pass-123"})
r = other.put(f"/api/uploads/{up4['id']}", params={"offset": 0}, content=data[:1024 * 1024])
check("another user can't add to someone's upload", r.status_code == 404, r.text)

print(f"\n{sum(results)}/{len(results)} checks passed")
sys.exit(0 if all(results) else 1)
