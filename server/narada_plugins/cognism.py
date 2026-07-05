"""Cognism plugin — implements the LeadSource protocol (direct API).

Cognism (cognism.com) is a B2B sales-intelligence platform with
GDPR-conscious, phone-verified contact data (Diamond Data®); strongest
coverage in EMEA. Its Search API filters contacts by job title,
seniority, region and account firmographics.

Credit model quirk: searching is credit-free (previews only count
toward a preview limit) — full contact details (emails/phones) come
from a separate Redeem call that DOES consume credits. Search previews
NEVER contain the actual email/linkedin/domain values, only has*
booleans (hasEmail, hasLinkedinUrl, ...) plus name/title/company — so
`search` returns leads without emails (hasEmail is kept in
source_metadata) and `find_email` runs a targeted search + one redeem.

Verified against the public developer portal (developers.cognism.com,
a Postman-published doc) + help-centre articles: search body fields
(jobTitles, seniority enum, regions/countries, account.domains,
account.industries, account.headcount, ...), redeem body
{"redeemIds": [...]} (1-20 ids), and the redeem response envelope
{"total": N, "result": [...]} — note `result` singular, vs `results`
for search. Redeemed contacts carry email as an OBJECT
{"address": ..., "quality": ...}. Not mapped from ICPFilters:
company_funding_stage (Cognism filters funding by event date/series,
not stage).

Auth: header `Authorization: Bearer <key>` (key generated in the
Cognism app under Settings → API). Base https://app.cognism.com/api.
Docs: https://developers.cognism.com/
Slug `cognism`; paste the API key as `api_key`.
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


COGNISM_API_BASE = "https://app.cognism.com/api"
COGNISM_TIMEOUT = 30
# app.cognism.com sits behind Cloudflare (help.cognism.com 403s the
# default urllib UA outright) — send a browser UA to avoid the
# client-fingerprint block, same trick as the RocketReach plugin.
COGNISM_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)
# Cognism accepts 20-100 records per search request; below 20 risks a
# 400, and search is credit-free, so we request at least the minimum
# page and slice locally to the caller's count.
COGNISM_MIN_PAGE = 20
COGNISM_MAX_PAGE = 100
COGNISM_MAX_PAGES = 10   # hard stop for the pagination loop

# Narada seniority slugs → Cognism `seniority` enum. The documented
# allowed values are exactly: Manager, Director, Partner, CXO, Owner,
# VP (developers.cognism.com, Search Contacts body). Values outside
# the enum risk a 400 for the whole request, so unknown slugs are
# DROPPED (power users can still use icp.raw).
COGNISM_SENIORITY_MAP = {
    "c_suite": "CXO",
    "c-suite": "CXO",
    "c_level": "CXO",
    "c-level": "CXO",
    "cxo": "CXO",
    "founder": "Owner",
    "owner": "Owner",
    "partner": "Partner",
    "vp": "VP",
    "vice_president": "VP",
    "director": "Director",
    "manager": "Manager",
}
COGNISM_SENIORITY_ALLOWED = {
    "Manager", "Director", "Partner", "CXO", "Owner", "VP",
}
# Slugs that aren't Cognism seniorities but map cleanly onto its
# separate `managementLevel` enum (Entry-Level, Team-Lead, Experienced
# Staff, Executive-Level, Senior Leadership, Middle-Management, CxO).
COGNISM_MANAGEMENT_LEVEL_MAP = {
    "entry": "Entry-Level",
    "junior": "Entry-Level",
    "intern": "Entry-Level",
    "senior": "Experienced Staff",
    "head": "Senior Leadership",
}
# ICP `locations` may be world regions, countries or cities. Cognism
# has SEPARATE `regions` and `countries` body fields with enum values;
# a country name inside `regions` matches nothing. Best-effort split:
# known world-region tokens → regions, everything else → countries.
COGNISM_REGION_TOKENS = {
    "emea", "apac", "nam", "amer", "latam", "apj", "anz", "dach",
    "nordics", "benelux", "europe", "asia", "africa", "oceania",
    "north america", "south america", "latin america", "middle east",
    "western europe", "eastern europe", "southeast asia",
}


def _api_key(member_email: str) -> str | None:
    cred = get_credential(member_email, "cognism")
    if not cred:
        return None
    return (cred.get("api_key") or "").strip() or None


def _call(member_email: str, method: str, path: str,
          body: dict | None = None, params: dict | None = None) -> dict:
    """Call Cognism's API. Returns parsed JSON, or {'error': ...}.
    Never raises."""
    api_key = _api_key(member_email)
    if not api_key:
        return {"error": "no Cognism credential for this member"}
    url = f"{COGNISM_API_BASE}{path}"
    if params:
        url += "?" + urlencode(params)
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = Request(url, data=data, method=method, headers={
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": COGNISM_USER_AGENT,
    })
    try:
        with urlopen(req, timeout=COGNISM_TIMEOUT) as r:
            text = r.read().decode("utf-8")
            parsed = json.loads(text) if text else {}
            # Some endpoints may return a bare list — normalise.
            return parsed if isinstance(parsed, dict) else {"results": parsed}
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
        touch_last_used(member_email, "cognism")


def _first_of(d: dict, *keys: str) -> str:
    """First non-empty string value among candidate keys."""
    for k in keys:
        v = d.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return ""


def _contact_id(c: dict) -> str:
    """The id used to redeem a search preview into a full contact."""
    for k in ("redeemId", "id", "contactId"):
        v = c.get(k)
        if v not in (None, ""):
            return str(v)
    return ""


def _contact_email(c: dict) -> str:
    """Best email from a Cognism contact. Redeemed contacts carry
    `email` as an OBJECT: {"address": ..., "quality": "HIGH_PLUS"}
    (per the documented redeem response); tolerate plain strings and
    an `emails` list too."""
    e = c.get("email")
    if isinstance(e, dict):
        addr = _first_of(e, "address", "email", "value")
        if addr:
            return addr.lower()
    direct = _first_of(c, "email", "workEmail", "primaryEmail")
    if direct:
        return direct.lower()
    for e in (c.get("emails") or []):
        if isinstance(e, str) and e.strip():
            return e.strip().lower()
        if isinstance(e, dict):
            addr = _first_of(e, "email", "address", "value")
            if addr:
                return addr.lower()
    return ""


def _extract_contacts(resp: dict) -> list[dict]:
    """Pull the contact list out of a search/redeem response.
    Search responds {"results": [...]} — redeem responds
    {"total": N, "result": [...]} (singular!), so check both."""
    for k in ("results", "result", "contacts", "data", "items"):
        v = resp.get(k)
        if isinstance(v, list):
            return [c for c in v if isinstance(c, dict)]
    return []


def _contact_to_lead(c: dict) -> Lead:
    account = c.get("account") if isinstance(c.get("account"), dict) else {}
    company = (_first_of(account, "name", "companyName")
               or _first_of(c, "companyName", "accountName"))
    domain = (_first_of(account, "domain", "website")
              or _first_of(c, "companyDomain", "companyWebsite"))
    if not domain:
        domains = account.get("domains") or []
        if domains and isinstance(domains[0], str):
            domain = domains[0].strip()
    domain = (domain.replace("https://", "").replace("http://", "")
              .strip("/").lower())
    return Lead(
        first_name=_first_of(c, "firstName", "first_name")[:120],
        last_name=_first_of(c, "lastName", "last_name")[:120],
        email=_contact_email(c)[:320],
        company=company[:255],
        company_domain=domain[:255],
        title=_first_of(c, "jobTitle", "title", "job_title")[:255],
        # redeemed contacts use `linkedinURL` (capital URL)
        linkedin_url=_first_of(c, "linkedinUrl", "linkedinURL",
                               "linkedin", "linkedin_url")[:512],
        source="cognism",
        source_metadata={
            "cognism_id": _contact_id(c),
            # preview flag — tells the core whether a redeem
            # (find_email) can actually yield an email for this lead
            "has_email": bool(c.get("hasEmail")),
        },
    )


def _icp_to_body(icp: ICPFilters) -> dict:
    """Map Narada's ICPFilters to Cognism's contact-search body
    (field names per developers.cognism.com Search Contacts).
    Unmapped: company_funding_stage — see module docstring."""
    body: dict = {}
    if icp.roles:
        body["jobTitles"] = list(icp.roles)
    seniorities: list[str] = []
    mgmt_levels: list[str] = []
    for s in icp.seniority:
        slug = (s or "").strip().lower()
        if not slug:
            continue
        mapped = COGNISM_SENIORITY_MAP.get(slug)
        if mapped:
            seniorities.append(mapped)
        elif slug.title() in COGNISM_SENIORITY_ALLOWED:
            seniorities.append(slug.title())
        elif slug in COGNISM_MANAGEMENT_LEVEL_MAP:
            mgmt_levels.append(COGNISM_MANAGEMENT_LEVEL_MAP[slug])
        # anything else: dropped — outside Cognism's documented enums
    if seniorities:
        body["seniority"] = sorted(set(seniorities))
    if mgmt_levels:
        body["managementLevel"] = sorted(set(mgmt_levels))
    regions: list[str] = []
    countries: list[str] = []
    for loc in icp.locations:
        loc = (loc or "").strip()
        if not loc:
            continue
        if loc.lower() in COGNISM_REGION_TOKENS:
            regions.append(loc.upper() if len(loc) <= 5 else loc.title())
        else:
            countries.append(loc)
    if regions:
        body["regions"] = regions
    if countries:
        body["countries"] = countries
    account: dict = {}
    if icp.industries:
        account["industries"] = list(icp.industries)
    if icp.company_size_min or icp.company_size_max:
        headcount: dict = {}
        if icp.company_size_min:
            headcount["from"] = int(icp.company_size_min)
        if icp.company_size_max:
            headcount["to"] = int(icp.company_size_max)
        account["headcount"] = headcount
    if icp.technologies:
        account["technologies"] = list(icp.technologies)
    if icp.keywords:
        account["keywords"] = list(icp.keywords)
    if account:
        body["account"] = account
    if icp.raw:
        body.update(icp.raw)
    return body


class CognismLeadSource:

    @classmethod
    def info(cls) -> PluginInfo:
        return PluginInfo(
            name="cognism",
            display_name="Cognism",
            category=PluginCategory.LEAD_SOURCE,
            auth_method=AuthMethod.API_KEY,
            requires_credentials=["api_key"],
            homepage="https://www.cognism.com",
            docs_url="https://developers.cognism.com/",
            description=(
                "GDPR-conscious B2B contact data with phone-verified "
                "'Diamond' records; strongest in Europe/EMEA. Searching "
                "by title, seniority, location and industry is credit-"
                "free (previews); revealing an email (find_email) redeems "
                "1 credit from your Cognism package. Paste the API key "
                "from Settings → API in the Cognism app."
            ),
            free_tier=False,
        )

    def is_available(self, member_email: str) -> bool:
        return has_credential(member_email, "cognism")

    def search(self, member_email: str, icp: ICPFilters,
               count: int = 50) -> list[Lead]:
        """Credit-free preview search. Pages forward with
        lastReturnedKey until `count` leads are collected. Previews
        carry name/title/company + has* flags but never the actual
        email — use find_email to redeem it (source_metadata.has_email
        says whether a redeem can succeed)."""
        body = _icp_to_body(icp)
        out: list[Lead] = []
        last_key = ""
        for _page in range(COGNISM_MAX_PAGES):
            if len(out) >= count:
                break
            params: dict = {"indexSize": max(
                COGNISM_MIN_PAGE, min(count - len(out), COGNISM_MAX_PAGE))}
            if last_key:
                params["lastReturnedKey"] = last_key
            resp = _call(member_email, "POST",
                         "/search/contact/search", body, params)
            if "error" in resp:
                print(f"[narada/cognism] search failed: {resp['error']}",
                      flush=True)
                break
            contacts = _extract_contacts(resp)
            if not contacts:
                break
            for c in contacts:
                out.append(_contact_to_lead(c))
                if len(out) >= count:
                    break
            last_key = str(resp.get("lastReturnedKey")
                           or resp.get("lastKey") or "")
            if not last_key:
                break
        return out[:count]

    def find_email(self, member_email: str, first_name: str,
                   last_name: str, company_domain: str) -> str | None:
        """Targeted search by name + company domain, then redeem the
        best match (1 credit). Previews only expose a hasEmail flag,
        so we redeem the first contact whose preview says an email
        exists — and skip the redeem entirely (saving the credit)
        when every preview says hasEmail=false. Returns None when no
        match."""
        if not ((first_name or last_name) and company_domain):
            return None
        body: dict = {"account": {"domains": [company_domain]}}
        if first_name:
            body["firstName"] = first_name
        if last_name:
            body["lastName"] = last_name
        resp = _call(member_email, "POST", "/search/contact/search",
                     body, params={"indexSize": COGNISM_MIN_PAGE})
        if "error" in resp:
            return None
        contacts = _extract_contacts(resp)
        if not contacts:
            return None
        top = next((c for c in contacts if c.get("hasEmail")), None)
        if top is None:
            if any("hasEmail" in c for c in contacts):
                return None   # Cognism has no email — don't burn a credit
            top = contacts[0]  # entitlement doesn't expose the flag
        # Defensive: previews shouldn't carry the address, but if the
        # entitlement ever includes it, don't spend the credit.
        addr = _contact_email(top)
        if addr:
            return addr
        cid = _contact_id(top)
        if not cid:
            return None
        redeemed = _call(member_email, "POST", "/search/contact/redeem",
                         {"redeemIds": [cid]})
        if "error" in redeemed:
            return None
        # Redeem responds {"total": N, "result": [...]} — parsed by
        # _extract_contacts; email arrives as {"address": ..., ...}.
        for rec in _extract_contacts(redeemed):
            addr = _contact_email(rec)
            if addr:
                return addr
        # Some responses return the single contact object directly.
        addr = _contact_email(redeemed)
        return addr or None

    def cost_estimate(self, action: str, count: int = 1) -> dict:
        n = max(1, count)
        credits = n if action in ("find_email", "redeem", "lookup") else 0
        return {
            "credits": credits,
            "approx_usd": 0.0,
            "notes": ("Cognism: contact search/previews are credit-free "
                      "(they count toward your preview limit); each email "
                      "reveal (find_email → redeem) consumes 1 credit from "
                      "your subscription package — no per-credit USD list "
                      "price."),
        }


# Auto-register
try:
    register(CognismLeadSource())
except Exception as _e:
    print(f"[narada/cognism] register failed: "
          f"{type(_e).__name__}: {_e}", flush=True)
