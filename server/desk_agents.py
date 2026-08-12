"""Four agents that work a shared inbox: rescue, respond, follow up, learn.

Built on the substrate in `email_desks.py`. Each agent is independent, opt-in
per desk, and bounded so that a bad run costs a human a minute of tidying rather
than a customer.

  RESCUE     Reads the desk's Spam folder and moves genuine business mail back
             to the Inbox. The only agent here that mutates a mailbox.
  RESPOND    A correspondent wrote in and is waiting. Classifies the request and
             drafts a reply grounded ONLY in the operator's playbook.
  FOLLOW UP  The inverse: we spoke last, they went quiet, and a nudge is due.
  LEARN      Reads what the human actually DID with the above — the edits they
             made before sending, the rescues they reversed — and writes a
             lesson each agent reads back on its next run.

WHY DRAFT-FIRST IS LOAD-BEARING, NOT TIMIDITY
---------------------------------------------
The LEARN agent only works because the others draft. A human editing a draft
before sending it is the richest correction signal available: it is free,
unprompted, and it shows the operator's real voice and real policy rather than
their stated one. An agent that sent directly would be both riskier AND unable
to learn — the feedback channel and the safety property are the same mechanism.

WHAT THE OPERATOR MUST SUPPLY
-----------------------------
These agents ship with NO opinion about your business. The classifier and the
composer are driven by config you write:

  DESK_BUSINESS_CONTEXT   what this desk is and who writes to it
  DESK_CATEGORIES         the request types you want distinguished
  DESK_REPLYABLE          which of those earn an automatic draft
  DESK_PLAYBOOK           the ONLY facts a reply may assert (prices, policy,
                          turnaround, what you do and don't offer)
  DESK_SIGNOFF            e.g. "{product} Support Team"
  DESK_NUDGE              the follow-up body; deterministic, no model involved

Leave DESK_PLAYBOOK empty and the composer is told it has no facts and must ask
rather than answer. That is the correct behaviour for an unconfigured install:
an assistant with no grounding does not become more useful by inventing prices,
it becomes a liability that quotes numbers you have to honour.
"""
from __future__ import annotations
import difflib
import json
from datetime import datetime, timezone

from db_helpers import db_read, db_write
from globus_llm import globus_call_chat

from email_desks import (
    AGENT_FOLLOWUP, AGENT_LEARNING, AGENT_RESPONDER, AGENT_SPAM_RESCUE,
    DeskSession, _cfg, _int, agent_enabled, agent_model, append_lesson,
    bare_email, content_of, customer_is_last_responder, decode_body, envflag,
    gapi, is_external, load_lessons, parse_json, product_for, reply_subject,
    rescue_from_spam, stamp_beacon, throttle, upsert_thread_draft,
    we_spoke_last, withdraw_stale_drafts,
)

DEFAULT_CATEGORIES = [
    "sales_inquiry", "support_request", "billing", "vendor_pitch",
    "newsletter", "other",
]


# ─────────────────────────────────────────────────────────────────────
# Operator configuration
# ─────────────────────────────────────────────────────────────────────

def business_context():
    return _cfg("DESK_BUSINESS_CONTEXT",
                "A shared inbox where customers and prospects write in.")


def categories():
    raw = [c.strip() for c in str(_cfg("DESK_CATEGORIES", "")).split(",") if c.strip()]
    return raw or list(DEFAULT_CATEGORIES)


def replyable():
    raw = [c.strip() for c in str(_cfg("DESK_REPLYABLE", "")).split(",") if c.strip()]
    return set(raw or ["sales_inquiry", "support_request"])


def playbook():
    """The ONLY facts a drafted reply is permitted to assert."""
    return _cfg("DESK_PLAYBOOK", "").strip()


def signoff_for(product):
    return _cfg("DESK_SIGNOFF", "{product} Team").replace("{product}", product or "Support")


def min_confidence():
    try:
        return float(str(_cfg("DESK_MIN_CONFIDENCE", "0.6")).strip())
    except (TypeError, ValueError):
        return 0.6


