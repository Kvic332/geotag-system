"""Postgres/PostGIS connection pool.

Postgres is the source of truth — Redis is only ever a cache in this system
(CLAUDE.md coding standards). The pool is a module-level singleton so it
survives across warm Lambda invocations.
"""

from __future__ import annotations

import threading
from contextlib import contextmanager
from typing import Any, Iterator

import psycopg2
import psycopg2.extras
import psycopg2.pool

from shared import config
from shared.logging_utils import get_logger

logger = get_logger("geotag.db")

_pool: psycopg2.pool.ThreadedConnectionPool | None = None
_pool_lock = threading.Lock()


def get_pool() -> psycopg2.pool.ThreadedConnectionPool:
    """Return (creating on first use) the process-wide connection pool."""
    global _pool
    if _pool is None:
        with _pool_lock:
            if _pool is None:
                dsn = config.database_url()
                _pool = psycopg2.pool.ThreadedConnectionPool(
                    minconn=config.env_int("DB_POOL_MIN", 1),
                    maxconn=config.env_int("DB_POOL_MAX", 10),
                    dsn=dsn,
                    connect_timeout=config.env_int("DB_CONNECT_TIMEOUT", 10),
                    application_name="geotag",
                )
                logger.info("db_pool_created")
    return _pool


def reset_pool() -> None:
    """Drop the pool (used by tests that repoint DATABASE_URL)."""
    global _pool
    with _pool_lock:
        if _pool is not None:
            _pool.closeall()
        _pool = None


@contextmanager
def connection() -> Iterator[Any]:
    """Check a connection out of the pool; commit on success, rollback on error."""
    pool = get_pool()
    conn = pool.getconn()
    try:
        yield conn
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        pool.putconn(conn)


@contextmanager
def cursor() -> Iterator[psycopg2.extras.RealDictCursor]:
    """Dict-returning cursor inside a transaction."""
    with connection() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            yield cur


def query_all(sql: str, params: tuple[Any, ...] | list[Any] | None = None) -> list[dict[str, Any]]:
    with cursor() as cur:
        cur.execute(sql, params)
        return [dict(row) for row in cur.fetchall()]


def query_one(sql: str, params: tuple[Any, ...] | list[Any] | None = None) -> dict[str, Any] | None:
    with cursor() as cur:
        cur.execute(sql, params)
        row = cur.fetchone()
        return dict(row) if row is not None else None


def execute(sql: str, params: tuple[Any, ...] | list[Any] | None = None) -> int:
    with cursor() as cur:
        cur.execute(sql, params)
        return cur.rowcount
