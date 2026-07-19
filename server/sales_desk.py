"""Sales desk — a daily ranked call list, a priorities brief, and a hygiene report.

WHAT THIS ANSWERS
-----------------
Three questions a sales team asks every morning, delivered into chat rather than
a dashboard nobody opens:

  1. "Who do I call today, in what order, and what do I say?"  → ranked call list
  2. "What's the one thing we must fix today?"                 → priorities brief
  3. "Where is the pipeline rotting?"                          → hygiene report

THE SHAPE
---------
    gather → bound → dedup → rank (batched LLM, global indices)
           → band-merge → fail open to a deterministic sort
           → chunk → deliver → stamp a beacon

Deterministic code does only the things that are NOT judgment: eligibility,
dedup, bounding, and arithmetic. The model does the judgment — which lead
matters most right now, and what the next step is. That split is the whole
design: a status-lookup sort cannot tell you that a lead who replied "send me
pricing" three days ago outranks a fresher one who never answered.

READ-ONLY. Nothing here writes to your CRM, and nothing here sends email. It
reads your pipeline and tells you about it.

FAIL OPEN
---------
Every model layer degrades to a deterministic path, because a sales team that
doesn't get its list has no fallback — they just don't call anyone. If ranking
fails, the list still posts in deterministic order. If the brief fails, the list
posts without it. The one place this fails CLOSED is an empty/stale lead source:
shipping an empty call list would read as "quiet day" when it means "the feed
broke", so that raises loudly instead.

NO STATUS STRINGS IN CODE
-------------------------
Pipeline stage names are DATA, not control flow. They live in a config map of
{status: {callable, weight, terminal}} so adding a stage is an edit, not a
release. The same applies to the roster, the destination, and the timezone —
there is no inline timezone offset anywhere in this file.
"""
from __future__ import annotations
import json
import os
import re
from datetime import datetime, timedelta, timezone

from db_helpers import db_read, db_write, cfg
from globus_llm import globus_call_chat

AGENT = "sales-desk"
BEACON_KEY = "sales_desk_last_run"

# Absolute priority bands. These are the model's OUTPUT CONTRACT, so they stay
# in code — but they are absolute, never "rank these relative to each other",
# which is what lets independently-ranked batches merge into one coherent list.
BANDS = ("call_now", "today", "this_week", "nurture")
BAND_LABEL = {"call_now": "🔴 CALL NOW", "today": "🟠 TODAY",
              "this_week": "🟡 THIS WEEK", "nurture": "⚪ NURTURE"}


# ─────────────────────────────────────────────────────────────────────
# Config
# ─────────────────────────────────────────────────────────────────────

def envflag(name, default=False):
    """`bool("0")` is True, so never use bare bool() on an env var."""
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() not in ("0", "false", "no", "off")


def _cfg(key, default=""):
    return (cfg(key, "") or os.environ.get(key, "") or default)


def _int(key, default):
    try:
        return int(str(_cfg(key, str(default))).strip())
    except (TypeError, ValueError):
        return default


# Default taxonomy for the built-in pipeline source. Every value is data:
#   callable — may appear on a call list at all
#   weight   — deterministic fallback ordering (higher first)
#   terminal — the deal is over; never call
DEFAULT_STATUS_RULES = {
    "replied":      {"callable": True,  "weight": 100, "terminal": False},
    "sent":         {"callable": True,  "weight": 60,  "terminal": False},
    "approved":     {"callable": True,  "weight": 50,  "terminal": False},
    "drafted":      {"callable": True,  "weight": 40,  "terminal": False},
    "enriched":     {"callable": True,  "weight": 30,  "terminal": False},
    "verified":     {"callable": True,  "weight": 25,  "terminal": False},
    "new":          {"callable": True,  "weight": 20,  "terminal": False},
    "unsubscribed": {"callable": False, "weight": 0,   "terminal": True},
    "bounced":      {"callable": False, "weight": 0,   "terminal": True},
    "suppressed":   {"callable": False, "weight": 0,   "terminal": True},
    "failed":       {"callable": False, "weight": 0,   "terminal": True},
}


