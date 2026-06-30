"""Composio integration — per-member managed-OAuth wrapper.

Composio (composio.dev) is a third-party catalog of 1000+ SaaS tools with
managed OAuth — Google Calendar, GitHub, Notion, Slack, Linear, Stripe,
etc. The model:
  1. Operator creates "auth configs" once per app in the Composio
     dashboard — generates one `auth_config_id` per app. We store
     those IDs in the `config` table:
       COMPOSIO_AUTH_CONFIG_GOOGLECALENDAR
       COMPOSIO_AUTH_CONFIG_GITHUB
       (one row per app you want exposed).
  2. Operator stores the Composio API key in the same `config` table
     as `COMPOSIO_API_KEY`.
  3. Per Globus member: first time they want to use a Composio tool,
     we call composio.connected_accounts.link(user_id, auth_config_id)
     → get a redirect URL → bounce the member through Composio's
     OAuth flow → callback writes the connection row.
  4. Per tool call: session.execute(tool_slug, arguments) using the
     member's email as the Composio user_id.

Composio holds the tokens + handles refresh. We never see them.
Per-member isolation matches Globus's own model 1:1.

This module is the thin glue. The typed LLM tools (calendar_create_event,
github_comment_on_pr, etc.) live in composio_tools.py and call into
here. Routes live in globus_server.py.
"""
from __future__ import annotations
import threading

from db_helpers import db_read, db_write, cfg


# Lazy SDK singleton — Composio() reads COMPOSIO_API_KEY from env, so we
# stuff the cfg() value into os.environ on first access. Held behind a
# lock to avoid two threads racing the import on first hit.
_CLIENT = None
_CLIENT_LOCK = threading.Lock()


def _client():
    """Return the Composio client, lazily initialised. Raises a clear
    error if COMPOSIO_API_KEY isn't configured."""
    global _CLIENT
    if _CLIENT is not None:
        return _CLIENT
    with _CLIENT_LOCK:
        if _CLIENT is not None:
            return _CLIENT
        api_key = (cfg("COMPOSIO_API_KEY", "") or "").strip()
        if not api_key:
            raise RuntimeError(
                "COMPOSIO_API_KEY not configured. Add to MySQL `config` "
                "table: INSERT INTO config (name, value) VALUES "
                "('COMPOSIO_API_KEY', '<your-key>'). Get the key from "
                "the Composio dashboard at https://app.composio.dev/.")
        try:
            from composio import Composio
        except ImportError as e:
            raise RuntimeError(
                f"composio package not installed: {e}. "
                "Run: pip install composio")
        import os
        # Composio reads from env, not constructor arg
        os.environ.setdefault("COMPOSIO_API_KEY", api_key)
        _CLIENT = Composio()
        return _CLIENT


def _user_id_for(email):
    """Map a Globus member email to the Composio user_id we use. Just the
    lowercased email — Composio accepts any string, this keeps the
    mapping trivially reversible."""
    return (email or "").strip().lower()


def _auth_config_id_for(app):
    """Look up the per-app Composio auth_config_id from the config table.
    Returns '' if not configured (caller turns that into a friendly
    'this app isn't wired on this install' error)."""
    key = f"COMPOSIO_AUTH_CONFIG_{app.upper()}"
    return (cfg(key, "") or "").strip()


# ─────────────────────────────────────────────────────────────────────
# Connection CRUD — globus_composio_connections
# ─────────────────────────────────────────────────────────────────────

def list_member_connections(email):
    """Return all rows for this member (one per app they've ever
    touched). Used by the /members/composio settings page."""
    return db_read(
        "SELECT id, app, status, composio_account_id, connected_at, "
        "       last_error, created_at "
        "FROM globus_composio_connections "
        "WHERE member_email=%s ORDER BY app",
        (email,)) or []


def get_connection(email, app):
    """Return the row for this (member, app) or None."""
    rows = db_read(
        "SELECT * FROM globus_composio_connections "
        "WHERE member_email=%s AND app=%s LIMIT 1",
        (email, app))
    return rows[0] if rows else None


