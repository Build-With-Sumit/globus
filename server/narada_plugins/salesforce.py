"""Salesforce plugin — implements the CRM protocol (direct API).

Pipes Narada hot replies into the marketer's Salesforce org as
Contacts + Opportunities + Tasks. The marketer creates a **Connected
App** with the *Client Credentials* OAuth flow enabled (Setup → App
Manager → New Connected App → enable "Client Credentials Flow" and
pick a run-as user), then pastes three values:

- `instance_url`  — the org's My Domain URL, e.g.
                    https://acme.my.salesforce.com (login.salesforce.com
                    does NOT support client-credentials tokens)
- `client_id`     — the Connected App's Consumer Key
- `client_secret` — the Connected App's Consumer Secret

Auth: OAuth2 client-credentials — POST {instance_url}/services/oauth2/token
with grant_type=client_credentials; we cache the bearer token per member
(~20 min) and re-mint on 401. No user-facing OAuth dance, so the setup
form is plain paste fields (AuthMethod.API_KEY).

Base: {instance_url}/services/data/v60.0
Docs: https://developer.salesforce.com/docs/atlas.en-us.api_rest.meta/api_rest/
Slug `salesforce`.

Object mapping: contact ↔ Contact (dedup via SOQL on Email; contacts
are created account-less "private" contacts — attaching Accounts is a
v2 concern), deal ↔ Opportunity (StageName/CloseDate are required by
Salesforce; we default to "Prospecting" / today+30d) + an
OpportunityContactRole to tie it to the contact, activity ↔ Task
(WhoId = the contact). Currency is org-controlled — we don't send
CurrencyIsoCode (single-currency orgs reject it).
"""
from __future__ import annotations
import json
import time
from datetime import date, timedelta
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from narada_creds import get_credential, has_credential, touch_last_used
from narada_plugins import register
from narada_plugins.types import (
    Activity, AuthMethod, DealData, Lead, PluginCategory, PluginInfo,
)


SALESFORCE_API_VERSION = "v60.0"
SALESFORCE_TIMEOUT = 30
SALESFORCE_UA = "Narada/1.0 (outbound-agent; +https://globussoft.ai)"
# Client-credentials tokens live as long as the Connected App's session
# timeout (org default 2h). We cache well inside that and re-mint on 401.
TOKEN_TTL_SECONDS = 20 * 60

# member_email -> (access_token, instance_url, expires_at_epoch)
_TOKEN_CACHE: dict[str, tuple[str, str, float]] = {}


def _cred(member_email: str) -> dict | None:
    cred = get_credential(member_email, "salesforce")
    if not cred:
        return None
    instance_url = (cred.get("instance_url") or "").strip().rstrip("/")
    client_id = (cred.get("client_id") or "").strip()
    client_secret = (cred.get("client_secret") or "").strip()
    if not (instance_url and client_id and client_secret):
        return None
    if not instance_url.startswith("http"):
        instance_url = f"https://{instance_url}"
    return {"instance_url": instance_url, "client_id": client_id,
            "client_secret": client_secret}


