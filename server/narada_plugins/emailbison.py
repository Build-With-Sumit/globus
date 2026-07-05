"""EmailBison plugin — implements the Sender protocol.

EmailBison (emailbison.com) is a cold-email sequencer with per-customer
dedicated instances, built-in warmup, unlimited lead storage and a
master inbox. Like Smartlead/Instantly it has NO transactional
single-send API — sending happens through platform-side campaigns.
Narada integrates the industry-standard way: `send()` upserts the
prospect as a lead with the personalised subject/body attached as the
custom variables `narada_subject` / `narada_body`, then attaches the
lead to the member's campaign. The member must create ONE campaign in
the EmailBison UI whose sequence step uses `{{narada_subject}}` as the
subject and `{{narada_body}}` as the body — EmailBison then owns
delivery, throttling, rotation and threading for that campaign.

Auth: Bearer token (Settings -> Developer API -> New API Token; prefer
`api-user` keys). Base URL is PER-INSTANCE (white-label / dedicated,
e.g. https://dedi.emailbison.com) so the member pastes it alongside
the key. Paste into /members/narada/credentials -> tool slug
`emailbison` with fields: `api_key`, `base_url`, `campaign_id`.

API (docs: https://docs.emailbison.com/, OpenAPI spec:
https://dedi.emailbison.com/api/reference.openapi):
  POST {base}/api/custom-variables                        create var
  GET  {base}/api/leads?search=                           find lead
  POST {base}/api/leads/create-or-update/multiple         upsert lead
  POST {base}/api/campaigns/{id}/leads/attach-leads       add to camp.
  GET  {base}/api/replies?folder=inbox&campaign_id=&page= replies
Responses are Laravel-style: `{"data": ...}` with `links`/`meta`
pagination (15/page). Leads attached to an active campaign sync
within ~5 minutes.
"""
from __future__ import annotations
import json
import re
from datetime import datetime, timezone
from urllib.error import HTTPError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from narada_creds import get_credential, has_credential, touch_last_used
from narada_plugins import register
from narada_plugins.types import (
    AuthMethod, PluginCategory, PluginInfo, Reply, SendResult, SendStatus,
)


EMAILBISON_TIMEOUT = 30
EMAILBISON_USER_AGENT = "Narada/1.0 (+https://globussoft.com)"
# EmailBison itself throttles via the sender accounts + schedule the
# member attached to the campaign; this is Narada's queueing guardrail
# so we don't dump an unbounded batch on the instance in one day.
EMAILBISON_DEFAULT_DAILY_CAP = 5000
# detect_replies pages the inbox newest-first; 15 replies per page
# (platform page size), hard cap so we never return unbounded sets.
EMAILBISON_MAX_REPLY_PAGES = 4

SUBJECT_VAR = "narada_subject"
BODY_VAR = "narada_body"

# (member_email, base_url) pairs for which we've already best-effort
# created the two custom variables this process lifetime.
_VARS_ENSURED: set[tuple[str, str]] = set()


def _cred(member_email: str) -> dict | None:
    cred = get_credential(member_email, "emailbison")
    if not cred or not (cred.get("api_key") or "").strip():
        return None
    return cred


def _base_url(cred: dict) -> str:
    """Normalise the member-pasted instance URL: force https scheme,
    drop trailing slashes and a trailing /api (we append it)."""
    base = (cred.get("base_url") or "").strip().rstrip("/")
    if not base:
        return ""
    if not re.match(r"^https?://", base, re.I):
        base = f"https://{base}"
    if base.lower().endswith("/api"):
        base = base[:-4]
    return base


def _request(member_email: str, method: str, url: str, api_key: str,
             payload: dict | None = None) -> dict:
    """Call EmailBison's REST API + return parsed JSON. Never raises —
    on transport/HTTP error returns {'error': '...', 'http_status': n}."""
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Accept": "application/json",
        "User-Agent": EMAILBISON_USER_AGENT,
    }
    data = None
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = Request(url, data=data, headers=headers, method=method)
    try:
        with urlopen(req, timeout=EMAILBISON_TIMEOUT) as r:
            resp = json.loads(r.read().decode("utf-8"))
    except HTTPError as e:
        body_txt = ""
        try:
            body_txt = e.read().decode("utf-8", "replace")[:300]
        except Exception:
            pass
        return {"error": f"HTTP {e.code}: {body_txt}", "http_status": e.code}
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}", "http_status": 0}
    if not isinstance(resp, dict):
        return {"error": f"unexpected response shape: {str(resp)[:200]}",
                "http_status": 0}
    try:
        touch_last_used(member_email, "emailbison")
    except Exception:
        pass  # freshness bump is best-effort; never fail the call on it
    return resp


