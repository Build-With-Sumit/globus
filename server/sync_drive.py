"""Drive sync orchestrator + background worker.

`sync_drive_connection(conn)` is the heart: 5-pass Drive sync (discover,
classify, parallel-download, index, aggregate). v0.3a is Drive-only;
Gmail/Analytics follow in v0.3b/c.

The background worker is a daemon thread started once from
`globus_server.main()` via `start_background_sync_worker()`. It picks up
connections that haven't synced in >1h and re-runs them. On service
restart it resets any orphaned `running` rows (otherwise a mid-sync
restart freezes the connector silently — same gotcha as buildwithsumit
prod, see Sumit's memory `feedback_sync_workers_reset_stale_running`).
"""
from __future__ import annotations
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from urllib.error import HTTPError

from db_helpers import db_read, db_write
from google_oauth import cleanup_expired_oauth_states
from oauth_db import (
    get_oauth_connection, get_valid_access_token, update_oauth_sync_status,
)
from google_drive import (
    GOOGLE_DRIVE_MAX_FILES, GOOGLE_DRIVE_LOOKBACK_DAYS,
    GOOGLE_DRIVE_AGG_FILES, GOOGLE_DRIVE_AGG_PER_FILE,
    GOOGLE_DRIVE_XLSX_EXPORT_MAX_BYTES,
    drive_list_files, drive_classify,
    drive_export_with_mime, drive_download_file, xlsx_to_text,
    vault_files_upsert, write_extracted_file,
)
from globus_vault_db import globus_upsert_source


# Parallel-worker count per Drive sync. Each worker is an outbound HTTP call
# to Drive's export/download endpoint, so this is I/O-bound — 24 threads
# saturates Drive's quota without tripping rate limits on a fresh project.
GLOBUS_DRIVE_WORKERS = 24

# How often the background worker wakes up to look for stale connections.
GLOBUS_BACKGROUND_SYNC_INTERVAL_SEC = 600       # 10 min between scans
GLOBUS_BACKGROUND_SYNC_MIN_AGE_SEC = 1 * 3600   # re-sync if older than 1h


# ─────────────────────────────────────────────────────────────────────
# Drive sync — 5 passes
# ─────────────────────────────────────────────────────────────────────

