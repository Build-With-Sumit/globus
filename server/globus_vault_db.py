"""Globus vault + chat DB layer — extracted from lead_server.py
2026-06-28 as refactor slice #6w. Pure DB-CRUD over the
globus_vault_sources, globus_intelligence, and globus_messages
tables. Two related concerns sit here because they share callers:

  - Vault sources: where every per-member source row lives
    (Drive/Gmail/Obsidian/Freshsales/GA static blobs). Read via
    globus_get_vault() which returns the system-prompt material
    Globus sees on every chat turn (either the pre-built
    intelligence digest OR a raw fallback aggregation).
  - Intelligence digest: a pre-built compact context (`globus_intelligence`
    row) produced offline via Claude Code / cron — never built
    from the live chat path.
  - Chat history: globus_messages + globus_count_today + globus_log_message.

What's here:
  - GLOBUS_VAULT_MAX_CHARS: cap on stored vault content per source row.
  - GLOBUS_PER_SOURCE_CAP: per-source cap going INTO system prompt.
  - GLOBUS_TOTAL_VAULT_CAP: total cap across all sources in the
    system prompt (leaves headroom for persona + history in 200K
    context window).
  - globus_get_sources(email): list every source row for a member.
  - globus_upsert_source(...): insert-or-update one source row.
  - globus_delete_source(email, source_id): per-member-scoped delete.
  - globus_get_intelligence(email) / globus_set_intelligence(...):
    digest read/write.
  - globus_get_vault(email): combined context for the chat path
    (digest-first, raw fallback).
  - globus_save_vault(...): backward-compat upsert wrapper for the
    obsidian-zip/paste flows.
  - globus_extract_md_from_zip(zip_bytes, max_chars=...): pull
    every .md out of a zip into a single concatenated blob.
  - globus_messages(email, limit) / globus_count_today(email) /
    globus_log_message(email, role, content): chat history CRUD.

Module deps: db_read + db_write (db_helpers), zipfile + io (stdlib).
Per-member isolation: every query is email-scoped — we never read
or write across members.
"""
from __future__ import annotations
import io
import zipfile
from db_helpers import db_read, db_write


GLOBUS_VAULT_MAX_CHARS = 200_000   # cap stored vault content
GLOBUS_PER_SOURCE_CAP = 200_000    # max chars from one source going into system prompt
GLOBUS_TOTAL_VAULT_CAP = 500_000   # ~125K tokens; leaves headroom for persona + history in 200K context


def globus_extract_md_from_zip(zip_bytes, max_chars=GLOBUS_VAULT_MAX_CHARS):
    """Extract all .md files from a zip; return (concatenated_markdown, file_count, total_chars)."""
    out = []
    file_count = 0
    total_chars = 0
    truncated = False
    with zipfile.ZipFile(io.BytesIO(zip_bytes), "r") as z:
        names = sorted(n for n in z.namelist() if n.lower().endswith(".md") and not n.endswith("/"))
        for name in names:
            try:
                with z.open(name) as f:
                    text = f.read().decode("utf-8", errors="replace").strip()
            except Exception:
                continue
            if not text:
                continue
            file_count += 1
            block = f"\n\n--- {name} ---\n\n{text}"
            if total_chars + len(block) > max_chars:
                out.append(f"\n\n[truncated at {max_chars} chars after {file_count - 1} files]")
                truncated = True
                file_count -= 1
                break
            out.append(block)
            total_chars += len(block)
    return "".join(out).strip(), file_count, total_chars, truncated


def globus_get_sources(email):
    """List every source for a member (UI + chat use this).

    Per-member isolation: ALL queries against globus_vault_sources filter by
    email; we never read across members. Source rows are tagged by
    source_type + source_identifier so Obsidian uploads, paste, Google
    Drives, and Gmail accounts stay distinct.
    """
    return db_read(
        "SELECT id, source_type, source_identifier, source_label, content, char_count, "
        "file_count, last_synced_at, updated_at "
        "FROM globus_vault_sources WHERE email=%s ORDER BY updated_at DESC",
        (email,)) or []


