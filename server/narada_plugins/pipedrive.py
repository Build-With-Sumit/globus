"""Pipedrive plugin — implements the CRM protocol (direct API).

Pipedrive is a contact/deal CRM — a clean fit for the protocol:
Persons ↔ contacts, Deals ↔ deals, Notes ↔ activities.

Auth: `?api_token=<key>` query param (Pipedrive's classic API token).
Base https://api.pipedrive.com/v1. Responses are wrapped:
`{success: bool, data: {...}}` — we unwrap `data`.
Docs: https://developers.pipedrive.com/docs/api/v1
Slug `pipedrive`; paste the API token as `api_key`.
"""
from __future__ import annotations
import json
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from narada_creds import get_credential, has_credential, touch_last_used
from narada_plugins import register
from narada_plugins.types import (
    Activity, AuthMethod, DealData, Lead, PluginCategory, PluginInfo,
)


PIPEDRIVE_API_BASE = "https://api.pipedrive.com/v1"
PIPEDRIVE_TIMEOUT = 30


def _api_key(member_email: str) -> str | None:
    cred = get_credential(member_email, "pipedrive")
    if not cred:
        return None
    return (cred.get("api_key") or "").strip() or None


def _api(member_email: str, method: str, path: str,
         body: dict | None = None, params: dict | None = None) -> dict:
    """Call Pipedrive's REST API. Returns the unwrapped `data` payload
    ({} or a dict/list), or {'error': ...}. Never raises."""
    api_key = _api_key(member_email)
    if not api_key:
        return {"error": "no Pipedrive credential for this member"}
    qp = dict(params or {})
    qp["api_token"] = api_key
    url = f"{PIPEDRIVE_API_BASE}{path}?{urlencode(qp)}"
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = Request(url, data=data, method=method,
                  headers={"Content-Type": "application/json"})
    try:
        with urlopen(req, timeout=PIPEDRIVE_TIMEOUT) as r:
            text = r.read().decode("utf-8")
            parsed = json.loads(text) if text else {}
    except HTTPError as e:
        body_txt = ""
        try:
            body_txt = e.read().decode("utf-8", "replace")[:300]
        except Exception:
            pass
        return {"error": f"HTTP {e.code}: {body_txt}"}
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}
    finally:
        touch_last_used(member_email, "pipedrive")
    if not parsed.get("success", True):
        return {"error": parsed.get("error") or "pipedrive: success=false"}
    return {"data": parsed.get("data")}


class PipedriveCRM:

    @classmethod
    def info(cls) -> PluginInfo:
        return PluginInfo(
            name="pipedrive",
            display_name="Pipedrive",
            category=PluginCategory.CRM,
            auth_method=AuthMethod.API_KEY,
            requires_credentials=["api_key"],
            homepage="https://www.pipedrive.com",
            docs_url="https://developers.pipedrive.com/docs/api/v1",
            description=(
                "Contact/deal CRM. Narada hot replies become Persons + "
                "Deals + Notes. Paste your Pipedrive API token "
                "(Settings → Personal preferences → API)."
            ),
        )

    def is_available(self, member_email: str) -> bool:
        return has_credential(member_email, "pipedrive")

    def upsert_contact(self, member_email: str, lead: Lead) -> str:
        """Dedup by email via persons/search; else create. Returns id."""
        if not lead.email:
            return ""
        found = _api(member_email, "GET", "/persons/search", params={
            "term": lead.email, "fields": "email", "exact_match": "true",
            "limit": 1,
        })
        if not found.get("error"):
            items = ((found.get("data") or {}).get("items")
                     if isinstance(found.get("data"), dict) else None) or []
            if items:
                return str((items[0].get("item") or {}).get("id") or "")
        name = f"{lead.first_name} {lead.last_name}".strip() or lead.email
        body = {
            "name": name,
            "email": [{"value": lead.email, "primary": True, "label": "work"}],
        }
        if lead.title:
            body["job_title"] = lead.title
        resp = _api(member_email, "POST", "/persons", body)
        if "error" in resp:
            print(f"[narada/pipedrive] upsert_contact failed: {resp['error']}",
                  flush=True)
            return ""
        return str((resp.get("data") or {}).get("id") or "")

    def create_deal(self, member_email: str, contact_id: str,
                    deal: DealData) -> str:
        if not (contact_id and deal.title):
            return ""
        body = {
            "title": deal.title,
            "person_id": contact_id,
            "value": deal.value or 0,
            "currency": deal.currency or "USD",
        }
        if deal.close_date:
            body["expected_close_date"] = deal.close_date
        resp = _api(member_email, "POST", "/deals", body)
        if "error" in resp:
            print(f"[narada/pipedrive] create_deal failed: {resp['error']}",
                  flush=True)
            return ""
        return str((resp.get("data") or {}).get("id") or "")

    def log_activity(self, member_email: str, contact_id: str,
                     activity: Activity) -> None:
        if not contact_id:
            return
        content = (f"[Narada {activity.type}] {activity.subject}\n\n"
                   f"{activity.body}")[:65000]
        resp = _api(member_email, "POST", "/notes", {
            "content": content,
            "person_id": contact_id,
        })
        if "error" in resp:
            print(f"[narada/pipedrive] log_activity failed: {resp['error']}",
                  flush=True)


# Auto-register
try:
    register(PipedriveCRM())
except Exception as _e:
    print(f"[narada/pipedrive] register failed: "
          f"{type(_e).__name__}: {_e}", flush=True)