def is_active(email, app):
    """True iff this member has an active connection for this app."""
    row = get_connection(email, app)
    return bool(row and row.get("status") == "active")


def _upsert_pending(email, app, composio_user_id, composio_connection_id):
    """Insert/update a row in 'pending' state right after we call link().
    The callback (or wait_for_connection) flips it to 'active'."""
    db_write(
        "INSERT INTO globus_composio_connections "
        "(member_email, app, composio_user_id, composio_connection_id, "
        " status) VALUES (%s, %s, %s, %s, 'pending') "
        "ON DUPLICATE KEY UPDATE "
        "  composio_connection_id=VALUES(composio_connection_id), "
        "  status='pending', last_error=NULL",
        (email, app, composio_user_id, composio_connection_id))


def _mark_active(email, app, composio_account_id):
    db_write(
        "UPDATE globus_composio_connections "
        "SET status='active', composio_account_id=%s, "
        "    connected_at=NOW(), last_error=NULL "
        "WHERE member_email=%s AND app=%s",
        (composio_account_id, email, app))


def _mark_error(email, app, err):
    db_write(
        "UPDATE globus_composio_connections "
        "SET last_error=%s WHERE member_email=%s AND app=%s",
        ((err or "")[:1000], email, app))


# ─────────────────────────────────────────────────────────────────────
# Connect flow — call from /members/composio/connect/<app> route
# ─────────────────────────────────────────────────────────────────────

def initiate_connect(email, app, callback_url):
    """Generate a Composio Connect Link for this (member, app). Returns
    `{"ok": True, "redirect_url": "...", "connection_id": "..."}`
    on success, or `{"ok": False, "error": "..."}` on misconfiguration."""
    auth_config_id = _auth_config_id_for(app)
    if not auth_config_id:
        return {"ok": False,
                "error": f"COMPOSIO_AUTH_CONFIG_{app.upper()} not "
                          f"configured. Create an auth config for {app} "
                          f"in the Composio dashboard, then INSERT INTO "
                          f"config (name, value) VALUES "
                          f"('COMPOSIO_AUTH_CONFIG_{app.upper()}', "
                          f"'<auth_config_id>')."}
    try:
        client = _client()
    except RuntimeError as e:
        return {"ok": False, "error": str(e)}
    user_id = _user_id_for(email)
    try:
        conn = client.connected_accounts.link(
            user_id=user_id,
            auth_config_id=auth_config_id,
            callback_url=callback_url)
    except Exception as e:
        return {"ok": False,
                "error": f"composio.connected_accounts.link failed: "
                          f"{type(e).__name__}: {e}"}
    _upsert_pending(email, app, user_id, conn.id)
    return {"ok": True,
            "redirect_url": conn.redirect_url,
            "connection_id": conn.id}


def finalize_connect(email, app, connection_id=None):
    """Called from the OAuth callback. Polls Composio for the connection
    status (it should be active by the time the callback fires) and
    flips the row to active. Returns dict for the route to render."""
    row = get_connection(email, app)
    if not row:
        return {"ok": False,
                "error": "no pending connection for this (member, app)"}
    cid = connection_id or row.get("composio_connection_id")
    if not cid:
        return {"ok": False, "error": "no connection_id to verify"}
    try:
        client = _client()
        acct = client.connected_accounts.wait_for_connection(
            id=cid, timeout=30)
    except Exception as e:
        err = f"wait_for_connection failed: {type(e).__name__}: {e}"
        _mark_error(email, app, err)
        return {"ok": False, "error": err}
    if not getattr(acct, "id", None):
        err = "connected_account returned without an id"
        _mark_error(email, app, err)
        return {"ok": False, "error": err}
    _mark_active(email, app, acct.id)
    return {"ok": True, "app": app, "account_id": acct.id}