def sync_drive_connection(conn):
    """Full Drive sync for one connection:
      1. Discovery — list metadata for ALL matching files (up to 10K).
      2. Classify — mime + size triage; index skips immediately.
      3. Download — extract keepers IN PARALLEL (24 workers).
      4. Index — upsert globus_vault_files row for every file.
      5. Aggregate — top N recent files → globus_vault_sources row as
         chat-fallback (used when no per-file digest exists yet).

    Returns (extracted_count, extracted_total_bytes).
    Raises on token errors (caller catches into globus_sync_runs)."""
    access = get_valid_access_token(conn)
    email = conn["email"]
    conn_id = conn["id"]
    pa = conn["provider_account"]
    folder_ids_raw = conn.get("drive_folder_ids")
    if isinstance(folder_ids_raw, (bytes, bytearray)):
        folder_ids_raw = folder_ids_raw.decode()
    folder_ids = []
    if folder_ids_raw:
        try:
            folder_ids = json.loads(folder_ids_raw) or []
        except Exception:
            folder_ids = []

    cutoff = (datetime.utcnow() - timedelta(days=GOOGLE_DRIVE_LOOKBACK_DAYS)
              ).strftime("%Y-%m-%dT%H:%M:%SZ")
    base_query = f"trashed=false and modifiedTime > '{cutoff}'"
    if folder_ids:
        folder_query = " or ".join(f"'{fid}' in parents" for fid in folder_ids)
        query = f"({folder_query}) and {base_query}"
    else:
        query = base_query

    # Pass 1: discovery
    listed = drive_list_files(access, query, max_results=GOOGLE_DRIVE_MAX_FILES)
    listed.sort(key=lambda f: f.get("modifiedTime", ""), reverse=True)

    # Pass 2: classify + index skips. Refresh token here so the parallel
    # pass below has a token good for ~1h with no per-file race.
    access = get_valid_access_token(conn)
    extracted_count = 0
    skipped_count = 0
    extracted_total_bytes = 0
    recent_for_agg = []
    keepers = []
    for f in listed:
        fid = f.get("id")
        if not fid:
            continue
        method, ext, export_mime, skip_reason = drive_classify(f)
        if method is None:
            vault_files_upsert(
                email=email, connection_id=conn_id, provider_account=pa,
                source_type="google-drive", external_id=fid,
                filename=f.get("name") or "(untitled)",
                mime_type=f.get("mimeType") or "",
                size_bytes=int(f["size"]) if f.get("size") else None,
                modified_at=f.get("modifiedTime"), skip_reason=skip_reason,
                metadata={"webViewLink": f.get("webViewLink"),
                          "owners": f.get("owners")})
            skipped_count += 1
        else:
            keepers.append((f, method, ext, export_mime))

    # Pass 3: parallel download + extract + index. Each file fully wrapped
    # so one bad file degrades to a skip instead of aborting the crawl.
    def _process(item):
        f, method, ext, export_mime = item
        fid = f["id"]
        name = f.get("name") or "(untitled)"
        mime = (f.get("mimeType") or "").lower()
        modified = f.get("modifiedTime")
        size_meta = f.get("size")
        try:
            if method == "export":
                is_sheet = mime == "application/vnd.google-apps.spreadsheet"
                raw = drive_export_with_mime(
                    access, fid, export_mime,
                    max_bytes=(GOOGLE_DRIVE_XLSX_EXPORT_MAX_BYTES
                               if is_sheet else None))
            else:
                raw = drive_download_file(access, fid)
            if mime == "application/vnd.google-apps.spreadsheet":
                text = xlsx_to_text(raw)
            else:
                text = (raw.decode("utf-8", errors="replace")
                        if isinstance(raw, (bytes, bytearray)) else str(raw))
            text = (text or "").strip()
            if not text:
                vault_files_upsert(
                    email=email, connection_id=conn_id, provider_account=pa,
                    source_type="google-drive", external_id=fid, filename=name,
                    mime_type=mime,
                    size_bytes=int(size_meta) if size_meta else None,
                    modified_at=modified, skip_reason="extracted empty")
                return None
            path, n_bytes = write_extracted_file(
                email, pa, "google-drive", fid, ext, text)
            vault_files_upsert(
                email=email, connection_id=conn_id, provider_account=pa,
                source_type="google-drive", external_id=fid, filename=name,
                mime_type=mime, size_bytes=n_bytes, modified_at=modified,
                extracted_path=path, extracted_chars=len(text),
                metadata={"webViewLink": f.get("webViewLink"),
                          "owners": f.get("owners")})
            return (modified or "", name, mime, text, n_bytes)
        except HTTPError as e:
            vault_files_upsert(
                email=email, connection_id=conn_id, provider_account=pa,
                source_type="google-drive", external_id=fid, filename=name,
                mime_type=mime,
                size_bytes=int(size_meta) if size_meta else None,
                modified_at=modified,
                skip_reason=f"HTTP {e.code} on download")
            return None
        except Exception as e:
            vault_files_upsert(
                email=email, connection_id=conn_id, provider_account=pa,
                source_type="google-drive", external_id=fid, filename=name,
                mime_type=mime,
                size_bytes=int(size_meta) if size_meta else None,
                modified_at=modified,
                skip_reason=f"{type(e).__name__}: {e}")
            return None

    if keepers:
        with ThreadPoolExecutor(max_workers=GLOBUS_DRIVE_WORKERS) as pool:
            for res in pool.map(_process, keepers):
                if res is None:
                    skipped_count += 1
                else:
                    extracted_count += 1
                    extracted_total_bytes += res[4]
                    if len(recent_for_agg) < GOOGLE_DRIVE_AGG_FILES:
                        recent_for_agg.append((res[0], res[1], res[2], res[3]))

    # Pass 5: aggregated chat-fallback row (top-N recent, capped per file)
    agg_parts = []
    for mt, name, mime, text in recent_for_agg:
        snippet = text[:GOOGLE_DRIVE_AGG_PER_FILE]
        agg_parts.append(
            f"\n\n--- {name} ({mime}) — modified {mt} ---\n\n{snippet}")
    agg_content = "".join(agg_parts).strip()
    globus_upsert_source(
        email=email,
        source_type="google-drive",
        content=agg_content,
        source_identifier=pa,
        file_count=extracted_count,
        source_label=f"Google Drive ({pa})")

    return extracted_count, extracted_total_bytes


