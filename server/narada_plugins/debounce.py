"""DeBounce plugin — implements the Verifier protocol.

DeBounce (debounce.com) is a budget email-verification service — one
of the cheapest per-verify vendors around, popular with agencies that
clean big cold-outreach lists. Pair it with any lead-source (Prospeo,
Apollo, RocketReach) as a cheap first-pass filter before spending
sender reputation.

Auth: API key, created under DeBounce dashboard → API. Paste into
/members/narada/credentials → tool slug `debounce`. Per-member
isolation via the globus_narada_credentials vault.

API: GET https://api.debounce.io/v1/?api=<key>&email=<addr>
Response: {"debounce": {code, result, reason, role, free_email,
send_transactional, did_you_mean}, "success": "1", "balance": "..."}.
Result codes: 1=syntax, 2=spam-trap, 3=disposable, 4=accept-all,
5=deliverable, 6=invalid (bounce), 7=unknown, 8=role. On failure
success=="0" and debounce.error carries the message.
Docs: https://developers.debounce.com/
Pricing: prepaid credits from ~$0.002/verify (5k tier), dropping to
~$0.0005/verify on 100k+ volume tiers.
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


DEBOUNCE_API_BASE = "https://api.debounce.io/v1"
DEBOUNCE_TIMEOUT = 30
DEBOUNCE_UA = "Narada/1.0 (outbound-agent; +https://buildwithsumit.com)"


def _api_key(member_email: str) -> str | None:
    cred = get_credential(member_email, "debounce")
    if not cred:
        return None
    return (cred.get("api_key") or "").strip() or None


def _get(member_email: str, params: dict) -> dict:
    """GET DeBounce's REST API + return parsed JSON. Never raises —
    on transport/HTTP error returns {'error': '...'}."""
    api_key = _api_key(member_email)
    if not api_key:
        return {"error": "no DeBounce credential for this member"}
    q = urlencode({**params, "api": api_key})
    url = f"{DEBOUNCE_API_BASE}/?{q}"
    req = Request(url, method="GET",
                  headers={"User-Agent": DEBOUNCE_UA,
                           "Accept": "application/json"})
    try:
        with urlopen(req, timeout=DEBOUNCE_TIMEOUT) as r:
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
    # DeBounce returns 200 with success=="0" for API-level errors
    # (bad key, no balance, malformed request); the message lives in
    # debounce.error.
    if str(resp.get("success", "")) == "0":
        detail = (resp.get("debounce") or {}).get("error", "") \
                 or resp.get("error", "") or "unknown API error"
        return {"error": f"api failure: {detail}"}
    touch_last_used(member_email, "debounce")
    return resp


class DeBounceVerifier:

    @classmethod
    def info(cls) -> PluginInfo:
        return PluginInfo(
            name="debounce",
            display_name="DeBounce",
            category=PluginCategory.VERIFIER,
            auth_method=AuthMethod.API_KEY,
            requires_credentials=["api_key"],
            homepage="https://debounce.com",
            docs_url="https://developers.debounce.com/",
            description=(
                "Budget email verification — one of the cheapest "
                "per-verify vendors (from ~$0.002/verify, less on "
                "volume). Flags syntax errors, spam traps, disposables, "
                "catch-alls and role accounts. Paste your dashboard "
                "API key."
            ),
            free_tier=False,
        )

    def is_available(self, member_email: str) -> bool:
        return has_credential(member_email, "debounce")

    def verify(self, member_email: str, email: str) -> VerifyResult:
        if not email:
            return VerifyResult(email="", status=VerifyStatus.INVALID,
                                raw={"reason": "empty email"})
        resp = _get(member_email, {"email": email})
        if "error" in resp:
            return VerifyResult(email=email, status=VerifyStatus.ERROR,
                                raw=resp)
        deb = resp.get("debounce") or {}
        code = str(deb.get("code") or "").strip()
        mapping = {
            "1": VerifyStatus.INVALID,   # syntax error
            "2": VerifyStatus.INVALID,   # spam trap — never send
            "3": VerifyStatus.RISKY,     # disposable
            "4": VerifyStatus.RISKY,     # accept-all / catch-all
            "5": VerifyStatus.VALID,     # deliverable
            "6": VerifyStatus.INVALID,   # verified bounce
            "7": VerifyStatus.UNKNOWN,   # server unresponsive
            # Per DeBounce docs code 8 (role) is NOT emitted by the
            # API — role arrives via the `role` field instead (handled
            # below). Kept defensively in case that ever changes.
            "8": VerifyStatus.RISKY,     # role account (info@, sales@)
        }
        status = mapping.get(code, VerifyStatus.UNKNOWN)
        return VerifyResult(
            email=email,
            status=status,
            # DeBounce returns a discrete code, not a 0-1 score; map
            # the confident buckets to 1.0, ambiguous to 0.5.
            confidence=1.0 if code in ("1", "2", "5", "6") else 0.5,
            is_catch_all=(code == "4"),
            is_disposable=(code == "3"),
            is_role=(code == "8")
                    or (str(deb.get("role") or "").lower() == "true"),
            raw=resp,
        )

    def cost_estimate(self, count: int = 1) -> dict:
        n = max(1, count)
        return {
            "credits": n,
            "approx_usd": round(n * 0.002, 4),
            "notes": ("DeBounce prepaid credits ~$0.002/verify at the "
                      "5k tier, down to ~$0.0005 on 100k+ volume. "
                      "1 credit per email checked."),
        }


# Auto-register
try:
    register(DeBounceVerifier())
except Exception as _e:
    print(f"[narada/debounce] register failed: "
          f"{type(_e).__name__}: {_e}", flush=True)
