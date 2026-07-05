"""Attio plugin — implements the CRM protocol (direct API).

Pipes Narada hot replies into the marketer's Attio workspace as People
+ Deals + Notes. The marketer provides a **workspace API key** (created
at Workspace settings → Developers → API keys) with record read-write,
note read-write and user-management read scopes.

Slug `attio`; paste the key as `api_key`.

Auth: Bearer <api_key>. Base https://api.attio.com/v2.
Docs: https://docs.attio.com/rest-api/overview
People upsert is native: PUT /v2/objects/people/records
?matching_attribute=email_addresses (Attio "assert" = dedup by email
in one call). Deals need the Deals object ENABLED in the workspace
(off by default — Settings → Objects) and by default require a deal
owner; we pick the workspace member matching the Narada member's
email, else the first admin, else attempt the create without an
owner. DealData.close_date has no default Attio attribute
and is not synced. Free tier: Attio's Free plan includes API access.
"""
from __future__ import annotations
import json
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from narada_creds import get_credential, has_credential, touch_last_used
from narada_plugins import register
from narada_plugins.types import (
    Activity, AuthMethod, DealData, Lead, PluginCategory, PluginInfo,
)


ATTIO_API_BASE = "https://api.attio.com/v2"
ATTIO_TIMEOUT = 30
ATTIO_USER_AGENT = "Narada/1.0 (outbound-marketing agent)"


def _token(member_email: str) -> str | None:
    cred = get_credential(member_email, "attio")
    if not cred:
        return None
    return (cred.get("api_key") or cred.get("access_token") or "").strip() or None


def _api(member_email: str, method: str, path: str,
         body: dict | None = None) -> dict:
    """Call Attio's v2 API. Returns parsed JSON, or {'error': ...}.
    Never raises."""
    token = _token(member_email)
    if not token:
        return {"error": "no Attio credential for this member"}
    url = f"{ATTIO_API_BASE}{path}"
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = Request(url, data=data, method=method, headers={
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "User-Agent": ATTIO_USER_AGENT,
    })
    try:
        with urlopen(req, timeout=ATTIO_TIMEOUT) as r:
            text = r.read().decode("utf-8")
            parsed = json.loads(text) if text else {}
            return parsed if isinstance(parsed, dict) else {"data": parsed}
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
        touch_last_used(member_email, "attio")


def _record_id(resp: dict) -> str:
    """Pull data.id.record_id out of a record response. Never raises."""
    try:
        return str(((resp.get("data") or {}).get("id") or {})
                   .get("record_id") or "")
    except Exception:
        return ""


