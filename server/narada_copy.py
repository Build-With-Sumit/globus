"""Narada copy generation — produces N personalised email variants
per prospect using the existing globus_call_chat LLM.

Per the PRD: 3 variants per prospect by default, scored by the
LLM itself so the human reviewer sees a 'best' suggestion. Pulls
from globus_narada_angle_memory when present — once a member has
run a few campaigns, prior winning patterns get injected into the
prompt as "what's worked for you before in this ICP."

No external deps beyond globus_llm (already wired in Globus core).
"""
from __future__ import annotations
import json
import re

from db_helpers import db_read
from globus_llm import globus_call_chat


# Char budget per generated email body — long enough to be a real
# email, short enough to keep token spend predictable. Personalisation
# tokens (Hi {name} → "Hi Sumit") are inside this budget.
COPY_BODY_MAX_CHARS = 1500
COPY_SUBJECT_MAX_CHARS = 120


# ─────────────────────────────────────────────────────────────────────
# System prompt — the persona Narada inhabits while writing copy.
# Conservative on safety: never claim relationship, never fake
# credentials, always have a clear ask, always offer easy decline.
# ─────────────────────────────────────────────────────────────────────

_COPY_SYSTEM = """\
You are Narada, the outreach copywriter for a B2B marketer. Your job
is to draft cold outreach emails that get replies WITHOUT being sleazy.

Rules — non-negotiable:
1. Never claim a relationship that doesn't exist ("I noticed you've
   been following my work…"). Never fake mutual connections.
2. Never invent stats about the prospect's company. If you don't have
   a verifiable signal, lead with the marketer's value, not fake
   research.
3. Always have ONE clear ask — usually a 15-min call or a yes/no
   question. Never multi-step CTAs.
4. Always offer an easy out ("If this isn't a fit, no worries — just
   reply 'pass' and I'll never email again.").
5. Tone: founder-to-founder, peer-to-peer, never marketing-speak.
6. Length: 80-150 words for the body. Subject line ≤ 60 chars.
7. No emojis, no exclamation marks (looks salesy).
8. Personalisation MUST be from real prospect/company signals provided
   to you. If signals are weak, fall back to ICP-level personalisation
   (industry, role) — never invent.
9. Always include an unsubscribe-equivalent line ("If you'd rather
   not hear from me, just say so.") — CAN-SPAM compliance.

Output format — EXACTLY this JSON, no markdown fence, no preamble:
{
  "variants": [
    {"subject": "...", "body": "...", "angle": "<3-word angle name>", "score": <1-10>},
    {"subject": "...", "body": "...", "angle": "...", "score": ...},
    {"subject": "...", "body": "...", "angle": "...", "score": ...}
  ]
}
Score is YOUR estimate of which variant a thoughtful marketer would
pick — 10 = most likely to get a reply for this specific prospect.
"""


# ─────────────────────────────────────────────────────────────────────
# Winning-angle memory — pulled from prior campaigns for this member
# ─────────────────────────────────────────────────────────────────────

def _winning_angle_context(member_email: str, icp_tag: str = "") -> str:
    """Fetch the member's top-performing angles for this ICP. Returns
    a text block to inject into the copy prompt, or "" if no history."""
    if not member_email:
        return ""
    if icp_tag:
        rows = db_read(
            "SELECT angle_summary, example_copy, reply_rate, sample_size "
            "FROM globus_narada_angle_memory "
            "WHERE member_email=%s AND icp_tag=%s "
            "  AND sample_size >= 5 "
            "ORDER BY reply_rate DESC LIMIT 3",
            (member_email, icp_tag))
    else:
        rows = db_read(
            "SELECT angle_summary, example_copy, reply_rate, sample_size "
            "FROM globus_narada_angle_memory "
            "WHERE member_email=%s AND sample_size >= 5 "
            "ORDER BY reply_rate DESC LIMIT 3",
            (member_email,))
    if not rows:
        return ""
    lines = ["\nWhat has worked for this marketer before:"]
    for r in rows:
        rate = float(r.get("reply_rate") or 0)
        n = int(r.get("sample_size") or 0)
        summary = (r.get("angle_summary") or "").strip()
        sample = (r.get("example_copy") or "").strip()[:300]
        lines.append(
            f"  • {summary} — {rate:.1%} reply rate over {n} sends. "
            f"Example opener: \"{sample}\"")
    lines.append("Lean on these patterns when you can; don't copy verbatim.")
    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────
# Main entry point — called from narada_core (or the LLM tool directly)
# ─────────────────────────────────────────────────────────────────────

