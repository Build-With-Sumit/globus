"""Globus — main HTTP server entrypoint (v0.2).

Run:
    python3 server/globus_server.py

v0.2 ships a fully working text-chat path: sign up via OTP, upload an
Obsidian zip, chat with Globus over the zip. Voice + Google OAuth +
WhatsApp/Telegram bridges are v0.3 (see ROADMAP.md).

Module wiring order matters — see ARCHITECTURE.md § 2. This file
boots config + .env → wires every server/*.py module via their
configure(...) hooks → serves requests via a stdlib Handler.
"""
from __future__ import annotations
import json
import os
import sys
import base64
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs


# ─────────────────────────────────────────────────────────────────────
# 1. Load .env
# ─────────────────────────────────────────────────────────────────────

def _load_env(path):
    if not os.path.isfile(path):
        return
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(),
                                  v.strip().strip('"').strip("'"))


_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_load_env(os.path.join(_REPO_ROOT, ".env"))


# ─────────────────────────────────────────────────────────────────────
# 2. Required config — fail fast if missing
# ─────────────────────────────────────────────────────────────────────

DB_CFG = {
    "host":     os.environ.get("DB_HOST", "127.0.0.1"),
    "port":     int(os.environ.get("DB_PORT", "3306")),
    "user":     os.environ.get("DB_USER", "globus"),
    "password": os.environ.get("DB_PASSWORD", ""),
    "database": os.environ.get("DB_NAME", "globus"),
}
HOST = os.environ.get("GLOBUS_HOST", "127.0.0.1")
PORT = int(os.environ.get("GLOBUS_PORT", "8090"))
SITE = os.environ.get("SITE", f"http://{HOST}:{PORT}")
SESSION_SECRET_HEX = os.environ.get("SESSION_SECRET", "")

if not SESSION_SECRET_HEX:
    print("ERROR: SESSION_SECRET not set in .env. Generate with:",
          file=sys.stderr)
    print("  python3 -c 'import secrets; print(secrets.token_hex(32))'",
          file=sys.stderr)
    sys.exit(1)
SESSION_SECRET = bytes.fromhex(SESSION_SECRET_HEX)


# ─────────────────────────────────────────────────────────────────────
# 3. Wire modules — order matters (see ARCHITECTURE.md § 2)
# ─────────────────────────────────────────────────────────────────────

# Data layer — every other module imports from here.
import db_helpers  # noqa: E402
db_helpers.configure(db_cfg=DB_CFG)
from db_helpers import db_read, db_write, cfg  # noqa: E402

# HTML chrome.
import html_chrome  # noqa: E402
MEMBERS_DIR = os.path.join(_REPO_ROOT, "members")
html_chrome.configure(site=SITE, members_dir=MEMBERS_DIR)

# Voice + cookies + members + auth — both need SESSION_SECRET.
import voice_helpers  # noqa: E402
voice_helpers.configure(session_secret=SESSION_SECRET)

import auth_cookies  # noqa: E402
auth_cookies.configure(session_secret=SESSION_SECRET,
                       session_ttl=int(os.environ.get("SESSION_TTL_SEC",
                                                       str(30 * 86400))))

import members_db  # noqa: E402
members_db.configure(db_read=db_read, db_write=db_write)

import members_auth_html  # noqa: E402
members_auth_html.configure(site=SITE)

import globus_auth  # noqa: E402
globus_auth.configure(session_secret=SESSION_SECRET)

# Page builders + orchestrator import from above; no configure needed.
from public_globus_html import public_globus_landing_html  # noqa: E402
from globus_setup_html import globus_setup_html  # noqa: E402
from globus_chat_html import globus_chat_html  # noqa: E402
from vault_progress_html import vault_progress_html  # noqa: E402
from html_chrome import _page, _members_shell, esc  # noqa: E402
from globus_vault_db import (  # noqa: E402
    globus_get_vault, globus_extract_md_from_zip,
    globus_upsert_source, GLOBUS_VAULT_MAX_CHARS,
)
from vault_stats import vault_progress_stats  # noqa: E402
from globus_orchestrator import (  # noqa: E402
    globus_chat_send, globus_count_today_for_member,
    GLOBUS_DAILY_CAP,
)
from globus_auth import (  # noqa: E402
    request_code, verify_code, parse_session_cookie,
)
from auth_cookies import make_cookie, CLEAR_COOKIE  # noqa: E402


