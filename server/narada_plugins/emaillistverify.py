"""EmailListVerify plugin — implements the Verifier protocol.

EmailListVerify (emaillistverify.com) is a budget email-verification
service — one of the cheapest per-check vendors around (~$0.004/verify
pay-as-you-go, 100 free credits on signup). Pair it with any
lead-source (Prospeo, Apollo, RocketReach) when the marketer wants a
cheap deliverability gate before burning sender reputation.

Auth: API key, paste into /members/narada/credentials → tool slug
`emaillistverify`, field `api_key`. Per-member isolation via the
globus_narada_credentials vault.

API: GET https://api.emaillistverify.com/api/verifyEmail
  ?secret=<key>&email=<addr>
(auth also accepted via `x-api-key` header; the legacy
apps.emaillistverify.com host serves the same backend). Response:
PLAIN TEXT — one token, not JSON. Official tokens (per the OpenAPI
spec at /api-doc): ok, unknown, dead_server, invalid_mx,
email_disabled, antispam_system, ok_for_all (catch-all),
smtp_protocol, invalid_syntax, disposable, spamtrap — plus
error_credit, which means the account is out of credits (a request
failure, NOT a verdict on the email). Bad API keys come back as
HTTP 401 with a JSON body, handled via the HTTPError path. We also
keep the legacy token names (fail, syntax_error, accept_all, role,
key_not_valid, ...) in our maps for safety — unmapped tokens
fail-open to UNKNOWN, never to VALID. Docs:
https://api.emaillistverify.com/api-doc
Pricing: pay-as-you-go ~$4 per 1,000 checks; volume tiers lower it.
"""
from __future__ import annotations
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from narada_creds import get_credential, has_credential, touch_last_used
from narada_plugins import register
from narada_plugins.types import (
    AuthMethod, PluginCategory, PluginInfo, VerifyResult, VerifyStatus,
)


ELV_API_BASE = "https://api.emaillistverify.com/api"
ELV_TIMEOUT = 30
ELV_USER_AGENT = "Narada/1.0 (outbound-agent; +https://globussoft.com)"

# Tokens that mean the API call itself failed (bad key, malformed
# request, empty balance) — the email was never actually checked.
# `error_credit` is the official out-of-credits token (OpenAPI spec);
# the rest are legacy spellings kept for safety.
_API_ERROR_TOKENS = {
    "error_credit",
    "key_not_valid", "missing_parameters", "missing parameters",
    "no_credit", "no_credits",
}

# Verdict token → normalised status. Anything not listed maps to
# UNKNOWN (EmailListVerify has grown its token set over the years;
# fail-open to UNKNOWN, never to VALID).
_STATUS_MAP = {
    # deliverable
    "ok":               VerifyStatus.VALID,
    # hard failures — expect a bounce
    "fail":             VerifyStatus.INVALID,
    "invalid":          VerifyStatus.INVALID,
    "invalid_mx":       VerifyStatus.INVALID,
    "unknown_email":    VerifyStatus.INVALID,
    "email_disabled":   VerifyStatus.INVALID,
    "dead_server":      VerifyStatus.INVALID,
    "domain_error":     VerifyStatus.INVALID,
    "invalid_syntax":   VerifyStatus.INVALID,   # official token
    "syntax_error":     VerifyStatus.INVALID,   # legacy spelling

    "incorrect":        VerifyStatus.INVALID,
    # spamtrap: technically deliverable, but sending torches the
    # member's sender reputation — treat as do-not-send.
    "spamtrap":         VerifyStatus.INVALID,
    # send at own risk
    "ok_for_all":       VerifyStatus.RISKY,
    "accept_all":       VerifyStatus.RISKY,
    "disposable":       VerifyStatus.RISKY,
    "role":             VerifyStatus.RISKY,
    "role_account":     VerifyStatus.RISKY,
    # inconclusive SMTP-level outcomes
    "unknown":          VerifyStatus.UNKNOWN,
    "error":            VerifyStatus.UNKNOWN,
    "smtp_error":       VerifyStatus.UNKNOWN,
    "smtp_protocol":    VerifyStatus.UNKNOWN,
    "antispam_system":  VerifyStatus.UNKNOWN,
    "attempt_rejected": VerifyStatus.UNKNOWN,
    "relay_error":      VerifyStatus.UNKNOWN,
    "greylisted":       VerifyStatus.UNKNOWN,
}


