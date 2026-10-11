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

from db_helpers import db_read, db_write, cfg
from oauth_db import encrypt_token, decrypt_token


def _shared_credential(tool: str) -> dict | None:
    """Company-wide fallback key deployed server-side.

    When an operator deploys an API key for a tool in the `config`
    table (or as an env var) named NARADA_<TOOL>_API_KEY or
    <TOOL>_API_KEY, that tool becomes available to EVERY member with
    no per-member paste — the campaign builder shows it as live (not
    'coming soon') and plugin calls use the shared key. This is how a
    Globus admin turns a lead source on for the whole install (e.g. a
    company Cognism / ZoomInfo seat) instead of asking each marketer
    for their own key.

    Per-member vault keys always win over this (see get_credential);
    the shared key is consulted only when the member has none. Single
    `api_key` credentials only — multi-field tools (Salesforce etc.)
    still require the per-member vault.

    Note: config-table values are cached at server start, so a newly
    added row takes effect on next restart (same as every other cfg()
    key); env vars are likewise read at process start.
    """
    slug = (tool or "").strip().upper().replace("-", "_").replace(".", "_")
    if not slug:
        return None
    for name in (f"NARADA_{slug}_API_KEY", f"{slug}_API_KEY"):
        val = (cfg(name) or "").strip()
        if val:
            return {"api_key": val}
    return None


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


def merge_credential(member_email: str, tool: str,
                      new_values: dict) -> None:
    """Overlay non-empty `new_values` onto the existing credential (or
    create a fresh one). Lets the UI change ONE field of a multi-field
    credential (e.g. Salesforce client_secret) without wiping the others,
    since the form only submits the fields the member actually filled.
    To fully clear a credential, use delete_credential."""
    existing = get_credential(member_email, tool) or {}
    merged = dict(existing)
    for k, v in (new_values or {}).items():
        if (v or "").strip():
            merged[k] = v
    if merged:
        set_credential(member_email, tool, merged)


def get_credential(member_email: str, tool: str) -> dict | None:
    """Decrypt + return the credential dict, or None if no active
    credential exists. Plugins call this on every request — it's
    fast (single indexed SELECT) and avoids stashing keys in memory."""
    rows = db_read(
        "SELECT credential_enc, status FROM globus_narada_credentials "
        "WHERE member_email=%s AND tool=%s",
        (member_email, tool))
    if rows and rows[0].get("status") == "active" \
            and rows[0]["credential_enc"] is not None:
        try:
            plain = decrypt_token(rows[0]["credential_enc"])
            if plain:
                return json.loads(plain)
        except Exception as e:
            print(f"[narada-creds] decrypt failed for {member_email}/{tool}: "
                  f"{type(e).__name__}: {e}", flush=True)
    # No usable per-member key — fall back to a company-wide key
    # deployed server-side (config table / env var), if any.
    return _shared_credential(tool)


def has_credential(member_email: str, tool: str) -> bool:
    """True iff there's an active credential row for this (member, tool).
    Cheaper than get_credential (no decrypt). Used by plugin
    is_available() implementations."""
    rows = db_read(
        "SELECT 1 FROM globus_narada_credentials "
        "WHERE member_email=%s AND tool=%s AND status='active' LIMIT 1",
        (member_email, tool))
    if rows:
        return True
    # Company-wide server-deployed key makes the tool live for everyone.
    return _shared_credential(tool) is not None


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