# ─────────────────────────────────────────────────────────────────────
# Dispatcher — fans out by source_type on the connection
# ─────────────────────────────────────────────────────────────────────

def sync_connection(conn_id, email):
    """Top-level sync for one connection. Updates status + writes one
    `globus_sync_runs` row per source. `email` enforces per-member ownership."""
    conn = get_oauth_connection(email, conn_id)
    if not conn:
        return False, "connection not found"
    update_oauth_sync_status(conn_id, 'running', None, mark_synced=False)
    sources = [s.strip()
               for s in (conn.get("source_types") or "").split(",") if s.strip()]
    total_items = 0
    total_chars = 0
    errors = []
    for s in sources:
        started = datetime.utcnow()
        try:
            if s == "drive":
                items, chars = sync_drive_connection(conn)
                source_type = "google-drive"
            else:
                # v0.3a is Drive-only. Gmail / Analytics arrive in v0.3b.
                continue
            total_items += items
            total_chars += chars
            db_write(
                "INSERT INTO globus_sync_runs (connection_id, email, source_type, "
                "status, items_count, chars_written, started_at, finished_at) "
                "VALUES (%s, %s, %s, 'success', %s, %s, %s, NOW())",
                (conn_id, email, source_type, items, chars, started))
        except Exception as e:
            err = f"{type(e).__name__}: {e}"
            errors.append(f"{s}: {err}")
            db_write(
                "INSERT INTO globus_sync_runs (connection_id, email, source_type, "
                "status, error_message, started_at, finished_at) "
                "VALUES (%s, %s, %s, 'error', %s, %s, NOW())",
                (conn_id, email,
                 {"drive": "google-drive"}.get(s, s),
                 err, started))
    if errors:
        update_oauth_sync_status(conn_id, 'error', "; ".join(errors))
        return False, "; ".join(errors)
    update_oauth_sync_status(conn_id, 'idle', None)
    db_write("UPDATE globus_oauth_connections SET needs_reconnect=0 "
             "WHERE id=%s", (conn_id,))
    return True, f"synced {total_items} items, {total_chars:,} chars"


def sync_connection_async(conn_id, email):
    """Spawn a background thread for the sync — fire-and-forget."""
    threading.Thread(target=sync_connection, args=(conn_id, email),
                     name=f"globus-sync-{conn_id}", daemon=True).start()


# ─────────────────────────────────────────────────────────────────────
# Background worker — runs in the globus_server process
# ─────────────────────────────────────────────────────────────────────

def _background_sync_loop():
    """Pick stale connections, sync them, sleep, repeat."""
    while True:
        try:
            cleanup_expired_oauth_states()
            rows = db_read(
                "SELECT id, email FROM globus_oauth_connections "
                "WHERE sync_status IN ('idle','error') "
                "  AND needs_reconnect=0 "
                "  AND (last_synced_at IS NULL "
                "       OR last_synced_at < (NOW() - INTERVAL %s SECOND)) "
                "ORDER BY (last_synced_at IS NOT NULL), last_synced_at ASC "
                "LIMIT 5",
                (GLOBUS_BACKGROUND_SYNC_MIN_AGE_SEC,)) or []
            for r in rows:
                try:
                    sync_connection(r["id"], r["email"])
                except Exception as e:
                    print(f"[bg-sync] {r['id']} ({r['email']}) error: {e}",
                          flush=True)
        except Exception as e:
            print(f"[bg-sync] loop error: {e}", flush=True)
        time.sleep(GLOBUS_BACKGROUND_SYNC_INTERVAL_SEC)


def start_background_sync_worker():
    """Called once from main(). Resets any 'running' rows orphaned by a
    prior crash/restart so they become eligible for sync again.
    (Per Sumit's memory: a mid-sync restart freezes the connector silently
    without this reclaim — five days stale before we noticed in prod.)"""
    db_write("UPDATE globus_oauth_connections SET sync_status='idle' "
             "WHERE sync_status='running'")
    threading.Thread(target=_background_sync_loop,
                     name="globus-bg-sync", daemon=True).start()