# ─────────────────────────────────────────────────────────────────────
# Agent 1 — Spam rescue
# ─────────────────────────────────────────────────────────────────────

_RESCUE_SYSTEM = """You are catching FALSE POSITIVES in a spam filter.

{context}

This message was filed as spam. Decide whether it is really business mail.

Return STRICT JSON, no prose: {{"business": <true|false>, "reason": "<=12 words"}}

business=true when it is plausibly a real person or organisation writing to this
desk for a real reason: a customer, a prospect, a partner, a supplier, an
invoice or account notice, or a reply to something we sent.

business=false ONLY for unambiguous junk: phishing and credential theft, fake
invoice or delivery notices, cryptocurrency promotion, adult or romance scams,
prize and lottery claims, unrelated mass-market advertising, and automated
bounces with no human intent.

When genuinely unsure, answer true. The costs are not symmetric: a wrong rescue
costs the operator one second of attention in their inbox, while a wrong
leave-in-spam loses a real customer silently and forever. Bias to rescue.
{lessons}"""


def _lesson_block(mailbox, agent, heading):
    lessons = load_lessons(mailbox, agent)
    return f"\n\n{heading}\n{lessons}" if lessons else ""


def rescue_spam(desk, dry_run=False, max_messages=None):
    """Move wrongly-spam-filtered business mail back to the Inbox."""
    mailbox = desk["mailbox"]
    stats = {"scanned": 0, "moved": 0, "left": 0, "errors": 0}
    if not agent_enabled(mailbox, AGENT_SPAM_RESCUE):
        return {"skipped": "not granted", **stats}

    session = DeskSession(mailbox)
    if not session.can_modify() and not dry_run:
        # Say so plainly rather than reporting a clean run that moved nothing:
        # "nothing to rescue" and "not allowed to rescue" must never look alike.
        return {"skipped": "token lacks gmail.modify", **stats}

    cap = max_messages or _int("DESK_SPAM_MAX", 40)
    lookback = _int("DESK_SPAM_LOOKBACK_DAYS", 0)
    query = "in:spam" + (f" newer_than:{lookback}d" if lookback > 0 else "")
    listing = gapi(session.token, "GET", "messages",
                   {"q": query, "maxResults": min(cap, 100)})
    ids = [m["id"] for m in (listing.get("messages") or [])][:cap]
    if not ids:
        stamp_beacon(AGENT_SPAM_RESCUE, mailbox, note="no spam to review", **stats)
        return stats

    decided = _already_decided(ids)
    model = agent_model(mailbox, AGENT_SPAM_RESCUE, _cfg("DESK_SPAM_MODEL", "sonnet"))
    lessons = _lesson_block(
        mailbox, AGENT_SPAM_RESCUE,
        "LEARNED ON THIS DESK (past rescues the owner reversed — "
        "do not rescue this class again):")
    system = _RESCUE_SYSTEM.format(context=business_context(), lessons=lessons)

    for msg_id in ids:
        if msg_id in decided:
            continue
        try:
            meta = _message(session.token, msg_id)
        except Exception:
            stats["errors"] += 1
            continue
        body = decode_body(meta["payload"]) or meta["snippet"]
        if not body.strip():
            continue
        stats["scanned"] += 1
        verdict = _ask(system, f"Subject: {meta['subject']}\nFrom: {meta['sender']}\n\n{body[:8000]}",
                       max_tokens=200, model=model)
        if verdict is None:
            stats["errors"] += 1
            continue
        business = bool(verdict.get("business"))
        reason = str(verdict.get("reason", ""))[:60]

        if dry_run:
            stats["moved" if business else "left"] += 1
            continue
        if business:
            try:
                rescue_from_spam(session.token, msg_id)
                stats["moved"] += 1
                _record_rescue(desk, meta, business, reason, "moved")
                throttle()
            except Exception:
                stats["errors"] += 1
        else:
            stats["left"] += 1
            _record_rescue(desk, meta, business, reason, "left")

    if not dry_run:
        stamp_beacon(AGENT_SPAM_RESCUE, mailbox, **stats)
    return stats