EMAIL_RE = __import__("re").compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


# ─────────────────────────────────────────────────────────────────────
# 4. Minimal page builders for the OSS install (no Google login button,
#    no "$99/month" CTA — those are buildwithsumit-specific)
# ─────────────────────────────────────────────────────────────────────

def _login_page(message=""):
    note = (f'<p class="form-note" style="color:#dc2626">{esc(message)}</p>'
            if message else "")
    body = (
        '<section class="section"><div class="container narrow center">'
        '<span class="eyebrow">Members</span>'
        '<h1>Sign in</h1>'
        '<p class="lead">Enter your email; we\'ll send a one-time code.</p>'
        '<form method="POST" action="/members/login" class="signup center" '
        'style="justify-content:center">'
        '<input type="email" name="email" required '
        'placeholder="you@example.com" aria-label="Email">'
        '<button class="btn btn-primary btn-lg" type="submit">'
        'Send code</button></form>'
        '<p class="muted small" style="margin-top:.6rem">'
        'Codes work for any registered member email. Members are '
        'provisioned via SQL — see INSTALL.md.</p>'
        f'{note}</div></section>')
    return _page("Sign in — Globus", body)


def _code_page(email, message=""):
    note = (f'<p class="form-note" style="color:#dc2626">{esc(message)}</p>'
            if message else "")
    body = (
        '<section class="section"><div class="container narrow center">'
        '<span class="eyebrow">Members</span>'
        '<h1>Enter your code</h1>'
        f'<p class="lead">We sent a 6-digit code to <strong>{esc(email)}</strong>.</p>'
        '<form method="POST" action="/members/login/code" class="signup center" '
        'style="justify-content:center">'
        f'<input type="hidden" name="email" value="{esc(email)}">'
        '<input type="text" name="code" required '
        'placeholder="123456" pattern="[0-9]{6}" '
        'inputmode="numeric" maxlength="6" aria-label="Code" '
        'style="font-family:ui-monospace,Menlo,monospace;'
        'letter-spacing:.3em;text-align:center">'
        '<button class="btn btn-primary btn-lg" type="submit">'
        'Verify</button></form>'
        '<p class="muted small" style="margin-top:.6rem">'
        '<a href="/members/login">&larr; Use a different email</a></p>'
        f'{note}</div></section>')
    return _page("Verify code — Globus", body)


def _members_landing(email):
    body = (
        '<span class="eyebrow">Members area</span>'
        f'<h1>Welcome back</h1>'
        f'<p class="lead">Signed in as <code>{esc(email)}</code>.</p>'
        '<div class="tools-grid">'
        '  <a class="tool-card" href="/members/globus">'
        '    <div class="tc-head"><div class="tc-title">'
        '      <span class="tc-icon">🧠</span> Globus</div></div>'
        '    <p class="tc-desc">Chat with your private AI over your '
        '    vault.</p><div class="tc-foot">Open &rarr;</div></a>'
        '  <a class="tool-card" href="/members/globus/setup">'
        '    <div class="tc-head"><div class="tc-title">'
        '      <span class="tc-icon">📂</span> Setup</div></div>'
        '    <p class="tc-desc">Upload an Obsidian vault or paste '
        '    markdown.</p><div class="tc-foot">Manage &rarr;</div></a>'
        '  <a class="tool-card" href="/members/vault-progress">'
        '    <div class="tc-head"><div class="tc-title">'
        '      <span class="tc-icon">📊</span> Vault progress</div></div>'
        '    <p class="tc-desc">Live build status of your indexed '
        '    notes.</p><div class="tc-foot">View &rarr;</div></a>'
        '</div>'
        '<p style="margin-top:2rem"><a href="/members/logout" '
        'class="muted small">Log out</a></p>')
    return _members_shell("Members · Globus", body)


