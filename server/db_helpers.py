"""MySQL helpers + DB-backed config — extracted from lead_server.py
2026-06-28 as refactor slice #6j. The upstream of all future page-
builder carves that need db access.

What's here:
  - _db() / db_read / db_write / db_insert: thin pymysql wrappers
    returning None / False / lastrowid on failure (silent — the
    callers in lead_server have historically depended on this
    fail-soft behavior).
  - _load_cfg() / _CFG / cfg(key, default=''): config table reader.
    DB wins; if missing OR empty, falls back to os.environ; finally
    the default. The DB-wins-on-empty-string-fallback rule is load-
    bearing for the voice stack — GLOBUS_VOICE_LLM_SECRET lives in
    BOTH config table and env, and cfg() must prefer DB.
    cfg() does NOT hot-reload. _load_cfg() runs exactly once, inside
    configure(); cfg() only reads the in-memory _CFG dict. A config
    edit reaches a long-running process (the web server) only after it
    restarts; per-run cron scripts see it on their next run because
    they configure() afresh.

Usage in lead_server.py:
    import db_helpers
    db_helpers.configure(db_cfg=DB_CFG)
    from db_helpers import db_read, db_write, db_insert, cfg

configure() loads the config table immediately so cfg() calls at
module top level (STRIPE_SK = cfg("STRIPE_SECRET_KEY") etc.) work
right after the import.
"""
from __future__ import annotations
import os
import pymysql


_DB_CFG: dict = {}
_CFG: dict = {}


def configure(*, db_cfg):
    """Wire in MySQL connection params + load the config table.
    Must be called from lead_server at startup BEFORE any module-level
    cfg() / db_read() callsite runs."""
    global _DB_CFG, _CFG
    if not isinstance(db_cfg, dict):
        raise TypeError("db_cfg must be a dict (DB_CFG-shaped)")
    _DB_CFG = dict(db_cfg)
    _CFG = _load_cfg()


def db_cfg_from_env():
    """The MySQL connection params from the environment
    (DB_HOST/PORT/USER/PASSWORD/NAME). The one place the defaults live."""
    return {
        "host": os.environ.get("DB_HOST", "127.0.0.1"),
        "port": int(os.environ.get("DB_PORT", "3306")),
        "user": os.environ.get("DB_USER", "globus"),
        "password": os.environ.get("DB_PASSWORD", ""),
        "database": os.environ.get("DB_NAME", "globus"),
    }


def ensure_configured():
    """configure() from the environment unless something already did.
    -> True if it configured.

    For a process that did not start as the web server (a cron script):
    it gets the same db params and config-table snapshot the server would.
    Idempotent on purpose -- inside the web process, or after a script that
    configured itself, it is a no-op, so the config table is never re-read
    behind the caller's back."""
    if _DB_CFG:
        return False
    configure(db_cfg=db_cfg_from_env())
    return True


def _db():
    return pymysql.connect(charset="utf8mb4", autocommit=True, connect_timeout=5,
                           cursorclass=pymysql.cursors.DictCursor, **_DB_CFG)


def db_read(sql, params=()):
    try:
        conn = _db()
        try:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                return cur.fetchall()
        finally:
            conn.close()
    except Exception:
        return None


def db_write(sql, params=()):
    try:
        conn = _db()
        try:
            with conn.cursor() as cur:
                cur.execute(sql, params)
            return True
        finally:
            conn.close()
    except Exception:
        return False


def db_insert(sql, params=()):
    try:
        conn = _db()
        try:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                return cur.lastrowid
        finally:
            conn.close()
    except Exception:
        return None


def _load_cfg():
    rows = db_read("SELECT name, value FROM config")
    return {r["name"]: r["value"] for r in rows} if rows else {}


def cfg(key, default=""):
    v = _CFG.get(key)
    if v is None or v == "":
        v = os.environ.get(key, default)
    return v if v is not None else default
