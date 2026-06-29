"""Member-facing connector setup pages — extracted from lead_server.py
2026-06-28 as refactor slice #6q. Three sibling pages that walk a
member through connecting an external messaging source to their
Globus vault.

What's here:
  - whatsapp_setup_html(email, token): WhatsApp Web + MS Teams personal
    (teams.live.com) bridge via the Chrome extension. Caller mints the
    token (whatsapp_token_make lives in lead_server) and passes it in
    so this module stays pure-HTML.
  - teams_setup_html(email, message=None, message_kind=None): Microsoft
    Teams (Graph API) connect/disconnect + ingest status.
  - telegram_setup_html(email): Telethon-based personal Telegram
    mirror status + setup instructions.

Each one reads stats from MySQL itself via db_read (cheap query for
the panel header) — the page-builder owns its data fetch since the
queries are stable and only render here.

Module deps: db_read + cfg (db_helpers), esc (html_chrome),
_globus_shell (globus_chrome). No further injection needed.
"""
from __future__ import annotations
from db_helpers import db_read, cfg
from html_chrome import esc
from globus_chrome import _globus_shell


def whatsapp_setup_html(email, token):
    """Settings page for the Chrome extension pairing flow. Despite the
    function name (kept for the /members/whatsapp URL), the extension
    now covers BOTH WhatsApp Web and Microsoft Teams personal
    (teams.live.com) — one install, one token. Caller mints a fresh
    token via whatsapp_token_make(email) so an old, leaked one expires
    after WHATSAPP_TOKEN_TTL_SEC even without explicit revocation."""
    wa = db_read(
        "SELECT COUNT(*) AS total, COUNT(DISTINCT chat_name) AS chats, "
        "MAX(received_at) AS latest "
        "FROM globus_whatsapp_messages WHERE member_email=%s", (email,))
    ws = (wa or [{}])[0]
    wa_total = int(ws.get("total") or 0)
    wa_chats = int(ws.get("chats") or 0)
    wa_latest = ws.get("latest")
    wa_latest_str = (wa_latest.strftime("%Y-%m-%d %H:%M UTC")
                     if hasattr(wa_latest, "strftime") else "never")
    tm = db_read(
        "SELECT COUNT(*) AS total, COUNT(DISTINCT chat_name) AS chats, "
        "MAX(received_at) AS latest "
        "FROM globus_teams_messages WHERE member_email=%s", (email,))
    ts = (tm or [{}])[0]
    tm_total = int(ts.get("total") or 0)
    tm_chats = int(ts.get("chats") or 0)
    tm_latest = ts.get("latest")
    tm_latest_str = (tm_latest.strftime("%Y-%m-%d %H:%M UTC")
                     if hasattr(tm_latest, "strftime") else "never")
    body = (
        '<a class="back-link" href="/members/globus">&larr; Back to Globus</a>'
        '<span class="eyebrow">Globus &middot; Teams &amp; WhatsApp bridge</span>'
        '<h1>Teams &amp; WhatsApp bridge</h1>'
        '<p class="lead">One Chrome extension, two sources. Read-only DOM '
        'mirror of your WhatsApp Web and Microsoft Teams (personal, '
        'teams.live.com) conversations into the Globus vault. No automated '
        'sending.</p>'
        '<div class="panel">'
        '<h3 style="margin-top:0">Status</h3>'
        f'<p class="muted small" style="margin:.2rem 0"><strong>Member:</strong> {esc(email)}</p>'
        '<div style="display:grid;grid-template-columns:1fr 1fr;gap:1rem;margin-top:.7rem">'
        '<div>'
        '<p style="margin:0 0 .2rem;font-weight:600">WhatsApp</p>'
        f'<p class="muted small" style="margin:.15rem 0">Messages: {wa_total:,}</p>'
        f'<p class="muted small" style="margin:.15rem 0">Chats: {wa_chats:,}</p>'
        f'<p class="muted small" style="margin:.15rem 0">Latest: {esc(wa_latest_str)}</p>'
        '</div>'
        '<div>'
        '<p style="margin:0 0 .2rem;font-weight:600">Microsoft Teams</p>'
        f'<p class="muted small" style="margin:.15rem 0">Messages: {tm_total:,}</p>'
        f'<p class="muted small" style="margin:.15rem 0">Chats: {tm_chats:,}</p>'
        f'<p class="muted small" style="margin:.15rem 0">Latest: {esc(tm_latest_str)}</p>'
        '</div>'
        '</div>'
        '</div>'
        '<div class="panel">'
        '<h3 style="margin-top:0">1. Install the extension</h3>'
        '<p style="margin:.4rem 0 1rem">'
        '<a class="btn btn-primary" href="/globus-whatsapp-bridge.zip" '
        'download>Download extension (.zip)</a></p>'
        '<p class="muted small" style="margin:.4rem 0 1rem">'
        'Open source &middot; AGPL-3.0 &middot; '
        '<a href="https://github.com/Build-With-Sumit/whatsapp-bridge" '
        'target="_blank" rel="noopener">source on GitHub &rarr;</a></p>'
        '<ol style="line-height:1.7">'
        '<li>Unzip the download anywhere on your machine.</li>'
        '<li>Open <code>chrome://extensions</code> in Chrome / Brave / Edge.</li>'
        '<li>Toggle <strong>Developer mode</strong> (top-right).</li>'
        '<li>Click <strong>Load unpacked</strong>, pick the unzipped folder.</li>'
        '<li>Pin the extension to your toolbar.</li>'
        '</ol>'
        '<p class="muted small">If you already have v0.2.x installed, click '
        '<strong>Reload</strong> on the extension card after replacing the '
        'folder contents.</p>'
        '</div>'
        '<div class="panel">'
        '<h3 style="margin-top:0">2. Paste this token into the extension popup</h3>'
        '<p class="muted small">Valid for 90 days. Bound to your account; '
        'don\'t share it. Reload this page to mint a fresh one. '
        '<strong>One token covers both WhatsApp and Teams.</strong></p>'
        f'<textarea readonly style="width:100%;font-family:ui-monospace,Menlo,monospace;'
        f'font-size:.85rem;padding:.7rem;border:1px solid var(--border);'
        f'border-radius:6px;background:var(--surface-sunken);min-height:5.5rem" '
        f'onclick="this.select()">{esc(token)}</textarea>'
        '<p class="muted small" style="margin-top:.6rem">Click the text to '
        'select all, then copy into the popup\'s "Pairing token" field.</p>'
        '</div>'
        '<div class="panel">'
        '<h3 style="margin-top:0">3. Open WhatsApp Web and/or Teams</h3>'
        '<p><strong>WhatsApp:</strong> '
        '<a href="https://web.whatsapp.com" target="_blank">web.whatsapp.com</a> '
        '&mdash; click around your chats normally; the extension scoops '
        'messages as you scroll, and the active cycler visits unread chats '
        'at random 1-10 min intervals so capture isn\'t tab-foreground-dependent.</p>'
        '<p><strong>Microsoft Teams:</strong> '
        '<a href="https://teams.live.com/v2/" target="_blank">teams.live.com/v2/</a> '
        '&mdash; same pattern. Group chats are the target; 1:1 chats are also '
        'captured but tagged separately. The popup\'s "Microsoft Teams" '
        'counter should climb as you read.</p>'
        '<p class="muted small">Nothing is sent. No clicks except the cycler '
        'visiting your own unread chats &mdash; the same thing a human '
        'opening their inbox would do.</p>'
        '</div>'
    )
    return _globus_shell("Teams & WhatsApp bridge · Globus", body)