def disconnect(email, app):
    """Delete the local connection row + best-effort revoke the
    Composio side. We don't fail the local delete if Composio's
    revocation API isn't reachable — the user expects 'gone'."""
    row = get_connection(email, app)
    if row and row.get("composio_account_id"):
        try:
            client = _client()
            # SDK shape isn't fully verified yet — wrap defensively.
            if hasattr(client.connected_accounts, "delete"):
                client.connected_accounts.delete(
                    id=row["composio_account_id"])
        except Exception as e:
            print(f"[composio] disconnect revoke failed for "
                  f"{email}/{app}: {type(e).__name__}: {e}", flush=True)
    db_write(
        "DELETE FROM globus_composio_connections "
        "WHERE member_email=%s AND app=%s",
        (email, app))
    return {"ok": True}


# ─────────────────────────────────────────────────────────────────────
# Execute action — the hot path called by every typed tool wrapper
# ─────────────────────────────────────────────────────────────────────

def execute(email, app, tool_slug, arguments):
    """Run a Composio action for this member. `tool_slug` is the
    Composio action id (e.g. 'GOOGLECALENDAR_CREATE_EVENT'); `app`
    is the lookup key for our connections table (e.g. 'googlecalendar').

    Returns a dict — never raises:
      {"ok": True,  "data": <action response>}
      {"ok": False, "error": "..."}

    Per-member ownership: the session is scoped to the member's Composio
    user_id; a tool call CANNOT pull data from another member's account
    even if tool_slug is right. If the member hasn't connected the app
    yet, returns a friendly error pointing them at /members/composio."""
    if not is_active(email, app):
        return {"ok": False,
                "error": f"you haven't connected {app} yet. Visit "
                          f"/members/composio and connect it first."}
    try:
        client = _client()
    except RuntimeError as e:
        return {"ok": False, "error": str(e)}
    user_id = _user_id_for(email)
    try:
        session = client.create(user_id=user_id)
        resp = session.execute(
            tool_slug=tool_slug, arguments=arguments or {})
    except Exception as e:
        err = f"composio.execute failed: {type(e).__name__}: {e}"
        print(f"[composio] {email}/{app}/{tool_slug}: {err}", flush=True)
        return {"ok": False, "error": err}
    # SessionExecuteResponse — shape may vary by SDK version; defensive.
    data = getattr(resp, "data", None)
    if data is None and hasattr(resp, "__dict__"):
        data = resp.__dict__
    elif data is None:
        data = resp  # last resort: hand back whatever it gave us
    return {"ok": True, "data": data}


# ─────────────────────────────────────────────────────────────────────
# Discover — meta-tool the LLM uses to explore apps we haven't typed
# ─────────────────────────────────────────────────────────────────────

def list_actions_for_app(email, app, query=""):
    """List available actions for a given app via the member's session.
    Used by the `composio_discover` LLM tool so the agent has an escape
    hatch for apps we don't expose as typed tools. Returns:
      {"ok": True, "actions": [{slug, name, description}, ...]}"""
    try:
        client = _client()
    except RuntimeError as e:
        return {"ok": False, "error": str(e)}
    user_id = _user_id_for(email)
    try:
        session = client.create(user_id=user_id)
        if query:
            results = session.search(query=f"{app} {query}")
        else:
            # Fall back to a generic app-name search if the SDK has no
            # direct "list actions for app" call exposed.
            results = session.search(query=app)
    except Exception as e:
        return {"ok": False,
                "error": f"composio search failed: "
                          f"{type(e).__name__}: {e}"}
    actions = []
    # Defensive shape parsing — return whatever has a slug/name we can find.
    items = getattr(results, "items", None) or getattr(results, "data", None) or results
    if isinstance(items, list):
        for item in items[:50]:
            slug = (getattr(item, "slug", None)
                    or getattr(item, "tool_slug", None)
                    or (item.get("slug") if isinstance(item, dict) else None))
            name = (getattr(item, "name", None)
                    or (item.get("name") if isinstance(item, dict) else None))
            desc = (getattr(item, "description", None)
                    or (item.get("description") if isinstance(item, dict) else None))
            if slug:
                actions.append({"slug": slug,
                                 "name": name or slug,
                                 "description": (desc or "")[:300]})
    return {"ok": True, "app": app, "actions": actions}
