"""Smartlead sender plugin — implements the Sender protocol.

Smartlead (smartlead.ai) is a cold-email sending platform: unlimited
connected mailboxes, built-in warmup, and its own delivery scheduler.
There is NO transactional single-send API — the correct integration is
campaign-based: the member creates a campaign in the Smartlead UI whose
sequence template references the variables {{narada_subject}} and
{{narada_body}}, and Narada "sends" by pushing each prospect into that
campaign with the personalised subject/body as custom variables.
Smartlead then delivers on its own schedule from the member's mailboxes,
so SendStatus.SENT here means "accepted into the campaign queue".

Auth: API key passed as `?api_key=` query param on every request.
Members paste TWO values into /members/narada/credentials → tool slug
`smartlead`:
  - api_key      — from Smartlead → Settings → Smartlead API Key
  - campaign_id  — numeric id of the Smartlead campaign whose sequence
                   uses {{narada_subject}} / {{narada_body}}

API base: https://server.smartlead.ai/api/v1
Docs: https://api.smartlead.ai/api-reference (the old /reference/*
slugs redirect there).
  - Add lead:  POST /campaigns/{id}/leads (max 400/req). Classic
    deploys answer {ok, upload_count, ...}; the 2026 docs describe
    {success, added_count, lead_ids} where lead_ids appears when
    settings.return_lead_ids is true — we accept both shapes and
    fall back to GET /leads/?email= for the platform lead id.
  - Replies:   GET /campaigns/{id}/statistics rows carry lead_email +
    reply_time + email_subject but NO RFC Message-ID and no body.
    Both come from GET /campaigns/{id}/leads/{lead_id}/message-history:
    the SENT entry's message_id is OUR send's RFC Message-ID and the
    REPLY entry's email_body is the reply itself.
"""
from __future__ import annotations
import json
from datetime import datetime, timezone
from urllib.error import HTTPError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from narada_creds import get_credential, has_credential, touch_last_used
from narada_plugins import register
from narada_plugins.types import (
    AuthMethod, PluginCategory, PluginInfo, Reply, SendResult, SendStatus,
)


SMARTLEAD_API_BASE = "https://server.smartlead.ai/api/v1"
SMARTLEAD_TIMEOUT = 30
SMARTLEAD_UA = "Narada/1.0 (outbound agent; +https://globussoft.com)"
# Smartlead's own scheduler enforces the real per-mailbox sending limits
# downstream, so Narada's cap only needs to not be the bottleneck. This
# matches the platform capacity noted in protocols.py ("Smartlead =
# 150000") — an account with hundreds of warmed mailboxes really can
# move that volume; smaller accounts are throttled by Smartlead itself.
SMARTLEAD_DEFAULT_DAILY_CAP = 150_000
# detect_replies bounds — the protocol forbids unbounded result sets,
# and every reply enrichment costs 2 extra API round-trips.
STATS_PAGE_SIZE = 100          # docs allow up to 1000/page; 100 keeps payloads sane
MAX_STATS_PAGES = 5            # ⇒ at most 500 stats rows per call
MAX_BODY_ENRICH = 20           # replies enriched via message-history per call


def _creds(member_email: str) -> tuple[str, str] | None:
    """Return (api_key, campaign_id) or None if either is missing."""
    cred = get_credential(member_email, "smartlead") or {}
    api_key = str(cred.get("api_key") or "").strip()
    campaign_id = str(cred.get("campaign_id") or "").strip()
    if not api_key or not campaign_id:
        return None
    return api_key, campaign_id


def _call(api_key: str, method: str, path: str,
          params: dict | None = None,
          payload: dict | None = None) -> dict:
    """Hit Smartlead's REST API + return parsed JSON. Never raises —
    on transport/HTTP error returns {'error': '...'}. Bare-array
    responses are wrapped as {'data': [...]} for uniform handling."""
    q = urlencode({**(params or {}), "api_key": api_key})
    url = f"{SMARTLEAD_API_BASE}{path}?{q}"
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    headers = {"User-Agent": SMARTLEAD_UA}
    if data is not None:
        headers["Content-Type"] = "application/json"
    req = Request(url, data=data, headers=headers, method=method)
    try:
        with urlopen(req, timeout=SMARTLEAD_TIMEOUT) as r:
            body_txt = r.read().decode("utf-8")
    except HTTPError as e:
        err_txt = ""
        try:
            err_txt = e.read().decode("utf-8", "replace")[:300]
        except Exception:
            pass
        return {"error": f"HTTP {e.code}: {err_txt}"}
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}
    try:
        resp = json.loads(body_txt)
    except Exception:
        return {"error": f"non-JSON response: {body_txt[:200]}"}
    if isinstance(resp, list):
        return {"data": resp}
    return resp if isinstance(resp, dict) else {"data": resp}


