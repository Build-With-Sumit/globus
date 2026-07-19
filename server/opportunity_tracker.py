"""Opportunity tracker — what did you send out, and who answered?

Any pursuit you send into the world and then lose track of: job applications,
partnership pitches, grant or CFP submissions, sponsorship asks. You send
dozens, replies trickle back over weeks from addresses that look nothing like
where you sent them, and the honest answer to "where does this stand?" quietly
becomes "no idea".

This does three things:

  1. keeps a record of each outbound opportunity and its stage,
  2. reads YOUR mailbox, matches replies back to the right opportunity, and
     classifies what kind of reply it is,
  3. reports the funnel, and flags the ones that have gone quiet.

READ-ONLY on your mail, and it never sends anything. It reads, matches, and
records. Chasing a stale opportunity is a decision, so it hands you a list
rather than emailing on your behalf — if you want outbound, this repo already
has Narada, which does it properly with suppression and per-send caps.

WHY THE CLASSIFIER LOOKS THE WAY IT DOES
----------------------------------------
Two distinctions do almost all the work, and both are easy to get wrong:

* An automated screener or assessment is a RESPONSE, but it is NOT a human
  conversation. Checked FIRST, because the phrasing overlaps heavily with a
  real invitation ("complete a short assessment", "schedule your video
  interview") and folding them together inflates the one number you actually
  care about.
* Marketing mail from a company you approached is not a reply from that
  company. Job boards, newsletters and "we're hiring!" blasts all match the
  sender domain perfectly, so they are excluded explicitly.

Matching is deliberately conservative: a wrong match silently rewrites the
history of an unrelated opportunity, which is worse than missing one — a miss
shows up as "no response yet", which is at least honest.
"""
from __future__ import annotations
import json
import os
import re
from datetime import datetime, timedelta, timezone

from db_helpers import db_read, db_write, cfg
from globus_llm import globus_call_chat

AGENT = "opportunity-tracker"
BEACON_KEY = "opportunity_tracker_last_run"

# Ordered. A stage may only ever move FORWARD: a templated rejection arriving
# after a scheduled interview must not undo the interview.
STAGES = ("queued", "submitted", "replied", "screener", "interview",
          "offer", "rejected", "closed")
STAGE_RANK = {s: i for i, s in enumerate(STAGES)}

# Kinds a message can be, in precedence order — when one message could be read
# two ways, the more consequential reading wins.
KIND_PRECEDENCE = ("interview", "offer", "rejected", "screener", "replied", "ack")

KIND_TO_STAGE = {"interview": "interview", "offer": "offer",
                 "rejected": "rejected", "screener": "screener",
                 "replied": "replied", "ack": "replied"}

_REJECT_RE = re.compile(
    r"\b(?:unfortunately|regret to inform|not (?:be )?(?:moving|proceeding) forward|"
    r"decided (?:not )?to (?:move|proceed) (?:forward )?with other|"
    r"we (?:have )?(?:decided|chosen) to (?:move|proceed) with (?:another|other)|"
    r"no longer under consideration|not (?:a|the) (?:right|best) fit|"
    r"position has been filled|filled the (?:role|position)|"
    r"will not be (?:moving|progressing))\b", re.I)

# Automated screeners / assessments. Checked BEFORE the interview pattern —
# these are responses, not conversations, and the wording deliberately mimics
# a real invitation.
_SCREENER_RE = re.compile(
    r"\b(?:ai[- ]?(?:powered )?screen(?:er|ing)|automated screen(?:ing)?|"
    r"one[- ]way (?:video|interview)|async(?:hronous)? (?:video|interview)|"
    r"video (?:assessment|questionnaire)|"
    r"complete (?:a |your |the )?(?:short |brief )?(?:assessment|screener|questionnaire|challenge)|"
    r"(?:coding|technical|skills) (?:assessment|challenge|test)|"
    r"take[- ]home (?:test|assignment|exercise))\b", re.I)

# A genuine human conversation — requires scheduling with a person.
_INTERVIEW_RE = re.compile(
    r"\b(?:schedule (?:a |some )?(?:call|time|interview|meeting|chat)|"
    r"availability for (?:a )?(?:call|interview|chat|meeting)|"
    r"phone (?:interview|screen)|video interview|interview with|"
    r"speak (?:with|to) (?:our|the) (?:team|hiring|recruiter)|"
    r"set up (?:a )?(?:call|interview|time)|book (?:a )?time|"
    r"calendly\.com|when are you (?:free|available))\b", re.I)