def _already_decided(ids):
    """Message ids already ruled on. Every decision is recorded, including
    'leave in spam' — otherwise each run re-classifies the same junk forever and
    the cost of the agent grows with the age of the Spam folder, not with new
    mail."""
    if not ids:
        return set()
    placeholders = ",".join(["%s"] * len(ids))
    rows = db_read(f"SELECT msg_id FROM desk_spam_rescues "
                   f"WHERE msg_id IN ({placeholders})", tuple(ids))
    return {r["msg_id"] for r in (rows or [])}


def _record_rescue(desk, meta, business, reason, action):
    db_write(
        "INSERT INTO desk_spam_rescues (msg_id, thread_id, mailbox, product, "
        "owner_email, sender, subject, snippet, verdict, action) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
        "ON DUPLICATE KEY UPDATE verdict=VALUES(verdict), action=VALUES(action)",
        (meta["id"], meta["thread_id"], desk["mailbox"], desk["product"],
         desk["owner"], meta["sender"][:320], meta["subject"][:500],
         meta["snippet"][:2000], (("business" if business else "spam") +
                                  (f": {reason}" if reason else ""))[:32], action))


# ─────────────────────────────────────────────────────────────────────
# Agent 2 — Responder
# ─────────────────────────────────────────────────────────────────────

_CLASSIFY_SYSTEM = """You route one email that arrived at a shared inbox.

{context}

Return STRICT JSON, no prose:
{{"category": "<one of: {categories}>",
  "confidence": <0.0-1.0>,
  "should_reply": <true|false>,
  "reason": "<=12 words"}}

should_reply=false whenever a reply would be wrong or wasted: bulk marketing and
newsletters, automated notifications, someone selling US something, a thread
already handled, or anything you are not confident about.

DIRECTION MATTERS. Someone inviting US to submit to, appear in, apply to or pay
for THEIR platform is not a customer inquiry, however commercial it sounds. Only
a person asking about what WE offer is.
{lessons}"""

_COMPOSE_SYSTEM = """You draft one reply for a human to review and send.

{context}

You are writing as: {signoff}

THE ONLY FACTS YOU MAY STATE ARE THESE:
{playbook}

Anything not written above, you do not know. If answering properly needs a fact
that is not there — a price, a date, a capability, a policy — do NOT estimate,
approximate or infer it. Ask the person for what you need, or say a colleague
will confirm it. An invented number in a drafted reply is worse than no reply at
all: a human skimming their drafts sends it, and then the business is committed
to it.

Write plainly and briefly. Answer the specific thing they asked rather than
delivering a general pitch. No subject line, no quoted history — reply body
only, ending with the signoff above.
{lessons}"""


