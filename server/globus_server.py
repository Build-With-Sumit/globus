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
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, quote


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

# bridge_ingest signs the long-TTL Chrome-extension tokens for WA + Teams.
# Same SESSION_SECRET so any rotation invalidates voice + bridge tokens
# in lock-step (good — minimises stale-credential blast radius).
import bridge_ingest  # noqa: E402
bridge_ingest.configure(session_secret=SESSION_SECRET)

# voice_providers needs a DeepSeek key getter (lazy — re-reads cfg() on
# each call) + the default model name shown in OpenAI-shape responses.
# Both safe to configure even on installs that don't enable voice — the
# module just sits idle if /api/globus/voice-llm/* is never hit.
import voice_providers  # noqa: E402

def _voice_default_model():
    # cfg() is evaluated at boot — voice_providers re-evaluates the
    # DEEPSEEK_API_KEY getter on every call, but the model name is
    # captured here. Override with VOICE_DEFAULT_MODEL in config.
    return cfg("VOICE_DEFAULT_MODEL", "claude-sonnet-4-6")

voice_providers.configure(
    deepseek_api_key_getter=lambda: (cfg("DEEPSEEK_API_KEY", "") or "").strip(),
    default_model=_voice_default_model())

# Narada plugin auto-loader — imports every server/narada_plugins/*.py,
# which triggers each plugin's module-level register() call. Safe even
# on installs that don't use Narada (no plugins = empty registry).
# Called AFTER db_helpers + oauth_db are configured because plugins
# may import narada_creds which talks to both.
import narada_plugins  # noqa: E402
narada_plugins.load_all_plugins()

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

# Google OAuth (Drive sync) — only relevant if GOOGLE_OAUTH_CLIENT_ID/SECRET
# are set in the config table. configure() always runs; the helpers raise a
# friendly RuntimeError if a client tries to start the flow without keys.
import google_oauth  # noqa: E402
google_oauth.configure(site=SITE)

# Page builders + orchestrator import from above; no configure needed.
from public_globus_html import public_globus_landing_html  # noqa: E402
from globus_setup_html import globus_setup_html  # noqa: E402
from globus_chat_html import globus_chat_html  # noqa: E402
from vault_progress_html import vault_progress_html  # noqa: E402
from members_connect_html import members_connect_html  # noqa: E402
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
from google_oauth import (  # noqa: E402
    create_oauth_state, consume_oauth_state,
    google_authorize_url, google_exchange_code, google_userinfo,
    google_revoke,
)
from oauth_db import (  # noqa: E402
    list_oauth_connections, list_oauth_connections_with_stats,
    count_oauth_connections, get_oauth_connection,
    upsert_oauth_connection, delete_oauth_connection, decrypt_token,
)
from sync_drive import (  # noqa: E402
    sync_connection_async, start_background_sync_worker,
)
from voice_helpers import voice_token_make  # noqa: E402
from voice_route import (  # noqa: E402
    authenticate_voice_request, voice_chat_handle, voice_chat_format_response,
)
from bridge_ingest import (  # noqa: E402
    whatsapp_token_make, whatsapp_token_verify,
    whatsapp_ingest_messages, teams_ingest_messages,
    BRIDGE_INGEST_MAX_BYTES, BRIDGE_INGEST_MAX_MESSAGES,
)
from connectors_html import whatsapp_setup_html  # noqa: E402
from agent_runner import (  # noqa: E402
    agent_status, agent_run_async, run_agent_for_member,
    catalog_for_member, find_agent,
)
from agents_dashboard_html import agents_dashboard_html  # noqa: E402
from telegram_bot_setup_html import telegram_bot_setup_html  # noqa: E402
from public_chat import public_chat_send, is_enabled as _public_enabled  # noqa: E402

# Narada (outbound agent) imports — kept after narada_plugins.load_all_plugins()
# above so the registry is populated before any route handler runs.
import narada_core  # noqa: E402
import narada_creds  # noqa: E402
from narada_html import (  # noqa: E402
    narada_dashboard_html, narada_credentials_html,
    narada_new_campaign_html, narada_campaign_detail_html,
)
from narada_plugins import (  # noqa: E402
    get_lead_source, get_verifier, get_sender, get_crm,
    list_available_for_member,
)
from narada_plugins.types import (  # noqa: E402
    ICPFilters, Lead, PluginCategory, SendStatus, VerifyStatus,
)
import urllib.request as _urlreq  # noqa: E402
import urllib.error as _urlerr  # noqa: E402


