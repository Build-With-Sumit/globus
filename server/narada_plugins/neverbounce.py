"""NeverBounce plugin — implements the Verifier protocol.

NeverBounce (neverbounce.com) is a dedicated email-verification service.
Pair it with any lead-source (Prospeo, Apollo, RocketReach) when the
marketer wants a second-opinion verify separate from the lead vendor —
verifying with the same vendor that sold you the list is a known
blind spot.

Auth: API key ("private" key, prefix `private_...`), paste into
/members/narada/credentials → tool slug `neverbounce`. Per-member
isolation via the globus_narada_credentials vault.

API: GET https://api.neverbounce.com/v4/single/check
  ?key=<key>&email=<addr>&address_info=1&credits_info=1
Response: {status: "success", result: valid|invalid|disposable|
catchall|unknown, flags: [...], suggested_correction: "..."}. Non-success
`status` (auth_failure, temp_unavail, throttle_triggered, bad_referrer)
carries a `message`. Docs: https://developers.neverbounce.com/
Pricing: pay-as-you-go from ~$0.008/verify; volume tiers lower it.
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


NEVERBOUNCE_API_BASE = "https://api.neverbounce.com/v4"
NEVERBOUNCE_TIMEOUT = 30


def _api_key(member_email: str) -> str | None:
    cred = get_credential(member_email, "neverbounce")
    if not cred:
        return None
    return (cred.get("api_key") or "").strip() or None


def _get(member_email: str, path: str, params: dict) -> dict:
    """GET NeverBounce's REST API + return parsed JSON. Never raises —
    on transport/HTTP error returns {'error': '...'}."""
    api_key = _api_key(member_email)
    if not api_key:
        return {"error": "no NeverBounce credential for this member"}
    q = urlencode({**params, "key": api_key})
    url = f"{NEVERBOUNCE_API_BASE}{path}?{q}"
    req = Request(url, method="GET")
    try:
        with urlopen(req, timeout=NEVERBOUNCE_TIMEOUT) as r:
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
    # NeverBounce returns 200 with status != success for API-level errors.
    if (resp.get("status") or "") not in ("success", ""):
        return {"error": f"{resp.get('status')}: {resp.get('message', '')}"}
    touch_last_used(member_email, "neverbounce")
    return resp


class NeverBounceVerifier:

    @classmethod
    def info(cls) -> PluginInfo:
        return PluginInfo(
            name="neverbounce",
            display_name="NeverBounce",
            category=PluginCategory.VERIFIER,
            auth_method=AuthMethod.API_KEY,
            requires_credentials=["api_key"],
            homepage="https://neverbounce.com",
            docs_url="https://developers.neverbounce.com/",
            description=(
                "Dedicated email verification. Use as a second-opinion "
                "verifier separate from your lead vendor. Pay-as-you-go "
                "from ~$0.008/verify. Paste your 'private_' API key."
            ),
            free_tier=False,
        )

    def is_available(self, member_email: str) -> bool:
        return has_credential(member_email, "neverbounce")

    def verify(self, member_email: str, email: str) -> VerifyResult:
        if not email:
            return VerifyResult(email="", status=VerifyStatus.INVALID,
                                raw={"reason": "empty email"})
        resp = _get(member_email, "/single/check",
                    {"email": email, "address_info": 1, "credits_info": 1})
        if "error" in resp:
            return VerifyResult(email=email, status=VerifyStatus.ERROR,
                                raw=resp)
        result = (resp.get("result") or "").lower()
        mapping = {
            "valid":     VerifyStatus.VALID,
            "invalid":   VerifyStatus.INVALID,
            "disposable": VerifyStatus.RISKY,
            "catchall":  VerifyStatus.RISKY,
            "unknown":   VerifyStatus.UNKNOWN,
        }
        status = mapping.get(result, VerifyStatus.UNKNOWN)
        flags = resp.get("flags") or []
        return VerifyResult(
            email=email,
            status=status,
            # NeverBounce returns a discrete result, not a 0-1 score;
            # map the confident buckets to 1.0, ambiguous to 0.5.
            confidence=1.0 if result in ("valid", "invalid") else 0.5,
            is_catch_all=(result == "catchall") or ("has_dns" in flags
                          and "accepts_all" in flags),
            is_disposable=(result == "disposable")
                          or ("disposable_email" in flags),
            is_role=("role_account" in flags),
            raw=resp,
        )

    def cost_estimate(self, count: int = 1) -> dict:
        n = max(1, count)
        return {
            "credits": n,
            "approx_usd": round(n * 0.008, 4),
            "notes": ("NeverBounce pay-as-you-go ~$0.008/verify (lower on "
                      "volume tiers). 1 credit per email checked."),
        }


# Auto-register
try:
    register(NeverBounceVerifier())
except Exception as _e:
    print(f"[narada/neverbounce] register failed: "
          f"{type(_e).__name__}: {_e}", flush=True)
