"""Behavioural tests for the shared-inbox desk agents.

These cover the invariants that are expensive to learn the hard way:

  * a draft is NEVER deleted once a human has sent it — and a genuinely unsent
    one still IS deleted, because a guard that refuses everything is not a fix,
  * an empty desk configuration resolves to NO desks, never to every mailbox,
  * an agent is off until granted, and an explicit opt-out survives a change of
    default,
  * an unparseable model reply produces NO action, never a defaulted verdict,
  * the learning pass ignores anything sent BEFORE the draft it is judging,
  * the lesson store stays bounded, because it is read back into a prompt.

Hermetic: db_helpers / globus_llm / google_gmail / oauth_db are stubbed in
sys.modules, so there is no MySQL, no network and no LLM.
Run with:  python tests/test_desk_agents.py
"""
import json
import os
import sys
import types
from datetime import datetime, timedelta, timezone

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(_HERE), "server"))

# ── stub every external dependency BEFORE importing the modules ──────────
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
_llm.globus_call_chat = lambda *a, **k: {"choices": [{"message": {"content": ""}}]}
sys.modules["globus_llm"] = _llm

_gm = types.ModuleType("google_gmail")
_gm.GMAIL_API = "https://gmail.googleapis.com/gmail/v1"
_gm.gmail_extract_body_text = lambda payload: (payload or {}).get("_text", "")
_gm.gmail_list_messages = lambda *a, **k: []
_gm.gmail_get_message = lambda *a, **k: {}
_gm.gmail_headers = lambda p: {}
_gm.gmail_list_labels = lambda *a, **k: []
_gm.gmail_ensure_label = lambda *a, **k: None
_gm.gmail_add_labels = lambda *a, **k: True
_gm.parse_email_date = lambda s: None
sys.modules["google_gmail"] = _gm

_oauth = types.ModuleType("oauth_db")
_oauth.get_valid_access_token = lambda conn: "test-token"
sys.modules["oauth_db"] = _oauth

import email_desks as D            # noqa: E402
import desk_agents as A            # noqa: E402

PASS, FAIL = [], []


def check(label, ok):
    (PASS if ok else FAIL).append(label)
    print(("  ok   " if ok else "  FAIL ") + label)


# ── the draft-deletion guard ─────────────────────────────────────────────
# The most expensive lesson in the module: when a human sends one of our drafts
# from the Gmail web UI, the draft id stays resolvable and re-points at the SENT
# message, so DELETE destroys a delivered reply permanently.
print("draft deletion:")

_CALLS = []


def _fake_gapi(labels_for_draft, raise_404=False, message_labels=None):
    def inner(token, method, path, params=None, body=None):
        _CALLS.append((method, path))
        if method == "GET" and path.startswith("drafts/"):
            if raise_404:
                from urllib.error import HTTPError
                raise HTTPError(path, 404, "gone", {}, None)
            return {"message": {"id": "m1", "labelIds": labels_for_draft}}
        if method == "GET" and path.startswith("messages/"):
            return {"labelIds": message_labels or []}
        return {}
    return inner


_orig_gapi = D.gapi

D.gapi = _fake_gapi(["DRAFT"])
_CALLS.clear()
check("a genuinely UNSENT draft is deleted (the guard still does its job)",
      D.delete_draft_if_unsent("t", "d1") is True
      and ("DELETE", "drafts/d1") in _CALLS)

D.gapi = _fake_gapi(["SENT", "INBOX"])
_CALLS.clear()
check("a draft the human already SENT is never deleted",
      D.delete_draft_if_unsent("t", "d1") is False
      and not any(m == "DELETE" for m, _ in _CALLS))

D.gapi = _fake_gapi([], message_labels=["SENT"])
_CALLS.clear()
check("labels absent on the draft are re-read from the message, then refused",
      D.delete_draft_if_unsent("t", "d1") is False
      and not any(m == "DELETE" for m, _ in _CALLS))


def _blows_up(token, method, path, params=None, body=None):
    if method == "GET" and path.startswith("drafts/"):
        return {"message": {"id": "m1", "labelIds": []}}
    if method == "GET" and path.startswith("messages/"):
        raise RuntimeError("gmail unavailable")
    _CALLS.append((method, path))
    return {}