_OFFER_RE = re.compile(
    r"\b(?:pleased to offer|offer of employment|we(?:'| a)re (?:excited|delighted) to offer|"
    r"formal offer|offer letter)\b", re.I)

# Bulk mail that matches the sender domain perfectly but is not a reply.
_MARKETING_RE = re.compile(
    r"\b(?:unsubscribe|newsletter|webinar|blog post|new(?:sletter)? digest|"
    r"job alert|jobs? you may|recommended (?:jobs|for you)|"
    r"we(?:'| a)re hiring|view in browser|manage (?:your )?preferences|"
    r"promotional|marketing preferences)\b", re.I)

_NOREPLY_OK = re.compile(r"no[-_.]?reply|donotreply", re.I)

# Words too generic to identify an organisation on their own.
_STOPWORDS = {"the", "inc", "llc", "ltd", "limited", "corp", "corporation",
              "company", "co", "group", "labs", "lab", "technologies", "tech",
              "software", "solutions", "systems", "services", "global",
              "international", "holdings", "partners", "ventures", "studio",
              "studios", "digital", "media", "consulting", "agency", "team"}


def envflag(name, default=False):
    """`bool("0")` is True — never use bare bool() on an env var."""
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


def stale_days():
    return _int("OPP_STALE_DAYS", 14)


def use_llm_fallback():
    """Whether to ask a model about messages the patterns couldn't classify.
    Off by default: the patterns handle the overwhelming majority, and a
    tracker that quietly costs money per inbound message is a bad default."""
    return envflag("OPP_LLM_FALLBACK", False)


def classify_model():
    return _cfg("OPP_CLASSIFY_MODEL", "haiku")


# ─────────────────────────────────────────────────────────────────────
# Store
# ─────────────────────────────────────────────────────────────────────

def add_opportunity(member_email, slug, org, title="", url="", domain="",
                    stage="submitted", source="", notes=""):
    """Record one outbound opportunity. Idempotent on (member, slug)."""
    return db_write(
        "INSERT INTO opportunities (member_email, slug, org, title, url, "
        " domain, stage, stage_updated_at, submitted_at, source, notes) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,NOW(),%s,%s,%s) "
        "ON DUPLICATE KEY UPDATE org=VALUES(org), title=VALUES(title), "
        " url=VALUES(url), domain=VALUES(domain), source=VALUES(source), "
        " notes=VALUES(notes)",
        (member_email, slug, org, title, url, (domain or "").lower(), stage,
         (datetime.utcnow() if stage != "queued" else None), source, notes))


def open_opportunities(member_email):
    """Everything still in play — terminal stages are excluded so a closed
    opportunity can't be resurrected by a stray newsletter."""
    return db_read(
        "SELECT * FROM opportunities WHERE member_email=%s "
        "AND stage NOT IN ('rejected','closed') ORDER BY id DESC",
        (member_email,)) or []


def all_opportunities(member_email):
    return db_read("SELECT * FROM opportunities WHERE member_email=%s "
                   "ORDER BY id DESC", (member_email,)) or []


def record_event(opp_id, stage, actor="tracker", detail=""):
    return db_write(
        "INSERT INTO opportunity_events (opportunity_id, stage, at, actor, "
        " detail) VALUES (%s,%s,NOW(),%s,%s)",
        (opp_id, stage, actor, (detail or "")[:500]))


def advance(opp, stage, detail="", actor="tracker"):
    """Move an opportunity forward. Returns True if it actually moved.

    Never moves BACKWARD: replies arrive out of order, and a templated
    "thanks for applying" landing after an interview invitation must not
    rewind the funnel."""
    cur = STAGE_RANK.get(opp.get("stage") or "queued", 0)
    new = STAGE_RANK.get(stage, 0)
    if new <= cur:
        return False
    ok = db_write("UPDATE opportunities SET stage=%s, stage_updated_at=NOW() "
                  "WHERE id=%s", (stage, opp["id"]))
    if ok:
        record_event(opp["id"], stage, actor, detail)
        opp["stage"] = stage
    return bool(ok)


# ─────────────────────────────────────────────────────────────────────
# Classification
# ─────────────────────────────────────────────────────────────────────

