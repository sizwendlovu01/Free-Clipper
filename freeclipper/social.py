"""Auto-posting clips to TikTok, Instagram (Reels) and YouTube (Shorts).

Each platform account a user connects is one row in social_accounts, holding that account's own keys
(encrypted with the server's SECRET_KEY). Any number of accounts per platform can be connected; every one
signs in on the platform's own page, so ClipForge never sees a password.

Posting runs in a background thread from a queue (the posts table): a post waits until its scheduled
time, then the clip is sent to the platform and the result (a link, or the reason it failed) is stored.
The queue keeps each account under its platform's daily limit by moving posts to later instead.
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import re
import shutil
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterator
from urllib.parse import urlencode

import httpx

from . import config, db, monitor, storage
from .worker import job_dir

log = logging.getLogger("freeclipper.social")

PLATFORMS = {"tiktok": "TikTok", "instagram": "Instagram", "youtube": "YouTube"}

SCOPES = {
    "tiktok": "user.info.basic,video.publish",
    "instagram": "instagram_business_basic,instagram_business_content_publish",
    "youtube": "https://www.googleapis.com/auth/youtube.upload https://www.googleapis.com/auth/youtube.readonly",
}

TIKTOK_API = "https://open.tiktokapis.com/v2"
TIKTOK_PRIVACY = ("PUBLIC_TO_EVERYONE", "MUTUAL_FOLLOW_FRIENDS", "FOLLOWER_OF_CREATOR", "SELF_ONLY")
YOUTUBE_PRIVACY = ("public", "unlisted", "private")
MAX_ATTEMPTS = 3

# Tests swap in a fake platform here (an httpx.MockTransport) and skip the waits between status checks.
transport: httpx.BaseTransport | None = None
pause: Callable[[float], None] = time.sleep


def http() -> httpx.Client:
    return httpx.Client(timeout=httpx.Timeout(120, connect=20), transport=transport)


def configured(platform: str) -> bool:
    return {
        "tiktok": bool(config.TIKTOK_CLIENT_KEY and config.TIKTOK_CLIENT_SECRET),
        "instagram": bool(config.INSTAGRAM_APP_ID and config.INSTAGRAM_APP_SECRET),
        "youtube": bool(config.GOOGLE_CLIENT_ID and config.GOOGLE_CLIENT_SECRET),
    }.get(platform, False)


class PostError(Exception):
    """A post (or sign-in) that didn't work. retry: worth trying again soon. wait: try again after this
    time (a daily limit). reconnect: the account's sign-in no longer works."""

    def __init__(self, message: str, retry: bool = False, reconnect: bool = False,
                 wait: datetime | None = None):
        super().__init__(message)
        self.retry, self.reconnect, self.wait = retry, reconnect, wait


# ---------------- time and secrets ----------------

def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime | None) -> str | None:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds") if dt else None


