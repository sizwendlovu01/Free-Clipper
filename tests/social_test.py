"""Auto-posting test against fake TikTok, Instagram and YouTube servers (no real accounts or network).

Connects accounts through the real sign-in routes, queues posts through the API, runs the posting worker
and checks what each "platform" received: the right endpoints, headers, upload bytes and settings, plus
token renewal, daily limits, retries, failures and account isolation.

    python tests/social_test.py

Exit code 0 only if every check passed.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
TMP = Path(tempfile.mkdtemp(prefix="social-test-"))
os.environ.update({
    "STORAGE_ROOT": str(TMP), "SECRET_KEY": "social-test-secret-0123456789", "PUBLIC_DOMAIN": "clips.example.test",
    "TIKTOK_CLIENT_KEY": "tt-key", "TIKTOK_CLIENT_SECRET": "tt-secret", "INSTAGRAM_APP_ID": "ig-app",
    "INSTAGRAM_APP_SECRET": "ig-secret", "GOOGLE_CLIENT_ID": "g-id", "GOOGLE_CLIENT_SECRET": "g-secret",
    "AI_PROVIDER": "heuristic", "PRELOAD_WHISPER": "false", "SESSION_HTTPS_ONLY": "false",
})
# Empty values (not missing ones) so nothing from the project's .env file is filled in: no setup code,
# no real storage keys, no alerts.
for k in ("SETUP_TOKEN", "FC_SETUP_TOKEN", "TIKTOK_AUDITED", "R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY",
          "R2_BUCKET", "ALERT_WEBHOOK_URL", "ANTHROPIC_API_KEY", "FC_DATA_DIR", "FC_HTTPS_ONLY_COOKIES"):
    os.environ[k] = ""

import httpx  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from freeclipper import config, db, main, social  # noqa: E402
from freeclipper.worker import job_dir  # noqa: E402

results: list[tuple[bool, str]] = []


def check(name: str, ok, detail: str = "") -> None:
    results.append((bool(ok), name))
    print(("PASS " if ok else "FAIL ") + name + ("" if ok else f"  [{detail}]"))


# ---------------- fake platforms ----------------

class Fake:
    def __init__(self):
        self.calls: list[str] = []
        self.tt_uploads: dict[str, bytearray] = {}
        self.tt_status_polls: dict[str, int] = {}
        self.tt_init_bodies: list[dict] = []
        self.ig_containers: dict[str, int] = {}
        self.ig_fetched: list[int] = []
        self.ig_bodies: list[dict] = []
        self.yt_meta: list[dict] = []
        self.yt_bytes: list[int] = []
        self.refreshes: Counter = Counter()
        self.spam_tokens: set[str] = set()
        self.ig_fail_create = 0
        self.google_refresh_ok = True
        self.revoked: list[str] = []

    def __call__(self, req: httpx.Request) -> httpx.Response:
        host, path = req.url.host, req.url.path
        self.calls.append(f"{req.method} {host}{path}")
        form = dict(parse_qs(req.content.decode(errors="ignore"))) if req.headers.get("content-type", "").startswith(
            "application/x-www-form-urlencoded") else {}
        form = {k: v[0] for k, v in form.items()}
        q = dict(req.url.params)
        auth = req.headers.get("authorization", "")[7:]
        # ---- TikTok ----
        if host == "open.tiktokapis.com":
            if path == "/v2/oauth/token/":
                if form.get("client_key") != "tt-key" or form.get("client_secret") != "tt-secret":
                    return httpx.Response(401, json={"error": "invalid_client", "error_description": "bad client"})
                if form["grant_type"] == "authorization_code":
                    n = form["code"].rsplit("-", 1)[1]
                    return httpx.Response(200, json={"access_token": f"tt-access-{n}", "refresh_token": f"tt-refresh-{n}",
                                                     "open_id": f"open-{n}", "expires_in": 7200,
                                                     "refresh_expires_in": 15552000, "token_type": "Bearer",
                                                     "scope": "user.info.basic,video.publish"})
                n = form["refresh_token"].split("-")[2]
                self.refreshes["tiktok"] += 1
                r = self.refreshes["tiktok"]
                return httpx.Response(200, json={"access_token": f"tt-access-{n}-r{r}", "refresh_token": f"tt-refresh-{n}-r{r}",
                                                 "open_id": f"open-{n}", "expires_in": 7200,
                                                 "refresh_expires_in": 15552000, "scope": "user.info.basic,video.publish"})
            if path == "/v2/user/info/":
                n = auth.split("-")[2]
                return httpx.Response(200, json={"data": {"user": {"open_id": f"open-{n}", "display_name": f"Creator {n}",
                                                                   "avatar_url": f"https://cdn.example/{n}.jpg"}},
                                                 "error": {"code": "ok"}})
            if not auth.startswith("tt-access-"):
                return httpx.Response(401, json={"error": {"code": "access_token_invalid", "message": "bad"}})
            if path == "/v2/post/publish/creator_info/query/":
                if auth in self.spam_tokens:
                    return httpx.Response(200, json={"data": {}, "error": {"code": "spam_risk_too_many_posts", "message": "x"}})
                n = auth.split("-")[2]
                return httpx.Response(200, json={"data": {
                    "creator_username": f"tt_user_{n}", "creator_nickname": f"Creator {n}", "creator_avatar_url": "",
                    "privacy_level_options": ["PUBLIC_TO_EVERYONE", "MUTUAL_FOLLOW_FRIENDS", "SELF_ONLY"],
                    "comment_disabled": False, "duet_disabled": True, "stitch_disabled": False,
                    "max_video_post_duration_sec": 600}, "error": {"code": "ok"}})
            if path == "/v2/post/publish/video/init/":
                body = json.loads(req.content)
                self.tt_init_bodies.append(body)
                pid = f"pub-{len(self.tt_init_bodies)}"
                self.tt_uploads[pid] = bytearray()
                self.tt_expected = body["source_info"]["video_size"]
                return httpx.Response(200, json={"data": {"publish_id": pid,
                                                          "upload_url": f"https://open-upload.tiktokapis.com/upload/?pid={pid}"},
                                                 "error": {"code": "ok"}})
            if path == "/v2/post/publish/status/fetch/":
                pid = json.loads(req.content)["publish_id"]
                self.tt_status_polls[pid] = self.tt_status_polls.get(pid, 0) + 1
                if self.tt_status_polls[pid] < 2:
                    return httpx.Response(200, json={"data": {"status": "PROCESSING_UPLOAD"}, "error": {"code": "ok"}})
                return httpx.Response(200, json={"data": {"status": "PUBLISH_COMPLETE",
                                                          "publicaly_available_post_id": [7300000000000000001]},
                                                 "error": {"code": "ok"}})
        if host == "open-upload.tiktokapis.com":
            pid = q["pid"]
            first, rest = req.headers["content-range"][6:].split("-")
            last, total = rest.split("/")
            ok = (int(first) == len(self.tt_uploads[pid]) and int(last) - int(first) + 1 == len(req.content)
                  == int(req.headers["content-length"]) and req.headers["content-type"] == "video/mp4")
            if not ok:
                return httpx.Response(416)
            self.tt_uploads[pid] += req.content
            return httpx.Response(201 if len(self.tt_uploads[pid]) == int(total) else 206)
        # ---- Instagram ----
        if host == "api.instagram.com" and path == "/oauth/access_token":
            return httpx.Response(200, json={"data": [{"access_token": "ig-short", "user_id": "17841400000000001",
                                                       "permissions": "instagram_business_basic,instagram_business_content_publish"}]})
        if host == "graph.instagram.com":
            if path == "/access_token":
                return httpx.Response(200, json={"access_token": "ig-long", "token_type": "bearer", "expires_in": 5183944})
            if path == "/refresh_access_token":
                self.refreshes["instagram"] += 1
                return httpx.Response(200, json={"access_token": "ig-long", "token_type": "bearer", "expires_in": 5183944})
            token = q.get("access_token") or form.get("access_token")
            if token != "ig-long":
                return httpx.Response(400, json={"error": {"message": "Invalid OAuth access token", "code": 190}})
            if path.endswith("/me"):
                return httpx.Response(200, json={"user_id": "17841400000000001", "username": "clips.daily",
                                                 "name": "Clips Daily", "profile_picture_url": ""})
            if path.endswith("/17841400000000001/media"):
                if self.ig_fail_create:
                    self.ig_fail_create -= 1
                    return httpx.Response(500, json={"error": {"message": "Service temporarily unavailable", "code": 2,
                                                               "is_transient": True}})
                self.ig_bodies.append(form)
                # Instagram downloads the video itself: fetch it the way Meta's servers would.
                u = urlparse(form["video_url"])
                got = client.get(u.path)
                self.ig_fetched.append(len(got.content) if got.status_code == 200 else -got.status_code)
                cid = f"cont-{len(self.ig_bodies)}"
                self.ig_containers[cid] = 0
                return httpx.Response(200, json={"id": cid})
            cid = path.rsplit("/", 1)[1]
            if req.method == "GET" and cid in self.ig_containers:
                self.ig_containers[cid] += 1
                return httpx.Response(200, json={"status_code": "FINISHED" if self.ig_containers[cid] > 1 else "IN_PROGRESS",
                                                 "id": cid})
            if path.endswith("/media_publish"):
                return httpx.Response(200, json={"id": "media-" + form["creation_id"]})
            if req.method == "GET" and cid.startswith("media-"):
                return httpx.Response(200, json={"permalink": f"https://www.instagram.com/reel/{cid}/", "id": cid})
        # ---- Google / YouTube ----
        if host == "oauth2.googleapis.com":
            if path == "/token":
                if form["grant_type"] == "authorization_code":
                    return httpx.Response(200, json={"access_token": "g-access", "refresh_token": "g-refresh", "expires_in": 3599,
                                                     "scope": social.SCOPES["youtube"], "token_type": "Bearer"})
                self.refreshes["youtube"] += 1
                if not self.google_refresh_ok:
                    return httpx.Response(400, json={"error": "invalid_grant", "error_description": "Token has been expired or revoked."})
                return httpx.Response(200, json={"access_token": "g-access", "expires_in": 3599, "scope": social.SCOPES["youtube"]})
            if path == "/revoke":
                self.revoked.append(q.get("token"))
                return httpx.Response(200)
        if host == "www.googleapis.com":
            if auth != "g-access":
                return httpx.Response(401, json={"error": {"code": 401, "message": "Invalid Credentials"}})
            if path == "/youtube/v3/channels":
                return httpx.Response(200, json={"items": [{"id": "UC123", "snippet": {"title": "My Channel", "customUrl": "@mychannel",
                                                                                        "thumbnails": {"default": {"url": ""}}}}]})
            if path == "/upload/youtube/v3/videos" and req.method == "POST":
                self.yt_meta.append({"meta": json.loads(req.content), "length": req.headers.get("x-upload-content-length"),
                                     "type": req.headers.get("x-upload-content-type"), "params": q})
                return httpx.Response(200, headers={"Location": "https://www.googleapis.com/upload/youtube/v3/videos?upload_id=up1"})
            if path == "/upload/youtube/v3/videos" and req.method == "PUT":
                body = req.read()
                self.yt_bytes.append(len(body))
                return httpx.Response(200, json={"id": f"ytvid{len(self.yt_bytes)}", "kind": "youtube#video"})
        return httpx.Response(404, json={"error": f"fake platform: no route for {req.method} {host}{path}"})


from collections import Counter  # noqa: E402

fake = Fake()
social.transport = httpx.MockTransport(fake)
social.pause = lambda _s: None
client = TestClient(main.app)


def make_clip(job_id: str, clip_id: str, idx: int, user_id: int) -> Path:
    if not db.one("SELECT id FROM jobs WHERE id = ?", (job_id,)):
        db.execute("INSERT INTO jobs(id, user_id, title, source_type, source, status, created_at, updated_at, language) "
                   "VALUES(?, ?, 'Test video', 'upload', 'x.mp4', 'done', ?, ?, 'en')", (job_id, user_id, db.now(), db.now()))
        (job_dir(job_id)).mkdir(parents=True, exist_ok=True)
        (job_dir(job_id) / "transcript.json").write_text(json.dumps({"language": "en", "segments": [
            {"start": 0, "end": 4, "text": "Nobody tells you the real reason most people never save money."},
            {"start": 4, "end": 8, "text": "Here is the one habit that changed everything for me, and it takes five minutes."},
            {"start": 8, "end": 12, "text": "Stop checking your balance every day and automate it instead."}]}), encoding="utf-8")
    db.execute("INSERT INTO clips(id, job_id, idx, start, \"end\", title, hashtags, status, created_at, updated_at, score) "
               "VALUES(?, ?, ?, 0, 12, 'The Real Reason You NEVER Save Money', ?, 'done', ?, ?, 80)",
               (clip_id, job_id, idx, json.dumps(["#money", "#saving", "#habits", "#fyp", "#viral"]), db.now(), db.now()))
    out = job_dir(job_id) / "clips" / clip_id / "clip.mp4"
    out.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run([config.FFMPEG, "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc=size=360x640:rate=30", "-f", "lavfi",
                    "-i", "sine=frequency=440", "-t", "3", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
                    "-shortest", str(out)], check=True)
    return out


def connect(platform: str, code: str) -> httpx.Response:
    r = client.get(f"/oauth/{platform}/start", follow_redirects=False)
    loc = urlparse(r.headers["location"])
    state = parse_qs(loc.query)["state"][0]
    return client.get(f"/oauth/{platform}/callback", params={"code": code, "state": state}, follow_redirects=False)


def run_queue() -> int:
    n = 0
    while social.process_next():
        n += 1
    return n


def due_now() -> None:
    db.execute("UPDATE posts SET scheduled_at = ? WHERE status = 'queued'", (db.now(),))


# ---------------- tests ----------------

r = client.post("/api/setup", json={"username": "owner", "password": "owner-pass-123"})
check("admin created", r.status_code == 200, r.text)
uid = db.one("SELECT id FROM users WHERE username = 'owner'")["id"]

d = client.get("/api/social/accounts").json()
check("all three platforms show as set up", all(p["configured"] for p in d["platforms"]), d)
check("admin sees the redirect URLs to register",
      d["redirects"].get("tiktok") == "https://clips.example.test/oauth/tiktok/callback", d["redirects"])

r = client.get("/oauth/tiktok/start", follow_redirects=False)
loc = urlparse(r.headers["location"])
qs = parse_qs(loc.query)
check("TikTok sign-in goes to TikTok with the right details",
      r.status_code == 303 and loc.netloc == "www.tiktok.com" and qs["client_key"] == ["tt-key"]
      and qs["scope"] == ["user.info.basic,video.publish"]
      and qs["redirect_uri"] == ["https://clips.example.test/oauth/tiktok/callback"], r.headers["location"])
r = client.get("/oauth/tiktok/callback", params={"code": "tt-code-9", "state": "forged"}, follow_redirects=False)
check("a forged sign-in is refused", r.status_code == 303 and not db.query("SELECT id FROM social_accounts"))

for platform, code in (("tiktok", "tt-code-1"), ("tiktok", "tt-code-2"), ("instagram", "ig-code"), ("youtube", "g-code")):
    r = connect(platform, code)
    check(f"connect {platform} ({code})", r.status_code == 303 and r.headers["location"] == "/settings#accounts", r.text)
page = client.get("/settings")
check("settings page loads after connecting", page.status_code == 200 and "Connected accounts" in page.text)

accts = client.get("/api/social/accounts").json()["accounts"]
by = {(a["platform"], a["display_name"]): a for a in accts}
check("four accounts connected (two TikTok)", len(accts) == 4 and sum(a["platform"] == "tiktok" for a in accts) == 2,
      [(a["platform"], a["display_name"]) for a in accts])
row = db.one("SELECT access_token, refresh_token FROM social_accounts WHERE platform = 'tiktok' LIMIT 1")
check("account keys are stored encrypted", "tt-access" not in row["access_token"] and "tt-refresh" not in row["refresh_token"])
check("API never returns keys", "token" not in json.dumps(accts).replace("posts_today", ""))
r = connect("tiktok", "tt-code-1")
check("reconnecting the same account doesn't duplicate it",
      db.one("SELECT COUNT(*) AS n FROM social_accounts")["n"] == 4)

tt1, tt2 = by[("tiktok", "Creator 1")]["id"], by[("tiktok", "Creator 2")]["id"]
ig, yt = by[("instagram", "Clips Daily")]["id"], by[("youtube", "My Channel")]["id"]

clip_file = make_clip("job1", "clipA", 0, uid)
make_clip("job1", "clipB", 1, uid)

v = client.get("/api/clips/clipA/post-text", params={"accounts": f"{tt1},{tt2},{ig},{yt}"}).json()["variants"]
check("each account gets its own caption", len({x["caption"] for x in v}) == 4, [x["caption"] for x in v])
check("captions get platform hashtags",
      "#shorts" in v[3]["caption"] and "#reels" in v[2]["caption"] and "#fyp" in v[0]["caption"], v)

info = client.get(f"/api/social/accounts/{tt1}/tiktok").json()
check("TikTok post screen: nickname shown, only private until audited",
      info["nickname"] == "Creator 1" and info["privacy_options"] == ["SELF_ONLY"] and info["duet_disabled"], info)


def tt(acc, **kw):
    return {"account_id": acc, "caption": "Watch this #money", "privacy": "SELF_ONLY", "consent": True, **kw}


bad = [
    ([tt(tt1, privacy="")], "no privacy choice"),
    ([tt(tt1, privacy="PUBLIC_TO_EVERYONE")], "public before TikTok approves the app"),
    ([tt(tt1, commercial=True)], "commercial content without saying whose"),
    ([tt(tt1, consent=False)], "no music consent"),
    ([{"account_id": yt, "caption": "x", "title": ""}], "YouTube without a title"),
    ([{"account_id": 999, "caption": "x"}], "someone else's account"),
    ([], "no accounts"),
]
for targets, why in bad:
    r = client.post("/api/clips/clipA/posts", json={"targets": targets})
    check(f"refused: {why}", r.status_code == 400, r.text)

r = client.post("/api/clips/clipA/posts", json={"spacing_minutes": 0, "targets": [
    tt(tt1, allow_comment=True, allow_duet=True),
    tt(tt2),
    {"account_id": ig, "caption": v[2]["caption"]},
    {"account_id": yt, "caption": v[3]["caption"], "title": v[3]["title"], "privacy": "unlisted"},
]})
check("queue a clip to four accounts", r.status_code == 200 and len(r.json()["posts"]) == 4, r.text)
r = client.post("/api/clips/clipA/posts", json={"targets": [tt(tt1)]})
check("the same clip can't be queued twice to one account", r.status_code == 400, r.text)

n = run_queue()
posts = {p["account_id"]: p for p in client.get("/api/posts", params={"job_id": "job1"}).json()["posts"]}
check("posting worker sent all four", n == 4 and all(p["status"] == "posted" for p in posts.values()),
      {k: (p["status"], p["error"]) for k, p in posts.items()})
size = clip_file.stat().st_size
check("TikTok received the whole video in order", [len(b) for b in fake.tt_uploads.values()] == [size, size],
      [len(b) for b in fake.tt_uploads.values()])
body = fake.tt_init_bodies[0]
check("TikTok got the chosen settings",
      body["post_info"]["privacy_level"] == "SELF_ONLY" and body["post_info"]["disable_comment"] is False
      and body["post_info"]["disable_duet"] is True and body["post_info"]["disable_stitch"] is True
      and body["source_info"] == {"source": "FILE_UPLOAD", "video_size": size, "chunk_size": size, "total_chunk_count": 1},
      body)
check("TikTok post links to the video", posts[tt1]["url"] == "https://www.tiktok.com/@tt_user_1/video/7300000000000000001",
      posts[tt1])
check("Instagram downloaded the clip from the signed public link", fake.ig_fetched == [size], fake.ig_fetched)
check("Instagram got a Reel with the caption",
      fake.ig_bodies[0]["media_type"] == "REELS" and fake.ig_bodies[0]["caption"] == v[2]["caption"]
      and fake.ig_bodies[0]["share_to_feed"] == "true", fake.ig_bodies)
check("Instagram post links to the reel", posts[ig]["url"] == "https://www.instagram.com/reel/media-cont-1/", posts[ig])
meta = fake.yt_meta[0]
check("YouTube got title, description, privacy and size",
      meta["meta"]["snippet"]["title"] == v[3]["title"] and meta["meta"]["status"]["privacyStatus"] == "unlisted"
      and meta["meta"]["status"]["selfDeclaredMadeForKids"] is False and meta["length"] == str(size)
      and meta["params"] == {"uploadType": "resumable", "part": "snippet,status"} and fake.yt_bytes == [size], meta)
check("YouTube post links to the Short", posts[yt]["url"] == "https://www.youtube.com/shorts/ytvid1", posts[yt])

r = client.post("/api/clips/clipB/posts", json={"spacing_minutes": 15, "targets": [tt(tt1), tt(tt2)]})
sched = [p["scheduled_at"] for p in r.json()["posts"]]
gap = datetime.fromisoformat(sched[1]) - datetime.fromisoformat(sched[0])
check("spaced posts are 15 minutes apart", timedelta(minutes=14) < gap <= timedelta(minutes=15), sched)
check("only the first spaced post goes out now", run_queue() == 1)
due_now()
# An expired TikTok key is renewed (and TikTok's new refresh key kept) before posting.
before = db.one("SELECT refresh_token FROM social_accounts WHERE id = ?", (tt2,))["refresh_token"]
db.execute("UPDATE social_accounts SET expires_at = ? WHERE id = ?",
           ((datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(timespec="seconds"), tt2))
run_queue()
after = db.one("SELECT refresh_token FROM social_accounts WHERE id = ?", (tt2,))["refresh_token"]
check("expired TikTok key renewed before posting", fake.refreshes["tiktok"] == 1 and before != after
      and social.unseal(after) == "tt-refresh-2-r1", fake.refreshes)
check("second spaced post sent after renewal",
      all(p["status"] == "posted" for p in client.get("/api/posts", params={"clip_id": "clipB"}).json()["posts"]))

# Daily limit: the next TikTok post waits instead of failing.
make_clip("job1", "clipC", 2, uid)
old_limit = config.TIKTOK_POSTS_PER_DAY
config.TIKTOK_POSTS_PER_DAY = 2
client.post("/api/clips/clipC/posts", json={"targets": [tt(tt1)]})
run_queue()
p = db.one("SELECT * FROM posts WHERE clip_id = 'clipC'")
check("over the daily limit: post waits for later", p["status"] == "queued" and "daily limit" in p["stage"]
      and datetime.fromisoformat(p["scheduled_at"]) > datetime.now(timezone.utc) + timedelta(hours=20), dict(p))
config.TIKTOK_POSTS_PER_DAY = old_limit
r = client.delete(f"/api/posts/{p['id']}")
check("a waiting post can be cancelled", r.status_code == 200 and
      db.one("SELECT status FROM posts WHERE id = ?", (p["id"],))["status"] == "cancelled")

# TikTok says the account posted too much today: wait a day.
r = client.post(f"/api/posts/{p['id']}/retry")
fake.spam_tokens.add("tt-access-1")
run_queue()
p = db.one("SELECT * FROM posts WHERE clip_id = 'clipC'")
check("TikTok's own posting limit: post waits a day", p["status"] == "queued" and "daily posting limit" in p["stage"], dict(p))
fake.spam_tokens.clear()
db.execute("UPDATE posts SET status = 'cancelled' WHERE clip_id = 'clipC'")

# Instagram hiccup: retried, then works.
fake.ig_fail_create = 1
client.post("/api/clips/clipC/posts", json={"targets": [{"account_id": ig, "caption": "Again #reels"}]})
run_queue()
p = db.one("SELECT * FROM posts WHERE clip_id = 'clipC' AND platform = 'instagram'")
check("temporary Instagram error: retried later", p["status"] == "queued" and p["attempts"] == 1
      and "Retrying" in p["stage"], dict(p))
due_now()
run_queue()
p = db.one("SELECT * FROM posts WHERE clip_id = 'clipC' AND platform = 'instagram'")
check("retry succeeded", p["status"] == "posted", dict(p))

# Google sign-in revoked: account flagged, post fails with a clear message.
fake.google_refresh_ok = False
db.execute("UPDATE social_accounts SET expires_at = ? WHERE id = ?",
           ((datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(timespec="seconds"), yt))
client.post("/api/clips/clipC/posts", json={"targets": [{"account_id": yt, "caption": "x", "title": "Title", "privacy": "public"}]})
run_queue()
p = db.one("SELECT * FROM posts WHERE clip_id = 'clipC' AND platform = 'youtube'")
acc = db.one("SELECT status FROM social_accounts WHERE id = ?", (yt,))
check("revoked Google sign-in: account needs reconnecting", acc["status"] == "reconnect" and p["status"] == "failed"
      and "Reconnect" in p["error"], (acc, dict(p)))
r = client.post(f"/api/posts/{p['id']}/retry")
check("retry is refused until the account is reconnected", r.status_code == 409, r.text)
fake.google_refresh_ok = True
connect("youtube", "g-code")
check("reconnecting fixes the account", db.one("SELECT status FROM social_accounts WHERE id = ?", (yt,))["status"] == "ok")
r = client.post(f"/api/posts/{p['id']}/retry")
run_queue()
check("retry after reconnecting posts it",
      db.one("SELECT status FROM posts WHERE id = ?", (p["id"],))["status"] == "posted")

# Big clips go to TikTok in several pieces, in order (pieces made small here to test it with a short clip).
make_clip("job1", "clipE", 4, uid)
real_chunks = social.tiktok_chunks
social.tiktok_chunks = lambda s: (s // 3, 3)
client.post("/api/clips/clipE/posts", json={"targets": [tt(tt1)]})
run_queue()
social.tiktok_chunks = real_chunks
last_pid = list(fake.tt_uploads)[-1]
size_e = (job_dir("job1") / "clips" / "clipE" / "clip.mp4").read_bytes()
check("multi-piece TikTok upload arrives whole", bytes(fake.tt_uploads[last_pid]) == size_e
      and fake.tt_init_bodies[-1]["source_info"]["total_chunk_count"] == 3
      and db.one("SELECT status FROM posts WHERE clip_id = 'clipE'")["status"] == "posted")

# Deleted clip: the waiting post fails cleanly.
make_clip("job1", "clipD", 3, uid)
client.post("/api/clips/clipD/posts", json={"targets": [{"account_id": ig, "caption": "Gone"}]})
db.execute("DELETE FROM clips WHERE id = 'clipD'")
run_queue()
check("post for a deleted clip fails cleanly",
      "deleted" in (db.one("SELECT error FROM posts WHERE clip_id = 'clipD'")["error"] or ""))

# A post cut off by a restart is not sent twice.
db.execute("UPDATE posts SET status = 'posting' WHERE id = (SELECT MIN(id) FROM posts)")
social.recover()
check("restart: an interrupted post is marked, not resent",
      db.one("SELECT status FROM posts WHERE id = (SELECT MIN(id) FROM posts)")["status"] == "failed")

# Public media links.
token = social.media_token("clipA")
r = client.get(f"/media/{token}/clip.mp4")
check("signed public link serves the clip", r.status_code == 200 and len(r.content) == size)
check("HEAD on the public link works", client.head(f"/media/{token}/clip.mp4").status_code == 200)
check("tampered public link is refused", client.get(f"/media/{token[:-2]}xx/clip.mp4").status_code == 404)
check("expired public link is refused", social.media_clip(token, max_age=-1) is None)

# Another user sees none of it.
client.post("/api/users", json={"username": "other", "password": "other-pass-123", "is_admin": False})
other = TestClient(main.app)
other.post("/api/login", json={"username": "other", "password": "other-pass-123"})
check("another user sees no accounts", other.get("/api/social/accounts").json()["accounts"] == [])
check("another user sees no posts", other.get("/api/posts").json()["posts"] == [])
check("another user can't use someone's account", other.get(f"/api/social/accounts/{tt1}/tiktok").status_code == 404)
check("another user can't cancel someone's post", other.delete("/api/posts/1").status_code == 404)
check("non-admins don't see setup details", other.get("/api/social/accounts").json()["redirects"] == {})

# Disconnect.
r = client.delete(f"/api/social/accounts/{yt}")
check("disconnecting YouTube revokes it at Google", r.status_code == 200 and fake.revoked == ["g-refresh"], fake.revoked)
check("disconnected account is gone", not db.one("SELECT id FROM social_accounts WHERE id = ?", (yt,)))

# Legal pages.
for path_, text in (("/privacy", "Privacy Policy"), ("/terms", "Terms of Service")):
    r = TestClient(main.app).get(path_)
    check(f"{path_} is public", r.status_code == 200 and text in r.text and "YouTube Terms of Service" in r.text)
check("landing page links to both", all(x in TestClient(main.app).get("/").text for x in ('href="/privacy"', 'href="/terms"')))

failed = [n for ok, n in results if not ok]
print(f"\n{len(results) - len(failed)}/{len(results)} checks passed")
sys.exit(1 if failed else 0)
