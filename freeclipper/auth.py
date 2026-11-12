"""Password hashing (stdlib scrypt), API tokens and simple login throttling."""
from __future__ import annotations

import hashlib
import hmac
import secrets
import threading
import time

from . import db

_SCRYPT = {"n": 2**14, "r": 8, "p": 1, "dklen": 64}


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, **_SCRYPT)
    return f"scrypt${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, salt_hex, digest_hex = stored.split("$")
    except ValueError:
        return False
    if scheme != "scrypt":
        return False
    digest = hashlib.scrypt(password.encode("utf-8"), salt=bytes.fromhex(salt_hex), **_SCRYPT)
    return hmac.compare_digest(digest.hex(), digest_hex)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def new_api_token(user_id: int) -> str:
    token = "fc_" + secrets.token_urlsafe(32)
    db.execute("UPDATE users SET api_token_hash = ? WHERE id = ?", (token_hash(token), user_id))
    return token


def user_by_token(token: str) -> dict | None:
    if not token:
        return None
    return db.one("SELECT * FROM users WHERE api_token_hash = ?", (token_hash(token),))


def create_user(username: str, password: str, is_admin: bool = False) -> int:
    username = username.strip()
    if not (3 <= len(username) <= 40) or not all(c.isalnum() or c in "._-" for c in username):
        raise ValueError("Username must be 3-40 characters: letters, numbers, dot, dash or underscore.")
    if len(password) < 8:
        raise ValueError("Password must be at least 8 characters.")
    if db.one("SELECT id FROM users WHERE username = ?", (username,)):
        raise ValueError("That username is taken.")
    return db.execute(
        "INSERT INTO users(username, password_hash, is_admin, created_at) VALUES(?, ?, ?, ?)",
        (username, hash_password(password), int(is_admin), db.now()),
    )


class LoginThrottle:
    """Blocks an IP for a while after repeated failed logins."""

    def __init__(self, max_failures: int = 8, window: int = 600):
        self.max_failures = max_failures
        self.window = window
        self._failures: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def blocked(self, ip: str) -> bool:
        with self._lock:
            cutoff = time.time() - self.window
            recent = [t for t in self._failures.get(ip, []) if t > cutoff]
            self._failures[ip] = recent
            return len(recent) >= self.max_failures

    def fail(self, ip: str) -> None:
        with self._lock:
            self._failures.setdefault(ip, []).append(time.time())

    def reset(self, ip: str) -> None:
        with self._lock:
            self._failures.pop(ip, None)


throttle = LoginThrottle()
