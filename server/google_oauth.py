"""Google OAuth — state CSRF + URL builders + token exchange/refresh/userinfo.

Pure stdlib. The DB-touching parts (encrypted refresh tokens, connection
upserts, `get_valid_access_token` auto-refresh) live in `oauth_db.py`.

Wire by calling `configure(site=..., redirect_path=...)` from
`globus_server.py` before importing/using.

Env / config keys required:
  - GOOGLE_OAUTH_CLIENT_ID
  - GOOGLE_OAUTH_CLIENT_SECRET

Both read via `db_helpers.cfg(...)` (DB config table first, then env).
"""
from __future__ import annotations
import json
import secrets
import urllib.parse
from urllib.request import Request, urlopen

from db_helpers import db_read, db_write, cfg


# ─────────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────────

GOOGLE_OAUTH_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO_URL = "https://www.googleapis.com/oauth2/v3/userinfo"
GOOGLE_REVOKE_URL = "https://oauth2.googleapis.com/revoke"

# Default callback path. The OSS install can override via configure().
DEFAULT_REDIRECT_PATH = "/members/connect/google/callback"

GOOGLE_SCOPES = {
    "drive": "https://www.googleapis.com/auth/drive.readonly",
    "gmail": "https://www.googleapis.com/auth/gmail.readonly",
    "analytics": "https://www.googleapis.com/auth/analytics.readonly",
    "profile": (
        "https://www.googleapis.com/auth/userinfo.email "
        "https://www.googleapis.com/auth/userinfo.profile openid"
    ),
}

_REDIRECT_URI = ""  # populated by configure()


def configure(*, site, redirect_path=DEFAULT_REDIRECT_PATH):
    """Wire the OAuth redirect URI from the boot site + path.

    `site` should be the externally-visible origin (e.g. https://globus.example.com).
    `redirect_path` defaults to /members/connect/google/callback — change only
    if the route is remapped in globus_server.py."""
    global _REDIRECT_URI
    _REDIRECT_URI = site.rstrip("/") + redirect_path


# ─────────────────────────────────────────────────────────────────────
# OAuth state (CSRF) — single-use token in globus_oauth_states
# ─────────────────────────────────────────────────────────────────────

def create_oauth_state(email, provider, source_types, redirect_after=None):
    """Insert a fresh state row with a 10-minute expiry; return the token."""
    state_token = secrets.token_hex(32)
    db_write(
        "INSERT INTO globus_oauth_states "
        "(state_token, email, provider, source_types, redirect_after, expires_at) "
        "VALUES (%s, %s, %s, %s, %s, (NOW() + INTERVAL 10 MINUTE))",
        (state_token, email, provider, source_types, redirect_after))
    return state_token


def consume_oauth_state(state_token):
    """Single-use lookup: returns the row if valid + unexpired, else None.
    Deletes the row on success."""
    rows = db_read(
        "SELECT * FROM globus_oauth_states "
        "WHERE state_token=%s AND expires_at > NOW()",
        (state_token,))
    if not rows:
        return None
    db_write("DELETE FROM globus_oauth_states WHERE state_token=%s",
             (state_token,))
    return rows[0]


def cleanup_expired_oauth_states():
    """Periodic janitor — called from the background sync loop."""
    db_write("DELETE FROM globus_oauth_states WHERE expires_at < NOW()")


# ─────────────────────────────────────────────────────────────────────
# OAuth URL builders + token exchange
# ─────────────────────────────────────────────────────────────────────

def google_authorize_url(state, source_types, redirect_uri=None, offline=True):
    """Build the Google OAuth consent URL.

    `source_types` is a list like ['drive'] or ['drive', 'gmail'] — we always
    prepend profile scopes so the callback can read the Google account email.
    `offline=True` requests a refresh token (needed for any later sync); set
    False for sign-in-only flows that never call APIs after."""
    client_id = cfg("GOOGLE_OAUTH_CLIENT_ID")
    if not client_id:
        raise RuntimeError("GOOGLE_OAUTH_CLIENT_ID not configured")
    if not _REDIRECT_URI and not redirect_uri:
        raise RuntimeError("google_oauth.configure(site=...) must be called "
                           "before google_authorize_url")
    scopes = [GOOGLE_SCOPES["profile"]]
    for s in source_types:
        if s in GOOGLE_SCOPES and s != "profile":
            scopes.append(GOOGLE_SCOPES[s])
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri or _REDIRECT_URI,
        "response_type": "code",
        "scope": " ".join(scopes),
        "include_granted_scopes": "true",
        "state": state,
    }
    if offline:
        params["access_type"] = "offline"
        params["prompt"] = "consent"   # force refresh_token even on re-auth
    return GOOGLE_OAUTH_AUTH_URL + "?" + urllib.parse.urlencode(params)


def google_exchange_code(code, redirect_uri=None):
    """Exchange the authorization code from the OAuth callback for tokens."""
    client_id = cfg("GOOGLE_OAUTH_CLIENT_ID")
    client_secret = cfg("GOOGLE_OAUTH_CLIENT_SECRET")
    if not (client_id and client_secret):
        raise RuntimeError("GOOGLE_OAUTH_CLIENT_ID / SECRET not configured")
    body = urllib.parse.urlencode({
        "code": code,
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": redirect_uri or _REDIRECT_URI,
        "grant_type": "authorization_code",
    }).encode()
    req = Request(GOOGLE_TOKEN_URL, data=body, method="POST",
                  headers={"Content-Type": "application/x-www-form-urlencoded"})
    with urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())


def google_refresh_token(refresh_token):
    """Trade a stored refresh token for a fresh access token."""
    client_id = cfg("GOOGLE_OAUTH_CLIENT_ID")
    client_secret = cfg("GOOGLE_OAUTH_CLIENT_SECRET")
    body = urllib.parse.urlencode({
        "client_id": client_id,
        "client_secret": client_secret,
        "refresh_token": refresh_token,
        "grant_type": "refresh_token",
    }).encode()
    req = Request(GOOGLE_TOKEN_URL, data=body, method="POST",
                  headers={"Content-Type": "application/x-www-form-urlencoded"})
    with urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())


def google_userinfo(access_token):
    """Read the OpenID userinfo (email, name, picture). Used to populate
    `provider_account` on the connection so multiple Google accounts are
    distinguishable per member."""
    req = Request(GOOGLE_USERINFO_URL,
                  headers={"Authorization": "Bearer " + access_token})
    with urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode())


def google_revoke(token):
    """Best-effort token revocation. Returns True if Google accepted, False
    otherwise. Never raises — callers use this on cleanup paths where the
    failure mode is 'leave the token live for a while', not catastrophic."""
    if not token:
        return False
    body = urllib.parse.urlencode({"token": token}).encode()
    req = Request(GOOGLE_REVOKE_URL, data=body, method="POST",
                  headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        urlopen(req, timeout=20)
        return True
    except Exception:
        return False
