"""Close (close.com) plugin — implements the CRM protocol (direct API).

Close is a sales CRM built around **Leads** (company/account objects)
that contain **Contacts**. That's the opposite shape from the
contact-centric CRM protocol, so this plugin maps the protocol's
`contact_id` to Close's **lead id**: `upsert_contact` creates/returns a
Lead (with the contact embedded), and `create_deal` / `log_activity`
attach to that lead id (Close opportunities + notes hang off the lead,
not the contact). The id is opaque to the Narada core, so this is safe.

Auth: HTTP Basic — API key as the username, empty password.
Base https://api.close.com/api/v1.
Docs: https://developer.close.com/
Slug `close`; paste the API key as `api_key`.
"""
from __future__ import annotations
import base64
import json
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from narada_creds import get_credential, has_credential, touch_last_used
from narada_plugins import register
from narada_plugins.types import (
    Activity, AuthMethod, DealData, Lead, PluginCategory, PluginInfo,
)


CLOSE_API_BASE = "https://api.close.com/api/v1"
CLOSE_TIMEOUT = 30


def _api_key(member_email: str) -> str | None:
    cred = get_credential(member_email, "close")
    if not cred:
        return None
    return (cred.get("api_key") or "").strip() or None


def _api(member_email: str, method: str, path: str,
         body: dict | None = None, params: dict | None = None) -> dict:
    """Call Close's REST API. Returns parsed JSON, or {'error': ...}.
    Never raises. Basic auth = api_key as username, blank password."""
    api_key = _api_key(member_email)
    if not api_key:
        return {"error": "no Close credential for this member"}
    url = f"{CLOSE_API_BASE}{path}"
    if params:
        url += "?" + urlencode(params)
    token = base64.b64encode(f"{api_key}:".encode()).decode()
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = Request(url, data=data, method=method, headers={
        "Authorization": f"Basic {token}",
        "Content-Type": "application/json",
    })
    try:
        with urlopen(req, timeout=CLOSE_TIMEOUT) as r:
            text = r.read().decode("utf-8")
            return json.loads(text) if text else {}
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
        touch_last_used(member_email, "close")


class CloseCRM:

    @classmethod
    def info(cls) -> PluginInfo:
        return PluginInfo(
            name="close",
            display_name="Close",
            category=PluginCategory.CRM,
            auth_method=AuthMethod.API_KEY,
            requires_credentials=["api_key"],
            homepage="https://close.com",
            docs_url="https://developer.close.com/",
            description=(
                "Sales CRM (lead-centric). Narada hot replies become Close "
                "Leads + Contacts, with Opportunities on 'interested' and "
                "Notes for each touch. Paste your Close API key."
            ),
        )

    def is_available(self, member_email: str) -> bool:
        return has_credential(member_email, "close")

    def upsert_contact(self, member_email: str, lead: Lead) -> str:
        """Dedup by email via Close's lead query; else create a Lead with
        the contact embedded. Returns the Close **lead** id."""
        if not lead.email:
            return ""
        # Dedup — Close smart-query on the contact email.
        found = _api(member_email, "GET", "/lead/", params={
            "query": f'email_address:"{lead.email}"',
            "_fields": "id",
            "_limit": 1,
        })
        if not found.get("error"):
            data = found.get("data") or []
            if data:
                return str(data[0].get("id") or "")
        # Create lead + embedded contact.
        contact = {
            "name": f"{lead.first_name} {lead.last_name}".strip()
                    or lead.email,
            "title": lead.title or "",
            "emails": [{"email": lead.email, "type": "office"}],
        }
        contact = {k: v for k, v in contact.items() if v}
        body = {
            "name": lead.company or f"{lead.first_name} {lead.last_name}".strip()
                    or lead.email,
            "contacts": [contact],
        }
        if lead.company_domain:
            body["url"] = lead.company_domain
        resp = _api(member_email, "POST", "/lead/", body)
        if "error" in resp:
            print(f"[narada/close] upsert_contact failed: {resp['error']}",
                  flush=True)
            return ""
        return str(resp.get("id") or "")

    def create_deal(self, member_email: str, contact_id: str,
                    deal: DealData) -> str:
        """Create an Opportunity on the lead (contact_id == Close lead id)."""
        if not (contact_id and deal.title):
            return ""
        body = {
            "lead_id": contact_id,
            "note": deal.title,
            "value": int((deal.value or 0) * 100),   # Close stores cents
            "value_period": "one_time",
            "confidence": 50,
        }
        if deal.close_date:
            body["date_won"] = deal.close_date
        resp = _api(member_email, "POST", "/opportunity/", body)
        if "error" in resp:
            print(f"[narada/close] create_deal failed: {resp['error']}",
                  flush=True)
            return ""
        return str(resp.get("id") or "")

    def log_activity(self, member_email: str, contact_id: str,
                     activity: Activity) -> None:
        """Attach a Note to the lead (contact_id == Close lead id)."""
        if not contact_id:
            return
        note = (f"[Narada {activity.type}] {activity.subject}\n\n"
                f"{activity.body}")[:5000]
        resp = _api(member_email, "POST", "/activity/note/", {
            "lead_id": contact_id,
            "note": note,
        })
        if "error" in resp:
            print(f"[narada/close] log_activity failed: {resp['error']}",
                  flush=True)


# Auto-register
try:
    register(CloseCRM())
except Exception as _e:
    print(f"[narada/close] register failed: "
          f"{type(_e).__name__}: {_e}", flush=True)