def status_rules():
    """{status: {callable, weight, terminal}} — operator-configurable via
    SALES_DESK_STATUS_RULES (JSON). An unknown status is treated as callable
    with weight 10: a stage someone added in the CRM this morning should show
    up on the list, not vanish from it."""
    raw = _cfg("SALES_DESK_STATUS_RULES", "")
    if raw:
        try:
            rules = json.loads(raw)
            if isinstance(rules, dict) and rules:
                return {str(k).lower(): v for k, v in rules.items()}
        except Exception as e:
            print(f"[{AGENT}] WARN: SALES_DESK_STATUS_RULES is not valid JSON "
                  f"({type(e).__name__}) — using defaults", flush=True)
    return DEFAULT_STATUS_RULES


def rule_for(status):
    return status_rules().get(str(status or "").lower(),
                              {"callable": True, "weight": 10, "terminal": False})


def business_context():
    """Free-text: what you sell, to whom, and what "urgent" means here. Ships
    EMPTY — a generic default would make the model confidently wrong about your
    pipeline."""
    return _cfg("SALES_DESK_CONTEXT", "").strip()


def list_size():
    return _int("SALES_DESK_LIST_SIZE", 50)


def rank_max():
    """Candidate pool ceiling. Bounds both tokens and the number of model calls
    — there is no point spending a premium model on cold leads that cannot
    reach the top of the list."""
    return _int("SALES_DESK_RANK_MAX", 220)


def rank_batch():
    """Leads per ranking call.

    Batch on OUTPUT rows, not input size. When you need one output line per
    input line, the reliability ceiling is the number of lines the model will
    emit — it starts dropping rows long before the input runs out. Ranking
    200+ leads in a single call silently returns a truncated list, which then
    looks like a complete one."""
    return _int("SALES_DESK_RANK_BATCH", 50)


def stale_days():
    return _int("SALES_DESK_STALE_DAYS", 21)


def rank_model():
    return _cfg("SALES_DESK_RANK_MODEL", "sonnet")


def brief_model():
    return _cfg("SALES_DESK_BRIEF_MODEL", "sonnet")


def tz_offset_minutes():
    """Local-time offset for "today". Config, never an inline timedelta — a
    hardcoded offset is invisible until it silently reports the wrong day."""
    return _int("SALES_DESK_TZ_OFFSET_MIN", 0)


def now_local():
    return datetime.now(timezone.utc) + timedelta(minutes=tz_offset_minutes())


BRIEF_MAX_CHARS = 3500
CHUNK_MAX = 3500
MIN_PLAUSIBLE_BRIEF = 40


# ─────────────────────────────────────────────────────────────────────
# Lead sources
# ─────────────────────────────────────────────────────────────────────
# A source returns a list of dicts in one common shape:
#
#   {id, name, email, company, title, status, owner, days_since,
#    note, source, link}
#
# The built-in source reads this install's own outbound pipeline. To read a
# CRM instead, register a callable here.
#
# NOTE ON CRMs: the bundled CRM plugins (narada_plugins/) implement a WRITE
# protocol — upsert_contact / create_deal / log_activity. They deliberately do
# not read, so there is no honest way to build a call list from them yet. Adding
# a read method per vendor is real work (pagination, field hydration, rate
# limits) and is left as an explicit extension point rather than faked.

_SOURCES = {}


def register_source(name, fn):
    """fn(member_email, limit) -> [lead dict]."""
    _SOURCES[name] = fn


def _days_since(dt):
    if not dt:
        return 9999
    if isinstance(dt, str):
        try:
            dt = datetime.fromisoformat(dt)
        except Exception:
            return 9999
    if dt.tzinfo:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return max(0, (datetime.utcnow() - dt).days)


