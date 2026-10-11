"""Generic outbound notification relay — service -> Telegram.

Globus can act as a small Telegram gateway: any service you run (a monitoring
tool, a CRM, a cron job) can POST a finished, ready-to-send alert to
`/api/notify` with its own relay key, and Globus forwards it through the
member's audited Telegram bot (see telegram_bot.py). This module owns ONLY the
relay-key auth + registry + a per-key rate limit — it holds ZERO domain
knowledge about any service. The actual send goes through
`telegram_bot.send_via_member_bot` (default-deny on the bot's
`allowed_send_chats`, every attempt audited in `globus_telegram_bot_sends`).

Keys are stored as SHA-256 hashes in `globus_relay_keys`; the plaintext is
shown ONCE at creation (`sk-relay-` + 48 hex) and handed to the service
out-of-band (never committed, never logged). A key maps to
(service_name, member_email) and may optionally be:
  - chat-scoped   (allowed_chats): can ONLY send to these chat_ids — tighter
                  than the bot's allowed_send_chats, shrinks a leaked key's
                  blast radius.
  - time-limited  (expires_at): rejected once expired.
  - labelled      (label): a tag to tell concurrent keys apart for rotation.

No keys exist until an operator mints one (`scripts/relay_key.py create`), so
a fresh install answers every /api/notify call with 401.

Schema: `schema/migrations/0003_relay_keys.sql` (also in globus_schema.sql).
"""
from __future__ import annotations
import hashlib
import json
import secrets
import threading
import time
from collections import deque

from db_helpers import db_read, db_write

# Per-key sliding-window rate limit (in-memory — the server is a single,
# threaded process; guarded by a lock). Stops a leaked/misbehaving key from
# flooding Telegram. Resets on restart.
RELAY_RATE_MAX = 60        # messages ...
RELAY_RATE_WINDOW = 60.0   # ... per this many seconds, per key
_rate_buckets: dict = {}
_rate_lock = threading.Lock()


def _hash(key: str) -> str:
    return hashlib.sha256((key or "").encode("utf-8")).hexdigest()


def create_relay_key(service_name: str, member_email: str,
                     allowed_chats=None, expires_at=None, label=None) -> str:
    """Mint a relay key, store its hash, and return the plaintext ONCE.

    allowed_chats: optional list of chat_ids this key may send to (stored as
      strings). None => fall back to the bot's own allowed_send_chats.
    expires_at: optional 'YYYY-MM-DD HH:MM:SS' UTC string; None => never expires.
    label: optional tag (useful when a service holds >1 key during rotation).

    VERIFIES the row landed — db_write is fail-soft (swallows errors, returns
    False), so without this a DB failure would still return a valid-looking but
    dead key. Raises RuntimeError if the INSERT did not persist."""
    key = "sk-relay-" + secrets.token_hex(24)
    kh = _hash(key)
    # Distinguish None (no scope -> fall back to the bot's allowed_send_chats)
    # from an EMPTY list (scope to no chats -> deny everything). `if
    # allowed_chats` would collapse [] to None (unscoped) — the opposite of
    # intent.
    chats_json = (json.dumps([str(c) for c in allowed_chats])
                  if allowed_chats is not None else None)
    db_write(
        "INSERT INTO globus_relay_keys "
        "(key_hash, service_name, member_email, allowed_chats, expires_at, "
        " label, status) VALUES (%s,%s,%s,%s,%s,%s,'active')",
        (kh, service_name, member_email, chats_json, expires_at, label))
    if not db_read("SELECT id FROM globus_relay_keys WHERE key_hash=%s", (kh,)):
        raise RuntimeError(
            "relay key INSERT did not persist (db_write is fail-soft) — check "
            "the DB credentials and that globus_relay_keys exists "
            "(python scripts/migrate.py up)")
    return key


def revoke_relay_key(service_name: str = None, key: str = None,
                     key_id: int = None) -> None:
    """Revoke a SINGLE key (by plaintext `key` or `key_id`) or ALL keys of a
    `service_name`. Prefer single-key revoke for rotation so siblings survive."""
    if key:
        db_write("UPDATE globus_relay_keys SET status='revoked' "
                 "WHERE key_hash=%s", (_hash(key),))
    elif key_id is not None:
        db_write("UPDATE globus_relay_keys SET status='revoked' WHERE id=%s",
                 (key_id,))
    elif service_name:
        db_write("UPDATE globus_relay_keys SET status='revoked' "
                 "WHERE service_name=%s", (service_name,))
    else:
        raise ValueError("revoke_relay_key needs key, key_id, or service_name")


