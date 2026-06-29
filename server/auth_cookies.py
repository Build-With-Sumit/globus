"""Session cookie + token gen/verify for the members area — extracted
from lead_server.py 2026-06-28 as refactor slice #6b.

Cookie shape: `bws_member=<base64url(email|expires)>.<hex_hmac>` with
HttpOnly, Secure, SameSite=Lax flags, 30-day Max-Age. HMAC is
SHA-256 of the inner `email|expires` payload (not the base64 wrapper)
so the wire format can be inspected without breaking verification.

SESSION_SECRET + SESSION_TTL are injected at startup via configure()
so this module has zero deps on lead_server (avoids circular import).
Same pattern as fb_capi / voice_helpers / members_db.
"""
from __future__ import annotations
import base64
import hashlib
import hmac
import time


# Module state set via configure().
_SESSION_SECRET: bytes = b""
_SESSION_TTL: int = 30 * 24 * 3600  # 30 days, matches the legacy default


def configure(*, session_secret, session_ttl=None):
    """Initialize the module. Called once at server startup from
    lead_server.py.

    session_secret: HMAC key (bytes) used to sign cookies. Same key
      used for voice tokens, but with a different payload shape so the
      two don't accidentally cross-validate.
    session_ttl:    cookie max-age in seconds (defaults to 30 days).
    """
    global _SESSION_SECRET, _SESSION_TTL
    if not isinstance(session_secret, (bytes, bytearray)):
        raise TypeError("session_secret must be bytes")
    _SESSION_SECRET = bytes(session_secret)
    if session_ttl is not None:
        _SESSION_TTL = int(session_ttl)


def verify_token(token):
    """Return the email if the token is valid + unexpired, else None.
    Never raises — bad tokens always return None."""
    try:
        b64, mac = token.split(".", 1)
        pad = "=" * (-len(b64) % 4)
        payload = base64.urlsafe_b64decode(b64 + pad).decode()
        expect = hmac.new(_SESSION_SECRET, payload.encode(),
                          hashlib.sha256).hexdigest()
        if not hmac.compare_digest(mac, expect):
            return None
        email, exp = payload.rsplit("|", 1)
        if int(exp) < int(time.time()):
            return None
        return email
    except Exception:
        return None


def make_cookie(email):
    """Build the Set-Cookie header value for a successful login.
    Encodes `email|expires_unix` as base64, signs with SESSION_SECRET,
    sets HttpOnly + Secure + SameSite=Lax + 30-day Max-Age."""
    payload = email + "|" + str(int(time.time()) + _SESSION_TTL)
    mac = hmac.new(_SESSION_SECRET, payload.encode(),
                   hashlib.sha256).hexdigest()
    token = (base64.urlsafe_b64encode(payload.encode())
             .decode().rstrip("=") + "." + mac)
    return ("bws_member=" + token + "; Path=/; HttpOnly; Secure; "
            "SameSite=Lax; Max-Age=" + str(_SESSION_TTL))


# Set-Cookie value that clears the session (Max-Age=0). Same Path +
# flags as the live cookie so browsers actually delete it.
CLEAR_COOKIE = ("bws_member=; Path=/; HttpOnly; Secure; "
                "SameSite=Lax; Max-Age=0")
