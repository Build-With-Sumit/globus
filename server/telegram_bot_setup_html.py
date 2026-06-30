"""Per-member Telegram bot setup page at /members/telegram/bot.

Member adds a bot by pasting:
  - the BotFather token (looks like `123456789:AAGw...`)
  - a comma-separated list of chat_ids the bot is allowed to post to

The server:
  - Validates the token by calling Telegram's `/bot{TOKEN}/getMe`
    (real network round-trip — if it 401s, the token's bad)
  - Fernet-encrypts the token (same key as Google OAuth refresh tokens)
  - Inserts/updates the bot row in `globus_telegram_bots`

The chat_id allow-list is the security boundary: a bot can ONLY post
to chat_ids the member explicitly listed here. Default-deny.

Public surface:
  - telegram_bot_setup_html(email, message=None, message_kind=None)
"""
from __future__ import annotations
import json

from db_helpers import db_read
from html_chrome import esc, _members_shell


def telegram_bot_setup_html(email, message=None, message_kind=None):
    """Render the bot setup page. Caller handles POST + redirects with
    `?msg=` / `?kind=` query params so this stays a pure render."""
    bots = db_read(
        "SELECT id, bot_username, allowed_send_chats, status, created_at "
        "FROM globus_telegram_bots WHERE member_email=%s "
        "ORDER BY id DESC", (email,)) or []

    sends = db_read(
        "SELECT target_chat_id, target_chat_name, status, "
        "       LEFT(error, 200) AS err, body_preview, created_at "
        "FROM globus_telegram_bot_sends "
        "WHERE member_email=%s "
        "ORDER BY id DESC LIMIT 10",
        (email,)) or []

    # Inline message banner
    msg_html = ""
    if message:
        cls = ("note-err" if message_kind == "error" else "note-ok")
        msg_html = (f'<div class="panel" style="margin-bottom:1.4rem">'
                    f'<p class="form-note {cls}" style="margin:0">'
                    f'{esc(message)}</p></div>')

    # --- Existing bots panel ---
    if bots:
        rows = []
        for b in bots:
            allowed = b.get("allowed_send_chats") or []
            if isinstance(allowed, (bytes, bytearray)):
                allowed = allowed.decode("utf-8", errors="replace")
            if isinstance(allowed, str):
                try:
                    allowed = json.loads(allowed)
                except Exception:
                    allowed = []
            allowed_str = ", ".join(str(x) for x in allowed) or "(none)"
            rows.append(
                '<div class="panel" style="margin-bottom:.8rem">'
                '<div style="display:flex;justify-content:space-between;'
                'align-items:flex-start;gap:1rem;flex-wrap:wrap">'
                '<div style="min-width:0;flex:1">'
                f'<h3 style="margin:0 0 .3rem">@{esc(b.get("bot_username") or "?")}'
                f'</h3>'
                f'<p class="muted small" style="margin:.15rem 0">'
                f'<strong>Status:</strong> {esc(b.get("status") or "?")} · '
                f'added {esc(str(b.get("created_at") or "")[:16])}</p>'
                f'<p class="muted small" style="margin:.15rem 0">'
                f'<strong>Allowed chat_ids:</strong> '
                f'<code style="font-size:.8rem">{esc(allowed_str)}</code></p>'
                '</div>'
                '<form method="POST" action="/members/telegram/bot/delete" '
                'style="margin:0">'
                f'<input type="hidden" name="bot_id" value="{int(b["id"])}">'
                '<button type="submit" class="btn">Delete</button>'
                '</form>'
                '</div></div>')
        bots_html = "".join(rows)
    else:
        bots_html = ('<p class="muted small">No Telegram bots configured. '
                     'Add one below to let Globus post on your behalf.</p>')

    # --- Add bot form ---
    add_form = (
        '<form method="POST" action="/members/telegram/bot/add">'
        '<label style="display:block;margin-bottom:.8rem">'
        '<span style="display:block;font-weight:600;margin-bottom:.2rem">'
        'Bot token</span>'
        '<input type="text" name="bot_token" required '
        'placeholder="123456789:AAGw...(token from @BotFather)" '
        'style="width:100%;font-family:ui-monospace,Menlo,monospace;'
        'font-size:.85rem;padding:.55rem;border:1px solid var(--line);'
        'border-radius:6px;background:var(--surface-sunken)">'
        '</label>'
        '<label style="display:block;margin-bottom:.8rem">'
        '<span style="display:block;font-weight:600;margin-bottom:.2rem">'
        'Allowed chat_ids '
        '<span class="muted small">(comma-separated)</span></span>'
        '<input type="text" name="allowed_chat_ids" required '
        'placeholder="-1001234567890, -1009876543210" '
        'style="width:100%;font-family:ui-monospace,Menlo,monospace;'
        'font-size:.85rem;padding:.55rem;border:1px solid var(--line);'
        'border-radius:6px;background:var(--surface-sunken)">'
        '</label>'
        '<p class="muted small" style="margin:.4rem 0">'
        'Tip: chat_ids start with <code>-100</code> for supergroups. '
        'Find them by sending one message in the chat, then visit '
        '<code>https://api.telegram.org/bot{TOKEN}/getUpdates</code>.'
        '</p>'
        '<button type="submit" class="btn btn-primary">Add bot</button>'
        '</form>')

    # --- Recent sends panel ---
    if sends:
        send_rows = "".join(
            '<tr>'
            f'<td class="muted small">{esc(str(s.get("created_at") or "")[:16])}</td>'
            f'<td>{esc(s.get("target_chat_name") or s.get("target_chat_id") or "?")}</td>'
            f'<td><span class="pill pill-'
            + ("done" if s.get("status") == "sent"
               else "soon" if s.get("status") == "denied"
               else "new") + '">'
            f'{esc(s.get("status") or "?")}</span></td>'
            f'<td class="muted small">{esc((s.get("body_preview") or "")[:80])}</td>'
            '</tr>'
            for s in sends)
        sends_html = (
            '<table class="table" style="width:100%;font-size:.88rem">'
            '<thead><tr><th>When</th><th>Chat</th>'
            '<th>Status</th><th>Body</th></tr></thead>'
            f'<tbody>{send_rows}</tbody></table>')
    else:
        sends_html = ('<p class="muted small">No send attempts yet. '
                      'Ask Globus in chat to post something.</p>')

    body = (
        '<a class="back-link" href="/members/connect">'
        '&larr; Back to data sources</a>'
        '<span class="eyebrow">Globus &middot; Telegram bot</span>'
        '<h1>Telegram bot (write path)</h1>'
        '<p class="lead">Let Globus post on your behalf via a Telegram '
        'bot you control. Default-deny: a bot can only send to chat_ids '
        'you explicitly allow. Every send is audited.</p>'
        + msg_html +
        '<div class="panel">'
        '<h3 style="margin-top:0">Your bots</h3>'
        + bots_html +
        '</div>'
        '<div class="panel">'
        '<h3 style="margin-top:0">Add a bot</h3>'
        '<ol style="line-height:1.7">'
        '<li>Open Telegram, talk to '
        '<a href="https://t.me/BotFather" target="_blank">@BotFather</a> '
        '&rarr; <code>/newbot</code>. Copy the token he gives you.</li>'
        '<li>Add the bot to whichever group / channel you want Globus '
        'to post in. (Give the bot admin permission only if needed — '
        'most public-post use cases don\'t need it.)</li>'
        '<li>Paste below.</li>'
        '</ol>'
        + add_form +
        '</div>'
        '<div class="panel">'
        '<h3 style="margin-top:0">Recent sends (last 10)</h3>'
        + sends_html +
        '</div>')
    return _members_shell("Telegram bot · Globus", body)
