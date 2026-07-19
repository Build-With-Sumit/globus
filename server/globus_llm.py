"""Globus LLM client wrappers — extracted from lead_server.py 2026-06-28
as refactor slice #6v. Provider-dispatch + OpenAI-shape vs Anthropic-
shape glue. Every chat / voice / agent path goes through here to talk
to an LLM.

What's here:
  - GLOBUS_MODEL: default Anthropic model id (used by claude_raw +
    claude paths).
  - globus_call_chat(system, msgs, max_tokens, tools):
        provider dispatcher (cfg('GLOBUS_LLM_PROVIDER')). Defaults to
        claude-oauth via the local proxy; falls back to DeepSeek if
        the proxy fails so voice/chat never dies.
  - globus_call_claude_oauth(system, msgs, ...): hits the local
    claude-oauth proxy at 127.0.0.1:8787 — zero per-call API spend
    (Sumit's subscription).
  - globus_call_deepseek_chat(system, msgs, ...): DeepSeek-V3 direct
    (OpenAI-compatible API).
  - globus_call_claude_raw(system, msgs, ...): Anthropic API direct,
    returns the FULL response dict (caller inspects tool_use blocks).
  - globus_call_claude(system, msgs, max_tokens): Anthropic API
    direct with prompt caching on system prompt; returns (text, usage)
    tuple.
  - _anthropic_to_openai_shape(resp): glue used by globus_call_chat
    when provider=anthropic.

Module deps: cfg (db_helpers), urllib, json, os. No DB writes, no
configure() needed — cfg() reads happen on every call so config
changes take effect immediately without a restart.
"""
from __future__ import annotations
import json
import os
from urllib.request import Request, urlopen
from db_helpers import cfg


GLOBUS_MODEL = "claude-sonnet-4-6"


def globus_call_claude_oauth(system, messages, max_tokens=2000, tools=None,
                              model="sonnet"):
    """Drop-in replacement for globus_call_deepseek_chat that routes to
    Sumit's Claude OAuth subscription via the local proxy at 127.0.0.1:8787
    (claude_oauth_proxy.service). The proxy wraps `claude --print --model
    sonnet` and returns OpenAI-shape JSON. ZERO per-call API spend; bounded
    by subscription rate limits. Default model is Sonnet (faster than Opus —
    matters for voice turn latency); override via GLOBUS_OAUTH_MODEL.

    Same signature + return shape as globus_call_deepseek_chat so callers
    are symmetric.

    On failure, raises — caller should fall back to DeepSeek."""
    payload = {
        "model": model,
        "messages": [{"role": "system", "content": system}] + list(messages),
        "max_tokens": max_tokens,
        "temperature": 0.4,
    }
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"
    req = Request(
        "http://127.0.0.1:8787/v1/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"})
    with urlopen(req, timeout=120) as r:
        return json.loads(r.read().decode("utf-8"))


def globus_call_chat(system, messages, max_tokens=2000, tools=None,
                      model=None):
    """Dispatcher: picks the LLM provider for Globus chat/voice based on
    config flag GLOBUS_LLM_PROVIDER (DB cfg, env fallback). Falls back to
    DeepSeek if the preferred provider fails — so voice/chat never dies.

    `model` PINS the model tier for this one call. Leave it None for the
    interactive chat/voice path (which follows GLOBUS_OAUTH_MODEL), but pass
    it explicitly from any BATCH caller — a background job that inherits the
    chat brain's tier silently changes cost and behaviour the moment someone
    retunes chat, and being a batch job, nothing tells you. Cheap, bulk work
    (e.g. classifying a mailbox) should pin a small model; only work that
    genuinely needs judgement should pin a large one.

      claude-oauth (default) → Claude Sonnet via local OAuth proxy
                               (subscription, zero API spend)
      deepseek               → DeepSeek-V3 direct (legacy; not used by default)
      anthropic              → Anthropic API (Sonnet) direct

    The Globus brain is all-Claude: primary is OAuth-proxy Sonnet; if the proxy
    is down the fallback is the Anthropic API direct (also Claude), never
    DeepSeek. Returns OpenAI-shape dict identical to globus_call_deepseek_chat."""
    provider = (cfg("GLOBUS_LLM_PROVIDER", "claude-oauth")
                or "claude-oauth").strip().lower()
    if provider == "deepseek":
        return globus_call_deepseek_chat(system, messages, max_tokens, tools)
    if provider == "anthropic":
        resp = globus_call_claude_raw(system, messages, max_tokens, tools)
        return _anthropic_to_openai_shape(resp)
    try:
        return globus_call_claude_oauth(
            system, messages, max_tokens, tools,
            model=(model or cfg("GLOBUS_OAUTH_MODEL", "sonnet")))
    except Exception as e:
        # Stay on Claude: fall back to the Anthropic API direct (Sonnet),
        # not DeepSeek, so the Globus brain is always Claude.
        print(f"[globus-chat] OAuth proxy failed ({type(e).__name__}: "
              f"{e}), falling back to Anthropic API direct (Claude)", flush=True)
        resp = globus_call_claude_raw(system, messages, max_tokens, tools)
        return _anthropic_to_openai_shape(resp)