def draft_replies(desk, dry_run=False, max_drafts=None):
    """Draft replies to correspondents who are waiting on this desk."""
    mailbox = desk["mailbox"]
    stats = {"scanned": 0, "drafted": 0, "skipped": 0, "errors": 0}
    if not agent_enabled(mailbox, AGENT_RESPONDER):
        return {"skipped_reason": "not granted", **stats}

    session = DeskSession(mailbox)
    if not session.can_draft() and not dry_run:
        return {"skipped_reason": "token cannot create drafts", **stats}

    # Before drafting anything new, retire drafts the humans have overtaken.
    if not dry_run:
        withdrawn, kept = withdraw_stale_drafts(session, mailbox)
        stats["withdrawn"], stats["already_sent"] = withdrawn, kept

    lookback = _int("DESK_RESPONDER_LOOKBACK_DAYS", 2)
    cap = max_drafts if max_drafts is not None else _int("DESK_RESPONDER_MAX_DRAFTS", 15)
    listing = gapi(session.token, "GET", "messages",
                   {"q": f"in:inbox newer_than:{lookback}d", "maxResults": 100})
    ids = [m["id"] for m in (listing.get("messages") or [])]

    model_classify = agent_model(mailbox, AGENT_RESPONDER,
                                 _cfg("DESK_CLASSIFY_MODEL", "haiku"))
    model_compose = _cfg("DESK_COMPOSE_MODEL", "sonnet")
    product = desk.get("product") or product_for(mailbox)
    classify_lessons = _lesson_block(mailbox, AGENT_RESPONDER,
                                     "LEARNED ON THIS DESK:")
    classify_system = _CLASSIFY_SYSTEM.format(
        context=business_context(), categories="|".join(categories()),
        lessons=classify_lessons)
    compose_system = _COMPOSE_SYSTEM.format(
        context=business_context(), signoff=signoff_for(product),
        playbook=playbook() or "(none configured — you have no facts to state)",
        lessons=_lesson_block(mailbox, AGENT_RESPONDER,
                              "HOW THIS DESK'S OWNER EDITS YOUR DRAFTS:"))

    for msg_id in ids:
        if cap and stats["drafted"] >= cap:
            break
        try:
            meta = _message(session.token, msg_id)
        except Exception:
            stats["errors"] += 1
            continue
        if not is_external(meta["sender"]):
            continue
        if _thread_has_row(mailbox, meta["thread_id"]):
            continue
        # Only reply where the correspondent genuinely spoke last. A thread a
        # colleague already answered is not waiting on us.
        if not customer_is_last_responder(session.token, meta["thread_id"]):
            continue

        body = decode_body(meta["payload"]) or meta["snippet"]
        if not body.strip():
            continue
        stats["scanned"] += 1

        verdict = _ask(classify_system,
                       f"Subject: {meta['subject']}\nFrom: {meta['sender']}\n\n{body[:6000]}",
                       max_tokens=200, model=model_classify)
        if verdict is None:
            stats["errors"] += 1
            continue
        category = str(verdict.get("category") or "other")
        try:
            confidence = float(verdict.get("confidence") or 0)
        except (TypeError, ValueError):
            confidence = 0.0
        if (not verdict.get("should_reply") or category not in replyable()
                or confidence < min_confidence()):
            stats["skipped"] += 1
            continue

        reply = content_of(_call(
            compose_system,
            f"Subject: {meta['subject']}\nFrom: {meta['sender']}\n\n{body[:6000]}",
            max_tokens=900, model=model_compose)).strip()
        if len(reply) < 20:
            # A near-empty completion is a failure, not a short answer. Drafting
            # it would put an empty reply in front of a human who is skimming.
            stats["errors"] += 1
            continue

        if dry_run:
            stats["drafted"] += 1
            continue
        try:
            created = upsert_thread_draft(
                session.token, mailbox, bare_email(meta["sender"]),
                reply_subject(meta["subject"]), reply,
                meta["thread_id"], meta["message_id_header"])
            _record_reply(desk, meta, category, confidence,
                          (created or {}).get("id"), body, reply)
            stats["drafted"] += 1
            throttle()
        except Exception:
            stats["errors"] += 1

    if not dry_run:
        stamp_beacon(AGENT_RESPONDER, mailbox, **{k: v for k, v in stats.items()
                                                  if isinstance(v, int)})
    return stats


def _thread_has_row(mailbox, thread_id):
    rows = db_read("SELECT 1 FROM desk_replies WHERE mailbox=%s AND thread_id=%s "
                   "LIMIT 1", (mailbox, thread_id))
    return bool(rows)


def _record_reply(desk, meta, category, confidence, draft_id, customer_body, reply):
    db_write(
        "INSERT INTO desk_replies (msg_id, thread_id, mailbox, product, "
        "owner_email, customer_email, subject, category, confidence, draft_id, "
        "customer_body, draft_body, mode) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'draft') "
        "ON DUPLICATE KEY UPDATE draft_id=VALUES(draft_id), "
        "draft_body=VALUES(draft_body), mode='draft'",
        (meta["id"], meta["thread_id"], desk["mailbox"], desk["product"],
         desk["owner"], bare_email(meta["sender"]), meta["subject"][:500],
         category[:40], confidence, draft_id, customer_body[:20000], reply[:20000]))


# ─────────────────────────────────────────────────────────────────────
# Agent 3 — Follow-up
# ─────────────────────────────────────────────────────────────────────

