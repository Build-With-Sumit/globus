"""MailerCheck plugin — implements the Verifier protocol.

MailerCheck (mailercheck.com) is the email-verification service from
the MailerLite team. Pair it with any lead-source (Prospeo, Apollo,
RocketReach) when the marketer wants a second-opinion verify separate
from the lead vendor — verifying with the same vendor that sold you
the list is a known blind spot.

Auth: API token (created in the MailerCheck UI under Integrations →
API), sent as `Authorization: Bearer <token>`. Paste into
/members/narada/credentials → tool slug `mailercheck`, field
`api_key`. Per-member isolation via the globus_narada_credentials
vault.

API: POST https://app.mailercheck.com/api/check/single
  body {"email": "<addr>"}
Response: {"status": "valid"} where status is one of valid |
catch_all | mailbox_full | role (a.k.a. role_based) | unknown |
syntax_error | typo | mailbox_not_found | disposable | blocked |
error. Rate limit 60 req/min (HTTP 429 + retry-after on breach).
Docs: https://developers.mailercheck.com/
Pricing: credits at ~$0.01/verify (volume tiers down to ~$0.006);
1 credit per single check; new accounts get a few free trial credits.
"""
from __future__ import annotations
import json
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from narada_creds import get_credential, has_credential, touch_last_used
from narada_plugins import register
from narada_plugins.types import (
    AuthMethod, PluginCategory, PluginInfo, VerifyResult, VerifyStatus,
)


MAILERCHECK_API_BASE = "https://app.mailercheck.com/api"
MAILERCHECK_TIMEOUT = 30


def _api_key(member_email: str) -> str | None:
    cred = get_credential(member_email, "mailercheck")
    if not cred:
        return None
    return (cred.get("api_key") or "").strip() or None


def _post(member_email: str, path: str, payload: dict) -> dict:
    """POST MailerCheck's REST API + return parsed JSON. Never raises —
    on transport/HTTP error returns {'error': '...'}."""
    api_key = _api_key(member_email)
    if not api_key:
        return {"error": "no MailerCheck credential for this member"}
    url = f"{MAILERCHECK_API_BASE}{path}"
    body = json.dumps(payload).encode("utf-8")
    req = Request(url, data=body, method="POST", headers={
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": "Narada/1.0 (outbound agent; +https://globussoft.ai)",
    })
    try:
        with urlopen(req, timeout=MAILERCHECK_TIMEOUT) as r:
            resp = json.loads(r.read().decode("utf-8"))
    except HTTPError as e:
        body_txt = ""
        try:
            body_txt = e.read().decode("utf-8", "replace")[:300]
        except Exception:
            pass
        if e.code == 401:
            return {"error": "HTTP 401: invalid MailerCheck API token"}
        if e.code == 429:
            retry = e.headers.get("retry-after", "?") if e.headers else "?"
            return {"error": f"HTTP 429: rate limited (60 req/min), "
                             f"retry after {retry}s"}
        return {"error": f"HTTP {e.code}: {body_txt}"}
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}
    if not isinstance(resp, dict):
        return {"error": f"unexpected response shape: {str(resp)[:200]}"}
    touch_last_used(member_email, "mailercheck")
    return resp


class MailerCheckVerifier:

    @classmethod
    def info(cls) -> PluginInfo:
        return PluginInfo(
            name="mailercheck",
            display_name="MailerCheck",
            category=PluginCategory.VERIFIER,
            auth_method=AuthMethod.API_KEY,
            requires_credentials=["api_key"],
            homepage="https://www.mailercheck.com",
            docs_url="https://developers.mailercheck.com/",
            description=(
                "Email verification from the MailerLite team. Catches "
                "bad syntax, dead mailboxes, catch-alls, disposables and "
                "role accounts before you send. Credits from ~$0.01/verify "
                "(cheaper on volume); paste your API token from "
                "Integrations → API."
            ),
            free_tier=True,   # new accounts include free trial credits
        )

    def is_available(self, member_email: str) -> bool:
        return has_credential(member_email, "mailercheck")

    def verify(self, member_email: str, email: str) -> VerifyResult:
        if not email:
            return VerifyResult(email="", status=VerifyStatus.INVALID,
                                raw={"reason": "empty email"})
        resp = _post(member_email, "/check/single", {"email": email})
        if "error" in resp and "status" not in resp:
            return VerifyResult(email=email, status=VerifyStatus.ERROR,
                                raw=resp)
        result = (resp.get("status") or "").lower()
        mapping = {
            "valid":             VerifyStatus.VALID,
            "syntax_error":      VerifyStatus.INVALID,
            "typo":              VerifyStatus.INVALID,
            "mailbox_not_found": VerifyStatus.INVALID,
            "blocked":           VerifyStatus.INVALID,
            "catch_all":         VerifyStatus.RISKY,
            "mailbox_full":      VerifyStatus.RISKY,
            "role":              VerifyStatus.RISKY,
            "role_based":        VerifyStatus.RISKY,
            "disposable":        VerifyStatus.RISKY,
            "unknown":           VerifyStatus.UNKNOWN,
            "error":             VerifyStatus.ERROR,
        }
        status = mapping.get(result, VerifyStatus.UNKNOWN)
        hard = ("valid", "syntax_error", "typo", "mailbox_not_found",
                "blocked")
        return VerifyResult(
            email=email,
            status=status,
            # MailerCheck returns a discrete status, not a 0-1 score;
            # map the confident buckets to 1.0, ambiguous to 0.5.
            confidence=1.0 if result in hard else 0.5,
            is_catch_all=(result == "catch_all"),
            is_disposable=(result == "disposable"),
            is_role=(result in ("role", "role_based")),
            raw=resp,
        )

    def cost_estimate(self, count: int = 1) -> dict:
        n = max(1, count)
        return {
            "credits": n,
            "approx_usd": round(n * 0.01, 4),
            "notes": ("MailerCheck credits ~$0.01/verify at the 1k tier "
                      "(volume tiers down to ~$0.006). 1 credit per email "
                      "checked; credits never expire."),
        }


# Auto-register
try:
    register(MailerCheckVerifier())
except Exception as _e:
    print(f"[narada/mailercheck] register failed: "
          f"{type(_e).__name__}: {_e}", flush=True)
