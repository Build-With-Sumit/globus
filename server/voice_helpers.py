"""Voice helper module — extracted from lead_server.py 2026-06-27 as
refactor slice #3a (first sub-slice of the voice carve-out).

Self-contained text utilities + HMAC token gen/verify for the Globus
voice path. Zero deps on lead_server. SESSION_SECRET is injected at
startup via configure() so we don't drag in lead_server's import chain.

Future sub-slices land in their own modules:
  #3b: voice_providers.py (deepseek + claude-CLI + response shaping)
  #3c: voice_context.py  (_voice_build_context + persona helpers)
"""
from __future__ import annotations
import hashlib
import hmac
import time


# Module config. SESSION_SECRET is the HMAC key used to sign voice
# tokens — must match what verified them on issue (i.e. the same
# server process). Set once via configure() at lead_server startup.
_SESSION_SECRET: bytes = b""

# 6h — covers a long session; user pages refresh on auth events anyway.
GLOBUS_VOICE_TOKEN_TTL_SEC = 6 * 3600


def configure(*, session_secret):
    """Initialize the module. Called once at server startup from
    lead_server.py after SESSION_SECRET is resolved. session_secret
    MUST be bytes (the HMAC key for token signing)."""
    global _SESSION_SECRET
    if not isinstance(session_secret, (bytes, bytearray)):
        raise TypeError("session_secret must be bytes")
    _SESSION_SECRET = bytes(session_secret)


def voice_token_make(email):
    """Return a time-limited HMAC token binding the email to this server.
    Format: `email|expires_unix|hex_hmac`."""
    expires = int(time.time()) + GLOBUS_VOICE_TOKEN_TTL_SEC
    payload = f"{email.lower()}|{expires}"
    sig = hmac.new(_SESSION_SECRET, payload.encode("utf-8"),
                   hashlib.sha256).hexdigest()
    return f"{payload}|{sig}"


def voice_token_verify(token):
    """Return the email if token is valid + unexpired, else None."""
    if not token or "|" not in token:
        return None
    try:
        email, expires_str, sig = token.rsplit("|", 2)
        expires = int(expires_str)
    except (ValueError, AttributeError):
        return None
    if expires < int(time.time()):
        return None
    payload = f"{email}|{expires}"
    expected = hmac.new(_SESSION_SECRET, payload.encode("utf-8"),
                        hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expected):
        return None
    return email.lower()


# ASR (Whisper-class) hallucinates these strings from silence / keyboard
# clicks / ambient noise. Treat as noise and drop silently instead of
# letting Globus respond "Sorry?" to every typing sound after a reply.
_VOICE_NOISE_HALLUCINATIONS = frozenset({
    "", ".", "..", "...", "…", "?", "!",
    "you", "you you", "the",
    "thank you", "thanks", "thanks.",
    "thanks for watching", "thank you for watching",
    "thanks for watching!", "thank you for watching.",
    "mm", "mmm", "mhm", "hmm", "hmmm",
    "uh", "um", "uhm", "ah", "oh", "huh", "eh",
    "bye", "bye.",
})


def is_voice_noise(text):
    """True iff text looks like an ASR hallucination (silence / typing /
    ambient noise misheard as speech). Conservative: only matches the
    well-known Whisper noise vocabulary so we don't drop real short
    answers like 'yes' / 'no' / 'okay'."""
    t = (text or "").strip().lower().strip(" .,!?…\"'")
    if not t:
        return True
    if t in _VOICE_NOISE_HALLUCINATIONS:
        return True
    # 1-2 char garbage that isn't a real word
    if len(t) <= 2 and t not in ("ok", "no", "hi", "ya", "wb", "gm"):
        return True
    return False


# Server-emitted "holding" chunks that should be stripped from history
# before the LLM sees prior assistant turns. Otherwise the LLM mimics the
# pattern ("Let me search your drive...") on subsequent turns without
# actually calling tools — perceived as hallucination.
_VOICE_HOLDING_PHRASES = (
    "let me search your drive for that...",
    "checking your inbox...",
    "pulling that file up...",
    "one second...",
)


def strip_voice_holding(text):
    """Remove any server-emitted holding phrase from the START of an
    assistant message. Why: ElevenLabs sends the full conversation
    history on every LLM call. If our holding chunk landed in history,
    the LLM would mimic the pattern on subsequent turns WITHOUT
    actually calling tools. Strip them before they reach LLM context."""
    if not text:
        return text
    stripped = text.lstrip()
    lowered = stripped.lower()
    for phrase in _VOICE_HOLDING_PHRASES:
        if lowered.startswith(phrase):
            return stripped[len(phrase):].lstrip()
    return text
