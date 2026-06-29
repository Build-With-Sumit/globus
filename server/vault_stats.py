"""Live vault-build stats — extracted from lead_server.py 2026-06-28
as refactor slice #6t. Memoized per-member stats reader for the
Obsidian-vault background builder, used by:
  - /members/vault-progress page + JSON endpoint
  - /members/globus chat header (sources / files / chars count)

What's here:
  - VAULT_SOURCE_META: dict mapping source_type → {label, tag, status}.
    Single source of truth for what the dashboard calls each
    backing-store + which ones are live vs planned.
  - vault_progress_stats(email): aggregates over globus_vault_files
    + globus_whatsapp_messages + globus_telegram_messages +
    globus_telegram_bot_sends + globus_vault_sources. Memoized 45s
    per-member (dashboard polls every 60s, voice chat hits this on
    every page render — caching stops HTTP thread saturation that
    was starving the voice-llm endpoint, root cause of the
    intermittent ElevenLabs 'upstream error' Sumit reported
    2026-06-24).

Module deps: db_read (db_helpers). Reads /opt/buildwithsumit/vault/auto
directly for the notes-by-type count, defensive against perm errors
(the auto-builder runs as root and can create subdirs blocked to
www-data — we skip unreadable subdirs rather than 500'ing).
"""
from __future__ import annotations
import os
import time
from db_helpers import db_read


VAULT_SOURCE_META = {
    "google-drive":       {"label": "Google Drive",       "tag": "File repository", "status": "live"},
    "gmail":              {"label": "Gmail",              "tag": "Email",           "status": "live"},
    "freshsales-contact": {"label": "Freshsales (contacts)", "tag": "CRM",         "status": "live"},
    "freshsales-account": {"label": "Freshsales (accounts)", "tag": "CRM",         "status": "live"},
    "freshsales-deal":    {"label": "Freshsales (deals)", "tag": "CRM",            "status": "live"},
    "whatsapp":           {"label": "WhatsApp Web",       "tag": "Communications", "status": "live"},
    "google-analytics":   {"label": "Google Analytics",   "tag": "Analytics",      "status": "live"},
    "obsidian-zip":       {"label": "Obsidian (uploads)", "tag": "Notes",          "status": "live"},
    "github":             {"label": "GitHub",             "tag": "Code",           "status": "planned"},
    "telegram":           {"label": "Telegram (personal)", "tag": "Communications", "status": "live"},
    "telegram-bot":       {"label": "Telegram (bot send)", "tag": "Communications", "status": "live"},
    "ahrefs":             {"label": "Ahrefs",             "tag": "SEO",            "status": "planned"},
}


_VAULT_PROGRESS_CACHE = {}  # email -> (unix_ts, stats_dict)
_VAULT_PROGRESS_TTL_SEC = 45  # dashboard polls 60s; serve cached in between