D.gapi = _blows_up
_CALLS.clear()
check("it FAILS CLOSED — an unprovable draft is left alone",
      D.delete_draft_if_unsent("t", "d1") is False
      and not any(m == "DELETE" for m, _ in _CALLS))

D.gapi = _fake_gapi(["DRAFT"], raise_404=True)
check("a 404 (already consumed by an API send) is not an error",
      D.delete_draft_if_unsent("t", "d1") is False)

check("no draft id is a no-op, not a crash",
      D.delete_draft_if_unsent("t", "") is False)
D.gapi = _orig_gapi


# ── withdrawal marks 'sent' vs 'superseded' honestly ─────────────────────
print("stale-draft withdrawal:")
_ROWS.append(("FROM desk_replies", [{"thread_id": "th1", "draft_id": "d1"}]))


class _Session:
    token = "t"


def _thread_ours(token, method, path, params=None, body=None):
    if path.startswith("threads/"):
        return {"messages": [{"labelIds": ["SENT"], "payload": {"headers": [
            {"name": "From", "value": "desk@acme.example"}]}}]}
    if path.startswith("drafts/"):
        return {"message": {"id": "m1", "labelIds": ["SENT"]}}   # human sent it
    return {}


_CFG["DESK_PRODUCTS"] = json.dumps({"acme.example": "Acme"})
D.gapi = _thread_ours
_WRITES.clear()
withdrawn, kept = D.withdraw_stale_drafts(_Session(), "desk@acme.example")
check("a draft the human already sent is counted as kept, not withdrawn",
      (withdrawn, kept) == (0, 1))
check("and the row is marked 'sent', so the log claims no withdrawal",
      any("sent" in str(p) for _, p in _WRITES))
D.gapi = _orig_gapi
_ROWS.clear()


# ── desk discovery ───────────────────────────────────────────────────────
print("desk discovery:")
check("no owners and no mailboxes resolves to NO desks, not every mailbox",
      D.discover_desks() == [])
_ROWS.append(("globus_oauth_connections",
              [{"provider_account": "support@acme.example",
                "email": "staff@acme.example"}]))
check("an explicit mailbox list resolves",
      [d["mailbox"] for d in D.discover_desks(mailboxes=["support@acme.example"])]
      == ["support@acme.example"])
check("the product comes from the DOMAIN map, not the local part",
      D.discover_desks(mailboxes=["support@acme.example"])[0]["product"] == "Acme")
check("an excluded mailbox drops out",
      D.discover_desks(mailboxes=["support@acme.example"],
                       exclude=["support@acme.example"]) == [])
_ROWS.clear()
check("an unmapped domain still gets a usable product name",
      D.product_for("help@widgets.example") == "Widgets")


# ── grants ───────────────────────────────────────────────────────────────
print("grants:")
check("an agent is OFF for a desk with no row",
      D.agent_enabled("support@acme.example", D.AGENT_RESPONDER) is False)
_ROWS.append(("desk_agent_config", [{"enabled": 0, "model": None}]))
check("an explicit opt-OUT beats a caller passing default=True",
      D.agent_enabled("support@acme.example", D.AGENT_RESPONDER,
                      default=True) is False)
_ROWS.clear()
_ROWS.append(("desk_agent_config", [{"enabled": 1, "model": "haiku"}]))
check("a granted desk runs", D.agent_enabled("support@acme.example",
                                             D.AGENT_RESPONDER) is True)
check("a per-desk model override wins over the fallback",
      D.agent_model("support@acme.example", D.AGENT_RESPONDER, "sonnet") == "haiku")
_ROWS.clear()
check("with no row the fallback model is used",
      D.agent_model("support@acme.example", D.AGENT_RESPONDER, "sonnet") == "sonnet")


# ── model replies are never defaulted ────────────────────────────────────
print("model replies:")
check("unparseable JSON yields None, not an invented verdict",
      D.parse_json({"choices": [{"message": {"content": "sure thing!"}}]}) is None)
check("a fenced JSON block parses",
      D.parse_json({"choices": [{"message": {
          "content": "```json\n{\"business\": true}\n```"}}]}) == {"business": True})
check("an explicit null content does not crash the caller",
      D.content_of({"choices": [{"message": {"content": None}}]}) == "")
