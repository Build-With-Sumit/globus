"""Gmail sync orchestrator + delta + on-demand freshen.

Three layers:
  - `sync_gmail_connection(conn)` — full crawl (cap 50K msgs), parallel
    fetch (24 workers), index every message in globus_vault_files,
    aggregate top-100 recent into globus_vault_sources.
  - `sync_gmail_delta(conn, query, max_wall_sec)` — cheap incremental:
    list IDs in `query` window, dedup against vault, fetch only NEW ones.
    Wall-clock capped so the tool loop doesn't hang on a Gmail stall.
  - `globus_freshen_gmail(email, max_wall_sec, background)` — the
    pre-tool-call freshen hook. Cooldown-throttled per member
    (`_GMAIL_DELTA_LAST_AT`) so multiple list_recent_emails calls in
    one tool loop only trigger one delta sync.

Per Sumit's directive: when asked about emails, sync the latest BEFORE
scanning. Don't rely on stale digest. Voice path passes background=True
because a multi-second inline sync blows ElevenLabs' per-turn budget.
"""
from __future__ import annotations
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from db_helpers import db_read, db_write
from oauth_db import get_valid_access_token
from google_gmail import (
    GMAIL_MAX_MESSAGES, GMAIL_PER_BODY_CHARS,
    gmail_list_messages, gmail_get_message, gmail_extract_body_text,
    parse_email_date,
)
from google_drive import vault_files_upsert, write_extracted_file
from globus_vault_db import globus_upsert_source


# Parallel-worker count per full Gmail sync (mirrors GLOBUS_DRIVE_WORKERS).
GLOBUS_GMAIL_WORKERS = 24


# ─────────────────────────────────────────────────────────────────────
# Full sync — 4 passes
# ─────────────────────────────────────────────────────────────────────

def sync_gmail_connection(conn):
    """Two-pass Gmail sync (mirror of Drive flow):
      1. Discovery — list message IDs (paginated, cap GMAIL_MAX_MESSAGES).
      2. Fetch — per-message GET in parallel, extract body, write to disk.
      3. Index — globus_vault_files row per message (kept or skipped).
      4. Aggregate — top-N recent rolled into globus_vault_sources row.

    Returns (extracted_count, extracted_total_bytes).
    The `gmail_query` column on the connection lets a member customise
    the search (defaults to 90 days, no spam/trash)."""
    access = get_valid_access_token(conn)
    email = conn["email"]
    conn_id = conn["id"]
    pa = conn["provider_account"]
    query = conn.get("gmail_query") or "newer_than:90d -in:spam -in:trash"
    listed = gmail_list_messages(access, query, max_results=GMAIL_MAX_MESSAGES)

    extracted_count = 0
    skipped_count = 0
    extracted_total_bytes = 0
    recent_for_agg = []

    def _process(stub):
        mid = stub.get("id")
        if not mid:
            return None
        try:
            full = gmail_get_message(access, mid)
            payload = full.get("payload") or {}
            headers = {h["name"]: h["value"]
                       for h in (payload.get("headers") or [])}
            subject = headers.get("Subject", "(no subject)")
            sender = headers.get("From", "?")
            recipient = headers.get("To", "?")
            date = headers.get("Date", "?")
            body = ((gmail_extract_body_text(payload) or "")
                    .strip()[:GMAIL_PER_BODY_CHARS])
            parsed_dt = parse_email_date(date)
            if not body:
                vault_files_upsert(
                    email=email, connection_id=conn_id, provider_account=pa,
                    source_type="gmail", external_id=mid,
                    filename=subject[:480], modified_at=parsed_dt,
                    skip_reason="no text body",
                    metadata={"From": sender[:200], "To": recipient[:200],
                              "Date": date[:60]})
                return None
            text = (
                f"Subject: {subject}\n"
                f"From: {sender}\nTo: {recipient}\nDate: {date}\n\n"
                f"{body}"
            )
            path, n_bytes = write_extracted_file(
                email, pa, "gmail", mid, "txt", text)
            vault_files_upsert(
                email=email, connection_id=conn_id, provider_account=pa,
                source_type="gmail", external_id=mid,
                filename=subject[:480], mime_type="message/rfc822",
                size_bytes=n_bytes, modified_at=parsed_dt,
                extracted_path=path, extracted_chars=len(text),
                metadata={"From": sender[:200], "To": recipient[:200],
                          "Date": date[:60]})
            return (date, subject, sender, body, n_bytes)
        except Exception as e:
            vault_files_upsert(
                email=email, connection_id=conn_id, provider_account=pa,
                source_type="gmail", external_id=mid,
                skip_reason=f"fetch failed: {type(e).__name__}")
            return None

    if listed:
        with ThreadPoolExecutor(max_workers=GLOBUS_GMAIL_WORKERS) as pool:
            for res in pool.map(_process, listed):
                if res is None:
                    skipped_count += 1
                else:
                    extracted_count += 1
                    extracted_total_bytes += res[4]
                    if len(recent_for_agg) < 100:
                        recent_for_agg.append((res[0], res[1], res[2], res[3]))

    agg_parts = []
    for date, subject, sender, body in recent_for_agg:
        snippet = body[:3000]
        agg_parts.append(
            f"\n\n--- Email: {subject} ---\n"
            f"From: {sender}\nDate: {date}\n\n{snippet}")
    agg_content = "".join(agg_parts).strip()
    globus_upsert_source(
        email=email,
        source_type="gmail",
        content=agg_content,
        source_identifier=pa,
        file_count=extracted_count,
        source_label=f"Gmail ({pa})")

    return extracted_count, extracted_total_bytes