def globus_upsert_source(email, source_type, content, source_identifier="",
                         file_count=None, source_label=None):
    """Insert or update one vault source for one member. Email-scoped."""
    db_write(
        "INSERT INTO globus_vault_sources "
        "(email, source_type, source_identifier, source_label, content, char_count, file_count) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s) "
        "ON DUPLICATE KEY UPDATE "
        "  source_label=COALESCE(VALUES(source_label), source_label), "
        "  content=VALUES(content), char_count=VALUES(char_count), "
        "  file_count=VALUES(file_count), updated_at=NOW()",
        (email, source_type, source_identifier or "", source_label,
         content, len(content), file_count))


def globus_delete_source(email, source_id):
    """Remove one source for one member. Email IS REQUIRED — never delete cross-member."""
    db_write("DELETE FROM globus_vault_sources WHERE email=%s AND id=%s",
             (email, source_id))


def globus_get_intelligence(email):
    """The per-member intelligence digest — pre-built OFFLINE via Claude
    Code (or future cron). The Anthropic API is NEVER used to build this.
    When present, this is what the live chat sends to Sonnet instead of
    the raw vault — keeps per-message token cost ~25x lower."""
    rows = db_read(
        "SELECT email, content, source_summary, built_with, raw_char_count, "
        "digest_char_count, built_at, updated_at "
        "FROM globus_intelligence WHERE email=%s", (email,))
    return rows[0] if rows else None


def globus_set_intelligence(email, content, source_summary=None,
                            built_with=None, raw_char_count=None):
    """Write/replace a member's intelligence digest. Called from offline
    tooling ONLY (scripts/build_intelligence.py, Claude Code sessions,
    or future cron). Never called from the live chat path."""
    db_write(
        "INSERT INTO globus_intelligence "
        "(email, content, source_summary, built_with, raw_char_count, digest_char_count) "
        "VALUES (%s, %s, %s, %s, %s, %s) "
        "ON DUPLICATE KEY UPDATE "
        "  content=VALUES(content), source_summary=VALUES(source_summary), "
        "  built_with=VALUES(built_with), raw_char_count=VALUES(raw_char_count), "
        "  digest_char_count=VALUES(digest_char_count), updated_at=NOW()",
        (email, content, source_summary, built_with,
         raw_char_count, len(content)))


def _globus_live_overlay(email):
    """Live, time-sensitive sources the offline intelligence digest doesn't
    capture (e.g. Google Analytics, refreshed on every sync). Always folded into
    the chat/voice context so Globus answers from them even on the digest path."""
    rows = db_read(
        "SELECT source_label, source_type, source_identifier, content "
        "FROM globus_vault_sources WHERE email=%s "
        # NOTE: '%%' not '%' — PyMySQL %-formats the query when params are
        # passed, so a literal % must be escaped or the whole query throws and
        # db_read silently returns None (which dropped GA + product from context).
        "AND (source_type = 'google-analytics' OR source_type LIKE 'product-%%') "
        "ORDER BY updated_at DESC", (email,)) or []
    parts = []
    for r in rows:
        c = (r.get("content") or "")[:GLOBUS_PER_SOURCE_CAP]
        if c.strip():
            ident = f", {r['source_identifier']}" if r.get("source_identifier") else ""
            parts.append(
                f"\n\n--- Source: {r.get('source_label') or r['source_type']} "
                f"({r['source_type']}{ident}) ---\n\n{c}")
    return "".join(parts).strip()


