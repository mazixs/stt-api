"""Одновладельческая БД. Только init создаёт файл; веб-процесс открывает mode=rw."""

import hashlib
import os
import secrets
import sqlite3
import time
from contextlib import closing, contextmanager
from datetime import UTC, datetime
from pathlib import Path

from argon2 import PasswordHasher, Type
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

from .errors import ApiError

HASHER = PasswordHasher(type=Type.ID)
IDLE_SECONDS = 30 * 60
ABSOLUTE_SECONDS = 12 * 3600
ATTEMPT_WINDOW = 60
ATTEMPT_LIMIT = 5
MAX_ATTEMPTS = 4096
SCHEMA = (
    "CREATE TABLE owner (id INTEGER PRIMARY KEY CHECK(id=1), username TEXT NOT NULL, "
    "password_hash TEXT NOT NULL, legacy_imported INTEGER NOT NULL CHECK(legacy_imported IN (0,1)))",
    "CREATE TABLE sessions (digest BLOB PRIMARY KEY NOT NULL, csrf TEXT NOT NULL, "
    "created REAL NOT NULL, touched REAL NOT NULL, owner_id INTEGER NOT NULL DEFAULT 1 "
    "REFERENCES owner(id) ON DELETE CASCADE)",
    "CREATE TABLE client_keys (id TEXT PRIMARY KEY NOT NULL, name TEXT NOT NULL, "
    "digest BLOB NOT NULL UNIQUE, created_at TEXT NOT NULL, revoked_at TEXT, "
    "owner_id INTEGER NOT NULL DEFAULT 1 REFERENCES owner(id) ON DELETE CASCADE)",
    "CREATE TABLE attempts (id INTEGER PRIMARY KEY, client TEXT NOT NULL, ts REAL NOT NULL)",
    "CREATE INDEX attempts_ts ON attempts(ts)",
    "CREATE INDEX attempts_client ON attempts(client)",
    "PRAGMA user_version=1",
)


def digest(token: str) -> bytes:
    return hashlib.sha256(token.encode()).digest()


def timestamp() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def unavailable() -> ApiError:
    return ApiError(503, "Хранилище доступа недоступно.", "owner_store_unavailable", "server_error")


def limited() -> ApiError:
    return ApiError(429, "Слишком много попыток входа. Повторите позже.", "login_rate_limited",
                    headers={"Retry-After": str(ATTEMPT_WINDOW)})


