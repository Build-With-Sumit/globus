"""FindyMail plugin — implements the LeadSource protocol (direct API).

FindyMail (findymail.com) is an email-finder: give it a person's name +
company domain (or a LinkedIn URL) and it returns a verified work email.
It charges only for valid emails found, so bounce rates stay low. It has
NO ICP-style people-search API — you can't ask it for "50 CMOs in
fintech" — so `search` always returns [] and the real value here is
`find_email` on prospects sourced elsewhere (CSV upload, LinkedIn,
another lead source).

Auth: header `Authorization: Bearer <key>`. Base https://app.findymail.com/api.
Docs: https://app.findymail.com/docs/
Slug `findymail`; paste the API key as `api_key`.
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


FINDYMAIL_API_BASE = "https://app.findymail.com/api"
FINDYMAIL_TIMEOUT = 30
# app.findymail.com sits behind a CDN that bot-walls the default urllib
# User-Agent (`Python-urllib/x.y`); a normal browser UA passes cleanly.
FINDYMAIL_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)


def _api_key(member_email: str) -> str | None:
    cred = get_credential(member_email, "findymail")
    if not cred:
        return None
    return (cred.get("api_key") or "").strip() or None


def _call(member_email: str, method: str, path: str,
          body: dict | None = None) -> dict:
    """Call FindyMail's API. Returns parsed JSON, or {'error': ...}.
    Never raises."""
    api_key = _api_key(member_email)
    if not api_key:
        return {"error": "no FindyMail credential for this member"}
    url = f"{FINDYMAIL_API_BASE}{path}"
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = Request(url, data=data, method=method, headers={
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": FINDYMAIL_USER_AGENT,
    })
    try:
        with urlopen(req, timeout=FINDYMAIL_TIMEOUT) as r:
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
    if not isinstance(parsed, dict):
        # JSON that isn't an object (array/string) would blow up
        # callers' .get() — normalise to the error shape instead.
        return {"error": f"unexpected response shape: "
                         f"{type(parsed).__name__}"}
    # Only on success — a dead key that 401s forever must not keep
    # refreshing the dashboard's "last used" freshness signal.
    touch_last_used(member_email, "findymail")
    return parsed


def _contact_email(resp: dict) -> str:
    """Pull the email out of a FindyMail search response.
    Shape: {"contact": {"name": ..., "domain": ..., "email": ...}};
    `contact` is null/absent when nothing was found (no credit spent)."""
    contact = resp.get("contact") or {}
    if not isinstance(contact, dict):
        return ""
    return (contact.get("email") or "").strip().lower()


class FindyMailLeadSource:

    @classmethod
    def info(cls) -> PluginInfo:
        return PluginInfo(
            name="findymail",
            display_name="FindyMail",
            category=PluginCategory.LEAD_SOURCE,
            auth_method=AuthMethod.API_KEY,
            requires_credentials=["api_key"],
            homepage="https://findymail.com",
            docs_url="https://app.findymail.com/docs/",
            description=(
                "Finds verified work emails from a name + company domain "
                "or a LinkedIn profile, and only charges for emails it "
                "actually finds. No prospect search — pair it with a lead "
                "list (CSV, LinkedIn, another source) and let it fill in "
                "the emails. Paste your FindyMail API key."
            ),
            free_tier=True,
        )

    def is_available(self, member_email: str) -> bool:
        return has_credential(member_email, "findymail")

    def search(self, member_email: str, icp: ICPFilters,
               count: int = 50) -> list[Lead]:
        """FindyMail has no people-search API — it's an email finder,
        not a prospect database. Always returns []; use `find_email`
        on leads sourced elsewhere."""
        print("[narada/findymail] search not supported — FindyMail is "
              "an email finder, not a prospect database; returning []",
              flush=True)
        return []

    def find_email(self, member_email: str, first_name: str,
                   last_name: str, company_domain: str) -> str | None:
        """Find a verified email from name + domain (1 finder credit,
        charged only if an email is actually found)."""
        if not ((first_name or last_name) and company_domain):
            return None
        name = f"{first_name} {last_name}".strip()
        resp = _call(member_email, "POST", "/search/name", {
            "name": name,
            "domain": company_domain,
        })
        if "error" in resp:
            print(f"[narada/findymail] find_email failed: "
                  f"{resp['error']}", flush=True)
            return None
        return _contact_email(resp) or None

    def find_email_from_linkedin(self, member_email: str,
                                 linkedin_url: str) -> str | None:
        """Extra (non-protocol) helper: find a verified email from a
        LinkedIn profile URL or username. Same pricing as find_email."""
        if not (linkedin_url or "").strip():
            return None
        resp = _call(member_email, "POST", "/search/business-profile", {
            "linkedin_url": linkedin_url.strip(),
        })
        if "error" in resp:
            print(f"[narada/findymail] linkedin lookup failed: "
                  f"{resp['error']}", flush=True)
            return None
        return _contact_email(resp) or None

    def cost_estimate(self, action: str, count: int = 1) -> dict:
        n = max(1, count)
        if action == "search":
            return {
                "credits": 0,
                "approx_usd": 0.0,
                "notes": ("FindyMail has no prospect search — search() "
                          "is a no-op and costs nothing."),
            }
        return {
            "credits": n,
            "approx_usd": round(n * 0.05, 4),
            "notes": ("FindyMail: 1 finder credit per email FOUND "
                      "(~$0.05 on the Basic plan; misses are free). "
                      "Verified at find-time — no separate verify step "
                      "needed."),
        }


# Auto-register
try:
    register(FindyMailLeadSource())
except Exception as _e:
    print(f"[narada/findymail] register failed: "
          f"{type(_e).__name__}: {_e}", flush=True)