# ─────────────────────────────────────────────────────────────────────
# Delta sync — incremental refresh for the on-demand freshen path
# ─────────────────────────────────────────────────────────────────────

def sync_gmail_delta(conn, query="newer_than:1d", max_wall_sec=20,
                      hard_limit=200):
    """Fast incremental Gmail sync — fetches ONLY message IDs in the
    `query` window that we don't already have indexed.

    Returns (new_count, seen_count, capped_at_wall_clock)."""
    t0 = time.time()
    try:
        access = get_valid_access_token(conn)
    except Exception as e:
        print(f"[gmail-delta] no access token for conn {conn.get('id')}: "
              f"{type(e).__name__}: {e}", flush=True)
        return 0, 0, False
    email = conn["email"]
    conn_id = conn["id"]
    pa = conn["provider_account"]
    try:
        listed = gmail_list_messages(access, query, max_results=hard_limit)
    except Exception as e:
        print(f"[gmail-delta] list failed: {type(e).__name__}: {e}",
              flush=True)
        return 0, 0, False
    seen_ids = [s.get("id") for s in listed if s.get("id")]
    if not seen_ids:
        return 0, 0, False
    placeholders = ",".join(["%s"] * len(seen_ids))
    existing = db_read(
        f"SELECT external_id FROM globus_vault_files "
        f"WHERE email=%s AND source_type='gmail' AND extracted=1 "
        f"  AND external_id IN ({placeholders})",
        (email, *seen_ids)) or []
    existing_set = {r["external_id"] for r in existing}
    new_ids = [m for m in seen_ids if m not in existing_set]
    new_count = 0
    capped = False
    for mid in new_ids:
        if (time.time() - t0) > max_wall_sec:
            capped = True
            print(f"[gmail-delta] hit {max_wall_sec}s cap with "
                  f"{len(new_ids) - new_count} of {len(new_ids)} new "
                  f"messages still unfetched", flush=True)
            break
        try:
            access = get_valid_access_token(conn)
            full = gmail_get_message(access, mid)
        except Exception as e:
            print(f"[gmail-delta] fetch {mid} failed: "
                  f"{type(e).__name__}: {e}", flush=True)
            continue
        payload = full.get("payload") or {}
        headers = {h["name"]: h["value"]
                   for h in (payload.get("headers") or [])}
        subject = headers.get("Subject", "(no subject)")
        sender = headers.get("From", "?")
        recipient = headers.get("To", "?")
        date = headers.get("Date", "?")
        body = ((gmail_extract_body_text(payload) or "")
                .strip()[:GMAIL_PER_BODY_CHARS])
        parsed_dt = parse_email_date(date)
        if not body:
            vault_files_upsert(
                email=email, connection_id=conn_id, provider_account=pa,
                source_type="gmail", external_id=mid,
                filename=subject[:480], modified_at=parsed_dt,
                skip_reason="no text body",
                metadata={"From": sender[:200], "To": recipient[:200],
                          "Date": date[:60]})
            continue
        text = (
            f"Subject: {subject}\n"
            f"From: {sender}\nTo: {recipient}\nDate: {date}\n\n"
            f"{body}"
        )
        path, n_bytes = write_extracted_file(
            email, pa, "gmail", mid, "txt", text)
        vault_files_upsert(
            email=email, connection_id=conn_id, provider_account=pa,
            source_type="gmail", external_id=mid,
            filename=subject[:480], mime_type="message/rfc822",
            size_bytes=n_bytes, modified_at=parsed_dt,
            extracted_path=path, extracted_chars=len(text),
            metadata={"From": sender[:200], "To": recipient[:200],
                      "Date": date[:60]})
        new_count += 1
    elapsed = time.time() - t0
    if new_count or capped:
        print(f"[gmail-delta] member={email} q={query!r} "
              f"seen={len(seen_ids)} new={new_count} capped={capped} "
              f"elapsed={elapsed:.1f}s", flush=True)
    return new_count, len(seen_ids), capped


