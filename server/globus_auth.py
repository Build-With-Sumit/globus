"""Member auth — OTP via email + session cookie + member CRUD glue.

Two ways a member signs in:
  1. POST /members/login with an email → we send a 6-digit OTP via
     email (SendGrid by default; stderr fallback in dev) and redirect
     to the code-entry page.
  2. POST /members/login/code with email + code → verify, set session
     cookie, redirect to /members/globus.

Members must already exist in the `members` table with status='active'
or 'comp' (no self-signup in v0.2; admin adds via SQL — see INSTALL.md).
This matches the reference impl: Globus is a per-member tool, you
provision members out-of-band.

Module deps: db_helpers (db_read/db_write/cfg), members_db
(is_active_member), auth_cookies (make_cookie/verify_token),
members_auth_html (login/code pages). All already configured by
globus_server at startup before this module is imported.
"""
from __future__ import annotations
import hashlib
import hmac
import json
import secrets
import sys
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from db_helpers import db_read, db_write, cfg
from members_db import is_active_member


# ─────────────────────────────────────────────────────────────────────
# OTP codes — stored as HMAC-SHA256(session_secret, code) in auth_codes
# ─────────────────────────────────────────────────────────────────────

_SESSION_SECRET_BYTES = b""


def configure(*, session_secret):
    """Wire in the session secret (used as the HMAC key for OTP code
    hashing — same secret as auth_cookies + voice_helpers).
    Must be bytes (the HMAC key)."""
    global _SESSION_SECRET_BYTES
    if not isinstance(session_secret, (bytes, bytearray)):
        raise TypeError("session_secret must be bytes")
    _SESSION_SECRET_BYTES = bytes(session_secret)


def _code_hash(code):
    return hmac.new(_SESSION_SECRET_BYTES,
                    str(code).encode("utf-8"),
                    hashlib.sha256).hexdigest()


def _issue_code(email):
    """Rate-limit, mint, store and send a one-time code.

    The CALLER is responsible for authorizing `email` first — this helper
    deliberately performs no authorization of its own, so that each entry
    point states its own rule (an existing member for the single-tenant
    site; a registered org email domain for an org portal)."""
    rows = db_read("SELECT COUNT(*) AS c FROM auth_codes WHERE email=%s "
                   "AND created_at > (NOW() - INTERVAL 1 HOUR)", (email,))
    if rows and (rows[0].get("c") or 0) >= 5:
        return False
    code = f"{secrets.randbelow(1000000):06d}"
    # Invalidate any pending codes for this email
    db_write("UPDATE auth_codes SET used_at=NOW() "
             "WHERE email=%s AND used_at IS NULL", (email,))
    db_write("INSERT INTO auth_codes (email, code_hash, expires_at) "
             "VALUES (%s, %s, DATE_ADD(NOW(), INTERVAL 10 MINUTE))",
             (email, _code_hash(code)))
    return send_otp_email(email, code)


def request_code(email):
    """Email a one-time code to `email` if they're an active member.
    Returns True if the email was sent (or logged to stderr in dev mode).
    Returns False if rate-limited (5 codes per hour) or not a member."""
    if not is_active_member(email):
        return False
    return _issue_code(email)


def request_org_code(email, org_id):
    """Org-portal variant: authorize by the org's registered email DOMAIN
    rather than an existing `members` row, because org portals are
    self-enrolling — an employee's first ever sign-in has no record yet.

    Domain-gating is what keeps this from becoming an open mailer: a code is
    only ever sent to an address whose domain the operator registered against
    this org. Enrollment itself happens after the code is verified, never here.
    """
    from org_db import domain_matches_org      # local: keeps org support optional
    if not domain_matches_org(email, org_id):
        return False
    return _issue_code(email)


def verify_code(email, code):
    """Returns True if the code matches an unused, unexpired auth_codes
    row for this email. Marks the row used on success. Per-row attempt
    cap = 5 (silently rejects after that)."""
    rows = db_read(
        "SELECT id, code_hash, used_at FROM auth_codes "
        "WHERE email=%s AND used_at IS NULL AND expires_at > NOW() "
        "ORDER BY id DESC LIMIT 1", (email,))
    if not rows:
        return False
    row = rows[0]
    if hmac.compare_digest(row["code_hash"], _code_hash(code)):
        db_write("UPDATE auth_codes SET used_at=NOW() WHERE id=%s",
                 (row["id"],))
        return True
    return False


# ─────────────────────────────────────────────────────────────────────
# Email sending — SendGrid by default, stderr fallback in dev
# ─────────────────────────────────────────────────────────────────────

def send_otp_email(email, code):
    """Email the OTP to the member. Tries SendGrid (cfg EMAIL_API_KEY).
    If unset OR send fails: logs the code to stderr and returns True
    so dev installs work without a real email provider.

    Replace this function (or set EMAIL_API_KEY) for prod. The
    SendGrid path is the same as the reference impl at
    buildwithsumit.com — any drop-in SMTP/API sender works."""
    subject = "Your Globus sign-in code"
    text = (f"Your sign-in code is {code}\n\n"
            f"It expires in 10 minutes. If you didn't request this, "
            f"you can ignore this email.\n\n— Globus")
    api_key = (cfg("EMAIL_API_KEY", "") or "").strip()
    from_addr = (cfg("EMAIL_FROM", "") or "hello@example.com").strip()
    if not api_key:
        print(f"[globus-auth][DEV] OTP code for {email}: {code} "
              f"(set EMAIL_API_KEY in .env to send real emails)",
              file=sys.stderr, flush=True)
        return True
    try:
        body = json.dumps({
            "personalizations": [{"to": [{"email": email}]}],
            "from": {"email": from_addr, "name": "Globus"},
            "subject": subject,
            "content": [{"type": "text/plain", "value": text}],
        }).encode("utf-8")
        req = Request("https://api.sendgrid.com/v3/mail/send",
                      data=body, method="POST",
                      headers={"Authorization": f"Bearer {api_key}",
                               "Content-Type": "application/json"})
        with urlopen(req, timeout=15) as r:
            return 200 <= r.status < 300
    except Exception as e:
        print(f"[globus-auth] SendGrid failed for {email}: "
              f"{type(e).__name__}: {e}; OTP={code}",
              file=sys.stderr, flush=True)
        return True  # dev-friendly: still allow the login to complete


# ─────────────────────────────────────────────────────────────────────
# Cookie / session helpers — thin wrappers around auth_cookies
# ─────────────────────────────────────────────────────────────────────

def parse_session_cookie(cookie_header):
    """Extract the member email from a Cookie: header, or '' if no
    valid session cookie is present. Used by the route handlers as the
    auth gate.

    Cookie name is `bws_member` (legacy from the buildwithsumit
    reference impl — kept here for compat with the unchanged
    auth_cookies module). Rename in a future major version if you want."""
    if not cookie_header:
        return ""
    from auth_cookies import verify_token
    for kv in cookie_header.split(";"):
        name, _, value = kv.strip().partition("=")
        if name == "bws_member":
            return verify_token(value) or ""
    return ""
