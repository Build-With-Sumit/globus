"""Freshsales CRM plugin — pipes Narada hot replies into the marketer's
Freshsales workspace as Contacts + Deals + Activities.

Auth: API key + subdomain (Freshsales uses workspace-scoped subdomains
like https://yourco.myfreshworks.com). Marketer pastes both into
/members/narada/credentials.

This is Sumit's CRM. Per memory `feedback_freshsales_api_include_required`,
single-contact GET silently returns null for contact_status_id +
owner_id without ?include=contact_status,owner — we always pass include
on reads to avoid that trap.
"""
from __future__ import annotations
import json
from urllib.error import HTTPError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from narada_creds import get_credential, has_credential, touch_last_used
from narada_plugins import register
from narada_plugins.types import (
    Activity, AuthMethod, DealData, Lead,
    PluginCategory, PluginInfo,
)


FRESHSALES_TIMEOUT = 30


def _creds(member_email: str) -> tuple[str, str] | None:
    """Return (api_key, subdomain) or None if not configured."""
    c = get_credential(member_email, "freshsales")
    if not c:
        return None
    api_key = (c.get("api_key") or "").strip()
    subdomain = (c.get("subdomain") or "").strip()
    if not api_key or not subdomain:
        return None
    return api_key, subdomain


def _api(member_email: str, method: str, path: str,
          body: dict | None = None) -> dict:
    """Call Freshsales REST API. Returns parsed JSON, or {'error': ...}.
    Path is the part after the base URL (e.g. '/api/contacts')."""
    creds = _creds(member_email)
    if not creds:
        return {"error": "no Freshsales credential for this member "
                          "(api_key + subdomain required)"}
    api_key, subdomain = creds
    base = f"https://{subdomain}.myfreshworks.com/crm/sales"
    url = f"{base}{path}"
    data = json.dumps(body).encode("utf-8") if body else None
    req = Request(url, data=data, method=method,
                  headers={
                      "Authorization": f"Token token={api_key}",
                      "Content-Type": "application/json",
                  })
    try:
        with urlopen(req, timeout=FRESHSALES_TIMEOUT) as r:
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
        touch_last_used(member_email, "freshsales")


# ─────────────────────────────────────────────────────────────────────
# CRM implementation
# ─────────────────────────────────────────────────────────────────────

class FreshsalesCRM:

    @classmethod
    def info(cls) -> PluginInfo:
        return PluginInfo(
            name="freshsales",
            display_name="Freshsales",
            category=PluginCategory.CRM,
            auth_method=AuthMethod.API_KEY,
            requires_credentials=["api_key", "subdomain"],
            homepage="https://www.freshworks.com/crm/",
            docs_url="https://developers.freshworks.com/crm/api/",
            description=(
                "Sumit's CRM. Pipes Narada hot replies into Freshsales "
                "as Contacts + Deals + Activities. Per-member-scoped "
                "credentials; each marketer brings their own workspace."
            ),
        )

    def is_available(self, member_email: str) -> bool:
        return has_credential(member_email, "freshsales")

    def upsert_contact(self, member_email: str, lead: Lead) -> str:
        """Upsert a contact by email. Returns the Freshsales contact id."""
        if not lead.email:
            return ""
        # Search first (dedup by email). Freshsales has POST
        # /api/search?q=<query> for free-text + scoped object search.
        q = quote(f"{lead.email}")
        existing = _api(member_email, "GET",
                         f"/api/search?include=contact&q={q}")
        if existing and not existing.get("error"):
            for hit in (existing if isinstance(existing, list) else []):
                if hit.get("type") == "contact":
                    return str(hit.get("id") or "")
        # Create
        body = {"contact": {
            "first_name": lead.first_name or "",
            "last_name": lead.last_name or "",
            "email": lead.email,
            "job_title": lead.title or "",
            "company": {"name": lead.company or ""}
                          if lead.company else None,
        }}
        # Clean None values
        body["contact"] = {k: v for k, v in body["contact"].items()
                            if v not in (None, "", [])}
        if not body["contact"]:
            return ""
        resp = _api(member_email, "POST", "/api/contacts", body)
        if "error" in resp:
            print(f"[narada/freshsales] upsert_contact failed: "
                  f"{resp['error']}", flush=True)
            return ""
        return str((resp.get("contact") or {}).get("id") or "")

    def create_deal(self, member_email: str, contact_id: str,
                     deal: DealData) -> str:
        """Create a deal attached to the contact. Maps DealData →
        Freshsales' shape. Status / sales_account need to come from
        marketer-configured custom fields long-term; v1 sets the basics."""
        if not (contact_id and deal.title):
            return ""
        body = {"deal": {
            "name": deal.title,
            "amount": deal.value or 0,
            "currency": deal.currency or "USD",
            "expected_close": deal.close_date or None,
            "contacts_added_list": [contact_id],
        }}
        body["deal"] = {k: v for k, v in body["deal"].items()
                          if v not in (None, "", [])}
        resp = _api(member_email, "POST", "/api/deals", body)
        if "error" in resp:
            print(f"[narada/freshsales] create_deal failed: "
                  f"{resp['error']}", flush=True)
            return ""
        return str((resp.get("deal") or {}).get("id") or "")

    def log_activity(self, member_email: str, contact_id: str,
                      activity: Activity) -> None:
        """Log a note against the contact. Freshsales' notes endpoint
        accepts {description, targetable_id, targetable_type}."""
        if not contact_id:
            return
        body = {"note": {
            "description": (
                f"[Narada {activity.type}] {activity.subject}\n\n"
                f"{activity.body}"
            )[:5000],
            "targetable_id": contact_id,
            "targetable_type": "Contact",
        }}
        resp = _api(member_email, "POST", "/api/notes", body)
        if "error" in resp:
            print(f"[narada/freshsales] log_activity failed: "
                  f"{resp['error']}", flush=True)


# Auto-register
try:
    register(FreshsalesCRM())
except Exception as _e:
    print(f"[narada/freshsales] register failed: "
          f"{type(_e).__name__}: {_e}", flush=True)