def _anthropic_to_openai_shape(claude_resp):
    """Convert Anthropic-shape response to OpenAI-shape so callers built
    around globus_call_deepseek_chat keep working unchanged."""
    content_blocks = claude_resp.get("content") or []
    text = "".join(b.get("text", "") for b in content_blocks
                   if b.get("type") == "text")
    tool_calls = []
    for b in content_blocks:
        if b.get("type") == "tool_use":
            tool_calls.append({
                "id": b.get("id"),
                "type": "function",
                "function": {
                    "name": b.get("name"),
                    "arguments": json.dumps(b.get("input") or {}),
                }})
    msg = {"role": "assistant", "content": text}
    if tool_calls:
        msg["tool_calls"] = tool_calls
    return {
        "id": claude_resp.get("id"),
        "choices": [{"index": 0, "message": msg,
                     "finish_reason": "stop"}],
        "model": claude_resp.get("model"),
        "usage": claude_resp.get("usage", {}),
    }


def globus_call_deepseek_chat(system, messages, max_tokens=2000, tools=None):
    """OpenAI-compatible DeepSeek chat completion with optional tools.
    Returns the full response dict (so caller can inspect tool_calls).
    System message is the first item in `messages`; we prepend it here
    so callers stay symmetric with the old globus_call_claude(system, msgs)
    signature."""
    api_key = (cfg("DEEPSEEK_API_KEY")
               or os.environ.get("DEEPSEEK_API_KEY") or "").strip()
    if not api_key:
        raise RuntimeError("DEEPSEEK_API_KEY not configured")
    payload = {
        "model": "deepseek-chat",
        "messages": [{"role": "system", "content": system}] + list(messages),
        "max_tokens": max_tokens,
        "temperature": 0.4,
    }
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"
    req = Request(
        "https://api.deepseek.com/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        })
    with urlopen(req, timeout=120) as r:
        return json.loads(r.read().decode("utf-8"))


def globus_call_claude_raw(system, messages, max_tokens=1500, tools=None):
    """Same as globus_call_claude but returns the FULL Anthropic response
    dict (so callers can inspect tool_use blocks). Used by the tool-use
    loop in globus_chat_send."""
    key = cfg("ANTHROPIC_API_KEY")
    if not key:
        raise RuntimeError("ANTHROPIC_API_KEY not configured")
    system_blocks = [{
        "type": "text",
        "text": system,
        "cache_control": {"type": "ephemeral"},
    }]
    body_dict = {
        "model": GLOBUS_MODEL,
        "max_tokens": max_tokens,
        "system": system_blocks,
        "messages": messages,
    }
    if tools:
        body_dict["tools"] = tools
    body = json.dumps(body_dict).encode()
    req = Request("https://api.anthropic.com/v1/messages",
                  data=body, method="POST",
                  headers={"x-api-key": key,
                           "anthropic-version": "2023-06-01",
                           "anthropic-beta": "prompt-caching-2024-07-31",
                           "content-type": "application/json"})
    with urlopen(req, timeout=120) as r:
        return json.loads(r.read().decode())


def globus_call_claude(system, messages, max_tokens=1500):
    """Anthropic Messages call with PROMPT CACHING on the system prompt.
    The persona+digest portion is identical across calls — caching it
    drops input costs 50-90% (Anthropic charges ~10% for cache hits and
    1.25x once for cache creation, valid for 5 min between calls).
    System prompt < 1024 tokens won't be cached (Anthropic minimum)."""
    key = cfg("ANTHROPIC_API_KEY")
    if not key:
        raise RuntimeError("ANTHROPIC_API_KEY not configured")
    system_blocks = [{
        "type": "text",
        "text": system,
        "cache_control": {"type": "ephemeral"},
    }]
    body = json.dumps({
        "model": GLOBUS_MODEL,
        "max_tokens": max_tokens,
        "system": system_blocks,
        "messages": messages,
    }).encode()
    req = Request("https://api.anthropic.com/v1/messages",
                  data=body, method="POST",
                  headers={"x-api-key": key,
                           "anthropic-version": "2023-06-01",
                           "anthropic-beta": "prompt-caching-2024-07-31",
                           "content-type": "application/json"})
    with urlopen(req, timeout=120) as r:
        d = json.loads(r.read().decode())
    parts = d.get("content") or []
    text = "".join(p.get("text", "") for p in parts if p.get("type") == "text").strip()
    usage = d.get("usage") or {}
    return text, usage
