"""Lemlist plugin — implements the Sender protocol.

Lemlist (lemlist.com) is a cold-outreach platform: campaigns with
multi-step sequences, per-mailbox rotation, and built-in warmup
(lemwarm). There is NO transactional single-send API — Lemlist sends
via platform-side campaigns. So this plugin follows the industry
pattern for campaign senders: `send()` ADDS the prospect to a Lemlist
campaign the member created in the Lemlist UI, passing Narada's
personalised copy as the custom variables `narada_subject` and
`narada_body`. The member's campaign sequence template MUST reference
{{narada_subject}} and {{narada_body}} — otherwise Lemlist sends the
template's own static copy instead of Narada's.

Auth: API key via HTTP Basic — BLANK username, key as password
(base64 of ":<api_key>"). Generate at lemlist Settings → Integrations.
Paste into /members/narada/credentials → tool slug `lemlist`, along
with `campaign_id` (the `cam_...` id of the campaign to feed, visible
in the campaign URL). Per-member isolation via the
globus_narada_credentials vault.

API base: https://api.lemlist.com/api
Docs: https://developer.lemlist.com/
  Add lead:  POST /campaigns/{campaignId}/leads/   (email in body;
             any extra body key becomes a {{custom variable}})
  Replies:   GET /activities?version=v2&type=emailsReplied
             &campaignId=...&minDate=<ISO>&limit=100&offset=N
Lemlist does not expose RFC Message-IDs, so Reply.in_reply_to_message_id
carries the Lemlist activity id (noted in raw).
"""
from __future__ import annotations
import base64
import json
from datetime import datetime
from urllib.error import HTTPError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from narada_creds import get_credential, has_credential, touch_last_used
from narada_plugins import register
from narada_plugins.types import (
    AuthMethod, PluginCategory, PluginInfo, Reply, SendResult, SendStatus,
)


LEMLIST_API_BASE = "https://api.lemlist.com/api"
LEMLIST_TIMEOUT = 30
LEMLIST_USER_AGENT = "Narada/1.0 (+https://globussoft.com)"
# Platform-tier default: Lemlist caps per connected mailbox (~200/day
# reputation-safe); teams rotate many mailboxes per campaign. 5000/day
# assumes ~25 mailboxes — Narada core throttles to this regardless.
LEMLIST_DEFAULT_DAILY_CAP = 5000
# detect_replies pages 100 at a time, hard-capped per call so we never
# return an unbounded result set (protocol requirement).
LEMLIST_REPLIES_MAX_PER_CALL = 300


def _creds(member_email: str) -> tuple[str, str]:
    """Return (api_key, campaign_id) — either may be '' if missing."""
    cred = get_credential(member_email, "lemlist") or {}
    return ((cred.get("api_key") or "").strip(),
            (cred.get("campaign_id") or "").strip())


def _auth_header(api_key: str) -> str:
    """Lemlist Basic auth: BLANK username, api key as the password."""
    token = base64.b64encode(f":{api_key}".encode()).decode("ascii")
    return f"Basic {token}"


def _api(api_key: str, method: str, path: str,
         params: dict | None = None, payload: dict | None = None):
    """Call Lemlist's REST API + return parsed JSON (dict or list).
    Never raises — on transport/HTTP error returns {'error': '...'}."""
    url = f"{LEMLIST_API_BASE}{path}"
    if params:
        url += "?" + urlencode(params)
    data = json.dumps(payload).encode() if payload is not None else None
    req = Request(url, data=data, method=method, headers={
        "Authorization": _auth_header(api_key),
        "Content-Type": "application/json",
        "User-Agent": LEMLIST_USER_AGENT,
    })
    try:
        with urlopen(req, timeout=LEMLIST_TIMEOUT) as r:
            body = r.read().decode("utf-8")
            return json.loads(body) if body.strip() else {}
    except HTTPError as e:
        body_txt = ""
        try:
            body_txt = e.read().decode("utf-8", "replace")[:300]
        except Exception:
            pass
        return {"error": f"HTTP {e.code}: {body_txt}"}
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}


