"""Zoho CRM plugin — implements the CRM protocol (direct API).

Pipes Narada hot replies into the marketer's Zoho CRM as Contacts +
Deals + Notes. Auth is Zoho's **self-client OAuth**: the marketer
creates a Self Client at https://api-console.zoho.com/ (scope
`ZohoCRM.modules.ALL` is simplest), generates a grant code, exchanges
it once for a refresh token, and pastes `client_id`, `client_secret`
and `refresh_token` into /members/narada/credentials. Optional
`accounts_domain` (default https://accounts.zoho.com) and
`api_domain` (default https://www.zohoapis.com) override the data
centre for .eu/.in/.com.au/etc accounts.

We exchange refresh token → access token (1h TTL, cached in-memory —
Zoho rate-limits refresh calls) and call the v6 REST API with
`Authorization: Zoho-oauthtoken <token>`.

NOTE: auth_method is API_KEY because the member pastes static values
into our form — we never run a redirect OAuth dance ourselves.

Slug `zoho_crm`. Base https://www.zohoapis.com/crm/v6.
Docs: https://www.zoho.com/crm/developer/docs/api/v6/
Contacts dedup via POST /crm/v6/Contacts/upsert with
duplicate_check_fields=["Email"] (server-side create-or-update).
Free tier: Zoho CRM's free edition (3 users) includes API access.
"""
from __future__ import annotations
import json
import time
from datetime import date, timedelta
from urllib.error import HTTPError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from narada_creds import get_credential, has_credential, touch_last_used
from narada_plugins import register
from narada_plugins.types import (
    Activity, AuthMethod, DealData, Lead, PluginCategory, PluginInfo,
)


ZOHO_DEFAULT_ACCOUNTS = "https://accounts.zoho.com"
ZOHO_DEFAULT_API = "https://www.zohoapis.com"
ZOHO_TIMEOUT = 30
ZOHO_UA = "Narada/1.0 (outbound-agent)"

# In-memory access-token cache: member_email -> (token, api_domain,
# expires_epoch). Zoho access tokens live ~1h and token refreshes are
# rate-limited per refresh token, so refreshing on every API call
# would get the member throttled. 2-minute safety margin on expiry.
_TOKEN_CACHE: dict[str, tuple[str, str, float]] = {}


def _domain_or(raw: str | None, default: str) -> str:
    """Normalise a member-typed Zoho domain override. The credentials
    form marks every field required, so members WILL type something
    here — often a bare host ('accounts.zoho.in') or junk ('-').
    Adds the https:// scheme when missing; falls back to `default`
    for blanks and values that can't be a Zoho host (every real Zoho
    accounts/API domain contains 'zoho': zoho.eu, zohoapis.in,
    zohocloud.ca, ...)."""
    d = (raw or "").strip().rstrip("/")
    if not d:
        return default
    if not d.startswith(("http://", "https://")):
        d = f"https://{d}"
    if "zoho" not in d.lower():
        return default
    return d


def _iso_date_or(value: str, fallback: date) -> str:
    """First 10 chars of an ISO timestamp if plausible, else fallback.
    Zoho rejects the whole Deal row with INVALID_DATA on a malformed
    Closing_Date, so garbage in must not pass through."""
    candidate = (value or "")[:10]
    if len(candidate) == 10 and candidate[4] == "-" and candidate[7] == "-":
        return candidate
    return fallback.isoformat()


def _creds(member_email: str) -> dict | None:
    """Return the credential dict iff the three OAuth fields are set."""
    c = get_credential(member_email, "zoho_crm")
    if not c:
        return None
    if not ((c.get("client_id") or "").strip()
            and (c.get("client_secret") or "").strip()
            and (c.get("refresh_token") or "").strip()):
        return None
    return c


