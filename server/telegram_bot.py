"""Telegram Bot API send path — used by the `send_telegram_via_bot`
LLM tool when the member asks Globus to actually post a reply.

The model is **default-deny**: the bot row in `globus_telegram_bots`
must list the target chat_id in its `allowed_send_chats` JSON column,
or the send is rejected up-front (audited as 'denied' in
`globus_telegram_bot_sends`). Per-member ownership is enforced — a
bot is only ever findable via its member_email scope.

Setup is via SQL (no UI in v1.0). Example — add Sumit's @SumitGlobusBot
with permission to reply in two specific chats:

    INSERT INTO globus_telegram_bots
      (member_email, bot_username, bot_token_enc, allowed_send_chats,
       allowed_actions, status)
    VALUES
      ('you@example.com', 'YourGlobusBot',
       <Fernet-encrypted bot token bytes>,
       JSON_ARRAY(-1001234567890, -1009876543210),
       JSON_ARRAY('reply', 'broadcast'),
       'active');

The token MUST be Fernet-encrypted using the same key as the OAuth
refresh tokens (`GLOBUS_OAUTH_ENCRYPTION_KEY`). Use `oauth_db.encrypt_token`
from a Python shell to do the encryption — never store the raw bot
token in the DB.
"""
from __future__ import annotations
import json
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from db_helpers import db_read, db_write
from oauth_db import decrypt_token


TELEGRAM_API = "https://api.telegram.org"
SEND_TIMEOUT_SEC = 20


# ─────────────────────────────────────────────────────────────────────
# Audit helper — INSERT one row into globus_telegram_bot_sends
# ─────────────────────────────────────────────────────────────────────

def _audit(email, bot_id, initiator, chat_id, status,
           body_preview="", target_chat_name=None,
           tg_message_id=None, error=None):
    """Insert one audit row. Never raises — audit failure shouldn't
    leak back to the caller (worst case: a silent send)."""
    try:
        db_write(
            "INSERT INTO globus_telegram_bot_sends "
            "(member_email, bot_id, initiator, target_chat_id, "
            " target_chat_name, tg_message_id, status, error, body_preview) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (email, bot_id, initiator, str(chat_id),
             (target_chat_name or "")[:255],
             tg_message_id, status,
             (error or "")[:1000] if error else None,
             (body_preview or "")[:512]))
    except Exception as e:
        print(f"[telegram-bot-audit] write failed: "
              f"{type(e).__name__}: {e}", flush=True)


# ─────────────────────────────────────────────────────────────────────
# Public send — used by the orchestrator's run_agent tool branch
# ─────────────────────────────────────────────────────────────────────

def send_via_member_bot(email, chat_id, text,
                         reply_to_message_id=None,
                         parse_mode=None,
                         initiator="globus-chat"):
    """Send `text` to `chat_id` via the member's first active TG bot.

    Per-member-scoped: looks up the bot via `member_email=email`. The
    target chat_id must appear in the bot's `allowed_send_chats`
    array, else the send is denied + audited.

    Returns dict:
      {"ok": True,  "tg_message_id": int, "target_chat_name": str}
      {"ok": False, "error": str}

    Every attempt (sent, denied, failed) leaves a row in
    `globus_telegram_bot_sends` keyed by member."""
    if not email or chat_id in (None, "", 0) or not text:
        return {"ok": False, "error": "missing email/chat_id/text"}

    rows = db_read(
        "SELECT id, bot_token_enc, allowed_send_chats, bot_username "
        "FROM globus_telegram_bots "
        "WHERE member_email=%s AND status='active' "
        "ORDER BY id LIMIT 1", (email,)) or []
    if not rows:
        return {"ok": False,
                "error": "no active Telegram bot configured for you — "
                         "add one via SQL (see telegram_bot.py header "
                         "for the schema). Setup UI is planned for a "
                         "later release."}
    bot = rows[0]

    # Allow-list check — default deny.
    allowed = bot.get("allowed_send_chats") or []
    if isinstance(allowed, (bytes, bytearray)):
        allowed = allowed.decode("utf-8", errors="replace")
    if isinstance(allowed, str):
        try:
            allowed = json.loads(allowed)
        except Exception:
            allowed = []
    allowed_str = {str(x) for x in (allowed or [])}
    if str(chat_id) not in allowed_str:
        bot_handle = bot.get("bot_username") or f"bot#{bot['id']}"
        msg = (f"chat_id {chat_id} is not in @{bot_handle}'s "
               f"allowed_send_chats — default-deny. Add it to the bot's "
               f"allowed_send_chats array to permit.")
        _audit(email, bot["id"], initiator, chat_id, "denied",
               body_preview=text, error=msg)
        return {"ok": False, "error": msg}

    # Decrypt the bot token + POST sendMessage.
    try:
        token = decrypt_token(bot["bot_token_enc"])
    except Exception as e:
        err = f"bot token decrypt failed: {type(e).__name__}: {e}"
        _audit(email, bot["id"], initiator, chat_id, "failed",
               body_preview=text, error=err)
        return {"ok": False, "error": err}

    payload = {"chat_id": chat_id, "text": text}
    if reply_to_message_id:
        payload["reply_to_message_id"] = reply_to_message_id
    if parse_mode:
        payload["parse_mode"] = parse_mode

    req = Request(
        f"{TELEGRAM_API}/bot{token}/sendMessage",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST")
    try:
        with urlopen(req, timeout=SEND_TIMEOUT_SEC) as r:
            resp = json.loads(r.read().decode())
    except HTTPError as e:
        try:
            body = e.read().decode("utf-8", "replace")[:300]
        except Exception:
            body = ""
        err = f"HTTP {e.code}: {body}"
        _audit(email, bot["id"], initiator, chat_id, "failed",
               body_preview=text, error=err)
        return {"ok": False, "error": f"HTTP {e.code}: {body[:200]}"}
    except Exception as e:
        err = f"{type(e).__name__}: {e}"
        _audit(email, bot["id"], initiator, chat_id, "failed",
               body_preview=text, error=err)
        return {"ok": False, "error": err}

    if not resp.get("ok"):
        err = resp.get("description") or "unknown error"
        _audit(email, bot["id"], initiator, chat_id, "failed",
               body_preview=text, error=err)
        return {"ok": False, "error": err}

    tg_msg = resp.get("result") or {}
    tg_msg_id = tg_msg.get("message_id")
    chat = tg_msg.get("chat") or {}
    target_name = (chat.get("title") or chat.get("username")
                   or str(chat_id))
    _audit(email, bot["id"], initiator, chat_id, "sent",
           body_preview=text, target_chat_name=target_name,
           tg_message_id=tg_msg_id)
    return {"ok": True, "tg_message_id": tg_msg_id,
            "target_chat_name": target_name}
