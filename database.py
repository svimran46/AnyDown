"""Database access and migrations for AnyDown (PostgreSQL with SQLite fallback)."""

from __future__ import annotations

import logging
import os
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger("anydown.database")

DATABASE_URL = os.getenv("DATABASE_URL", "").strip()

# Normalize postgres:// to postgresql:// if needed
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = "postgresql://" + DATABASE_URL[len("postgres://"):]

_is_postgres = bool(DATABASE_URL and DATABASE_URL.startswith("postgresql://"))


def _get_connection():
    if _is_postgres:
        import psycopg
        from psycopg.rows import dict_row
        return psycopg.connect(DATABASE_URL, row_factory=dict_row)
    else:
        # Default local development / test database
        db_path = os.getenv("SQLITE_DB_PATH", os.path.join(os.path.dirname(__file__), "anydown.db"))
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        return conn


def init_db() -> None:
    """Run migrations to ensure tables exist."""
    conn = _get_connection()
    try:
        if _is_postgres:
            with conn.cursor() as cur:
                migration_file = os.path.join(os.path.dirname(__file__), "migrations", "001_init.sql")
                if os.path.isfile(migration_file):
                    with open(migration_file, "r", encoding="utf-8") as f:
                        cur.execute(f.read())
                else:
                    cur.execute("""
                        CREATE TABLE IF NOT EXISTS users (
                            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                            google_sub TEXT UNIQUE NOT NULL,
                            email TEXT NOT NULL,
                            email_verified BOOLEAN NOT NULL DEFAULT false,
                            name TEXT,
                            avatar_url TEXT,
                            status TEXT NOT NULL DEFAULT 'active',
                            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                            last_login_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                        );
                        CREATE TABLE IF NOT EXISTS sessions (
                            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                            user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                            token_hash TEXT UNIQUE NOT NULL,
                            expires_at TIMESTAMPTZ NOT NULL,
                            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                            last_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                        );
                        CREATE INDEX IF NOT EXISTS sessions_user_id_idx ON sessions(user_id);
                        CREATE INDEX IF NOT EXISTS sessions_expires_at_idx ON sessions(expires_at);
                        CREATE INDEX IF NOT EXISTS sessions_token_hash_idx ON sessions(token_hash);
                        CREATE TABLE IF NOT EXISTS download_events (
                            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                            user_id UUID REFERENCES users(id) ON DELETE SET NULL,
                            provider TEXT,
                            requested_height INTEGER,
                            authenticated BOOLEAN NOT NULL DEFAULT false,
                            status TEXT NOT NULL DEFAULT 'started',
                            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                        );
                    """)
                conn.commit()
            logger.info("Initialized PostgreSQL database tables successfully.")
        else:
            with conn:
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS users (
                        id TEXT PRIMARY KEY,
                        google_sub TEXT UNIQUE NOT NULL,
                        email TEXT NOT NULL,
                        email_verified INTEGER NOT NULL DEFAULT 0,
                        name TEXT,
                        avatar_url TEXT,
                        status TEXT NOT NULL DEFAULT 'active',
                        created_at TEXT NOT NULL,
                        last_login_at TEXT NOT NULL
                    );
                """)
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS sessions (
                        id TEXT PRIMARY KEY,
                        user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                        token_hash TEXT UNIQUE NOT NULL,
                        expires_at TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        last_seen_at TEXT NOT NULL
                    );
                """)
                conn.execute("CREATE INDEX IF NOT EXISTS sessions_user_id_idx ON sessions(user_id);")
                conn.execute("CREATE INDEX IF NOT EXISTS sessions_expires_at_idx ON sessions(expires_at);")
                conn.execute("CREATE INDEX IF NOT EXISTS sessions_token_hash_idx ON sessions(token_hash);")
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS download_events (
                        id TEXT PRIMARY KEY,
                        user_id TEXT,
                        provider TEXT,
                        requested_height INTEGER,
                        authenticated INTEGER NOT NULL DEFAULT 0,
                        status TEXT NOT NULL DEFAULT 'started',
                        created_at TEXT NOT NULL
                    );
                """)
            logger.info("Initialized local SQLite database tables successfully.")
    finally:
        conn.close()


def get_user_by_google_sub(google_sub: str) -> dict[str, Any] | None:
    conn = _get_connection()
    try:
        if _is_postgres:
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM users WHERE google_sub = %s", (google_sub,))
                row = cur.fetchone()
                return dict(row) if row else None
        else:
            cur = conn.cursor()
            cur.execute("SELECT * FROM users WHERE google_sub = ?", (google_sub,))
            row = cur.fetchone()
            if not row:
                return None
            d = dict(row)
            d["email_verified"] = bool(d.get("email_verified"))
            return d
    finally:
        conn.close()


def upsert_user(
    google_sub: str,
    email: str,
    email_verified: bool,
    name: str | None = None,
    avatar_url: str | None = None,
) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    conn = _get_connection()
    try:
        if _is_postgres:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO users (google_sub, email, email_verified, name, avatar_url, last_login_at)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    ON CONFLICT (google_sub) DO UPDATE SET
                        email = EXCLUDED.email,
                        email_verified = EXCLUDED.email_verified,
                        name = COALESCE(EXCLUDED.name, users.name),
                        avatar_url = COALESCE(EXCLUDED.avatar_url, users.avatar_url),
                        last_login_at = EXCLUDED.last_login_at
                    RETURNING *;
                    """,
                    (google_sub, email, email_verified, name, avatar_url, now),
                )
                row = cur.fetchone()
                conn.commit()
                return dict(row)
        else:
            with conn:
                cur = conn.cursor()
                cur.execute("SELECT * FROM users WHERE google_sub = ?", (google_sub,))
                existing = cur.fetchone()
                now_iso = now.isoformat()
                if existing:
                    user_id = existing["id"]
                    cur.execute(
                        """
                        UPDATE users
                        SET email = ?, email_verified = ?, name = COALESCE(?, name),
                            avatar_url = COALESCE(?, avatar_url), last_login_at = ?
                        WHERE id = ?;
                        """,
                        (email, 1 if email_verified else 0, name, avatar_url, now_iso, user_id),
                    )
                else:
                    user_id = str(uuid.uuid4())
                    cur.execute(
                        """
                        INSERT INTO users (id, google_sub, email, email_verified, name, avatar_url, status, created_at, last_login_at)
                        VALUES (?, ?, ?, ?, ?, ?, 'active', ?, ?);
                        """,
                        (user_id, google_sub, email, 1 if email_verified else 0, name, avatar_url, now_iso, now_iso),
                    )
                cur.execute("SELECT * FROM users WHERE id = ?", (user_id,))
                row = cur.fetchone()
                d = dict(row)
                d["email_verified"] = bool(d.get("email_verified"))
                return d
    finally:
        conn.close()


