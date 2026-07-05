"""Instantly.ai plugin — implements the Sender protocol.

Instantly (instantly.ai) is a cold-email platform: campaigns, rotating
sending accounts, built-in warmup, and a unified inbox. It has NO
transactional single-send API — sends happen via platform campaigns.
So this plugin follows the industry-standard pattern: Narada ADDS the
prospect to a member-created Instantly campaign whose sequence template
references the variables {{narada_subject}} and {{narada_body}}; the
subject/body Narada composed are passed as custom variables and
Instantly's own scheduler delivers the email.

Setup for members (paste into /members/narada/credentials, slug
`instantly`; exact field names):
  api_key     — Instantly API v2 key (Settings → Integrations → API keys)
  campaign_id — the UUID of the Instantly campaign to feed. Create it in
                the Instantly UI with a ONE-step sequence whose subject
                is {{narada_subject}} and body is {{narada_body}}, and
                attach your sending accounts to it.

Auth: Bearer API key. API base: https://api.instantly.ai/api/v2
Docs: https://developer.instantly.ai
Add lead: POST /leads  {campaign, email, custom_variables, ...} → {id}.
Replies:  GET /emails?campaign_id=&email_type=received&
          min_timestamp_created= — inbound mail on a campaign is, by
          construction, replies from leads. Instantly does not expose
          the RFC Message-ID of the ORIGINAL send, so Reply.
          in_reply_to_message_id carries the platform email id instead
          (noted in raw).
"""
from __future__ import annotations
import json
from datetime import datetime, timezone
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from narada_creds import get_credential, has_credential, touch_last_used
from narada_plugins import register
from narada_plugins.types import (
    AuthMethod, PluginCategory, PluginInfo, Reply, SendResult, SendStatus,
)


INSTANTLY_API_BASE = "https://api.instantly.ai/api/v2"
INSTANTLY_TIMEOUT = 30
INSTANTLY_USER_AGENT = "Narada/1.0 (+https://globussoft.com)"
# Instantly doesn't publish a hard daily send ceiling — monthly email
# quotas are plan-tier (Growth ~5k/mo, Hypergrowth ~100k/mo, Light Speed
# ~500k/mo) and actual throughput depends on how many sending accounts
# the member attached to the campaign. 5000/day is a sane high default;
# Instantly's own scheduler paces below whatever the accounts allow.
INSTANTLY_DEFAULT_DAILY_CAP = 5000


def _creds(member_email: str) -> tuple[str | None, str | None]:
    """Return (api_key, campaign_id) for this member, or (None, None)."""
    cred = get_credential(member_email, "instantly")
    if not cred:
        return None, None
    api_key = (cred.get("api_key") or "").strip() or None
    campaign_id = (cred.get("campaign_id") or "").strip() or None
    return api_key, campaign_id


def _request(member_email: str, method: str, path: str,
             params: dict | None = None,
             payload: dict | None = None) -> dict:
    """Call Instantly's REST API + return parsed JSON. Never raises —
    on transport/HTTP error returns {'error': '...', 'http_status': n}."""
    api_key, _ = _creds(member_email)
    if not api_key:
        return {"error": "no Instantly credential for this member"}
    url = f"{INSTANTLY_API_BASE}{path}"
    if params:
        url += "?" + urlencode(params)
    data = json.dumps(payload).encode("utf-8") if payload is not None \
        else None
    req = Request(url, data=data, method=method, headers={
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "User-Agent": INSTANTLY_USER_AGENT,
    })
    try:
        with urlopen(req, timeout=INSTANTLY_TIMEOUT) as r:
            resp = json.loads(r.read().decode("utf-8"))
    except HTTPError as e:
        body_txt = ""
        try:
            body_txt = e.read().decode("utf-8", "replace")[:300]
        except Exception:
            pass
        return {"error": f"HTTP {e.code}: {body_txt}",
                "http_status": e.code}
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}
    if not isinstance(resp, dict):
        return {"error": f"unexpected response shape: "
                         f"{type(resp).__name__}"}
    touch_last_used(member_email, "instantly")
    return resp


def _reply_body_text(item: dict) -> str:
    """Extract plain text from an /emails item. `body` is normally
    {'text': ..., 'html': ...}; preview-mode items only carry
    content_preview."""
    body = item.get("body")
    if isinstance(body, dict):
        text = body.get("text") or body.get("html") or ""
    elif isinstance(body, str):
        text = body
    else:
        text = ""
    return (text or item.get("content_preview") or "")[:5000]