class LemlistSender:
    """Sender plugin: feeds prospects into a member-owned Lemlist
    campaign; Lemlist owns sequencing, delivery, and warmup."""

    @classmethod
    def info(cls) -> PluginInfo:
        return PluginInfo(
            name="lemlist",
            display_name="Lemlist",
            category=PluginCategory.SENDER,
            auth_method=AuthMethod.API_KEY,
            requires_credentials=["api_key", "campaign_id"],
            homepage="https://www.lemlist.com",
            docs_url="https://developer.lemlist.com/",
            description=(
                "Cold-outreach platform with mailbox rotation and "
                "built-in warmup (lemwarm). Narada adds each prospect "
                "to a Lemlist campaign you create, passing the "
                "personalised copy as custom variables — your campaign's "
                "email template must contain {{narada_subject}} and "
                "{{narada_body}}. Paste your API key (Settings → "
                "Integrations) and the campaign's cam_... id."
            ),
            free_tier=False,
        )

    def is_available(self, member_email: str) -> bool:
        return has_credential(member_email, "lemlist")

    def daily_send_cap(self, member_email: str) -> int:
        return LEMLIST_DEFAULT_DAILY_CAP

    def supports_warmup(self) -> bool:
        return True   # lemwarm is built into the platform

    def send(self, member_email: str, from_addr: str, to: str,
             subject: str, body: str, headers: dict | None = None,
             reply_to: str | None = None) -> SendResult:
        """Add `to` to the member's Lemlist campaign with Narada's copy
        as custom variables. Lemlist sends from the campaign's own
        configured mailboxes — `from_addr`, `headers` and `reply_to`
        are platform-owned and ignored here. Lemlist QUEUES the lead
        into the sequence; we map that to SENT because the platform
        owns delivery from this point (details in raw). Dedup against
        prior outreach is Narada's local suppression list — we do NOT
        pass deduplicate=true, so a re-add of an email already in this
        campaign fails loudly instead of silently dropping."""
        api_key, campaign_id = _creds(member_email)
        if not api_key or not campaign_id:
            return SendResult(
                status=SendStatus.FAILED,
                error="lemlist credential incomplete — need api_key AND "
                      "campaign_id at /members/narada/credentials")
        if not to or "@" not in to:
            return SendResult(status=SendStatus.FAILED,
                              error=f"invalid recipient: {to!r}")
        payload = {
            "email": to,
            # Extra keys become {{custom variables}} the campaign
            # template references — this is how Narada's copy rides in.
            "narada_subject": subject or "",
            "narada_body": body or "",
        }
        resp = _api(api_key, "POST",
                    f"/campaigns/{quote(campaign_id, safe='')}/leads/",
                    payload=payload)
        if not isinstance(resp, dict) or "error" in resp:
            err = resp.get("error") if isinstance(resp, dict) else str(resp)
            return SendResult(status=SendStatus.FAILED,
                              error=f"lemlist add-lead failed: {err}")
        try:
            touch_last_used(member_email, "lemlist")
        except Exception:
            pass   # freshness bump is best-effort — the lead IS queued
        return SendResult(
            status=SendStatus.SENT,   # queued into the campaign sequence
            external_id=str(resp.get("_id") or ""),
            raw={"note": "lead queued into lemlist campaign sequence; "
                         "lemlist owns delivery timing + mailbox choice",
                 "campaign_id": str(resp.get("campaignId") or campaign_id),
                 "contact_id": str(resp.get("contactId") or ""),
                 "response": resp})

    def detect_replies(self, member_email: str,
                       since: datetime) -> list[Reply]:
        """Pull emailsReplied activities for the configured campaign
        since `since`. Lemlist exposes no RFC Message-IDs, so
        in_reply_to_message_id is the Lemlist activity id (act_...) —
        callers match replies to sends by lead email / campaign
        instead (flagged in raw)."""
        api_key, campaign_id = _creds(member_email)
        if not api_key or not campaign_id:
            # Without campaignId the /activities call would return the
            # team's replies across ALL lemlist campaigns — including
            # ones Narada never touched. Scope strictly to ours.
            return []
        min_date = since.strftime("%Y-%m-%dT%H:%M:%SZ")
        out: list[Reply] = []
        offset = 0
        while offset < LEMLIST_REPLIES_MAX_PER_CALL:
            params = {"version": "v2", "type": "emailsReplied",
                      "campaignId": campaign_id, "limit": 100,
                      "offset": offset, "minDate": min_date}
            resp = _api(api_key, "GET", "/activities", params=params)
            if not isinstance(resp, list):
                break   # {'error': ...} or unexpected shape — stop paging
            for act in resp:
                try:
                    if not isinstance(act, dict):
                        continue
                    lead = act.get("lead")
                    if not isinstance(lead, dict):
                        lead = {}
                    meta = act.get("metaData")
                    if not isinstance(meta, dict):
                        meta = {}
                    body_txt = str(meta.get("text") or meta.get("body")
                                   or act.get("text") or "")
                    out.append(Reply(
                        # No RFC Message-ID from lemlist → activity id.
                        in_reply_to_message_id=str(act.get("_id") or ""),
                        from_addr=str(act.get("leadEmail")
                                      or lead.get("email") or ""),
                        subject=str(act.get("subject")
                                    or meta.get("subject") or ""),
                        body=body_txt[:5000],
                        received_at=str(act.get("createdAt") or ""),
                        thread_id=str(act.get("campaignId") or ""),
                        raw={"note": "in_reply_to_message_id is the "
                                     "lemlist activity id — lemlist "
                                     "exposes no RFC Message-IDs; match "
                                     "by lead email",
                             "activity_id": str(act.get("_id") or ""),
                             "lead_id": str(act.get("leadId") or ""),
                             "sequence_step": act.get("sequenceStep")}))
                except Exception:
                    continue   # one malformed activity ≠ a dead batch
            if len(resp) < 100:
                break
            offset += 100
        if out:
            try:
                touch_last_used(member_email, "lemlist")
            except Exception:
                pass
        return out


# Auto-register
try:
    register(LemlistSender())
except Exception as _e:
    print(f"[narada/lemlist] register failed: "
          f"{type(_e).__name__}: {_e}", flush=True)
