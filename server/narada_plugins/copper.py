"""Copper plugin — implements the CRM protocol (direct API).

Pipes Narada hot replies into the marketer's Copper (formerly
ProsperWorks — the CRM that lives inside Google Workspace) as People
+ Opportunities + Note activities. The marketer provides their
**API key** (Copper → Settings → Integrations → API Keys) AND the
**email address of the Copper user the key belongs to** — Copper
requires both on every request.

Auth: header-based — `X-PW-AccessToken: <api key>`,
`X-PW-UserEmail: <owner email>`, `X-PW-Application: developer_api`.
Base https://api.copper.com/developer_api/v1.
Docs: https://developer.copper.com/
Slug `copper`; credential fields: `api_key`, `user_email`.

Dedup: email is a unique People key in Copper, so upsert goes
POST /people/fetch_by_email (404 = not found) then POST /people.
Deals are Opportunities (`name` + `primary_contact_id` required,
close_date is MM/DD/YYYY). Notes are Activities with the hard-coded
type {"category": "user", "id": 0}.
Free tier: none — Copper is paid-only (14-day trial).
"""
from __future__ import annotations
import json
import time
from datetime import datetime
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from narada_creds import get_credential, has_credential, touch_last_used
from narada_plugins import register
from narada_plugins.types import (
    Activity, AuthMethod, DealData, Lead, PluginCategory, PluginInfo,
)


COPPER_API_BASE = "https://api.copper.com/developer_api/v1"
COPPER_TIMEOUT = 30
USER_AGENT = "Narada/1.0 (outbound agent; +https://globussoft.ai)"


def _creds(member_email: str) -> tuple[str, str] | None:
    """(api_key, user_email) or None. Copper needs both headers."""
    cred = get_credential(member_email, "copper")
    if not cred:
        return None
    api_key = (cred.get("api_key") or "").strip()
    user_email = (cred.get("user_email") or cred.get("email") or "").strip()
    if not (api_key and user_email):
        return None
    return api_key, user_email


def _api(member_email: str, method: str, path: str,
         body: dict | None = None) -> dict | list:
    """Call Copper's developer API v1. Returns parsed JSON (dict, or
    list for e.g. GET /pipelines), or {'error': ..., 'status': int}.
    Never raises."""
    creds = _creds(member_email)
    if not creds:
        return {"error": "no Copper credential (api_key + user_email) "
                         "for this member"}
    api_key, user_email = creds
    url = f"{COPPER_API_BASE}{path}"
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = Request(url, data=data, method=method, headers={
        "X-PW-AccessToken": api_key,
        "X-PW-UserEmail": user_email,
        "X-PW-Application": "developer_api",
        "Content-Type": "application/json",
        "User-Agent": USER_AGENT,
    })
    try:
        with urlopen(req, timeout=COPPER_TIMEOUT) as r:
            text = r.read().decode("utf-8")
            return json.loads(text) if text else {}
    except HTTPError as e:
        body_txt = ""
        try:
            body_txt = e.read().decode("utf-8", "replace")[:300]
        except Exception:
            pass
        return {"error": f"HTTP {e.code}: {body_txt}", "status": e.code}
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}
    finally:
        touch_last_used(member_email, "copper")


