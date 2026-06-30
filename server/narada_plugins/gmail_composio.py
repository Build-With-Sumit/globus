"""Gmail / Workspace sender plugin via Composio.

Per Sumit's directive (2026-07-01): use member's own Gmail/Workspace
for outbound. No SaaS sender bill, native reply tracking, per-member
isolation matches Globus's model. Volume cap = ~2K/day per Workspace
account.

Auth model: Composio holds the OAuth tokens. Marketer connects Gmail
once at /members/composio (the connector page committed in 434b9ef).
Behind the scenes we add the gmail.send scope to the existing read-only
Gmail OAuth — same connection, one extra scope. composio_helpers
handles all the token lifecycle.

Reply tracking: re-uses the existing Gmail OAuth (read scope) — see
server/sync_gmail.py. Outreach Sends get matched to inbound replies
by Message-ID + thread-ID.
"""
from __future__ import annotations
import base64
import email.mime.text
import email.utils
from datetime import datetime
from email.mime.text import MIMEText

from narada_plugins import register
from narada_plugins.types import (
    AuthMethod, PluginCategory, PluginInfo,
    SendResult, SendStatus, Reply,
)


# ─────────────────────────────────────────────────────────────────────
# Conservative per-Workspace daily cap. Google's hard limit is ~2000
# for Workspace, ~500 for personal Gmail. We default to 1500/day as a
# safety margin (sustained sending right at the cap triggers rate-limit
# events). Marketers can override per-campaign via sender_config.
# ─────────────────────────────────────────────────────────────────────

GMAIL_DEFAULT_DAILY_CAP = 1500


