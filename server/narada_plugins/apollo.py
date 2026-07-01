"""Apollo.io plugin — implements the LeadSource protocol (direct API).

Apollo (apollo.io) is a large B2B people/company database with search +
email enrichment. This plugin covers lead SEARCH + single email
enrichment. (Apollo also has outbound Sequences; that's a separate
Sender plugin, not built here — Gmail is Narada's primary sender.)

⚠️ Apollo masks emails in search results by default
(`email_not_unlocked@domain.com`). Real emails come from the People
Match endpoint (`find_email`), which consumes an enrichment credit —
so the Narada core should search first, then reveal only the leads the
marketer keeps. We never bulk-reveal (credit-burn protection).

Auth: header `X-Api-Key: <key>`. Base https://api.apollo.io.
Docs: https://docs.apollo.io/reference/
Slug `apollo`; paste the API key as `api_key`.
"""
from __future__ import annotations
import json
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from narada_creds import get_credential, has_credential, touch_last_used
from narada_plugins import register
from narada_plugins.types import (
    AuthMethod, ICPFilters, Lead, PluginCategory, PluginInfo,
)


APOLLO_API_BASE = "https://api.apollo.io"
APOLLO_TIMEOUT = 30
_EMAIL_LOCKED = "email_not_unlocked"


def _api_key(member_email: str) -> str | None:
    cred = get_credential(member_email, "apollo")
    if not cred:
        return None
    return (cred.get("api_key") or "").strip() or None


def _post(member_email: str, path: str, body: dict) -> dict:
    """POST to Apollo's REST API + return parsed JSON, or {'error': ...}.
    Never raises."""
    api_key = _api_key(member_email)
    if not api_key:
        return {"error": "no Apollo credential for this member"}
    url = f"{APOLLO_API_BASE}{path}"
    data = json.dumps(body).encode("utf-8")
    req = Request(url, data=data, method="POST", headers={
        "Content-Type": "application/json",
        "Cache-Control": "no-cache",
        "X-Api-Key": api_key,
    })
    try:
        with urlopen(req, timeout=APOLLO_TIMEOUT) as r:
            resp = json.loads(r.read().decode("utf-8"))
    except HTTPError as e:
        body_txt = ""
        try:
            body_txt = e.read().decode("utf-8", "replace")[:300]
        except Exception:
            pass
        return {"error": f"HTTP {e.code}: {body_txt}"}
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}
    touch_last_used(member_email, "apollo")
    return resp


def _clean_email(v: str) -> str:
    v = (v or "").strip().lower()
    return "" if (not v or _EMAIL_LOCKED in v) else v


def _org_size_ranges(icp: ICPFilters) -> list[str]:
    """Apollo wants employee ranges as 'min,max' strings."""
    lo = icp.company_size_min or 0
    hi = icp.company_size_max or 0
    if not (lo or hi):
        return []
    return [f"{lo or 1},{hi or 1000000}"]


class ApolloLeadSource:

    @classmethod
    def info(cls) -> PluginInfo:
        return PluginInfo(
            name="apollo",
            display_name="Apollo.io",
            category=PluginCategory.LEAD_SOURCE,
            auth_method=AuthMethod.API_KEY,
            requires_credentials=["api_key"],
            homepage="https://apollo.io",
            docs_url="https://docs.apollo.io/reference/",
            description=(
                "B2B people/company search + email enrichment. Search is "
                "cheap; revealing an email costs an enrichment credit "
                "(done per-lead via find_email, never bulk). Paste your "
                "Apollo API key."
            ),
            free_tier=True,
        )

    def is_available(self, member_email: str) -> bool:
        return has_credential(member_email, "apollo")

    def search(self, member_email: str, icp: ICPFilters,
               count: int = 50) -> list[Lead]:
        keywords = list(icp.keywords) + list(icp.industries)
        body: dict = {
            "page": 1,
            "per_page": max(1, min(count, 100)),
        }
        if icp.roles:
            body["person_titles"] = icp.roles
        if icp.seniority:
            body["person_seniorities"] = icp.seniority
        if icp.locations:
            body["person_locations"] = icp.locations
        sizes = _org_size_ranges(icp)
        if sizes:
            body["organization_num_employees_ranges"] = sizes
        if keywords:
            body["q_keywords"] = " ".join(keywords)
        if icp.raw:
            body.update(icp.raw)

        resp = _post(member_email, "/v1/mixed_people/search", body)
        if "error" in resp:
            print(f"[narada/apollo] search failed: {resp['error']}",
                  flush=True)
            return []
        people = resp.get("people") or []
        out: list[Lead] = []
        for p in people[:count]:
            org = p.get("organization") or {}
            domain = (org.get("primary_domain")
                      or org.get("website_url") or "")
            out.append(Lead(
                first_name=(p.get("first_name") or "")[:120],
                last_name=(p.get("last_name") or "")[:120],
                email=_clean_email(p.get("email"))[:320],
                company=(org.get("name") or "")[:255],
                company_domain=domain.replace("http://", "")
                                     .replace("https://", "").strip("/")[:255],
                title=(p.get("title") or "")[:255],
                linkedin_url=(p.get("linkedin_url") or "")[:512],
                source="apollo",
                source_metadata={"apollo_id": p.get("id"),
                                 "email_status": p.get("email_status")},
            ))
        return out

    def find_email(self, member_email: str, first_name: str,
                   last_name: str, company_domain: str) -> str | None:
        """People Match — reveals a verified email (1 enrichment credit)."""
        if not (first_name and last_name and company_domain):
            return None
        resp = _post(member_email, "/v1/people/match", {
            "first_name": first_name,
            "last_name": last_name,
            "domain": company_domain,
            "reveal_personal_emails": False,
        })
        if "error" in resp:
            return None
        person = resp.get("person") or {}
        return _clean_email(person.get("email")) or None

    def cost_estimate(self, action: str, count: int = 1) -> dict:
        n = max(1, count)
        # Search results are effectively free within plan; email reveal
        # is the metered action (1 credit).
        credits = n if action in ("find_email", "reveal", "enrich") else 0
        return {
            "credits": credits,
            "approx_usd": round(credits * 0.03, 4),
            "notes": ("Apollo: search is plan-included; each email reveal "
                      "(find_email) is ~1 enrichment credit (~$0.03 on "
                      "mid-tier). Search itself doesn't burn credits."),
        }


# Auto-register
try:
    register(ApolloLeadSource())
except Exception as _e:
    print(f"[narada/apollo] register failed: "
          f"{type(_e).__name__}: {_e}", flush=True)