_GATE_SYSTEM = """Decide whether one email thread deserves a follow-up nudge.

{context}

Return STRICT JSON, no prose:
{{"relevant": <true|false>, "reason": "<=10 words"}}

relevant=true ONLY when the external person was genuinely engaged in the
business this desk handles and a gentle check-in would be welcome.

relevant=false for: bulk marketing and newsletters they sent us, automated
notifications, vendors selling to us, internal conversations, anything already
concluded, and any invitation for US to act on THEIR platform.

🔴 Judge intent from the [THEM] lines only. A message WE already sent into the
thread is not evidence of what they want — including an earlier nudge from this
very agent. Without that rule the first wrong nudge re-justifies itself on every
subsequent run and the mistake becomes permanent.

When unsure, answer false. A missing nudge costs nothing; a wrong one is a
stranger receiving a chase-up about something they never asked for."""


def draft_followups(desk, dry_run=False, max_drafts=None):
    """Draft a nudge on threads where we spoke last and they went quiet."""
    mailbox = desk["mailbox"]
    stats = {"candidates": 0, "drafted": 0, "gated_out": 0, "errors": 0}
    if not agent_enabled(mailbox, AGENT_FOLLOWUP):
        return {"skipped_reason": "not granted", **stats}

    session = DeskSession(mailbox)
    if not session.can_draft() and not dry_run:
        return {"skipped_reason": "token cannot create drafts", **stats}

    stale_hours = _int("DESK_FOLLOWUP_STALE_HOURS", 48)
    lookback = _int("DESK_FOLLOWUP_LOOKBACK_DAYS", 30)
    cap = max_drafts if max_drafts is not None else _int("DESK_FOLLOWUP_MAX_DRAFTS", 0)

    # 🔴 ASK GMAIL FOR STALE THREADS — do not fetch the newest and filter after.
    #
    # The obvious implementation takes the most recent N sent threads and then
    # keeps the ones older than the staleness bar. On a quiet desk that works.
    # On a busy one the two windows stop overlapping — the newest N are all more
    # recent than the bar — and the agent produces nothing, forever, with no
    # error and no deploy to blame. Measured on a live desk: 627 sent threads in
    # 30 days against a cap of 200 meant the OLDEST thread reachable was 10
    # hours old, against a 48-hour bar. Not one candidate was structurally
    # possible.
    #
    # Asking for `older_than:` inverts it: the cap now bounds a population that
    # already qualifies, so a busier desk yields MORE candidates, not fewer.
    query = (f"in:sent newer_than:{lookback}d "
             f"older_than:{max(1, stale_hours // 24)}d")
    listing = gapi(session.token, "GET", "threads", {"q": query, "maxResults": 100})
    thread_ids = [t["id"] for t in (listing.get("threads") or [])]

    gate_on = envflag("DESK_FOLLOWUP_GATE", True)
    gate_model = agent_model(mailbox, AGENT_FOLLOWUP,
                             _cfg("DESK_FOLLOWUP_GATE_MODEL", "haiku"))
    gate_system = _GATE_SYSTEM.format(context=business_context())
    product = desk.get("product") or product_for(mailbox)
    nudge = _cfg("DESK_NUDGE", _DEFAULT_NUDGE).replace("{product}", product)

    for thread_id in thread_ids:
        if cap and stats["drafted"] >= cap:
            break
        if _followed_up(mailbox, thread_id) or _thread_has_row(mailbox, thread_id):
            continue
        if not we_spoke_last(session.token, thread_id):
            continue
        try:
            thread = gapi(session.token, "GET", f"threads/{thread_id}", {"format": "full"})
        except Exception:
            stats["errors"] += 1
            continue
        transcript, correspondent, last_message_id = _transcript(thread)
        if not correspondent:
            continue
        stats["candidates"] += 1

        if gate_on:
            verdict = _ask(gate_system, transcript[:8000], max_tokens=120,
                           model=gate_model)
            # Fails CLOSED. An unreadable verdict must not become a nudge to a
            # stranger — the whole point of the gate is the mail we do not send.
            if verdict is None or not verdict.get("relevant"):
                stats["gated_out"] += 1
                continue

        if dry_run:
            stats["drafted"] += 1
            continue
        try:
            subject = reply_subject(_thread_subject(thread))
            created = upsert_thread_draft(session.token, mailbox, correspondent,
                                          subject, nudge, thread_id, last_message_id)
            db_write(
                "INSERT INTO desk_followups (mailbox, thread_id, product, "
                "owner_email, correspondent, subject, draft_id) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s) "
                "ON DUPLICATE KEY UPDATE draft_id=VALUES(draft_id)",
                (mailbox, thread_id, desk["product"], desk["owner"],
                 correspondent, subject[:500], (created or {}).get("id")))
            stats["drafted"] += 1
            throttle()
        except Exception:
            stats["errors"] += 1

    if not dry_run:
        stamp_beacon(AGENT_FOLLOWUP, mailbox, **stats)
    return stats