def teams_setup_html(email, message=None, message_kind=None):
    """Microsoft Teams setup page — connect / disconnect MS account
    and show ingest status. Mirrors whatsapp_setup_html structure."""
    conns = db_read(
        "SELECT id, provider_account, scopes, expires_at, sync_status, "
        "  last_synced_at, last_sync_error, needs_reconnect, created_at "
        "FROM globus_oauth_connections "
        "WHERE email=%s AND provider='microsoft' "
        "ORDER BY created_at DESC", (email,)) or []
    stats = db_read(
        "SELECT COUNT(*) total, COUNT(DISTINCT ms_chat_id) chats, "
        "  MAX(received_at) latest "
        "FROM globus_teams_messages WHERE member_email=%s", (email,))
    s = (stats or [{}])[0]
    total = int(s.get("total") or 0)
    chats = int(s.get("chats") or 0)
    latest = s.get("latest")
    latest_str = (latest.strftime("%Y-%m-%d %H:%M UTC")
                  if hasattr(latest, "strftime") else "never")

    msg_html = ""
    if message:
        cls = "note-err" if message_kind == "error" else "note-ok"
        msg_html = (f'<div class="panel" style="margin-bottom:1.4rem">'
                    f'<p class="form-note {cls}" style="margin:0">'
                    f'{esc(message)}</p></div>')

    oauth_ready = bool(cfg("MICROSOFT_OAUTH_CLIENT_ID")
                       and cfg("MICROSOFT_OAUTH_CLIENT_SECRET"))
    setup_warning = ""
    if not oauth_ready:
        setup_warning = (
            '<div class="panel" style="background:#FFF5EC;border-color:#F0CB95">'
            '<h3 style="margin:0 0 .3rem">Microsoft OAuth not yet configured</h3>'
            '<p class="muted small" style="margin:0">'
            'Set <code>MICROSOFT_OAUTH_CLIENT_ID</code> and '
            '<code>MICROSOFT_OAUTH_CLIENT_SECRET</code> in the MySQL '
            '<code>config</code> table before connecting.</p>'
            '</div>')

    if not conns:
        conn_list_html = (
            '<div class="panel">'
            '<p class="muted" style="margin:0">No Microsoft account connected '
            'yet. Connect below so Globus can read your Teams group chats.</p>'
            '</div>')
    else:
        rows = []
        for c in conns:
            acct = esc(c["provider_account"])
            status = c.get("sync_status", "idle")
            pill = {
                "idle":    '<span class="pill pill-done">Idle</span>',
                "running": '<span class="pill pill-v0">Running</span>',
                "error":   '<span class="pill pill-new">Error</span>',
            }.get(status, '<span class="pill pill-soon">'
                          + esc(status) + '</span>')
            if c.get("needs_reconnect"):
                pill = ('<span class="pill pill-new">Needs reconnect</span>')
            last = c.get("last_synced_at")
            last_str = (last.strftime("%Y-%m-%d %H:%M UTC")
                        if hasattr(last, "strftime") else "never")
            err = c.get("last_sync_error")
            err_html = ''
            if err and (status == "error" or c.get("needs_reconnect")):
                err_html = ('<p class="form-note note-err" '
                            'style="margin:.6rem 0 0">'
                            + esc(err)[:400] + '</p>')
            rows.append(
                f'<div class="panel" style="margin-bottom:.8rem">'
                f'<div style="display:flex;justify-content:space-between;'
                f'align-items:center;gap:.6rem">'
                f'<div><strong>{acct}</strong> {pill}</div>'
                f'<form method="POST" action="/members/connect/microsoft/disconnect" '
                f'style="margin:0">'
                f'<input type="hidden" name="id" value="{c["id"]}">'
                f'<button class="btn btn-sm" type="submit" '
                f'onclick="return confirm(\'Disconnect this Microsoft account?\')">'
                f'Disconnect</button>'
                f'</form>'
                f'</div>'
                f'<p class="muted small" style="margin:.4rem 0 0">'
                f'Last sync: {last_str}</p>'
                f'{err_html}'
                f'</div>')
        conn_list_html = "".join(rows)

    body = (
        '<a class="back-link" href="/members/globus">&larr; Back to Globus</a>'
        '<span class="eyebrow">Globus &middot; Microsoft Teams</span>'
        '<h1>Microsoft Teams</h1>'
        '<p class="lead">Reads your Teams group chats into the Globus '
        'vault. Personal Microsoft accounts only (work / school tenants '
        'work the same way but with broader Graph access).</p>'
        '<div class="panel">'
        '<h3 style="margin-top:0">Status</h3>'
        f'<p class="muted small" style="margin:.2rem 0">'
        f'<strong>Messages ingested:</strong> {total:,}</p>'
        f'<p class="muted small" style="margin:.2rem 0">'
        f'<strong>Chats seen:</strong> {chats:,}</p>'
        f'<p class="muted small" style="margin:.2rem 0">'
        f'<strong>Latest message:</strong> {esc(latest_str)}</p>'
        '</div>'
        f'{msg_html}'
        f'{setup_warning}'
        '<h3 class="category-head">Connected accounts</h3>'
        f'{conn_list_html}'
        '<hr class="divider">'
        '<div class="panel">'
        '<h3 style="margin-top:0">Connect a Microsoft account</h3>'
        '<p class="muted small">Read-only — Globus pulls your group-chat '
        'messages every 5 min. No messages are sent. We request scopes: '
        '<code>Chat.Read</code>, <code>Chat.ReadBasic</code>, '
        '<code>User.Read</code>, <code>offline_access</code>.</p>'
        '<p style="margin-top:1rem"><a class="btn btn-primary" '
        'href="/members/connect/microsoft/start">Connect Microsoft account</a></p>'
        '</div>')
    return _globus_shell("Microsoft Teams · Globus", body)


