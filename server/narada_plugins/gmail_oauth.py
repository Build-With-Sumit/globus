"""Gmail sender plugin via the member's EXISTING Google OAuth.

Unlike gmail_composio (which routes through the Composio substrate), this
plugin sends directly through the Gmail API using the OAuth refresh tokens
already stored in `globus_oauth_connections` — the same connections the
member set up at /members/connect for Drive/Gmail sync. No Composio, no
extra SaaS bill, no separate connect flow: if the member has a Google
account connected with the `gmail.send` scope, Narada can send from it.

Sends AS the campaign's `from_addr` when that address is a connected
account; otherwise falls back to the member's default gmail.send account.
Replies are pulled straight from the Gmail API and matched to sends by
Message-ID. Volume cap ~1500/day (Workspace) as a reputation-safe margin.

Slug `gmail_oauth`. Auth = OAUTH_CUSTOM (no credential form — reuses the
existing connection).
"""
from __future__ import annotations
import base64
import json
from datetime import datetime
from email.mime.text import MIMEText
from email.utils import make_msgid, formatdate
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from urllib.error import HTTPError

from db_helpers import db_read, cfg
from oauth_db import decrypt_token
from narada_plugins import register
from narada_plugins.types import (
    AuthMethod, PluginCategory, PluginInfo, SendResult, SendStatus, Reply,
)


GMAIL_DEFAULT_DAILY_CAP = 1500
GMAIL_API = "https://gmail.googleapis.com/gmail/v1/users/me"


def _google_connection(from_addr: str, member_email: str) -> dict | None:
    """Find the OAuth connection to send from. Prefer the exact sending
    address; else the member's default account with the gmail.send scope."""
    rows = db_read(
        "SELECT refresh_token_enc, provider_account FROM "
        "globus_oauth_connections WHERE provider_account=%s AND "
        "provider='google' AND scopes LIKE '%%gmail.send%%' "
        "ORDER BY updated_at DESC LIMIT 1", (from_addr,)) or []
    if not rows:
        rows = db_read(
            "SELECT refresh_token_enc, provider_account FROM "
            "globus_oauth_connections WHERE email=%s AND provider='google' "
            "AND scopes LIKE '%%gmail.send%%' ORDER BY updated_at DESC "
            "LIMIT 1", (member_email,)) or []
    return rows[0] if rows else None


def _access_token(refresh_token_enc) -> str:
    """Exchange the stored (encrypted) refresh token for a fresh access
    token. Raises on failure — callers wrap in try/except."""
    rt = decrypt_token(refresh_token_enc)
    data = urlencode({
        "client_id": cfg("GOOGLE_OAUTH_CLIENT_ID", ""),
        "client_secret": cfg("GOOGLE_OAUTH_CLIENT_SECRET", ""),
        "refresh_token": rt, "grant_type": "refresh_token"}).encode()
    req = Request("https://oauth2.googleapis.com/token", data=data,
                  headers={"Content-Type": "application/x-www-form-urlencoded"})
    with urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode())["access_token"]


def _member_send_accounts(member_email: str) -> list[dict]:
    return db_read(
        "SELECT refresh_token_enc, provider_account FROM "
        "globus_oauth_connections WHERE email=%s AND provider='google' "
        "AND scopes LIKE '%%gmail.send%%' ORDER BY updated_at DESC",
        (member_email,)) or []


