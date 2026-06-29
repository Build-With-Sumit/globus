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
    BOTH config table and env, and cfg() must prefer DB so that
    rotating the secret in DB takes immediate effect without redeploy.

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
