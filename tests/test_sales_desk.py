"""Behavioural tests for the sales desk.

The invariants here are the ones that decide whether a sales team can trust
the list:

  * ranking is BATCHED with GLOBAL indices, so batches merge coherently,
  * a lead is NEVER silently dropped, even if the model skips it,
  * a partial ranking is discarded rather than shipped as if complete,
  * every model layer FAILS OPEN to a deterministic order — the list always
    renders,
  * an empty pool fails CLOSED and loudly, because an empty call list looks
    exactly like a quiet day,
  * a model failure never becomes the brief's content.

Hermetic: db_helpers and globus_llm are stubbed. No DB, no network, no LLM.
Run with:  python tests/test_sales_desk.py
"""
import json
import os
import sys
import types

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(_HERE), "server"))

_CFG, _WRITES = {}, []

_dbh = types.ModuleType("db_helpers")
_dbh.db_read = lambda sql, params=(): []
_dbh.db_write = lambda sql, params=(): _WRITES.append((sql, params)) or True
_dbh.cfg = lambda k, d="": _CFG.get(k, d)
sys.modules["db_helpers"] = _dbh

_llm = types.ModuleType("globus_llm")
_llm._mode = "ok"        # ok | raise | short | garbage | partial
_llm._calls = []


def _call_chat(system, messages, max_tokens=2000, tools=None, model=None):
    body = messages[0]["content"]
    _llm._calls.append({"model": model, "body": body})
    if _llm._mode == "raise":
        raise RuntimeError("provider down")
    if "rank sales leads" in system:
        idxs = []
        for line in body.splitlines():
            n = line.split("\t")[0].strip()
            if n.isdigit():
                idxs.append(int(n))
        if _llm._mode == "garbage":
            return {"choices": [{"message": {"content": "I cannot help."}}]}
        if _llm._mode == "partial":
            idxs = idxs[:1]                      # answer almost nothing
        out = "\n".join(f"{i}|call_now|Call about their reply" for i in idxs)
        return {"choices": [{"message": {"content": out}}]}
    # brief
    if _llm._mode == "short":
        return {"choices": [{"message": {"content": "ok"}}]}
    return {"choices": [{"message": {"content":
            "Focus on the three replied leads today; the rest can wait."}}]}


_llm.globus_call_chat = _call_chat
sys.modules["globus_llm"] = _llm

import sales_desk as S  # noqa: E402

PASS, FAIL = [], []


def check(name, cond):
    (PASS if cond else FAIL).append(name)
    print(("  ok   " if cond else "  FAIL ") + name)


def leads(n, status="sent", days=3):
    return [{"id": f"l{i}", "name": f"Lead {i}", "email": f"l{i}@x.com",
             "company": f"Co {i}", "title": "", "status": status,
             "owner": "rep@x.com", "days_since": days, "note": "",
             "source": "test", "link": ""} for i in range(1, n + 1)]


def use_source(rows):
    S.register_source("test", lambda member, limit: list(rows))
    _CFG["SALES_DESK_SOURCES"] = "test"


# ── gather: eligibility, dedup, bounding ────────────────────────────────
print("gather:")

use_source(leads(3) + [{"id": "dup", "name": "Dup", "email": "l1@x.com",
                        "status": "sent", "days_since": 1}])
check("dedups by email", len(S.gather("m@x.com")) == 3)

use_source(leads(2) + [dict(l, status="bounced") for l in leads(2)])
check("terminal statuses are excluded",
      all(l["status"] != "bounced" for l in S.gather("m@x.com")))

_CFG["SALES_DESK_STATUS_RULES"] = json.dumps(
    {"sent": {"callable": False, "weight": 0, "terminal": False}})
use_source(leads(3))
check("a status marked NOT callable is excluded (statuses are data)",
      S.gather("m@x.com") == [])
_CFG.pop("SALES_DESK_STATUS_RULES")

use_source(leads(5) + [dict(l, id=f"u{l['id']}", email=f"u{l['email']}",
                            status="brand_new_stage") for l in leads(2)])
check("an UNKNOWN status stays callable (a new CRM stage must not vanish)",
      any(l["status"] == "brand_new_stage" for l in S.gather("m@x.com")))

_CFG["SALES_DESK_RANK_MAX"] = "10"
use_source(leads(50))
check("the candidate pool is bounded", len(S.gather("m@x.com")) == 10)
_CFG.pop("SALES_DESK_RANK_MAX")


def broken(member, limit):
    raise RuntimeError("source exploded")


S.register_source("broken", broken)
S.register_source("good", lambda m, l: leads(3))
_CFG["SALES_DESK_SOURCES"] = "broken,good"
check("one broken source does not deny the team its list",
      len(S.gather("m@x.com")) == 3)
_CFG["SALES_DESK_SOURCES"] = "test"


# ── batching + global indices ───────────────────────────────────────────
print("ranking — batching and merge:")

_llm._mode = "ok"
_llm._calls.clear()
_CFG["SALES_DESK_RANK_BATCH"] = "10"
pool = leads(25)
ranked = S.llm_rank(pool)
check("ranking is batched (25 leads / batch 10 -> 3 calls)",
      len(_llm._calls) == 3)
check("every lead survives the merge", len(ranked) == 25)
idxs = [int(l.split("\t")[0]) for c in _llm._calls
        for l in c["body"].splitlines()]
check("indices are GLOBAL and contiguous across batches",
      idxs == list(range(1, 26)))
