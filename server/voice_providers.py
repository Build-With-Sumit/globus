"""Voice LLM provider calls + OpenAI-shape response builders — extracted
from lead_server.py 2026-06-27 as refactor slice #3b.

Functions:
  - deepseek_voice_stream(): SSE-streaming generator (currently unused).
  - deepseek_voice_call():   non-streaming DeepSeek call. Used by
    lead_server.globus_voice_llm_call() when GLOBUS_VOICE_PROVIDER=deepseek.
  - voice_llm_response_json(): wrap a reply in OpenAI chat-completions
    response shape (non-streaming).
  - voice_llm_sse_chunks():   emit OpenAI streaming SSE chunks.

⚠️ DEEPSEEK MODEL — 2026-07-24 deprecation, NOT a trivial rename:
   `deepseek-chat` is being deprecated 2026-07-24 15:59 UTC. The
   replacements DeepSeek currently exposes (`deepseek-v4-flash`,
   `deepseek-v4-pro`) are REASONING-ONLY models: all output lands in
   `reasoning_content` and `content` stays empty. Tested 2026-06-28 with
   reasoning_effort=none / thinking=false / enable_thinking=false /
   thinking_budget=0 — none turn off thinking. So `model:` here can't
   just flip to v4-flash without also parsing reasoning_content into the
   reply (or switching providers). Decision deferred — see TODO.md.

Module config injected at startup via configure(). Uses a callable for
the DeepSeek API key so we preserve the per-call cfg() re-evaluation
behaviour the inline version had.
"""
from __future__ import annotations
import json
import secrets
import time
from urllib.error import HTTPError
from urllib.request import Request, urlopen


# Public — request-validation constants used by the route handler too.
GLOBUS_VOICE_MAX_OUTPUT_TOKENS = 220   # voice = short replies
GLOBUS_VOICE_LLM_TIMEOUT = 30          # claude CLI per-turn cap (sec)

# Module state set via configure().
_DEEPSEEK_API_KEY_GETTER = lambda: ""  # callable; preserves per-call refresh
_DEFAULT_MODEL = "claude-sonnet-4-6"


def configure(*, deepseek_api_key_getter, default_model,
              llm_timeout=GLOBUS_VOICE_LLM_TIMEOUT):
    """Initialize the module. Called once at server startup from
    lead_server.py.

    deepseek_api_key_getter: a no-arg callable that returns the current
      DeepSeek API key (string, possibly empty). Using a callable
      preserves per-call refresh behaviour for the cfg() lookup.
    default_model: model name used in OpenAI-shape response payloads
      when the caller doesn't override (e.g. "claude-sonnet-4-6").
    llm_timeout: per-call HTTP timeout in seconds. Defaults to 30s
      (the legacy GLOBUS_VOICE_LLM_TIMEOUT value).
    """
    global _DEEPSEEK_API_KEY_GETTER, _DEFAULT_MODEL, GLOBUS_VOICE_LLM_TIMEOUT
    if not callable(deepseek_api_key_getter):
        raise TypeError("deepseek_api_key_getter must be callable")
    _DEEPSEEK_API_KEY_GETTER = deepseek_api_key_getter
    _DEFAULT_MODEL = default_model
    GLOBUS_VOICE_LLM_TIMEOUT = llm_timeout


