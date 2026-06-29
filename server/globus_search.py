"""Globus LLM search tools — extracted from lead_server.py 2026-06-28
as refactor slice #6x. Pure DB-read tools the LLM invokes via the
tool-loop. Each one is per-member-scoped via the `email` arg (defense-
in-depth on top of the session-cookie-only email source the chat
endpoint already enforces).

What's here (4 pure-DB tools):
  - globus_search_files(email, query, limit): filename LIKE +
    token-OR ranking over globus_vault_files.
  - globus_search_content(email, query, limit): grep INSIDE extracted
    file content (scans 500 newest sheets/docs/presentations first,
    200K-char read cap per file, snippet around first hit).
  - globus_search_telegram(email, query, ...): WHERE/LIKE filter over
    globus_telegram_messages (Telethon daemon ingest).
  - globus_search_whatsapp(email, query, ...): same shape as telegram
    but over globus_whatsapp_messages (Chrome-extension ingest).

NOT here (left in lead_server because of cross-deps):
  - globus_read_file: needs Drive download/export helpers
  - globus_list_recent_emails: needs globus_freshen_gmail (sync deps)
  - globus_send_telegram_via_bot: needs urllib + bot perms checks

Module deps: db_read (db_helpers), re + os (stdlib). No DB writes —
pure read path.
"""
from __future__ import annotations
import os
import re
from db_helpers import db_read


# Constant ------------------------------------------------------------------
GLOBUS_SEARCH_LIMIT_MAX = 20           # protect against gigantic result sets


# ----------------------------------------------------------------------------
def globus_search_files(email, query, limit=5):
    """Search this member's indexed files by filename (LIKE match).
    Returns up to `limit` rows ordered by extracted-first then most-recent.
    Each row is a dict the LLM can pick from before calling read_file."""
    if not email or not query:
        return []
    try:
        limit = max(1, min(int(limit or 5), GLOBUS_SEARCH_LIMIT_MAX))
    except (TypeError, ValueError):
        limit = 5
    raw_q = str(query).strip()
    # Token-OR search: split the query into 1-4 meaningful tokens, find
    # files whose filename contains ANY token, rank by number of tokens
    # matched (descending). Falls back to substring match if tokenization
    # produced only one token.
    #
    # Why: substring-only ("LIKE %EmpMonitor pipeline%") needed the exact
    # phrase. The user's question "EmpMonitor pipeline" returned 0 hits
    # because no file has those words ADJACENT — but plenty of files
    # contain both words elsewhere. Token-OR + rank-by-hit-count surfaces
    # them properly while still preferring multi-word matches.
    tokens = [t for t in re.findall(r"[A-Za-z0-9]{3,}", raw_q) if len(t) >= 3]
    # Dedupe + cap at 4 (avoid pathological many-token queries)
    seen = set(); uniq_tokens = []
    for t in tokens:
        tl = t.lower()
        if tl not in seen:
            seen.add(tl); uniq_tokens.append(t)
        if len(uniq_tokens) >= 4: break

    if len(uniq_tokens) <= 1:
        # Single token / empty — substring match as before.
        q = f"%{raw_q}%"
        rows = db_read(
            "SELECT id, filename, mime_type, modified_at, extracted_chars, "
            "       extracted, source_type "
            "FROM globus_vault_files "
            "WHERE email=%s AND filename LIKE %s "
            "ORDER BY extracted DESC, "
            "  CASE WHEN mime_type IN ("
            "    'application/vnd.google-apps.spreadsheet', "
            "    'application/vnd.google-apps.document', "
            "    'application/vnd.google-apps.presentation') THEN 0 "
            "  ELSE 1 END, "
            "  extracted_chars DESC, "
            "  modified_at DESC "
            "LIMIT %s",
            (email, q, limit))
    else:
        # Multi-token OR with hit-count ranking.
        like_parts = " OR ".join(["filename LIKE %s"] * len(uniq_tokens))
        rank_parts = " + ".join(
            [f"(CASE WHEN filename LIKE %s THEN 1 ELSE 0 END)"]
            * len(uniq_tokens))
        like_args = [f"%{t}%" for t in uniq_tokens]
        sql = (
            "SELECT id, filename, mime_type, modified_at, extracted_chars, "
            "       extracted, source_type "
            "FROM globus_vault_files "
            f"WHERE email=%s AND ({like_parts}) "
            "ORDER BY extracted DESC, "
            f"  ({rank_parts}) DESC, "
            "  CASE WHEN mime_type IN ("
            "    'application/vnd.google-apps.spreadsheet', "
            "    'application/vnd.google-apps.document', "
            "    'application/vnd.google-apps.presentation') THEN 0 "
            "  ELSE 1 END, "
            "  extracted_chars DESC, "
            "  modified_at DESC "
            "LIMIT %s")
        rows = db_read(sql, ([email] + like_args + like_args + [limit]))
    out = []
    for r in (rows or []):
        out.append({
            "file_id":      r["id"],
            "filename":     r["filename"],
            "mime_type":    r["mime_type"],
            "source":       r["source_type"],
            "modified_at":  str(r["modified_at"]) if r["modified_at"] else None,
            "char_count":   int(r["extracted_chars"] or 0),
            "has_content":  bool(r["extracted"]),
        })
    return out