check("no response at all yields None", D.parse_json(None) is None)


# ── thread direction ─────────────────────────────────────────────────────
print("thread direction:")
D.gapi = lambda *a, **k: {"messages": []}
check("an empty thread is False in BOTH directions (neither side 'spoke last')",
      D.customer_is_last_responder("t", "th") is False
      and D.we_spoke_last("t", "th") is False)


def _mixed(token, method, path, params=None, body=None):
    return {"messages": [
        {"internalDate": "100", "payload": {"headers": [
            {"name": "From", "value": "buyer@outside.example"}]}},
        {"internalDate": "200", "payload": {"headers": [
            {"name": "From", "value": "desk@acme.example"}]}},
        {"internalDate": "300", "labelIds": ["DRAFT"], "payload": {"headers": [
            {"name": "From", "value": "desk@acme.example"}]}},
    ]}


D.gapi = _mixed
check("our own pending DRAFT is not counted as the newest message",
      D.we_spoke_last("t", "th") is True)
check("and the customer is correctly not last",
      D.customer_is_last_responder("t", "th") is False)
D.gapi = _orig_gapi

check("a no-reply sender is never external (never nudged)",
      D.is_external("Bounce <no-reply@outside.example>") is False)
check("one of our own domains is not external",
      D.is_external("someone@acme.example") is False)
check("a real outsider is external",
      D.is_external("buyer@outside.example") is True)


# ── the learning pass ────────────────────────────────────────────────────
print("learning:")


def _thread_with_sends(token, method, path, params=None, body=None):
    return {"messages": [
        {"labelIds": ["SENT"], "internalDate": "1000",
         "payload": {"_text": "an OLDER turn quoting a one-off discount"}},
        {"labelIds": ["SENT"], "internalDate": "3000",
         "payload": {"_text": "the human's edit of our draft"}},
    ]}


# `desk_agents` imports gapi by name, so it holds its own reference — patching
# only email_desks.gapi would leave the agent talking to the real one.
_orig_agent_gapi = A.gapi
A.gapi = _thread_with_sends
check("only a send that POSTDATES the draft can be the human's edit of it",
      A._our_latest_send("t", "th", 2000) == "the human's edit of our draft")
check("with nothing after the draft, there is no lesson to learn",
      A._our_latest_send("t", "th", 5000) == "")
A.gapi = _orig_agent_gapi


# ── beacons ──────────────────────────────────────────────────────────────
print("beacons:")
long_desk = "a-very-long-desk-address@an-extremely-long-domain-name.example.com"
check("the beacon key is per (agent, desk), not one shared key",
      D.beacon_key("responder", "a@b.c") != D.beacon_key("followup", "a@b.c"))
check("and it fits config.name (VARCHAR(80))",
      len(D.beacon_key("responder", long_desk)) <= 80)
_WRITES.clear()
D.stamp_beacon("responder", "support@acme.example", note="quiet", drafted=0)
payload = json.loads(_WRITES[-1][1][1])
check("a quiet run still stamps proof of life",
      payload["drafted"] == 0 and payload["agent"] == "responder")
check("the timestamp is stored in the value, not inferred from updated_at",
      "at" in payload)


# ── the lesson store ─────────────────────────────────────────────────────
print("lesson store:")
import tempfile                                                  # noqa: E402
_tmp = tempfile.mkdtemp()
_CFG["DESK_LESSONS_DIR"] = _tmp
for i in range(12):
    D.append_lesson("support@acme.example", "responder", f"lesson {i}", keep=5)
text = D.load_lessons("support@acme.example", "responder")
bullets = [ln for ln in text.splitlines() if ln.startswith("- ")]
check("the file is bounded (it is read back into a prompt every run)",
      len(bullets) == 5)
check("and it keeps the FRESHEST lessons", "lesson 11" in text)
check("an empty lesson is not written",
      D.append_lesson("support@acme.example", "responder", "   ") is False)
check("a missing lesson file reads as empty, never as an error",
      D.load_lessons("nobody@nowhere.example", "responder") == "")
_CFG.pop("DESK_LESSONS_DIR")


# ── operator configuration ───────────────────────────────────────────────
print("configuration:")
check("the playbook ships EMPTY — an unconfigured install must not invent facts",
      A.playbook() == "")
