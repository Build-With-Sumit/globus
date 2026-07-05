"""Folk plugin — implements the CRM protocol (direct API).

Pipes Narada hot replies into the marketer's Folk workspace as People +
(optionally) Deals + Notes. The marketer provides a Folk **API key**
(Settings → Developers → API keys) pasted as `api_key`.

Deals in Folk are group-scoped custom objects, created via
`POST /v1/groups/{groupId}/{objectType}`. So `create_deal` only works if
the member also supplies `deals_group_id` (the group that holds their
deals) and, optionally, `deals_object_type` (the object name in that
group config; defaults to "deals"). Without them, `create_deal` is a
no-op — contacts + notes still sync.

Auth: Bearer <api_key>. Base https://api.folk.app.
Docs: https://developer.folk.app/api-reference
People email dedup: GET /v1/people?filter[emails][eq]=<email>.
Notes attach via an `entity` object; deals attach people via `people[]`.
Free tier: Folk API is included on paid Folk plans (no free API tier).
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


FOLK_API_BASE = "https://api.folk.app"
FOLK_TIMEOUT = 30


def _cred(member_email: str) -> dict | None:
    cred = get_credential(member_email, "folk")
    if not cred:
        return None
    if not (cred.get("api_key") or "").strip():
        return None
    return cred


def _api(member_email: str, method: str, path: str,
         body: dict | None = None, params: dict | None = None) -> dict:
    """Call Folk's v1 API. Returns parsed JSON, or {'error': ...}.
    Never raises."""
    cred = _cred(member_email)
    if not cred:
        return {"error": "no Folk credential for this member"}
    token = (cred.get("api_key") or "").strip()
    url = f"{FOLK_API_BASE}{path}"
    if params:
        url += "?" + urlencode(params)
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = Request(url, data=data, method=method, headers={
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    })
    try:
        with urlopen(req, timeout=FOLK_TIMEOUT) as r:
            text = r.read().decode("utf-8")
            parsed = json.loads(text) if text else {}
            if not isinstance(parsed, dict):
                return {"error": "unexpected non-object response"}
            try:
                touch_last_used(member_email, "folk")
            except Exception:
                pass
            return parsed
    except HTTPError as e:
        body_txt = ""
        try:
            body_txt = e.read().decode("utf-8", "replace")[:300]
        except Exception:
            pass
        return {"error": f"HTTP {e.code}: {body_txt}"}
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}


class FolkCRM:

    @classmethod
    def info(cls) -> PluginInfo:
        return PluginInfo(
            name="folk",
            display_name="Folk",
            category=PluginCategory.CRM,
            auth_method=AuthMethod.API_KEY,
            requires_credentials=["api_key"],
            homepage="https://www.folk.app",
            docs_url="https://developer.folk.app/api-reference",
            description=(
                "Pipe Narada hot replies into Folk as People + Notes "
                "(and Deals if you set a deals group). Paste a Folk API "
                "key from Settings → Developers. Deals are optional: add "
                "`deals_group_id` (+ `deals_object_type`, default 'deals') "
                "to enable them. Folk API needs a paid Folk plan."
            ),
            free_tier=False,
        )

    def is_available(self, member_email: str) -> bool:
        return has_credential(member_email, "folk")

    def upsert_contact(self, member_email: str, lead: Lead) -> str:
        """Upsert by email (dedup). Returns the Folk person id."""
        if not lead.email:
            return ""
        # Search by primary email first.
        search = _api(member_email, "GET", "/v1/people", params={
            "filter[emails][eq]": lead.email,
            "limit": 1,
        })
        if not search.get("error"):
            items = ((search.get("data") or {}).get("items")) or []
            if items and isinstance(items[0], dict) and items[0].get("id"):
                return str(items[0]["id"])
        # Create.
        props: dict = {"emails": [lead.email]}
        if lead.first_name:
            props["firstName"] = lead.first_name[:500]
        if lead.last_name:
            props["lastName"] = lead.last_name[:500]
        if lead.title:
            props["jobTitle"] = lead.title[:500]
        if lead.company:
            props["companies"] = [{"name": lead.company[:1000]}]
        resp = _api(member_email, "POST", "/v1/people", props)
        if "error" in resp:
            print(f"[narada/folk] upsert_contact failed: {resp['error']}",
                  flush=True)
            return ""
        return str((resp.get("data") or {}).get("id") or "")

    def create_deal(self, member_email: str, contact_id: str,
                    deal: DealData) -> str:
        """Create a deal in the configured deals group, associated to the
        contact. No-op (returns "") unless `deals_group_id` is set — Folk
        deals are group-scoped custom objects."""
        if not (contact_id and deal.title):
            return ""
        cred = _cred(member_email) or {}
        group_id = (cred.get("deals_group_id") or "").strip()
        if not group_id:
            return ""   # deals not configured for this member — skip cleanly
        object_type = (cred.get("deals_object_type") or "deals").strip()
        body = {
            "name": deal.title[:1000],
            "people": [{"id": contact_id}],
        }
        resp = _api(member_email, "POST",
                    f"/v1/groups/{group_id}/{object_type}", body)
        if "error" in resp:
            print(f"[narada/folk] create_deal failed: {resp['error']}",
                  flush=True)
            return ""
        return str((resp.get("data") or {}).get("id") or "")

    def log_activity(self, member_email: str, contact_id: str,
                     activity: Activity) -> None:
        """Log a private Note attached to the person (entity)."""
        if not contact_id:
            return
        content = (f"[Narada {activity.type}] {activity.subject}\n\n"
                   f"{activity.body}")[:100000]
        body = {
            "entity": {"id": contact_id},
            "visibility": "private",
            "content": content,
        }
        resp = _api(member_email, "POST", "/v1/notes", body)
        if "error" in resp:
            print(f"[narada/folk] log_activity failed: {resp['error']}",
                  flush=True)


# Auto-register
try:
    register(FolkCRM())
except Exception as _e:
    print(f"[narada/folk] register failed: "
          f"{type(_e).__name__}: {_e}", flush=True)