def _get_token(member_email: str,
               force_refresh: bool = False) -> tuple[str, str] | None:
    """Return (access_token, instance_url) via the client-credentials
    flow, cached ~20 min per member. None on any failure. Never raises."""
    if not force_refresh:
        cached = _TOKEN_CACHE.get(member_email)
        if cached and cached[2] > time.time():
            return cached[0], cached[1]
    cred = _cred(member_email)
    if not cred:
        return None
    form = urlencode({
        "grant_type": "client_credentials",
        "client_id": cred["client_id"],
        "client_secret": cred["client_secret"],
    }).encode("utf-8")
    req = Request(f"{cred['instance_url']}/services/oauth2/token",
                  data=form, method="POST", headers={
                      "Content-Type": "application/x-www-form-urlencoded",
                      "Accept": "application/json",
                      "User-Agent": SALESFORCE_UA,
                  })
    try:
        with urlopen(req, timeout=SALESFORCE_TIMEOUT) as r:
            parsed = json.loads(r.read().decode("utf-8"))
    except HTTPError as e:
        body_txt = ""
        try:
            body_txt = e.read().decode("utf-8", "replace")[:300]
        except Exception:
            pass
        print(f"[narada/salesforce] token fetch failed: HTTP {e.code}: "
              f"{body_txt}", flush=True)
        return None
    except Exception as e:
        print(f"[narada/salesforce] token fetch failed: "
              f"{type(e).__name__}: {e}", flush=True)
        return None
    token = parsed.get("access_token") or ""
    if not token:
        print(f"[narada/salesforce] token response missing access_token: "
              f"{str(parsed)[:200]}", flush=True)
        return None
    # Prefer the instance_url Salesforce echoes back — it's authoritative
    # (handles sandbox/domain redirects); fall back to the pasted one.
    instance_url = (parsed.get("instance_url")
                    or cred["instance_url"]).rstrip("/")
    _TOKEN_CACHE[member_email] = (
        token, instance_url, time.time() + TOKEN_TTL_SECONDS)
    return token, instance_url


def _api(member_email: str, method: str, path: str,
         body: dict | None = None, params: dict | None = None,
         _retried: bool = False) -> dict:
    """Call the Salesforce REST API. `path` is relative to the instance
    root (e.g. /services/data/v60.0/sobjects/Contact). Returns parsed
    JSON ({} for 204 No Content), or {'error': ...}. Never raises.
    On a 401 (expired session) it re-mints the token and retries once."""
    tok = _get_token(member_email)
    if not tok:
        return {"error": "no usable Salesforce credential for this member"}
    token, instance_url = tok
    url = f"{instance_url}{path}"
    if params:
        url = f"{url}?{urlencode(params)}"
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = Request(url, data=data, method=method, headers={
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": SALESFORCE_UA,
    })
    try:
        with urlopen(req, timeout=SALESFORCE_TIMEOUT) as r:
            text = r.read().decode("utf-8")
            parsed = json.loads(text) if text else {}
            # Error payloads are JSON *lists*; success dicts stay as-is.
            return parsed if isinstance(parsed, dict) else {"data": parsed}
    except HTTPError as e:
        if e.code == 401 and not _retried:
            _TOKEN_CACHE.pop(member_email, None)
            return _api(member_email, method, path, body=body,
                        params=params, _retried=True)
        body_txt = ""
        try:
            body_txt = e.read().decode("utf-8", "replace")[:300]
        except Exception:
            pass
        return {"error": f"HTTP {e.code}: {body_txt}"}
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}
    finally:
        touch_last_used(member_email, "salesforce")


def _soql_quote(value: str) -> str:
    """Escape a string for embedding in single-quoted SOQL literals."""
    return value.replace("\\", "\\\\").replace("'", "\\'")


def _iso_date_or(value: str, fallback: date) -> str:
    """First 10 chars of an ISO timestamp if plausible, else fallback."""
    candidate = (value or "")[:10]
    if len(candidate) == 10 and candidate[4] == "-" and candidate[7] == "-":
        return candidate
    return fallback.isoformat()


