"""Globus — main HTTP server entrypoint.

Run:
    python3 server/globus_server.py

This is a deliberately small bootstrap file. The actual Globus logic
lives in the sibling modules (see ARCHITECTURE.md for the module map):

  - db_helpers          — MySQL wrappers + cfg() (DB-first config)
  - html_chrome         — shared HTML primitives + members CSS
  - globus_chrome       — Globus-specific page chrome
  - voice_helpers       — voice-token mint/verify
  - voice_providers     — DeepSeek voice streaming
  - members_db          — member CRUD + is_active_member()
  - members_auth_html   — login + OTP pages
  - members_landing_html — /members landing
  - auth_cookies        — session cookie HMAC
  - globus_setup_html   — vault setup page
  - globus_chat_html    — chat page (voice orb + transcript + composer)
  - globus_briefs_html  — single-brief viewer
  - globus_agents_html  — agents dashboard + sidebar
  - globus_agents_helpers — _ga_* filesystem helpers
  - globus_agents_catalog — agent metadata (CUSTOMIZE)
  - globus_tools_schema — LLM tool schemas (OpenAI shape)
  - globus_vault_db     — vault sources + chat history CRUD
  - globus_search       — pure-DB LLM search tools
  - globus_llm          — Claude OAuth / Anthropic / DeepSeek wrappers
  - globus_chat_helpers — capabilities block + tool instructions + markup scrubber
  - agents_runtime      — per-member dir plumbing + agent runner
  - vault_stats         — vault progress stats (memoized)
  - vault_progress_html — live vault progress page
  - members_connect_html — /members/connect data-sources page
  - connectors_html     — WhatsApp / Teams / Telegram setup pages
  - public_globus_html  — public /globus landing

This file is intentionally small. It boots the modules in the right
order, wires up the HTTP handler, and serves requests. The actual
route handlers + chat orchestrator + tool implementations were
extracted from a larger monolith (lead_server.py in the buildwithsumit
reference impl). Porting the remaining tool implementations
(globus_read_file, globus_list_recent_emails,
globus_send_telegram_via_bot, the voice path) is the v0.2 milestone —
see ROADMAP.md.

For v0.1, this file:
  - Boots the config + module-configure chain correctly
  - Serves the public /globus landing
  - Serves static assets (favicon, styles)
  - Returns a friendly placeholder for /members/globus and friends
    (with a one-line "this is a v0.1 skeleton" note pointing at
    ROADMAP.md so installers know what's wired up vs what's coming)

Once you've completed v0.2, this file's Handler class becomes the
single source of truth for routing. PRs welcome.
"""
from __future__ import annotations
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse


# ─────────────────────────────────────────────────────────────────────
# 1. Load .env (simple parser; no python-dotenv dep)
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
            k = k.strip()
            v = v.strip().strip('"').strip("'")
            os.environ.setdefault(k, v)


_load_env(os.path.join(os.path.dirname(__file__), "..", ".env"))


# ─────────────────────────────────────────────────────────────────────
# 2. Required config (fail fast if missing)
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
# 3. Wire up modules (order matters — see module docstrings)
# ─────────────────────────────────────────────────────────────────────

# DB layer + cfg() must be first — every other module reads from it.
import db_helpers  # noqa: E402
db_helpers.configure(db_cfg=DB_CFG)
from db_helpers import db_read, db_write, cfg  # noqa: E402

# HTML chrome — every page uses _page(), esc(), _members_shell().
import html_chrome  # noqa: E402
MEMBERS_DIR = os.path.join(os.path.dirname(__file__), "..", "members")
html_chrome.configure(site=SITE, members_dir=MEMBERS_DIR)

# Voice + auth + members. Voice + auth need SESSION_SECRET.
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

# Page builders + helper modules don't need explicit configure() —
# they read from db_helpers / html_chrome lazily at first call.

from public_globus_html import public_globus_landing_html  # noqa: E402


# ─────────────────────────────────────────────────────────────────────
# 4. HTTP handler — v0.1 minimal routes
# ─────────────────────────────────────────────────────────────────────

_PLACEHOLDER_BODY = """\
<section class="section"><div class="container narrow">
  <h1>Globus — v0.1 skeleton</h1>
  <p class="lead">This route is part of the reference implementation
  but isn't wired up yet in this minimal install. The full
  implementation (chat orchestrator + tool-use loop + voice path +
  background agents) is tracked in
  <a href="https://github.com/Build-With-Sumit/globus/blob/main/ROADMAP.md">ROADMAP.md</a>.</p>
  <p>For now: read <a href="https://github.com/Build-With-Sumit/globus/blob/main/ARCHITECTURE.md">ARCHITECTURE.md</a>
  for the module map and what's available in each.</p>
  <p><a href="/globus">&larr; Back to the public landing</a></p>
</div></section>"""


class Handler(BaseHTTPRequestHandler):
    server_version = "globus/0.1"

    def log_message(self, fmt, *args):
        # Quiet the default per-request access log. Use a proper
        # access-log setup (nginx in front + structured app logs) in
        # production.
        return

    def _send(self, code, body, content_type="text/html; charset=utf-8"):
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_html(self, code, body):
        self._send(code, body, "text/html; charset=utf-8")

    def do_GET(self):
        parsed = urlparse(self.path)
        route = parsed.path

        if route in ("/", "/globus", "/index.html"):
            return self._send_html(200, public_globus_landing_html())

        if route == "/api/health":
            return self._send(200, '{"ok":true,"app":"globus","v":"0.1"}',
                              "application/json")

        # Members + chat + voice + agents routes are part of the
        # ROADMAP.md v0.2 work. Until those are ported, return a
        # friendly skeleton page so installers can tell the stub apart
        # from a real 404.
        if route.startswith(("/members", "/api/globus")):
            from html_chrome import _page
            return self._send_html(200, _page("Globus — coming in v0.2",
                                              _PLACEHOLDER_BODY))

        # Static assets — the reference HTML expects /favicon.svg + /styles.css
        # in the public/ dir. Serve them if present, 404 if not.
        if route in ("/favicon.svg", "/styles.css", "/main.js"):
            public_dir = os.path.join(os.path.dirname(__file__),
                                      "..", "public")
            path = os.path.join(public_dir, route.lstrip("/"))
            if os.path.isfile(path):
                ct = ("image/svg+xml" if route.endswith(".svg")
                      else "text/css" if route.endswith(".css")
                      else "application/javascript")
                with open(path, "rb") as fh:
                    return self._send(200, fh.read(), ct)

        return self._send_html(404, "<h1>404 — not found</h1>")

    def do_POST(self):
        # All POST routes (chat send, vault upload, OAuth callbacks,
        # voice LLM endpoint) are v0.2 work. See ROADMAP.md.
        return self._send_html(501,
            "<h1>501 — not implemented in v0.1</h1>"
            "<p>See "
            "<a href='https://github.com/Build-With-Sumit/globus/blob/main/ROADMAP.md'>"
            "ROADMAP.md</a> for the v0.2 plan.</p>")


def main():
    print(f"globus/0.1 booting on {HOST}:{PORT}", flush=True)
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