# ----------------------------------------------------------------------------
def globus_search_content(email, query, limit=5):
    """Grep INSIDE extracted file content for a keyword. Slower than
    search_files (filename-only) but finds matches that live in the
    content — e.g. 'June' / 'Q3 2026' numbers inside a yearly sheet.

    Scans the 500 newest authored files (Sheets/Docs first) for the
    member, caps per-file read at 200K chars, returns up to `limit`
    matches with a snippet around the first hit so the LLM can decide
    whether to read_file."""
    if not email:
        return []
    q = str(query or "").strip()
    if len(q) < 3:
        return [{"error": "query must be at least 3 characters"}]
    try:
        limit = max(1, min(int(limit or 5), 10))
    except (TypeError, ValueError):
        limit = 5
    rows = db_read(
        "SELECT id, filename, mime_type, modified_at, extracted_chars, "
        "       extracted_path "
        "FROM globus_vault_files "
        "WHERE email=%s AND extracted=1 AND extracted_path IS NOT NULL "
        "ORDER BY "
        "  CASE WHEN mime_type IN ("
        "    'application/vnd.google-apps.spreadsheet', "
        "    'application/vnd.google-apps.document', "
        "    'application/vnd.google-apps.presentation') THEN 0 "
        "  ELSE 1 END, "
        "  modified_at DESC "
        "LIMIT 500",
        (email,))
    q_lower = q.lower()
    out = []
    scanned = 0
    for r in (rows or []):
        if len(out) >= limit:
            break
        path = r.get("extracted_path")
        if not path or not os.path.isfile(path):
            continue
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                content = fh.read(200_000)
        except OSError:
            continue
        scanned += 1
        idx = content.lower().find(q_lower)
        if idx < 0:
            continue
        start = max(0, idx - 100)
        end = min(len(content), idx + len(q) + 250)
        snippet = content[start:end].replace("\n", " ").replace("\t", " ").strip()
        # Collapse runs of spaces for readability.
        snippet = re.sub(r"\s{2,}", " ", snippet)
        out.append({
            "file_id":     r["id"],
            "filename":    r["filename"],
            "mime_type":   r["mime_type"],
            "modified_at": str(r["modified_at"]) if r["modified_at"] else None,
            "char_count":  int(r["extracted_chars"] or 0),
            "snippet":     "..." + snippet + "...",
        })
    print(f"[search_content] email={email} q={q!r} scanned={scanned} hits={len(out)}",
          flush=True)
    return out