class GmailOAuthSender:
    """Sender plugin: sends through the member's own Gmail via their
    existing Google OAuth connection (globus_oauth_connections)."""

    @classmethod
    def info(cls) -> PluginInfo:
        return PluginInfo(
            name="gmail_oauth",
            display_name="Gmail (your connected account)",
            category=PluginCategory.SENDER,
            auth_method=AuthMethod.OAUTH_CUSTOM,
            homepage="https://workspace.google.com",
            docs_url="https://developers.google.com/gmail/api",
            description=(
                "Send from the Gmail / Workspace account you already "
                "connected at /members/connect — no Composio, no extra "
                "setup. Native reply tracking, ~1500/day cap. Best for "
                "personalised cold outreach where domain reputation "
                "matters. Requires the gmail.send scope on your Google "
                "connection (reconnect at /members/connect to grant it)."
            ),
            free_tier=True,
        )

    def is_available(self, member_email: str) -> bool:
        return bool(db_read(
            "SELECT 1 FROM globus_oauth_connections WHERE email=%s AND "
            "provider='google' AND scopes LIKE '%%gmail.send%%' LIMIT 1",
            (member_email,)))

    def daily_send_cap(self, member_email: str) -> int:
        return GMAIL_DEFAULT_DAILY_CAP

    def supports_warmup(self) -> bool:
        return False

    def send(self, member_email: str, from_addr: str, to: str,
             subject: str, body: str, headers: dict | None = None,
             reply_to: str | None = None) -> SendResult:
        conn = _google_connection(from_addr or member_email, member_email)
        if not conn:
            return SendResult(
                status=SendStatus.FAILED,
                error="no connected Google account has the gmail.send scope "
                      "— reconnect at /members/connect")
        sender = conn.get("provider_account") or from_addr
        try:
            token = _access_token(conn["refresh_token_enc"])
        except Exception as e:
            return SendResult(status=SendStatus.FAILED,
                              error=f"token refresh failed: "
                                    f"{type(e).__name__}: {e}")
        domain = (sender.split("@")[-1] if sender and "@" in sender
                  else "globussoft.com")
        msg = MIMEText(body, _charset="utf-8")
        msg["From"] = sender
        msg["To"] = to
        msg["Subject"] = subject
        msg["Message-ID"] = make_msgid(domain=domain)
        msg["Date"] = formatdate(localtime=False)
        if reply_to:
            msg["Reply-To"] = reply_to
        hdrs = dict(headers or {})
        hdrs.setdefault("List-Unsubscribe",
                        f"<mailto:unsubscribe@{domain}>")
        for k, v in hdrs.items():
            if k.lower() in ("from", "to", "subject", "message-id", "date"):
                continue
            msg[k] = v
        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("ascii")
        req = Request(
            f"{GMAIL_API}/messages/send",
            data=json.dumps({"raw": raw}).encode(),
            headers={"Authorization": f"Bearer {token}",
                     "Content-Type": "application/json"})
        try:
            with urlopen(req, timeout=30) as r:
                resp = json.loads(r.read().decode())
        except HTTPError as e:
            body_txt = ""
            try:
                body_txt = e.read().decode("utf-8", "replace")[:300]
            except Exception:
                pass
            return SendResult(status=SendStatus.FAILED,
                              error=f"HTTP {e.code}: {body_txt}")
        except Exception as e:
            return SendResult(status=SendStatus.FAILED,
                              error=f"{type(e).__name__}: {e}")
        return SendResult(
            status=SendStatus.SENT,
            message_id=msg["Message-ID"] or "",
            thread_id=str(resp.get("threadId") or ""),
            external_id=str(resp.get("id") or ""),
            raw=resp if isinstance(resp, dict) else {})

    def detect_replies(self, member_email: str,
                       since: datetime) -> list[Reply]:
        """Pull replies across ALL the member's connected Gmail accounts
        (replies land on whichever account sent), match to sends by
        In-Reply-To Message-ID downstream."""
        when = since.strftime("%Y/%m/%d")
        q = f"after:{when} -in:sent -in:drafts -in:spam -in:trash"
        out: list[Reply] = []
        for acct in _member_send_accounts(member_email):
            try:
                token = _access_token(acct["refresh_token_enc"])
            except Exception:
                continue
            hdr = {"Authorization": f"Bearer {token}"}
            try:
                url = (f"{GMAIL_API}/messages?"
                       + urlencode({"q": q, "maxResults": 50}))
                with urlopen(Request(url, headers=hdr), timeout=30) as r:
                    ids = [m["id"] for m in
                           (json.loads(r.read().decode()).get("messages")
                            or [])]
            except Exception:
                continue
            for mid in ids[:50]:
                try:
                    murl = (f"{GMAIL_API}/messages/{mid}?"
                            + urlencode({"format": "full"}))
                    with urlopen(Request(murl, headers=hdr), timeout=30) as r:
                        full = json.loads(r.read().decode())
                except Exception:
                    continue
                payload = full.get("payload") or {}
                h = {x.get("name", "").lower(): x.get("value", "")
                     for x in (payload.get("headers") or [])}
                in_reply_to = h.get("in-reply-to") or ""
                if not in_reply_to:
                    continue   # only care about actual replies to our sends
                body = ""
                for part in (payload.get("parts") or [payload]):
                    if part.get("mimeType") == "text/plain":
                        d = (part.get("body") or {}).get("data") or ""
                        try:
                            body = base64.urlsafe_b64decode(
                                d + "==").decode("utf-8", "replace")
                        except Exception:
                            pass
                        if body:
                            break
                out.append(Reply(
                    in_reply_to_message_id=in_reply_to,
                    from_addr=h.get("from") or "",
                    subject=h.get("subject") or "",
                    body=body[:5000],
                    received_at=datetime.utcnow().isoformat() + "Z",
                    thread_id=str(full.get("threadId") or ""),
                    raw={}))
        return out


# Auto-register
try:
    register(GmailOAuthSender())
except Exception as _e:
    print(f"[narada/gmail_oauth] register failed: "
          f"{type(_e).__name__}: {_e}", flush=True)