check("the ranking model is pinned (not inherited from chat)",
      all(c["model"] for c in _llm._calls))


print("ranking — never drop, never ship partial:")

_llm._mode = "partial"          # model answers 1 index per batch
_CFG["SALES_DESK_RANK_BATCH"] = "10"
check("a badly partial ranking is DISCARDED, not shipped short",
      S.llm_rank(leads(25)) is None)

# a nearly-complete ranking is kept, and the skipped lead still appears
_llm._mode = "ok"


def _drop_one(system, messages, max_tokens=2000, tools=None, model=None):
    body = messages[0]["content"]
    idxs = [int(l.split("\t")[0]) for l in body.splitlines()
            if l.split("\t")[0].strip().isdigit()]
    idxs = idxs[:-1] if len(idxs) > 1 else idxs      # skip the last one
    return {"choices": [{"message": {"content":
            "\n".join(f"{i}|today|Do the thing" for i in idxs)}}]}


# NOTE: sales_desk does `from globus_llm import globus_call_chat`, which binds
# the function at import time — so the substitute must be installed on the
# sales_desk module, not on globus_llm. Patching the wrong one silently leaves
# the real stub in place and the test passes without exercising anything.
S.globus_call_chat = _drop_one
_CFG["SALES_DESK_RANK_BATCH"] = "100"
r = S.llm_rank(leads(100))
check("a lead the model skipped is still present (never silently dropped)",
      r is not None and len(r) == 100)
check("...and it is marked as unranked rather than faked",
      any(x.get("_unranked") for x in r))
check("...exactly one lead was left unranked",
      sum(1 for x in r if x.get("_unranked")) == 1)
S.globus_call_chat = _call_chat

print("ranking — hallucinated / malformed output:")
got = S._parse_rank_lines("5|call_now|a\n99|today|b\nxx|today|c", 1, 10)
check("an index outside the batch is rejected", 99 not in got)
check("a non-numeric index is ignored", len(got) == 1)
got = S._parse_rank_lines("- 3 | URGENT!! | do it", 1, 10)
check("an unknown band is coerced, not dropped", got[3][0] == "today")
got = S._parse_rank_lines("4|nurture|", 1, 10)
check("an empty action is tolerated", 4 in got)


# ── fail open ───────────────────────────────────────────────────────────
print("fail-open:")

_llm._mode = "raise"
check("a provider outage yields None -> caller falls back",
      S.llm_rank(leads(5)) is None)
check("the brief never returns the error text", S.build_brief(
    S.deterministic_rank(leads(5))) == "")

_llm._mode = "garbage"
check("an unparseable ranking is discarded", S.llm_rank(leads(20)) is None)

_llm._mode = "short"
check("an implausibly short brief is treated as a failure",
      S.build_brief(S.deterministic_rank(leads(5))) == "")

_llm._mode = "raise"
use_source(leads(6))
chunks, meta = S.run("m@x.com", use_llm=True)
check("THE LIST STILL POSTS when every model layer is down", bool(chunks))
check("...and the fallback is reported, not hidden", meta["fell_back"] is True)
check("...and it is genuinely deterministic order", meta["ranked"] == 6)
_llm._mode = "ok"


# ── fail closed on an empty feed ────────────────────────────────────────
print("fail-closed:")

use_source([])
raised = False
try:
    S.run("m@x.com")
except RuntimeError as e:
    raised = "empty list" in str(e) or "no callable leads" in str(e)
check("an empty pool RAISES (an empty list reads as a quiet day)", raised)


# ── rendering ───────────────────────────────────────────────────────────
print("rendering:")

use_source(leads(60))
chunks, meta = S.run("m@x.com", use_llm=False, limit=40)
check("chunks respect the transport limit",
      all(len(c) <= S.CHUNK_MAX + 400 for c in chunks))
check("the header states shown-vs-total coverage", "of" in chunks[0])
joined = "\n".join(chunks)
check("band headers are rendered", any(lbl.split()[-1] in joined
                                       for lbl in S.BAND_LABEL.values()))
h = S.hygiene(leads(10, days=99))
check("hygiene counts stale leads deterministically", h["stale"] == 10)
check("hygiene needs no model", "no_email" in h and h["total"] == 10)


# ── beacon ──────────────────────────────────────────────────────────────
print("beacon:")
_WRITES.clear()
S.stamp_beacon("posted", "pool=10")
check("a beacon is stamped with a timestamp + status",
      _WRITES and '"status": "posted"' in _WRITES[0][1][1]
      and '"at"' in _WRITES[0][1][1])


def _boom(sql, params=()):
    raise RuntimeError("db down")


_dbh.db_write = _boom
S.stamp_beacon("posted")          # must not raise
check("a beacon write failure can never crash the run", True)
_dbh.db_write = lambda sql, params=(): _WRITES.append((sql, params)) or True

print("config:")
check("business context ships EMPTY", S.business_context() == "")
check("timezone comes from config, never an inline offset",
      S.tz_offset_minutes() == 0)
_CFG["SALES_DESK_TZ_OFFSET_MIN"] = "330"
check("...and is honoured when set", S.tz_offset_minutes() == 330)
_CFG.pop("SALES_DESK_TZ_OFFSET_MIN")

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
if FAIL:
    for f in FAIL:
        print("  FAILED: " + f)
    sys.exit(1)
print("sales-desk invariants hold.")