EMAIL_RE = __import__("re").compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# How many Google accounts a member can connect.
GLOBUS_MAX_CONNECTIONS_PER_MEMBER = int(
    os.environ.get("GLOBUS_MAX_CONNECTIONS_PER_MEMBER", "5"))


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
        '  <a class="tool-card" href="/members/connect">'
        '    <div class="tc-head"><div class="tc-title">'
        '      <span class="tc-icon">🔗</span> Connect data</div></div>'
        '    <p class="tc-desc">Sync Google Drive + Gmail into your vault.</p>'
        '    <div class="tc-foot">Connect &rarr;</div></a>'
        '  <a class="tool-card" href="/members/whatsapp">'
        '    <div class="tc-head"><div class="tc-title">'
        '      <span class="tc-icon">💬</span> Teams &amp; WhatsApp</div></div>'
        '    <p class="tc-desc">Chrome extension bridge for chat history.</p>'
        '    <div class="tc-foot">Pair &rarr;</div></a>'
        '  <a class="tool-card" href="/members/globus/agents">'
        '    <div class="tc-head"><div class="tc-title">'
        '      <span class="tc-icon">🤖</span> Agents</div></div>'
        '    <p class="tc-desc">Background tasks that produce daily briefs.</p>'
        '    <div class="tc-foot">Open &rarr;</div></a>'
        '  <a class="tool-card" href="/members/narada">'
        '    <div class="tc-head"><div class="tc-title">'
        '      <span class="tc-icon">📣</span> Narada (Outbound)</div></div>'
        '    <p class="tc-desc">End-to-end cold outreach campaigns.</p>'
        '    <div class="tc-foot">Open &rarr;</div></a>'
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
# Deep health probe — invoked by GET /api/health?deep=1. Each check
# returns ok/error individually so the operator can see which piece
# of the install is broken. Returns (http_status, body_dict).
# ─────────────────────────────────────────────────────────────────────

def _deep_health():
    checks = {}
    overall_ok = True

    # DB ping
    try:
        rows = db_read("SELECT 1 AS one")
        checks["db"] = {
            "ok": bool(rows and rows[0].get("one") == 1),
            "host": DB_CFG["host"], "database": DB_CFG["database"]}
    except Exception as e:
        checks["db"] = {"ok": False,
                         "error": f"{type(e).__name__}: {e}"[:200]}
        overall_ok = False

    # Storage probe — confirm we can write to the agent + raw-data dirs.
    for name, env_key, default_path in [
        ("agents_dir", "GLOBUS_AGENTS_WORK_DIR", "/var/lib/globus/agents"),
        ("raw_data_dir", "GLOBUS_RAW_DATA_DIR", "/var/lib/globus/raw-data"),
    ]:
        path = os.environ.get(env_key, default_path)
        probe = os.path.join(path, ".health_probe")
        try:
            os.makedirs(path, exist_ok=True)
            with open(probe, "w") as f:
                f.write("ok")
            os.remove(probe)
            checks[name] = {"ok": True, "path": path}
        except Exception as e:
            checks[name] = {"ok": False, "path": path,
                             "error": f"{type(e).__name__}: {e}"[:200]}
            overall_ok = False

    # Fernet sanity — encrypt + decrypt a known string to verify the
    # GLOBUS_OAUTH_ENCRYPTION_KEY config is set and parseable. Skipped
    # if the install hasn't enabled OAuth (no key configured).
    try:
        if cfg("GLOBUS_OAUTH_ENCRYPTION_KEY", ""):
            from oauth_db import encrypt_token, decrypt_token
            r = decrypt_token(encrypt_token("health-probe"))
            checks["fernet"] = {"ok": r == "health-probe"}
        else:
            checks["fernet"] = {"ok": True,
                                 "skipped": "no GLOBUS_OAUTH_ENCRYPTION_KEY"}
    except Exception as e:
        checks["fernet"] = {"ok": False,
                             "error": f"{type(e).__name__}: {e}"[:200]}
        overall_ok = False

    # Persona file — warn (but don't fail) if running on the example.
    persona_path = os.path.join(_REPO_ROOT, "config", "persona.md")
    if os.path.isfile(persona_path):
        checks["persona"] = {"ok": True, "source": "config/persona.md"}
    elif os.path.isfile(os.path.join(_REPO_ROOT, "config",
                                       "persona.example.md")):
        checks["persona"] = {"ok": True,
                              "source": "config/persona.example.md",
                              "warning": "copy persona.example.md to "
                                          "persona.md and customise"}
    else:
        checks["persona"] = {"ok": False,
                              "error": "no persona file found"}
        overall_ok = False

    # LLM provider — just report what's configured. Don't ping the
    # provider (could be costly + slow).
    checks["llm"] = {"ok": True,
                      "provider": cfg("GLOBUS_LLM_PROVIDER",
                                       "claude-oauth")}

    return (200 if overall_ok else 503), {
        "ok": overall_ok, "app": "globus", "v": "0.6",
        "checks": checks,
    }