class AttioCRM:

    @classmethod
    def info(cls) -> PluginInfo:
        return PluginInfo(
            name="attio",
            display_name="Attio",
            category=PluginCategory.CRM,
            auth_method=AuthMethod.API_KEY,
            requires_credentials=["api_key"],
            homepage="https://attio.com",
            docs_url="https://docs.attio.com/rest-api/overview",
            description=(
                "Pipe Narada hot replies into Attio as People + Deals "
                "+ Notes. Paste a workspace API key (Workspace settings "
                "→ Developers) with record + note write scopes. Deals "
                "sync needs the Deals object enabled in your workspace. "
                "Attio's Free plan includes API access."
            ),
            free_tier=True,
        )

    def is_available(self, member_email: str) -> bool:
        return has_credential(member_email, "attio")

    def upsert_contact(self, member_email: str, lead: Lead) -> str:
        """Native assert by email (dedup). Returns the Attio record id."""
        if not lead.email:
            return ""
        values: dict = {
            "email_addresses": [{"email_address": lead.email}],
        }
        first = (lead.first_name or "").strip()
        last = (lead.last_name or "").strip()
        if first or last:
            values["name"] = [{
                "first_name": first,
                "last_name": last,
                "full_name": f"{first} {last}".strip(),
            }]
        if lead.title:
            values["job_title"] = [{"value": lead.title}]
        if lead.linkedin_url and lead.linkedin_url.startswith("http"):
            values["linkedin"] = [{"value": lead.linkedin_url}]
        if lead.company_domain:
            values["company"] = [{
                "target_object": "companies",
                "domains": [{"domain": lead.company_domain}],
            }]
        resp = _api(member_email, "PUT",
                    "/objects/people/records"
                    "?matching_attribute=email_addresses",
                    {"data": {"values": values}})
        if "error" in resp and ("company" in values or "linkedin" in values):
            # A bad company domain / LinkedIn URL fails the whole assert —
            # retry with the core identity fields only.
            values.pop("company", None)
            values.pop("linkedin", None)
            resp = _api(member_email, "PUT",
                        "/objects/people/records"
                        "?matching_attribute=email_addresses",
                        {"data": {"values": values}})
        if "error" in resp:
            print(f"[narada/attio] upsert_contact failed: {resp['error']}",
                  flush=True)
            return ""
        return _record_id(resp)

    def create_deal(self, member_email: str, contact_id: str,
                    deal: DealData) -> str:
        """Create a deal attached to the person. Attio's default Deals
        config requires a deal owner (actor reference) — we pick the
        workspace member matching the Narada member's email, else the
        first admin. If the lookup fails (e.g. the API key is missing
        the user-management read scope) we still attempt the create
        without an owner: some workspaces relax the requirement, and
        otherwise Attio's validation error lands in the log."""
        if not (contact_id and deal.title):
            return ""
        values: dict = {
            "name": [{"value": deal.title}],
            "stage": [{"status": deal.stage or "Lead"}],
            "associated_people": [{
                "target_object": "people",
                "target_record_id": contact_id,
            }],
        }
        owner_id = self._owner_actor_id(member_email)
        if owner_id:
            values["owner"] = [{
                "referenced_actor_type": "workspace-member",
                "referenced_actor_id": owner_id,
            }]
        else:
            print("[narada/attio] create_deal: no deal owner resolved "
                  "(API key missing user_management:read scope?) — "
                  "trying without owner", flush=True)
        if deal.value:
            # Attio currency values allow at most 4 decimal places.
            values["value"] = [{"currency_value": round(deal.value, 4)}]
        resp = _api(member_email, "POST", "/objects/deals/records",
                    {"data": {"values": values}})
        if "error" in resp and deal.stage and deal.stage != "Lead":
            # Stage titles are workspace-configured — retry with the
            # default "Lead" if the caller's stage doesn't exist.
            values["stage"] = [{"status": "Lead"}]
            resp = _api(member_email, "POST", "/objects/deals/records",
                        {"data": {"values": values}})
        if "error" in resp:
            print(f"[narada/attio] create_deal failed: {resp['error']}",
                  flush=True)
            return ""
        return _record_id(resp)

    def log_activity(self, member_email: str, contact_id: str,
                     activity: Activity) -> None:
        """Log a plaintext Note against the person. Best-effort."""
        if not contact_id:
            return
        title = (f"[Narada {activity.type}] {activity.subject}"
                 .strip())[:200]
        body = {
            "data": {
                "parent_object": "people",
                "parent_record_id": contact_id,
                "title": title or f"[Narada {activity.type}]",
                "format": "plaintext",
                "content": (activity.body or activity.subject or "")[:65000],
            },
        }
        resp = _api(member_email, "POST", "/notes", body)
        if "error" in resp:
            print(f"[narada/attio] log_activity failed: {resp['error']}",
                  flush=True)

    # ── helpers ──────────────────────────────────────────────────────

    def _owner_actor_id(self, member_email: str) -> str:
        """Pick a workspace member to own deals: exact email match with
        the Narada member first, else the first admin, else the first
        non-suspended member. Returns "" if none found. Never raises."""
        resp = _api(member_email, "GET", "/workspace_members")
        if "error" in resp:
            print(f"[narada/attio] list workspace members failed: "
                  f"{resp['error']}", flush=True)
            return ""
        members = resp.get("data") or []
        if not isinstance(members, list):
            return ""
        match_id = admin_id = active_id = ""
        for m in members:
            if not isinstance(m, dict):
                continue
            mid = str(((m.get("id") or {}).get("workspace_member_id"))
                      or "")
            if not mid:
                continue
            level = (m.get("access_level") or "").lower()
            if level == "suspended":
                continue
            email = (m.get("email_address") or "").strip().lower()
            if email and email == member_email.strip().lower():
                match_id = match_id or mid
            if level == "admin":
                admin_id = admin_id or mid
            active_id = active_id or mid
        return match_id or admin_id or active_id


# Auto-register
try:
    register(AttioCRM())
except Exception as _e:
    print(f"[narada/attio] register failed: "
          f"{type(_e).__name__}: {_e}", flush=True)