def _ensure_custom_variables(member_email: str, base: str,
                             api_key: str) -> None:
    """Best-effort create the narada_subject / narada_body custom
    variables (EmailBison requires vars to exist per-workspace before
    they can be attached to leads). Idempotent-ish: errors (incl.
    'already exists') are swallowed, and we only try once per
    (member, instance) per process."""
    key = (member_email, base)
    if key in _VARS_ENSURED:
        return
    for name in (SUBJECT_VAR, BODY_VAR):
        try:
            _request(member_email, "POST", f"{base}/api/custom-variables",
                     api_key, {"name": name})
        except Exception:
            pass
    _VARS_ENSURED.add(key)


def _first_name_from_email(addr: str) -> str:
    """EmailBison requires first_name on lead creation; when Narada
    only knows the address, derive one from the local part
    (john.doe@x.com -> John)."""
    local = (addr or "").split("@")[0]
    token = re.split(r"[._+\-]", local)[0]
    return token.capitalize() if token else "Unknown"


def _existing_first_name(member_email: str, base: str, api_key: str,
                         to: str) -> str:
    """If the lead already exists, reuse its stored first_name so the
    patch-upsert doesn't clobber a real name with a derived one.
    EmailBison has no lead-by-email path (GET /api/leads/{lead_id}
    takes the numeric id only) — filter the list endpoint with
    ?search= and exact-match the email, case-insensitively."""
    q = urlencode({"search": to})
    resp = _request(member_email, "GET", f"{base}/api/leads?{q}", api_key)
    rows = resp.get("data")
    if not isinstance(rows, list):
        return ""
    want = to.strip().lower()
    for row in rows:
        if not isinstance(row, dict):
            continue
        if str(row.get("email") or "").strip().lower() == want:
            return str(row.get("first_name") or "")
    return ""


def _parse_iso(value: str) -> datetime | None:
    """Parse EmailBison's ISO-8601 timestamps to naive UTC."""
    try:
        dt = datetime.fromisoformat(
            (value or "").strip().replace("Z", "+00:00"))
    except Exception:
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def _strip_html(html: str) -> str:
    return re.sub(r"<[^>]+>", " ", html or "").strip()