def globus_get_vault(email):
    """Combined context for sending to Claude as system-prompt material.

    Two paths:
      1. **Digest path (preferred):** if a `globus_intelligence` row exists
         (pre-built offline via Claude Code / cron) we use THAT — small,
         focused, cheap. This is the intended steady state.
      2. **Raw fallback:** if no digest exists yet, fall back to the raw
         `globus_vault_sources` aggregation with per-source caps. This
         burns ~25x more API tokens per chat and should be considered a
         degraded state until the offline digest is built.

    The Anthropic API is NEVER called from this function or anything it
    transitively touches. All digestion happens offline by design — see
    `scripts/build_intelligence.py`.
    """
    intel = globus_get_intelligence(email)
    if intel and (intel.get("content") or "").strip():
        content = intel["content"]
        overlay = _globus_live_overlay(email)
        if overlay:
            content = content + "\n\n" + overlay
        sources_used = [{
            "id": 0,
            "source_type": "intelligence-digest",
            "source_identifier": email,
            "source_label": (intel.get("source_summary")
                             or "Intelligence digest"),
            "char_count": (intel.get("digest_char_count")
                           or len(intel["content"])),
            "file_count": None,
            "updated_at": intel.get("updated_at"),
        }]
        return {
            "content": content,
            "char_count": len(content),
            "file_count": 0,
            "source_count": len(sources_used) + (1 if overlay else 0),
            "sources_used": sources_used,
            "uploaded_at": intel.get("updated_at"),
            "from_digest": True,
        }
    rows = globus_get_sources(email)
    if not rows:
        return None
    parts = []
    total = 0
    sources_used = []
    for r in rows:
        snippet = (r["content"] or "")[:GLOBUS_PER_SOURCE_CAP]
        header = (f"\n\n--- Source: {r.get('source_label') or r['source_type']} "
                  f"({r['source_type']}"
                  + (f", {r['source_identifier']}" if r.get('source_identifier') else "")
                  + ") ---\n\n")
        block = header + snippet
        if total + len(block) > GLOBUS_TOTAL_VAULT_CAP:
            break
        parts.append(block)
        total += len(block)
        sources_used.append({
            "id": r["id"],
            "source_type": r["source_type"],
            "source_identifier": r.get("source_identifier") or "",
            "source_label": r.get("source_label") or r["source_type"],
            "char_count": r["char_count"],
            "file_count": r.get("file_count"),
            "updated_at": r.get("updated_at"),
        })
    if not parts:
        return None
    return {
        "content": "".join(parts).strip(),
        "char_count": total,
        "file_count": sum((r.get("file_count") or 0) for r in rows),
        "source_count": len(sources_used),
        "sources_used": sources_used,
        # Keep "uploaded_at" key so the existing chat-html template doesn't break
        "uploaded_at": rows[0].get("updated_at"),
        "from_digest": False,
    }


def globus_save_vault(email, source, content, file_count):
    """Backward-compat wrapper. Maps the old single-source upsert into the
    new multi-source table."""
    if source in ("obsidian-zip", "obsidian-paste"):
        source_type = source
    elif source == "paste":
        source_type = "obsidian-paste"
    else:
        source_type = "other"
    label = {
        "obsidian-zip": "Obsidian (zip upload)",
        "obsidian-paste": "Pasted notes",
        "paste": "Pasted notes",
    }.get(source, source)
    globus_upsert_source(email, source_type, content,
                         source_identifier="",
                         file_count=file_count,
                         source_label=label)


def globus_messages(email, limit=20):
    rows = db_read(
        "SELECT role, content, created_at FROM globus_messages WHERE email=%s "
        "ORDER BY id DESC LIMIT %s", (email, limit)) or []
    return list(reversed(rows))


def globus_count_today(email):
    rows = db_read(
        "SELECT COUNT(*) AS c FROM globus_messages WHERE email=%s "
        "AND role='user' AND created_at >= UTC_DATE()", (email,))
    return int(rows[0]["c"]) if rows else 0


def globus_log_message(email, role, content):
    db_write(
        "INSERT INTO globus_messages (email, role, content) VALUES (%s, %s, %s)",
        (email, role, content))
