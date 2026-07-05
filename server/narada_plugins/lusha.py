"""Lusha plugin — implements the LeadSource protocol (direct API, V3).

Lusha (lusha.com) is a B2B contact-data platform: ICP prospecting
(title, seniority, location, industry, company size) plus person
enrichment (email from name + company). Coverage is strongest for
US/EU decision-makers.

Flow quirk: Lusha's V3 prospecting search returns contact *previews*
only (name/title/company + a `has` list — no emails). Emails cost
credits and come from a second call: POST /v3/contacts/enrich with the
stable contact ids. So `search` runs search → enrich per page and
returns leads with emails where Lusha has them (previews are kept even
if enrich fails — name+company still useful). We request
reveal=["emails"] only — phone reveals cost 5 credits each and Narada
doesn't need them. `find_email` uses POST /v3/contacts/search-and-enrich
(name + company lookup), the V3 replacement for GET /v2/person.

Why V3: the V2 endpoints (POST /prospecting/contact/search|enrich,
GET /v2/person) carry Sunset headers since 2026-05-18 and stop
responding 2026-11-18 — no new integration should target them.

Filter coverage gaps: keywords + funding stage aren't mapped (no Lusha
equivalent); locations map country-level only; seniority maps Lusha's
documented 1-10 enum (Intern…Founder). `icp.raw` is merged verbatim
into the request body for full Lusha-native filter control.

Auth: header `api_key: <key>`. Base https://api.lusha.com (V3 paths).
Docs: https://docs.lusha.com — rate limit 25 req/s.
Slug `lusha`; paste the API key as `api_key`.
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


LUSHA_API_BASE = "https://api.lusha.com"
LUSHA_TIMEOUT = 30
LUSHA_USER_AGENT = "narada-outbound/1.0"
# V3 prospecting pages are capped at 50 results; we page + slice so a
# count of 50 still burns exactly 50 email-reveal credits, never more.
LUSHA_PAGE_SIZE_MAX = 50
LUSHA_MAX_PAGES = 5
LUSHA_ENRICH_BATCH_MAX = 100     # V3 enrich accepts up to 100 ids

# Lusha's seniority filter wants numeric ids. Official V3 enum
# (docs.lusha.com Prospecting seniority reference):
#   1 Intern, 2 Entry, 3 Associate, 4 Senior, 5 Manager, 6 Director,
#   7 Vice President, 8 C-Level/Executive, 9 Partner, 10 Founder.
LUSHA_SENIORITY_IDS = {
    "intern": 1,
    "entry": 2,
    "junior": 2,
    "associate": 3,
    "senior": 4,
    "manager": 5,
    "director": 6,
    "vp": 7,
    "vice_president": 7,
    "vice president": 7,
    "c_suite": 8,
    "c-suite": 8,
    "c_level": 8,
    "c-level": 8,
    "cxo": 8,
    "executive": 8,
    "partner": 9,
    "founder": 10,
    "owner": 10,
}
# Director / VP / C-suite — the usual outbound target, used when the
# ICP carries no mappable filter at all (Lusha rejects an empty set).
LUSHA_DECISION_MAKER_IDS = [6, 7, 8]


def _api_key(member_email: str) -> str | None:
    cred = get_credential(member_email, "lusha")
    if not cred:
        return None
    return (cred.get("api_key") or "").strip() or None


def _call(member_email: str, method: str, path: str,
          body: dict | None = None) -> dict:
    """Call Lusha's API. Returns parsed JSON, or {'error': ...}.
    Never raises."""
    api_key = _api_key(member_email)
    if not api_key:
        return {"error": "no Lusha credential for this member"}
    url = f"{LUSHA_API_BASE}{path}"
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = Request(url, data=data, method=method, headers={
        "api_key": api_key,
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": LUSHA_USER_AGENT,
    })
    try:
        with urlopen(req, timeout=LUSHA_TIMEOUT) as r:
            text = r.read().decode("utf-8")
            parsed = json.loads(text) if text else {}
    except HTTPError as e:
        body_txt = ""
        try:
            body_txt = e.read().decode("utf-8", "replace")[:300]
        except Exception:
            pass
        return {"error": f"HTTP {e.code}: {body_txt}"}
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}
    try:
        touch_last_used(member_email, "lusha")
    except Exception as e:
        print(f"[narada/lusha] touch_last_used failed: "
              f"{type(e).__name__}: {e}", flush=True)
    if not isinstance(parsed, dict):
        return {"results": parsed}
    # Lusha error envelope: {"statusCode": 4xx, "message": "..."}
    status = parsed.get("statusCode")
    if isinstance(status, int) and status >= 400:
        return {"error": f"API {status}: "
                         f"{str(parsed.get('message', ''))[:300]}"}
    return parsed


def _pick_email(contact: dict) -> str:
    """Pick the best email from a V3 contact record. Enriched contacts
    carry `emails`: [{email, type, confidence, ...}] — prefer work
    emails over personal."""
    emails = contact.get("emails") or contact.get("emailAddresses") or []
    if isinstance(emails, (str, dict)):
        emails = [emails]
    work: list[str] = []
    other: list[str] = []
    for e in emails:
        if isinstance(e, str):
            addr, etype = e, ""
        elif isinstance(e, dict):
            addr = e.get("email") or e.get("emailAddress") or ""
            etype = e.get("type") or e.get("emailType") or ""
        else:
            continue
        addr = addr.strip().lower()
        if not addr:
            continue
        (work if etype in ("work", "professional") else other).append(addr)
    if work:
        return work[0]
    if other:
        return other[0]
    fallback = contact.get("email")
    return fallback.strip().lower() if isinstance(fallback, str) else ""


def _size_ranges(size_min: int, size_max: int) -> list[dict]:
    """Map the ICP headcount range onto Lusha's `sizes` filter. V3
    accepts arbitrary {min, max} ranges (docs example: 51-500);
    omitting max = open-ended."""
    if size_min <= 0 and size_max <= 0:
        return []
    lo = max(size_min, 1)
    hi = size_max if size_max > 0 else 0
    if hi and hi < lo:                    # nonsensical range — swap
        lo, hi = max(hi, 1), lo
    rng: dict = {"min": lo}
    if hi:
        rng["max"] = hi
    return [rng]


def _icp_filters(icp: ICPFilters) -> dict:
    """Map ICPFilters onto Lusha V3's filters.{contacts,companies}
    .include shape. Unsupported filters are dropped (documented in the
    module docstring)."""
    contact_inc: dict = {}
    company_inc: dict = {}
    if icp.roles:
        contact_inc["jobTitles"] = list(icp.roles)
    sen_ids = sorted({LUSHA_SENIORITY_IDS[s.strip().lower()]
                      for s in icp.seniority
                      if s.strip().lower() in LUSHA_SENIORITY_IDS})
    if sen_ids:
        contact_inc["seniority"] = sen_ids
    if icp.locations:
        contact_inc["locations"] = [{"country": loc}
                                    for loc in icp.locations]
    if icp.industries:
        company_inc["industriesLabels"] = list(icp.industries)
    if icp.technologies:
        company_inc["technologies"] = list(icp.technologies)
    sizes = _size_ranges(icp.company_size_min, icp.company_size_max)
    if sizes:
        company_inc["sizes"] = sizes
    if not contact_inc and not company_inc:
        contact_inc["seniority"] = list(LUSHA_DECISION_MAKER_IDS)
    filters: dict = {}
    if contact_inc:
        filters["contacts"] = {"include": contact_inc}
    if company_inc:
        filters["companies"] = {"include": company_inc}
    return filters


def _enrich(member_email: str, stubs: list[dict]) -> dict[str, dict]:
    """Reveal emails for searched previews (1 credit per revealed
    email). Skips previews whose `has` list says Lusha holds no email
    — no point paying to enrich those. Returns {contact_id: enriched
    record}; empty on any failure so search can still return the bare
    previews."""
    ids: list[str] = []
    for s in stubs:
        cid = s.get("id")
        if cid is None:
            continue
        has = s.get("has")
        if isinstance(has, list) and "emails" not in has:
            continue
        ids.append(str(cid))
    if not ids:
        return {}
    resp = _call(member_email, "POST", "/v3/contacts/enrich",
                 {"ids": ids[:LUSHA_ENRICH_BATCH_MAX],
                  "reveal": ["emails"]})
    if "error" in resp:
        print(f"[narada/lusha] enrich failed: {resp['error']}", flush=True)
        return {}
    out: dict[str, dict] = {}
    for c in resp.get("results") or []:
        if not isinstance(c, dict) or c.get("error"):
            continue
        cid = c.get("id")
        if cid is not None:
            out[str(cid)] = c
    return out


def _company_of(rec: dict) -> dict:
    comp = rec.get("company")
    return comp if isinstance(comp, dict) else {}


def _lead_from_contact(stub: dict, enriched: dict | None) -> Lead:
    """Merge a search preview with its enriched record into a Lead.
    V3 nests title under jobTitle.title, company under company.{name,
    domain,id} and LinkedIn under socialLinks.linkedin."""
    enr = enriched or {}
    merged = {**stub, **{k: v for k, v in enr.items() if v}}
    company = {**_company_of(stub),
               **{k: v for k, v in _company_of(enr).items() if v}}
    job = merged.get("jobTitle")
    title = (job.get("title") or "") if isinstance(job, dict) else (job or "")
    first = (merged.get("firstName") or "").strip()
    last = (merged.get("lastName") or "").strip()
    full = (merged.get("fullName") or merged.get("name") or "").strip()
    if not (first or last) and full:
        parts = full.split(" ")
        first, last = parts[0], " ".join(parts[1:])
    social = merged.get("socialLinks")
    linkedin = (social.get("linkedin") or "") if isinstance(social, dict) \
        else ""
    return Lead(
        first_name=first[:120],
        last_name=last[:120],
        email=_pick_email(merged)[:320],
        company=(company.get("name") or "")[:255],
        company_domain=(company.get("domain") or "")[:255],
        title=(title or "")[:255],
        linkedin_url=linkedin[:512],
        source="lusha",
        source_metadata={"lusha_contact_id": stub.get("id"),
                         "company_id": company.get("id")},
    )


class LushaLeadSource:

    @classmethod
    def info(cls) -> PluginInfo:
        return PluginInfo(
            name="lusha",
            display_name="Lusha",
            category=PluginCategory.LEAD_SOURCE,
            auth_method=AuthMethod.API_KEY,
            requires_credentials=["api_key"],
            homepage="https://www.lusha.com",
            docs_url="https://docs.lusha.com",
            description=(
                "B2B contact database with strong US/EU decision-maker "
                "coverage. Search prospects by title, seniority, "
                "location, industry and company size; Lusha then reveals "
                "work emails (1 credit per contact). Can also look up "
                "an email from a name + company domain. Needs a Lusha "
                "plan with API credits; paste your Lusha API key."
            ),
            free_tier=False,
        )

    def is_available(self, member_email: str) -> bool:
        return has_credential(member_email, "lusha")

    def search(self, member_email: str, icp: ICPFilters,
               count: int = 50) -> list[Lead]:
        """V3 prospecting search (cheap previews) + per-page enrich
        (credits). We enrich only up to `count` contacts, exactly —
        per-credit pricing."""
        try:
            filters = _icp_filters(icp)
            size = max(1, min(count, LUSHA_PAGE_SIZE_MAX))
            out: list[Lead] = []
            for page in range(LUSHA_MAX_PAGES):
                if len(out) >= count:
                    break
                body = {"pagination": {"page": page, "size": size},
                        "filters": filters}
                if icp.raw:
                    body.update(icp.raw)
                resp = _call(member_email, "POST",
                             "/v3/contacts/prospecting", body)
                if "error" in resp:
                    print(f"[narada/lusha] search failed: "
                          f"{resp['error']}", flush=True)
                    break
                stubs = [s for s in (resp.get("results") or [])
                         if isinstance(s, dict)]
                if not stubs:
                    break
                take = stubs[:count - len(out)]
                enriched = _enrich(member_email, take)
                for s in take:
                    out.append(_lead_from_contact(
                        s, enriched.get(str(s.get("id")))))
                if len(stubs) < size:
                    break   # Lusha ran out of matches
            return out[:count]
        except Exception as e:
            print(f"[narada/lusha] search crashed: "
                  f"{type(e).__name__}: {e}", flush=True)
            return []

    def find_email(self, member_email: str, first_name: str,
                   last_name: str, company_domain: str) -> str | None:
        """V3 search-and-enrich lookup by name + company (1 credit on
        an email match). Lusha requires BOTH names plus a company name
        or domain."""
        if not (first_name and last_name and company_domain):
            return None
        try:
            entry: dict = {
                "clientReferenceId": "narada-1",
                "firstName": first_name.strip(),
                "lastName": last_name.strip(),
            }
            domain_key = ("companyDomain" if "." in company_domain
                          else "companyName")
            entry[domain_key] = company_domain.strip()
            resp = _call(member_email, "POST",
                         "/v3/contacts/search-and-enrich",
                         {"contacts": [entry], "reveal": ["emails"]})
            if "error" in resp:
                return None
            for rec in resp.get("results") or []:
                if not isinstance(rec, dict) or rec.get("error"):
                    continue
                addr = _pick_email(rec)
                if addr:
                    return addr
            return None
        except Exception as e:
            print(f"[narada/lusha] find_email crashed: "
                  f"{type(e).__name__}: {e}", flush=True)
            return None

    def cost_estimate(self, action: str, count: int = 1) -> dict:
        n = max(1, count)
        return {
            "credits": n,
            "approx_usd": round(n * 0.08, 4),
            "notes": ("Lusha V3: prospecting previews bill as cheap "
                      "api_search usage; revealing an email (search "
                      "auto-enrich + find_email) costs 1 credit per "
                      "contact (~$0.08, plan-dependent). Phone reveals "
                      "(5 credits each) are never requested."),
        }


# Auto-register
try:
    register(LushaLeadSource())
except Exception as _e:
    print(f"[narada/lusha] register failed: "
          f"{type(_e).__name__}: {_e}", flush=True)