def _session(member_email: str) -> tuple[str, str] | None:
    """Return (access_token, api_domain) for this member, exchanging
    the stored refresh token when the cached access token is missing
    or expired. Never raises."""
    cached = _TOKEN_CACHE.get(member_email)
    if cached and cached[2] > time.time():
        return cached[0], cached[1]
    c = _creds(member_email)
    if not c:
        return None
    accounts = _domain_or(c.get("accounts_domain"), ZOHO_DEFAULT_ACCOUNTS)
    form = urlencode({
        "grant_type": "refresh_token",
        "client_id": c["client_id"].strip(),
        "client_secret": c["client_secret"].strip(),
        "refresh_token": c["refresh_token"].strip(),
    }).encode("utf-8")
    req = Request(f"{accounts}/oauth/v2/token", data=form, method="POST",
                  headers={
                      "Content-Type": "application/x-www-form-urlencoded",
                      "User-Agent": ZOHO_UA,
                  })
    try:
        with urlopen(req, timeout=ZOHO_TIMEOUT) as r:
            resp = json.loads(r.read().decode("utf-8") or "{}")
    except HTTPError as e:
        body_txt = ""
        try:
            body_txt = e.read().decode("utf-8", "replace")[:300]
        except Exception:
            pass
        print(f"[narada/zoho_crm] token refresh failed: "
              f"HTTP {e.code}: {body_txt}", flush=True)
        return None
    except Exception as e:
        print(f"[narada/zoho_crm] token refresh failed: "
              f"{type(e).__name__}: {e}", flush=True)
        return None
    token = (resp.get("access_token") or "").strip()
    if not token:
        # Zoho answers 200 with {"error": "invalid_code"} on bad
        # refresh tokens instead of a 4xx.
        print(f"[narada/zoho_crm] token refresh rejected: "
              f"{resp.get('error') or resp}", flush=True)
        return None
    # Precedence: member override → the api_domain Zoho echoes back in
    # the token response (authoritative for the DC) → the .com default.
    echoed = ((resp.get("api_domain") or "").strip().rstrip("/")
              or ZOHO_DEFAULT_API)
    api_domain = _domain_or(c.get("api_domain"), echoed)
    try:
        ttl = int(resp.get("expires_in") or 3600)
    except Exception:
        ttl = 3600
    _TOKEN_CACHE[member_email] = (
        token, api_domain, time.time() + max(ttl - 120, 60))
    return token, api_domain


def _api(member_email: str, method: str, path: str,
         body: dict | None = None, _retry: bool = True) -> dict:
    """Call Zoho CRM's v6 REST API. Returns parsed JSON, or {'error': ...}.
    Retries once on 401 (token revoked upstream of our cache TTL).
    Never raises."""
    sess = _session(member_email)
    if not sess:
        return {"error": "no Zoho CRM credential for this member "
                         "(client_id + client_secret + refresh_token "
                         "required)"}
    token, api_domain = sess
    url = f"{api_domain}{path}"
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = Request(url, data=data, method=method, headers={
        "Authorization": f"Zoho-oauthtoken {token}",
        "Content-Type": "application/json",
        "User-Agent": ZOHO_UA,
    })
    try:
        with urlopen(req, timeout=ZOHO_TIMEOUT) as r:
            text = r.read().decode("utf-8")
            return json.loads(text) if text else {}
    except HTTPError as e:
        if e.code == 401 and _retry:
            _TOKEN_CACHE.pop(member_email, None)
            return _api(member_email, method, path, body, _retry=False)
        body_txt = ""
        try:
            body_txt = e.read().decode("utf-8", "replace")[:300]
        except Exception:
            pass
        return {"error": f"HTTP {e.code}: {body_txt}"}
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}
    finally:
        touch_last_used(member_email, "zoho_crm")


def _first_record_id(resp: dict) -> str:
    """Pull details.id from Zoho's {'data': [{code, status, details}]}
    mutation envelope. Returns '' on row-level failure."""
    try:
        row = (resp.get("data") or [{}])[0]
        if (row.get("status") or "").lower() != "success":
            return ""
        return str((row.get("details") or {}).get("id") or "")
    except Exception:
        return ""


# ─────────────────────────────────────────────────────────────────────
# CRM implementation
# ─────────────────────────────────────────────────────────────────────

