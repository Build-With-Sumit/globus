"""Members-area "connect your data" page — extracted from lead_server.py
2026-06-28 as refactor slice #6r. ~290 lines of mostly inline-data-fetch
HTML. Renders the Google OAuth connection list (Drive / Gmail /
Analytics) + a tile grid for the other source types (WhatsApp / Teams /
Telegram) pointing at their dedicated setup pages.

Public surface:
  - members_connect_html(email, connections, cap, message=None,
    message_kind=None): caller pre-loads `connections` (list of
    globus_oauth_connections rows for this member) + passes the per-
    member cap (GLOBUS_MAX_CONNECTIONS_PER_MEMBER). The page itself
    runs a few small follow-up db_reads for Analytics content and
    per-tile message counts (kept inside this module since the queries
    are cheap and only render here).

Module deps: db_read + cfg (db_helpers), esc + fmt_dt + _fmt_size +
_members_shell (html_chrome). No further configure() needed.
"""
from __future__ import annotations
import re
from db_helpers import db_read, cfg
from html_chrome import esc, fmt_dt, _fmt_size, _members_shell


def members_connect_html(email, connections, cap, message=None, message_kind=None):
    """List + add/remove Google connections that feed Globus's vault."""
    used = len(connections)
    at_cap = used >= cap
    oauth_ready = bool(cfg("GOOGLE_OAUTH_CLIENT_ID")
                       and cfg("GOOGLE_OAUTH_CLIENT_SECRET"))

    msg_html = ""
    if message:
        cls = "note-err" if message_kind == "error" else "note-ok"
        msg_html = (f'<div class="panel" style="margin-bottom:1.4rem">'
                    f'<p class="form-note {cls}" style="margin:0">{esc(message)}</p>'
                    f'</div>')

    setup_warning = ""
    if not oauth_ready:
        setup_warning = (
            '<div class="panel" style="background:#FFF5EC;border-color:#F0CB95">'
            '<h3 style="margin:0 0 .3rem">Google OAuth not yet configured</h3>'
            '<p class="muted small" style="margin:0">'
            'Set <code>GOOGLE_OAUTH_CLIENT_ID</code> and '
            '<code>GOOGLE_OAUTH_CLIENT_SECRET</code> in the MySQL '
            '<code>config</code> table before adding connections.</p>'
            '</div>'
        )

    if not connections:
        conn_list_html = (
            '<div class="panel">'
            '<p class="muted" style="margin:0">No Google accounts connected yet. '
            'Add one below so Globus can read your Drive docs and starred Gmail.</p>'
            '</div>'
        )
    else:
        rows = []
        ga_by_acct = {r["source_identifier"]: r for r in (db_read(
            "SELECT source_identifier, char_count, updated_at, content "
            "FROM globus_vault_sources WHERE email=%s AND source_type='google-analytics'",
            (email,)) or [])}
        for c in connections:
            account = esc(c["provider_account"])
            picked = [s.strip() for s in (c.get("source_types") or "").split(",")
                      if s.strip()]
            last = c.get("last_synced_at")
            last_str = (fmt_dt(last) + " UTC") if last else "never"
            status = c.get("sync_status", "idle")
            pill = {
                "idle":     '<span class="pill pill-done">Idle</span>',
                "running":  '<span class="pill pill-v0">Running</span>',
                "error":    '<span class="pill pill-new">Error</span>',
                "disabled": '<span class="pill pill-soon">Disabled</span>',
            }.get(status, '<span class="pill pill-soon">' + esc(status) + '</span>')
            err = c.get("last_sync_error")
            err_html = ''
            if err and status == "error":
                err_html = ('<p class="form-note note-err" style="margin:.6rem 0 0">'
                            + esc(err)[:400] + '</p>')

            stat_chips = []
            if "drive" in picked:
                dc = int(c.get("drive_bytes") or 0)
                df = int(c.get("drive_files") or 0)
                if dc or df:
                    stat_chips.append(
                        f'<span class="stat-chip"><strong>Drive</strong> '
                        f'{df} files &middot; {_fmt_size(dc)}</span>')
                else:
                    stat_chips.append(
                        '<span class="stat-chip stat-empty"><strong>Drive</strong> '
                        + ('syncing&hellip;' if status == 'running' else 'no data yet')
                        + '</span>')
            if "gmail" in picked:
                gc = int(c.get("gmail_bytes") or 0)
                gf = int(c.get("gmail_files") or 0)
                if gc or gf:
                    stat_chips.append(
                        f'<span class="stat-chip"><strong>Gmail</strong> '
                        f'{gf} messages &middot; {_fmt_size(gc)}</span>')
                else:
                    stat_chips.append(
                        '<span class="stat-chip stat-empty"><strong>Gmail</strong> '
                        + ('syncing&hellip;' if status == 'running' else 'no data yet')
                        + '</span>')
            ga = ga_by_acct.get(c["provider_account"])
            ga_detail_html = ''
            if ("analytics" in picked) or ga:
                if ga and (ga.get("char_count") or 0) > 0:
                    gtext = ga.get("content") or ""
                    _m = re.search(r"\((\d+)\s+propert", gtext)
                    nprops = _m.group(1) if _m else str(gtext.count("\n## "))
                    stat_chips.append(
                        f'<span class="stat-chip"><strong>Analytics</strong> '
                        f'{nprops} properties &middot; {_fmt_size(ga.get("char_count") or 0)}</span>')
                    ga_detail_html = (
                        '<details style="margin-top:.5rem"><summary class="muted small" '
                        'style="cursor:pointer">&#128202; View Google Analytics data '
                        '&middot; synced ' + esc(str(ga.get("updated_at") or "")[:16])
                        + '</summary>'
                        '<pre style="white-space:pre-wrap;font-size:.78rem;background:#faf8f4;'
                        'border:1px solid #eee;border-radius:8px;padding:.7rem;max-height:340px;'
                        'overflow:auto;margin:.5rem 0 0">'
                        + esc(gtext[:9000]) + '</pre></details>')
                else:
                    stat_chips.append(
                        '<span class="stat-chip stat-empty"><strong>Analytics</strong> '
                        + ('syncing&hellip;' if status == 'running' else 'no data yet')
                        + '</span>')
            stats_html = ('<div class="stat-row">' + "".join(stat_chips) + '</div>'
                          if stat_chips else '')

            reconnect_html = ''
            if c.get("needs_reconnect"):
                reconnect_html = (
                    '<div class="stat-row"><span class="stat-chip" '
                    'style="background:#fdecea;color:#b00020;border:1px solid #f3b9b3;'
                    'font-weight:600">&#9888; Reconnect needed &mdash; Google access '
                    'expired or was revoked. Use &ldquo;Continue with Google&rdquo; above '
                    '(tick the same sources) to restore syncing.</span></div>')
            cid = c["id"]
            rows.append(
                '<div class="panel" style="margin-bottom:1rem">'
                '<div style="display:flex;justify-content:space-between;'
                'align-items:flex-start;gap:1rem;flex-wrap:wrap">'
                '<div style="min-width:0;flex:1">'
                f'<h3 style="margin:0 0 .3rem">{account}</h3>'
                '<p class="muted small" style="margin:0">'
                f'Last sync: {esc(last_str)} &middot; {pill}'
                '</p>'
                f'{stats_html}'
                f'{reconnect_html}'
                f'{err_html}'
                f'{ga_detail_html}'
                '</div>'
                '<div style="display:flex;gap:.5rem;flex-wrap:wrap">'
                '<form method="POST" action="/members/connect/google/sync" style="margin:0">'
                f'<input type="hidden" name="conn_id" value="{cid}">'
                '<button class="btn" type="submit">Sync now</button>'
                '</form>'
                '<form method="POST" action="/members/connect/google/disconnect" '
                f'onsubmit="return confirm(\'Disconnect {account}? '
                'Cached vault data from this account will be deleted.\');" '
                'style="margin:0">'
                f'<input type="hidden" name="conn_id" value="{cid}">'
                '<button class="btn" type="submit" '
                'style="color:#9F361D;border-color:#E5BFB4">Disconnect</button>'
                '</form>'
                '</div>'
                '</div>'
                '</div>'
            )
        conn_list_html = "".join(rows)

    if at_cap:
        add_form = (
            '<div class="panel">'
            '<h3>Add another Google account</h3>'
            '<p class="muted" style="margin:0">'
            f'You\'ve reached the maximum of {cap} connected Google accounts. '
            'Disconnect one above to add another.</p>'
            '</div>'
        )
    else:
        add_attrs = '' if oauth_ready else 'disabled'
        add_form = (
            '<div class="panel">'
            '<h3>Connect a Google account</h3>'
            '<p class="muted small" style="margin:0 0 .9rem">'
            'You choose which sources to share. Everything is read-only and '
            'per-member-private. Refresh tokens are encrypted at rest.</p>'
            '<form method="GET" action="/members/connect/google/start">'
            '<div style="margin-bottom:.9rem;display:flex;flex-direction:column;gap:.4rem">'
            '<label><input type="checkbox" name="drive" value="1" checked> '
            '<strong>Google Drive</strong> '
            '<span class="muted small">— recent Google Docs + .md / .txt files</span>'
            '</label>'
            '<label><input type="checkbox" name="gmail" value="1"> '
            '<strong>Gmail</strong> '
            '<span class="muted small">— all emails from the last 90 days (excluding spam &amp; trash)</span>'
            '</label>'
            '<label><input type="checkbox" name="analytics" value="1"> '
            '<strong>Google Analytics</strong> '
            '<span class="muted small">— traffic, users &amp; conversions per property (30d &amp; 90d)</span>'
            '</label>'
            '</div>'
            f'<button class="btn btn-primary" type="submit" {add_attrs}>'
            'Continue with Google</button>'
            '</form>'
            '</div>'
        )

    try:
        wa_cnt = (db_read(
            "SELECT COUNT(*) AS n FROM globus_whatsapp_messages "
            "WHERE member_email=%s", (email,)) or [{"n": 0}])[0]["n"]
    except Exception: wa_cnt = 0
    try:
        teams_cnt = (db_read(
            "SELECT COUNT(*) AS n FROM globus_teams_messages "
            "WHERE member_email=%s", (email,)) or [{"n": 0}])[0]["n"]
    except Exception: teams_cnt = 0
    try:
        tg_cnt = (db_read(
            "SELECT COUNT(*) AS n FROM globus_telegram_messages "
            "WHERE member_email=%s", (email,)) or [{"n": 0}])[0]["n"]
    except Exception: tg_cnt = 0
    try:
        ms_cnt = (db_read(
            "SELECT COUNT(*) AS n FROM globus_oauth_connections "
            "WHERE email=%s AND provider='microsoft' "
            "AND needs_reconnect=0", (email,)) or [{"n": 0}])[0]["n"]
    except Exception: ms_cnt = 0

    def _src_tile(href, icon, name, status, desc):
        if status == "connected":
            pill = ('<span class="pill pill-done" style="font-size:.7rem">'
                    'Connected</span>')
        elif status == "soon":
            pill = ('<span class="pill pill-soon" style="font-size:.7rem">'
                    'Soon</span>')
        else:
            pill = ('<span class="pill pill-new" style="font-size:.7rem">'
                    'Not connected</span>')
        return (
            f'<a class="tool-card" href="{href}">'
            f'<div class="tc-head">'
            f'<div class="tc-title"><span class="tc-icon">{icon}</span>'
            f'{name}</div>{pill}</div>'
            f'<p class="tc-desc">{desc}</p>'
            f'<div class="tc-foot">Set up &rarr;</div>'
            f'</a>')

    other_sources_html = (
        '<div class="tool-grid" style="display:grid;grid-template-columns:'
        'repeat(auto-fit,minmax(280px,1fr));gap:1rem;margin-top:.8rem">'
        + _src_tile(
            "/members/whatsapp", "💬", "WhatsApp",
            "connected" if wa_cnt > 0 else "not connected",
            f"Chrome extension that mirrors WhatsApp Web "
            f"conversations into your vault. "
            f"{wa_cnt:,} messages ingested so far." if wa_cnt > 0 else
            "Chrome extension that mirrors WhatsApp Web "
            "conversations into your vault.")
        + _src_tile(
            "/members/teams", "👥", "Microsoft Teams",
            "connected" if (teams_cnt > 0 or ms_cnt > 0) else "not connected",
            f"Same Chrome extension as WhatsApp — also reads "
            f"teams.live.com group chats. "
            f"{teams_cnt:,} messages ingested." if teams_cnt > 0 else
            "Chrome extension reads teams.live.com group chats. "
            "Same install as the WhatsApp bridge.")
        + _src_tile(
            "/members/telegram", "✈️", "Telegram",
            "connected" if tg_cnt > 0 else "soon",
            f"{tg_cnt:,} messages already in your vault." if tg_cnt > 0 else
            "Server-side Telethon poller mirrors your personal "
            "Telegram account (all groups + DMs). Sign-up flow coming.")
        + '</div>'
    )

    body = (
        '<p style="margin-bottom:1rem">'
        '<a href="/members" class="muted small">&larr; Members area</a></p>'
        '<span class="eyebrow">Connect your data</span>'
        '<h1>Data sources for Globus</h1>'
        '<p class="lead">Plug in everything Globus should see — Google '
        '(Drive + Gmail), WhatsApp, Microsoft Teams, Telegram. All '
        'read-only, all per-member-private, refresh tokens encrypted '
        'at rest.</p>'
        '<p style="margin-top:-.6rem"><a href="/members/vault-progress">'
        '&rarr; Live vault build progress</a></p>'
        f'{msg_html}'
        f'{setup_warning}'
        '<h3 class="category-head" style="margin-top:2rem">'
        f'Google accounts <span class="muted" style="font-weight:400">'
        f'· {used} of {cap}</span></h3>'
        '<p class="muted small" style="margin-top:-.4rem">'
        'Google Drive, Gmail, Analytics. The bulk of your vault.</p>'
        f'{conn_list_html}'
        f'{add_form}'
        '<h3 class="category-head" style="margin-top:2.5rem">'
        'Other sources</h3>'
        '<p class="muted small" style="margin-top:-.4rem">'
        'Chat platforms + chat-history mirrors. Each has its own '
        'setup flow.</p>'
        f'{other_sources_html}'
        '<hr class="divider">'
        '<p class="muted small">'
        'Disconnecting a Google account revokes our access at Google and '
        'removes its cached content from your vault. Background sync runs '
        'every 10 minutes for connections older than 6 hours. '
        '<a href="/members/globus">&rarr; Open Globus</a> &middot; '
        '<a href="/members/vault-progress">&rarr; Vault build progress</a></p>'
    )
    return _members_shell("Connect data sources · Members", body)
