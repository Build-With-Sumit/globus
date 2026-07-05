"""UpLead plugin — implements the LeadSource protocol (direct API).

UpLead (uplead.com) is a B2B prospecting database with a 95%
data-accuracy guarantee. Prospector Pro search filters by job title,
management level, industry, location and company size — and returns
work emails DIRECTLY in the search results (no separate reveal step
like Apollo), each tagged with a live `email_status`.

Credit quirk: UpLead deducts 1 credit per contact RETURNED (only when
the email is Valid/Accept-All) — search itself is metered, so `count`
maps 1:1 to spend. We never request more than asked.

Coverage gaps (documented per types.py convention): UpLead takes ONE
management level per search, so only the first mappable ICP seniority
is used; keywords / technologies / funding-stage filters aren't
supported and are ignored; free-text locations are treated as country
names or AMER/EMEA/APAC/LATAM region codes (city-level ICPs should
pass icp.raw["cities"]). Industry names must match UpLead's taxonomy.

Auth: header `Authorization: <key>`. Base https://api.uplead.com/v2.
Docs: https://docs.uplead.com
Slug `uplead`; paste the API key as `api_key`.
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


UPLEAD_API_BASE = "https://api.uplead.com/v2"
UPLEAD_TIMEOUT = 30
# Browser UA — UpLead sits behind a WAF that fingerprint-blocks the
# default `Python-urllib/x.y` UA (same class of block RocketReach's
# Cloudflare does); a normal browser UA passes cleanly.
UPLEAD_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)

# UpLead's fixed employee-count buckets (docs: prospector-pro-search
# `employees` param). We pick every bucket overlapping the ICP range.
_EMPLOYEE_BUCKETS: list[tuple[int, int, str]] = [
    (1, 10, "1-10"),
    (11, 50, "11-50"),
    (51, 200, "51-200"),
    (201, 500, "201-500"),
    (501, 1000, "501-1000"),
    (1001, 5000, "1001-5000"),
    (5001, 10000, "5001-10000"),
    (10001, 10**9, "10001+"),
]

# ICP seniority terms → UpLead management_level codes (M/D/VP/C/CX).
_SENIORITY_TO_LEVEL = {
    "c_suite": "C", "c-suite": "C", "csuite": "C", "cxo": "C",
    "founder": "C", "owner": "C", "partner": "C",
    "vp": "VP", "vice_president": "VP", "vice-president": "VP",
    "director": "D", "head": "D",
    "manager": "M",
}

_REGION_CODES = {"amer", "emea", "apac", "latam"}


def _api_key(member_email: str) -> str | None:
    cred = get_credential(member_email, "uplead")
    if not cred:
        return None
    return (cred.get("api_key") or "").strip() or None


def _post(member_email: str, path: str, body: dict) -> dict:
    """POST to UpLead's API + return parsed JSON, or {'error': ...}.
    Never raises."""
    api_key = _api_key(member_email)
    if not api_key:
        return {"error": "no UpLead credential for this member"}
    url = f"{UPLEAD_API_BASE}{path}"
    try:
        data = json.dumps(body).encode("utf-8")
    except (TypeError, ValueError) as e:
        # icp.raw is an untyped escape hatch — a non-serializable value
        # in it must not let search() raise (protocol: never raise).
        return {"error": f"unserializable request body: {e}"}
    req = Request(url, data=data, method="POST", headers={
        "Authorization": api_key,
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": UPLEAD_USER_AGENT,
    })
    try:
        with urlopen(req, timeout=UPLEAD_TIMEOUT) as r:
            text = r.read().decode("utf-8")
            resp = json.loads(text) if text else {}
    except HTTPError as e:
        body_txt = ""
        try:
            body_txt = e.read().decode("utf-8", "replace")[:300]
        except Exception:
            pass
        return {"error": f"HTTP {e.code}: {body_txt}"}
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}
    touch_last_used(member_email, "uplead")
    return resp if isinstance(resp, dict) else {"data": resp}


def _employee_buckets(icp: ICPFilters) -> list[str]:
    """Map the ICP's [min, max] head-count range onto UpLead's fixed
    buckets — every bucket that overlaps the range is included."""
    lo = icp.company_size_min or 0
    hi = icp.company_size_max or 0
    if not (lo or hi):
        return []
    lo = lo or 1
    hi = hi or 10**9
    return [label for b_lo, b_hi, label in _EMPLOYEE_BUCKETS
            if b_lo <= hi and b_hi >= lo]


def _management_level(seniority: list[str]) -> str:
    """UpLead takes ONE management_level (M/D/VP/C). Use the first ICP
    seniority term that translates; the rest are a documented gap."""
    for s in seniority:
        level = _SENIORITY_TO_LEVEL.get((s or "").strip().lower())
        if level:
            return level
    return ""


def _split_locations(locations: list[str]) -> tuple[list[str], list[str]]:
    """UpLead splits regions (AMER/EMEA/APAC/LATAM) from countries.
    We can't reliably tell cities from countries, so every non-region
    value goes to `countries`; city ICPs should use icp.raw["cities"]."""
    regions: list[str] = []
    countries: list[str] = []
    for loc in locations:
        v = (loc or "").strip()
        if not v:
            continue
        if v.lower() in _REGION_CODES:
            regions.append(v.upper())
        else:
            countries.append(v)
    return regions, countries


class UpLeadLeadSource:

    @classmethod
    def info(cls) -> PluginInfo:
        return PluginInfo(
            name="uplead",
            display_name="UpLead",
            category=PluginCategory.LEAD_SOURCE,
            auth_method=AuthMethod.API_KEY,
            requires_credentials=["api_key"],
            homepage="https://www.uplead.com",
            docs_url="https://docs.uplead.com",
            description=(
                "B2B contact database with a 95% data-accuracy guarantee. "
                "Search by title, seniority, industry, location and company "
                "size — verified work emails come back directly in the "
                "results, no separate reveal step. One credit per contact "
                "returned, charged only when the email is valid. Paste "
                "your UpLead API key (Account > API)."
            ),
            free_tier=False,
        )

    def is_available(self, member_email: str) -> bool:
        return has_credential(member_email, "uplead")

    def search(self, member_email: str, icp: ICPFilters,
               count: int = 50) -> list[Lead]:
        if count <= 0:
            # Don't fire a metered call (UpLead charges per contact
            # RETURNED) just to slice the results down to nothing.
            return []
        body: dict = {
            "page": 1,
            "per_page": max(1, min(count, 100)),
        }
        if icp.roles:
            body["titles"] = icp.roles
            body["title_search_mode"] = "include"
        level = _management_level(icp.seniority)
        if level:
            body["management_level"] = level
        regions, countries = _split_locations(icp.locations)
        if regions:
            body["regions"] = regions
        if countries:
            body["countries"] = countries
        if regions or countries:
            # Filter by where the PERSON sits, not company HQ — that's
            # what a location in an ICP means (UpLead defaults to HQ).
            body["location_target"] = "contact"
        if icp.industries:
            body["industries"] = icp.industries
        buckets = _employee_buckets(icp)
        if buckets:
            body["employees"] = buckets
        if icp.raw:
            body.update(icp.raw)

        resp = _post(member_email, "/prospector-pro-search", body)
        if "error" in resp:
            print(f"[narada/uplead] search failed: {resp['error']}",
                  flush=True)
            return []
        data = resp.get("data") or {}
        results = data.get("results") if isinstance(data, dict) else data
        results = results if isinstance(results, list) else []
        out: list[Lead] = []
        for c in results[:count]:
            if not isinstance(c, dict):
                continue
            company = c.get("company") if isinstance(c.get("company"),
                                                     dict) else {}
            out.append(Lead(
                first_name=(c.get("first_name") or "")[:120],
                last_name=(c.get("last_name") or "")[:120],
                email=(c.get("email") or "").strip().lower()[:320],
                company=(c.get("company_name")
                         or company.get("company_name") or "")[:255],
                company_domain=(c.get("domain")
                                or company.get("domain") or "")[:255],
                title=(c.get("title") or "")[:255],
                linkedin_url=(c.get("linkedin_url") or "")[:512],
                source="uplead",
                source_metadata={"uplead_id": c.get("id"),
                                 "email_status": c.get("email_status")},
            ))
        return out

    def find_email(self, member_email: str, first_name: str,
                   last_name: str, company_domain: str) -> str | None:
        """Person Search by name + company domain (1 credit when the
        returned email is Valid/Accept-All)."""
        if not (first_name and last_name and company_domain):
            return None
        resp = _post(member_email, "/person-search", {
            "first_name": first_name,
            "last_name": last_name,
            "domain": company_domain,
        })
        if "error" in resp:
            return None
        data = resp.get("data")
        if isinstance(data, list):
            data = data[0] if data else {}
        if not isinstance(data, dict):
            return None
        addr = (data.get("email") or "").strip().lower()
        return addr or None

    def cost_estimate(self, action: str, count: int = 1) -> dict:
        n = max(1, count)
        # Everything is metered on UpLead: 1 credit per contact returned
        # (search) and per lookup hit (find_email).
        return {
            "credits": n,
            "approx_usd": round(n * 0.40, 4),
            "notes": ("UpLead: 1 credit per contact returned — search "
                      "results AND find_email lookups are both metered, "
                      "but credits only deduct when the email is "
                      "Valid/Accept-All (~$0.40-0.60/credit by plan)."),
        }


# Auto-register
try:
    register(UpLeadLeadSource())
except Exception as _e:
    print(f"[narada/uplead] register failed: "
          f"{type(_e).__name__}: {_e}", flush=True)