def pipeline_source(member_email, limit=1000):
    """Built-in source: this install's outbound pipeline, joined to its most
    recent engagement so the model can see what actually happened — a reply,
    a bounce, or silence — rather than only a stage name."""
    rows = db_read(
        "SELECT p.id, p.first_name, p.last_name, p.email, p.company, p.title, "
        "       p.status, p.member_email, p.updated_at, p.campaign_id, "
        "       s.status AS send_status, s.reply_classification, "
        "       s.reply_body, s.sent_at, s.reply_received_at "
        "FROM globus_narada_prospects p "
        "LEFT JOIN globus_narada_sends s ON s.id = ("
        "   SELECT id FROM globus_narada_sends "
        "   WHERE prospect_id = p.id ORDER BY id DESC LIMIT 1) "
        "WHERE p.member_email = %s "
        "ORDER BY p.updated_at DESC LIMIT %s",
        (member_email, int(limit))) or []
    out = []
    for r in rows:
        name = " ".join(x for x in [r.get("first_name"), r.get("last_name")] if x)
        last_at = (r.get("reply_received_at") or r.get("sent_at")
                   or r.get("updated_at"))
        note = ""
        if r.get("reply_classification"):
            note = f"replied[{r['reply_classification']}]: " \
                   f"{(r.get('reply_body') or '').strip()[:200]}"
        elif r.get("send_status"):
            note = f"last send {r['send_status']}"
        out.append({
            "id": f"p{r['id']}",
            "name": name or (r.get("email") or "?"),
            "email": (r.get("email") or "").lower(),
            "company": r.get("company") or "",
            "title": r.get("title") or "",
            "status": r.get("status") or "new",
            "owner": r.get("member_email") or "",
            "days_since": _days_since(last_at),
            "note": note,
            "source": "pipeline",
            "link": "",
        })
    return out


register_source("pipeline", pipeline_source)


def gather(member_email, sources=None):
    """Pull from every configured source, drop terminal + uncallable leads,
    dedup by email, and bound the pool.

    Ordering here is irrelevant — the model re-ranks. This stage exists only to
    decide WHO IS ELIGIBLE and to keep the pool small enough to rank reliably."""
    names = sources or [s.strip() for s in
                        _cfg("SALES_DESK_SOURCES", "pipeline").split(",")
                        if s.strip()]
    pool, seen = [], set()
    for nm in names:
        fn = _SOURCES.get(nm)
        if not fn:
            print(f"[{AGENT}] WARN: unknown lead source {nm!r} — skipped",
                  flush=True)
            continue
        try:
            leads = fn(member_email, rank_max() * 3) or []
        except Exception as e:
            # One broken source must not deny the team its whole list.
            print(f"[{AGENT}] source {nm} failed ({type(e).__name__}: {e}) — "
                  f"continuing without it", flush=True)
            continue
        for ld in leads:
            rule = rule_for(ld.get("status"))
            if rule.get("terminal") or not rule.get("callable", True):
                continue
            key = (ld.get("email") or ld.get("id") or "").lower()
            if not key or key in seen:
                continue
            seen.add(key)
            pool.append(ld)
    pool.sort(key=lambda l: (-rule_for(l["status"]).get("weight", 0),
                             l.get("days_since", 9999)))
    return pool[:rank_max()]


# ─────────────────────────────────────────────────────────────────────
# Deterministic ranking — the fallback the whole design leans on
# ─────────────────────────────────────────────────────────────────────

def deterministic_rank(pool):
    """Weight-then-recency ordering with a derived band. Not as good as the
    model, but always available — and it is what makes every AI layer safe to
    fail."""
    def band_of(ld):
        w = rule_for(ld["status"]).get("weight", 0)
        d = ld.get("days_since", 9999)
        if w >= 100:
            return "call_now"
        if w >= 50:
            return "today" if d <= stale_days() else "this_week"
        return "this_week" if d <= stale_days() else "nurture"
    out = []
    for ld in pool:
        item = dict(ld)
        item["band"] = band_of(ld)
        item["action"] = (f"Follow up — {ld['status']}, "
                          f"{ld.get('days_since', '?')}d since last touch")
        out.append(item)
    out.sort(key=lambda l: (BANDS.index(l["band"]),
                            -rule_for(l["status"]).get("weight", 0),
                            l.get("days_since", 9999)))
    return out