class InstantlySender:
    """Sender plugin: pushes prospects into a member-owned Instantly
    campaign; Instantly's scheduler + sending accounts own delivery."""

    @classmethod
    def info(cls) -> PluginInfo:
        return PluginInfo(
            name="instantly",
            display_name="Instantly",
            category=PluginCategory.SENDER,
            auth_method=AuthMethod.API_KEY,
            requires_credentials=["api_key", "campaign_id"],
            homepage="https://instantly.ai",
            docs_url="https://developer.instantly.ai",
            description=(
                "Send through your Instantly campaign — rotating inboxes, "
                "built-in warmup, and deliverability tooling. Create a "
                "campaign in Instantly with a one-step sequence whose "
                "subject is {{narada_subject}} and body is "
                "{{narada_body}}, attach your sending accounts, then "
                "paste your API key and that campaign's ID here. Narada "
                "adds each prospect with its personalised copy and "
                "Instantly handles the actual sending and pacing."
            ),
            free_tier=False,
        )

    def is_available(self, member_email: str) -> bool:
        return has_credential(member_email, "instantly")

    def daily_send_cap(self, member_email: str) -> int:
        return INSTANTLY_DEFAULT_DAILY_CAP

    def supports_warmup(self) -> bool:
        return True   # Instantly ships built-in inbox warmup

    def send(self, member_email: str, from_addr: str, to: str,
             subject: str, body: str, headers: dict | None = None,
             reply_to: str | None = None) -> SendResult:
        """Add `to` as a lead on the member's Instantly campaign with
        the composed subject/body as custom variables. `from_addr`,
        `headers` and `reply_to` are ignored — Instantly sends from the
        accounts attached to the campaign. 2xx maps to SENT (Instantly
        owns delivery); details land in raw."""
        if not to:
            return SendResult(status=SendStatus.FAILED,
                              error="empty recipient address")
        api_key, campaign_id = _creds(member_email)
        if not api_key or not campaign_id:
            return SendResult(
                status=SendStatus.FAILED,
                error="Instantly credential incomplete — need both "
                      "api_key and campaign_id at "
                      "/members/narada/credentials")
        payload = {
            "campaign": campaign_id,
            "email": to,
            "custom_variables": {
                "narada_subject": subject or "",
                "narada_body": body or "",
            },
            # Don't double-add a prospect already in this campaign —
            # Instantly would restart the sequence for them.
            "skip_if_in_campaign": True,
        }
        resp = _request(member_email, "POST", "/leads", payload=payload)
        if "error" in resp:
            if resp.get("http_status") == 429:
                return SendResult(status=SendStatus.THROTTLED,
                                  error=resp["error"], raw=resp)
            return SendResult(status=SendStatus.FAILED,
                              error=resp["error"], raw=resp)
        lead_id = str(resp.get("id") or "")
        if not lead_id:
            return SendResult(
                status=SendStatus.FAILED,
                error="Instantly accepted the request but returned no "
                      "lead id",
                raw=resp)
        return SendResult(
            status=SendStatus.SENT,   # queued on the platform; Instantly
                                      # owns actual delivery timing
            external_id=lead_id,
            raw={"lead_id": lead_id,
                 "campaign": str(resp.get("campaign") or campaign_id),
                 "status": str(resp.get("status") or ""),
                 "note": "SENT = accepted into the Instantly campaign; "
                         "delivery is paced by Instantly's scheduler"})

    def detect_replies(self, member_email: str,
                       since: datetime) -> list[Reply]:
        """Pull inbound emails on the campaign since `since` via
        GET /emails?email_type=received. One page of up to 100 —
        the caller windows over time, we never return unbounded sets.
        Auto-replies (OOO) are skipped."""
        _, campaign_id = _creds(member_email)
        if not campaign_id:
            return []
        # min_timestamp_created is UTC ('Z'); a tz-aware `since` (e.g.
        # IST) formatted naively would be hours off and drop replies.
        if since.tzinfo is not None:
            since = since.astimezone(timezone.utc).replace(tzinfo=None)
        params = {
            "campaign_id": campaign_id,
            "email_type": "received",
            "min_timestamp_created": since.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "sort_order": "asc",
            "limit": 100,
        }
        resp = _request(member_email, "GET", "/emails", params=params)
        if "error" in resp:
            return []
        out: list[Reply] = []
        for item in (resp.get("items") or []):
            try:
                if item.get("is_auto_reply"):
                    continue
                platform_id = str(item.get("id") or "")
                if not platform_id:
                    continue
                out.append(Reply(
                    # Instantly doesn't expose the RFC Message-ID of the
                    # ORIGINAL send, so we carry the platform email id
                    # here; the reply's own RFC id is in raw.message_id.
                    in_reply_to_message_id=platform_id,
                    from_addr=str(item.get("from_address_email") or ""),
                    subject=str(item.get("subject") or ""),
                    body=_reply_body_text(item),
                    received_at=str(item.get("timestamp_email")
                                    or item.get("timestamp_created")
                                    or ""),
                    thread_id=str(item.get("thread_id") or ""),
                    raw={"id": platform_id,
                         "message_id": str(item.get("message_id") or ""),
                         "lead": str(item.get("lead") or ""),
                         "campaign_id": str(item.get("campaign_id")
                                            or ""),
                         "eaccount": str(item.get("eaccount") or ""),
                         "note": "in_reply_to_message_id is the "
                                 "Instantly email id — the platform "
                                 "doesn't expose the original send's "
                                 "RFC Message-ID"}))
            except Exception:
                continue
        return out


# Auto-register
try:
    register(InstantlySender())
except Exception as _e:
    print(f"[narada/instantly] register failed: "
          f"{type(_e).__name__}: {_e}", flush=True)