def vault_progress_stats(email):
    """Live stats for the Obsidian-vault background builder, used by
    /members/vault-progress and its JSON endpoint.

    Aggregates over globus_vault_files (60K+ rows), globus_telegram_messages
    (124K+), globus_whatsapp_messages, globus_vault_sources. Memoized for
    45s per-member because the dashboard auto-polls every 60s — serving
    the cached result on tight repolls stops the HTTP thread pool from
    saturating and starving the voice-llm endpoint (root cause of the
    intermittent ElevenLabs 'upstream error' Sumit reported 2026-06-24).

    The runner uses CLAIM_SENTINEL='1970-01-01 00:00:01' to mark
    'claimed by a worker, in-flight' rows. We treat those as NEITHER
    processed nor pending — they're 'in flight' (handful at any time)."""
    cached = _VAULT_PROGRESS_CACHE.get(email)
    if cached and (time.time() - cached[0]) < _VAULT_PROGRESS_TTL_SEC:
        return cached[1]
    rows = db_read(
        "SELECT "
        "  SUM(extracted=1) AS extracted, "
        "  SUM(extracted=1 AND vault_processed_at > '2000-01-01') AS processed, "
        "  SUM(extracted=1 AND vault_processed_at IS NULL) AS pending, "
        "  SUM(extracted=1 AND vault_processed_at = '1970-01-01 00:00:01') AS in_flight, "
        "  MAX(CASE WHEN vault_processed_at > '2000-01-01' THEN vault_processed_at END) AS last_processed_at "
        "FROM globus_vault_files WHERE email=%s", (email,)) or [{}]
    s = rows[0] or {}
    extracted = int(s.get("extracted") or 0)
    processed = int(s.get("processed") or 0)
    pending = int(s.get("pending") or 0)
    in_flight = int(s.get("in_flight") or 0)
    pct = (processed / extracted * 100.0) if extracted else 0.0

    by_source_map = {}

    def _add(src_type, extracted, processed):
        meta = VAULT_SOURCE_META.get(src_type, {"label": src_type,
                                                 "tag": "Other",
                                                 "status": "live"})
        by_source_map[src_type] = {
            "source_type": src_type,
            "label":       meta["label"],
            "tag":         meta["tag"],
            "status":      meta["status"],
            "extracted":   int(extracted or 0),
            "processed":   int(processed or 0),
        }

    for r in (db_read(
        "SELECT source_type, "
        "  SUM(extracted=1) AS extracted, "
        "  SUM(extracted=1 AND vault_processed_at > '2000-01-01') AS processed "
        "FROM globus_vault_files WHERE email=%s GROUP BY source_type",
        (email,)) or []):
        _add(r["source_type"], r.get("extracted"), r.get("processed"))

    try:
        wa = db_read(
            "SELECT COUNT(*) AS n FROM globus_whatsapp_messages "
            "WHERE member_email=%s", (email,)) or [{}]
        wa_n = int((wa[0] or {}).get("n") or 0)
        if wa_n:
            _add("whatsapp", wa_n, wa_n)
    except Exception:
        pass

    try:
        tg = db_read(
            "SELECT COUNT(*) AS n FROM globus_telegram_messages "
            "WHERE member_email=%s", (email,)) or [{}]
        tg_n = int((tg[0] or {}).get("n") or 0)
        if tg_n:
            _add("telegram", tg_n, tg_n)
    except Exception:
        pass

    try:
        tb = db_read(
            "SELECT COUNT(*) AS n FROM globus_telegram_bot_sends "
            "WHERE member_email=%s AND status='sent'",
            (email,)) or [{}]
        tb_n = int((tb[0] or {}).get("n") or 0)
        if tb_n:
            _add("telegram-bot", tb_n, tb_n)
    except Exception:
        pass

    try:
        for r in (db_read(
            "SELECT source_type, COUNT(*) AS n FROM globus_vault_sources "
            "WHERE email=%s GROUP BY source_type", (email,)) or []):
            st = r["source_type"]
            if st in by_source_map or st.startswith("product-"):
                continue
            n = int(r.get("n") or 0)
            _add(st, n, n)
    except Exception:
        pass

    for src_type, meta in VAULT_SOURCE_META.items():
        if meta.get("status") == "planned" and src_type not in by_source_map:
            _add(src_type, 0, 0)

    by_source = sorted(
        by_source_map.values(),
        key=lambda r: (
            0 if r["status"] == "live" else 1,
            -r["processed"],
        ))

    recent = db_read(
        "SELECT source_type, IFNULL(filename,'') AS filename, "
        "  vault_processed_at, IFNULL(extracted_chars,0) AS chars "
        "FROM globus_vault_files WHERE email=%s AND vault_processed_at IS NOT NULL "
        "ORDER BY vault_processed_at DESC LIMIT 20", (email,)) or []

    rate_rows = db_read(
        "SELECT COUNT(*) AS c FROM globus_vault_files "
        "WHERE email=%s AND vault_processed_at > (NOW() - INTERVAL 1 HOUR)",
        (email,)) or [{}]
    per_hour = int((rate_rows[0] or {}).get("c") or 0)
    eta_min = (pending / per_hour * 60) if per_hour else None

    # Notes on disk by type. Defensive: the auto-builder runs as root
    # and can create subdirs with perms that block www-data. Skip
    # unreadable subdirs rather than 500'ing the entire vault-progress
    # poll (the live page parses the response as JSON; an HTML 500
    # error there breaks the whole dashboard).
    notes_by_type = {}
    base = "/opt/buildwithsumit/vault/auto"
    try:
        subdirs = os.listdir(base) if os.path.isdir(base) else []
    except OSError:
        subdirs = []
    for ntype in subdirs:
        d = os.path.join(base, ntype)
        try:
            if os.path.isdir(d):
                notes_by_type[ntype] = sum(
                    1 for n in os.listdir(d) if n.endswith(".md"))
        except OSError:
            notes_by_type[ntype] = -1
    total_notes = sum(v for v in notes_by_type.values() if v >= 0)

    result = {
        "extracted": extracted,
        "processed": processed,
        "pending": pending,
        "in_flight": in_flight,
        "pct": round(pct, 1),
        "by_source": by_source,
        "recent": recent,
        "per_hour": per_hour,
        "eta_min": eta_min,
        "total_notes": total_notes,
        "notes_by_type": notes_by_type,
        "last_processed_at": s.get("last_processed_at"),
    }
    _VAULT_PROGRESS_CACHE[email] = (time.time(), result)
    return result