def telegram_setup_html(email):
    """Member-facing Telegram setup page. Sumit currently uses a
    Telethon daemon hooked into his personal TG account — that's the
    real ingest path. For other members, we need a multi-tenant
    Telethon flow (QR-code login or phone+code) which isn't built yet,
    so this page shows the status of any existing ingest + a clear
    'sign-up coming' explainer with two interim options."""
    try:
        stats = db_read(
            "SELECT COUNT(*) AS total, "
            "       COUNT(DISTINCT chat_name) AS chats, "
            "       MAX(received_at) AS latest "
            "FROM globus_telegram_messages WHERE member_email=%s",
            (email,)) or []
        s = stats[0] if stats else {}
        total = int(s.get("total") or 0)
        chats = int(s.get("chats") or 0)
        latest = s.get("latest")
        latest_str = (latest.strftime("%Y-%m-%d %H:%M UTC")
                      if hasattr(latest, "strftime") else "never")
    except Exception:
        total, chats, latest_str = 0, 0, "never"

    if total > 0:
        status_panel = (
            '<div class="panel">'
            '<h3 style="margin-top:0">Status</h3>'
            f'<p class="muted small" style="margin:.2rem 0">'
            f'<strong>Member:</strong> {esc(email)}</p>'
            f'<p class="muted small" style="margin:.2rem 0">'
            f'<strong>Messages in vault:</strong> {total:,}</p>'
            f'<p class="muted small" style="margin:.2rem 0">'
            f'<strong>Chats:</strong> {chats:,}</p>'
            f'<p class="muted small" style="margin:.2rem 0">'
            f'<strong>Latest:</strong> {esc(latest_str)}</p>'
            '</div>'
        )
    else:
        status_panel = (
            '<div class="panel">'
            '<h3 style="margin-top:0">Status</h3>'
            '<p class="muted small" style="margin:0">'
            'No Telegram messages ingested yet for your account.</p>'
            '</div>'
        )

    body = (
        '<a class="back-link" href="/members/connect">'
        '&larr; Back to data sources</a>'
        '<span class="eyebrow">Globus &middot; Telegram</span>'
        '<h1>Telegram</h1>'
        '<p class="lead">Read-only mirror of your personal Telegram '
        'account (all groups + DMs you have access to) into your '
        'private Globus vault. Globus can then answer questions like '
        '<em>"what did Mayur say about the NBT deal last week?"</em> '
        'without you scrolling through chats.</p>'
        + status_panel +
        '<div class="panel">'
        '<h3 style="margin-top:0">How it works</h3>'
        '<p>Telegram ingest uses <strong>Telethon</strong> — a '
        'lightweight client that logs into your personal TG account '
        '(same way the official desktop app does) and reads every chat '
        'you can see. Messages flow into <code>globus_telegram_messages</code>, '
        'per-member-private and per-row-scoped — no one else can see '
        'your messages.</p>'
        '<p class="muted small" style="margin-bottom:0">'
        'Telethon is the only practical way to read Telegram groups '
        '(the Bot API has privacy mode + can\'t see history). It uses '
        'your real account so it sees what you see.</p>'
        '</div>'
        '<div class="panel">'
        '<h3 style="margin-top:0">Connect your Telegram</h3>'
        '<p>Member-facing self-serve connect (QR-code login flow) is '
        'being built. It needs Telethon\'s phone+code or '
        'QR auth flow wired into the members UI + per-member session '
        'storage. Until that ships, two options:</p>'
        '<ol style="line-height:1.7">'
        '<li><strong>Email <a href="mailto:sumit@buildwithsumit.com">'
        'sumit@buildwithsumit.com</a></strong> with the subject '
        '<em>"Telegram ingest"</em> &mdash; we\'ll do the one-time '
        'auth handshake with you and your messages start flowing in '
        'minutes.</li>'
        '<li><strong>Self-host the daemon</strong> &mdash; the '
        '<a href="https://github.com/Build-With-Sumit/telegram-bridge" '
        'target="_blank" rel="noopener">tg_daemon.py</a> script in the '
        'public repo runs anywhere with Python and writes into your '
        'own DB. Useful if you don\'t want your TG session on our '
        'server.</li>'
        '</ol>'
        '</div>'
        '<div class="panel">'
        '<h3 style="margin-top:0">What gets ingested</h3>'
        '<ul style="line-height:1.7">'
        '<li>Every message in every chat your TG account can see &mdash; '
        'groups, channels, DMs, supergroups.</li>'
        '<li>Sender display name + username + chat name + timestamp + '
        'reply-to info + media flag (we don\'t store the media itself).</li>'
        '<li>Nothing outbound &mdash; this is purely read-only.</li>'
        '</ul>'
        '<p class="muted small" style="margin:0">Server-side, you can '
        'pause/resume your daemon any time. Disconnecting deletes the '
        'session token; we cannot reconnect without you running the '
        'auth flow again.</p>'
        '</div>'
    )
    return _globus_shell("Telegram · Globus", body)