class EmailBisonSender:
    """Sender plugin: queues personalised emails through the member's
    EmailBison instance via a Narada-templated campaign."""

    @classmethod
    def info(cls) -> PluginInfo:
        return PluginInfo(
            name="emailbison",
            display_name="EmailBison",
            category=PluginCategory.SENDER,
            auth_method=AuthMethod.API_KEY,
            requires_credentials=["api_key", "base_url", "campaign_id"],
            homepage="https://emailbison.com",
            docs_url="https://docs.emailbison.com/",
            description=(
                "Cold-email sequencer with built-in warmup, inbox "
                "rotation and a master inbox, on your own dedicated "
                "instance. Create ONE campaign in EmailBison whose "
                "sequence subject is {{narada_subject}} and body is "
                "{{narada_body}}, then paste your API token (Settings "
                "-> Developer API), your instance URL (e.g. "
                "https://dedi.emailbison.com) and that campaign's ID. "
                "Narada writes each personalised email into those "
                "variables and EmailBison handles the delivery."
            ),
            free_tier=False,
        )

    def is_available(self, member_email: str) -> bool:
        return has_credential(member_email, "emailbison")

    def daily_send_cap(self, member_email: str) -> int:
        return EMAILBISON_DEFAULT_DAILY_CAP

    def supports_warmup(self) -> bool:
        return True

    def send(self, member_email: str, from_addr: str, to: str,
             subject: str, body: str, headers: dict | None = None,
             reply_to: str | None = None) -> SendResult:
        """Upsert the prospect as a lead carrying narada_subject /
        narada_body, then attach it to the member's campaign. QUEUED
        semantics mapped to SENT — EmailBison owns actual delivery
        (leads sync to an active campaign within ~5 minutes).
        `from_addr`, `headers` and `reply_to` are ignored: the sender
        identity/rotation is whatever the member attached to the
        campaign inside EmailBison."""
        cred = _cred(member_email)
        if not cred:
            return SendResult(
                status=SendStatus.FAILED,
                error="no EmailBison credential for this member — add "
                      "api_key/base_url/campaign_id at "
                      "/members/narada/credentials")
        base = _base_url(cred)
        campaign_id = str(cred.get("campaign_id") or "").strip()
        if not base or not campaign_id:
            return SendResult(
                status=SendStatus.FAILED,
                error="EmailBison credential is missing base_url or "
                      "campaign_id — both are required")
        if not to:
            return SendResult(status=SendStatus.FAILED,
                              error="empty recipient address")
        api_key = (cred.get("api_key") or "").strip()
        _ensure_custom_variables(member_email, base, api_key)
        try:
            first_name = (_existing_first_name(member_email, base,
                                               api_key, to)
                          or _first_name_from_email(to))
            # Upsert (patch) so repeat campaigns to the same prospect
            # update the variables instead of failing on unique email.
            upsert = _request(
                member_email, "POST",
                f"{base}/api/leads/create-or-update/multiple", api_key,
                {"existing_lead_behavior": "patch",
                 "leads": [{
                     "first_name": first_name,
                     "email": to,
                     "custom_variables": [
                         {"name": SUBJECT_VAR, "value": subject or ""},
                         {"name": BODY_VAR, "value": body or ""},
                     ]}]})
            if "error" in upsert:
                status = (SendStatus.THROTTLED
                          if upsert.get("http_status") == 429
                          else SendStatus.FAILED)
                return SendResult(status=status,
                                  error=f"lead upsert failed: "
                                        f"{upsert['error']}")
            leads = upsert.get("data")
            lead_id = None
            if (isinstance(leads, list) and leads
                    and isinstance(leads[0], dict)):
                lead_id = leads[0].get("id")
            if not lead_id:
                return SendResult(
                    status=SendStatus.FAILED,
                    error="lead upsert returned no lead id (personal "
                          "domains like gmail.com are skipped unless "
                          "enabled on your EmailBison instance)",
                    raw=upsert)
            attach = _request(
                member_email, "POST",
                f"{base}/api/campaigns/{quote(campaign_id, safe='')}"
                f"/leads/attach-leads", api_key,
                {"lead_ids": [lead_id]})
            if "error" in attach:
                status = (SendStatus.THROTTLED
                          if attach.get("http_status") == 429
                          else SendStatus.FAILED)
                return SendResult(status=status,
                                  error=f"attach to campaign "
                                        f"{campaign_id} failed: "
                                        f"{attach['error']}")
            return SendResult(
                status=SendStatus.SENT,
                external_id=str(lead_id),
                raw={"lead_id": lead_id,
                     "campaign_id": campaign_id,
                     "attach": attach.get("data") or {},
                     "note": "queued into the EmailBison campaign — "
                             "the platform owns delivery; leads sync "
                             "to an active campaign within ~5 min"})
        except Exception as e:
            return SendResult(status=SendStatus.FAILED,
                              error=f"{type(e).__name__}: {e}")

    def detect_replies(self, member_email: str,
                       since: datetime) -> list[Reply]:
        """Page the campaign's inbox folder newest-first and keep
        replies with date_received >= since. EmailBison does not
        expose the RFC Message-ID of the original send, so
        in_reply_to_message_id carries the EmailBison reply id
        (noted in raw) — downstream matches on lead email instead."""
        out: list[Reply] = []
        try:
            cred = _cred(member_email)
            if not cred:
                return out
            base = _base_url(cred)
            campaign_id = str(cred.get("campaign_id") or "").strip()
            if not base or not campaign_id:
                return out
            api_key = (cred.get("api_key") or "").strip()
            if since.tzinfo is not None:
                since = since.astimezone(timezone.utc).replace(tzinfo=None)
            for page in range(1, EMAILBISON_MAX_REPLY_PAGES + 1):
                q = urlencode({"folder": "inbox",
                               "campaign_id": campaign_id,
                               "page": page})
                resp = _request(member_email, "GET",
                                f"{base}/api/replies?{q}", api_key)
                if "error" in resp:
                    break
                rows = resp.get("data") or []
                if not rows:
                    break
                page_had_fresh = False
                for r in rows:
                    if not isinstance(r, dict):
                        continue
                    received = _parse_iso(r.get("date_received") or "")
                    if received is None or received < since:
                        continue
                    page_had_fresh = True
                    body = (r.get("text_body")
                            or _strip_html(r.get("html_body") or ""))
                    lead = r.get("lead") or {}
                    out.append(Reply(
                        in_reply_to_message_id=str(r.get("id") or ""),
                        from_addr=str(r.get("from_email_address")
                                      or lead.get("email") or ""),
                        subject=str(r.get("subject") or ""),
                        body=body[:5000],
                        received_at=(received.isoformat() + "Z"),
                        thread_id=str(r.get("scheduled_email_id") or ""),
                        raw={"reply_id": r.get("id"),
                             "uuid": r.get("uuid"),
                             "campaign_id": r.get("campaign_id"),
                             "lead_id": r.get("lead_id"),
                             "lead_email": lead.get("email"),
                             "raw_message_id": r.get("raw_message_id"),
                             "interested": r.get("interested"),
                             "automated_reply": r.get("automated_reply"),
                             "note": "in_reply_to_message_id is the "
                                     "EmailBison reply id; "
                                     "raw_message_id is the RFC "
                                     "Message-ID of the reply itself — "
                                     "the original send's Message-ID "
                                     "is not exposed"}))
                # Inbox is newest-first: a page with zero fresh replies
                # means everything older is stale too — stop paging.
                if not page_had_fresh:
                    break
                if not (resp.get("links") or {}).get("next"):
                    break
        except Exception as e:
            print(f"[narada/emailbison] detect_replies failed: "
                  f"{type(e).__name__}: {e}", flush=True)
        return out


# Auto-register
try:
    register(EmailBisonSender())
except Exception as _e:
    print(f"[narada/emailbison] register failed: "
          f"{type(_e).__name__}: {_e}", flush=True)
