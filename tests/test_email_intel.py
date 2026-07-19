"""Behavioural tests for the email-intelligence agent.

These cover the invariants that are expensive to learn the hard way:

  * the digest is HEARTBEAT-GATED — it never reports all-clear over a mailbox
    that never ran, and it names the dead one,
  * an unparseable model reply produces NO action, never a defaulted one,
  * the digest chunks, so a big day can't silently stop delivering forever,
  * a boolean env var is parsed honestly (`X=0` means off).

Hermetic: db_helpers / globus_llm / google_gmail / oauth_db are stubbed in
sys.modules, so there is no MySQL, no network and no LLM.
Run with:  python tests/test_email_intel.py
"""
import json
import os
import sys
import types
from datetime import datetime, timedelta, timezone

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(_HERE), "server"))

# ── stub every external dependency BEFORE importing email_intel ──────────
_CFG, _ROWS, _WRITES = {}, [], []


def _db_read(sql, params=()):
    for matcher, result in _ROWS:
        if matcher in sql:
            return result(params) if callable(result) else result
    return []


def _db_write(sql, params=()):
    _WRITES.append((sql, params))
    return True


_dbh = types.ModuleType("db_helpers")
_dbh.db_read = _db_read
_dbh.db_write = _db_write
_dbh.cfg = lambda k, d="": _CFG.get(k, d)
sys.modules["db_helpers"] = _dbh

_llm = types.ModuleType("globus_llm")
_llm._reply = ""
_llm._raise = False


def _call_chat(system, messages, max_tokens=2000, tools=None, model=None):
    if _llm._raise:
        raise RuntimeError("provider down")
    return {"choices": [{"message": {"content": _llm._reply}}]}


_llm.globus_call_chat = _call_chat
sys.modules["globus_llm"] = _llm

_gm = types.ModuleType("google_gmail")
for _n in ("gmail_list_messages", "gmail_get_message", "gmail_headers",
           "gmail_extract_body_text", "gmail_list_labels",
           "gmail_ensure_label", "gmail_add_labels", "parse_email_date"):
    setattr(_gm, _n, lambda *a, **k: None)
sys.modules["google_gmail"] = _gm

_oa = types.ModuleType("oauth_db")
_oa.get_valid_access_token = lambda conn: "tok"
sys.modules["oauth_db"] = _oa

import email_intel as E  # noqa: E402

PASS, FAIL = [], []


def check(name, cond):
    (PASS if cond else FAIL).append(name)
    print(("  ok   " if cond else "  FAIL ") + name)


def beacon(account, hours_ago):
    at = (datetime.now(timezone.utc) - timedelta(hours=hours_ago)
          ).replace(microsecond=0).isoformat()
    return {"name": E.beacon_key(account),
            "value": json.dumps({"at": at, "account": account})}


def rows_for(beacons=(), items=()):
    _ROWS.clear()
    _ROWS.append(("FROM config", list(beacons)))
    _ROWS.append(("FROM email_intel", list(items)))


ITEM = {"id": 1, "account": "a@x.com", "msg_id": "m1", "category": "Customer",
        "urgency": "high", "action_summary": "Reply today", "sender": "c@y.com",
        "subject": "Contract question", "received_at": None}


# ── the heartbeat gate ───────────────────────────────────────────────────
print("digest — heartbeat gating:")

rows_for(beacons=[], items=[])
out = E.build_digest(["a@x.com"], stale_hours=26)
text = out[0][0]
check("a mailbox that NEVER ran is reported DOWN, not all-clear",
      "PIPELINE DOWN" in text and "never" in text)
check("...and no all-clear is emitted at all", "No medium/high" not in text)
check("...and the dead mailbox is NAMED", "a@x.com" in text)

rows_for(beacons=[beacon("a@x.com", 100)], items=[])
text = E.build_digest(["a@x.com"], stale_hours=26)[0][0]
check("a STALE mailbox is reported DOWN", "PIPELINE DOWN" in text
      and "100h ago" in text)

rows_for(beacons=[beacon("a@x.com", 1)], items=[])
text = E.build_digest(["a@x.com"], stale_hours=26)[0][0]
check("a LIVE mailbox with no items gets a scoped all-clear",
      "No medium/high" in text and "PIPELINE DOWN" not in text)
check("...and the all-clear states how fresh the run was", "freshest run" in text)

rows_for(beacons=[beacon("a@x.com", 1)], items=[])
text = E.build_digest(["a@x.com", "dead@x.com"], stale_hours=26)[0][0]
check("mixed: all-clear is SCOPED and the dead mailbox still named",
      "PIPELINE DOWN" in text and "dead@x.com" in text
      and "No medium/high" in text)
check("header states coverage (live/total)", "1/2" in text)

rows_for(beacons=[beacon("a@x.com", 1)], items=[ITEM])
out = E.build_digest(["a@x.com"], stale_hours=26)
check("a live mailbox with an item renders it",
      "Contract question" in out[0][0] and out[0][1] == [1])


# ── beacon semantics ─────────────────────────────────────────────────────
print("beacon:")

