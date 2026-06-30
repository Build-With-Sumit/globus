"""Browser-extension bridge ingest — WhatsApp Web + Microsoft Teams.

Both bridges follow the same shape:
  - The user installs an open-source Chrome extension once
    (Build-With-Sumit/whatsapp-bridge on GitHub; one install, two
    sources — same extension scrapes both WA Web and Teams personal).
  - The extension pairs with the Globus server by HMAC token (90-day
    TTL, member-scoped) — minted server-side from /members/whatsapp
    and pasted into the extension popup.
  - The extension POSTs scraped messages to /api/globus/whatsapp/ingest
    or /api/globus/teams/ingest in JSON batches (up to 500 messages,
    4 MB).
  - This module's ingest helpers dedupe by fingerprint and bulk-insert
    into globus_whatsapp_messages / globus_teams_messages. The chat
    orchestrator's search_whatsapp / search_telegram tools read from
    those tables — no separate sync worker.

Token format mirrors voice_helpers: `email|expires_unix|hex_hmac`,
HMAC-SHA256 keyed on the same SESSION_SECRET. One token covers BOTH
sources (the extension uses the same auth header on either endpoint).
"""
from __future__ import annotations
import hashlib
import hmac
import time
from datetime import datetime

from db_helpers import db_write


# Module config — set once via configure() from globus_server.
_SESSION_SECRET: bytes = b""

# 90 days — extension pairs once, forgets about it. Long enough that the
# user isn't re-pasting tokens monthly; short enough that a leaked
# token doesn't grant forever access.
WHATSAPP_TOKEN_TTL_SEC = 90 * 24 * 3600

# Ingest body cap — a batch of 500 messages with full bodies can hit
# ~4 MB. Beyond that we'd risk OOM on the server.
BRIDGE_INGEST_MAX_BYTES = 4 * 1024 * 1024
BRIDGE_INGEST_MAX_MESSAGES = 500


def configure(*, session_secret):
    """Wire the SESSION_SECRET (bytes, same HMAC key as cookies + voice)."""
    global _SESSION_SECRET
    if not isinstance(session_secret, (bytes, bytearray)):
        raise TypeError("session_secret must be bytes")
    _SESSION_SECRET = bytes(session_secret)


# ─────────────────────────────────────────────────────────────────────
# Token mint + verify — one token works for BOTH /whatsapp/ingest and
# /teams/ingest endpoints (the extension handles both sources).
# ─────────────────────────────────────────────────────────────────────

def whatsapp_token_make(email):
    """Mint a fresh 90-day HMAC token for the Chrome extension."""
    expires = int(time.time()) + WHATSAPP_TOKEN_TTL_SEC
    payload = f"{email.lower()}|{expires}"
    sig = hmac.new(_SESSION_SECRET, payload.encode("utf-8"),
                   hashlib.sha256).hexdigest()
    return f"{payload}|{sig}"


def whatsapp_token_verify(token):
    """Return the member email if the token is valid + unexpired, else None."""
    if not token or "|" not in token:
        return None
    try:
        email, expires_str, sig = token.rsplit("|", 2)
        expires = int(expires_str)
    except (ValueError, AttributeError):
        return None
    if expires < int(time.time()):
        return None
    expected = hmac.new(_SESSION_SECRET,
                        f"{email}|{expires_str}".encode("utf-8"),
                        hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expected):
        return None
    return email.lower()


# ─────────────────────────────────────────────────────────────────────
# WhatsApp ingest — bulk insert into globus_whatsapp_messages with
# fingerprint-based dedup. Returns (saved, total).
# ─────────────────────────────────────────────────────────────────────

def whatsapp_ingest_messages(member, messages):
    """Bulk-insert WA Web messages, deduped by (member, fingerprint).
    Caller is responsible for member auth — pass the verified email."""
    saved = 0
    for m in messages or []:
        if not isinstance(m, dict):
            continue
        chat = (m.get("chat") or "").strip()[:240]
        sender = (m.get("sender") or "").strip()[:240]
        body = (m.get("body") or "")
        if not chat or not body:
            continue
        body = body[:6000]
        direction = m.get("direction") or "unknown"
        if direction not in ("in", "out", "unknown"):
            direction = "unknown"
        wa_ts = (m.get("ts") or "")[:60]
        fp = hashlib.sha256(
            f"{chat}|{wa_ts}|{sender}|{body[:200]}".encode("utf-8")
        ).hexdigest()
        try:
            db_write(
                "INSERT INTO globus_whatsapp_messages "
                "(member_email, chat_name, sender, body, direction, wa_ts, "
                " fingerprint) VALUES (%s,%s,%s,%s,%s,%s,%s) "
                "ON DUPLICATE KEY UPDATE body=VALUES(body), "
                "direction=VALUES(direction)",
                (member, chat, sender, body, direction, wa_ts, fp))
            saved += 1
        except Exception as e:
            print(f"[wa-ingest] {member} insert failed: "
                  f"{type(e).__name__}: {e}", flush=True)
    return saved, len(messages or [])


# ─────────────────────────────────────────────────────────────────────
# Teams ingest — same shape, into globus_teams_messages. DOM scraper
# doesn't have real Graph IDs, so ms_chat_id/ms_message_id are
# synthetic hashes derived from chat name + payload.
# ─────────────────────────────────────────────────────────────────────

def teams_ingest_messages(member, messages):
    """Bulk-insert Teams messages scraped by the Chrome extension.
    Deduped by (member, fingerprint) — INSERT IGNORE drops collisions.
    Returns (saved, total)."""
    saved = 0
    for m in messages or []:
        if not isinstance(m, dict):
            continue
        chat = (m.get("chat") or "").strip()[:480]
        sender = (m.get("sender") or "").strip()[:240]
        body = (m.get("body") or "")
        if not chat or not body:
            continue
        body = body[:6000]
        chat_type = (m.get("chat_type") or "unknown")[:32]
        ts_raw = (m.get("ts") or "")[:80]
        ms_ts = None
        if ts_raw:
            try:
                dt = datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
                ms_ts = dt.strftime("%Y-%m-%d %H:%M:%S")
            except Exception:
                ms_ts = None
        mid_raw = (m.get("mid") or "").strip()[:64]
        chat_id_synth = hashlib.sha256(
            f"teams-live|{member}|{chat}".encode("utf-8")
        ).hexdigest()[:64]
        fp = hashlib.sha256(
            f"{chat}|{mid_raw or ts_raw}|{sender}|{body[:200]}".encode("utf-8")
        ).hexdigest()
        msg_id_synth = mid_raw or fp[:64]
        try:
            db_write(
                "INSERT IGNORE INTO globus_teams_messages "
                "(member_email, ms_chat_id, ms_message_id, chat_name, "
                " chat_type, sender, sender_user_id, body, body_type, "
                " ms_ts, fingerprint) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (member, chat_id_synth, msg_id_synth, chat, chat_type,
                 sender, "", body, "text", ms_ts, fp))
            saved += 1
        except Exception as e:
            print(f"[teams-ingest] {member} insert failed: "
                  f"{type(e).__name__}: {e}", flush=True)
    return saved, len(messages or [])