def create_session(user_id: str, token_hash: str, expires_at: datetime) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    conn = _get_connection()
    try:
        if _is_postgres:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO sessions (user_id, token_hash, expires_at, created_at, last_seen_at)
                    VALUES (%s, %s, %s, %s, %s)
                    RETURNING *;
                    """,
                    (user_id, token_hash, expires_at, now, now),
                )
                row = cur.fetchone()
                conn.commit()
                return dict(row)
        else:
            session_id = str(uuid.uuid4())
            with conn:
                cur = conn.cursor()
                cur.execute(
                    """
                    INSERT INTO sessions (id, user_id, token_hash, expires_at, created_at, last_seen_at)
                    VALUES (?, ?, ?, ?, ?, ?);
                    """,
                    (session_id, user_id, token_hash, expires_at.isoformat(), now.isoformat(), now.isoformat()),
                )
                cur.execute("SELECT * FROM sessions WHERE id = ?", (session_id,))
                return dict(cur.fetchone())
    finally:
        conn.close()


def get_session_user(token_hash: str) -> dict[str, Any] | None:
    now = datetime.now(timezone.utc)
    conn = _get_connection()
    try:
        if _is_postgres:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT u.*, s.id AS session_id, s.expires_at AS session_expires_at
                    FROM sessions s
                    JOIN users u ON s.user_id = u.id
                    WHERE s.token_hash = %s AND s.expires_at > %s AND u.status = 'active';
                    """,
                    (token_hash, now),
                )
                row = cur.fetchone()
                if not row:
                    return None
                # Update last_seen_at
                cur.execute("UPDATE sessions SET last_seen_at = %s WHERE id = %s", (now, row["session_id"]))
                conn.commit()
                user_dict = dict(row)
                del user_dict["session_id"]
                del user_dict["session_expires_at"]
                return user_dict
        else:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT u.*, s.id AS session_id, s.expires_at AS session_expires_at
                FROM sessions s
                JOIN users u ON s.user_id = u.id
                WHERE s.token_hash = ? AND s.expires_at > ? AND u.status = 'active';
                """,
                (token_hash, now.isoformat()),
            )
            row = cur.fetchone()
            if not row:
                return None
            user_dict = dict(row)
            session_id = user_dict.pop("session_id")
            user_dict.pop("session_expires_at", None)
            with conn:
                conn.execute("UPDATE sessions SET last_seen_at = ? WHERE id = ?", (now.isoformat(), session_id))
            user_dict["email_verified"] = bool(user_dict.get("email_verified"))
            return user_dict
    finally:
        conn.close()


def delete_session(token_hash: str) -> bool:
    conn = _get_connection()
    try:
        if _is_postgres:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM sessions WHERE token_hash = %s", (token_hash,))
                affected = cur.rowcount
                conn.commit()
                return affected > 0
        else:
            with conn:
                cur = conn.cursor()
                cur.execute("DELETE FROM sessions WHERE token_hash = ?", (token_hash,))
                return cur.rowcount > 0
    finally:
        conn.close()


def record_download_event(
    user_id: str | None,
    provider: str | None,
    requested_height: int | None,
    authenticated: bool,
    status: str = "started",
) -> None:
    now = datetime.now(timezone.utc)
    try:
        conn = _get_connection()
        try:
            if _is_postgres:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO download_events (user_id, provider, requested_height, authenticated, status, created_at)
                        VALUES (%s, %s, %s, %s, %s, %s);
                        """,
                        (user_id, provider, requested_height, authenticated, status, now),
                    )
                    conn.commit()
            else:
                event_id = str(uuid.uuid4())
                with conn:
                    conn.execute(
                        """
                        INSERT INTO download_events (id, user_id, provider, requested_height, authenticated, status, created_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?);
                        """,
                        (event_id, user_id, provider, requested_height, 1 if authenticated else 0, status, now.isoformat()),
                    )
        finally:
            conn.close()
    except Exception as exc:
        logger.warning("Could not record download event: %s", exc)
