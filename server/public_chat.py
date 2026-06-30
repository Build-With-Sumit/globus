"""Anonymous public-preview chat handler.

OPT-IN — disabled by default. Enable by setting:
  GLOBUS_PUBLIC_CHAT_ENABLED=1   (env or DB config)

What it is:
  A no-vault, no-tools, IP-rate-limited demo chat anyone can hit at
  POST /api/public/chat (and use via the chat box on /globus). The
  LLM sees only a brief "Globus is …" persona; no member data, no
  search tools, no agent triggers. Used for "let me try it" landing-
  page demos without exposing the real member surface.

Safety boundaries:
  - Default-off — fresh installs ship safe.
  - Per-IP rate limit (in-memory sliding window): 5 msgs / hour.
  - DB-backed daily cap per IP: 25 msgs / day (survives restarts).
  - 500-char input cap (truncated past that — no error).
  - 600-token output cap (short demo replies).
  - Hard-coded short persona — operator can edit but NOT inject vault.
  - Every request audited in `globus_public_chat_log` with status.

Caveats:
  - Spend is on YOUR LLM provider. Set GLOBUS_PUBLIC_CHAT_MAX_DAILY
    in DB config to cap install-wide daily count (default 500).
  - In-memory sliding window resets on container restart (DB daily
    cap is the durable boundary).
  - This is intentionally NOT for production-quality public chat —
    add Turnstile/recaptcha + a proper queue for that.
"""
from __future__ import annotations
import threading
import time

from db_helpers import db_read, db_write, cfg
from globus_llm import globus_call_chat


_PUBLIC_PERSONA = (
    "You are Globus — a demo of a private AI assistant that "
    "normally reads a member's own data (vault, email, chats, CRM) "
    "and answers from it. This is the PUBLIC preview: you have NO "
    "access to any vault and NO tools. Answer general questions "
    "about what Globus is, how it works, and how to install it "
    "(see https://github.com/Build-With-Sumit/globus). Keep replies "
    "short — 2-4 sentences. If asked to do anything that requires "
    "real data ('what's in my inbox?', 'search my files'), explain "
    "that the preview can't see anything; the member-side install "
    "at /members/login is where the real magic happens. Refuse "
    "off-topic asks (write me a poem, what's the weather) with a "
    "one-line redirect back to what Globus does."
)


# Per-IP sliding window for rate limiting. {ip: deque[unix_ts]} —
# entries older than the window get discarded on each call.
_RATE_LOCK = threading.Lock()
_RATE_HITS: dict = {}

PUBLIC_RATE_WINDOW_SEC = 3600        # 1 hour
PUBLIC_RATE_HITS_IN_WINDOW = 5       # 5 messages per IP per hour
PUBLIC_INPUT_MAX_CHARS = 500
PUBLIC_OUTPUT_MAX_TOKENS = 600


def is_enabled():
    """Check the opt-in flag every call — operator can flip it via
    DB config without a server restart (cfg() doesn't refresh, but
    the env var does)."""
    v = (cfg("GLOBUS_PUBLIC_CHAT_ENABLED", "") or "").strip().lower()
    return v in ("1", "true", "yes", "on")


# ─────────────────────────────────────────────────────────────────────
# Audit + rate-limit helpers
# ─────────────────────────────────────────────────────────────────────

def _audit(ip, user_agent, body_chars, reply_chars, status):
    try:
        db_write(
            "INSERT INTO globus_public_chat_log "
            "(ip, user_agent, body_chars, reply_chars, status) "
            "VALUES (%s, %s, %s, %s, %s)",
            (ip[:64], (user_agent or "")[:255],
             int(body_chars), int(reply_chars), status))
    except Exception as e:
        print(f"[public-chat-audit] write failed: "
              f"{type(e).__name__}: {e}", flush=True)


def _hit_ip(ip):
    """Record a hit + return True if the IP is now over the limit."""
    now = time.time()
    cutoff = now - PUBLIC_RATE_WINDOW_SEC
    with _RATE_LOCK:
        hits = _RATE_HITS.get(ip) or []
        hits = [t for t in hits if t > cutoff]
        hits.append(now)
        _RATE_HITS[ip] = hits
        return len(hits) > PUBLIC_RATE_HITS_IN_WINDOW


def _daily_cap_hit(ip):
    """DB-backed daily cap — survives restart, per-IP."""
    rows = db_read(
        "SELECT COUNT(*) AS n FROM globus_public_chat_log "
        "WHERE ip=%s AND status='ok' "
        "  AND created_at > NOW() - INTERVAL 1 DAY",
        (ip,))
    n = int(rows[0]["n"]) if rows else 0
    return n >= 25  # per-IP daily cap


def _install_cap_hit():
    """Install-wide daily cap so a coordinated abuse run can't bankrupt
    your LLM bill. Default 500/day; tune via DB config."""
    try:
        cap = int(cfg("GLOBUS_PUBLIC_CHAT_MAX_DAILY", "500") or "500")
    except (TypeError, ValueError):
        cap = 500
    rows = db_read(
        "SELECT COUNT(*) AS n FROM globus_public_chat_log "
        "WHERE status='ok' AND created_at > NOW() - INTERVAL 1 DAY")
    n = int(rows[0]["n"]) if rows else 0
    return n >= cap, n, cap


# ─────────────────────────────────────────────────────────────────────
# Public surface — called from globus_server's route handler
# ─────────────────────────────────────────────────────────────────────

def public_chat_send(ip, user_agent, message):
    """Handle one anonymous chat send. Returns dict:
      {ok, reply, error?}
    Never raises — every failure path is captured + audited."""
    if not is_enabled():
        return {"ok": False,
                "error": "public chat is disabled on this install"}
    msg = (message or "").strip()[:PUBLIC_INPUT_MAX_CHARS]
    if not msg:
        return {"ok": False, "error": "empty message"}

    # Per-IP sliding window
    if _hit_ip(ip):
        _audit(ip, user_agent, len(msg), 0, "rate_limited")
        return {"ok": False,
                "error": (f"rate limit — max "
                          f"{PUBLIC_RATE_HITS_IN_WINDOW} messages per "
                          f"hour from your IP. Try again later or "
                          f"sign in at /members/login.")}

    # DB-backed daily caps (per-IP + install-wide)
    if _daily_cap_hit(ip):
        _audit(ip, user_agent, len(msg), 0, "rate_limited")
        return {"ok": False,
                "error": "daily limit from your IP reached. Sign in at "
                         "/members/login for the full vault chat."}
    install_full, count_so_far, cap = _install_cap_hit()
    if install_full:
        _audit(ip, user_agent, len(msg), 0, "blocked")
        return {"ok": False,
                "error": f"public demo at capacity for today "
                          f"({count_so_far}/{cap}). Sign in at "
                          f"/members/login for the unrestricted path."}

    try:
        resp = globus_call_chat(
            system=_PUBLIC_PERSONA,
            messages=[{"role": "user", "content": msg}],
            max_tokens=PUBLIC_OUTPUT_MAX_TOKENS)
        # OpenAI-shape: {"choices": [{"message": {"content": "..."}}]}
        choice = (resp.get("choices") or [{}])[0]
        reply = ((choice.get("message") or {}).get("content") or "").strip()
    except Exception as e:
        _audit(ip, user_agent, len(msg), 0, "error")
        return {"ok": False,
                "error": f"LLM call failed: {type(e).__name__}"}

    reply = reply or "(empty reply from LLM)"
    _audit(ip, user_agent, len(msg), len(reply), "ok")
    return {"ok": True, "reply": reply}
