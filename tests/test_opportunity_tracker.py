"""Behavioural tests for the opportunity tracker.

The invariants that decide whether the funnel can be trusted:

  * an automated screener is NOT a human interview (checked first, because the
    wording overlaps and conflating them inflates the one number that matters),
  * marketing mail from a company you approached is not a reply from them,
  * stages only ever move FORWARD — replies arrive out of order,
  * matching is conservative: a wrong match rewrites an unrelated
    opportunity's history, a miss merely reads as "no response yet",
  * re-running over the same mail changes nothing.

Hermetic: db_helpers and globus_llm stubbed. No DB, no network, no LLM.
Run with:  python tests/test_opportunity_tracker.py
"""
import os
import sys
import types
from datetime import datetime, timedelta

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(_HERE), "server"))

_CFG, _WRITES = {}, []
_OPPS = []

_dbh = types.ModuleType("db_helpers")


def _db_read(sql, params=()):
    if "FROM opportunities" in sql:
        rows = [dict(o) for o in _OPPS]
        if "NOT IN ('rejected','closed')" in sql:
            rows = [r for r in rows if r["stage"] not in ("rejected", "closed")]
        return rows
    return []


def _db_write(sql, params=()):
    _WRITES.append((sql, params))
    if sql.startswith("UPDATE opportunities SET stage"):
        stage, oid = params[0], params[1]
        for o in _OPPS:
            if o["id"] == oid:
                o["stage"] = stage
    return True


_dbh.db_read = _db_read
_dbh.db_write = _db_write
_dbh.cfg = lambda k, d="": _CFG.get(k, d)
sys.modules["db_helpers"] = _dbh

_llm = types.ModuleType("globus_llm")
_llm._reply = "replied"
_llm._raise = False


def _call_chat(system, messages, max_tokens=2000, tools=None, model=None):
    if _llm._raise:
        raise RuntimeError("provider down")
    return {"choices": [{"message": {"content": _llm._reply}}]}


_llm.globus_call_chat = _call_chat
sys.modules["globus_llm"] = _llm

import opportunity_tracker as T  # noqa: E402

PASS, FAIL = [], []


def check(name, cond):
    (PASS if cond else FAIL).append(name)
    print(("  ok   " if cond else "  FAIL ") + name)


def opp(i, org, domain="", stage="submitted", days_ago=1, title=""):
    return {"id": i, "member_email": "me@x.com", "slug": f"s{i}", "org": org,
            "title": title, "url": "", "domain": domain, "stage": stage,
            "stage_updated_at": datetime.utcnow() - timedelta(days=days_ago),
            "submitted_at": None, "source": "", "notes": ""}


def msg(subject, snippet="", frm="someone@acme.com"):
    return {"id": "m1", "from_email": frm, "subject": subject,
            "snippet": snippet, "received_at": None}


# ── classification ──────────────────────────────────────────────────────
print("classification:")

check("a rejection is recognised",
      T.classify("Update on your application",
                 "Unfortunately we will not be moving forward") == "rejected")
check("an offer outranks everything",
      T.classify("Good news", "We are pleased to offer you the role") == "offer")
check("a human interview is recognised",
      T.classify("Next steps", "Can you share your availability for a call?")
      == "interview")

print("  — the distinction that matters most:")
for subj, snip in [
    ("Interview invitation", "Complete a short assessment to continue"),
    ("Your video interview", "This is a one-way video interview"),
    ("Next step", "Please complete the coding challenge"),
    ("Screening", "Our AI screener will ask a few questions"),
]:
    check(f"screener not interview: {snip[:34]!r}",
          T.classify(subj, snip) == "screener")

check("a real scheduling request is still an interview",
      T.classify("Chat?", "Grab a time on my calendly.com/x") == "interview")

print("  — marketing is not a reply:")
for subj, snip in [
    ("Jobs you may like", "Recommended jobs for you. Unsubscribe"),
    ("We're hiring!", "View in browser"),
    ("Weekly digest", "newsletter — manage your preferences"),
]:
    check(f"excluded: {subj!r}", T.classify(subj, snip) is None)

check("a no-reply automated receipt is an ack",
      T.classify("We received your application", "Thanks for applying",
                 "no-reply@acme.com") == "ack")
check("a plain human reply is 'replied'",
      T.classify("Re: your application", "Thanks, taking a look this week.",
                 "jane@acme.com") == "replied")


# ── matching ────────────────────────────────────────────────────────────
print("matching:")

opps = [opp(1, "Acme Corp", "acme.com"), opp(2, "Globex Health", "globex.com")]
check("sender domain match is authoritative",
      T.match_message(msg("hi", "", "recruiter@acme.com"), opps)["id"] == 1)
check("a subdomain still matches the org",
      T.match_message(msg("hi", "", "no-reply@mail.acme.com"), opps)["id"] == 1)
check("an unrelated sender does not match",
      T.match_message(msg("hi", "", "x@random.com"), opps) is None)

no_dom = [opp(3, "Zephyr Robotics")]
check("name match works when no domain is known",
      T.match_message(msg("Your Zephyr Robotics application"), no_dom)["id"] == 3)
