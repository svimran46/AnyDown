"""Database access and migrations for AnyDown (PostgreSQL with SQLite fallback)."""

from __future__ import annotations

import logging
import os
import sqlite3
import uuid
from datetime import datetime, timezone

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
                        CREATE TABLE IF NOT EXISTS download_events (
                            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                            provider TEXT,
                            requested_height INTEGER,
                            status TEXT NOT NULL DEFAULT 'started',
                            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                        );
                    """)
                conn.commit()
            logger.info("Initialized PostgreSQL database tables successfully.")
        else:
            with conn:
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS download_events (
                        id TEXT PRIMARY KEY,
                        provider TEXT,
                        requested_height INTEGER,
                        status TEXT NOT NULL DEFAULT 'started',
                        created_at TEXT NOT NULL
                    );
                """)
            logger.info("Initialized local SQLite database tables successfully.")
    finally:
        conn.close()


def record_download_event(
    provider: str | None,
    requested_height: int | None,
    status: str = "started",
) -> None:
    """Append-only log of download requests. Best effort: never raises."""
    now = datetime.now(timezone.utc)
    try:
        conn = _get_connection()
        try:
            if _is_postgres:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO download_events (provider, requested_height, status, created_at)
                        VALUES (%s, %s, %s, %s);
                        """,
                        (provider, requested_height, status, now),
                    )
                    conn.commit()
            else:
                event_id = str(uuid.uuid4())
                with conn:
                    conn.execute(
                        """
                        INSERT INTO download_events (id, provider, requested_height, status, created_at)
                        VALUES (?, ?, ?, ?, ?);
                        """,
                        (event_id, provider, requested_height, status, now.isoformat()),
                    )
        finally:
            conn.close()
    except Exception as exc:
        logger.warning("Could not record download event: %s", exc)