def classify(subject, snippet, from_email=""):
    """→ one of KIND_PRECEDENCE, or None when this isn't a response at all.

    Order matters: rejection and offer are unambiguous, screeners are checked
    BEFORE interviews because their wording overlaps, and marketing is
    excluded outright rather than being allowed to look like a reply."""
    blob = f"{subject or ''}\n{snippet or ''}"
    if _MARKETING_RE.search(blob):
        return None
    if _OFFER_RE.search(blob):
        return "offer"
    if _REJECT_RE.search(blob):
        return "rejected"
    if _SCREENER_RE.search(blob):
        return "screener"
    if _INTERVIEW_RE.search(blob):
        return "interview"
    if from_email and _NOREPLY_OK.search(from_email):
        return "ack"
    return "replied"


_LLM_SYSTEM = """You classify one reply to an outbound application or pitch.

Answer with exactly one word from this list and nothing else:
  offer      — a formal offer is being made
  rejected   — they are declining or not proceeding
  interview  — they want to schedule time with a HUMAN
  screener   — an automated assessment, test, or one-way/async video step
  replied    — a substantive human reply that is none of the above
  ack        — an automated acknowledgement of receipt
  none       — not a reply to us at all (marketing, newsletter, job alert)

An automated screener is NOT an interview, even when it says "interview".
"""


def classify_llm(subject, snippet, from_email=""):
    """Optional second opinion for messages the patterns couldn't place.
    Returns None on any failure — an unclassifiable message stays
    unclassified, which is honest, rather than being guessed into the funnel."""
    try:
        resp = globus_call_chat(
            _LLM_SYSTEM,
            [{"role": "user", "content": f"From: {from_email}\n"
                                          f"Subject: {subject}\n\n{snippet}"}],
            max_tokens=10, model=classify_model())
        word = ((resp.get("choices") or [{}])[0].get("message", {})
                .get("content", "") or "").strip().lower().split()
    except Exception as e:
        print(f"[{AGENT}] classify fallback failed ({type(e).__name__}: {e})",
              flush=True)
        return None
    if not word:
        return None
    w = re.sub(r"[^a-z]", "", word[0])
    if w == "none":
        return None
    return w if w in KIND_PRECEDENCE else None


# ─────────────────────────────────────────────────────────────────────
# Matching
# ─────────────────────────────────────────────────────────────────────

def _domain_of(addr):
    m = re.search(r"@([A-Za-z0-9.\-]+)", addr or "")
    return (m.group(1).lower().strip(". ") if m else "")


def _registrable(domain):
    """Cheap eTLD-ish reduction: last two labels, or three for a known
    two-part public suffix. Enough to treat mail.acme.com as acme.com."""
    parts = [p for p in (domain or "").lower().split(".") if p]
    if len(parts) < 2:
        return domain or ""
    two_part = {"co.uk", "com.au", "co.in", "co.jp", "com.br", "co.nz"}
    if len(parts) >= 3 and ".".join(parts[-2:]) in two_part:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def org_tokens(org):
    """Distinctive words from an organisation name.

    Generic words are dropped: matching on "Global Solutions" would attach
    half the inbox to one opportunity. A single very short token is dropped
    too — two-letter names are indistinguishable from noise."""
    words = re.findall(r"[A-Za-z0-9]+", (org or "").lower())
    return [w for w in words if w not in _STOPWORDS and len(w) >= 4]


def match_message(msg, opps):
    """Find the opportunity a message belongs to, or None.

    Domain match is authoritative. Name matching is the fallback and is
    deliberately strict, because a wrong match silently rewrites an unrelated
    opportunity's history — a miss just reads as "no response yet"."""
    frm = msg.get("from_email") or ""
    dom = _registrable(_domain_of(frm))
    if dom:
        for o in opps:
            od = _registrable((o.get("domain") or "").lower())
            if od and od == dom:
                return o
    blob = f"{msg.get('subject','')} {msg.get('snippet','')}".lower()
    if not blob.strip():
        return None
    best, best_score = None, 0
    for o in opps:
        toks = org_tokens(o.get("org"))
        if not toks:
            continue
        hits = sum(1 for t in toks if re.search(rf"\b{re.escape(t)}\b", blob))
        # A single distinctive token is enough only if it is the org's ONLY
        # token; otherwise require at least two so "Acme" doesn't claim mail
        # meant for "Acme Health".
        need = 1 if len(toks) == 1 else 2
        if hits >= need and hits > best_score:
            best, best_score = o, hits
    return best