def deepseek_voice_stream(persona, chat_msgs, max_tokens):
    """Generator that streams DeepSeek's response token-by-token. Yields
    (delta_text, finish_reason). True progressive streaming so ElevenLabs
    can start TTS on the first chunk and the user can barge-in mid-reply
    with fine granularity. Used when GLOBUS_VOICE_PROVIDER=deepseek and
    the client requested stream=True."""
    api_key = (_DEEPSEEK_API_KEY_GETTER() or "").strip()
    if not api_key:
        raise RuntimeError("DEEPSEEK_API_KEY not set")
    payload = {
        "model": "deepseek-chat",
        "messages": [{"role": "system", "content": persona}] + chat_msgs,
        "max_tokens": max_tokens,
        "temperature": 0.4,
        "stream": True,
    }
    req = Request(
        "https://api.deepseek.com/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        })
    resp = urlopen(req, timeout=GLOBUS_VOICE_LLM_TIMEOUT)
    for raw_line in resp:
        line = raw_line.decode("utf-8", errors="replace").rstrip()
        if not line or not line.startswith("data:"):
            continue
        payload_str = line[5:].strip()
        if payload_str == "[DONE]":
            break
        try:
            evt = json.loads(payload_str)
        except Exception:
            continue
        choice = (evt.get("choices") or [{}])[0]
        delta = (choice.get("delta") or {}).get("content") or ""
        finish = choice.get("finish_reason")
        if delta or finish:
            yield delta, finish


def deepseek_voice_call(persona, chat_msgs, max_tokens):
    """Call DeepSeek's OpenAI-compatible chat-completions API for voice.
    Returns (reply_text or None, usage_dict or error_string).

    ~12x cheaper than Anthropic API ($0.27/M in, $1.10/M out vs $3/$15)
    and unaffected by Anthropic quota/rate-limit issues. Used when
    GLOBUS_VOICE_PROVIDER=deepseek."""
    api_key = (_DEEPSEEK_API_KEY_GETTER() or "").strip()
    if not api_key:
        return None, "DEEPSEEK_API_KEY not set"
    payload = {
        "model": "deepseek-chat",
        "messages": [{"role": "system", "content": persona}] + chat_msgs,
        "max_tokens": max_tokens,
        "temperature": 0.4,
    }
    req = Request(
        "https://api.deepseek.com/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        })
    try:
        with urlopen(req, timeout=GLOBUS_VOICE_LLM_TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        reply = (data["choices"][0]["message"]["content"] or "").strip()
        u = data.get("usage", {}) or {}
        return reply, {
            "input_tokens":  int(u.get("prompt_tokens", 0) or 0),
            "output_tokens": int(u.get("completion_tokens", 0) or 0),
            "service_tier":  "deepseek",
            "cache_hit_tokens": int(u.get("prompt_cache_hit_tokens", 0) or 0),
        }
    except HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")[:300]
        return None, f"HTTP {e.code}: {body}"
    except Exception as e:
        return None, f"{type(e).__name__}: {e}"


def voice_llm_response_json(reply, usage, model=None):
    """Wrap a Claude reply in OpenAI chat-completions response shape
    (non-streaming)."""
    if model is None:
        model = _DEFAULT_MODEL
    return {
        "id": "chatcmpl-globus-" + secrets.token_hex(8),
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": reply},
            "finish_reason": "stop",
        }],
        "usage": {
            "prompt_tokens":     int(usage.get("input_tokens", 0) or 0),
            "completion_tokens": int(usage.get("output_tokens", 0) or 0),
            "total_tokens":      int(usage.get("input_tokens", 0) or 0)
                                  + int(usage.get("output_tokens", 0) or 0),
        },
    }


def voice_llm_sse_chunks(reply, model=None):
    """Emit OpenAI streaming SSE chunks for `reply`. We send a single
    content chunk + a finish chunk + [DONE], not true progressive
    streaming. Enough for ElevenLabs to start TTS as soon as it arrives."""
    if model is None:
        model = _DEFAULT_MODEL
    chat_id = "chatcmpl-globus-" + secrets.token_hex(8)
    now = int(time.time())

    def chunk(delta, finish_reason=None):
        return json.dumps({
            "id": chat_id,
            "object": "chat.completion.chunk",
            "created": now,
            "model": model,
            "choices": [{
                "index": 0,
                "delta": delta,
                "finish_reason": finish_reason,
            }],
        })

    yield "data: " + chunk({"role": "assistant", "content": ""}) + "\n\n"
    yield "data: " + chunk({"content": reply}) + "\n\n"
    yield "data: " + chunk({}, finish_reason="stop") + "\n\n"
    yield "data: [DONE]\n\n"
