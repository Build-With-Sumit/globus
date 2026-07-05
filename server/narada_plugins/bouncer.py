"""Bouncer plugin — implements the Verifier protocol.

Bouncer (usebouncer.com) is a dedicated email-verification service
known for high accuracy on catch-all resolution and a per-address
toxicity signal (breached / widely-circulated addresses). Pair it with
any lead-source (Prospeo, Apollo, RocketReach) for a second-opinion
verify separate from the vendor that sold you the list.

Auth: API key (from the Bouncer dashboard → API), sent as an
`x-api-key` header. Paste into /members/narada/credentials → tool slug
`bouncer`. Per-member isolation via the globus_narada_credentials vault.

API: GET https://api.usebouncer.com/v1.1/email/verify
  ?email=<addr>&timeout=<sec>   (header: x-api-key)
Response: {email, status: deliverable|risky|undeliverable|unknown,
reason: accepted_email|low_deliverability|invalid_email|..., domain:
{acceptAll, disposable, free}, account: {role, disabled, fullMailbox},
score: 0-100, toxic, ...}. Errors are plain HTTP: 401 bad key,
402 out of credits, 429 rate limit (default 1000 req/min).
Docs: https://docs.usebouncer.com/
Pricing: 100 free credits on signup; pay-as-you-go from ~$0.008/verify,
volume tiers lower it.
"""
from __future__ import annotations
import json
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from narada_creds import get_credential, has_credential, touch_last_used
from narada_plugins import register
from narada_plugins.types import (
    AuthMethod, PluginCategory, PluginInfo, VerifyResult, VerifyStatus,
)


BOUNCER_API_BASE = "https://api.usebouncer.com/v1.1"
BOUNCER_TIMEOUT = 30
BOUNCER_UA = "Narada/1.0 (outbound-agent)"
# Bouncer's own SMTP-probe budget (seconds). Their default is 10;
# 15 resolves more greedy/slow mail servers while staying well under
# our 30s HTTP timeout.
BOUNCER_VERIFY_TIMEOUT = 15


def _api_key(member_email: str) -> str | None:
    cred = get_credential(member_email, "bouncer")
    if not cred:
        return None
    return (cred.get("api_key") or "").strip() or None


def _get(member_email: str, path: str, params: dict) -> dict:
    """GET Bouncer's REST API + return parsed JSON. Never raises —
    on transport/HTTP error returns {'error': '...'}."""
    api_key = _api_key(member_email)
    if not api_key:
        return {"error": "no Bouncer credential for this member"}
    url = f"{BOUNCER_API_BASE}{path}?{urlencode(params)}"
    req = Request(url, method="GET",
                  headers={"x-api-key": api_key,
                           "User-Agent": BOUNCER_UA})
    try:
        with urlopen(req, timeout=BOUNCER_TIMEOUT) as r:
            resp = json.loads(r.read().decode("utf-8"))
    except HTTPError as e:
        # Bouncer signals API-level failures via HTTP codes: 401 bad
        # key, 402 out of credits, 429 rate limit; body carries JSON
        # {status, message} — surface the first slice of it.
        body_txt = ""
        try:
            body_txt = e.read().decode("utf-8", "replace")[:300]
        except Exception:
            pass
        return {"error": f"HTTP {e.code}: {body_txt}"}
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}
    if not isinstance(resp, dict):
        return {"error": f"unexpected response shape: {type(resp).__name__}"}
    touch_last_used(member_email, "bouncer")
    return resp


class BouncerVerifier:

    @classmethod
    def info(cls) -> PluginInfo:
        return PluginInfo(
            name="bouncer",
            display_name="Bouncer",
            category=PluginCategory.VERIFIER,
            auth_method=AuthMethod.API_KEY,
            requires_credentials=["api_key"],
            homepage="https://www.usebouncer.com",
            docs_url="https://docs.usebouncer.com/",
            description=(
                "Email verification with strong catch-all resolution "
                "and a toxicity signal for breached/spam-circulated "
                "addresses. 100 free credits on signup, then "
                "pay-as-you-go from ~$0.008/verify. Paste the API key "
                "from your Bouncer dashboard."
            ),
            free_tier=True,
        )

    def is_available(self, member_email: str) -> bool:
        return has_credential(member_email, "bouncer")

    def verify(self, member_email: str, email: str) -> VerifyResult:
        if not email:
            return VerifyResult(email="", status=VerifyStatus.INVALID,
                                raw={"reason": "empty email"})
        resp = _get(member_email, "/email/verify",
                    {"email": email, "timeout": BOUNCER_VERIFY_TIMEOUT})
        if "error" in resp:
            return VerifyResult(email=email, status=VerifyStatus.ERROR,
                                raw=resp)
        status_str = (resp.get("status") or "").lower()
        mapping = {
            "deliverable":   VerifyStatus.VALID,
            "undeliverable": VerifyStatus.INVALID,
            "risky":         VerifyStatus.RISKY,
            "unknown":       VerifyStatus.UNKNOWN,
        }
        status = mapping.get(status_str, VerifyStatus.UNKNOWN)
        # domain/account sub-fields are "yes"/"no"/"unknown" strings.
        domain = resp.get("domain") or {}
        account = resp.get("account") or {}
        # Bouncer's optional score (0-100) estimates the odds that
        # delivery succeeds — it is NOT confidence in the verdict. A
        # hard "undeliverable" comes with score ~0, which must not be
        # reported as near-zero confidence, so pin that bucket to 1.0
        # and use the score only for the deliverable/ambiguous cases.
        score = resp.get("score")
        if status_str == "undeliverable":
            confidence = 1.0
        elif isinstance(score, (int, float)):
            confidence = max(0.0, min(1.0, float(score) / 100.0))
        else:
            confidence = 1.0 if status_str == "deliverable" else 0.5
        return VerifyResult(
            email=email,
            status=status,
            confidence=confidence,
            is_catch_all=(domain.get("acceptAll") == "yes"),
            is_disposable=(domain.get("disposable") == "yes"),
            is_role=(account.get("role") == "yes"),
            raw=resp,
        )

    def cost_estimate(self, count: int = 1) -> dict:
        n = max(1, count)
        return {
            "credits": n,
            "approx_usd": round(n * 0.008, 4),
            "notes": ("Bouncer pay-as-you-go ~$0.008/verify (lower on "
                      "volume tiers); 100 free credits on signup. "
                      "1 credit per email verified."),
        }


# Auto-register
try:
    register(BouncerVerifier())
except Exception as _e:
    print(f"[narada/bouncer] register failed: "
          f"{type(_e).__name__}: {_e}", flush=True)