def _api_key(member_email: str) -> str | None:
    cred = get_credential(member_email, "emaillistverify")
    if not cred:
        return None
    return (cred.get("api_key") or "").strip() or None


def _get_text(member_email: str, path: str, params: dict) -> dict:
    """GET EmailListVerify's plain-text API. Never raises — returns
    {'result': '<token>'} on success or {'error': '...'} on any
    transport/HTTP/API failure."""
    api_key = _api_key(member_email)
    if not api_key:
        return {"error": "no EmailListVerify credential for this member"}
    q = urlencode({**params, "secret": api_key})
    url = f"{ELV_API_BASE}{path}?{q}"
    req = Request(url, method="GET",
                  headers={"User-Agent": ELV_USER_AGENT})
    try:
        with urlopen(req, timeout=ELV_TIMEOUT) as r:
            text = r.read().decode("utf-8", "replace").strip()
    except HTTPError as e:
        body_txt = ""
        try:
            body_txt = e.read().decode("utf-8", "replace")[:300]
        except Exception:
            pass
        return {"error": f"HTTP {e.code}: {body_txt}"}
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}
    token = text.lower()
    # The API returns 200 with an error token for auth/request problems.
    if not token or token in _API_ERROR_TOKENS:
        return {"error": text or "empty response"}
    touch_last_used(member_email, "emaillistverify")
    return {"result": token}


class EmailListVerifyVerifier:

    @classmethod
    def info(cls) -> PluginInfo:
        return PluginInfo(
            name="emaillistverify",
            display_name="EmailListVerify",
            category=PluginCategory.VERIFIER,
            auth_method=AuthMethod.API_KEY,
            requires_credentials=["api_key"],
            homepage="https://emaillistverify.com",
            docs_url="https://api.emaillistverify.com/api-doc",
            description=(
                "Budget email verification — around $4 per 1,000 checks, "
                "one of the cheapest deliverability gates around. Catches "
                "dead mailboxes, catch-alls, disposables and spam traps "
                "before they hurt your sender reputation. 100 free "
                "credits on signup; paste your API key."
            ),
            free_tier=True,
        )

    def is_available(self, member_email: str) -> bool:
        return has_credential(member_email, "emaillistverify")

    def verify(self, member_email: str, email: str) -> VerifyResult:
        if not email:
            return VerifyResult(email="", status=VerifyStatus.INVALID,
                                raw={"reason": "empty email"})
        resp = _get_text(member_email, "/verifyEmail", {"email": email})
        if "error" in resp:
            return VerifyResult(email=email, status=VerifyStatus.ERROR,
                                raw=resp)
        result = resp["result"]
        status = _STATUS_MAP.get(result, VerifyStatus.UNKNOWN)
        # EmailListVerify returns a discrete token, not a 0-1 score;
        # confident buckets map to 1.0, risky to 0.5, inconclusive 0.0.
        if status in (VerifyStatus.VALID, VerifyStatus.INVALID):
            confidence = 1.0
        elif status is VerifyStatus.RISKY:
            confidence = 0.5
        else:
            confidence = 0.0
        return VerifyResult(
            email=email,
            status=status,
            confidence=confidence,
            is_catch_all=result in ("ok_for_all", "accept_all"),
            is_disposable=(result == "disposable"),
            is_role=result in ("role", "role_account"),
            raw={"result": result},
        )

    def cost_estimate(self, count: int = 1) -> dict:
        n = max(1, count)
        return {
            "credits": n,
            "approx_usd": round(n * 0.004, 4),
            "notes": ("EmailListVerify pay-as-you-go ~$4/1,000 checks "
                      "(~$0.004/verify, lower on volume tiers). 1 credit "
                      "per email checked; 100 free credits on signup."),
        }


# Auto-register
try:
    register(EmailListVerifyVerifier())
except Exception as _e:
    print(f"[narada/emaillistverify] register failed: "
          f"{type(_e).__name__}: {_e}", flush=True)