def _parse_iso(ts: str) -> datetime | None:
    """Parse Smartlead's ISO-8601 timestamps ('...Z' or offset form)
    into naive UTC so they compare cleanly against `since`."""
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except Exception:
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def _lead_id_by_email(api_key: str, email: str) -> str:
    """Resolve the platform lead id by email via GET /leads/?email=
    (returns the lead object, or an empty object if unknown).
    Best-effort — '' on any failure."""
    if not email:
        return ""
    resp = _call(api_key, "GET", "/leads/", {"email": email})
    if "error" in resp:
        return ""
    lid = resp.get("id")
    if lid is None and isinstance(resp.get("data"), dict):
        lid = resp["data"].get("id")
    return str(lid) if lid is not None else ""


def _reply_details(api_key: str, campaign_id: str, lead_email: str,
                   since: datetime) -> dict:
    """Best-effort message-history lookup for one replied lead. The
    campaign statistics rows carry neither the reply body nor any RFC
    Message-ID, so both come from here: the newest REPLY entry at/after
    `since` supplies the body (email_body) + its own message_id, and
    the last outbound (SENT) entry before it supplies the RFC
    Message-ID of OUR send for Reply.in_reply_to_message_id. Two extra
    API calls — bounded by MAX_BODY_ENRICH. Returns {} on any failure;
    keys: body, orig_message_id, reply_message_id, stats_id."""
    lead_id = _lead_id_by_email(api_key, lead_email)
    if not lead_id:
        return {}
    resp = _call(
        api_key, "GET",
        f"/campaigns/{quote(campaign_id, safe='')}/leads/"
        f"{quote(lead_id, safe='')}/message-history")
    if "error" in resp:
        return {}
    history = resp.get("history") or resp.get("data") or []
    if not isinstance(history, list):
        return {}
    last_out_msg_id = ""
    det: dict = {}
    for item in history:
        if not isinstance(item, dict):
            continue
        if str(item.get("type") or "").upper() != "REPLY":
            # Outbound entry (type SENT) — remember OUR send's RFC
            # Message-ID so the next REPLY can reference it.
            last_out_msg_id = (str(item.get("message_id") or "")
                               or last_out_msg_id)
            continue
        when = _parse_iso(str(item.get("time") or ""))
        if when is not None and when < since:
            continue
        entry = {
            "body": str(item.get("email_body") or ""),
            "orig_message_id": last_out_msg_id,
            "reply_message_id": str(item.get("message_id") or ""),
            "stats_id": str(item.get("stats_id") or ""),
        }
        # History runs oldest→newest: prefer the newest qualifying
        # REPLY that actually has a body, else the newest overall.
        if entry["body"] or not det:
            det = entry
    return det


