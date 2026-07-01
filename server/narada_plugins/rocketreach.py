"""RocketReach plugin — implements the LeadSource protocol (direct API).

RocketReach (rocketreach.co) finds professional + personal emails and
phone numbers from name + company or a LinkedIn URL. Strong on
contact-info coverage; lighter on ICP-style bulk search than Apollo.

Flow quirk: `person/search` returns profile stubs (name/title/company/
LinkedIn) — emails come from `person/lookup`, which may be async
(returns `status: "progress"` then completes). So `search` returns
leads with emails when present, and `find_email` runs an explicit
lookup. We don't poll aggressively — one lookup call per find_email.

Auth: header `Api-Key: <key>`. Base https://api.rocketreach.co/api/v2.
Docs: https://rocketreach.co/api/v2/docs
Slug `rocketreach`; paste the API key as `api_key`.
"""
from __future__ import annotations
import json
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from narada_creds import get_credential, has_credential, touch_last_used
from narada_plugins import register
from narada_plugins.types import (
    AuthMethod, ICPFilters, Lead, PluginCategory, PluginInfo,
)


ROCKETREACH_API_BASE = "https://api.rocketreach.co/api/v2"
ROCKETREACH_TIMEOUT = 30


def _api_key(member_email: str) -> str | None:
    cred = get_credential(member_email, "rocketreach")
    if not cred:
        return None
    return (cred.get("api_key") or "").strip() or None


def _call(member_email: str, method: str, path: str,
          body: dict | None = None, params: dict | None = None) -> dict:
    """Call RocketReach's API. Returns parsed JSON, or {'error': ...}.
    Never raises."""
    api_key = _api_key(member_email)
    if not api_key:
        return {"error": "no RocketReach credential for this member"}
    url = f"{ROCKETREACH_API_BASE}{path}"
    if params:
        url += "?" + urlencode(params)
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = Request(url, data=data, method=method, headers={
        "Api-Key": api_key,
        "Content-Type": "application/json",
        "Accept": "application/json",
    })
    try:
        with urlopen(req, timeout=ROCKETREACH_TIMEOUT) as r:
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
        touch_last_used(member_email, "rocketreach")


def _best_email(profile: dict) -> str:
    """Pick the highest-grade email from a RocketReach profile."""
    if profile.get("current_work_email"):
        return profile["current_work_email"].strip().lower()
    emails = profile.get("emails") or []
    # emails is a list of {email, smtp_valid, type}; prefer valid + professional.
    ranked = sorted(
        emails,
        key=lambda e: (e.get("smtp_valid") == "valid",
                       e.get("type") == "professional"),
        reverse=True)
    for e in ranked:
        addr = (e.get("email") or "").strip().lower()
        if addr:
            return addr
    return (profile.get("email") or "").strip().lower()


class RocketReachLeadSource:

    @classmethod
    def info(cls) -> PluginInfo:
        return PluginInfo(
            name="rocketreach",
            display_name="RocketReach",
            category=PluginCategory.LEAD_SOURCE,
            auth_method=AuthMethod.API_KEY,
            requires_credentials=["api_key"],
            homepage="https://rocketreach.co",
            docs_url="https://rocketreach.co/api/v2/docs",
            description=(
                "Find work/personal emails + phones from name + company or "
                "a LinkedIn URL. Best for contact-info coverage. Search "
                "returns profiles; emails come from a per-lead lookup "
                "(find_email). Paste your RocketReach API key."
            ),
            free_tier=True,
        )

    def is_available(self, member_email: str) -> bool:
        return has_credential(member_email, "rocketreach")

    def search(self, member_email: str, icp: ICPFilters,
               count: int = 50) -> list[Lead]:
        query: dict = {}
        if icp.roles:
            query["current_title"] = icp.roles
        if icp.locations:
            query["location"] = icp.locations
        if icp.keywords or icp.industries:
            query["keyword"] = list(icp.keywords) + list(icp.industries)
        body = {
            "query": query or {"keyword": ["b2b"]},
            "start": 1,
            "page_size": max(1, min(count, 100)),
        }
        if icp.raw:
            body.update(icp.raw)
        resp = _call(member_email, "POST", "/person/search", body)
        if "error" in resp:
            print(f"[narada/rocketreach] search failed: {resp['error']}",
                  flush=True)
            return []
        profiles = resp.get("profiles") or []
        out: list[Lead] = []
        for p in profiles[:count]:
            employer = p.get("current_employer") or ""
            out.append(Lead(
                first_name=(p.get("first_name")
                            or (p.get("name") or "").split(" ")[0])[:120],
                last_name=(p.get("last_name")
                           or " ".join((p.get("name") or "").split(" ")[1:]))[:120],
                email=_best_email(p)[:320],
                company=employer[:255],
                company_domain=(p.get("current_employer_domain") or "")[:255],
                title=(p.get("current_title") or "")[:255],
                linkedin_url=(p.get("linkedin_url") or "")[:512],
                source="rocketreach",
                source_metadata={"rr_id": p.get("id")},
            ))
        return out

    def find_email(self, member_email: str, first_name: str,
                   last_name: str, company_domain: str) -> str | None:
        """Lookup by name + company (1 lookup credit). RocketReach may
        return status 'progress' while it resolves; we take what's ready."""
        if not ((first_name or last_name) and company_domain):
            return None
        name = f"{first_name} {last_name}".strip()
        resp = _call(member_email, "GET", "/person/lookup", params={
            "name": name,
            "current_employer": company_domain,
        })
        if "error" in resp:
            return None
        addr = _best_email(resp)
        return addr or None

    def cost_estimate(self, action: str, count: int = 1) -> dict:
        n = max(1, count)
        credits = n if action in ("find_email", "lookup") else 0
        return {
            "credits": credits,
            "approx_usd": round(credits * 0.05, 4),
            "notes": ("RocketReach: search returns stubs (plan-included); "
                      "each email lookup (find_email) is ~1 lookup credit "
                      "(~$0.05 depending on tier)."),
        }


# Auto-register
try:
    register(RocketReachLeadSource())
except Exception as _e:
    print(f"[narada/rocketreach] register failed: "
          f"{type(_e).__name__}: {_e}", flush=True)