check("ONE distinctive word is not enough for a two-word org",
      T.match_message(msg("Your Zephyr application"), no_dom) is None)

single = [opp(4, "Palantir")]
check("a single-token org can match on its one token",
      T.match_message(msg("Palantir update"), single)["id"] == 4)

generic = [opp(5, "The Solutions Company Ltd")]
check("an all-generic org name never matches (would swallow the inbox)",
      T.match_message(msg("solutions company update"), generic) is None)
check("org_tokens drops generic words",
      T.org_tokens("The Global Tech Solutions Ltd") == [])


# ── stage transitions ───────────────────────────────────────────────────
print("stages — forward only:")

o = opp(1, "Acme", "acme.com", stage="submitted")
check("submitted -> interview advances", T.advance(o, "interview") is True)
check("interview -> replied is REFUSED (never rewind)",
      T.advance(o, "replied") is False)
check("...and the stage is unchanged", o["stage"] == "interview")
check("interview -> offer advances", T.advance(o, "offer") is True)
check("the same stage twice is a no-op (idempotent)",
      T.advance(o, "offer") is False)


# ── scan ────────────────────────────────────────────────────────────────
print("scan:")

_OPPS[:] = [opp(1, "Acme Corp", "acme.com"), opp(2, "Globex", "globex.com")]
res = T.scan("me@x.com", [
    msg("Next steps", "share your availability for a call", "hr@acme.com"),
    msg("Jobs you may like", "unsubscribe", "jobs@acme.com"),
    msg("Nothing to do with us", "", "x@other.com"),
])
check("only the real reply is matched", res["matched"] == 1)
check("...and it advanced one opportunity", res["advanced"] == 1)
check("...to interview", _OPPS[0]["stage"] == "interview")
check("unmatched mail is counted, not guessed at", res["unmatched"] == 2)

# re-running the same mail must change nothing
before = _OPPS[0]["stage"]
res2 = T.scan("me@x.com", [
    msg("Next steps", "share your availability for a call", "hr@acme.com")])
check("re-scanning the same mail advances nothing (idempotent)",
      res2["advanced"] == 0 and _OPPS[0]["stage"] == before)

# precedence: the most consequential reading wins within one run
_OPPS[:] = [opp(1, "Acme Corp", "acme.com")]
T.scan("me@x.com", [
    msg("We received your application", "thanks for applying",
        "no-reply@acme.com"),
    msg("Interview", "please share availability for a call", "hr@acme.com"),
])
check("when one org sends several messages, the strongest wins",
      _OPPS[0]["stage"] == "interview")

_OPPS[:] = [opp(1, "Acme Corp", "acme.com")]
T.scan("me@x.com", [msg("Next steps", "availability for a call",
                        "hr@acme.com")], dry_run=True)
check("a dry run changes nothing", _OPPS[0]["stage"] == "submitted")


# ── LLM fallback ────────────────────────────────────────────────────────
print("llm fallback:")

check("it is OFF by default (no silent per-message cost)",
      T.use_llm_fallback() is False)
_llm._raise = True
check("a provider failure leaves the message unclassified, never guessed",
      T.classify_llm("s", "b") is None)
_llm._raise = False
_llm._reply = "banana"
check("an off-menu answer is rejected", T.classify_llm("s", "b") is None)
_llm._reply = "none"
check("'none' means not-a-reply", T.classify_llm("s", "b") is None)
_llm._reply = "screener"
check("a valid answer is accepted", T.classify_llm("s", "b") == "screener")


# ── reporting ───────────────────────────────────────────────────────────
print("reporting:")

_OPPS[:] = [opp(1, "A", stage="submitted"), opp(2, "B", stage="interview"),
            opp(3, "C", stage="rejected"), opp(4, "D", stage="offer"),
            opp(5, "E", stage="queued")]
f = T.funnel("me@x.com")
check("queued is not counted as sent", f["sent"] == 4)
check("responded counts every stage past submitted", f["responded"] == 3)
check("interview rate counts offers too", f["interviews"] == 2)
check("rates are computed against sent, not total",
      abs(f["response_rate"] - 0.75) < 0.01)

_OPPS[:] = [opp(1, "Quiet Co", stage="submitted", days_ago=40),
            opp(2, "Fresh Co", stage="submitted", days_ago=1),
            opp(3, "Answered", stage="interview", days_ago=40)]
st = T.stale("me@x.com", days=14)
check("stale finds only unanswered, old, submitted ones",
      [o["org"] for o in st] == ["Quiet Co"])
txt = T.report_text("me@x.com", days=14)
check("the report names what to nudge", "Quiet Co" in txt and "nudge" in txt)

print("beacon:")
_WRITES.clear()
T.stamp_beacon("ok", "matched=3")
check("a beacon is stamped with timestamp + status",
      _WRITES and '"status": "ok"' in _WRITES[0][1][1])
_dbh.db_write = lambda s, p=(): (_ for _ in ()).throw(RuntimeError("db down"))
T.stamp_beacon("ok")
check("a beacon failure can never crash the run", True)
_dbh.db_write = _db_write

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
if FAIL:
    for f_ in FAIL:
        print("  FAILED: " + f_)
    sys.exit(1)
print("opportunity-tracker invariants hold.")