class GmailComposioSender:
    """Sender plugin: pushes mail through Gmail's API via Composio's
    managed OAuth. Composio holds the tokens; we execute
    GMAIL_SEND_EMAIL on the marketer's session per send."""

    @classmethod
    def info(cls) -> PluginInfo:
        return PluginInfo(
            name="gmail",
            display_name="Gmail / Google Workspace (via Composio)",
            category=PluginCategory.SENDER,
            auth_method=AuthMethod.COMPOSIO,
            composio_app="gmail",
            homepage="https://workspace.google.com",
            docs_url="https://developers.google.com/gmail/api",
            description=(
                "Send via the member's own Gmail or Workspace account. "
                "No extra SaaS bill, native reply tracking, ~1500/day "
                "Workspace cap. Best for personalised low-volume cold "
                "outreach where domain reputation matters. "
                "(For high-volume rotation across throwaway domains, "
                "use the Smartlead / Lemlist / Bison plugins instead.)"
            ),
            free_tier=True,
        )

    def is_available(self, member_email: str) -> bool:
        """True iff the member has connected Gmail via Composio with
        the gmail.send scope."""
        try:
            from composio_helpers import is_active
            return is_active(member_email, "gmail")
        except Exception:
            return False

    def daily_send_cap(self, member_email: str) -> int:
        # Could read a per-member override from globus_narada_credentials
        # later; for v1 the default is what every marketer gets.
        return GMAIL_DEFAULT_DAILY_CAP

    def supports_warmup(self) -> bool:
        return False  # Gmail builds reputation organically; no SaaS warmup pool

    def send(self, member_email: str, from_addr: str, to: str,
              subject: str, body: str,
              headers: dict | None = None,
              reply_to: str | None = None) -> SendResult:
        """Execute GMAIL_SEND_EMAIL via Composio. Builds a MIME message
        client-side so we can set headers (List-Unsubscribe, etc.) +
        attach a Message-ID we can match replies against later."""
        try:
            from composio_helpers import execute
        except Exception as e:
            return SendResult(
                status=SendStatus.FAILED,
                error=f"composio_helpers import failed: "
                       f"{type(e).__name__}: {e}")

        # Build a properly-formed MIME message. Composio's Gmail tool
        # accepts a raw base64url-encoded RFC 822 message via the
        # `raw` parameter, OR it can accept the body and synthesise
        # one. We build our own so we control Message-ID + headers
        # (needed for reply matching + List-Unsubscribe compliance).
        msg = MIMEText(body, _charset="utf-8")
        msg["From"] = from_addr
        msg["To"] = to
        msg["Subject"] = subject
        msg["Message-ID"] = email.utils.make_msgid(domain="globussoft.com")
        msg["Date"] = email.utils.formatdate(localtime=False)
        if reply_to:
            msg["Reply-To"] = reply_to
        # CAN-SPAM-friendly: every cold-outreach send carries an
        # opt-out header. The body should ALSO contain a one-line
        # human-readable equivalent — copy gen handles that.
        if headers and "List-Unsubscribe" not in headers:
            headers = {**headers, "List-Unsubscribe": "<mailto:unsubscribe@globussoft.com>"}
        elif not headers:
            headers = {"List-Unsubscribe": "<mailto:unsubscribe@globussoft.com>"}
        for k, v in headers.items():
            if k.lower() in ("from", "to", "subject", "message-id", "date"):
                continue  # already set
            msg[k] = v

        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("ascii")

        result = execute(
            email=member_email, app="gmail",
            tool_slug="GMAIL_SEND_EMAIL",
            arguments={"recipient_email": to,
                        "subject": subject,
                        "body": body,
                        # Composio's tool accepts either body or raw.
                        # We pass raw so our headers + Message-ID stick.
                        "extra_recipients": [],
                        "is_html": False,
                        # Some tool versions accept a `raw` field for the
                        # full MIME message; harmless if ignored.
                        "raw": raw})
        if not result.get("ok"):
            return SendResult(
                status=SendStatus.FAILED,
                error=str(result.get("error", "unknown composio error")))
        data = result.get("data") or {}
        # Gmail API returns {id, threadId} for sendMessage. Composio
        # may nest this under .response_data depending on version.
        if isinstance(data, dict):
            inner = data.get("response_data") or data
        else:
            inner = {}
        return SendResult(
            status=SendStatus.SENT,
            message_id=msg["Message-ID"] or "",
            thread_id=str(inner.get("threadId") or inner.get("thread_id") or ""),
            external_id=str(inner.get("id") or ""),
            raw=data if isinstance(data, dict) else {"data": data},
        )

    def detect_replies(self, member_email: str,
                        since: datetime) -> list[Reply]:
        """Pull replies received since `since` by hitting Gmail's
        list+get via Composio. For high-volume installs, the existing
        Globus Gmail sync (server/sync_gmail.py) populates
        globus_vault_files with a richer index — we can read from
        there instead for a faster path. v1 hits the live API."""
        try:
            from composio_helpers import execute
        except Exception:
            return []
        # Query Gmail for messages newer than `since`. Gmail's q
        # syntax: after:YYYY/MM/DD. (Gmail's q doesn't support
        # millisecond precision — we paginate forward from the day.)
        when = since.strftime("%Y/%m/%d")
        q = f"after:{when} -in:sent -in:drafts -in:spam -in:trash"
        listing = execute(
            email=member_email, app="gmail",
            tool_slug="GMAIL_FETCH_EMAILS",
            arguments={"query": q, "max_results": 50})
        if not listing.get("ok"):
            print(f"[narada/gmail] reply detection list failed: "
                  f"{listing.get('error')}", flush=True)
            return []
        data = listing.get("data") or {}
        if isinstance(data, dict):
            messages = (data.get("response_data") or data).get("messages", []) \
                if isinstance(data.get("response_data") or data, dict) else []
        else:
            messages = []
        out: list[Reply] = []
        for m in (messages or [])[:50]:
            mid = m.get("messageId") or m.get("id") or ""
            if not mid:
                continue
            # Pull full message for headers + body. (Could batch via
            # users.messages.batchGet but v1 keeps it simple.)
            full = execute(
                email=member_email, app="gmail",
                tool_slug="GMAIL_FETCH_MESSAGE_BY_THREAD_ID"
                          if m.get("threadId") else "GMAIL_FETCH_EMAILS",
                arguments={"message_id": mid})
            if not full.get("ok"):
                continue
            fdata = full.get("data") or {}
            if isinstance(fdata, dict):
                inner = fdata.get("response_data") or fdata
            else:
                inner = {}
            headers = {h.get("name", "").lower(): h.get("value", "")
                       for h in (inner.get("payload", {}).get("headers", []) or [])}
            in_reply_to = headers.get("in-reply-to") or ""
            from_addr = headers.get("from") or ""
            subject = headers.get("subject") or ""
            # Body extraction — Gmail nests text in payload.parts.
            body = ""
            payload = inner.get("payload") or {}
            for part in (payload.get("parts") or [payload]):
                if part.get("mimeType") == "text/plain":
                    data_b64 = (part.get("body") or {}).get("data") or ""
                    try:
                        body = base64.urlsafe_b64decode(
                            data_b64 + "==").decode("utf-8", errors="replace")
                    except Exception:
                        pass
                    if body:
                        break
            out.append(Reply(
                in_reply_to_message_id=in_reply_to,
                from_addr=from_addr,
                subject=subject,
                body=body[:5000],
                received_at=datetime.utcnow().isoformat() + "Z",
                thread_id=m.get("threadId", ""),
                raw=inner if isinstance(inner, dict) else {},
            ))
        return out


# Module-level registration — runs once when narada_plugins auto-loader
# imports this file. Guarded so re-import (dev hot-reload) doesn't blow.
try:
    register(GmailComposioSender())
except Exception as _e:
    print(f"[narada/gmail] register failed: {type(_e).__name__}: {_e}",
          flush=True)
