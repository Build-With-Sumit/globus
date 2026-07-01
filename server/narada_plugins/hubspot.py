"""HubSpot plugin — implements the CRM protocol (direct API).

Pipes Narada hot replies into the marketer's HubSpot as Contacts +
Deals + Notes. The marketer provides a **Private App access token**
(prefix `pat-...` or `na2-...`) with `crm.objects.contacts.write`,
`crm.objects.deals.write`, and `crm.objects.notes` scopes.

NOTE: HubSpot can also be connected via Composio (OAuth). This plugin
is the direct-token path — simplest when the member already has a
Private App token. Slug `hubspot`; paste the token as `api_key`.

Auth: Bearer <token>. Base https://api.hubapi.com.
Docs: https://developers.hubspot.com/docs/api/crm/contacts
Association type ids (HUBSPOT_DEFINED): deal→contact = 3, note→contact = 202.
Free tier: HubSpot CRM is free; API included.
"""
from __future__ import annotations
import json
import time
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from narada_creds import get_credential, has_credential, touch_last_used
from narada_plugins import register
from narada_plugins.types import (
    Activity, AuthMethod, DealData, Lead, PluginCategory, PluginInfo,
)


HUBSPOT_API_BASE = "https://api.hubapi.com"
HUBSPOT_TIMEOUT = 30


def _token(member_email: str) -> str | None:
    cred = get_credential(member_email, "hubspot")
    if not cred:
        return None
    return (cred.get("api_key") or cred.get("access_token") or "").strip() or None


def _api(member_email: str, method: str, path: str,
         body: dict | None = None) -> dict:
    """Call HubSpot's CRM v3 API. Returns parsed JSON, or {'error': ...}.
    Never raises."""
    token = _token(member_email)
    if not token:
        return {"error": "no HubSpot credential for this member"}
    url = f"{HUBSPOT_API_BASE}{path}"
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = Request(url, data=data, method=method, headers={
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    })
    try:
        with urlopen(req, timeout=HUBSPOT_TIMEOUT) as r:
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
        touch_last_used(member_email, "hubspot")


class HubSpotCRM:

    @classmethod
    def info(cls) -> PluginInfo:
        return PluginInfo(
            name="hubspot",
            display_name="HubSpot",
            category=PluginCategory.CRM,
            auth_method=AuthMethod.API_KEY,
            requires_credentials=["api_key"],
            homepage="https://www.hubspot.com/products/crm",
            docs_url="https://developers.hubspot.com/docs/api/crm/contacts",
            description=(
                "Pipe Narada hot replies into HubSpot as Contacts + Deals "
                "+ Notes. Paste a Private App access token (pat-/na2- prefix) "
                "with contacts/deals/notes write scopes. HubSpot CRM is free."
            ),
            free_tier=True,
        )

    def is_available(self, member_email: str) -> bool:
        return has_credential(member_email, "hubspot")

    def upsert_contact(self, member_email: str, lead: Lead) -> str:
        """Upsert by email (dedup). Returns the HubSpot contact id."""
        if not lead.email:
            return ""
        # Search by email first.
        search = _api(member_email, "POST",
                      "/crm/v3/objects/contacts/search", {
                          "filterGroups": [{"filters": [{
                              "propertyName": "email",
                              "operator": "EQ",
                              "value": lead.email,
                          }]}],
                          "properties": ["email"],
                          "limit": 1,
                      })
        if not search.get("error"):
            results = search.get("results") or []
            if results:
                return str(results[0].get("id") or "")
        # Create.
        props = {
            "email": lead.email,
            "firstname": lead.first_name or "",
            "lastname": lead.last_name or "",
            "jobtitle": lead.title or "",
            "company": lead.company or "",
            "website": lead.company_domain or "",
        }
        props = {k: v for k, v in props.items() if v}
        resp = _api(member_email, "POST", "/crm/v3/objects/contacts",
                    {"properties": props})
        if "error" in resp:
            print(f"[narada/hubspot] upsert_contact failed: {resp['error']}",
                  flush=True)
            # A 409 conflict means the contact exists — try to recover its id.
            return ""
        return str(resp.get("id") or "")

    def create_deal(self, member_email: str, contact_id: str,
                    deal: DealData) -> str:
        """Create a deal associated to the contact (assoc type 3)."""
        if not (contact_id and deal.title):
            return ""
        props = {"dealname": deal.title}
        if deal.value:
            props["amount"] = str(deal.value)
        if deal.stage:
            props["dealstage"] = deal.stage
        if deal.close_date:
            props["closedate"] = deal.close_date
        body = {
            "properties": props,
            "associations": [{
                "to": {"id": contact_id},
                "types": [{
                    "associationCategory": "HUBSPOT_DEFINED",
                    "associationTypeId": 3,   # deal → contact
                }],
            }],
        }
        resp = _api(member_email, "POST", "/crm/v3/objects/deals", body)
        if "error" in resp:
            print(f"[narada/hubspot] create_deal failed: {resp['error']}",
                  flush=True)
            return ""
        return str(resp.get("id") or "")

    def log_activity(self, member_email: str, contact_id: str,
                     activity: Activity) -> None:
        """Log a Note against the contact (assoc type 202). hs_timestamp
        is required (ms epoch)."""
        if not contact_id:
            return
        body_text = (f"[Narada {activity.type}] {activity.subject}\n\n"
                     f"{activity.body}")[:65000]
        body = {
            "properties": {
                "hs_note_body": body_text,
                "hs_timestamp": str(int(time.time() * 1000)),
            },
            "associations": [{
                "to": {"id": contact_id},
                "types": [{
                    "associationCategory": "HUBSPOT_DEFINED",
                    "associationTypeId": 202,   # note → contact
                }],
            }],
        }
        resp = _api(member_email, "POST", "/crm/v3/objects/notes", body)
        if "error" in resp:
            print(f"[narada/hubspot] log_activity failed: {resp['error']}",
                  flush=True)


# Auto-register
try:
    register(HubSpotCRM())
except Exception as _e:
    print(f"[narada/hubspot] register failed: "
          f"{type(_e).__name__}: {_e}", flush=True)