class SmartleadSender:
    """Sender plugin: queues prospects into the member's Smartlead
    campaign (custom vars narada_subject / narada_body) and detects
    replies via the campaign statistics + message-history endpoints."""

    @classmethod
    def info(cls) -> PluginInfo:
        return PluginInfo(
            name="smartlead",
            display_name="Smartlead",
            category=PluginCategory.SENDER,
            auth_method=AuthMethod.API_KEY,
            requires_credentials=["api_key", "campaign_id"],
            homepage="https://smartlead.ai",
            docs_url="https://api.smartlead.ai/authentication",
            description=(
                "Cold email at scale through your Smartlead account — "
                "unlimited mailboxes, built-in warmup, and Smartlead's "
                "own delivery scheduler. Create a campaign in Smartlead "
                "whose sequence uses the variables {{narada_subject}} "
                "and {{narada_body}}, then paste your API key and that "
                "campaign's id here. Narada drops each prospect into the "
                "campaign with a personalised subject/body and Smartlead "
                "handles delivery, throttling and reply tracking."
            ),
            free_tier=False,
        )

    def is_available(self, member_email: str) -> bool:
        return has_credential(member_email, "smartlead")

    def daily_send_cap(self, member_email: str) -> int:
        return SMARTLEAD_DEFAULT_DAILY_CAP

    def supports_warmup(self) -> bool:
        return True

    def send(self, member_email: str, from_addr: str, to: str,
             subject: str, body: str, headers: dict | None = None,
             reply_to: str | None = None) -> SendResult:
        creds = _creds(member_email)
        if not creds:
            return SendResult(
                status=SendStatus.FAILED,
                error="no Smartlead credential (need api_key + "
                      "campaign_id) — add at /members/narada/credentials")
        api_key, campaign_id = creds
        if not to or "@" not in to:
            return SendResult(status=SendStatus.FAILED,
                              error=f"invalid recipient address: {to!r}")
        # from_addr / reply_to / custom headers are per-mailbox settings
        # inside the Smartlead campaign, not per-message — ignored here.
        payload = {
            "lead_list": [{
                "email": to,
                "custom_fields": {
                    "narada_subject": subject or "",
                    "narada_body": body or "",
                },
            }],
            "settings": {
                "ignore_global_block_list": False,
                "ignore_unsubscribe_list": False,
                "ignore_duplicate_leads_in_other_campaign": False,
                # Newer API revisions return the created lead ids in
                # the response; classic deploys ignore this key.
                "return_lead_ids": True,
            },
        }
        resp = _call(api_key, "POST",
                     f"/campaigns/{quote(campaign_id, safe='')}/leads",
                     payload=payload)
        if "error" in resp:
            return SendResult(status=SendStatus.FAILED,
                              error=resp["error"], raw=resp)
        uploaded = 0
        try:
            uploaded = int(resp.get("upload_count")       # classic shape
                           or resp.get("added_count")     # 2026 docs shape
                           or 0)
        except Exception:
            pass
        # Classic deploys answer {"ok": true, "upload_count": n};
        # newer ones {"success": true, "added_count": n}. If neither
        # flag is present, trust the count alone.
        flag = resp.get("ok")
        if flag is None:
            flag = resp.get("success")
        ok = uploaded >= 1 if flag is None else bool(flag)
        if not ok or uploaded < 1:
            return SendResult(
                status=SendStatus.FAILED,
                error=("Smartlead did not accept the lead (duplicate, "
                       "blocklist or invalid email): "
                       f"{str(resp)[:200]}"),
                raw=resp)
        touch_last_used(member_email, "smartlead")
        lead_ids = resp.get("lead_ids")
        external_id = (str(lead_ids[0])
                       if isinstance(lead_ids, list) and lead_ids
                       else "")
        if not external_id:
            external_id = _lead_id_by_email(api_key, to)
        # No Message-ID exists yet — Smartlead sends later from its own
        # scheduler; SENT here means "queued into the platform campaign".
        return SendResult(
            status=SendStatus.SENT,
            external_id=external_id,
            raw={"campaign_id": campaign_id, "response": resp,
                 "note": "queued into Smartlead campaign; delivery is "
                         "owned by Smartlead's scheduler"})

    def detect_replies(self, member_email: str,
                       since: datetime) -> list[Reply]:
        """Page the campaign statistics endpoint for rows with a
        reply_time >= since, then enrich the newest MAX_BODY_ENRICH of
        them from message-history — the stats rows carry neither the
        reply body nor any RFC Message-ID, so both come from there."""
        creds = _creds(member_email)
        if not creds:
            return []
        api_key, campaign_id = creds
        if since.tzinfo is not None:
            since = since.astimezone(timezone.utc).replace(tzinfo=None)
        stats_path = f"/campaigns/{quote(campaign_id, safe='')}/statistics"
        base = {"limit": STATS_PAGE_SIZE, "email_status": "replied"}
        rows: list[dict] = []
        try:
            for page in range(MAX_STATS_PAGES):
                resp = _call(api_key, "GET", stats_path,
                             {**base, "offset": page * STATS_PAGE_SIZE})
                if "error" in resp and page == 0:
                    # Some deploys reject the email_status filter —
                    # retry unfiltered; reply_time filtering below is
                    # authoritative either way.
                    base = {"limit": STATS_PAGE_SIZE}
                    resp = _call(api_key, "GET", stats_path,
                                 {**base, "offset": 0})
                if "error" in resp:
                    break
                data = resp.get("data") or []
                if not isinstance(data, list) or not data:
                    break
                rows.extend(x for x in data if isinstance(x, dict))
                if len(data) < STATS_PAGE_SIZE:
                    break
        except Exception:
            pass
        # Keep only rows that replied inside the window, newest first
        # so the enrichment budget goes to the freshest replies.
        fresh: list[tuple[datetime, dict]] = []
        for row in rows:
            reply_at = _parse_iso(str(row.get("reply_time") or ""))
            if reply_at is not None and reply_at >= since:
                fresh.append((reply_at, row))
        fresh.sort(key=lambda p: p[0], reverse=True)
        out: list[Reply] = []
        for i, (reply_at, row) in enumerate(fresh):
            try:
                lead_email = str(row.get("lead_email") or "")
                det: dict = {}
                if i < MAX_BODY_ENRICH:
                    det = _reply_details(api_key, campaign_id,
                                         lead_email, since)
                orig_msg_id = str(det.get("orig_message_id") or "")
                if orig_msg_id:
                    src = "message-history SENT entry (RFC Message-ID)"
                elif i >= MAX_BODY_ENRICH:
                    src = ("not fetched — beyond this call's "
                           "message-history enrichment budget")
                else:
                    src = ("unavailable — message-history had no SENT "
                           "entry with a message_id before the reply")
                out.append(Reply(
                    in_reply_to_message_id=orig_msg_id,
                    from_addr=lead_email,
                    subject=str(row.get("email_subject") or ""),
                    body=str(det.get("body") or "")[:5000],
                    received_at=reply_at.isoformat() + "Z",
                    thread_id=str(det.get("stats_id") or ""),
                    raw={"campaign_id": campaign_id,
                         "sequence_number": row.get("sequence_number"),
                         "reply_message_id": str(
                             det.get("reply_message_id") or ""),
                         "message_id_source": src}))
            except Exception:
                continue
        if out:
            try:
                touch_last_used(member_email, "smartlead")
            except Exception:
                pass
        return out


# Auto-register
try:
    register(SmartleadSender())
except Exception as _e:
    print(f"[narada/smartlead] register failed: "
          f"{type(_e).__name__}: {_e}", flush=True)