check("categories ship with a usable default", len(A.categories()) >= 5)
check("only some categories earn an automatic draft",
      A.replyable() < set(A.categories()))
_CFG["DESK_SIGNOFF"] = "{product} Support Team"
check("the signoff resolves per desk", A.signoff_for("Acme") == "Acme Support Team")
_CFG.pop("DESK_SIGNOFF")
for raw, want in (("0", False), ("false", False), ("off", False),
                  ("", False), ("1", True), ("yes", True)):
    os.environ["_DESK_TEST_FLAG"] = raw
    check(f"envflag({raw!r}) -> {want}",
          D.envflag("_DESK_TEST_FLAG", False) is want)
os.environ.pop("_DESK_TEST_FLAG", None)


# ── the digest ───────────────────────────────────────────────────────────
# A digest that cannot tell "nothing happened" from "nothing ran" will
# eventually report a confident all-clear over a dead pipeline.
print("digest:")

_DESKS = [{"mailbox": "support@acme.example", "product": "Acme",
           "owner": "staff@acme.example"}]


def _digest(granted, ages, rows=None):
    """Build a digest with the given grants, beacon ages and pending rows."""
    D.agent_enabled = lambda mb, a, default=False: a in granted
    D.beacon_ages = lambda: ages
    A.agent_enabled = D.agent_enabled
    A.beacon_ages = D.beacon_ages
    A.desk_activity = lambda mbs, lookback_hours=24: {
        m: {"rescued": 1, "drafted": 2, "nudged": 3, "lessons": 4} for m in mbs}
    A.pending_drafts = lambda mbs, limit=200: (rows or [])
    return A.build_desk_digest(_DESKS)


_real_enabled, _real_ages = D.agent_enabled, D.beacon_ages
_real_activity, _real_pending = A.desk_activity, A.pending_drafts

out = _digest(granted=[], ages={})[0][0]
check("with NOTHING granted it says so — never an all-clear",
      "nothing is granted" in out and "✅" not in out)

out = _digest(granted=["responder"], ages={})[0][0]
check("a granted agent that has NEVER run is reported as not reporting",
      "NOT REPORTING" in out and "never" in out)
check("...and no all-clear is emitted alongside that warning", "✅" not in out)

out = _digest(granted=["responder"],
              ages={("responder", "support@acme.example"): 99.0})[0][0]
check("a STALE beacon is reported as not reporting", "NOT REPORTING" in out)

out = _digest(granted=["responder"],
              ages={("responder", "support@acme.example"): 1.0})[0][0]
check("a fresh beacon with no pending drafts gives a real all-clear",
      "✅ No drafts waiting" in out and "NOT REPORTING" not in out)
check("...and the activity roll-up is included",
      "1 rescued from spam" in out and "2 replies drafted" in out)

out = _digest(granted=["responder", "learning"],
              ages={("responder", "support@acme.example"): 1.0})[0][0]
check("one silent agent on an otherwise healthy desk is still named",
      "NOT REPORTING" in out and "learning" in out)

rows = [{"mailbox": "support@acme.example", "subject": f"Question {i}",
         "customer_email": f"buyer{i}@outside.example", "category": "sales_inquiry",
         "thread_id": f"th{i}"} for i in range(60)]
chunks = _digest(granted=["responder"],
                 ages={("responder", "support@acme.example"): 1.0}, rows=rows)
check("a big day chunks rather than emitting one oversized message",
      len(chunks) > 1)
check("...and every chunk respects the transport limit",
      all(len(c[0]) <= 3500 + 400 for c in chunks))
check("...and every pending draft appears exactly once across the chunks",
      sum(c[0].count("outside.example") for c in chunks) == 60)
check("the deep link pins the mailbox with authuser",
      "authuser=support@acme.example" in chunks[0][0])

D.agent_enabled, D.beacon_ages = _real_enabled, _real_ages
A.agent_enabled, A.beacon_ages = _real_enabled, _real_ages
A.desk_activity, A.pending_drafts = _real_activity, _real_pending


print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
if FAIL:
    for f in FAIL:
        print("  FAILED: " + f)
    sys.exit(1)
print("desk-agent invariants hold.")