# ─────────────────────────────────────────────────────────────────────
# LLM ranking
# ─────────────────────────────────────────────────────────────────────

_RANK_SYSTEM = """You rank sales leads for a rep about to start calling.

{context}For EACH numbered lead, output exactly one line:

<index>|<band>|<action>

band is exactly one of: call_now | today | this_week | nurture
  call_now  — someone is waiting on us right now, or a live opportunity decays today
  today     — should be contacted before end of day
  this_week — worth a touch this week
  nurture   — no near-term reason to call

action is the next step in 10 words or fewer, written for the rep.

Rules:
- The bands are ABSOLUTE, not relative to this batch. Judge each lead on its
  own merits so that separate batches remain comparable.
- Ground the action in THAT lead's own status and note. Never invent a fact,
  a name, a price, or a commitment that is not shown to you.
- Output one line per lead and nothing else. No preamble, no numbering scheme
  of your own, no blank lines.
"""


def _lead_line(idx, ld):
    return (f"{idx}\t{(ld.get('name') or '')[:40]} | {ld.get('company','')[:30]}"
            f" | status={ld.get('status','')}"
            f" | {ld.get('days_since','?')}d since touch"
            f" | note={(ld.get('note') or '')[:160]}")


def _parse_rank_lines(text, lo, hi):
    """→ {index: (band, action)}. Defensive on purpose: a model that returns a
    stray index, a bullet, or an unknown band must not corrupt or drop a lead."""
    out = {}
    for raw in (text or "").splitlines():
        line = raw.strip().lstrip("-•* ").strip()
        if "|" not in line:
            continue
        parts = [p.strip() for p in line.split("|")]
        if len(parts) < 2:
            continue
        digits = re.sub(r"\D", "", parts[0])
        if not digits:
            continue
        idx = int(digits)
        # Reject an index outside THIS batch — a hallucinated index would
        # otherwise overwrite another batch's lead.
        if idx < lo or idx > hi:
            continue
        band = parts[1].strip().lower().replace(" ", "_")
        if band not in BANDS:
            band = "today"           # coerce, never drop
        action = (parts[2].strip() if len(parts) > 2 else "")[:120]
        out[idx] = (band, action)
    return out


def _rank_batch(pool, lo, hi):
    """Rank pool[lo-1:hi] using GLOBAL indices, so batches merge cleanly."""
    lines = [_lead_line(i, pool[i - 1]) for i in range(lo, hi + 1)]
    system = _RANK_SYSTEM.format(
        context=(f"Business context:\n{business_context()}\n\n"
                 if business_context() else ""))
    resp = globus_call_chat(system, [{"role": "user",
                                      "content": "\n".join(lines)}],
                            max_tokens=6000, model=rank_model())
    text = (resp.get("choices") or [{}])[0].get("message", {}).get("content", "")
    return _parse_rank_lines(text, lo, hi)


