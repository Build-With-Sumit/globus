"""Hunter.io plugin — implements the LeadSource protocol (direct API).

Hunter (hunter.io) is domain-centric: it indexes the public web for
email addresses at a given company domain. Great at "who can I email
at acme.com" (Domain Search) and "what's Jane Doe's email at acme.com"
(Email Finder). It has NO people/ICP search — you can't ask it for
"CMOs at Series-B SaaS companies", so `search` only works when the ICP
carries explicit targets: `icp.raw["domains"]`, `icp.raw["companies"]`,
or keywords that look like domains. Otherwise it returns [] and the
agent should source domains elsewhere first. Roles map to Hunter's
`job_titles` filter and seniority to its junior/senior/executive bands.

Auth: `?api_key=<key>` query param. Base https://api.hunter.io/v2.
Docs: https://hunter.io/api-documentation/v2
Slug `hunter`; paste the API key as `api_key`.
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


HUNTER_API_BASE = "https://api.hunter.io/v2"
HUNTER_TIMEOUT = 30
HUNTER_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)
# Hunter caps Domain Search at 100 emails per request.
HUNTER_MAX_PAGE = 100

# ICP seniority tokens → Hunter's three bands (junior/senior/executive).
_SENIORITY_MAP = {
    "c_suite": "executive", "cxo": "executive", "executive": "executive",
    "founder": "executive", "owner": "executive", "partner": "executive",
    "vp": "executive",
    "director": "senior", "head": "senior", "senior": "senior",
    "manager": "senior",
    "entry": "junior", "junior": "junior", "intern": "junior",
}


def _api_key(member_email: str) -> str | None:
    cred = get_credential(member_email, "hunter")
    if not cred:
        return None
    return (cred.get("api_key") or "").strip() or None


def _call(member_email: str, path: str, params: dict) -> dict:
    """GET a Hunter v2 endpoint. Returns parsed JSON, or {'error': ...}.
    Never raises."""
    api_key = _api_key(member_email)
    if not api_key:
        return {"error": "no Hunter credential for this member"}
    qs = urlencode({**params, "api_key": api_key})
    req = Request(f"{HUNTER_API_BASE}{path}?{qs}", headers={
        "Accept": "application/json",
        "User-Agent": HUNTER_USER_AGENT,
    })
    try:
        with urlopen(req, timeout=HUNTER_TIMEOUT) as r:
            text = r.read().decode("utf-8")
            resp = json.loads(text) if text else {}
    except HTTPError as e:
        detail = ""
        try:
            # Hunter errors: {"errors": [{"id", "code", "details"}]}
            err = json.loads(e.read().decode("utf-8", "replace"))
            detail = (err.get("errors") or [{}])[0].get("details") or ""
        except Exception:
            pass
        return {"error": f"HTTP {e.code}: {detail[:300]}"}
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}
    try:  # successful hit only; a DB hiccup must not break the call
        touch_last_used(member_email, "hunter")
    except Exception:
        pass
    return resp


def _looks_like_domain(s: str) -> bool:
    s = s.strip().lower()
    return bool(s) and "." in s and " " not in s and "@" not in s


def _as_list(v) -> list[str]:
    """Normalise a raw-filter value: list, or comma-separated string."""
    if isinstance(v, str):
        return [x.strip() for x in v.split(",") if x.strip()]
    if isinstance(v, (list, tuple)):
        return [str(x).strip() for x in v if str(x).strip()]
    return []


def _targets_from_icp(icp: ICPFilters) -> list[tuple[str, str]]:
    """Hunter has no ICP search — collect explicit Domain Search targets
    as (param_name, value) pairs: raw['domains'], raw['companies'], plus
    any keyword that looks like a domain."""
    targets: list[tuple[str, str]] = []
    for d in _as_list((icp.raw or {}).get("domains")):
        targets.append(("domain", d.lower()))
    for c in _as_list((icp.raw or {}).get("companies")):
        targets.append(("company", c))
    for kw in icp.keywords:
        if _looks_like_domain(kw):
            targets.append(("domain", kw.strip().lower()))
    seen: set[tuple[str, str]] = set()
    return [t for t in targets if not (t in seen or seen.add(t))]


def _filter_params(icp: ICPFilters) -> dict:
    """Map the ICP's roles/seniority onto Hunter's Domain Search filters."""
    params: dict = {}
    if icp.roles:
        params["job_titles"] = ",".join(icp.roles)
    bands = {_SENIORITY_MAP[s.strip().lower()]
             for s in icp.seniority if s.strip().lower() in _SENIORITY_MAP}
    if bands:
        params["seniority"] = ",".join(sorted(bands))
    return params


class HunterLeadSource:

    @classmethod
    def info(cls) -> PluginInfo:
        return PluginInfo(
            name="hunter",
            display_name="Hunter.io",
            category=PluginCategory.LEAD_SOURCE,
            auth_method=AuthMethod.API_KEY,
            requires_credentials=["api_key"],
            homepage="https://hunter.io",
            docs_url="https://hunter.io/api-documentation/v2",
            description=(
                "Find everyone's email at a company domain (Domain Search) "
                "or one person's email from their name + company (Email "
                "Finder). Domain-centric: give it target domains or company "
                "names — it can't search by industry/ICP on its own. "
                "Paste your Hunter API key."
            ),
            free_tier=True,
        )

    def is_available(self, member_email: str) -> bool:
        return has_credential(member_email, "hunter")

    def search(self, member_email: str, icp: ICPFilters,
               count: int = 50) -> list[Lead]:
        targets = _targets_from_icp(icp)
        if not targets:
            print("[narada/hunter] search skipped: no domains/companies in "
                  "ICP (Hunter has no ICP-style people search)", flush=True)
            return []
        filters = _filter_params(icp)
        out: list[Lead] = []
        seen_emails: set[str] = set()
        for param, value in targets:
            remaining = count - len(out)
            if remaining <= 0:
                break
            resp = _call(member_email, "/domain-search", {
                param: value,
                "limit": max(1, min(remaining, HUNTER_MAX_PAGE)),
                **filters,
            })
            if "error" in resp:
                print(f"[narada/hunter] domain-search {value!r} failed: "
                      f"{resp['error']}", flush=True)
                continue
            data = resp.get("data") or {}
            company = data.get("organization") or ""
            domain = data.get("domain") or ""
            for e in (data.get("emails") or [])[:remaining]:
                addr = (e.get("value") or "").strip().lower()
                if not addr or addr in seen_emails:
                    continue
                seen_emails.add(addr)
                out.append(Lead(
                    first_name=(e.get("first_name") or "")[:120],
                    last_name=(e.get("last_name") or "")[:120],
                    email=addr[:320],
                    company=company[:255],
                    company_domain=domain[:255],
                    title=(e.get("position") or "")[:255],
                    linkedin_url=(e.get("linkedin") or "")[:512],
                    source="hunter",
                    source_metadata={
                        "confidence": e.get("confidence"),
                        "type": e.get("type"),
                        "seniority": e.get("seniority"),
                        "department": e.get("department"),
                        "verification": (e.get("verification") or {}
                                         ).get("status"),
                    },
                ))
        return out[:count]

    def find_email(self, member_email: str, first_name: str,
                   last_name: str, company_domain: str) -> str | None:
        """Email Finder (1 search credit when it returns a result).
        Hunter auto-verifies the result; we return it either way and
        leave verification policy to the Verifier plugin."""
        # Hunter requires BOTH first_name and last_name (we don't send
        # full_name/linkedin_handle) — a one-name call is a guaranteed 400.
        if not (first_name and last_name and company_domain):
            return None
        resp = _call(member_email, "/email-finder", {
            "domain": company_domain,
            "first_name": first_name,
            "last_name": last_name,
        })
        if "error" in resp:
            return None
        addr = ((resp.get("data") or {}).get("email") or "").strip().lower()
        return addr or None

    def cost_estimate(self, action: str, count: int = 1) -> dict:
        n = max(1, count)
        if action in ("find_email", "lookup"):
            credits = n
        else:
            # Domain Search: 1 search credit per 10 emails returned.
            credits = -(-n // 10)  # ceil(n / 10)
        return {
            "credits": credits,
            "approx_usd": round(credits * 0.05, 4),
            "notes": ("Hunter: Domain Search costs 1 search per 10 emails; "
                      "Email Finder is 1 search per lookup (~$0.03-0.10 "
                      "each depending on plan; free tier: 25 searches/mo)."),
        }


# Auto-register
try:
    register(HunterLeadSource())
except Exception as _e:
    print(f"[narada/hunter] register failed: "
          f"{type(_e).__name__}: {_e}", flush=True)