def scan(member_email, messages, dry_run=False):
    """Match + classify a batch of inbound messages and advance stages.

    `messages` are dicts: {id, from_email, subject, snippet, received_at}.
    Idempotent — advancing is monotonic, so re-running over the same mail is
    a no-op rather than a duplicate."""
    opps = open_opportunities(member_email)
    if not opps:
        return {"messages": len(messages), "matched": 0, "advanced": 0,
                "unmatched": len(messages)}
    matched = advanced = 0
    proposals = {}
    for m in messages:
        opp = match_message(m, opps)
        if not opp:
            continue
        kind = classify(m.get("subject"), m.get("snippet"),
                        m.get("from_email"))
        if kind is None and use_llm_fallback():
            kind = classify_llm(m.get("subject"), m.get("snippet"),
                                m.get("from_email"))
        if kind is None:
            continue
        matched += 1
        # One opportunity can receive several messages in a run; keep the most
        # consequential reading rather than whichever arrived last.
        prev = proposals.get(opp["id"])
        rank = KIND_PRECEDENCE.index(kind)
        if prev is None or rank < prev[1]:
            proposals[opp["id"]] = (kind, rank, opp, m)

    for opp_id, (kind, _r, opp, m) in proposals.items():
        stage = KIND_TO_STAGE.get(kind)
        if not stage:
            continue
        if dry_run:
            print(f"[{AGENT}] would advance {opp.get('org')} -> {stage} "
                  f"({kind}: {(m.get('subject') or '')[:60]})", flush=True)
            continue
        if advance(opp, stage, detail=f"{kind}: {(m.get('subject') or '')[:200]}"):
            advanced += 1
    return {"messages": len(messages), "matched": matched,
            "advanced": advanced, "unmatched": len(messages) - matched}


# ─────────────────────────────────────────────────────────────────────
# Reporting
# ─────────────────────────────────────────────────────────────────────

def funnel(member_email):
    rows = all_opportunities(member_email)
    counts = {s: 0 for s in STAGES}
    for r in rows:
        counts[r.get("stage") or "queued"] = counts.get(r.get("stage") or "queued", 0) + 1
    sent = sum(counts[s] for s in STAGES if STAGE_RANK[s] >= STAGE_RANK["submitted"])
    responded = sum(counts[s] for s in ("replied", "screener", "interview",
                                        "offer", "rejected"))
    interviews = counts["interview"] + counts["offer"]
    return {"total": len(rows), "counts": counts, "sent": sent,
            "responded": responded, "interviews": interviews,
            "response_rate": (responded / sent if sent else 0.0),
            "interview_rate": (interviews / sent if sent else 0.0)}


def stale(member_email, days=None):
    """Submitted, never answered, and quiet for longer than `days`.

    This is a list for a human to act on. Chasing is a judgment call, so the
    tracker surfaces the candidates and stops there."""
    days = days or stale_days()
    cutoff = datetime.utcnow() - timedelta(days=days)
    return [o for o in all_opportunities(member_email)
            if (o.get("stage") == "submitted"
                and o.get("stage_updated_at")
                and o["stage_updated_at"] <= cutoff)]


def report_text(member_email, days=None):
    f = funnel(member_email)
    c = f["counts"]
    st = stale(member_email, days)
    lines = [
        f"📊 Opportunities — {f['total']} tracked, {f['sent']} sent",
        f"   replied {c['replied']} · screener {c['screener']} · "
        f"interview {c['interview']} · offer {c['offer']} · "
        f"rejected {c['rejected']}",
        f"   response rate {f['response_rate']*100:.0f}% · "
        f"interview rate {f['interview_rate']*100:.0f}%",
    ]
    if st:
        lines.append(f"\n🕓 Quiet >{days or stale_days()}d ({len(st)}) — "
                     f"worth a nudge:")
        for o in st[:15]:
            lines.append(f"   • {o.get('org','?')}"
                         + (f" — {o['title']}" if o.get("title") else ""))
        if len(st) > 15:
            lines.append(f"   … and {len(st) - 15} more")
    return "\n".join(lines)


def stamp_beacon(status, extra=""):
    """Stamped on every completion so "the tracker stopped" is queryable
    rather than looking like "nobody replied this week"."""
    try:
        db_write("INSERT INTO config (name, value) VALUES (%s, %s) "
                 "ON DUPLICATE KEY UPDATE value=VALUES(value)",
                 (BEACON_KEY, json.dumps({
                     "at": datetime.now(timezone.utc).replace(
                         microsecond=0).isoformat(),
                     "status": status, "extra": str(extra)[:280]})))
    except Exception:
        pass