class SalesforceCRM:

    @classmethod
    def info(cls) -> PluginInfo:
        return PluginInfo(
            name="salesforce",
            display_name="Salesforce",
            category=PluginCategory.CRM,
            auth_method=AuthMethod.API_KEY,
            requires_credentials=["instance_url", "client_id",
                                  "client_secret"],
            homepage="https://www.salesforce.com",
            docs_url=("https://developer.salesforce.com/docs/atlas.en-us."
                      "api_rest.meta/api_rest/"),
            description=(
                "Pipe Narada hot replies into Salesforce as Contacts, "
                "Opportunities and Tasks. Create a Connected App with the "
                "Client Credentials flow enabled, then paste your My Domain "
                "URL plus the app's Consumer Key and Secret. Needs a paid "
                "Salesforce edition with API access (or a free Developer "
                "Edition org)."
            ),
            free_tier=False,
        )

    def is_available(self, member_email: str) -> bool:
        return has_credential(member_email, "salesforce")

    def upsert_contact(self, member_email: str, lead: Lead) -> str:
        """Upsert by email (SOQL dedup). Returns the Salesforce Contact Id."""
        if not lead.email:
            return ""
        # Search by email first.
        soql = ("SELECT Id FROM Contact "
                f"WHERE Email = '{_soql_quote(lead.email)}' LIMIT 1")
        found = _api(member_email, "GET",
                     f"/services/data/{SALESFORCE_API_VERSION}/query",
                     params={"q": soql})
        if not found.get("error"):
            records = found.get("records") or []
            if records:
                return str(records[0].get("Id") or "")
        # Create. LastName is mandatory on Contact — fall back so a
        # first-name-only lead still lands in the CRM.
        props = {
            "FirstName": lead.first_name or "",
            "LastName": lead.last_name or lead.first_name or "Unknown",
            "Email": lead.email,
            "Title": lead.title or "",
        }
        company_bits = " ".join(part for part in (
            lead.company,
            f"({lead.company_domain})" if lead.company_domain else "",
        ) if part)
        if company_bits:
            props["Description"] = f"Company: {company_bits}"
        props = {k: v for k, v in props.items() if v}
        resp = _api(member_email, "POST",
                    f"/services/data/{SALESFORCE_API_VERSION}"
                    "/sobjects/Contact", props)
        if "error" in resp:
            print(f"[narada/salesforce] upsert_contact failed: "
                  f"{resp['error']}", flush=True)
            return ""
        return str(resp.get("id") or "")

    def create_deal(self, member_email: str, contact_id: str,
                    deal: DealData) -> str:
        """Create an Opportunity + OpportunityContactRole for the contact.
        StageName and CloseDate are required by Salesforce — defaults are
        'Prospecting' and today+30d when the caller doesn't provide them."""
        if not (contact_id and deal.title):
            return ""
        props = {
            "Name": deal.title[:120],
            "StageName": deal.stage or "Prospecting",
            "CloseDate": _iso_date_or(deal.close_date,
                                      date.today() + timedelta(days=30)),
        }
        if deal.value:
            props["Amount"] = deal.value
        resp = _api(member_email, "POST",
                    f"/services/data/{SALESFORCE_API_VERSION}"
                    "/sobjects/Opportunity", props)
        if "error" in resp:
            print(f"[narada/salesforce] create_deal failed: "
                  f"{resp['error']}", flush=True)
            return ""
        opp_id = str(resp.get("id") or "")
        if not opp_id:
            return ""
        # Tie the opportunity to the contact. Best-effort — the
        # opportunity already exists, so a role failure isn't fatal.
        role = _api(member_email, "POST",
                    f"/services/data/{SALESFORCE_API_VERSION}"
                    "/sobjects/OpportunityContactRole", {
                        "OpportunityId": opp_id,
                        "ContactId": contact_id,
                        "IsPrimary": True,
                    })
        if "error" in role:
            print(f"[narada/salesforce] contact-role attach failed: "
                  f"{role['error']}", flush=True)
        return opp_id

    def log_activity(self, member_email: str, contact_id: str,
                     activity: Activity) -> None:
        """Log a completed Task against the contact (WhoId). Best-effort."""
        if not contact_id:
            return
        props = {
            "Subject": (f"[Narada {activity.type}] "
                        f"{activity.subject}")[:255],
            "Description": (activity.body or "")[:32000],
            "Status": "Completed",
            "WhoId": contact_id,
            "ActivityDate": _iso_date_or(activity.occurred_at,
                                         date.today()),
        }
        resp = _api(member_email, "POST",
                    f"/services/data/{SALESFORCE_API_VERSION}"
                    "/sobjects/Task", props)
        if "error" in resp:
            print(f"[narada/salesforce] log_activity failed: "
                  f"{resp['error']}", flush=True)


# Auto-register
try:
    register(SalesforceCRM())
except Exception as _e:
    print(f"[narada/salesforce] register failed: "
          f"{type(_e).__name__}: {_e}", flush=True)