def _parse(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def _in(seconds) -> str | None:
    try:
        return _iso(_now() + timedelta(seconds=int(seconds))) if seconds else None
    except (TypeError, ValueError):
        return None


def _fernet():
    from cryptography.fernet import Fernet

    key = hashlib.sha256(b"clipforge-social-tokens:" + config.SECRET_KEY.encode()).digest()
    return Fernet(base64.urlsafe_b64encode(key))


def seal(value: str | None) -> str | None:
    return _fernet().encrypt(value.encode()).decode() if value else None


def unseal(value: str | None) -> str | None:
    if not value:
        return None
    from cryptography.fernet import InvalidToken

    try:
        return _fernet().decrypt(value.encode()).decode()
    except InvalidToken:  # SECRET_KEY changed since the account was connected
        raise PostError("This account's sign-in can't be read any more (the server's SECRET_KEY changed). "
                        "Reconnect it in Settings.", reconnect=True)


def _media_signer():
    from itsdangerous import URLSafeTimedSerializer

    return URLSafeTimedSerializer(config.SECRET_KEY, salt="clipforge-public-media")


def media_token(clip_id: str) -> str:
    return _media_signer().dumps(clip_id)


def media_clip(token: str, max_age: int = 6 * 3600) -> str | None:
    """The clip a public media link points to, if the link is genuine and recent."""
    from itsdangerous import BadSignature

    try:
        return _media_signer().loads(token, max_age=max_age)
    except BadSignature:
        return None


# ---------------- connecting accounts (OAuth) ----------------

def authorize_url(platform: str, state: str, redirect_uri: str) -> str:
    if platform == "tiktok":
        return "https://www.tiktok.com/v2/auth/authorize/?" + urlencode({
            "client_key": config.TIKTOK_CLIENT_KEY, "response_type": "code", "scope": SCOPES["tiktok"],
            "redirect_uri": redirect_uri, "state": state, "disable_auto_auth": 1})
    if platform == "instagram":
        return "https://www.instagram.com/oauth/authorize?" + urlencode({
            "client_id": config.INSTAGRAM_APP_ID, "redirect_uri": redirect_uri, "response_type": "code",
            "scope": SCOPES["instagram"], "state": state, "force_reauth": "true"})
    if platform == "youtube":
        return "https://accounts.google.com/o/oauth2/v2/auth?" + urlencode({
            "client_id": config.GOOGLE_CLIENT_ID, "redirect_uri": redirect_uri, "response_type": "code",
            "scope": SCOPES["youtube"], "access_type": "offline", "prompt": "select_account consent",
            "include_granted_scopes": "true", "state": state})
    raise ValueError(platform)


def _json(r: httpx.Response) -> dict:
    try:
        data = r.json()
    except ValueError:
        data = {}
    return data if isinstance(data, dict) else {}


def _unwrap(data: dict) -> dict:
    """Some Instagram responses come wrapped as {"data": [{...}]}."""
    inner = data.get("data")
    return inner[0] if isinstance(inner, list) and inner and isinstance(inner[0], dict) else data


def _oauth_error(r: httpx.Response, what: str) -> PostError:
    d = _json(r)
    err = d.get("error")
    detail = (d.get("error_description") or (err.get("message") if isinstance(err, dict) else err)
              or d.get("error_message") or r.text[:200])
    return PostError(f"{what} failed: {detail}", retry=r.status_code >= 500)


def exchange(platform: str, code: str, redirect_uri: str) -> dict:
    """Trade the sign-in code for the account's keys and profile."""
    with http() as c:
        if platform == "tiktok":
            r = c.post(f"{TIKTOK_API}/oauth/token/", data={
                "client_key": config.TIKTOK_CLIENT_KEY, "client_secret": config.TIKTOK_CLIENT_SECRET,
                "code": code, "grant_type": "authorization_code", "redirect_uri": redirect_uri})
            t = _json(r)
            if r.status_code != 200 or t.get("error") or not t.get("access_token"):
                raise _oauth_error(r, "TikTok sign-in")
            if "video.publish" not in (t.get("scope") or ""):
                raise PostError("TikTok sign-in worked, but posting wasn't allowed. Connect again and leave "
                                "\"Post content\" ticked.")
            u = c.get(f"{TIKTOK_API}/user/info/", params={"fields": "open_id,avatar_url,display_name"},
                      headers={"Authorization": f"Bearer {t['access_token']}"})
            user = (_json(u).get("data") or {}).get("user") or {}
            return {"external_id": t.get("open_id") or user.get("open_id"),
                    "username": "", "display_name": user.get("display_name") or "TikTok account",
                    "avatar_url": user.get("avatar_url") or "", "access_token": t["access_token"],
                    "refresh_token": t.get("refresh_token"), "expires_at": _in(t.get("expires_in")),
                    "refresh_expires_at": _in(t.get("refresh_expires_in")), "scopes": t.get("scope") or ""}
        if platform == "instagram":
            r = c.post("https://api.instagram.com/oauth/access_token", data={
                "client_id": config.INSTAGRAM_APP_ID, "client_secret": config.INSTAGRAM_APP_SECRET,
                "grant_type": "authorization_code", "redirect_uri": redirect_uri, "code": code})
            t = _unwrap(_json(r))
            if r.status_code != 200 or not t.get("access_token"):
                raise _oauth_error(r, "Instagram sign-in")
            perms = t.get("permissions") or ""
            perms = ",".join(perms) if isinstance(perms, list) else str(perms)
            if perms and "instagram_business_content_publish" not in perms:
                raise PostError("Instagram sign-in worked, but posting wasn't allowed. Connect again and allow "
                                "content publishing.")
            r = c.get("https://graph.instagram.com/access_token", params={
                "grant_type": "ig_exchange_token", "client_secret": config.INSTAGRAM_APP_SECRET,
                "access_token": t["access_token"]})
            long = _json(r)
            if r.status_code != 200 or not long.get("access_token"):
                raise _oauth_error(r, "Instagram sign-in")
            r = c.get(f"https://graph.instagram.com/{config.INSTAGRAM_API_VERSION}/me", params={
                "fields": "user_id,username,name,profile_picture_url,account_type",
                "access_token": long["access_token"]})
            me = _unwrap(_json(r))
            ig_id = str(me.get("user_id") or t.get("user_id") or me.get("id") or "")
            if r.status_code != 200 or not ig_id:
                raise _oauth_error(r, "Reading the Instagram profile")
            return {"external_id": ig_id, "username": me.get("username") or "",
                    "display_name": me.get("name") or me.get("username") or "Instagram account",
                    "avatar_url": me.get("profile_picture_url") or "", "access_token": long["access_token"],
                    "refresh_token": None, "expires_at": _in(long.get("expires_in") or 60 * 86400),
                    "refresh_expires_at": None, "scopes": perms}
        if platform == "youtube":
            r = c.post("https://oauth2.googleapis.com/token", data={
                "code": code, "client_id": config.GOOGLE_CLIENT_ID, "client_secret": config.GOOGLE_CLIENT_SECRET,
                "redirect_uri": redirect_uri, "grant_type": "authorization_code"})
            t = _json(r)
            if r.status_code != 200 or not t.get("access_token"):
                raise _oauth_error(r, "Google sign-in")
            if "youtube.upload" not in (t.get("scope") or ""):
                raise PostError("Google sign-in worked, but uploading wasn't allowed. Connect again and tick "
                                "\"Manage your YouTube videos\".")
            if not t.get("refresh_token"):
                raise PostError("Google didn't give a lasting sign-in. Remove ClipForge from your Google "
                                "account's third-party access, then connect again.")
            r = c.get("https://www.googleapis.com/youtube/v3/channels", params={"part": "snippet", "mine": "true"},
                      headers={"Authorization": f"Bearer {t['access_token']}"})
            items = _json(r).get("items") or []
            if r.status_code != 200 or not items:
                raise PostError("This Google account has no YouTube channel. Create one on YouTube first.")
            ch = items[0]
            snip = ch.get("snippet") or {}
            thumb = ((snip.get("thumbnails") or {}).get("default") or {}).get("url") or ""
            return {"external_id": ch.get("id"), "username": (snip.get("customUrl") or "").lstrip("@"),
                    "display_name": snip.get("title") or "YouTube channel", "avatar_url": thumb,
                    "access_token": t["access_token"], "refresh_token": t["refresh_token"],
                    "expires_at": _in(t.get("expires_in")), "refresh_expires_at": None,
                    "scopes": t.get("scope") or ""}
    raise ValueError(platform)


def save_account(user_id: int, platform: str, info: dict) -> int:
    """Add a newly connected account, or update its keys if it was connected before."""
    if not info.get("external_id"):
        raise PostError("The platform didn't say which account signed in. Try connecting again.")
    existing = db.one("SELECT id FROM social_accounts WHERE user_id = ? AND platform = ? AND external_id = ?",
                      (user_id, platform, info["external_id"]))
    fields = (info["username"], info["display_name"], info["avatar_url"], seal(info["access_token"]),
              seal(info.get("refresh_token")), info.get("expires_at"), info.get("refresh_expires_at"),
              info.get("scopes") or "")
    if existing:
        db.execute("UPDATE social_accounts SET username = ?, display_name = ?, avatar_url = ?, access_token = ?, "
                   "refresh_token = ?, expires_at = ?, refresh_expires_at = ?, scopes = ?, status = 'ok', "
                   "last_error = NULL, updated_at = ? WHERE id = ?", (*fields, db.now(), existing["id"]))
        return existing["id"]
    count = db.one("SELECT COUNT(*) AS n FROM social_accounts WHERE user_id = ? AND platform = ?",
                   (user_id, platform))["n"]
    if count >= config.MAX_SOCIAL_ACCOUNTS:
        raise PostError(f"You can connect up to {config.MAX_SOCIAL_ACCOUNTS} {PLATFORMS[platform]} accounts.")
    return db.execute(
        "INSERT INTO social_accounts(user_id, platform, external_id, username, display_name, avatar_url, "
        "access_token, refresh_token, expires_at, refresh_expires_at, scopes, status, created_at, updated_at) "
        "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'ok', ?, ?)",
        (user_id, platform, info["external_id"], *fields, db.now(), db.now()))


def disconnect(account: dict) -> None:
    """Forget an account (and cancel its waiting posts). Google keys are also revoked at Google."""
    if account["platform"] == "youtube":
        try:
            token = unseal(account["refresh_token"]) or unseal(account["access_token"])
            with http() as c:
                c.post("https://oauth2.googleapis.com/revoke", params={"token": token})
        except Exception:
            pass
    db.execute("DELETE FROM social_accounts WHERE id = ?", (account["id"],))


def mark_reconnect(account_id: int, message: str) -> None:
    db.execute("UPDATE social_accounts SET status = 'reconnect', last_error = ?, updated_at = ? WHERE id = ?",
               (message[:500], db.now(), account_id))


def refresh(account: dict) -> str:
    """Renew an account's access key. Returns the new key."""
    platform = account["platform"]
    with http() as c:
        if platform == "tiktok":
            r = c.post(f"{TIKTOK_API}/oauth/token/", data={
                "client_key": config.TIKTOK_CLIENT_KEY, "client_secret": config.TIKTOK_CLIENT_SECRET,
                "grant_type": "refresh_token", "refresh_token": unseal(account["refresh_token"])})
            t = _json(r)
            if r.status_code != 200 or t.get("error") or not t.get("access_token"):
                if r.status_code < 500:
                    mark_reconnect(account["id"], "TikTok asked to sign in again.")
                    raise PostError("TikTok asked to sign in to this account again. Reconnect it in Settings.",
                                    reconnect=True)
                raise _oauth_error(r, "Renewing the TikTok sign-in")
            # TikTok replaces the refresh key every time: keep the newest one.
            db.execute("UPDATE social_accounts SET access_token = ?, refresh_token = ?, expires_at = ?, "
                       "refresh_expires_at = ?, status = 'ok', last_error = NULL, updated_at = ? WHERE id = ?",
                       (seal(t["access_token"]), seal(t.get("refresh_token")) or account["refresh_token"],
                        _in(t.get("expires_in")), _in(t.get("refresh_expires_in")) or account["refresh_expires_at"],
                        db.now(), account["id"]))
            return t["access_token"]
        if platform == "instagram":
            r = c.get("https://graph.instagram.com/refresh_access_token", params={
                "grant_type": "ig_refresh_token", "access_token": unseal(account["access_token"])})
            t = _json(r)
            if r.status_code != 200 or not t.get("access_token"):
                if r.status_code < 500:
                    mark_reconnect(account["id"], "Instagram asked to sign in again.")
                    raise PostError("Instagram asked to sign in to this account again. Reconnect it in Settings.",
                                    reconnect=True)
                raise _oauth_error(r, "Renewing the Instagram sign-in")
            db.execute("UPDATE social_accounts SET access_token = ?, expires_at = ?, status = 'ok', "
                       "last_error = NULL, updated_at = ? WHERE id = ?",
                       (seal(t["access_token"]), _in(t.get("expires_in") or 60 * 86400), db.now(), account["id"]))
            return t["access_token"]
        if platform == "youtube":
            r = c.post("https://oauth2.googleapis.com/token", data={
                "client_id": config.GOOGLE_CLIENT_ID, "client_secret": config.GOOGLE_CLIENT_SECRET,
                "grant_type": "refresh_token", "refresh_token": unseal(account["refresh_token"])})
            t = _json(r)
            if r.status_code != 200 or not t.get("access_token"):
                if r.status_code < 500:
                    mark_reconnect(account["id"], "Google asked to sign in again.")
                    raise PostError("Google asked to sign in to this channel again (in Testing mode Google ends "
                                    "sign-ins after 7 days). Reconnect it in Settings.", reconnect=True)
                raise _oauth_error(r, "Renewing the Google sign-in")
            db.execute("UPDATE social_accounts SET access_token = ?, expires_at = ?, status = 'ok', "
                       "last_error = NULL, updated_at = ? WHERE id = ?",
                       (seal(t["access_token"]), _in(t.get("expires_in")), db.now(), account["id"]))
            return t["access_token"]
    raise ValueError(platform)


def access_token(account: dict) -> str:
    """A working access key for the account, renewed first if it's about to run out."""
    if account["status"] == "reconnect":
        raise PostError("This account needs to be reconnected in Settings.", reconnect=True)
    expires = _parse(account["expires_at"])
    if expires and expires - _now() < timedelta(minutes=5):
        if account["platform"] == "instagram":
            mark_reconnect(account["id"], "The Instagram sign-in ran out.")
            raise PostError("The Instagram sign-in ran out. Reconnect the account in Settings.", reconnect=True)
        return refresh(account)
    return unseal(account["access_token"])


def maintenance() -> None:
    """Keep idle accounts signed in: Instagram keys last 60 days and TikTok refresh keys 180 days unless
    renewed. Runs about once an hour."""
    soon = _iso(_now() + timedelta(days=20))
    day_old = _iso(_now() - timedelta(days=1))
    rows = db.query(
        "SELECT * FROM social_accounts WHERE status = 'ok' AND ("
        " (platform = 'instagram' AND expires_at < ? AND updated_at < ?)"
        " OR (platform = 'tiktok' AND refresh_expires_at < ?))", (soon, day_old, soon))
    for account in rows:
        try:
            refresh(account)
        except Exception as exc:
            log.warning("couldn't renew %s account %s: %s", account["platform"], account["id"], exc)


# ---------------- what a post says ----------------

PLATFORM_TAGS = {"tiktok": ["#fyp", "#foryou"], "instagram": ["#reels", "#explore"], "youtube": ["#shorts"]}
TITLE_LEADS = ["Wait For It", "Nobody Talks About This", "Watch Till the End", "You Need to Hear This",
               "This Changed Everything", "Real Talk", "Hot Take", "Listen to This"]
DISCOVERY_TAGS = {"#fyp", "#viral", "#foryou", "#foryoupage", "#reels", "#explore", "#shorts", "#trending"}


def caption_variants(clip: dict, platforms: list[str]) -> list[dict]:
    """A title and caption for each account a clip is going to, all different: posting identical text to
    several accounts looks like spam to the platforms. The first keeps the clip's own title."""
    from .pipeline import titles

    text, language = "", "en"
    try:
        transcript = json.loads((job_dir(clip["job_id"]) / "transcript.json").read_text(encoding="utf-8"))
        language = transcript.get("language") or "en"
        text = " ".join(s["text"] for s in transcript.get("segments", [])
                        if s["end"] > clip["start"] and s["start"] < clip["end"])
    except (OSError, ValueError, KeyError):
        pass
    tags = [t for t in json.loads(clip["hashtags"] or "[]") if t.lower() not in DISCOVERY_TAGS]
    seen: set[str] = set()
    out = []
    for k, platform in enumerate(platforms):
        title = clip["title"] or "Watch This"
        if k:
            candidates = [titles.viral_title(text, language, clip["idx"] + k * 3 + n) for n in range(6)] \
                if text.strip() else []
            # A short clip may have only one or two good lines: fall back to a different lead-in.
            base = re.sub(r"^[^:]{3,40}:\s*", "", clip["title"] or "Watch This").strip("“”\"")
            candidates += [f"{lead}: {base}" for lead in TITLE_LEADS[k % len(TITLE_LEADS):] + TITLE_LEADS]
            title = next((t for t in candidates if t.lower() not in seen), title)
        seen.add(title.lower())
        topic = tags[k % len(tags):] + tags[:k % len(tags)] if tags else []
        hashtags = topic + PLATFORM_TAGS.get(platform, [])
        out.append({"title": title, "hashtags": hashtags, "caption": f"{title}\n\n{' '.join(hashtags)}".strip()})
    return out


# ---------------- queueing posts ----------------

def account_json(a: dict) -> dict:
    since = _iso(_now() - timedelta(days=1))
    today = db.one("SELECT COUNT(*) AS n FROM posts WHERE account_id = ? AND status = 'posted' AND posted_at >= ?",
                   (a["id"], since))["n"]
    return {"id": a["id"], "platform": a["platform"], "platform_label": PLATFORMS[a["platform"]],
            "username": a["username"], "display_name": a["display_name"], "avatar_url": a["avatar_url"],
            "status": a["status"], "last_error": a["last_error"], "created_at": a["created_at"],
            "posts_today": today, "daily_limit": _daily_limit(a["platform"])}


def post_json(p: dict) -> dict:
    a = db.one("SELECT username, display_name FROM social_accounts WHERE id = ?", (p["account_id"],)) or {}
    return {"id": p["id"], "platform": p["platform"], "platform_label": PLATFORMS.get(p["platform"], p["platform"]),
            "account_id": p["account_id"], "account": "@" + a["username"] if a.get("username") else a.get("display_name") or "",
            "clip_id": p["clip_id"], "job_id": p["job_id"], "title": p["title"], "caption": p["caption"],
            "status": p["status"], "stage": p["stage"], "scheduled_at": p["scheduled_at"], "url": p["url"],
            "error": p["error"], "posted_at": p["posted_at"], "attempts": p["attempts"]}


def _daily_limit(platform: str) -> int:
    return {"tiktok": config.TIKTOK_POSTS_PER_DAY, "instagram": config.INSTAGRAM_POSTS_PER_DAY,
            "youtube": config.YOUTUBE_UPLOADS_PER_DAY}[platform]


def clean_target(t: dict, account: dict) -> tuple[str, str, dict]:
    """Check one account's post settings. Returns (title, caption, options) or raises ValueError."""
    platform = account["platform"]
    name = account["username"] or account["display_name"]
    caption = str(t.get("caption") or "").strip()
    title = str(t.get("title") or "").strip()
    if len(caption) > 2200:
        raise ValueError(f"The caption for {name} is longer than 2200 characters.")
    opts: dict = {}
    if platform == "tiktok":
        privacy = t.get("privacy")
        if privacy not in TIKTOK_PRIVACY:
            raise ValueError(f"Choose who can see the TikTok post on {name}.")
        if not config.TIKTOK_AUDITED and privacy != "SELF_ONLY":
            raise ValueError("Until TikTok approves this app, TikTok posts can only be private (Only me).")
        commercial = bool(t.get("commercial"))
        brand_organic, brand_content = bool(t.get("brand_organic")), bool(t.get("brand_content"))
        if commercial and not (brand_organic or brand_content):
            raise ValueError("You need to indicate if your content promotes yourself, a third party, or both.")
        if commercial and brand_content and privacy == "SELF_ONLY":
            raise ValueError("Branded content visibility cannot be set to private.")
        if not t.get("consent"):
            raise ValueError("Agree to TikTok's Music Usage Confirmation to post to TikTok.")
        opts = {"privacy": privacy, "allow_comment": bool(t.get("allow_comment")),
                "allow_duet": bool(t.get("allow_duet")), "allow_stitch": bool(t.get("allow_stitch")),
                "brand_organic": commercial and brand_organic, "brand_content": commercial and brand_content}
        title = caption.split("\n")[0][:150]
    elif platform == "youtube":
        if not title:
            raise ValueError(f"Give the YouTube video on {name} a title.")
        if len(title) > 100:
            raise ValueError("YouTube titles can be at most 100 characters.")
        if "<" in title or ">" in title:
            raise ValueError("YouTube titles can't contain < or >.")
        privacy = t.get("privacy") or "public"
        if privacy not in YOUTUBE_PRIVACY:
            raise ValueError("Choose Public, Unlisted or Private for YouTube.")
        opts = {"privacy": privacy}
    elif platform == "instagram":
        if caption.count("#") > 30:
            raise ValueError("Instagram allows at most 30 hashtags in a caption.")
        title = caption.split("\n")[0][:150]
    return title, caption, opts


def queue(user_id: int, clip: dict, targets: list[dict], spacing_minutes: int) -> list[int]:
    """Queue a clip for several accounts, one after another `spacing_minutes` apart."""
    if not targets:
        raise ValueError("Pick at least one account.")
    rows = []
    for t in targets:
        account = db.one("SELECT * FROM social_accounts WHERE id = ? AND user_id = ?", (t.get("account_id"), user_id))
        if not account:
            raise ValueError("One of the accounts isn't connected any more. Reload the page.")
        if account["status"] == "reconnect":
            raise ValueError(f"Reconnect {account['username'] or account['display_name']} in Settings first.")
        if db.one("SELECT 1 AS x FROM posts WHERE clip_id = ? AND account_id = ? AND status IN "
                  "('queued', 'posting', 'posted')", (clip["id"], account["id"])):
            raise ValueError(f"This clip is already posted or waiting to post on "
                             f"{account['username'] or account['display_name']}.")
        rows.append((account, *clean_target(t, account)))
    ids = []
    start = _now()
    for i, (account, title, caption, opts) in enumerate(rows):
        ids.append(db.execute(
            "INSERT INTO posts(user_id, account_id, platform, job_id, clip_id, title, caption, options, status, "
            "stage, scheduled_at, created_at, updated_at) VALUES(?, ?, ?, ?, ?, ?, ?, ?, 'queued', ?, ?, ?, ?)",
            (user_id, account["id"], account["platform"], clip["job_id"], clip["id"], title, caption,
             json.dumps(opts), "Waiting to post" if i == 0 or not spacing_minutes else "Scheduled",
             _iso(start + timedelta(minutes=spacing_minutes * i)), db.now(), db.now())))
    notify()
    return ids


# ---------------- the posting worker ----------------

_wake = threading.Event()


def notify() -> None:
    _wake.set()


def recover() -> None:
    """Posts cut off by a restart may or may not have gone out, so they aren't sent again automatically."""
    db.execute("UPDATE posts SET status = 'failed', stage = 'Interrupted', error = ?, updated_at = ? "
               "WHERE status = 'posting'",
               ("The server restarted while this was posting. Check the account: if the clip isn't there, "
                "press Retry.", db.now()))


def poster_loop(stopping: Callable[[], bool]) -> None:
    recover()
    last_maintenance = 0.0
    while not stopping():
        try:
            if time.monotonic() - last_maintenance > 3600:
                last_maintenance = time.monotonic()
                maintenance()
            while not stopping() and process_next():
                pass
        except Exception:
            log.exception("posting loop error")
        _wake.wait(15)
        _wake.clear()


def claim() -> dict | None:
    rows = db.query(
        "UPDATE posts SET status = 'posting', stage = 'Starting', attempts = attempts + 1, updated_at = ? "
        "WHERE id = (SELECT id FROM posts WHERE status = 'queued' AND scheduled_at <= ? "
        "ORDER BY scheduled_at, id LIMIT 1) AND status = 'queued' RETURNING *", (db.now(), _iso(_now())))
    return rows[0] if rows else None


def process_next() -> bool:
    post = claim()
    if not post:
        return False
    run(post)
    return True


def _stage(post_id: int, text: str) -> None:
    db.execute("UPDATE posts SET stage = ?, updated_at = ? WHERE id = ?", (text, db.now(), post_id))


def _fail(post: dict, message: str) -> None:
    db.execute("UPDATE posts SET status = 'failed', stage = 'Failed', error = ?, updated_at = ? WHERE id = ?",
               (message[:900], db.now(), post["id"]))


def _requeue(post: dict, when: datetime, stage: str, count_attempt: bool = True) -> None:
    db.execute("UPDATE posts SET status = 'queued', stage = ?, scheduled_at = ?, attempts = attempts - ?, "
               "updated_at = ? WHERE id = ?", (stage, _iso(when), 0 if count_attempt else 1, db.now(), post["id"]))


def _limit_wait(account: dict) -> datetime | None:
    """When the account (or, for YouTube, the whole app) may post again, if it's at its daily limit."""
    since = _iso(_now() - timedelta(days=1))
    if account["platform"] == "youtube":
        rows = db.query("SELECT posted_at FROM posts WHERE platform = 'youtube' AND status = 'posted' "
                        "AND posted_at >= ? ORDER BY posted_at", (since,))
    else:
        rows = db.query("SELECT posted_at FROM posts WHERE account_id = ? AND status = 'posted' AND posted_at >= ? "
                        "ORDER BY posted_at", (account["id"], since))
    limit = _daily_limit(account["platform"])
    if len(rows) < limit:
        return None
    return _parse(rows[len(rows) - limit]["posted_at"]) + timedelta(days=1, minutes=1)


def run(post: dict) -> None:
    account = db.one("SELECT * FROM social_accounts WHERE id = ?", (post["account_id"],))
    clip = db.one("SELECT * FROM clips WHERE id = ?", (post["clip_id"],))
    if not account:
        return _fail(post, "The account was disconnected.")
    if not clip or clip["status"] != "done":
        return _fail(post, "The clip was deleted or is being re-rendered.")
    wait = _limit_wait(account)
    if wait:
        return _requeue(post, wait, f"Waiting: {PLATFORMS[account['platform']]}'s daily limit was reached",
                        count_attempt=False)
    try:
        external_id, url, note = PUBLISHERS[account["platform"]](post, account, clip)
    except PostError as exc:
        if exc.reconnect:
            mark_reconnect(account["id"], str(exc))
            return _fail(post, str(exc))
        if exc.wait:
            return _requeue(post, exc.wait, f"Waiting: {exc}", count_attempt=False)
        if exc.retry and post["attempts"] < MAX_ATTEMPTS:
            return _requeue(post, _now() + timedelta(minutes=5 * post["attempts"]),
                            f"Retrying soon ({exc})")
        monitor.record("warning", "posting", f"{PLATFORMS[account['platform']]} post failed: {exc}"[:400],
                       job_id=post["job_id"], user_id=post["user_id"], alert=False)
        return _fail(post, str(exc))
    except (httpx.TransportError, OSError) as exc:
        if post["attempts"] < MAX_ATTEMPTS:
            return _requeue(post, _now() + timedelta(minutes=5 * post["attempts"]),
                            "Retrying soon (couldn't reach the platform)")
        return _fail(post, f"Couldn't reach {PLATFORMS[account['platform']]}: {type(exc).__name__}")
    except Exception as exc:
        log.exception("post %s failed", post["id"])
        monitor.record("error", "posting", f"Post {post['id']} crashed: {type(exc).__name__}: {exc}"[:400],
                       job_id=post["job_id"], user_id=post["user_id"])
        return _fail(post, "Something went wrong while posting. Try again later.")
    db.execute("UPDATE posts SET status = 'posted', stage = ?, external_id = ?, url = ?, error = NULL, "
               "posted_at = ?, updated_at = ? WHERE id = ?",
               (note or "Posted", external_id, url, db.now(), db.now(), post["id"]))
    log.info("post %s: clip %s posted to %s account %s", post["id"], post["clip_id"], account["platform"],
             account["id"])


@contextmanager
def clip_path(clip: dict) -> Iterator[Path]:
    """The clip's video on local disk, downloaded from R2 for the duration if it's stored there."""
    rel = f"clips/{clip['id']}/clip.mp4"
    local = job_dir(clip["job_id"]) / rel
    if local.exists():
        yield local
        return
    tmp_dir = config.DATA_DIR / "posting" / clip["id"]
    tmp = tmp_dir / "clip.mp4"
    try:
        if not storage.fetch(clip["job_id"], rel, tmp):
            raise PostError("The clip's video file is missing.")
        yield tmp
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def public_video_url(clip: dict) -> str:
    """A link the platform's servers can download the clip from (Instagram fetches videos itself)."""
    rel = f"clips/{clip['id']}/clip.mp4"
    if storage.is_remote(clip["job_id"], rel):
        return storage.link(clip["job_id"], rel, content_type="video/mp4")
    if not config.PUBLIC_DOMAIN:
        raise PostError("Instagram needs the app's public web address. Set PUBLIC_DOMAIN in the server settings.")
    return f"https://{config.PUBLIC_DOMAIN}/media/{media_token(clip['id'])}/clip.mp4"


# ---- TikTok ----

def _tiktok_call(c: httpx.Client, token: str, path: str, body: dict | None = None) -> dict:
    r = c.post(f"{TIKTOK_API}{path}", json=body or {},
               headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json; charset=UTF-8"})
    d = _json(r)
    err = d.get("error") or {}
    code = err.get("code") or ("ok" if r.status_code == 200 else "http_" + str(r.status_code))
    if code == "ok" and r.status_code == 200:
        return d.get("data") or {}
    message = TIKTOK_ERRORS.get(code) or err.get("message") or (
        f"TikTok's server had a problem (HTTP {r.status_code})." if code.startswith("http_") else code)
    if code in ("access_token_invalid", "scope_not_authorized"):
        raise PostError(f"TikTok: {message}", reconnect=True)
    if code == "spam_risk_too_many_posts":
        raise PostError(f"TikTok: {message}", wait=_now() + timedelta(hours=24))
    raise PostError(f"TikTok: {message}", retry=r.status_code >= 500 or code == "rate_limit_exceeded")


TIKTOK_ERRORS = {
    "spam_risk_too_many_posts": "this account has reached TikTok's daily posting limit.",
    "spam_risk_user_banned_from_posting": "TikTok has blocked this account from posting.",
    "reached_active_user_cap": "TikTok's daily limit of accounts posting through this app was reached "
                               "(5 a day until TikTok approves the app).",
    "unaudited_client_can_only_post_to_private_accounts": "until TikTok approves this app, the TikTok account "
                                                          "must be set to private to post.",
    "privacy_level_option_mismatch": "that privacy choice isn't available for this account any more.",
    "access_token_invalid": "the sign-in stopped working. Reconnect the account in Settings.",
    "scope_not_authorized": "posting permission was removed. Reconnect the account in Settings.",
    "rate_limit_exceeded": "too many requests; trying again shortly.",
}


def tiktok_creator_info(account: dict) -> dict:
    with http() as c:
        return _tiktok_call(c, access_token(account), "/post/publish/creator_info/query/")


def tiktok_chunks(size: int) -> tuple[int, int]:
    """(chunk_size, total_chunk_count) by TikTok's rules: a file up to 64 MB goes in one piece; bigger
    ones in 32 MB pieces, the last piece taking the remainder."""
    if size <= 64 * 1024 * 1024:
        return size, 1
    chunk = 32 * 1024 * 1024
    return chunk, size // chunk


def _post_tiktok(post: dict, account: dict, clip: dict) -> tuple[str | None, str | None, str]:
    opts = json.loads(post["options"] or "{}")
    with http() as c, clip_path(clip) as path:
        token = access_token(account)
        _stage(post["id"], "Checking the TikTok account")
        info = _tiktok_call(c, token, "/post/publish/creator_info/query/")
        if opts["privacy"] not in (info.get("privacy_level_options") or []):
            raise PostError("TikTok: that privacy choice isn't available for this account.")
        max_secs = info.get("max_video_post_duration_sec")
        if max_secs and clip["end"] - clip["start"] > max_secs + 0.5:
            raise PostError(f"TikTok: this account can post videos up to {max_secs} seconds; the clip is longer.")
        size = path.stat().st_size
        chunk, count = tiktok_chunks(size)
        data = _tiktok_call(c, token, "/post/publish/video/init/", {
            "post_info": {
                "title": post["caption"][:2000], "privacy_level": opts["privacy"],
                "disable_comment": not opts.get("allow_comment") or bool(info.get("comment_disabled")),
                "disable_duet": not opts.get("allow_duet") or bool(info.get("duet_disabled")),
                "disable_stitch": not opts.get("allow_stitch") or bool(info.get("stitch_disabled")),
                "brand_content_toggle": bool(opts.get("brand_content")),
                "brand_organic_toggle": bool(opts.get("brand_organic")),
            },
            "source_info": {"source": "FILE_UPLOAD", "video_size": size, "chunk_size": chunk,
                            "total_chunk_count": count},
        })
        publish_id, upload_url = data.get("publish_id"), data.get("upload_url")
        if not publish_id or not upload_url:
            raise PostError("TikTok didn't return an upload address.", retry=True)
        with path.open("rb") as f:
            for i in range(count):
                _stage(post["id"], f"Uploading to TikTok ({i + 1}/{count})")
                first = i * chunk
                last = size - 1 if i == count - 1 else first + chunk - 1
                f.seek(first)
                body = f.read(last - first + 1)
                r = c.put(upload_url, content=body, headers={
                    "Content-Type": "video/mp4", "Content-Length": str(len(body)),
                    "Content-Range": f"bytes {first}-{last}/{size}"})
                if r.status_code not in (200, 201, 206):
                    raise PostError(f"TikTok upload failed (HTTP {r.status_code}).", retry=r.status_code >= 500)
        _stage(post["id"], "TikTok is processing the video")
        deadline = time.monotonic() + 15 * 60
        while time.monotonic() < deadline:
            status = _tiktok_call(c, token, "/post/publish/status/fetch/", {"publish_id": publish_id})
            state = status.get("status")
            if state == "PUBLISH_COMPLETE":
                ids = status.get("publicaly_available_post_id") or []
                handle = info.get("creator_username")
                if handle and handle != account["username"]:
                    db.execute("UPDATE social_accounts SET username = ? WHERE id = ?", (handle, account["id"]))
                url = f"https://www.tiktok.com/@{handle}/video/{ids[0]}" if ids and handle else None
                note = "Posted" if url else ("Posted (private: only you can see it)" if opts["privacy"] == "SELF_ONLY"
                                             else "Posted (TikTok is still reviewing it)")
                return str(ids[0]) if ids else publish_id, url, note
            if state == "FAILED":
                reason = status.get("fail_reason") or "unknown reason"
                raise PostError(f"TikTok couldn't post it ({reason.replace('_', ' ')}).", retry=reason == "internal")
            pause(5)
        raise PostError("TikTok is taking a long time to process the video. Check the account in a few minutes.")


# ---- Instagram ----

def _ig(c: httpx.Client, method: str, path: str, token: str, **params) -> dict:
    url = f"https://graph.instagram.com/{config.INSTAGRAM_API_VERSION}/{path}"
    params["access_token"] = token
    r = c.request(method, url, params=params if method == "GET" else None, data=params if method != "GET" else None)
    d = _json(r)
    if r.status_code == 200 and "error" not in d:
        return d
    err = d.get("error") or {}
    code, msg = err.get("code"), err.get("error_user_msg") or err.get("message") or f"HTTP {r.status_code}"
    if code == 190:
        raise PostError("Instagram: the sign-in stopped working. Reconnect the account in Settings.", reconnect=True)
    if code in (4, 17, 32, 613) or r.status_code == 429:
        raise PostError(f"Instagram: too many requests ({msg}).", retry=True)
    if code == 9 or err.get("error_subcode") == 2207042:
        raise PostError("Instagram: this account reached its daily posting limit.",
                        wait=_now() + timedelta(hours=24))
    raise PostError(f"Instagram: {msg}", retry=r.status_code >= 500 or bool(err.get("is_transient")))


def _post_instagram(post: dict, account: dict, clip: dict) -> tuple[str | None, str | None, str]:
    token = access_token(account)
    ig_id = account["external_id"]
    with http() as c:
        _stage(post["id"], "Sending the clip to Instagram")
        container = _ig(c, "POST", f"{ig_id}/media", token, media_type="REELS",
                        video_url=public_video_url(clip), caption=post["caption"], share_to_feed="true")["id"]
        _stage(post["id"], "Instagram is processing the video")
        deadline = time.monotonic() + 15 * 60
        while True:
            state = _ig(c, "GET", container, token, fields="status_code,status")
            code = state.get("status_code")
            if code == "FINISHED":
                break
            if code in ("ERROR", "EXPIRED"):
                raise PostError(f"Instagram couldn't process the video ({state.get('status') or code}).")
            if time.monotonic() > deadline:
                raise PostError("Instagram is taking too long to process the video.", retry=True)
            pause(10)
        _stage(post["id"], "Publishing on Instagram")
        media_id = _ig(c, "POST", f"{ig_id}/media_publish", token, creation_id=container)["id"]
        try:
            url = _ig(c, "GET", media_id, token, fields="permalink").get("permalink")
        except PostError:
            url = None
    return media_id, url, "Posted"


# ---- YouTube ----

def _youtube_error(r: httpx.Response) -> PostError:
    err = _json(r).get("error") or {}
    reasons = {e.get("reason") for e in err.get("errors") or [] if isinstance(e, dict)}
    msg = err.get("message") or f"HTTP {r.status_code}"
    if r.status_code == 401:
        return PostError("YouTube: the sign-in stopped working. Reconnect the channel in Settings.", reconnect=True)
    if reasons & {"quotaExceeded", "uploadLimitExceeded", "rateLimitExceeded", "userRateLimitExceeded"}:
        return PostError(f"YouTube: daily upload limit reached ({msg}).", wait=_now() + timedelta(hours=24))
    return PostError(f"YouTube: {msg}", retry=r.status_code >= 500)


def _file_blocks(path: Path, block: int = 1024 * 1024) -> Iterator[bytes]:
    with path.open("rb") as f:
        while chunk := f.read(block):
            yield chunk


def _post_youtube(post: dict, account: dict, clip: dict) -> tuple[str | None, str | None, str]:
    opts = json.loads(post["options"] or "{}")
    tags = [t.lstrip("#") for t in re.findall(r"#\w+", post["caption"])][:15]
    meta = {"snippet": {"title": post["title"][:100], "description": post["caption"][:4900], "tags": tags,
                        "categoryId": "22"},
            "status": {"privacyStatus": opts.get("privacy", "public"), "selfDeclaredMadeForKids": False}}
    with http() as c, clip_path(clip) as path:
        token = access_token(account)
        size = path.stat().st_size
        _stage(post["id"], "Starting the YouTube upload")
        r = c.post("https://www.googleapis.com/upload/youtube/v3/videos",
                   params={"uploadType": "resumable", "part": "snippet,status"}, json=meta,
                   headers={"Authorization": f"Bearer {token}", "X-Upload-Content-Length": str(size),
                            "X-Upload-Content-Type": "video/mp4"})
        if r.status_code != 200 or not r.headers.get("location"):
            raise _youtube_error(r)
        _stage(post["id"], "Uploading to YouTube")
        r = c.put(r.headers["location"], content=_file_blocks(path),
                  headers={"Authorization": f"Bearer {token}", "Content-Type": "video/mp4",
                           "Content-Length": str(size)})
        if r.status_code not in (200, 201):
            raise _youtube_error(r)
        video_id = _json(r).get("id")
    if not video_id:
        raise PostError("YouTube didn't confirm the upload.")
    return video_id, f"https://www.youtube.com/shorts/{video_id}", "Posted" if opts.get("privacy") == "public" \
        else f"Posted ({opts.get('privacy')})"


PUBLISHERS = {"tiktok": _post_tiktok, "instagram": _post_instagram, "youtube": _post_youtube}

