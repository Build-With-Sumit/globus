"""ZoomInfo plugin — implements the LeadSource protocol (direct API).

ZoomInfo (zoominfo.com) is the heavyweight B2B contact + company
database — deepest US coverage, strict enterprise contracts. This
plugin talks to the legacy Enterprise API at https://api.zoominfo.com.

Flow quirk: `POST /search/contact` returns profile stubs (name/title/
company + a `hasEmail` flag) — emails are only released by
`POST /enrich/contact`, which burns 1 enrich credit per matched
record. So `search` returns stub leads (email empty, ZoomInfo person
id stashed in source_metadata) and `find_email` does search-then-
enrich for one contact. Nothing is cached; every invocation
re-authenticates (tokens only live ~60 min anyway).

Auth: POST /authenticate with {"username", "password"} -> {"jwt"},
sent as `Authorization: Bearer <jwt>` (expires ~60 minutes). PKI
key-pair auth exists but is NOT supported here — username/password only.
Docs: https://api-docs.zoominfo.com/
Slug `zoominfo`; members paste `username` and `password`.

ICP coverage gaps: funding stage, technologies (needs numeric
techAttributeTagList codes) and free-text keywords are not mapped;
industries are passed as `industryKeywords` (free text) because
`industryCodes` only accepts lookup-endpoint dot-codes like
"mfg.car"; company_size maps to employeeRangeMin/Max; city/metro
locations are passed as `state`, which ZoomInfo may not match — use
icp.raw for `metroRegion` etc. (merged into the search body verbatim).
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


ZOOMINFO_API_BASE = "https://api.zoominfo.com"
ZOOMINFO_TIMEOUT = 30
ZOOMINFO_USER_AGENT = "Narada/1.0 (outbound agent; +https://globussoft.ai)"
# Legacy contact search: rpp defaults to 25 and 400s above 100
# ("Results per page (rpp) is over max allowed value (100)"); paging
# past 1000 total records also 400s ("Total record pagination is over
# max allowed value (1000)"). We page up to `count` and never past it.
ZOOMINFO_MAX_RPP = 100
ZOOMINFO_MAX_TOTAL = 1000

# ICP seniority tokens -> ZoomInfo managementLevel labels. These are
# the canonical taxonomy labels ZoomInfo emits in its own responses
# ("C-Level", "VP-Level", ...); the search input parser is tolerant —
# official examples pass "C Level Execs" and "directors" — and
# GET /lookup/managementLevel is the authoritative value list. Unknown
# tokens are DROPPED rather than passed through (a bad value risks
# 400ing or zero-matching the whole search).
_SENIORITY_MAP = {
    "c_suite": "C-Level", "c-suite": "C-Level", "c_level": "C-Level",
    "c-level": "C-Level", "cxo": "C-Level", "founder": "C-Level",
    "owner": "C-Level", "partner": "C-Level",
    "vp": "VP-Level", "vice_president": "VP-Level",
    "vice president": "VP-Level",
    "director": "Director", "head": "Director",
    "manager": "Manager", "senior": "Manager",
    "entry": "Non-Manager", "junior": "Non-Manager",
    "individual_contributor": "Non-Manager", "ic": "Non-Manager",
}

# Country names ZoomInfo's `country` filter accepts (lowercased). ICP
# locations not in this set fall through to `state` (see docstring gap).
_COUNTRY_ALIASES = {
    "us": "United States", "usa": "United States",
    "united states": "United States",
    "united states of america": "United States",
    "uk": "United Kingdom", "united kingdom": "United Kingdom",
    "uae": "United Arab Emirates",
    "united arab emirates": "United Arab Emirates",
}
_COUNTRIES = {
    "canada", "australia", "india", "germany", "france", "singapore",
    "netherlands", "spain", "italy", "brazil", "japan", "china",
    "israel", "ireland", "sweden", "switzerland", "belgium", "norway",
    "denmark", "finland", "poland", "portugal", "austria",
    "new zealand", "mexico", "south africa", "argentina", "colombia",
    "philippines", "indonesia", "malaysia", "vietnam", "thailand",
    "south korea", "turkey", "saudi arabia", "egypt", "nigeria",
    "kenya", "pakistan", "bangladesh", "sri lanka", "romania",
    "czech republic", "hungary", "greece", "ukraine",
}


def _creds(member_email: str) -> tuple[str, str] | None:
    cred = get_credential(member_email, "zoominfo")
    if not cred:
        return None
    username = (cred.get("username") or "").strip()
    password = (cred.get("password") or "").strip()
    if not (username and password):
        return None
    return username, password


def _authenticate(member_email: str) -> dict:
    """POST /authenticate -> {'jwt': ...} or {'error': ...}. The JWT
    lives ~60 min; we fetch a fresh one per public-method invocation
    (cache nothing). Never raises."""
    creds = _creds(member_email)
    if not creds:
        return {"error": "no ZoomInfo credential for this member"}
    username, password = creds
    req = Request(
        f"{ZOOMINFO_API_BASE}/authenticate",
        data=json.dumps({"username": username,
                         "password": password}).encode("utf-8"),
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": ZOOMINFO_USER_AGENT,
        })
    try:
        with urlopen(req, timeout=ZOOMINFO_TIMEOUT) as r:
            resp = json.loads(r.read().decode("utf-8") or "{}")
    except HTTPError as e:
        body_txt = ""
        try:
            body_txt = e.read().decode("utf-8", "replace")[:300]
        except Exception:
            pass
        return {"error": f"auth HTTP {e.code}: {body_txt}"}
    except Exception as e:
        return {"error": f"auth {type(e).__name__}: {e}"}
    if not isinstance(resp, dict):
        return {"error": f"auth response not an object: {str(resp)[:200]}"}
    jwt = resp.get("jwt")
    jwt = jwt.strip() if isinstance(jwt, str) else ""
    if not jwt:
        return {"error": f"auth response had no jwt: {str(resp)[:200]}"}
    return {"jwt": jwt}


def _call(member_email: str, jwt: str, path: str, body: dict) -> dict:
    """POST to ZoomInfo's API with a Bearer JWT. Returns parsed JSON,
    or {'error': ...}. Never raises."""
    req = Request(
        f"{ZOOMINFO_API_BASE}{path}",
        data=json.dumps(body).encode("utf-8"),
        method="POST",
        headers={
            "Authorization": f"Bearer {jwt}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": ZOOMINFO_USER_AGENT,
        })
    try:
        with urlopen(req, timeout=ZOOMINFO_TIMEOUT) as r:
            text = r.read().decode("utf-8")
            parsed = json.loads(text) if text else {}
            # Normalise a bare-array response so callers can .get().
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
        touch_last_used(member_email, "zoominfo")


def _icp_to_search_body(icp: ICPFilters) -> dict:
    """Map ICPFilters onto legacy /search/contact params. ZoomInfo's
    filter fields are strings, not arrays — multi-value fields are
    either comma-separated or OR-separated per the param docs."""
    body: dict = {}
    if icp.roles:
        # Docs: "Use OR to input multiple job titles."
        body["jobTitle"] = " OR ".join(icp.roles)
    levels = []
    for s in icp.seniority:
        mapped = _SENIORITY_MAP.get((s or "").strip().lower())
        if mapped and mapped not in levels:
            levels.append(mapped)
    if levels:
        body["managementLevel"] = ",".join(levels)
    countries, states = [], []
    for loc in icp.locations:
        key = (loc or "").strip().lower()
        if not key:
            continue
        if key in _COUNTRY_ALIASES:
            countries.append(_COUNTRY_ALIASES[key])
        elif key in _COUNTRIES:
            countries.append(key.title())
        else:
            states.append(loc.strip())
    if countries:
        body["country"] = ",".join(countries)
    if states:
        body["state"] = ",".join(states)
    if icp.industries:
        # industryCodes only accepts lookup-endpoint dot-codes (e.g.
        # "mfg.car"); ICP industries are free text, so use the
        # documented free-text param: "industryKeywords — Industry
        # keywords associated with a company. Can include a
        # comma-separated list."
        body["industryKeywords"] = ",".join(icp.industries)
    if icp.company_size_min > 0:
        body["employeeRangeMin"] = str(icp.company_size_min)
    if icp.company_size_max > 0:
        body["employeeRangeMax"] = str(icp.company_size_max)
    if icp.raw:
        body.update(icp.raw)
    return body


def _str_field(record: dict, key: str) -> str:
    """String value of record[key], or '' for missing/non-string."""
    v = record.get(key)
    return v.strip() if isinstance(v, str) else ""


def _enrich_email(resp: dict) -> str:
    """Pull the first email out of an /enrich/contact response
    (data.result[].data[].email). Shape-tolerant, never raises."""
    data = resp.get("data")
    if not isinstance(data, dict):
        return ""
    results = data.get("result")
    if not isinstance(results, list):
        return ""
    for result in results:
        if not isinstance(result, dict):
            continue
        recs = result.get("data")
        if not isinstance(recs, list):
            continue
        for rec in recs:
            if not isinstance(rec, dict):
                continue
            addr = _str_field(rec, "email").lower()
            if addr:
                return addr
    return ""


class ZoomInfoLeadSource:

    @classmethod
    def info(cls) -> PluginInfo:
        return PluginInfo(
            name="zoominfo",
            display_name="ZoomInfo",
            category=PluginCategory.LEAD_SOURCE,
            auth_method=AuthMethod.API_KEY,
            requires_credentials=["username", "password"],
            homepage="https://www.zoominfo.com",
            docs_url="https://api-docs.zoominfo.com/",
            description=(
                "The enterprise B2B database — deepest US contact + "
                "company coverage. Search finds people by title, "
                "seniority, location and industry; revealing an email "
                "costs one enrich credit per contact (find_email). "
                "Paste your ZoomInfo API username and password "
                "(requires an Enterprise API subscription)."
            ),
            free_tier=False,
        )

    def is_available(self, member_email: str) -> bool:
        return has_credential(member_email, "zoominfo")

    def search(self, member_email: str, icp: ICPFilters,
               count: int = 50) -> list[Lead]:
        auth = _authenticate(member_email)
        if "error" in auth:
            print(f"[narada/zoominfo] search auth failed: {auth['error']}",
                  flush=True)
            return []
        jwt = auth["jwt"]
        base_body = _icp_to_search_body(icp)
        # Legacy pagination 400s past 1000 total records.
        count = min(count, ZOOMINFO_MAX_TOTAL)
        # rpp must stay CONSTANT across pages — page N with a smaller
        # rpp re-reads earlier offsets and returns duplicate leads.
        rpp = max(1, min(ZOOMINFO_MAX_RPP, count))
        out: list[Lead] = []
        page = 1
        while len(out) < count:
            body = dict(base_body)
            body.update({"rpp": rpp, "page": page,
                         "sortBy": "contactAccuracyScore",
                         "sortOrder": "desc"})
            resp = _call(member_email, jwt, "/search/contact", body)
            if "error" in resp:
                print(f"[narada/zoominfo] search failed: {resp['error']}",
                      flush=True)
                break
            rows = resp.get("data")
            if not isinstance(rows, list):
                break
            for p in rows:
                if not isinstance(p, dict):
                    continue
                company = p.get("company")
                if not isinstance(company, dict):
                    company = {}
                out.append(Lead(
                    first_name=_str_field(p, "firstName")[:120],
                    last_name=_str_field(p, "lastName")[:120],
                    email="",  # search returns stubs; enrich reveals email
                    company=(_str_field(company, "name")
                             or _str_field(p, "companyName"))[:255],
                    company_domain=(_str_field(company, "website")
                                    or _str_field(p, "companyWebsite"))[:255],
                    title=_str_field(p, "jobTitle")[:255],
                    linkedin_url="",  # not in search stubs
                    source="zoominfo",
                    source_metadata={
                        "zi_person_id": p.get("id"),
                        "zi_company_id": company.get("id"),
                        "has_email": p.get("hasEmail"),
                        "accuracy_score": p.get("contactAccuracyScore"),
                    },
                ))
                if len(out) >= count:
                    break
            if len(rows) < rpp:   # last page reached
                break
            page += 1
        return out[:count]

    def find_email(self, member_email: str, first_name: str,
                   last_name: str, company_domain: str) -> str | None:
        """Search by name + company website to pin the ZoomInfo person
        id, then enrich that one record (1 enrich credit)."""
        if not ((first_name or last_name) and company_domain):
            return None
        auth = _authenticate(member_email)
        if "error" in auth:
            print(f"[narada/zoominfo] find_email auth failed: "
                  f"{auth['error']}", flush=True)
            return None
        jwt = auth["jwt"]
        full_name = f"{first_name} {last_name}".strip()
        # Docs want companyWebsite "in http://www.example.com format".
        website = (company_domain if "://" in company_domain
                   else f"http://{company_domain}")
        resp = _call(member_email, jwt, "/search/contact", {
            "fullName": full_name,
            "companyWebsite": website,
            "rpp": 1,
            "page": 1,
        })
        if "error" in resp:
            print(f"[narada/zoominfo] find_email search failed: "
                  f"{resp['error']}", flush=True)
            return None
        rows = resp.get("data")
        first = rows[0] if isinstance(rows, list) and rows else None
        person_id = first.get("id") if isinstance(first, dict) else None
        if not person_id:
            return None
        enriched = _call(member_email, jwt, "/enrich/contact", {
            "matchPersonInput": [{"personId": person_id}],
            "outputFields": ["id", "firstName", "lastName", "email",
                             "jobTitle", "companyName", "companyWebsite"],
        })
        if "error" in enriched:
            print(f"[narada/zoominfo] find_email enrich failed: "
                  f"{enriched['error']}", flush=True)
            return None
        return _enrich_email(enriched) or None

    def cost_estimate(self, action: str, count: int = 1) -> dict:
        n = max(1, count)
        credits = n if action in ("find_email", "enrich", "lookup") else 0
        return {
            "credits": credits,
            "approx_usd": round(credits * 0.25, 4),
            "notes": ("ZoomInfo: search returns stubs without emails "
                      "(plan-included); each email reveal (find_email) "
                      "burns 1 enrich credit. Credit pricing is "
                      "contract-specific — ~$0.25 is a mid-market "
                      "estimate, check your ZoomInfo agreement."),
        }


# Auto-register
try:
    register(ZoomInfoLeadSource())
except Exception as _e:
    print(f"[narada/zoominfo] register failed: "
          f"{type(_e).__name__}: {_e}", flush=True)
