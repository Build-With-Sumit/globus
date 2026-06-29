"""OAuth connection storage — Fernet-encrypted token at rest + CRUD.

The refresh + access tokens in `globus_oauth_connections.refresh_token_enc`
and `access_token_enc` are stored as Fernet ciphertext. The key comes from
the `GLOBUS_OAUTH_ENCRYPTION_KEY` config (DB > env). Generate one with:

    python3 -c 'from cryptography.fernet import Fernet; \\
                print(Fernet.generate_key().decode())'

`get_valid_access_token(conn)` is the hot-path call every sync worker uses
before each Drive/Gmail API hit — it refreshes when within 2 min of expiry
and flips needs_reconnect=1 on `invalid_grant` so the dashboard can show
the badge.
"""
from __future__ import annotations
import json
import threading
from datetime import datetime, timedelta
from urllib.error import HTTPError

from db_helpers import db_read, db_write, cfg
from google_oauth import google_refresh_token


# ─────────────────────────────────────────────────────────────────────
# Encryption — lazy-init Fernet singleton (cryptography is the only
# non-stdlib runtime dep besides PyMySQL; see requirements.txt).
# ─────────────────────────────────────────────────────────────────────

_FERNET = None
_FERNET_LOCK = threading.Lock()


def _fernet():
    global _FERNET
    if _FERNET is None:
        with _FERNET_LOCK:
            if _FERNET is None:
                from cryptography.fernet import Fernet
                key = cfg("GLOBUS_OAUTH_ENCRYPTION_KEY")
                if not key:
                    raise RuntimeError(
                        "GLOBUS_OAUTH_ENCRYPTION_KEY not configured. "
                        "Generate one with: python3 -c "
                        "'from cryptography.fernet import Fernet; "
                        "print(Fernet.generate_key().decode())'")
                _FERNET = Fernet(key.encode() if isinstance(key, str)
                                  else key)
    return _FERNET


def encrypt_token(plaintext):
    if plaintext is None:
        return None
    return _fernet().encrypt(plaintext.encode("utf-8"))


def decrypt_token(ciphertext):
    if ciphertext is None:
        return None
    if isinstance(ciphertext, str):
        ciphertext = ciphertext.encode("utf-8")
    return _fernet().decrypt(ciphertext).decode("utf-8")


# ─────────────────────────────────────────────────────────────────────
# Connection CRUD — per-member-scoped (email arg required on every read)
# ─────────────────────────────────────────────────────────────────────

def list_oauth_connections(email):
    return db_read(
        "SELECT id, provider, provider_account, scopes, expires_at, user_info, "
        "source_types, drive_folder_ids, gmail_query, last_synced_at, "
        "last_sync_error, sync_status, needs_reconnect, created_at "
        "FROM globus_oauth_connections WHERE email=%s ORDER BY created_at DESC",
        (email,)) or []


def list_oauth_connections_with_stats(email):
    """Connections joined with per-source file/byte stats from
    `globus_vault_files`. Used by the /members/connect dashboard."""
    rows = db_read(
        "SELECT c.id, c.provider, c.provider_account, c.scopes, c.expires_at, "
        "c.user_info, c.source_types, c.drive_folder_ids, c.gmail_query, "
        "c.last_synced_at, c.last_sync_error, c.sync_status, c.needs_reconnect, "
        "c.created_at, "
        "  IFNULL(d.bytes, 0)     AS drive_bytes, "
        "  IFNULL(d.files, 0)     AS drive_files, "
        "  d.last_modified        AS drive_updated_at, "
        "  IFNULL(g.bytes, 0)     AS gmail_bytes, "
        "  IFNULL(g.files, 0)     AS gmail_files, "
        "  g.last_modified        AS gmail_updated_at "
        "FROM globus_oauth_connections c "
        "LEFT JOIN ("
        "  SELECT email, provider_account, "
        "    SUM(IFNULL(size_bytes, IFNULL(extracted_chars,0))) AS bytes, "
        "    COUNT(*) AS files, "
        "    MAX(modified_at) AS last_modified "
        "  FROM globus_vault_files "
        "  WHERE extracted=1 AND source_type='google-drive' "
        "  GROUP BY email, provider_account"
        ") d ON d.email=c.email AND d.provider_account=c.provider_account "
        "LEFT JOIN ("
        "  SELECT email, provider_account, "
        "    SUM(IFNULL(size_bytes, IFNULL(extracted_chars,0))) AS bytes, "
        "    COUNT(*) AS files, "
        "    MAX(modified_at) AS last_modified "
        "  FROM globus_vault_files "
        "  WHERE extracted=1 AND source_type='gmail' "
        "  GROUP BY email, provider_account"
        ") g ON g.email=c.email AND g.provider_account=c.provider_account "
        "WHERE c.email=%s ORDER BY c.created_at DESC",
        (email,)) or []
    return rows