_DEFAULT_NUDGE = (
    "Hi,\n\n"
    "Just circling back on my last note — I wanted to make sure it reached you, "
    "and to check whether you had any questions.\n\n"
    "If it's useful to pick this back up, simply reply here. No rush either "
    "way.\n\n"
    "Best regards,\n"
    "{product} Team"
)


def _followed_up(mailbox, thread_id):
    """One nudge per thread, ever. The UNIQUE key in the table is the real
    guarantee; this is the cheap check that avoids the work."""
    rows = db_read("SELECT 1 FROM desk_followups WHERE mailbox=%s AND "
                   "thread_id=%s LIMIT 1", (mailbox, thread_id))
    return bool(rows)


def _transcript(thread):
    """(labelled transcript, their address, last Message-ID header).

    Messages are tagged [THEM] / [US] so the gate can be told to weigh only one
    of them. Handing a model an unlabelled thread and asking what "they" want
    invites it to read our own words back as theirs."""
    lines, correspondent, last_message_id = [], "", None
    for m in (thread.get("messages") or []):
        if "DRAFT" in (m.get("labelIds") or []):
            continue
        payload = m.get("payload") or {}
        headers = {h["name"].lower(): (h.get("value") or "")
                   for h in (payload.get("headers") or [])}
        sender = headers.get("from", "")
        external = is_external(sender)
        if external and not correspondent:
            correspondent = bare_email(sender)
        last_message_id = headers.get("message-id") or last_message_id
        body = decode_body(payload)[:1500]
        lines.append(f"[{'THEM' if external else 'US'}] {body}")
    return "\n\n".join(lines), correspondent, last_message_id


def _thread_subject(thread):
    for m in (thread.get("messages") or []):
        for h in ((m.get("payload") or {}).get("headers") or []):
            if h["name"].lower() == "subject":
                return h.get("value") or ""
    return ""


# ─────────────────────────────────────────────────────────────────────
# Agent 4 — Learning
# ─────────────────────────────────────────────────────────────────────

_DISTILL_SYSTEM = """You improve an email assistant by studying one correction.

Below is a reply the assistant DRAFTED and the reply a human actually SENT after
editing it. In ONE sentence of at most 25 words, state the concrete lesson for
next time — a tone shift, a fact or price they corrected, something they always
add, something they always cut.

If the differences are only cosmetic — whitespace, a greeting, reordering —
reply with exactly: SKIP

Output the sentence alone, nothing else."""


def learn_from_humans(desk, dry_run=False):
    """Distil lessons from what the humans did with this desk's output."""
    mailbox = desk["mailbox"]
    stats = {"reviewed": 0, "lessons": 0, "sent_as_is": 0, "reversed": 0}
    if not agent_enabled(mailbox, AGENT_LEARNING):
        return {"skipped_reason": "not granted", **stats}

    session = DeskSession(mailbox)
    today = datetime.now(timezone.utc).date().isoformat()
    grace = _int("DESK_LEARNING_GRACE_HOURS", 18)
    lookback = _int("DESK_LEARNING_LOOKBACK_DAYS", 14)
    limit = _int("DESK_LEARNING_MAX", 40)
    model = agent_model(mailbox, AGENT_LEARNING, _cfg("DESK_LEARNING_MODEL", "haiku"))

    _learn_from_edits(desk, session, stats, today, grace, lookback, limit,
                      model, dry_run)
    _learn_from_reversals(desk, session, stats, today, grace, lookback, limit,
                          dry_run)
    if not dry_run:
        stamp_beacon(AGENT_LEARNING, mailbox, **stats)
    return stats