def list_relay_keys(member_email: str = None) -> list:
    """Key metadata for operators (never the key or its hash)."""
    sql = ("SELECT id, service_name, member_email, status, label, "
           "allowed_chats, expires_at, created_at, last_used_at "
           "FROM globus_relay_keys ")
    args: tuple = ()
    if member_email:
        sql += "WHERE member_email=%s "
        args = (member_email,)
    return db_read(sql + "ORDER BY id", args) or []


def relay_key_verify(key: str):
    """Return a dict for an ACTIVE, UNEXPIRED key, else None. Bumps last_used_at.

    Dict: {id, service_name, member_email, allowed_chats(list[str]|None)}."""
    if not key:
        return None
    rows = db_read(
        "SELECT id, service_name, member_email, allowed_chats, status, "
        "  (expires_at IS NOT NULL AND expires_at <= NOW()) AS expired "
        "FROM globus_relay_keys WHERE key_hash=%s", (_hash(key),))
    if not rows:
        return None
    r = rows[0]
    if r.get("status") != "active" or r.get("expired"):
        return None
    db_write("UPDATE globus_relay_keys SET last_used_at=NOW() WHERE id=%s",
             (r["id"],))
    ac = r.get("allowed_chats")
    if isinstance(ac, str):
        try:
            ac = json.loads(ac)
        except Exception:
            ac = None
    # NULL or a corrupt non-list scalar -> unscoped (None); an EMPTY list stays
    # an empty allow-list (deny every chat), not collapsed to unscoped. `if ac`
    # would turn [] into None and also raise 'for c in <scalar>' on a
    # hand-edited row.
    if not isinstance(ac, list):
        ac = None
    return {"id": r["id"], "service_name": r["service_name"],
            "member_email": r["member_email"],
            "allowed_chats": ([str(c) for c in ac] if ac is not None else None)}


def rate_limit_ok(key_id) -> bool:
    """Per-key sliding-window limiter. True if within budget, False if over."""
    now = time.time()
    cutoff = now - RELAY_RATE_WINDOW
    bucket = str(key_id)
    with _rate_lock:
        dq = _rate_buckets.get(bucket)
        if dq is None:
            dq = deque()
            _rate_buckets[bucket] = dq
        while dq and dq[0] < cutoff:
            dq.popleft()
        if len(dq) >= RELAY_RATE_MAX:
            return False
        dq.append(now)
        return True


def handle_notify(headers, data, send):
    """The whole /api/notify contract, separate from the HTTP layer so it is
    testable. Returns (status_code, json_body).

    headers: mapping with .get(); data: the parsed JSON body (or None);
    send: callable(email=, chat_id=, text=, parse_mode=, initiator=) -> dict
    shaped like telegram_bot.send_via_member_bot's result.

    Auth is `Authorization: Bearer <relay-key>` or `X-Relay-Key`. The bot's
    allowed_send_chats still gates the target, so a leaked key can only reach
    chats that bot already permits."""
    auth = headers.get("Authorization", "") or ""
    key = (auth[7:].strip() if auth.startswith("Bearer ")
           else (headers.get("X-Relay-Key", "") or "").strip())
    info = relay_key_verify(key)
    if not info:
        return 401, {"ok": False, "error": "unauthorized"}
    if not rate_limit_ok(info["id"]):
        return 429, {"ok": False, "error": "rate limit exceeded"}
    if not isinstance(data, dict):
        return 400, {"ok": False, "error":
                     "body must be JSON (send Content-Type: application/json)"}
    chat_id = data.get("chat_id")
    text = data.get("text")
    if not chat_id or not text:
        return 400, {"ok": False, "error": "chat_id and text required"}
    # Chat-scoped key: if it pins allowed_chats, the target must be in it (ON
    # TOP of the bot's own allowed_send_chats default-deny).
    scoped = info.get("allowed_chats")
    if scoped is not None and str(chat_id) not in set(scoped):
        # A 403 never reaches the send helper, so it leaves no audit row —
        # log here so scoped-key probing has a server-side trace.
        print(f"[relay] service={info['service_name']} DENIED chat={chat_id} "
              f"(not in key scope)", flush=True)
        return 403, {"ok": False, "error": "chat not allowed"}
    text = str(text)
    # Telegram's 4096 cap is UTF-16 code units, not code points — measure the
    # same way so emoji-heavy text gets a clean 413 here, not a 502 from
    # Telegram.
    if len(text.encode("utf-16-le")) // 2 > 4096:
        return 413, {"ok": False, "error": "text exceeds 4096 chars"}
    res = send(email=info["member_email"], chat_id=chat_id, text=text,
               parse_mode=data.get("parse_mode"),
               initiator=f"relay:{info['service_name']}")
    print(f"[relay] service={info['service_name']} "
          f"member={info['member_email']} chat={chat_id} "
          f"ok={res.get('ok')} err={res.get('error')}", flush=True)
    return (200 if res.get("ok") else 502), res
