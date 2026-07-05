"""MillionVerifier plugin — implements the Verifier protocol.

MillionVerifier (millionverifier.com) is a budget-friendly email
verification service — among the cheapest per-verify on the market.
Pair it with any lead-source (Prospeo, Apollo, RocketReach) when the
marketer wants a low-cost deliverability check before a send, or a
second opinion separate from the lead vendor.

Auth: API key, paste into /members/narada/credentials → tool slug
`millionverifier`, credential field `api_key`. Per-member isolation
via the globus_narada_credentials vault.

API: GET https://api.millionverifier.com/api/v3/
  ?api=<key>&email=<addr>&timeout=<2-60s>
Response: {email, result: ok|catch_all|unknown|error|disposable|
invalid, resultcode: 1-6, quality: good|risky|bad, subresult,
free: bool, role: bool, didyoumean, credits, error: ""}. API-level
failures (invalid key, no credits) come back HTTP 200 with a
non-empty `error` field. Docs: https://developer.millionverifier.com/
Pricing: credit packs from ~$37/10k (~$0.0037/verify); free trial
credits on signup.
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


MILLIONVERIFIER_API_BASE = "https://api.millionverifier.com/api/v3"
MILLIONVERIFIER_TIMEOUT = 30      # our socket timeout (urlopen)
MILLIONVERIFIER_API_TIMEOUT = 20  # their server-side SMTP probe timeout
USER_AGENT = "Narada/1.0 (outbound agent; +https://globussoft.ai)"


def _api_key(member_email: str) -> str | None:
    cred = get_credential(member_email, "millionverifier")
    if not cred:
        return None
    return (cred.get("api_key") or "").strip() or None


def _get(member_email: str, params: dict) -> dict:
    """GET MillionVerifier's REST API + return parsed JSON. Never
    raises — on transport/HTTP/API error returns {'error': '...'}."""
    api_key = _api_key(member_email)
    if not api_key:
        return {"error": "no MillionVerifier credential for this member"}
    q = urlencode({**params, "api": api_key})
    url = f"{MILLIONVERIFIER_API_BASE}/?{q}"
    req = Request(url, method="GET",
                  headers={"User-Agent": USER_AGENT})
    try:
        with urlopen(req, timeout=MILLIONVERIFIER_TIMEOUT) as r:
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
    if not isinstance(resp, dict):
        return {"error": f"unexpected response shape: {type(resp).__name__}"}
    # MillionVerifier returns 200 with a non-empty `error` field for
    # API-level errors (api_key_invalid, insufficient credits, ...).
    if (resp.get("error") or "").strip():
        return {"error": str(resp.get("error")), "raw": resp}
    touch_last_used(member_email, "millionverifier")
    return resp


class MillionVerifierVerifier:

    @classmethod
    def info(cls) -> PluginInfo:
        return PluginInfo(
            name="millionverifier",
            display_name="MillionVerifier",
            category=PluginCategory.VERIFIER,
            auth_method=AuthMethod.API_KEY,
            requires_credentials=["api_key"],
            homepage="https://millionverifier.com",
            docs_url="https://developer.millionverifier.com/",
            description=(
                "Budget email verification — one of the cheapest per "
                "check (~$0.0037/verify on the 10k pack). Catches "
                "invalid, disposable and catch-all addresses before "
                "you send. Free trial credits on signup; paste your "
                "API key from the MillionVerifier dashboard."
            ),
            free_tier=True,
        )

    def is_available(self, member_email: str) -> bool:
        return has_credential(member_email, "millionverifier")

    def verify(self, member_email: str, email: str) -> VerifyResult:
        if not email:
            return VerifyResult(email="", status=VerifyStatus.INVALID,
                                raw={"reason": "empty email"})
        resp = _get(member_email,
                    {"email": email,
                     "timeout": MILLIONVERIFIER_API_TIMEOUT})
        if resp.get("error"):
            return VerifyResult(email=email, status=VerifyStatus.ERROR,
                                raw=resp)
        result = (resp.get("result") or "").lower()
        mapping = {
            "ok":         VerifyStatus.VALID,
            "invalid":    VerifyStatus.INVALID,
            "disposable": VerifyStatus.RISKY,
            "catch_all":  VerifyStatus.RISKY,
            "unknown":    VerifyStatus.UNKNOWN,
            # result == "error" (resultcode 4): their probe itself
            # failed — treat as an API error, not a verdict.
            "error":      VerifyStatus.ERROR,
        }
        status = mapping.get(result, VerifyStatus.UNKNOWN)
        return VerifyResult(
            email=email,
            status=status,
            # MillionVerifier returns a discrete result, not a 0-1
            # score; map the confident buckets to 1.0, ambiguous to 0.5.
            confidence=1.0 if result in ("ok", "invalid") else 0.5,
            is_catch_all=(result == "catch_all"),
            is_disposable=(result == "disposable"),
            is_role=bool(resp.get("role")),
            raw=resp,
        )

    def cost_estimate(self, count: int = 1) -> dict:
        n = max(1, count)
        return {
            "credits": n,
            "approx_usd": round(n * 0.0037, 4),
            "notes": ("MillionVerifier packs from ~$37/10k "
                      "(~$0.0037/verify), cheaper on volume tiers. "
                      "1 credit per email checked."),
        }


# Auto-register
try:
    register(MillionVerifierVerifier())
except Exception as _e:
    print(f"[narada/millionverifier] register failed: "
          f"{type(_e).__name__}: {_e}", flush=True)