def get_oauth_connection(email, conn_id):
    rows = db_read(
        "SELECT * FROM globus_oauth_connections WHERE email=%s AND id=%s",
        (email, conn_id))
    return rows[0] if rows else None


def count_oauth_connections(email):
    rows = db_read("SELECT COUNT(*) AS c FROM globus_oauth_connections "
                    "WHERE email=%s", (email,))
    return int(rows[0]["c"]) if rows else 0


def upsert_oauth_connection(email, provider_account, scopes, refresh_token,
                            access_token, expires_at, user_info, source_types,
                            provider="google"):
    db_write(
        "INSERT INTO globus_oauth_connections "
        "(email, provider, provider_account, scopes, refresh_token_enc, "
        " access_token_enc, expires_at, user_info, source_types) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) "
        "ON DUPLICATE KEY UPDATE "
        "  scopes=VALUES(scopes), refresh_token_enc=VALUES(refresh_token_enc), "
        "  access_token_enc=VALUES(access_token_enc), expires_at=VALUES(expires_at), "
        "  user_info=VALUES(user_info), source_types=VALUES(source_types), "
        "  needs_reconnect=0",
        (email, provider, provider_account, scopes,
         encrypt_token(refresh_token), encrypt_token(access_token),
         expires_at, json.dumps(user_info) if user_info else None, source_types))


def update_oauth_access_token(conn_id, access_token, expires_at):
    db_write(
        "UPDATE globus_oauth_connections SET access_token_enc=%s, "
        "expires_at=%s WHERE id=%s",
        (encrypt_token(access_token), expires_at, conn_id))


def delete_oauth_connection(email, conn_id):
    db_write("DELETE FROM globus_oauth_connections WHERE email=%s AND id=%s",
             (email, conn_id))


def update_oauth_sync_status(conn_id, status, error_message=None,
                              mark_synced=True):
    if mark_synced:
        db_write(
            "UPDATE globus_oauth_connections SET sync_status=%s, "
            "last_sync_error=%s, last_synced_at=NOW() WHERE id=%s",
            (status, error_message, conn_id))
    else:
        db_write(
            "UPDATE globus_oauth_connections SET sync_status=%s, "
            "last_sync_error=%s WHERE id=%s",
            (status, error_message, conn_id))


# ─────────────────────────────────────────────────────────────────────
# Access-token refresh — called before every Drive/Gmail API hit
# ─────────────────────────────────────────────────────────────────────

def get_valid_access_token(conn):
    """Return a usable access token, refreshing if within ~2 min of expiry.
    Mutates `conn` in place (access_token_enc + expires_at) so a long
    multi-hour sync never re-fetches a fresh row from MySQL.

    On `invalid_grant` (revoked at Google end / consent withdrawn) the
    connection is flagged needs_reconnect=1 and a RECONNECT_NEEDED RuntimeError
    is raised — the sync aborts cleanly and the dashboard shows the badge."""
    now = datetime.utcnow()
    expires = conn.get("expires_at")
    if (expires and conn.get("access_token_enc")
            and expires > now + timedelta(seconds=120)):
        return decrypt_token(conn["access_token_enc"])
    refresh = decrypt_token(conn["refresh_token_enc"])
    try:
        fresh = google_refresh_token(refresh)
    except HTTPError as e:
        try:
            body = e.read().decode("utf-8", "replace")
        except Exception:
            body = str(e)
        if e.code in (400, 401) and "invalid_grant" in body:
            db_write("UPDATE globus_oauth_connections SET needs_reconnect=1 "
                     "WHERE id=%s", (conn["id"],))
            raise RuntimeError(
                "RECONNECT_NEEDED: Google refresh token is invalid or "
                "revoked — reconnect this account.")
        raise
    new_access = fresh["access_token"]
    new_expires = now + timedelta(seconds=int(fresh.get("expires_in", 3600)))
    update_oauth_access_token(conn["id"], new_access, new_expires)
    conn["access_token_enc"] = encrypt_token(new_access)
    conn["expires_at"] = new_expires
    return new_access