# ----------------------------------------------------------------------------
def globus_search_telegram(email, query, chat_filter=None,
                            sender_filter=None, chat_type=None,
                            days_back=30, limit=20):
    """Search Telegram messages captured by the Telethon daemon.
    Shape mirrors globus_search_whatsapp."""
    if not email:
        return []
    try:
        days_back = max(1, min(int(days_back or 30), 365))
    except (TypeError, ValueError):
        days_back = 30
    try:
        limit = max(1, min(int(limit or 20), 100))
    except (TypeError, ValueError):
        limit = 20
    where = ["member_email=%s",
             "(tg_ts >= NOW() - INTERVAL %s DAY "
             " OR received_at >= NOW() - INTERVAL %s DAY)"]
    params = [email, days_back, days_back]
    if query and str(query).strip():
        where.append("body LIKE %s")
        params.append(f"%{str(query).strip()}%")
    if chat_filter:
        where.append("chat_name LIKE %s")
        params.append(f"%{chat_filter}%")
    if sender_filter:
        where.append("(sender LIKE %s OR sender_username LIKE %s)")
        params.append(f"%{sender_filter}%")
        params.append(f"%{sender_filter}%")
    if chat_type:
        where.append("chat_type=%s")
        params.append(str(chat_type).lower())
    sql = (
        "SELECT id, tg_chat_id, tg_message_id, chat_name, chat_type, "
        "  sender, sender_username, body, direction, tg_ts, received_at "
        "FROM globus_telegram_messages "
        f"WHERE {' AND '.join(where)} "
        "ORDER BY COALESCE(tg_ts, received_at) DESC LIMIT %s")
    params.append(limit)
    rows = db_read(sql, tuple(params)) or []
    return [{
        "id": r["id"],
        "tg_chat_id": r["tg_chat_id"],
        "tg_message_id": r["tg_message_id"],
        "chat": r["chat_name"],
        "chat_type": r["chat_type"],
        "sender": r["sender"] or "(unknown)",
        "sender_username": r["sender_username"],
        "direction": r["direction"],
        "tg_ts": str(r["tg_ts"]) if r["tg_ts"] else "",
        "captured_at": str(r["received_at"]) if r["received_at"] else "",
        "snippet": (r["body"] or "")[:600],
    } for r in rows]

# ----------------------------------------------------------------------------
def globus_search_whatsapp(email, query, chat_filter=None,
                           sender_filter=None, days_back=30, limit=20):
    """Search WhatsApp messages captured by the Chrome extension.
    Returns list of dicts the LLM can cite directly."""
    if not email:
        return []
    try:
        days_back = max(1, min(int(days_back or 30), 365))
    except (TypeError, ValueError):
        days_back = 30
    try:
        limit = max(1, min(int(limit or 20), 100))
    except (TypeError, ValueError):
        limit = 20
    where = ["member_email=%s",
             "received_at >= NOW() - INTERVAL %s DAY"]
    params = [email, days_back]
    if query and str(query).strip():
        where.append("body LIKE %s")
        params.append(f"%{str(query).strip()}%")
    if chat_filter:
        where.append("chat_name LIKE %s")
        params.append(f"%{chat_filter}%")
    if sender_filter:
        where.append("sender LIKE %s")
        params.append(f"%{sender_filter}%")
    sql = (
        "SELECT id, chat_name, sender, body, direction, wa_ts, received_at "
        "FROM globus_whatsapp_messages "
        f"WHERE {' AND '.join(where)} "
        "ORDER BY received_at DESC LIMIT %s")
    params.append(limit)
    rows = db_read(sql, tuple(params)) or []
    return [{
        "id": r["id"],
        "chat": r["chat_name"],
        "sender": r["sender"] or "(unknown)",
        "direction": r["direction"],
        "wa_ts": r["wa_ts"] or "",
        "captured_at": str(r["received_at"]) if r["received_at"] else "",
        "snippet": (r["body"] or "")[:600],
    } for r in rows]

