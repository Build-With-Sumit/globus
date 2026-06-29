"""ElevenLabs custom-LLM endpoint — voice path orchestrator wrapper.

The architecture: the chat page boots ElevenLabs' browser SDK, which
opens a WebSocket to ElevenLabs' cloud. EL does ASR, then calls THIS
endpoint (configured as the agent's "custom LLM" in EL's dashboard)
with an OpenAI-shape `/chat/completions` request. We verify the
voice_token (HMAC, member-scoped, 6h TTL), drop ASR noise, run the
member's question through the chat orchestrator (same brain as text
chat — `globus_chat_send` does all the heavy lifting), and return
an OpenAI-shape response. EL then TTS-es it back to the browser.

What's NOT in v0.4 OSS:
  - The DeepSeek-V3 → Claude-CLI fallback chain (the orchestrator's
    `globus_call_chat` handles provider switching already).
  - True progressive SSE token streaming. We send the full reply as
    one chunk; EL starts TTS as soon as it lands. Good enough for
    most installs; replace voice_llm_sse_chunks() if you need
    word-by-word streaming.
  - Per-turn keepalive thread (the prod path emits filler audio
    during long tool calls to prevent EL hanging up). Tool loops
    that take >25s on the OSS path will see EL time out — keep
    `read_file` calls fast.

Module deps: voice_helpers (token verify, noise filter), voice_providers
(OpenAI-shape response builders), globus_orchestrator (the brain).
All already wired by globus_server at startup.
"""
from __future__ import annotations
import json

from voice_helpers import voice_token_verify, is_voice_noise
from voice_providers import (
    voice_llm_response_json, voice_llm_sse_chunks,
    GLOBUS_VOICE_MAX_OUTPUT_TOKENS,
)
from globus_orchestrator import globus_chat_send


# Hard cap on output tokens for voice — short replies are mandatory.
# A 500-token wall of text takes 25+ seconds to TTS, which kills the
# back-and-forth feel and triggers ElevenLabs' interrupt detection.
VOICE_MAX_OUTPUT_TOKENS = GLOBUS_VOICE_MAX_OUTPUT_TOKENS


# ─────────────────────────────────────────────────────────────────────
# Auth — voice_token comes from ElevenLabs as either an Authorization
# header (Bearer scheme) or in the request body's `voice_token` field.
# The browser passes it as a dynamic variable on session start; EL
# forwards it to our LLM endpoint as a header per its custom-LLM spec.
# ─────────────────────────────────────────────────────────────────────

def extract_voice_token(headers, body):
    """Pull the voice token from `Authorization: Bearer ...` or, falling
    back, from a `voice_token` field on the body (helpful for dev/curl
    testing). Returns the raw token string or '' if missing."""
    auth = headers.get("Authorization") or headers.get("authorization") or ""
    if auth.lower().startswith("bearer "):
        token = auth.split(None, 1)[1].strip()
        if token:
            return token
    if isinstance(body, dict):
        return (body.get("voice_token") or "").strip()
    return ""


def authenticate_voice_request(headers, body):
    """Verify the voice token and return the member email, or '' if
    invalid/expired/missing. Caller turns '' into a 401."""
    token = extract_voice_token(headers, body)
    if not token:
        return ""
    return voice_token_verify(token) or ""


# ─────────────────────────────────────────────────────────────────────
# Request handler — called from globus_server.do_POST when the route
# matches /api/globus/voice-llm/chat/completions
# ─────────────────────────────────────────────────────────────────────

def voice_chat_handle(email, request_body):
    """Take an OpenAI-shape chat-completions request from ElevenLabs,
    pull out the latest user message, run it through the orchestrator,
    return (reply, usage_dict, stream_requested).

    The conversation history EL sends back on every turn isn't used to
    rebuild context — the orchestrator pulls history from
    `globus_messages` (per-member, server-side source of truth). EL's
    history is only useful for the latest user turn.

    Returns:
      (reply, usage, stream)
        reply: assistant text to TTS back (may be '' if input was noise)
        usage: dict with input_tokens / output_tokens (for response shape)
        stream: bool — True if caller should emit SSE chunks instead of
                a JSON body. From request_body['stream']."""
    stream = bool(request_body.get("stream", False))
    messages = request_body.get("messages") or []

    # Find the latest user message.
    user_msg = ""
    for m in reversed(messages):
        if (m.get("role") or "").lower() == "user":
            user_msg = (m.get("content") or "").strip()
            break

    if not user_msg or is_voice_noise(user_msg):
        # Drop silently — EL TTSes '' as silence, which is what we want
        # for false-positive ASR triggers (typing, ambient noise, etc.).
        return "", {"input_tokens": 0, "output_tokens": 0,
                    "service_tier": "noise-filtered"}, stream

    reply, usage = globus_chat_send(email, user_msg)
    return reply, usage, stream


def voice_chat_format_response(reply, usage, stream, model_name):
    """Format the orchestrator response for the wire. Returns either:
      - (json_bytes, content_type='application/json') for non-streaming
      - (sse_string, content_type='text/event-stream') for streaming

    Caller writes the bytes directly to the response body."""
    if stream:
        # Concatenate the SSE chunks generator into a single body.
        body = "".join(voice_llm_sse_chunks(reply, model=model_name))
        return body.encode("utf-8"), "text/event-stream"
    payload = voice_llm_response_json(reply, usage, model=model_name)
    return json.dumps(payload).encode("utf-8"), "application/json"