def _to_int(value: str):
    """Copper ids are integers; credentials/contact ids travel as
    strings through Narada. Best-effort convert, pass through if not."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return value


def _iso_to_mdy(iso_date: str) -> str:
    """Copper close_date wants MM/DD/YYYY; DealData carries ISO."""
    try:
        y, m, d = iso_date[:10].split("-")
        return f"{m}/{d}/{y}"
    except Exception:
        return iso_date


def _epoch(iso_ts: str) -> int:
    """ISO-8601 → unix seconds for activity_date. Now() on failure."""
    try:
        return int(datetime.fromisoformat(
            iso_ts.replace("Z", "+00:00")).timestamp())
    except Exception:
        return int(time.time())


def _resolve_stage(member_email: str,
                   stage: str) -> tuple[int | None, int | None]:
    """Map DealData.stage → (pipeline_id, pipeline_stage_id). Accepts
    a numeric stage id directly, or a stage name matched case-
    insensitively against GET /pipelines. (None, None) if unresolvable
    — Copper then files the opportunity in its default pipeline."""
    if not stage:
        return None, None
    s = stage.strip()
    if s.isdigit():
        return None, int(s)
    pipelines = _api(member_email, "GET", "/pipelines")
    if not isinstance(pipelines, list):
        return None, None
    for pipe in pipelines:
        for st in (pipe.get("stages") or []):
            if str(st.get("name") or "").strip().lower() == s.lower():
                return pipe.get("id"), st.get("id")
    return None, None


class CopperCRM:

    @classmethod
    def info(cls) -> PluginInfo:
        return PluginInfo(
            name="copper",
            display_name="Copper",
            category=PluginCategory.CRM,
            auth_method=AuthMethod.API_KEY,
            requires_credentials=["api_key", "user_email"],
            homepage="https://www.copper.com",
            docs_url="https://developer.copper.com/",
            description=(
                "Pipe Narada hot replies into Copper — the CRM built "
                "for Google Workspace — as People + Opportunities + "
                "Notes. Grab an API key from Settings → Integrations → "
                "API Keys and paste it along with the email of the "
                "Copper user it belongs to (both are required on every "
                "API call)."
            ),
            free_tier=False,
        )

    def is_available(self, member_email: str) -> bool:
        return has_credential(member_email, "copper")

    def upsert_contact(self, member_email: str, lead: Lead) -> str:
        """Upsert by email (dedup — email is a unique People key in
        Copper). Returns the Copper person id."""
        if not lead.email:
            return ""
        # Lookup first: 404 = not found, anything with an id = hit.
        found = _api(member_email, "POST", "/people/fetch_by_email",
                     {"email": lead.email})
        if isinstance(found, dict) and not found.get("error") \
                and found.get("id"):
            return str(found["id"])
        if isinstance(found, dict) and found.get("error") \
                and found.get("status") != 404:
            print(f"[narada/copper] fetch_by_email failed: "
                  f"{found['error']}", flush=True)
            # Fall through and try the create anyway.
        # Create. `name` is required by Copper.
        name = f"{lead.first_name} {lead.last_name}".strip() or lead.email
        body: dict = {
            "name": name,
            "emails": [{"email": lead.email, "category": "work"}],
        }
        if lead.title:
            body["title"] = lead.title
        if lead.company:
            body["company_name"] = lead.company
        if lead.company_domain:
            body["websites"] = [{"url": lead.company_domain,
                                 "category": "work"}]
        if lead.linkedin_url:
            body["socials"] = [{"url": lead.linkedin_url,
                                "category": "linkedin"}]
        resp = _api(member_email, "POST", "/people", body)
        if not isinstance(resp, dict) or resp.get("error"):
            err = resp.get("error") if isinstance(resp, dict) \
                else "unexpected response shape"
            print(f"[narada/copper] upsert_contact failed: {err}",
                  flush=True)
            return ""
        return str(resp.get("id") or "")

    def create_deal(self, member_email: str, contact_id: str,
                    deal: DealData) -> str:
        """Create an Opportunity with the person as primary contact
        (`name` + `primary_contact_id` are Copper's required fields)."""
        if not (contact_id and deal.title):
            return ""
        body: dict = {
            "name": deal.title,
            "primary_contact_id": _to_int(contact_id),
        }
        if deal.value:
            body["monetary_value"] = deal.value
        if deal.close_date:
            body["close_date"] = _iso_to_mdy(deal.close_date)
        pipeline_id, stage_id = _resolve_stage(member_email, deal.stage)
        if pipeline_id:
            body["pipeline_id"] = pipeline_id
        if stage_id:
            body["pipeline_stage_id"] = stage_id
        resp = _api(member_email, "POST", "/opportunities", body)
        if not isinstance(resp, dict) or resp.get("error"):
            err = resp.get("error") if isinstance(resp, dict) \
                else "unexpected response shape"
            print(f"[narada/copper] create_deal failed: {err}",
                  flush=True)
            return ""
        return str(resp.get("id") or "")

    def log_activity(self, member_email: str, contact_id: str,
                     activity: Activity) -> None:
        """Log a Note activity against the person. Copper's Note type
        is hard-coded {"category": "user", "id": 0}."""
        if not contact_id:
            return
        details = (f"[Narada {activity.type}] {activity.subject}\n\n"
                   f"{activity.body}")[:65000]
        body = {
            "parent": {"type": "person", "id": _to_int(contact_id)},
            "type": {"category": "user", "id": 0},   # Note (built-in)
            "details": details,
            "activity_date": _epoch(activity.occurred_at)
            if activity.occurred_at else int(time.time()),
        }
        resp = _api(member_email, "POST", "/activities", body)
        if isinstance(resp, dict) and resp.get("error"):
            print(f"[narada/copper] log_activity failed: "
                  f"{resp['error']}", flush=True)


# Auto-register
try:
    register(CopperCRM())
except Exception as _e:
    print(f"[narada/copper] register failed: "
          f"{type(_e).__name__}: {_e}", flush=True)