# ─────────────────────────────────────────────────────────────────────
# 5. HTTP handler
# ─────────────────────────────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):
    server_version = "globus/0.2"

    def log_message(self, fmt, *args):
        return

    # ---- small utils ----
    def _send(self, code, body, content_type="text/html; charset=utf-8",
              extra_headers=None):
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        for h, v in (extra_headers or []):
            self.send_header(h, v)
        self.end_headers()
        self.wfile.write(data)

    def _send_html(self, code, body, extra_headers=None):
        self._send(code, body, "text/html; charset=utf-8", extra_headers)

    def _send_json(self, code, obj, extra_headers=None):
        self._send(code, json.dumps(obj), "application/json", extra_headers)

    def _redirect(self, location, extra_headers=None):
        self.send_response(302)
        self.send_header("Location", location)
        for h, v in (extra_headers or []):
            self.send_header(h, v)
        self.end_headers()

    def _member_email(self):
        """Return the authenticated member email or '' if not signed in."""
        return parse_session_cookie(self.headers.get("Cookie", ""))

    def _read_body(self, max_bytes=10 * 1024 * 1024):
        try:
            n = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            n = 0
        if n <= 0 or n > max_bytes:
            return b""
        return self.rfile.read(n)

    def _form(self):
        """Parse application/x-www-form-urlencoded into a dict."""
        body = self._read_body().decode("utf-8", errors="replace")
        out = {}
        for k, v in parse_qs(body, keep_blank_values=True).items():
            out[k] = v[0] if v else ""
        return out

    def _json(self):
        try:
            return json.loads(self._read_body().decode("utf-8")) or {}
        except Exception:
            return {}

    # ---- GET ----
    def do_GET(self):
        parsed = urlparse(self.path)
        route = parsed.path

        if route in ("/", "/globus", "/index.html"):
            return self._send_html(200, public_globus_landing_html())

        if route == "/api/health":
            return self._send_json(200,
                                    {"ok": True, "app": "globus", "v": "0.2"})

        # Static assets
        if route in ("/favicon.svg", "/styles.css", "/main.js"):
            path = os.path.join(_REPO_ROOT, "public", route.lstrip("/"))
            if os.path.isfile(path):
                ct = ("image/svg+xml" if route.endswith(".svg")
                      else "text/css" if route.endswith(".css")
                      else "application/javascript")
                with open(path, "rb") as fh:
                    return self._send(200, fh.read(), ct)

        # ---- Auth flow ----
        if route == "/members/login":
            return self._send_html(200, _login_page())

        if route == "/members/login/code":
            email = (parse_qs(parsed.query).get("email") or [""])[0]
            if not email:
                return self._redirect("/members/login")
            return self._send_html(200, _code_page(email))

        if route == "/members/logout":
            return self._redirect("/", [("Set-Cookie", CLEAR_COOKIE)])

        # ---- Auth-gated routes ----
        email = self._member_email()
        if route.startswith("/members") or route.startswith("/api/globus/"):
            if not email:
                if route.startswith("/api/"):
                    return self._send_json(401, {"error": "sign in"})
                return self._redirect("/members/login")

        if route == "/members":
            return self._send_html(200, _members_landing(email))

        if route == "/members/globus":
            vault = globus_get_vault(email)
            if not vault:
                return self._redirect("/members/globus/setup")
            msgs = []  # render empty; caller can history-fetch via JS later
            used = globus_count_today_for_member(email)
            try:
                vstats = vault_progress_stats(email)
            except Exception:
                vstats = None
            return self._send_html(200, globus_chat_html(
                email, vault, msgs, used, GLOBUS_DAILY_CAP, vstats))

        if route == "/members/globus/setup":
            return self._send_html(200, globus_setup_html(email))

        if route == "/members/vault-progress":
            return self._send_html(200, vault_progress_html(email))

        if route == "/api/globus/vault-progress":
            try:
                return self._send_json(200, vault_progress_stats(email))
            except Exception as e:
                return self._send_json(500,
                                        {"error": f"{type(e).__name__}: {e}"})

        if route == "/api/globus/agent-status":
            # v0.2: no agents wired by default. Return empty so the
            # chat-page agent console renders quietly.
            return self._send_json(200, {"running": [], "recent_runs": [],
                                          "latest_per_agent": {}})

        if route == "/api/globus/client-error":
            return self._send(204, b"")  # accept + drop

        return self._send_html(404, "<h1>404 — not found</h1>")

    # ---- POST ----
    def do_POST(self):
        parsed = urlparse(self.path)
        route = parsed.path

        # ---- Auth ----
        if route == "/members/login":
            form = self._form()
            email = (form.get("email") or "").strip().lower()
            if not EMAIL_RE.match(email):
                return self._send_html(200,
                                        _login_page("Enter a valid email."))
            if not request_code(email):
                return self._send_html(200, _login_page(
                    "We couldn't send a code — that email may not be "
                    "registered, or you've hit the per-hour limit."))
            return self._redirect(f"/members/login/code?email={email}")

        if route == "/members/login/code":
            form = self._form()
            email = (form.get("email") or "").strip().lower()
            code = (form.get("code") or "").strip()
            if not EMAIL_RE.match(email) or not code.isdigit() or len(code) != 6:
                return self._send_html(200, _code_page(email, "Bad code."))
            if not verify_code(email, code):
                return self._send_html(200, _code_page(email,
                                                        "Code wrong or expired."))
            return self._redirect("/members/globus",
                                  [("Set-Cookie", make_cookie(email))])

        # ---- Auth-gated POST routes ----
        email = self._member_email()
        if not email:
            return self._send_json(401, {"error": "sign in"})

        if route == "/members/globus/upload":
            payload = self._json()
            src = payload.get("source") or ""
            try:
                if src == "obsidian-zip":
                    zip_b64 = payload.get("zip_base64") or ""
                    zip_bytes = base64.b64decode(zip_b64)
                    text, fcount, tchars, truncated = globus_extract_md_from_zip(
                        zip_bytes, max_chars=GLOBUS_VAULT_MAX_CHARS)
                    if not text:
                        return self._send_json(400,
                                {"error": "no .md files found in zip"})
                    globus_upsert_source(
                        email, "obsidian-zip", text,
                        source_identifier="",
                        file_count=fcount,
                        source_label="Obsidian (zip upload)")
                    return self._send_json(200, {
                        "ok": True, "file_count": fcount,
                        "char_count": tchars, "truncated": truncated,
                    })
                if src == "paste":
                    text = (payload.get("markdown") or "").strip()
                    if not text:
                        return self._send_json(400,
                                {"error": "empty paste"})
                    text = text[:GLOBUS_VAULT_MAX_CHARS]
                    globus_upsert_source(
                        email, "obsidian-paste", text,
                        source_identifier="",
                        file_count=None,
                        source_label="Pasted notes")
                    return self._send_json(200, {
                        "ok": True, "char_count": len(text),
                    })
                return self._send_json(400,
                        {"error": f"unknown source: {src!r}"})
            except Exception as e:
                return self._send_json(500,
                        {"error": f"{type(e).__name__}: {e}"})

        if route == "/members/globus/chat":
            payload = self._json()
            msg = (payload.get("message") or "").strip()
            if not msg:
                return self._send_json(400, {"error": "empty message"})
            used = globus_count_today_for_member(email)
            if used >= GLOBUS_DAILY_CAP:
                return self._send_json(429, {
                    "error": f"daily cap reached ({GLOBUS_DAILY_CAP} "
                             f"messages); resets at 00:00 UTC"})
            try:
                reply, usage = globus_chat_send(email, msg)
                return self._send_json(200, {"reply": reply, "usage": usage})
            except Exception as e:
                return self._send_json(500,
                        {"error": f"{type(e).__name__}: {e}"})

        if route == "/api/globus/client-error":
            return self._send(204, b"")

        return self._send_html(404, "<h1>404 — not found</h1>")


def main():
    print(f"globus/0.2 booting on {HOST}:{PORT}", flush=True)
    print(f"  site:     {SITE}", flush=True)
    print(f"  db:       {DB_CFG['user']}@{DB_CFG['host']}:{DB_CFG['port']}/"
          f"{DB_CFG['database']}", flush=True)
    print(f"  llm:      {cfg('GLOBUS_LLM_PROVIDER', 'claude-oauth')}",
          flush=True)
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nshutting down", flush=True)
        server.shutdown()


if __name__ == "__main__":
    main()