rows_for(beacons=[beacon("a@x.com", 3)], items=[])
ages = E.beacon_ages(["a@x.com", "never@x.com"])
check("age is measured for a known mailbox", 2.5 < (ages["a@x.com"] or 0) < 3.5)
check("an unseen mailbox is None (never ran), not 0",
      ages["never@x.com"] is None)

rows_for(beacons=[{"name": E.beacon_key("a@x.com"), "value": "not json"}],
         items=[])
check("an unparseable beacon counts as NO proof of life",
      E.beacon_ages(["a@x.com"])["a@x.com"] is None)

_WRITES.clear()
E.stamp_beacon("a@x.com", note="empty-inbox")
check("stamping writes an explicit ISO timestamp (not relying on updated_at)",
      _WRITES and '"at"' in _WRITES[0][1][1])
check("...and records the account inside the payload",
      '"account": "a@x.com"' in _WRITES[0][1][1])
long_acct = ("x" * 90) + "@example.com"
check("a very long mailbox address cannot overflow config.name (80)",
      len(E.beacon_key(long_acct)) <= 80)


# ── parse failures must never become actions ─────────────────────────────
print("model parse failures:")

_llm._raise = True
check("a provider outage yields NO labels (whole batch retried later)",
      E.classify_batch([{"from": "a", "subject": "b", "snippet": "c"}]) == {})
check("a provider outage yields NO judgment (never a default category)",
      E.reason_one("s", "f", "b") is None)
_llm._raise = False

_llm._reply = "I'm sorry, I can't help with that."
check("unparseable triage reply yields no labels", E.classify_batch(
    [{"from": "a", "subject": "b", "snippet": "c"}]) == {})
check("unparseable reason reply yields None (retried, not bookmarked)",
      E.reason_one("s", "f", "b") is None)

_llm._reply = '{"n": 1, "label": "Customer"}\nnot json at all\n{"n": 2, "label": "Billing"}'
got = E.classify_batch([{"from": "a"}, {"from": "b"}, {"from": "c"}])
check("a malformed LINE is skipped, the good ones still land",
      got == {1: "Customer", 2: "Billing"})

_llm._reply = '```json\n{"category":"Deal","urgency":"HIGH","action":"Call",' \
              '"reasoning":"x"}\n```'
j = E.reason_one("s", "f", "b")
check("code-fenced JSON parses", j and j["category"] == "Deal")
check("urgency is normalised to the enum", j["urgency"] == "high")

_llm._reply = '{"category":"X","urgency":"catastrophic","action":"","reasoning":""}'
check("an out-of-range urgency degrades to 'none', never invented",
      E.reason_one("s", "f", "b")["urgency"] == "none")


# ── chunking + delivery ──────────────────────────────────────────────────
print("digest chunking:")

many = []
for i in range(40):
    it = dict(ITEM)
    it["id"] = i + 1
    it["subject"] = f"Subject number {i} " + ("y" * 60)
    many.append(it)
rows_for(beacons=[beacon("a@x.com", 1)], items=many)
chunks = E.build_digest(["a@x.com"], stale_hours=26, max_chars=1200)
check("a large day is split into multiple deliverable chunks", len(chunks) > 1)
all_ids = [i for _, ids in chunks for i in ids]
check("every row appears in exactly one chunk",
      sorted(all_ids) == list(range(1, 41)))
check("each chunk stays within the transport limit",
      all(len(t) <= 1400 for t, _ in chunks))
check("chunk ids let the caller resolve ONLY what was delivered",
      all(ids for _, ids in chunks))


# ── env flag honesty ─────────────────────────────────────────────────────
print("env flags:")
for raw, want in (("0", False), ("false", False), ("no", False), ("off", False),
                  ("", False), ("1", True), ("true", True), ("yes", True)):
    os.environ["_EI_TEST_FLAG"] = raw
    check(f"envflag({raw!r}) -> {want}",
          E.envflag("_EI_TEST_FLAG", False) is want)
os.environ.pop("_EI_TEST_FLAG", None)
check("an unset flag takes the default", E.envflag("_EI_UNSET", True) is True)


# ── config shape ─────────────────────────────────────────────────────────
print("config:")
check("a taxonomy ships by default so it runs out of the box",
      len(E.taxonomy()) >= 5)
_CFG["EMAIL_INTEL_TAXONOMY"] = '[{"name":"Alpha","description":"d"}]'
check("operator taxonomy replaces the default",
      E.taxonomy_names() == ["Alpha"])
_CFG["EMAIL_INTEL_TAXONOMY"] = "{not json"
check("a malformed taxonomy falls back to defaults instead of crashing",
      len(E.taxonomy()) >= 5)
_CFG.pop("EMAIL_INTEL_TAXONOMY")
check("business context ships EMPTY (a generic default would be wrong)",
      E.business_context() == "")
check("list cap and reason cap are SEPARATE knobs (cap spend, not sight)",
      E.list_max() > E.reason_max())

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
if FAIL:
    for f in FAIL:
        print("  FAILED: " + f)
    sys.exit(1)
print("email-intelligence invariants hold.")
