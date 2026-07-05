"""ZeroBounce plugin — implements the Verifier protocol.

ZeroBounce (zerobounce.net) is a dedicated email-verification service
with one of the larger known-trap databases in the space — it flags
spamtraps and known abuse/complainer mailboxes explicitly, which most
verifiers lump into "risky". Pair it with any lead-source (Prospeo,
Apollo, RocketReach) for a second-opinion verify separate from the
vendor that sold you the list.

Auth: API key (32-hex string from the ZeroBounce dashboard → API),
paste into /members/narada/credentials → tool slug `zerobounce`.
Per-member isolation via the globus_narada_credentials vault.

API: GET https://api.zerobounce.net/v2/validate
  ?api_key=<key>&email=<addr>&ip_address=   (ip_address optional)
Response: {address, status: valid|invalid|catch-all|unknown|spamtrap|
abuse|do_not_mail, sub_status: disposable|role_based|toxic|..., ...}.
Auth/credit failures come back in-body as {"error": "..."} (sometimes
with a 4xx code). Docs: https://www.zerobounce.net/docs/
Pricing: 100 free validations/month; pay-as-you-go from ~$0.009/verify,
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


ZEROBOUNCE_API_BASE = "https://api.zerobounce.net/v2"
ZEROBOUNCE_TIMEOUT = 30
ZEROBOUNCE_UA = "Narada/1.0 (outbound-agent)"


def _api_key(member_email: str) -> str | None:
    cred = get_credential(member_email, "zerobounce")
    if not cred:
        return None
    return (cred.get("api_key") or "").strip() or None


def _get(member_email: str, path: str, params: dict) -> dict:
    """GET ZeroBounce's REST API + return parsed JSON. Never raises —
    on transport/HTTP error returns {'error': '...'}."""
    api_key = _api_key(member_email)
    if not api_key:
        return {"error": "no ZeroBounce credential for this member"}
    q = urlencode({**params, "api_key": api_key})
    url = f"{ZEROBOUNCE_API_BASE}{path}?{q}"
    req = Request(url, method="GET",
                  headers={"User-Agent": ZEROBOUNCE_UA})
    try:
        with urlopen(req, timeout=ZEROBOUNCE_TIMEOUT) as r:
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
    # ZeroBounce reports bad key / out-of-credits in-body via "error"
    # (occasionally alongside HTTP 200) — pass it through as-is.
    if isinstance(resp, dict) and resp.get("error"):
        return {"error": str(resp.get("error"))}
    if not isinstance(resp, dict):
        return {"error": f"unexpected response shape: {type(resp).__name__}"}
    touch_last_used(member_email, "zerobounce")
    return resp


class ZeroBounceVerifier:

    @classmethod
    def info(cls) -> PluginInfo:
        return PluginInfo(
            name="zerobounce",
            display_name="ZeroBounce",
            category=PluginCategory.VERIFIER,
            auth_method=AuthMethod.API_KEY,
            requires_credentials=["api_key"],
            homepage="https://www.zerobounce.net",
            docs_url="https://www.zerobounce.net/docs/",
            description=(
                "Email verification with explicit spamtrap and abuse "
                "(known-complainer) detection on top of the usual "
                "valid/invalid/catch-all buckets. 100 free checks per "
                "month, then pay-as-you-go from ~$0.009/verify. Paste "
                "the API key from your ZeroBounce dashboard."
            ),
            free_tier=True,
        )

    def is_available(self, member_email: str) -> bool:
        return has_credential(member_email, "zerobounce")

    def verify(self, member_email: str, email: str) -> VerifyResult:
        if not email:
            return VerifyResult(email="", status=VerifyStatus.INVALID,
                                raw={"reason": "empty email"})
        # ip_address is optional per current docs; blank is fine and
        # matches ZeroBounce's own request examples.
        resp = _get(member_email, "/validate",
                    {"email": email, "ip_address": ""})
        if "error" in resp:
            return VerifyResult(email=email, status=VerifyStatus.ERROR,
                                raw=resp)
        status_str = (resp.get("status") or "").lower()
        sub_status = (resp.get("sub_status") or "").lower()
        mapping = {
            "valid":       VerifyStatus.VALID,
            "invalid":     VerifyStatus.INVALID,
            "catch-all":   VerifyStatus.RISKY,
            # Spamtraps hard-kill sender reputation — treat as invalid.
            "spamtrap":    VerifyStatus.INVALID,
            # Abuse = real mailbox but a known complainer; do_not_mail =
            # role/disposable/toxic domain. Both are send-at-own-risk.
            "abuse":       VerifyStatus.RISKY,
            "do_not_mail": VerifyStatus.RISKY,
            "unknown":     VerifyStatus.UNKNOWN,
        }
        status = mapping.get(status_str, VerifyStatus.UNKNOWN)
        return VerifyResult(
            email=email,
            status=status,
            # ZeroBounce /validate returns a discrete result, not a 0-1
            # score (that's their separate Scoring API) — map the
            # confident buckets to 1.0, ambiguous to 0.5.
            confidence=1.0 if status_str in ("valid", "invalid",
                                             "spamtrap") else 0.5,
            is_catch_all=(status_str == "catch-all")
                         or (sub_status == "role_based_catch_all"),
            is_disposable=(sub_status == "disposable"),
            is_role=(sub_status in ("role_based",
                                    "role_based_catch_all")),
            raw=resp,
        )

    def cost_estimate(self, count: int = 1) -> dict:
        n = max(1, count)
        return {
            "credits": n,
            "approx_usd": round(n * 0.009, 4),
            "notes": ("ZeroBounce ~$0.009/verify pay-as-you-go (lower on "
                      "volume tiers); 100 free credits/month. 1 credit "
                      "per email validated."),
        }


# Auto-register
try:
    register(ZeroBounceVerifier())
except Exception as _e:
    print(f"[narada/zerobounce] register failed: "
          f"{type(_e).__name__}: {_e}", flush=True)