def _learn_from_edits(desk, session, stats, today, grace, lookback, limit,
                      model, dry_run):
    """Compare each draft we left against what the human actually sent."""
    rows = db_read(
        "SELECT msg_id, thread_id, product, draft_body, created_at "
        "FROM desk_replies WHERE mailbox=%s AND mode='draft' "
        "AND draft_body IS NOT NULL "
        "AND created_at < NOW() - INTERVAL %s HOUR "
        "AND created_at > NOW() - INTERVAL %s DAY "
        "ORDER BY created_at DESC LIMIT %s",
        (desk["mailbox"], grace, lookback, limit)) or []
    seen = _already_learned("draft_edit", [r["msg_id"] for r in rows])

    for r in rows:
        if r["msg_id"] in seen:
            continue
        sent = _our_latest_send(session.token, r["thread_id"],
                               _epoch_ms(r.get("created_at")))
        if not sent:
            continue                       # not sent yet — reconsider next run
        stats["reviewed"] += 1
        draft = (r.get("draft_body") or "").strip()
        ratio = difflib.SequenceMatcher(None, _norm(draft), _norm(sent)).ratio()
        if ratio >= 0.92:
            stats["sent_as_is"] += 1
            _mark_learned("draft_edit", r["msg_id"], desk["mailbox"],
                          AGENT_RESPONDER, "sent as drafted", dry_run)
            continue
        lesson = content_of(_call(
            _DISTILL_SYSTEM,
            f"--- WE DRAFTED ---\n{draft[:4000]}\n\n--- HUMAN SENT ---\n{sent[:4000]}",
            max_tokens=120, model=model)).strip()
        if not lesson or lesson.upper().startswith("SKIP") or len(lesson) < 8:
            _mark_learned("draft_edit", r["msg_id"], desk["mailbox"],
                          AGENT_RESPONDER, "cosmetic edit", dry_run)
            continue
        if not dry_run and append_lesson(desk["mailbox"], AGENT_RESPONDER,
                                         lesson, today):
            stats["lessons"] += 1
        _mark_learned("draft_edit", r["msg_id"], desk["mailbox"],
                      AGENT_RESPONDER, lesson, dry_run)


def _learn_from_reversals(desk, session, stats, today, grace, lookback, limit,
                          dry_run):
    """A rescue the human sent back to Spam is a labelled false positive."""
    rows = db_read(
        "SELECT msg_id, sender, subject FROM desk_spam_rescues "
        "WHERE mailbox=%s AND action='moved' "
        "AND created_at < NOW() - INTERVAL %s HOUR "
        "AND created_at > NOW() - INTERVAL %s DAY "
        "ORDER BY created_at DESC LIMIT %s",
        (desk["mailbox"], grace, lookback, limit)) or []
    seen = _already_learned("rescue_reversed", [r["msg_id"] for r in rows])

    for r in rows:
        if r["msg_id"] in seen:
            continue
        try:
            labels = gapi(session.token, "GET", f"messages/{r['msg_id']}",
                          {"format": "minimal"}).get("labelIds") or []
        except Exception:
            continue                       # cannot tell — do not guess a lesson
        stats["reviewed"] += 1
        if "SPAM" not in labels:
            _mark_learned("rescue_reversed", r["msg_id"], desk["mailbox"],
                          AGENT_SPAM_RESCUE, "rescue accepted", dry_run)
            continue
        lesson = (f"We rescued and the owner returned it to Spam: "
                  f"\"{(r.get('subject') or '')[:80]}\" from "
                  f"{(r.get('sender') or '')[:70]} — leave this class in Spam.")
        stats["reversed"] += 1
        if not dry_run and append_lesson(desk["mailbox"], AGENT_SPAM_RESCUE,
                                         lesson, today):
            stats["lessons"] += 1
        _mark_learned("rescue_reversed", r["msg_id"], desk["mailbox"],
                      AGENT_SPAM_RESCUE, lesson, dry_run)


