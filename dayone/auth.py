"""Back-office accounts, roles and sessions.

Passwords are hashed with scrypt; session tokens are random and only their
SHA-256 is stored. Roles: ``reviewer`` (review, confirm, retake, timelines) and
``admin`` (also demo reset, AI switch, users and WhatsApp senders).
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import threading
import time
from datetime import datetime, timedelta, timezone

ROLES = ("reviewer", "admin")
SESSION_HOURS = 12
MAX_FAILURES = 5
LOCKOUT_SECONDS = 300
USERNAME = re.compile(r"[a-z0-9][a-z0-9._-]{1,39}")

AUTH_SQL = """
CREATE TABLE IF NOT EXISTS users (
    username TEXT PRIMARY KEY,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL,
    created_at TEXT NOT NULL,
    disabled INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY,
    username TEXT NOT NULL REFERENCES users(username),
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);
"""


class AuthError(Exception):
    def __init__(self, code: str, message: str, status: int = 401):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=2 ** 14, r=8, p=1, dklen=32)
    return f"scrypt$16384$8$1${salt.hex()}${digest.hex()}"


def check_password(password: str, stored: str) -> bool:
    try:
        _, n, r, p, salt, digest = stored.split("$")
        candidate = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=int(n), r=int(r), p=int(p),
                                   dklen=len(bytes.fromhex(digest)))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(candidate.hex(), digest)


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class Auth:
    def __init__(self, store, clock=_now):
        self.store = store
        self.clock = clock
        self._failures: dict[str, list[float]] = {}
        self._lock = threading.Lock()
        with store.tx() as db:
            for statement in filter(str.strip, AUTH_SQL.split(";")):
                db.execute(statement)

    # ------------------------------------------------------------ users

    def create_user(self, username: str, password: str, role: str) -> dict:
        username = (username or "").strip().lower()
        if not USERNAME.fullmatch(username):
            raise AuthError("INVALID_USERNAME", "Identifiant : 2 à 40 caractères (a-z, 0-9, . _ -).", 422)
        if role not in ROLES:
            raise AuthError("INVALID_ROLE", "Rôle inconnu.", 422)
        if len(password or "") < 10:
            raise AuthError("WEAK_PASSWORD", "Mot de passe : 10 caractères minimum.", 422)
        with self.store.tx() as db:
            if db.execute("SELECT 1 FROM users WHERE username = ?", (username,)).fetchone():
                raise AuthError("USER_EXISTS", "Cet identifiant existe déjà.", 409)
            db.execute("INSERT INTO users(username, password_hash, role, created_at) VALUES (?, ?, ?, ?)",
                       (username, hash_password(password), role, self.clock().isoformat()))
        return {"username": username, "role": role}

    def set_password(self, username: str, password: str) -> None:
        if len(password or "") < 10:
            raise AuthError("WEAK_PASSWORD", "Mot de passe : 10 caractères minimum.", 422)
        with self.store.tx() as db:
            updated = db.execute("UPDATE users SET password_hash = ? WHERE username = ?",
                                 (hash_password(password), username)).rowcount
            db.execute("DELETE FROM sessions WHERE username = ?", (username,))
        if not updated:
            raise AuthError("USER_NOT_FOUND", "Utilisateur introuvable.", 404)

    def list_users(self) -> list[dict]:
        with self.store.read() as db:
            return [dict(row) for row in db.execute("SELECT username, role, created_at, disabled FROM users ORDER BY username")]

    def has_users(self) -> bool:
        with self.store.read() as db:
            return db.execute("SELECT COUNT(*) FROM users").fetchone()[0] > 0

    def ensure_admin(self) -> str | None:
        """Create ``admin`` with a random password when no account exists; returns that password once."""
        if self.has_users():
            return None
        password = secrets.token_urlsafe(12)
        self.create_user("admin", password, "admin")
        return password

    # ------------------------------------------------------------ sessions

    def login(self, username: str, password: str) -> tuple[str, dict]:
        username = (username or "").strip().lower()
        with self._lock:
            recent = [t for t in self._failures.get(username, []) if time.monotonic() - t < LOCKOUT_SECONDS]
            self._failures[username] = recent
            if len(recent) >= MAX_FAILURES:
                raise AuthError("LOCKED", "Trop d'essais : réessayez dans quelques minutes.", 429)
        with self.store.read() as db:
            user = db.execute("SELECT * FROM users WHERE username = ? AND disabled = 0", (username,)).fetchone()
        if user is None or not check_password(password or "", user["password_hash"]):
            if user is None:
                check_password(password or "", hash_password("timing-equaliser"))
            with self._lock:
                self._failures.setdefault(username, []).append(time.monotonic())
            raise AuthError("BAD_CREDENTIALS", "Identifiant ou mot de passe incorrect.")
        with self._lock:
            self._failures.pop(username, None)
        token = secrets.token_urlsafe(32)
        now = self.clock()
        with self.store.tx() as db:
            db.execute("DELETE FROM sessions WHERE expires_at < ?", (now.isoformat(),))
            db.execute("INSERT INTO sessions(token_hash, username, created_at, expires_at) VALUES (?, ?, ?, ?)",
                       (_token_hash(token), username, now.isoformat(), (now + timedelta(hours=SESSION_HOURS)).isoformat()))
        return token, {"username": username, "role": user["role"]}

    def user_for(self, token: str | None) -> dict | None:
        if not token:
            return None
        with self.store.read() as db:
            row = db.execute(
                "SELECT u.username, u.role, s.expires_at FROM sessions s JOIN users u USING (username) "
                "WHERE s.token_hash = ? AND u.disabled = 0", (_token_hash(token),)).fetchone()
        if row is None or row["expires_at"] < self.clock().isoformat():
            return None
        return {"username": row["username"], "role": row["role"]}

    def logout(self, token: str | None) -> None:
        if token:
            with self.store.tx() as db:
                db.execute("DELETE FROM sessions WHERE token_hash = ?", (_token_hash(token),))