def draft_copy_for_prospect(
        *, member_email: str,
        product: str,                 # "VideoraIQ", what we're selling
        prospect: dict,               # row from globus_narada_prospects
        campaign_icp_description: str = "",
        icp_tag: str = "",
        variants: int = 3) -> list[dict]:
    """Generate `variants` personalised email drafts for one prospect.
    Returns a list of dicts: [{subject, body, angle, score}, ...].

    On LLM failure, returns a single fallback variant with a
    generic-but-honest template, so callers don't have to retry/error-
    handle in the campaign loop."""
    enrichment = prospect.get("enrichment") or {}
    if isinstance(enrichment, str):
        try:
            enrichment = json.loads(enrichment)
        except Exception:
            enrichment = {}

    # Build the prospect context for the LLM
    proj_lines = [
        f"Marketer is selling: {product or '(not specified)'}",
        f"ICP described as: {campaign_icp_description or '(not specified)'}",
        f"Variants to produce: {variants}",
        "",
        "Prospect:",
        f"  Name: {prospect.get('first_name','')} {prospect.get('last_name','')}".strip(),
        f"  Title: {prospect.get('title','(unknown)')}",
        f"  Company: {prospect.get('company','(unknown)')}",
        f"  Domain: {prospect.get('company_domain','')}",
        f"  LinkedIn: {prospect.get('linkedin_url','(none)')}",
    ]
    if enrichment:
        proj_lines.append("")
        proj_lines.append("Enrichment signals (use what's actually here, ignore the rest):")
        for k, v in enrichment.items():
            if v:
                proj_lines.append(f"  {k}: {str(v)[:300]}")

    angle_ctx = _winning_angle_context(member_email, icp_tag)
    if angle_ctx:
        proj_lines.append(angle_ctx)

    user_prompt = "\n".join(proj_lines)

    try:
        resp = globus_call_chat(
            system=_COPY_SYSTEM,
            messages=[{"role": "user", "content": user_prompt}],
            max_tokens=2000)
        # OpenAI-shape: {"choices":[{"message":{"content":"..."}}]}
        text = ((resp.get("choices") or [{}])[0].get("message") or {}
                ).get("content", "").strip()
    except Exception as e:
        print(f"[narada-copy] LLM call failed: {type(e).__name__}: {e}",
              flush=True)
        return [_fallback_variant(product, prospect)]

    parsed = _safe_extract_json(text)
    if not parsed or not isinstance(parsed.get("variants"), list):
        print(f"[narada-copy] LLM returned non-parseable output, "
              f"falling back. raw[:200]={text[:200]!r}", flush=True)
        return [_fallback_variant(product, prospect)]

    # Clamp + sanitise each variant
    out = []
    for v in parsed["variants"][:variants]:
        if not isinstance(v, dict):
            continue
        out.append({
            "subject": (v.get("subject") or "").strip()[:COPY_SUBJECT_MAX_CHARS],
            "body":    (v.get("body") or "").strip()[:COPY_BODY_MAX_CHARS],
            "angle":   (v.get("angle") or "")[:80],
            "score":   _coerce_score(v.get("score")),
        })
    return out or [_fallback_variant(product, prospect)]


# ─────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────

_JSON_BLOCK_RE = re.compile(r"\{[\s\S]*\}")


def _safe_extract_json(text: str) -> dict | None:
    """LLM sometimes wraps the JSON in markdown fences or adds preamble.
    Find the first {...} block + try to parse. Returns None on fail."""
    if not text:
        return None
    text = text.strip()
    # Strip a leading ```json / ``` fence if present.
    if text.startswith("```"):
        # Drop opening fence line + closing fence
        lines = text.splitlines()
        if lines:
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    try:
        return json.loads(text)
    except Exception:
        pass
    # Last resort — grep for the outermost {…} block
    m = _JSON_BLOCK_RE.search(text)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except Exception:
        return None


def _coerce_score(v) -> int:
    try:
        n = int(round(float(v)))
        return max(1, min(10, n))
    except (TypeError, ValueError):
        return 5


def _fallback_variant(product: str, prospect: dict) -> dict:
    """Generic template used only when the LLM fails. Honest about
    the situation: not personalised, brief, with a clear out."""
    first = (prospect.get("first_name") or "there").strip()
    company = (prospect.get("company") or "your company").strip()
    body = (
        f"Hi {first},\n\n"
        f"I'll keep this brief. We make {product or 'a tool'} that "
        f"a few teams in your space have started using. Worth a 15-min "
        f"chat to see if it could fit {company}?\n\n"
        f"If not a fit, no worries — just reply 'pass' and I won't "
        f"email again.\n\n"
        f"Thanks,\nSumit"
    )
    return {
        "subject": f"Quick question for {company}"[:COPY_SUBJECT_MAX_CHARS],
        "body": body[:COPY_BODY_MAX_CHARS],
        "angle": "fallback",
        "score": 3,
    }