class OwnerStore:
    def __init__(self, directory: Path):
        self.path = directory / "owner.sqlite3"
        self.uri = self.path.resolve().as_uri() + "?mode=rw"

    @contextmanager
    def connection(self, *, write=False):
        try:
            with closing(sqlite3.connect(self.uri, uri=True, timeout=2)) as db, db:
                db.row_factory = sqlite3.Row
                db.execute("PRAGMA foreign_keys=ON")
                if write:
                    db.execute("BEGIN IMMEDIATE")
                yield db
        except sqlite3.Error:
            raise unavailable() from None

    def initialize(self, username: str, password: str, legacy_key: str | None):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        encoded = HASHER.hash(password)
        # O_EXCL: ни повторный, ни параллельный init не заменит живое хранилище.
        os.close(os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
        try:
            with self.connection(write=True) as db:
                for stmt in SCHEMA:
                    db.execute(stmt)
                db.execute("INSERT INTO owner VALUES(1, ?, ?, ?)", (username, encoded, legacy_key is not None))
                if legacy_key is not None:
                    self._insert_key(db, "legacy", legacy_key)
        except Exception:
            self.path.unlink(missing_ok=True)
            raise

    def check(self):
        with self.connection(write=True) as db:
            if (db.execute("PRAGMA user_version").fetchone()[0] != 1 or
                    db.execute("PRAGMA quick_check").fetchone()[0] != "ok" or
                    db.execute("PRAGMA foreign_key_check").fetchone() is not None):
                raise unavailable()
            row = db.execute("SELECT username, password_hash, legacy_imported FROM owner WHERE id=1").fetchone()
            if not row or not row[0] or not row[1].startswith("$argon2id$"):
                raise unavailable()
            for query in ("SELECT id, name, digest, created_at, revoked_at, owner_id FROM client_keys",
                          "SELECT digest, csrf, created, touched, owner_id FROM sessions",
                          "SELECT id, client, ts FROM attempts"):
                db.execute(query + " LIMIT 0")

    def guard_local(self, *, rollback: bool, key: str):
        try:
            self.path.lstat()
        except FileNotFoundError:
            return
        if not rollback or not key.strip():
            raise ValueError("Каталог инициализирован для OWNER_ACCESS. Для локального отката нужны "
                             "ALLOW_OWNER_LOCAL_ROLLBACK=1 и новый API_KEY.")
        self.check()
        with self.connection() as db:
            if db.execute("SELECT 1 FROM client_keys WHERE digest=?", (digest(key),)).fetchone():
                raise ValueError("Для отката нужен новый API_KEY, не выданный и не импортированный ранее.")

    def reset_password(self, username: str, password: str):
        self.check()
        encoded = HASHER.hash(password)
        with self.connection(write=True) as db:
            if not db.execute("UPDATE owner SET password_hash=? WHERE id=1 AND username=?",
                              (encoded, username)).rowcount:
                raise ValueError("Владелец с указанным именем не найден.")
            db.execute("DELETE FROM sessions")
            db.execute("DELETE FROM attempts")

    def login(self, client: str, username: str, password: str):
        with self.connection(write=True) as db:
            now = time.time()
            db.execute("DELETE FROM attempts WHERE ts<=?", (now - ATTEMPT_WINDOW,))
            if (db.execute("SELECT count(*) FROM attempts WHERE client=?", (client,)).fetchone()[0] >= ATTEMPT_LIMIT
                    or db.execute("SELECT count(*) FROM attempts").fetchone()[0] >= MAX_ATTEMPTS):
                raise limited()
            attempt = db.execute("INSERT INTO attempts(client,ts) VALUES(?,?)", (client, now)).lastrowid
            row = db.execute("SELECT username, password_hash FROM owner WHERE id=1").fetchone()
            if not row:
                raise unavailable()
        # Та же проверка Argon2 и при чужом имени; транзакция на это время закрыта.
        try:
            valid = HASHER.verify(row[1], password)
        except VerifyMismatchError:
            valid = False
        except (InvalidHashError, VerificationError):
            raise unavailable() from None
        if not valid or row[0] != username:
            return None
        token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        with self.connection(write=True) as db:
            now = time.time()
            db.execute("DELETE FROM sessions WHERE touched<=? OR created<=?",
                       (now - IDLE_SECONDS, now - ABSOLUTE_SECONDS))
            # Reset мог сменить пароль, пока Argon2 выполнялся без блокировки БД.
            if not db.execute("INSERT INTO sessions(digest,csrf,created,touched) "
                              "SELECT ?,?,?,? FROM owner WHERE id=1 AND password_hash=?",
                              (digest(token), csrf, now, now, row[1])).rowcount:
                return None
            db.execute("DELETE FROM attempts WHERE id=? AND client=?", (attempt, client))
        return token, csrf

    def session(self, token: str, touch=False):
        if not token or len(token) != 43:
            return None
        with self.connection(write=touch) as db:
            now = time.time()
            where = " WHERE digest=? AND touched>? AND created>? AND owner_id IN (SELECT id FROM owner WHERE id=1)"
            args = (digest(token), now - IDLE_SECONDS, now - ABSOLUTE_SECONDS)
            if touch:
                row = db.execute("UPDATE sessions SET touched=?" + where + " RETURNING csrf", (now, *args)).fetchone()
            else:
                row = db.execute("SELECT csrf FROM sessions" + where, args).fetchone()
            return row[0] if row else None

    def logout(self, token: str):
        with self.connection(write=True) as db:
            db.execute("DELETE FROM sessions WHERE digest=?", (digest(token),))

    def accepts_key(self, key: str) -> bool:
        with self.connection() as db:
            return db.execute("SELECT 1 FROM client_keys JOIN owner ON owner.id=client_keys.owner_id "
                              "WHERE owner.id=1 AND digest=? AND revoked_at IS NULL", (digest(key),)).fetchone() is not None

    @staticmethod
    def _insert_key(db, name, key):
        record = dict(id=secrets.token_hex(12), name=name, created_at=timestamp(), revoked_at=None)
        db.execute("INSERT INTO client_keys(id,name,digest,created_at) VALUES(:id,:name,:digest,:created_at)",
                   {**record, "digest": digest(key)})
        return record

    def create_key(self, name: str):
        key = secrets.token_urlsafe(32)
        with self.connection(write=True) as db:
            return {**self._insert_key(db, name, key), "key": key}

    def keys(self):
        with self.connection() as db:
            return [dict(row) for row in db.execute("SELECT id,name,created_at,revoked_at FROM client_keys ORDER BY rowid DESC")]

    def revoke(self, key_id: str):
        with self.connection(write=True) as db:
            if not db.execute("UPDATE client_keys SET revoked_at=coalesce(revoked_at,?) WHERE id=?",
                              (timestamp(), key_id)).rowcount:
                raise ApiError(404, "Ключ не найден.", "key_not_found")