class ZohoCRM:

    @classmethod
    def info(cls) -> PluginInfo:
        return PluginInfo(
            name="zoho_crm",
            display_name="Zoho CRM",
            category=PluginCategory.CRM,
            auth_method=AuthMethod.API_KEY,
            requires_credentials=[
                "client_id", "client_secret", "refresh_token",
                "accounts_domain", "api_domain",
            ],
            homepage="https://www.zoho.com/crm/",
            docs_url="https://www.zoho.com/crm/developer/docs/api/v6/",
            description=(
                "Pipe Narada hot replies into Zoho CRM as Contacts + "
                "Deals + Notes. Create a Self Client at "
                "api-console.zoho.com, paste client_id, client_secret "
                "and refresh_token. US accounts paste the defaults "
                "https://accounts.zoho.com + https://www.zohoapis.com "
                "in the domain fields; other data centres use theirs "
                "(e.g. accounts.zoho.eu + www.zohoapis.eu, .in, "
                ".com.au). Zoho CRM's free edition includes API access."
            ),
            free_tier=True,
        )

    def is_available(self, member_email: str) -> bool:
        return has_credential(member_email, "zoho_crm")

    def upsert_contact(self, member_email: str, lead: Lead) -> str:
        """Upsert by email (dedup). Zoho's native upsert endpoint does
        the search-or-create server-side via
        duplicate_check_fields=["Email"]. Returns the Zoho contact id."""
        if not lead.email:
            return ""
        # Zoho Contacts require Last_Name — degrade gracefully when the
        # lead only has a first name / email.
        last = (lead.last_name or lead.first_name
                or lead.email.split("@")[0] or "Unknown")
        # Company is an Accounts *lookup* on Zoho Contacts (needs an
        # Account record id), so we stash company + LinkedIn in
        # Description instead of guessing account ids.
        desc_bits = []
        if lead.company:
            desc_bits.append(f"Company: {lead.company}")
        if lead.company_domain:
            desc_bits.append(f"Domain: {lead.company_domain}")
        if lead.linkedin_url:
            desc_bits.append(f"LinkedIn: {lead.linkedin_url}")
        record = {
            "Last_Name": last,
            "Email": lead.email,
            "First_Name": lead.first_name if lead.last_name else "",
            "Title": lead.title or "",
            "Description": " | ".join(desc_bits),
        }
        record = {k: v for k, v in record.items() if v}
        resp = _api(member_email, "POST", "/crm/v6/Contacts/upsert", {
            "data": [record],
            "duplicate_check_fields": ["Email"],
        })
        if "error" in resp:
            print(f"[narada/zoho_crm] upsert_contact failed: "
                  f"{resp['error']}", flush=True)
            return ""
        contact_id = _first_record_id(resp)
        if not contact_id:
            # HTTP 200 with a row-level error (MANDATORY_NOT_FOUND,
            # INVALID_DATA, ...) — surface it instead of failing silently.
            print(f"[narada/zoho_crm] upsert_contact rejected: "
                  f"{json.dumps(resp)[:300]}", flush=True)
        return contact_id

    def create_deal(self, member_email: str, contact_id: str,
                    deal: DealData) -> str:
        """Create a Deal linked via the Contact_Name lookup. Zoho's
        default layout mandates Deal_Name + Stage + Closing_Date, so we
        default Stage='Qualification' and Closing_Date=+30 days when
        the caller doesn't set them."""
        if not (contact_id and deal.title):
            return ""
        record = {
            "Deal_Name": deal.title[:120],
            "Stage": deal.stage or "Qualification",
            "Closing_Date": _iso_date_or(deal.close_date,
                                         date.today() + timedelta(days=30)),
            "Contact_Name": {"id": contact_id},
        }
        if deal.value:
            record["Amount"] = deal.value
        resp = _api(member_email, "POST", "/crm/v6/Deals",
                    {"data": [record]})
        if "error" in resp:
            print(f"[narada/zoho_crm] create_deal failed: "
                  f"{resp['error']}", flush=True)
            return ""
        deal_id = _first_record_id(resp)
        if not deal_id:
            print(f"[narada/zoho_crm] create_deal rejected: "
                  f"{json.dumps(resp)[:300]}", flush=True)
        return deal_id

    def log_activity(self, member_email: str, contact_id: str,
                     activity: Activity) -> None:
        """Log a Note against the contact via the record-scoped Notes
        endpoint (POST /Contacts/{id}/Notes — no Parent_Id needed).
        Best-effort — failures don't block the send."""
        if not contact_id:
            return
        content = (f"[Narada {activity.type}] {activity.subject}\n\n"
                   f"{activity.body}")[:32000]
        body = {"data": [{
            "Note_Title": f"Narada {activity.type}"[:120],
            "Note_Content": content,
        }]}
        resp = _api(member_email, "POST",
                    f"/crm/v6/Contacts/{quote(str(contact_id), safe='')}"
                    f"/Notes", body)
        if "error" in resp:
            print(f"[narada/zoho_crm] log_activity failed: "
                  f"{resp['error']}", flush=True)
        elif not _first_record_id(resp):
            print(f"[narada/zoho_crm] log_activity rejected: "
                  f"{json.dumps(resp)[:300]}", flush=True)


# Auto-register
try:
    register(ZohoCRM())
except Exception as _e:
    print(f"[narada/zoho_crm] register failed: "
          f"{type(_e).__name__}: {_e}", flush=True)
