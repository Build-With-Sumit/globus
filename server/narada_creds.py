"""Per-member, per-tool credential vault for Narada plugins.

Plugins like Smartlead, Prospeo, NeverBounce, EmailBison etc. authenticate
with an API key (no OAuth). Marketers paste the key into the
/members/narada/credentials page; we Fernet-encrypt and store in
globus_narada_credentials. Plugins read via `get_credential(member, tool)`.

Composio-backed plugins (Gmail, HubSpot, Salesforce, etc.) DON'T use
this vault — they go through globus_composio_connections instead.

Same Fernet key as Google OAuth refresh tokens
(GLOBUS_OAUTH_ENCRYPTION_KEY) — one key per install, rotate together.
"""
from __future__ import annotations
import json

from db_helpers import db_read, db_write
from oauth_db import encrypt_token, decrypt_token


def set_credential(member_email: str, tool: str,
                    credential: dict) -> None:
    """Store credentials for a (member, tool). `credential` is an
    arbitrary dict — typically {"api_key": "..."} but plugins can stash
    multiple values ({"api_key": "...", "workspace_id": "..."}).
    Fernet-encrypted at rest."""
    payload = json.dumps(credential)
    enc = encrypt_token(payload)
    db_write(
        "INSERT INTO globus_narada_credentials "
        "(member_email, tool, credential_enc, status) "
        "VALUES (%s, %s, %s, 'active') "
        "ON DUPLICATE KEY UPDATE "
        "  credential_enc=VALUES(credential_enc), status='active'",
        (member_email, tool, enc))


def get_credential(member_email: str, tool: str) -> dict | None:
    """Decrypt + return the credential dict, or None if no active
    credential exists. Plugins call this on every request — it's
    fast (single indexed SELECT) and avoids stashing keys in memory."""
    rows = db_read(
        "SELECT credential_enc, status FROM globus_narada_credentials "
        "WHERE member_email=%s AND tool=%s",
        (member_email, tool))
    if not rows or rows[0].get("status") != "active":
        return None
    enc = rows[0]["credential_enc"]
    if enc is None:
        return None
    try:
        plain = decrypt_token(enc)
        return json.loads(plain) if plain else None
    except Exception as e:
        print(f"[narada-creds] decrypt failed for {member_email}/{tool}: "
              f"{type(e).__name__}: {e}", flush=True)
        return None


def has_credential(member_email: str, tool: str) -> bool:
    """True iff there's an active credential row for this (member, tool).
    Cheaper than get_credential (no decrypt). Used by plugin
    is_available() implementations."""
    rows = db_read(
        "SELECT 1 FROM globus_narada_credentials "
        "WHERE member_email=%s AND tool=%s AND status='active' LIMIT 1",
        (member_email, tool))
    return bool(rows)


def list_member_credentials(member_email: str) -> list[dict]:
    """List which tools the member has credentials for. Used by the
    /members/narada/credentials page. Does NOT return the plaintext
    credential — just the metadata."""
    return db_read(
        "SELECT tool, status, last_used_at, created_at "
        "FROM globus_narada_credentials WHERE member_email=%s "
        "ORDER BY tool",
        (member_email,)) or []


def delete_credential(member_email: str, tool: str) -> None:
    db_write(
        "DELETE FROM globus_narada_credentials "
        "WHERE member_email=%s AND tool=%s",
        (member_email, tool))


def touch_last_used(member_email: str, tool: str) -> None:
    """Bump the last_used_at on a credential. Plugins call this
    after every successful API hit so the dashboard can show 'last
    used 3 minutes ago' freshness signal."""
    db_write(
        "UPDATE globus_narada_credentials SET last_used_at=NOW() "
        "WHERE member_email=%s AND tool=%s",
        (member_email, tool))