# ─────────────────────────────────────────────────────────────────────
# 5. HTTP handler
# ─────────────────────────────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):
    server_version = "globus/0.6"

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

    def _narada_run_action(self, email, camp, action, form):
        """Dispatch a campaign-detail POST action to the right plugin.
        Returns {"kind": "ok"|"error", "msg": "..."} for the redirect
        banner. Never raises — every failure surfaces as a banner so
        the marketer always sees what happened."""
        cid = int(camp["id"])

        if action == "import":
            leads = narada_core.parse_pasted_leads(form.get("leads") or "")
            if not leads:
                return {"kind": "error",
                        "msg": "no valid emails found — paste one lead per "
                               "line (email, or 'email, first, last, "
                               "company')"}
            res = narada_core.add_prospects(email, cid, leads)
            return {"kind": "ok",
                    "msg": f"Imported {res['added']} lead(s) "
                           f"(dup {res['skipped_dup']}, "
                           f"suppressed {res['skipped_suppressed']})."}

        if action == "find-leads":
            ls_slug = camp.get("lead_source") or ""
            ls = get_lead_source(ls_slug)
            if not ls:
                return {"kind": "error",
                        "msg": f"lead source {ls_slug!r} not registered"}
            try:
                count = max(1, min(int(form.get("count") or 50), 500))
            except ValueError:
                count = 50
            # Parse the free-text icp_description into STRUCTURED filters
            # (roles/seniority/locations/…) so the lead source actually
            # searches; cached on the campaign after the first parse.
            from narada_copy import build_icp
            icp = build_icp(camp)
            try:
                leads = ls.search(email, icp, count=count)
            except Exception as e:
                return {"kind": "error",
                        "msg": f"search failed: {type(e).__name__}"}
            res = narada_core.add_prospects(email, cid, leads)
            return {"kind": "ok",
                    "msg": f"Found {len(leads)} leads, added "
                            f"{res['added']} (dup {res['skipped_dup']}, "
                            f"suppressed {res['skipped_suppressed']})."}

        if action == "verify":
            v_slug = camp.get("verifier") or ""
            v = get_verifier(v_slug) if v_slug else None
            if not v:
                return {"kind": "error",
                        "msg": "no verifier configured for this campaign"}
            prospects = narada_core.list_prospects(email, cid, status="new")
            ok = bad = 0
            for p in prospects[:50]:  # cap per click; rerun for more
                result = v.verify(email, p["email"])
                if result.status == VerifyStatus.VALID:
                    narada_core.set_prospect_verified(email, p["id"], True)
                    ok += 1
                elif result.status == VerifyStatus.INVALID:
                    narada_core.set_prospect_verified(email, p["id"], False)
                    bad += 1
            return {"kind": "ok",
                    "msg": f"Verified {ok+bad} ({ok} valid / {bad} invalid)."}

        if action == "draft":
            # Lazy-import the LLM module so callers without LLM config
            # don't pay the import cost on every Narada page load.
            from narada_copy import draft_copy_for_prospect
            prospects = [p for p in narada_core.list_prospects(email, cid)
                          if p["status"] in ("new", "verified", "enriched")]
            drafted = 0
            for p in prospects[:20]:  # cap for budget; rerun for more
                variants = draft_copy_for_prospect(
                    member_email=email,
                    product=camp.get("product") or "",
                    prospect=p,
                    campaign_icp_description=camp.get("icp_description") or "")
                if variants:
                    narada_core.set_prospect_copy(email, p["id"], variants)
                    drafted += 1
            return {"kind": "ok",
                    "msg": f"Drafted copy for {drafted} prospect(s). "
                            "Review + approve before sending."}

        if action == "send":
            sender_slug = camp.get("sender") or ""
            sender = get_sender(sender_slug)
            if not sender:
                return {"kind": "error",
                        "msg": f"sender {sender_slug!r} not registered"}
            if not sender.is_available(email):
                return {"kind": "error",
                        "msg": f"sender {sender_slug!r} not connected. "
                                "Set up at /members/narada/credentials."}
            # In autopilot mode, treat all DRAFTED as approved; else
            # only send APPROVED.
            target_status = ("drafted"
                              if camp.get("send_mode") == "autopilot"
                              else "approved")
            prospects = narada_core.list_prospects(
                email, cid, status=target_status)
            cap = sender.daily_send_cap(email)
            sent = failed = 0
            from_addr = (narada_core.sender_config_of(camp).get("from_addr")
                         or email)
            for p in prospects[:cap]:
                variants = p.get("copy_variants") or []
                if isinstance(variants, str):
                    try:
                        variants = json.loads(variants)
                    except Exception:
                        variants = []
                idx = p.get("approved_variant_idx")
                if idx is None and camp.get("send_mode") == "autopilot":
                    idx = 0  # autopilot picks the first variant
                if idx is None or not (0 <= idx < len(variants)):
                    continue
                v = variants[idx]
                send_id = narada_core.queue_send(
                    email, cid, p["id"],
                    from_addr=from_addr,
                    subject=v.get("subject") or "",
                    body=v.get("body") or "",
                    sender_slug=sender_slug)
                if not send_id:
                    continue  # suppressed
                try:
                    result = sender.send(
                        member_email=email, from_addr=from_addr,
                        to=p["email"], subject=v.get("subject") or "",
                        body=v.get("body") or "")
                except Exception as e:
                    narada_core.mark_send_failed(
                        send_id, f"{type(e).__name__}: {e}")
                    failed += 1
                    continue
                if result.status == SendStatus.SENT:
                    narada_core.mark_send_sent(
                        send_id, result.message_id,
                        result.thread_id, result.external_id)
                    narada_core.update_prospect_status(
                        email, p["id"], "sent")
                    sent += 1
                else:
                    narada_core.mark_send_failed(send_id, result.error)
                    failed += 1
            if sent > 0:
                narada_core.update_campaign_status(email, cid, "sending")
            return {"kind": "ok",
                    "msg": f"Sent {sent} ({failed} failed)."}

        if action == "check-replies":
            sender_slug = camp.get("sender") or ""
            sender = get_sender(sender_slug)
            if not sender:
                return {"kind": "error",
                        "msg": f"sender {sender_slug!r} not registered"}
            # Look back 7 days for replies. Sender plugin pulls inbound
            # via its native channel (Gmail API for gmail plugin, webhook
            # cache for SaaS senders).
            from datetime import datetime, timedelta
            since = datetime.utcnow() - timedelta(days=7)
            try:
                replies = sender.detect_replies(email, since=since)
            except Exception as e:
                return {"kind": "error",
                        "msg": f"reply check failed: {type(e).__name__}"}
            # Match each reply to a send by Message-ID. Simple v1: only
            # classify replies whose in-reply-to matches a send row.
            matched = 0
            from db_helpers import db_read
            for r in replies:
                if not r.in_reply_to_message_id:
                    continue
                rows = db_read(
                    "SELECT id FROM globus_narada_sends "
                    "WHERE member_email=%s AND campaign_id=%s "
                    "  AND message_id=%s LIMIT 1",
                    (email, cid, r.in_reply_to_message_id))
                if rows:
                    # v1 classification: any reply that isn't auto-out-
                    # of-office is "interested" until we wire a real
                    # classifier (v2). Even crude classification helps.
                    body_lower = (r.body or "").lower()
                    if "out of office" in body_lower or "auto reply" in body_lower:
                        cls = "ooo"
                    elif "unsubscribe" in body_lower or "remove" in body_lower:
                        cls = "unsubscribe"
                    else:
                        cls = "interested"
                    narada_core.record_reply(
                        rows[0]["id"], cls, r.body)
                    matched += 1
            return {"kind": "ok",
                    "msg": f"Checked {len(replies)} message(s); "
                            f"matched {matched} as replies."}

        return {"kind": "error", "msg": f"unknown action: {action}"}

    # ---- GET ----
    def do_GET(self):
        parsed = urlparse(self.path)
        route = parsed.path

        if route in ("/", "/globus", "/index.html"):
            return self._send_html(200, public_globus_landing_html(
                public_chat_enabled=_public_enabled()))

        if route == "/api/health":
            # Cheap by default — used by Docker HEALTHCHECK + load
            # balancers. `?deep=1` runs a full DB ping + storage probe
            # + Fernet sanity + persona check (slower; use for ops).
            qs = parse_qs(parsed.query)
            deep = bool((qs.get("deep") or [""])[0])
            if not deep:
                return self._send_json(
                    200, {"ok": True, "app": "globus", "v": "0.6"})
            return self._send_json(*_deep_health())

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

        if route == "/members/connect":
            qs = parse_qs(parsed.query)
            msg = (qs.get("msg") or [""])[0]
            kind = (qs.get("kind") or [""])[0]
            connections = list_oauth_connections_with_stats(email)
            return self._send_html(200, members_connect_html(
                email, connections, GLOBUS_MAX_CONNECTIONS_PER_MEMBER,
                message=msg or None,
                message_kind=("error" if kind == "error" else "ok")))

        if route == "/members/connect/google/start":
            if count_oauth_connections(email) >= GLOBUS_MAX_CONNECTIONS_PER_MEMBER:
                return self._redirect(
                    "/members/connect?kind=error&msg="
                    + quote(f"Maximum of {GLOBUS_MAX_CONNECTIONS_PER_MEMBER} "
                            "Google accounts reached."))
            qs = parse_qs(parsed.query)
            sources = []
            if (qs.get("drive") or [""])[0]:
                sources.append("drive")
            if (qs.get("gmail") or [""])[0]:
                sources.append("gmail")
            # Analytics + Teams land in later phases; reject here so users
            # get a clear error rather than a sync that silently no-ops.
            if not sources:
                return self._redirect(
                    "/members/connect?kind=error&msg="
                    + quote("Pick at least one source (Drive or Gmail)."))
            try:
                state = create_oauth_state(email, "google", ",".join(sources))
                url = google_authorize_url(state, sources)
            except RuntimeError as e:
                return self._redirect(
                    "/members/connect?kind=error&msg=" + quote(str(e)))
            return self._redirect(url)

        if route == "/members/connect/google/callback":
            qs = parse_qs(parsed.query)
            state = (qs.get("state") or [""])[0]
            code = (qs.get("code") or [""])[0]
            err = (qs.get("error") or [""])[0]
            if err:
                return self._redirect(
                    "/members/connect?kind=error&msg="
                    + quote(f"Google returned: {err}"))
            if not (state and code):
                return self._redirect(
                    "/members/connect?kind=error&msg="
                    + quote("Missing state or code from Google."))
            st = consume_oauth_state(state)
            if not st:
                return self._redirect(
                    "/members/connect?kind=error&msg="
                    + quote("OAuth state expired or invalid — try again."))
            owner_email = st["email"]
            if owner_email != email:
                return self._redirect(
                    "/members/connect?kind=error&msg="
                    + quote("Session/member mismatch — please retry."))
            try:
                tokens = google_exchange_code(code)
            except Exception as e:
                return self._redirect(
                    "/members/connect?kind=error&msg="
                    + quote(f"Token exchange failed: {type(e).__name__}"))
            refresh = tokens.get("refresh_token")
            access = tokens.get("access_token")
            scopes_str = tokens.get("scope", "")
            expires_in = int(tokens.get("expires_in", 3600))
            expires_at = datetime.utcnow() + timedelta(seconds=expires_in)
            if not (refresh and access):
                return self._redirect(
                    "/members/connect?kind=error&msg="
                    + quote("Google did not return a refresh token — try again."))
            try:
                info = google_userinfo(access)
            except Exception:
                info = {}
            account = (info.get("email") or "").lower()
            if not account:
                return self._redirect(
                    "/members/connect?kind=error&msg="
                    + quote("Could not read Google account email."))
            # Re-check cap with the now-known account (upserts of an existing
            # one don't grow the count).
            existing_accounts = {c["provider_account"]
                                 for c in list_oauth_connections(email)}
            if (account not in existing_accounts
                    and len(existing_accounts) >= GLOBUS_MAX_CONNECTIONS_PER_MEMBER):
                google_revoke(refresh)
                return self._redirect(
                    "/members/connect?kind=error&msg="
                    + quote(f"Maximum of {GLOBUS_MAX_CONNECTIONS_PER_MEMBER} "
                            "accounts reached."))
            upsert_oauth_connection(
                email=email,
                provider_account=account,
                scopes=scopes_str,
                refresh_token=refresh,
                access_token=access,
                expires_at=expires_at,
                user_info=info,
                source_types=st["source_types"])
            for c in list_oauth_connections(email):
                if c["provider_account"] == account:
                    sync_connection_async(c["id"], email)
                    break
            return self._redirect(
                "/members/connect?kind=ok&msg="
                + quote(f"Connected {account}. First sync started in the background."))

        if route == "/members/narada":
            qs = parse_qs(parsed.query)
            return self._send_html(200, narada_dashboard_html(
                email, narada_core.list_campaigns(email),
                message=(qs.get("msg") or [""])[0] or None,
                kind=(qs.get("kind") or [""])[0] or None))

        if route == "/members/narada/credentials":
            qs = parse_qs(parsed.query)
            configured = {r["tool"] for r in
                           narada_creds.list_member_credentials(email)}
            return self._send_html(200, narada_credentials_html(
                email, configured,
                message=(qs.get("msg") or [""])[0] or None,
                kind=(qs.get("kind") or [""])[0] or None))

        if route == "/members/narada/new":
            qs = parse_qs(parsed.query)
            return self._send_html(200, narada_new_campaign_html(
                email,
                message=(qs.get("msg") or [""])[0] or None,
                kind=(qs.get("kind") or [""])[0] or None,
                send_from_accounts=narada_core.member_send_accounts(email)))

        # Campaign detail — /members/narada/<int>
        if route.startswith("/members/narada/"):
            tail = route.removeprefix("/members/narada/").rstrip("/")
            if tail.isdigit():
                cid = int(tail)
                camp = narada_core.get_campaign(email, cid)
                if not camp:
                    return self._send_html(404, "<h1>campaign not found</h1>")
                qs = parse_qs(parsed.query)
                return self._send_html(200, narada_campaign_detail_html(
                    email, camp,
                    narada_core.list_prospects(email, cid),
                    narada_core.campaign_stats(email, cid),
                    message=(qs.get("msg") or [""])[0] or None,
                    kind=(qs.get("kind") or [""])[0] or None))

        if route == "/members/whatsapp":
            # Settings page for the Chrome-extension pairing flow.
            # Mints a fresh 90d HMAC token on every render so any old
            # leaked token gets superseded the next time the member
            # opens this page.
            return self._send_html(200, whatsapp_setup_html(
                email, whatsapp_token_make(email)))

        if route == "/members/globus/agents":
            return self._send_html(200, agents_dashboard_html(
                email,
                catalog_for_member(email),
                agent_status(email=email)))

        if route == "/members/telegram/bot":
            qs = parse_qs(parsed.query)
            return self._send_html(200, telegram_bot_setup_html(
                email,
                message=(qs.get("msg") or [""])[0] or None,
                message_kind=(qs.get("kind") or [""])[0] or None))

        if route == "/members/vault-progress":
            return self._send_html(200, vault_progress_html(email))

        if route == "/api/globus/vault-progress":
            try:
                return self._send_json(200, vault_progress_stats(email))
            except Exception as e:
                return self._send_json(500,
                                        {"error": f"{type(e).__name__}: {e}"})

        if route == "/api/globus/agent-status":
            # Powers the chat-page activity console. v0.5+: backed by
            # the globus_agent_runs table via agent_runner.agent_status.
            return self._send_json(200, agent_status(email=email))

        if route == "/api/globus/voice-token":
            # Cookie-authed refresh route — used by the chat page if
            # the embedded token expires during a long session. The
            # token is also embedded at render time so most loads
            # never need this.
            return self._send_json(200, {"token": voice_token_make(email)})

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

        # ---- Public preview chat (no auth, IP rate-limited) ----
        # Opt-in via GLOBUS_PUBLIC_CHAT_ENABLED. Default off — fresh
        # installs ship safe; no anonymous LLM spend until operator
        # explicitly turns it on.
        if route == "/api/public/chat":
            if not _public_enabled():
                return self._send_json(404, {"error": "not found"})
            # Trust X-Forwarded-For if present (nginx + co set it on
            # reverse-proxy installs); fall back to the socket peer.
            # Take only the leftmost address — the chain is
            # client, proxy1, proxy2, ... and we want the client.
            fwd = self.headers.get("X-Forwarded-For", "")
            client_ip = (fwd.split(",")[0].strip()
                          if fwd else self.client_address[0])
            ua = (self.headers.get("User-Agent") or "")[:255]
            payload = self._json()
            msg = (payload.get("message") or "").strip()
            result = public_chat_send(client_ip, ua, msg)
            return self._send_json(200 if result.get("ok") else 429,
                                    result)

        # ---- Bridge ingest (extension-token auth, NOT cookie) ----
        # WhatsApp Web + Teams personal — same Chrome extension, two
        # endpoints. Auth: `Authorization: Bearer <wa-token>`. The body
        # is read with a higher cap (4MB) so a single batch of 500 WA
        # messages with full bodies fits.
        if route in ("/api/globus/whatsapp/ingest",
                     "/api/globus/teams/ingest"):
            auth = self.headers.get("Authorization", "")
            if not auth.startswith("Bearer "):
                return self._send_json(401, {"error": "missing bearer"})
            member = whatsapp_token_verify(auth[7:].strip())
            if not member:
                return self._send_json(
                    401, {"error": "invalid or expired token"})
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                return self._send_json(400, {"error": "bad length"})
            if length <= 0 or length > BRIDGE_INGEST_MAX_BYTES:
                return self._send_json(413, {
                    "error": f"body must be 1..{BRIDGE_INGEST_MAX_BYTES} bytes"})
            ctype = (self.headers.get("Content-Type") or "").lower()
            if "application/json" not in ctype:
                return self._send_json(415, {
                    "error": "content-type must be application/json"})
            try:
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
            except Exception as e:
                return self._send_json(400, {
                    "error": f"could not parse body: {type(e).__name__}"})
            if not isinstance(payload, dict):
                return self._send_json(400, {"error": "bad json"})
            messages = payload.get("messages") or []
            if not isinstance(messages, list):
                return self._send_json(422, {
                    "error": "messages must be a list"})
            if len(messages) > BRIDGE_INGEST_MAX_MESSAGES:
                return self._send_json(413, {
                    "error": f"max {BRIDGE_INGEST_MAX_MESSAGES} messages "
                             "per batch"})
            if route == "/api/globus/whatsapp/ingest":
                saved, total = whatsapp_ingest_messages(member, messages)
                print(f"[wa-ingest] member={member} got={total} "
                      f"saved={saved}", flush=True)
            else:
                saved, total = teams_ingest_messages(member, messages)
                print(f"[teams-ingest] member={member} got={total} "
                      f"saved={saved}", flush=True)
            return self._send_json(200, {"saved": saved, "total": total})

        # ---- Voice custom-LLM endpoint (voice-token auth, NOT cookie) ----
        # ElevenLabs hits this from its cloud, so we can't rely on a
        # session cookie. The voice_token is HMAC-signed and binds the
        # request to a member (6h TTL — issued at chat page render).
        if route == "/api/globus/voice-llm/chat/completions":
            body = self._json()
            voice_email = authenticate_voice_request(self.headers, body)
            if not voice_email:
                return self._send_json(401, {"error": "voice_token invalid "
                                              "or expired"})
            try:
                reply, usage, stream = voice_chat_handle(voice_email, body)
                data, ctype = voice_chat_format_response(
                    reply, usage, stream, model_name=cfg(
                        "VOICE_DEFAULT_MODEL", "claude-sonnet-4-6"))
                return self._send(200, data, ctype)
            except Exception as e:
                return self._send_json(500, {
                    "error": f"{type(e).__name__}: {e}"})

        # ---- Auth-gated POST routes (cookie) ----
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

        # ─────────────────────────────────────────────────────────────
        # Narada — credential management
        # ─────────────────────────────────────────────────────────────
        if route == "/members/narada/credentials/save":
            form = self._form()
            tool = (form.get("tool") or "").strip()
            if not tool:
                return self._redirect(
                    "/members/narada/credentials?kind=error&msg="
                    + quote("tool name required"))
            # Collect every non-empty field except 'tool' as the
            # credential dict — plugin determines which keys it needs.
            cred = {k: v for k, v in form.items()
                     if k != "tool" and (v or "").strip()}
            if not cred:
                return self._redirect(
                    "/members/narada/credentials?kind=error&msg="
                    + quote("no credential values supplied"))
            try:
                narada_creds.set_credential(email, tool, cred)
            except Exception as e:
                return self._redirect(
                    "/members/narada/credentials?kind=error&msg="
                    + quote(f"save failed: {type(e).__name__}"))
            return self._redirect(
                "/members/narada/credentials?kind=ok&msg="
                + quote(f"{tool} credentials saved."))

        if route == "/members/narada/credentials/delete":
            form = self._form()
            tool = (form.get("tool") or "").strip()
            if tool:
                narada_creds.delete_credential(email, tool)
            return self._redirect(
                "/members/narada/credentials?kind=ok&msg="
                + quote(f"{tool or 'credential'} deleted."))

        # ─────────────────────────────────────────────────────────────
        # Narada — campaign create + state-machine actions
        # ─────────────────────────────────────────────────────────────
        if route == "/members/narada/new":
            form = self._form()
            name = (form.get("name") or "").strip()
            if not name:
                return self._redirect(
                    "/members/narada/new?kind=error&msg="
                    + quote("name required"))
            send_from = (form.get("send_from") or "").strip().lower()
            if send_from and send_from not in \
                    narada_core.member_send_accounts(email):
                send_from = ""   # only allow the member's own accounts
            try:
                cid = narada_core.create_campaign(
                    email,
                    name=name,
                    product=(form.get("product") or "").strip(),
                    icp_description=(form.get("icp_description") or "").strip(),
                    lead_source=(form.get("lead_source") or "").strip(),
                    verifier=(form.get("verifier") or "").strip(),
                    sender=(form.get("sender") or "").strip(),
                    sender_config=({"from_addr": send_from}
                                   if send_from else None),
                    crm=(form.get("crm") or "").strip(),
                    send_mode=(form.get("send_mode") or "approve_each").strip())
            except ValueError as e:
                return self._redirect(
                    "/members/narada/new?kind=error&msg=" + quote(str(e)))
            except Exception as e:
                return self._redirect(
                    "/members/narada/new?kind=error&msg="
                    + quote(f"create failed: {type(e).__name__}"))
            return self._redirect(f"/members/narada/{cid}?kind=ok&msg="
                                    + quote("Campaign created."))

        # Campaign-detail POST actions — /members/narada/<id>/<action>
        if route.startswith("/members/narada/"):
            parts = route.removeprefix("/members/narada/").strip("/").split("/")
            if len(parts) == 2 and parts[0].isdigit():
                cid = int(parts[0])
                action = parts[1]
                camp = narada_core.get_campaign(email, cid)
                if not camp:
                    return self._send_json(404, {"error": "not found"})
                form = self._form()
                msg = self._narada_run_action(email, camp, action, form)
                return self._redirect(
                    f"/members/narada/{cid}?kind={msg.get('kind','ok')}&msg="
                    + quote(msg.get("msg", "done")))

        if route == "/members/globus/agents/run":
            form = self._form()
            name = (form.get("agent") or "").strip()
            if not find_agent(name):
                return self._redirect("/members/globus/agents")
            agent_run_async(name, email)
            return self._redirect("/members/globus/agents")

        if route == "/members/telegram/bot/add":
            form = self._form()
            token = (form.get("bot_token") or "").strip()
            chats_raw = (form.get("allowed_chat_ids") or "").strip()
            # Tokens look like `<int>:<35 chars of alnum + - + _>`. Reject
            # obviously-malformed before hitting Telegram.
            if not (":" in token and len(token) >= 30):
                return self._redirect(
                    "/members/telegram/bot?kind=error&msg="
                    + quote("Bot token doesn't look right — should be "
                            "<int>:<35+ chars> from @BotFather."))
            try:
                chat_ids = [int(x.strip()) for x in chats_raw.split(",")
                            if x.strip()]
            except ValueError:
                return self._redirect(
                    "/members/telegram/bot?kind=error&msg="
                    + quote("allowed_chat_ids must be comma-separated "
                            "integers (e.g. -1001234567890)."))
            if not chat_ids:
                return self._redirect(
                    "/members/telegram/bot?kind=error&msg="
                    + quote("Add at least one chat_id to the allow-list."))
            # Verify the token by hitting Telegram's getMe. Catches typos
            # + revoked tokens up-front so the audit log isn't full of
            # 401s the first time the LLM tries to send.
            try:
                req = _urlreq.Request(
                    f"https://api.telegram.org/bot{token}/getMe",
                    method="GET")
                with _urlreq.urlopen(req, timeout=15) as r:
                    info = json.loads(r.read().decode())
            except _urlerr.HTTPError as e:
                return self._redirect(
                    "/members/telegram/bot?kind=error&msg="
                    + quote(f"Telegram rejected the token: HTTP {e.code}. "
                            "Double-check it with @BotFather."))
            except Exception as e:
                return self._redirect(
                    "/members/telegram/bot?kind=error&msg="
                    + quote(f"Couldn't reach Telegram: "
                            f"{type(e).__name__}"))
            if not info.get("ok"):
                return self._redirect(
                    "/members/telegram/bot?kind=error&msg="
                    + quote(f"Telegram rejected the token: "
                            f"{info.get('description','unknown')}"))
            bot_username = (info.get("result") or {}).get("username", "?")
            try:
                from oauth_db import encrypt_token
                from db_helpers import db_write
                db_write(
                    "INSERT INTO globus_telegram_bots "
                    "(member_email, bot_username, bot_token_enc, "
                    " allowed_send_chats, allowed_actions, status) "
                    "VALUES (%s, %s, %s, %s, %s, 'active')",
                    (email, bot_username, encrypt_token(token),
                     json.dumps(chat_ids),
                     json.dumps(["reply", "broadcast"])))
            except Exception as e:
                return self._redirect(
                    "/members/telegram/bot?kind=error&msg="
                    + quote(f"Couldn't save: {type(e).__name__}"))
            return self._redirect(
                "/members/telegram/bot?kind=ok&msg="
                + quote(f"Added @{bot_username} with "
                        f"{len(chat_ids)} allowed chat_id(s)."))

        if route == "/members/telegram/bot/delete":
            form = self._form()
            try:
                bot_id = int(form.get("bot_id") or "0")
            except (TypeError, ValueError):
                bot_id = 0
            if bot_id:
                from db_helpers import db_write
                db_write(
                    "DELETE FROM globus_telegram_bots "
                    "WHERE id=%s AND member_email=%s",
                    (bot_id, email))
            return self._redirect(
                "/members/telegram/bot?kind=ok&msg="
                + quote("Bot deleted."))

        if route == "/members/connect/google/sync":
            form = self._form()
            try:
                conn_id = int((form.get("conn_id") or "0"))
            except (TypeError, ValueError):
                conn_id = 0
            conn = get_oauth_connection(email, conn_id) if conn_id else None
            if not conn:
                return self._redirect(
                    "/members/connect?kind=error&msg="
                    + quote("Unknown connection."))
            sync_connection_async(conn_id, email)
            return self._redirect(
                "/members/connect?kind=ok&msg="
                + quote(f"Sync started for {conn['provider_account']}."))

        if route == "/members/connect/google/disconnect":
            form = self._form()
            try:
                conn_id = int((form.get("conn_id") or "0"))
            except (TypeError, ValueError):
                conn_id = 0
            conn = get_oauth_connection(email, conn_id) if conn_id else None
            if not conn:
                return self._redirect(
                    "/members/connect?kind=error&msg="
                    + quote("Unknown connection."))
            try:
                refresh = decrypt_token(conn["refresh_token_enc"])
                google_revoke(refresh)
            except Exception:
                pass
            from db_helpers import db_write
            db_write(
                "DELETE FROM globus_vault_sources WHERE email=%s "
                "AND source_type IN ('google-drive','gmail') "
                "AND source_identifier=%s",
                (email, conn["provider_account"]))
            delete_oauth_connection(email, conn_id)
            return self._redirect(
                "/members/connect?kind=ok&msg="
                + quote(f"Disconnected {conn['provider_account']}."))

        if route == "/api/globus/client-error":
            return self._send(204, b"")

        return self._send_html(404, "<h1>404 — not found</h1>")


def main():
    print(f"globus/0.6 booting on {HOST}:{PORT}", flush=True)
    print(f"  site:     {SITE}", flush=True)
    print(f"  db:       {DB_CFG['user']}@{DB_CFG['host']}:{DB_CFG['port']}/"
          f"{DB_CFG['database']}", flush=True)
    print(f"  llm:      {cfg('GLOBUS_LLM_PROVIDER', 'claude-oauth')}",
          flush=True)
    # Start the background sync worker only if OAuth is wired (otherwise it
    # would loop forever doing nothing). Safe to enable later — bouncing the
    # service after setting GOOGLE_OAUTH_CLIENT_ID in config kicks it on.
    if cfg("GOOGLE_OAUTH_CLIENT_ID"):
        start_background_sync_worker()
        print("  bg-sync:  enabled (Google OAuth configured)", flush=True)
    else:
        print("  bg-sync:  disabled (set GOOGLE_OAUTH_CLIENT_ID + SECRET "
              "to enable Drive sync)", flush=True)
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nshutting down", flush=True)
        server.shutdown()


if __name__ == "__main__":
    main()