# ─────────────────────────────────────────────────────────────────────
# On-demand freshen — called inline from globus_list_recent_emails
# ─────────────────────────────────────────────────────────────────────

_GMAIL_DELTA_LAST_AT = {}
GMAIL_DELTA_MIN_INTERVAL_SEC = 60   # at most once per minute per member


def globus_freshen_gmail(email, max_wall_sec=20, background=False):
    """Per-member cooldown-throttled delta sync. Used as the freshness
    step before list_recent_emails so the answer doesn't come from a
    stale digest.

    `background=True` runs the sync on a daemon thread and returns
    immediately — used by the voice path. A multi-second inline sync
    blows ElevenLabs' per-turn budget: EL times out the turn, emits a
    malformed error event, and the browser SDK hard-disconnects."""
    if not email:
        return
    now = time.time()
    last = _GMAIL_DELTA_LAST_AT.get(email, 0)
    if now - last < GMAIL_DELTA_MIN_INTERVAL_SEC:
        return
    _GMAIL_DELTA_LAST_AT[email] = now

    def _run():
        try:
            # PyMySQL %-escape gotcha: literal `%` inside a LIKE pattern
            # parameter needs no escaping (params are passed separately),
            # but if the LIKE pattern were inlined we'd need `%%`. We pass
            # `%gmail%` as a param value, so no double-escaping needed.
            conns = db_read(
                "SELECT * FROM globus_oauth_connections "
                "WHERE email=%s AND source_types LIKE %s "
                "  AND needs_reconnect=0 AND sync_status != 'running'",
                (email, "%gmail%")) or []
        except Exception as e:
            print(f"[gmail-delta] conn lookup failed: "
                  f"{type(e).__name__}: {e}", flush=True)
            return
        for conn in conns:
            try:
                sync_gmail_delta(conn, query="newer_than:1d",
                                  max_wall_sec=max_wall_sec)
            except Exception as e:
                print(f"[gmail-delta] sync conn {conn.get('id')} failed: "
                      f"{type(e).__name__}: {e}", flush=True)

    if background:
        threading.Thread(target=_run, daemon=True,
                          name="gmail-freshen").start()
    else:
        _run()