def _our_latest_send(token, thread_id, after_ms):
    """Body of the newest message WE sent on this thread AFTER `after_ms`.

    🔴 The time bound is load-bearing and money-facing. `after_ms` is when our
    draft was created; only a send that POSTDATES it can be a human's edit OF
    it. Without the bound, a multi-turn thread gets our NEW draft diffed against
    an OLDER outbound turn — so a draft correctly quoting the current price, set
    against an earlier turn that quoted a one-off discount, distils a confident
    lesson to offer the discount as standard. That lesson then feeds straight
    back into the composer's prompt and biases every future draft downward. A
    missed lesson costs nothing; a wrong one compounds."""
    try:
        thread = gapi(token, "GET", f"threads/{thread_id}", {"format": "full"})
    except Exception:
        return ""
    best_ts, best_body = -1, ""
    for m in (thread.get("messages") or []):
        labels = m.get("labelIds") or []
        if "SENT" not in labels or "DRAFT" in labels:
            continue
        ts = int(m.get("internalDate") or 0)
        if ts <= after_ms or ts <= best_ts:
            continue
        best_ts, best_body = ts, decode_body(m.get("payload") or {})
    return (best_body or "").strip()


def _already_learned(source, ids):
    if not ids:
        return set()
    placeholders = ",".join(["%s"] * len(ids))
    rows = db_read(f"SELECT source_id FROM desk_lessons_seen WHERE source=%s "
                   f"AND source_id IN ({placeholders})", (source, *ids))
    return {r["source_id"] for r in (rows or [])}


def _mark_learned(source, source_id, mailbox, agent, lesson, dry_run):
    if dry_run:
        return
    db_write(
        "INSERT INTO desk_lessons_seen (source, source_id, mailbox, agent, lesson) "
        "VALUES (%s,%s,%s,%s,%s) ON DUPLICATE KEY UPDATE lesson=VALUES(lesson)",
        (source, source_id, mailbox, agent, (lesson or "")[:2000]))


def _epoch_ms(dt):
    if not dt:
        return 0
    try:
        return int(dt.replace(tzinfo=timezone.utc).timestamp() * 1000)
    except (AttributeError, TypeError, ValueError):
        return 0


def _norm(text):
    return " ".join((text or "").split()).lower()


# ─────────────────────────────────────────────────────────────────────
# Shared internals
# ─────────────────────────────────────────────────────────────────────

def _message(token, msg_id):
    r = gapi(token, "GET", f"messages/{msg_id}", {"format": "full"})
    payload = r.get("payload") or {}
    headers = {h["name"].lower(): (h.get("value") or "")
               for h in (payload.get("headers") or [])}
    return {
        "id": msg_id,
        "thread_id": r.get("threadId"),
        "sender": headers.get("from", "")[:400],
        "subject": headers.get("subject", "")[:500],
        "message_id_header": headers.get("message-id"),
        "snippet": (r.get("snippet") or "")[:400],
        "payload": payload,
    }


def _call(system, user, max_tokens, model):
    try:
        return globus_call_chat(system, [{"role": "user", "content": user}],
                                max_tokens=max_tokens, model=model)
    except Exception as e:
        print(f"[desk-agents] model call failed ({type(e).__name__}: {e})",
              flush=True)
        return None


def _ask(system, user, max_tokens, model):
    """A JSON verdict, or None where the model did not produce one.

    None is a first-class answer here. Every caller treats it as "do nothing" —
    never as a default verdict — because a fabricated judgment is
    indistinguishable from a real one the moment it is written down."""
    return parse_json(_call(system, user, max_tokens, model))


AGENTS = {
    AGENT_SPAM_RESCUE: rescue_spam,
    AGENT_RESPONDER: draft_replies,
    AGENT_FOLLOWUP: draft_followups,
    AGENT_LEARNING: learn_from_humans,
}