def llm_rank(pool):
    """→ ranked list, or None to fall back to the deterministic order.

    Returns None (rather than a partial list) when the model returned too few
    usable rows: a short ranking looks exactly like a complete one to whoever
    reads it, and quietly omits the leads that mattered."""
    if not pool:
        return []
    ranked_meta, n = {}, len(pool)
    size = max(1, rank_batch())
    try:
        for lo in range(1, n + 1, size):
            hi = min(n, lo + size - 1)
            ranked_meta.update(_rank_batch(pool, lo, hi))
    except Exception as e:
        print(f"[{AGENT}] ranking failed ({type(e).__name__}: {e}) — "
              f"falling back to deterministic order", flush=True)
        return None

    floor = max(10, int(n * 0.85))
    if len(ranked_meta) < min(floor, n):
        print(f"[{AGENT}] ranking parsed only {len(ranked_meta)}/{n} leads "
              f"(floor {min(floor, n)}) — discarding the whole ranking rather "
              f"than shipping a short list", flush=True)
        return None

    out = []
    for i, ld in enumerate(pool, 1):
        item = dict(ld)
        band, action = ranked_meta.get(i, (None, ""))
        if band is None:
            # Never silently drop a lead the model skipped: give it a derived
            # band and let it sort to the end of that band.
            item["band"] = deterministic_rank([ld])[0]["band"]
            item["action"] = f"Follow up — {ld.get('status','')}"
            item["_unranked"] = True
        else:
            item["band"] = band
            item["action"] = action or f"Follow up — {ld.get('status','')}"
        out.append(item)
    out.sort(key=lambda l: (BANDS.index(l["band"]), bool(l.get("_unranked"))))
    return out


# ─────────────────────────────────────────────────────────────────────
# Priorities brief
# ─────────────────────────────────────────────────────────────────────

_BRIEF_SYSTEM = """You write a short daily priorities note for a sales team.

{context}You are given today's ranked call list, already prioritised. In 120
words or fewer, tell the team what actually matters today: the pattern in the
queue, the one or two leads that must not slip, and any visible risk (for
example a lot of leads going stale, or replies not being followed up).

Be concrete and reference real entries. Do not invent numbers. Do not restate
the list — they can already see it. No preamble.
"""


def build_brief(ranked):
    """→ brief text, or "" when it could not be produced.

    Never returns an error string. A model's failure message is fluent and
    well-formed, so it would sail straight into the team's channel as if it
    were the brief — the classic error-as-data failure. A suspiciously short
    result is treated as a failure for the same reason."""
    if not ranked:
        return ""
    counts = {b: sum(1 for r in ranked if r["band"] == b) for b in BANDS}
    stale = sum(1 for r in ranked if r.get("days_since", 0) > stale_days())
    lines = ["Queue: " + ", ".join(f"{b}={counts[b]}" for b in BANDS)
             + f"; {stale} stale (>{stale_days()}d)"]
    for r in ranked[:25]:
        lines.append(f"- [{r['band']}] {r.get('name','?')} "
                     f"({r.get('company','')}) status={r.get('status','')} "
                     f"{r.get('days_since','?')}d — {r.get('action','')}")
    system = _BRIEF_SYSTEM.format(
        context=(f"Business context:\n{business_context()}\n\n"
                 if business_context() else ""))
    try:
        resp = globus_call_chat(system, [{"role": "user",
                                          "content": "\n".join(lines)}],
                                max_tokens=1200, model=brief_model())
        text = ((resp.get("choices") or [{}])[0]
                .get("message", {}).get("content", "") or "").strip()
    except Exception as e:
        print(f"[{AGENT}] brief failed ({type(e).__name__}: {e}) — "
              f"posting the list without it", flush=True)
        return ""
    if len(text) < MIN_PLAUSIBLE_BRIEF:
        print(f"[{AGENT}] brief implausibly short ({len(text)} chars) — "
              f"treating as a failure", flush=True)
        return ""
    return text[:BRIEF_MAX_CHARS]


# ─────────────────────────────────────────────────────────────────────
# Hygiene — deterministic, no model
# ─────────────────────────────────────────────────────────────────────

def hygiene(pool):
    """Counting, not judgment — so no model is involved."""
    stale = [l for l in pool if l.get("days_since", 0) > stale_days()]
    no_email = [l for l in pool if not l.get("email")]
    no_owner = [l for l in pool if not l.get("owner")]
    no_company = [l for l in pool if not l.get("company")]
    return {"total": len(pool), "stale": len(stale), "no_email": len(no_email),
            "no_owner": len(no_owner), "no_company": len(no_company),
            "stale_days": stale_days()}


