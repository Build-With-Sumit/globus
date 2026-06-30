"""Prospeo plugin — implements both LeadSource and Verifier protocols.

Prospeo (prospeo.io) provides B2B lead search, email finding, and
verification in one subscription. Sumit picked this as the v1 lead
source. Starter tier ($37/user/mo) gives 2K credits/month + API access.

Auth: API key, paste into /members/narada/credentials. Per-member
isolation via globus_narada_credentials.

Credit model (verify ahead of every action so we don't surprise the
marketer):
  - 1 credit per email lookup
  - 1 credit per verification
  - 1 credit per company search result
Free tier: 100 credits/mo. Starter: 2K/mo. Growth: 5K/mo.
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
    VerifyResult, VerifyStatus,
)


PROSPEO_API_BASE = "https://api.prospeo.io"
PROSPEO_DEFAULT_TIMEOUT = 30  # seconds per HTTP call


def _api_key(member_email: str) -> str | None:
    cred = get_credential(member_email, "prospeo")
    if not cred:
        return None
    key = (cred.get("api_key") or "").strip()
    return key or None


def _post(member_email: str, path: str, body: dict) -> dict:
    """POST to Prospeo's REST API + return parsed JSON. Never raises —
    on error returns {'error': '...'} so callers can dispatch cleanly."""
    api_key = _api_key(member_email)
    if not api_key:
        return {"error": "no Prospeo credential for this member"}
    url = f"{PROSPEO_API_BASE}{path}"
    data = json.dumps(body).encode("utf-8")
    req = Request(url, data=data, method="POST",
                  headers={"Content-Type": "application/json",
                           "X-KEY": api_key})
    try:
        with urlopen(req, timeout=PROSPEO_DEFAULT_TIMEOUT) as r:
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
    touch_last_used(member_email, "prospeo")
    return resp


# ─────────────────────────────────────────────────────────────────────
# LeadSource implementation
# ─────────────────────────────────────────────────────────────────────

class ProspeoLeadSource:

    @classmethod
    def info(cls) -> PluginInfo:
        return PluginInfo(
            name="prospeo",
            display_name="Prospeo",
            category=PluginCategory.LEAD_SOURCE,
            auth_method=AuthMethod.API_KEY,
            requires_credentials=["api_key"],
            homepage="https://prospeo.io",
            docs_url="https://docs.prospeo.io",
            description=(
                "B2B lead search + email find + verify in one. "
                "Starter $37/user/mo, 2K credits/mo. Free tier: 100. "
                "Combine with the prospeo-verifier plugin for verification "
                "from the same subscription."
            ),
            free_tier=True,
        )

    def is_available(self, member_email: str) -> bool:
        return has_credential(member_email, "prospeo")

    def search(self, member_email: str, icp: ICPFilters,
                count: int = 50) -> list[Lead]:
        """Prospeo's people-search endpoint. Maps ICPFilters to their
        filter shape. Caller passes count; we never silently exceed
        (credit burn protection)."""
        # Prospeo's filter shape uses keys like `job_titles`,
        # `industries`, `company_size`, `locations`. Map best-effort;
        # we pass `raw` straight through if the marketer set it.
        body = {
            "max_results": max(1, min(count, 100)),
        }
        if icp.roles:
            body["job_titles"] = icp.roles
        if icp.industries:
            body["industries"] = icp.industries
        if icp.locations:
            body["locations"] = icp.locations
        if icp.company_size_min or icp.company_size_max:
            body["company_size"] = {
                "min": icp.company_size_min or 1,
                "max": icp.company_size_max or 999999,
            }
        if icp.keywords:
            body["keywords"] = icp.keywords
        if icp.technologies:
            body["technologies"] = icp.technologies
        # Marketer-supplied escape hatch
        if icp.raw:
            body.update(icp.raw)

        resp = _post(member_email, "/people-search", body)
        if "error" in resp:
            print(f"[narada/prospeo] search failed: {resp['error']}",
                  flush=True)
            return []
        people = resp.get("people") or resp.get("results") or []
        out: list[Lead] = []
        for p in people[:count]:
            out.append(Lead(
                first_name=(p.get("first_name") or "")[:120],
                last_name=(p.get("last_name") or "")[:120],
                email=(p.get("email") or "")[:320].lower(),
                company=(p.get("company") or "")[:255],
                company_domain=(p.get("company_domain")
                                or p.get("company_website") or "")[:255],
                title=(p.get("title") or p.get("job_title") or "")[:255],
                linkedin_url=(p.get("linkedin") or
                              p.get("linkedin_url") or "")[:512],
                source="prospeo",
                source_metadata=p,
            ))
        return out

    def find_email(self, member_email: str, first_name: str,
                    last_name: str, company_domain: str) -> str | None:
        """Prospeo's email-finder. 1 credit per call. Returns lowercase
        email or None if nothing found."""
        if not (first_name and last_name and company_domain):
            return None
        resp = _post(member_email, "/email-finder", {
            "first_name": first_name,
            "last_name": last_name,
            "company": company_domain,
        })
        if "error" in resp:
            return None
        email_addr = (resp.get("email")
                      or (resp.get("response") or {}).get("email") or "")
        email_addr = email_addr.strip().lower()
        return email_addr or None

    def cost_estimate(self, action: str, count: int = 1) -> dict:
        # All Prospeo actions are 1 credit; we just sum.
        credits = max(1, count)
        return {
            "credits": credits,
            "approx_usd": credits * 0.018,  # Starter tier: $37 / 2000 cr
            "notes": ("Prospeo Starter tier: 1 credit per "
                       "search-result / email-find / verify. "
                       "2K credits / month included at $37/user."),
        }


# ─────────────────────────────────────────────────────────────────────
# Verifier implementation — separate slug so the campaign builder can
# pair Prospeo lead-source with a different verifier (e.g. NeverBounce)
# if the marketer prefers.
# ─────────────────────────────────────────────────────────────────────

class ProspeoVerifier:

    @classmethod
    def info(cls) -> PluginInfo:
        return PluginInfo(
            name="prospeo_verify",
            display_name="Prospeo (Email Verify)",
            category=PluginCategory.VERIFIER,
            auth_method=AuthMethod.API_KEY,
            requires_credentials=["api_key"],
            homepage="https://prospeo.io",
            docs_url="https://docs.prospeo.io",
            description=(
                "Email deliverability check. Uses the same Prospeo "
                "subscription as the lead-source plugin (1 credit per "
                "verify). Pair with Prospeo lead-source for a one-vendor "
                "find+verify flow."
            ),
            free_tier=True,
        )

    def is_available(self, member_email: str) -> bool:
        return has_credential(member_email, "prospeo")

    def verify(self, member_email: str, email: str) -> VerifyResult:
        if not email:
            return VerifyResult(email="", status=VerifyStatus.INVALID,
                                  raw={"reason": "empty email"})
        resp = _post(member_email, "/email-verifier", {"email": email})
        if "error" in resp:
            return VerifyResult(email=email, status=VerifyStatus.ERROR,
                                  raw=resp)
        inner = resp.get("response") if isinstance(resp.get("response"), dict) else resp
        raw_status = (inner.get("status") or inner.get("deliverability")
                       or inner.get("result") or "").lower()
        mapping = {
            "deliverable": VerifyStatus.VALID,
            "valid":       VerifyStatus.VALID,
            "undeliverable": VerifyStatus.INVALID,
            "invalid":      VerifyStatus.INVALID,
            "risky":        VerifyStatus.RISKY,
            "unknown":      VerifyStatus.UNKNOWN,
            "catch_all":    VerifyStatus.RISKY,
            "catch-all":    VerifyStatus.RISKY,
        }
        status = mapping.get(raw_status, VerifyStatus.UNKNOWN)
        score = inner.get("score") or inner.get("confidence") or 0
        try:
            confidence = float(score) / (100 if float(score) > 1 else 1)
        except (TypeError, ValueError):
            confidence = 0.0
        return VerifyResult(
            email=email,
            status=status,
            confidence=confidence,
            is_catch_all=bool(inner.get("catch_all")
                              or inner.get("catch-all")),
            is_disposable=bool(inner.get("disposable")),
            is_role=bool(inner.get("role_account") or inner.get("role")),
            raw=inner,
        )

    def cost_estimate(self, count: int = 1) -> dict:
        credits = max(1, count)
        return {"credits": credits,
                "approx_usd": credits * 0.018,
                "notes": "1 Prospeo credit per verify."}


# Module-level registration. Both classes auto-register on import.
try:
    register(ProspeoLeadSource())
    register(ProspeoVerifier())
except Exception as _e:
    print(f"[narada/prospeo] register failed: {type(_e).__name__}: {_e}",
          flush=True)
