"""End-to-end smoke test for a running ClipForge AI server (local container or a Railway deployment).

Drives the real HTTP API: health, setup/login/logout/sessions, signup and user isolation, an upload job,
an optional link job, every export format, failure handling, quotas, security headers, backup and cleanup.

    python tests/e2e_smoke.py --base http://127.0.0.1:8790 --video sample.mp4 \
        --admin-user admin --admin-pass 'secret123' [--setup-token CODE] [--url https://youtu.be/...]

Restart-recovery check (run in two steps around a server restart):
    python tests/e2e_smoke.py ... --phase start-job      # queues a job and saves its id
    (restart the server while it is processing)
    python tests/e2e_smoke.py ... --phase verify-resume  # the job must resume and finish

Exit code 0 only if every executed check passed. Uses small inputs; never run it with large files.
"""
from __future__ import annotations

import argparse
import io
import json
import sys
import time
import uuid
import zipfile
from pathlib import Path

import httpx

RESULTS: list[tuple[str, bool, str]] = []
STATE_FILE = Path(__file__).with_name(".e2e_state.json")


def check(name: str, ok: bool, detail: str = "") -> bool:
    RESULTS.append((name, bool(ok), detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}{(' - ' + detail) if detail else ''}", flush=True)
    return bool(ok)


def client(base: str) -> httpx.Client:
    return httpx.Client(base_url=base, timeout=httpx.Timeout(120.0, connect=15.0), follow_redirects=False)


def sign_in(c: httpx.Client, user: str, password: str, setup_token: str | None) -> bool:
    r = c.post("/api/login", json={"username": user, "password": password})
    if r.status_code == 200:
        return True
    body = {"username": user, "password": password}
    if setup_token:
        body["setup_token"] = setup_token
    r = c.post("/api/setup", json=body)
    return r.status_code == 200


def wait_job(c: httpx.Client, job_id: str, timeout: int = 1500) -> dict:
    end = time.time() + timeout
    last_stage = ""
    while time.time() < end:
        job = c.get(f"/api/jobs/{job_id}").json()
        if job["stage"] != last_stage:
            print(f"       {job['status']:>9} {job['progress'] * 100:5.1f}%  {job['stage']}", flush=True)
            last_stage = job["stage"]
        if job["status"] in ("done", "failed", "cancelled"):
            return job
        time.sleep(3)
    return c.get(f"/api/jobs/{job_id}").json()


def upload(c: httpx.Client, video: Path, options: dict) -> httpx.Response:
    with video.open("rb") as f:
        return c.post("/api/jobs", files={"file": (video.name, f, "video/mp4")},
                      data={"options": json.dumps(options)})


def test_exports(c: httpx.Client, job: dict) -> None:
    clip = next((x for x in job["clips"] if x["status"] == "done"), None)
    if not check("at least one clip rendered", clip is not None, f"{len(job['clips'])} clips"):
        return
    r = c.get(clip["files"]["video"] + "&download=1")
    check("MP4 download", r.status_code == 200 and r.content[4:8] == b"ftyp" and len(r.content) > 10_000,
          f"{len(r.content)} bytes")
    r = c.get(clip["files"]["video"], headers={"Range": "bytes=0-99"})
    check("MP4 range request (video seeking)", r.status_code == 206 and len(r.content) == 100)
    r = c.get(clip["files"]["srt"])
    check("SRT export", r.status_code == 200 and "-->" in r.text)
    r = c.get(clip["files"]["vtt"])
    check("WebVTT export", r.status_code == 200 and r.text.startswith("WEBVTT"))
    r = c.get(clip["files"]["ass"])
    check("ASS export", r.status_code == 200 and "[Script Info]" in r.text)
    r = c.get(clip["files"]["meta"])
    check("JSON metadata export", r.status_code == 200 and r.json().get("width") in (1080, 1920))
    r = c.get(clip["files"]["thumb"])
    check("thumbnail", r.status_code == 200 and r.content[:2] == b"\xff\xd8")
    for fmt, marker in (("srt", "-->"), ("vtt", "WEBVTT"), ("txt", ""), ("json", "segments")):
        r = c.get(f"/api/jobs/{job['id']}/transcript.{fmt}")
        check(f"transcript .{fmt}", r.status_code == 200 and marker in r.text)
    r = c.get(f"/api/jobs/{job['id']}/export.zip")
    ok = r.status_code == 200
    names = zipfile.ZipFile(io.BytesIO(r.content)).namelist() if ok else []
    check("ZIP export", ok and "clips.json" in names and any(n.endswith("clip.mp4") for n in names),
          f"{len(names)} files")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", required=True)
    ap.add_argument("--video", type=Path, required=True, help="short test video with speech (<2 min)")
    ap.add_argument("--admin-user", required=True)
    ap.add_argument("--admin-pass", required=True)
    ap.add_argument("--setup-token")
    ap.add_argument("--url", help="optional short public video link to test link downloads")
    ap.add_argument("--whisper-model", default="tiny")
    ap.add_argument("--phase", choices=["full", "start-job", "verify-resume"], default="full")
    ap.add_argument("--expect", choices=["done", "failed"], help="verify-resume: expected final status")
    args = ap.parse_args()
    base = args.base.rstrip("/")
    opts = {"max_clips": 2, "min_len": 15, "max_len": 30, "whisper_model": args.whisper_model,
            "provider": "heuristic", "reframe": "auto"}

    admin = client(base)
    if args.phase == "start-job":
        check("admin sign-in", sign_in(admin, args.admin_user, args.admin_pass, args.setup_token))
        r = upload(admin, args.video, {**opts, "whisper_model": "base"})
        check("queue job for restart test", r.status_code == 200, r.text[:200])
        if r.status_code == 200:
            STATE_FILE.write_text(json.dumps({"job": r.json()["id"], "cookies": dict(admin.cookies)}))
            for _ in range(60):
                if admin.get(f"/api/jobs/{r.json()['id']}").json()["status"] == "running":
                    break
                time.sleep(1)
            check("job is running before restart", admin.get(f"/api/jobs/{r.json()['id']}").json()["status"] == "running")
        return summary()
    if args.phase == "verify-resume":
        state = json.loads(STATE_FILE.read_text())
        for name, value in state.get("cookies", {}).items():
            admin.cookies.set(name, value)
        for _ in range(60):  # wait for the server to come back
            try:
                if admin.get("/health").status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            time.sleep(2)
        check("session survives restart (same cookie)", admin.get("/api/me").status_code == 200)
        job = wait_job(admin, state["job"])
        expect = args.expect or "done"
        check(f"interrupted job ended as '{expect}'", job["status"] == expect,
              f"{job['stage']} {job.get('error') or ''}")
        check("project data persisted across restart", len(admin.get("/api/jobs").json()) >= 1)
        STATE_FILE.unlink(missing_ok=True)
        return summary()

    # ---- health and headers ----
    r = admin.get("/health")
    check("GET /health", r.status_code == 200 and r.json().get("ok") is True, r.text[:100])
    check("security headers", r.headers.get("x-content-type-options") == "nosniff"
          and r.headers.get("x-frame-options") == "DENY")
    check("pages require sign-in", admin.get("/").status_code in (302, 303, 307))
    check("API requires sign-in", admin.get("/api/jobs").status_code == 401)

    # ---- auth ----
    check("admin setup or login", sign_in(admin, args.admin_user, args.admin_pass, args.setup_token))
    me = admin.get("/api/me")
    check("session works", me.status_code == 200 and me.json()["is_admin"], me.text[:100])
    admin.post("/logout")
    check("logout ends the session", admin.get("/api/me").status_code == 401)
    check("wrong password rejected", admin.post("/api/login", json={
        "username": args.admin_user, "password": "definitely-wrong"}).status_code == 401)
    check("login again", admin.post("/api/login", json={
        "username": args.admin_user, "password": args.admin_pass}).status_code == 200)

    # ---- a second (normal) user for isolation and quotas ----
    user = client(base)
    uname, upass = f"tester{uuid.uuid4().hex[:6]}", "tester-pass-123"
    r = user.post("/api/signup", json={"username": uname, "password": upass})
    if r.status_code == 403:  # sign-up disabled: the admin creates the account
        admin.post("/api/users", json={"username": uname, "password": upass})
        r = user.post("/api/login", json={"username": uname, "password": upass})
    check("second user can sign in", r.status_code == 200, r.text[:120])

    # ---- failure handling ----
    check("invalid link rejected", admin.post("/api/jobs", data={"url": "not a link"}).status_code == 400)
    r = admin.post("/api/jobs", data={"url": "http://127.0.0.1:8000/secret"})
    check("internal-address link rejected (SSRF)", r.status_code == 400, r.text[:100])
    r = admin.post("/api/jobs", data={"url": "http://169.254.169.254/latest/meta-data"})
    check("cloud-metadata link rejected (SSRF)", r.status_code == 400)
    r = admin.post("/api/jobs", files={"file": ("fake.mp4", b"this is not a video " * 100, "video/mp4")},
                   data={"options": "{}"})
    check("non-media upload rejected", r.status_code == 400, r.text[:100])
    playlist = b"#EXTM3U\n#EXT-X-TARGETDURATION:1\n#EXTINF:1,\nfile:///etc/passwd\n#EXT-X-ENDLIST\n"
    r = admin.post("/api/jobs", files={"file": ("evil.mp4", playlist, "video/mp4")}, data={"options": "{}"})
    check("disguised playlist upload rejected", r.status_code == 400, r.text[:100])
    sysinfo = admin.get("/api/system").json()
    limit_mb = sysinfo["limits"]["max_upload_mb"]
    if limit_mb <= 100:  # only test with a small configured limit; never send huge files
        big = b"\0" * ((limit_mb + 2) * 1024 * 1024)
        r = admin.post("/api/jobs", files={"file": ("big.mp4", big, "video/mp4")}, data={"options": "{}"})
        check("oversized upload rejected", r.status_code == 413, f"limit {limit_mb} MB")
    else:
        print(f"[SKIP] oversized upload (MAX_UPLOAD_SIZE_MB={limit_mb} is too large to test quickly)")
    check("non-admin can't see server paths", "data_dir" not in user.get("/api/system").json())

    # ---- upload job: transcription, highlights, rendering, captions ----
    r = upload(admin, args.video, opts)
    check("upload accepted", r.status_code == 200, r.text[:200])
    job = wait_job(admin, r.json()["id"]) if r.status_code == 200 else {}
    if check("upload job finished", job.get("status") == "done", f"{job.get('stage')} {job.get('error') or ''}"):
        check("transcription produced a language", bool(job.get("language")), job.get("language") or "")
        test_exports(admin, job)
        check("other users can't read this project", user.get(f"/api/jobs/{job['id']}").status_code == 404)
        clip = job["clips"][0]
        check("other users can't download its clips", user.get(clip["files"]["video"]).status_code == 404)
        r = admin.patch(f"/api/clips/{clip['id']}", json={"rerender": True, "title": "Edited"})
        check("clip edit queues a re-render", r.status_code == 200 and r.json()["status"] == "queued")
        end = time.time() + 600
        while time.time() < end:
            c = next(x for x in admin.get(f"/api/jobs/{job['id']}").json()["clips"] if x["id"] == clip["id"])
            if c["status"] in ("done", "failed"):
                break
            time.sleep(3)
        check("re-render finished", c["status"] == "done", c.get("error") or "")

    # ---- per-user quota: active jobs ----
    quota = user.get("/api/system").json()["limits"]
    first = upload(user, args.video, opts)
    extra = [upload(user, args.video, opts) for _ in range(quota["user_max_active_jobs"])]
    blocked = [r for r in extra if r.status_code == 429]
    check("per-user active-job quota enforced", first.status_code == 200 and len(blocked) >= 1,
          f"limit {quota['user_max_active_jobs']}")
    for r in [first, *extra]:
        if r.status_code == 200:
            user.post(f"/api/jobs/{r.json()['id']}/cancel")

    # ---- link job ----
    if args.url:
        r = admin.post("/api/jobs", data={"url": args.url, "options": json.dumps({**opts, "max_clips": 1,
                                                                                    "min_len": 5})})
        check("link accepted", r.status_code == 200, r.text[:200])
        if r.status_code == 200:
            job = wait_job(admin, r.json()["id"])
            check("link job finished (download + transcript + render)", job["status"] == "done",
                  f"{job['stage']} {job.get('error') or ''}")

    # ---- operations ----
    r = admin.get("/api/admin/backup")
    check("database backup download", r.status_code == 200 and r.content[:15] == b"SQLite format 3")
    check("non-admin can't download backup", user.get("/api/admin/backup").status_code == 403)
    r = admin.post("/api/admin/cleanup")
    check("cleanup runs", r.status_code == 200 and "disk_free_mb" in r.json(), r.text[:200])
    return summary()


def summary() -> int:
    failed = [n for n, ok, _ in RESULTS if not ok]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} checks passed")
    for name in failed:
        print(f"  FAILED: {name}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