def hygiene_text(h):
    return (f"🩺 Pipeline hygiene — {h['total']} callable\n"
            f"   • {h['stale']} untouched >{h['stale_days']}d\n"
            f"   • {h['no_email']} missing an email address\n"
            f"   • {h['no_owner']} with no owner\n"
            f"   • {h['no_company']} missing a company")


# ─────────────────────────────────────────────────────────────────────
# Rendering + delivery
# ─────────────────────────────────────────────────────────────────────

def build_messages(ranked, brief="", hyg=None, limit=None, max_chars=CHUNK_MAX):
    """→ [text, ...] chunks. Chunked because most chat transports hard-reject
    an oversized message: an unchunked list silently stops posting on the day
    the pipeline grows, which is exactly the day it mattered."""
    limit = limit or list_size()
    shown = ranked[:limit]
    head = (f"📞 Call list — {now_local():%a %d %b} · "
            f"{len(shown)} of {len(ranked)} callable")
    if len(ranked) > len(shown):
        head += f" (showing top {len(shown)})"
    parts = [head]
    if brief:
        parts.append("\n📋 " + brief)
    if hyg:
        parts.append("\n" + hygiene_text(hyg))
    chunks, cur = [], "\n".join(parts) + "\n"
    last_band = None
    for r in shown:
        block = ""
        if r["band"] != last_band:
            block += f"\n{BAND_LABEL.get(r['band'], r['band'])}\n"
            last_band = r["band"]
        block += (f"• {r.get('name','?')}"
                  + (f" · {r['company']}" if r.get("company") else "")
                  + f" — {r.get('action','')}\n"
                  f"   {r.get('status','')} · {r.get('days_since','?')}d"
                  + (f" · {r['email']}" if r.get("email") else "") + "\n")
        if len(cur) + len(block) > max_chars:
            chunks.append(cur)
            cur = head + " (cont.)\n"
            last_band = None
            # re-emit the band header in the new chunk
            block = (f"\n{BAND_LABEL.get(r['band'], r['band'])}\n"
                     + block.lstrip("\n"))
            last_band = r["band"]
        cur += block
    chunks.append(cur)
    return chunks


def stamp_beacon(status, extra=""):
    """Stamped on EVERY completion — success, empty, or failure — so that
    "the desk stopped running" is detectable rather than indistinguishable
    from "quiet day". A beacon write must never crash the run."""
    try:
        payload = json.dumps({
            "at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            "status": status, "extra": str(extra)[:280]})
        db_write("INSERT INTO config (name, value) VALUES (%s, %s) "
                 "ON DUPLICATE KEY UPDATE value=VALUES(value)",
                 (BEACON_KEY, payload))
    except Exception:
        pass


def run(member_email, sources=None, use_llm=True, limit=None):
    """Build the day's desk. Returns (chunks, meta).

    Fails CLOSED on an empty pool: an empty call list is indistinguishable from
    a quiet day, and shipping one hides a broken feed behind a cheerful-looking
    message."""
    pool = gather(member_email, sources)
    if not pool:
        raise RuntimeError(
            "no callable leads found — this is reported rather than posted as "
            "an empty list, because an empty list looks like a quiet day. "
            "Check that a lead source is configured (SALES_DESK_SOURCES), that "
            "it has data for this member, and that SALES_DESK_STATUS_RULES is "
            "not marking every stage terminal.")
    hyg = hygiene(pool)
    ranked = llm_rank(pool) if use_llm else None
    fell_back = ranked is None
    if fell_back:
        ranked = deterministic_rank(pool)
    brief = build_brief(ranked) if use_llm else ""
    chunks = build_messages(ranked, brief=brief, hyg=hyg, limit=limit)
    return chunks, {"pool": len(pool), "ranked": len(ranked),
                    "fell_back": fell_back, "brief": bool(brief),
                    "hygiene": hyg}
