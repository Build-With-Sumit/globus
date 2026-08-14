"""Behavioural tests for per-member installation state.

The module exists to keep four things apart that a boolean would merge, so that
is what these pin down:

  * IMPLEMENTED vs AVAILABLE — a capability the operator never configured reads
    as `unavailable`, never as "the member hasn't connected it", because the fix
    and the person who has to make it are different,
  * CONNECTED vs WORKING — a revoked or erroring connection is `error`, not
    `ready`, and a connection with nothing through it yet is `connected`,
  * a ZERO vs a FAILED READ — db_read returns None on failure and empty on
    success, and `or []` merges them. With the database down every member would
    otherwise be reported as brand new with nothing connected, which is the one
    output a health surface must never produce,
  * WHOSE problem it is — blockers are attributed to the operator or the member.

Hermetic: db_helpers is stubbed in sys.modules. No MySQL.
Run with:  python tests/test_member_state.py
"""
import os
import sys
import types

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(_HERE), "server"))

_CFG, _ROWS = {}, []
_FAIL_MATCHING = []          # SQL fragments whose reads should return None


def _db_read(sql, params=()):
    for frag in _FAIL_MATCHING:
        if frag in sql:
            return None                     # fail-soft failure, as in prod
    for matcher, result in _ROWS:
        if matcher in sql:
            return result(params) if callable(result) else result
    return []


_dbh = types.ModuleType("db_helpers")
_dbh.db_read = _db_read
_dbh.db_write = lambda sql, params=(): True
_dbh.cfg = lambda k, d="": _CFG.get(k, d)
sys.modules["db_helpers"] = _dbh

import member_state as MS            # noqa: E402

PASS, FAIL = [], []


def check(label, ok):
    (PASS if ok else FAIL).append(label)
    print(("  ok   " if ok else "  FAIL ") + label)


def reset():
    _ROWS.clear()
    _FAIL_MATCHING.clear()
    _CFG.clear()


def google_configured():
    _CFG["GOOGLE_OAUTH_CLIENT_ID"] = "id"
    _CFG["GOOGLE_OAUTH_CLIENT_SECRET"] = "secret"


# ── implemented vs available ─────────────────────────────────────────────
print("implemented vs available:")
reset()
caps = MS.install_capabilities()
check("with no Google credentials, Drive is NOT available on this install",
      caps["google_drive"] is False and caps["gmail"] is False)
check("the vault and digest need no third-party credential, so they are always "
      "available", caps["vault"] and caps["digest"])
google_configured()
check("configuring the client makes Drive available",
      MS.install_capabilities()["google_drive"] is True)

reset()
state = MS.member_state("a@x.example")
check("an unconfigured capability reads 'unavailable', NOT 'not_connected'",
      state["checks"]["google_drive"]["stage"] == MS.STAGE_UNAVAILABLE)
check("...and that is attributed to the OPERATOR, not the member",
      any(b["capability"] == "google_drive" and b["owner"] == "operator"
          for b in MS.blockers(state)))

reset()
google_configured()
state = MS.member_state("a@x.example")
check("configured but unconnected reads 'not_connected'",
      state["checks"]["google_drive"]["stage"] == MS.STAGE_NOT_CONNECTED)
check("...and THAT is the member's to close",
      any(b["capability"] == "google_drive" and b["owner"] == "member"
          for b in MS.blockers(state)))


# ── connected vs working ─────────────────────────────────────────────────
print("\nconnected vs working:")
reset()
google_configured()
_ROWS.append(("globus_oauth_connections",
              [{"sync_status": "idle", "needs_reconnect": 0,
                "last_synced_at": None, "last_sync_error": None,
                "source_types": "drive", "drive_files": 0, "gmail_files": 0}]))
check("connected with nothing through it is 'connected', not 'ready'",
      MS.member_state("a@x.example")["checks"]["google_drive"]["stage"]
      == MS.STAGE_CONNECTED)

reset()
google_configured()
_ROWS.append(("globus_oauth_connections",
              [{"sync_status": "idle", "needs_reconnect": 0,
                "last_synced_at": "2026-08-14", "last_sync_error": None,
                "source_types": "drive", "drive_files": 42, "gmail_files": 0}]))
check("connected with files through it is 'ready'",
      MS.member_state("a@x.example")["checks"]["google_drive"]["stage"]
      == MS.STAGE_READY)

reset()
google_configured()
_ROWS.append(("globus_oauth_connections",
              [{"sync_status": "error", "needs_reconnect": 1,
                "last_synced_at": None, "last_sync_error": "invalid_grant",
                "source_types": "drive", "drive_files": 99, "gmail_files": 0}]))
state = MS.member_state("a@x.example")
check("a revoked connection is 'error' even though it once synced files",
      state["checks"]["google_drive"]["stage"] == MS.STAGE_ERROR)
check("...and the reason is carried, not just the state",
      "invalid_grant" in str(state["checks"]["google_drive"].get("detail")))
check("a broken capability makes the member's headline 'error'",
      state["stage"] == MS.STAGE_ERROR)


# ── the vault is only ready when it is BUILT ─────────────────────────────
print("\nvault build:")
reset()
_ROWS.append(("globus_vault_files",
              [{"files": 100, "processed": 0, "skipped": 0}]))
check("files ingested but none processed is 'ingesting'",
      MS.member_state("a@x.example")["checks"]["vault"]["stage"]
      == MS.STAGE_INGESTING)

reset()
_ROWS.append(("globus_vault_files",
              [{"files": 100, "processed": 40, "skipped": 0}]))
check("a PARTIALLY built vault is still 'ingesting' — it answers confidently "
      "from the part it has", MS.member_state("a@x.example")["checks"]["vault"]["stage"]
      == MS.STAGE_INGESTING)

reset()
_ROWS.append(("globus_vault_files",
              [{"files": 100, "processed": 90, "skipped": 10}]))
check("skipped files do not block 'ready' (they are never processable)",
      MS.member_state("a@x.example")["checks"]["vault"]["stage"]
      == MS.STAGE_READY)


# ── a digest that exists but is empty is broken, not ready ───────────────
print("\ndigest:")
reset()
_ROWS.append(("globus_intelligence",
              [{"digest_char_count": 40000, "built_at": "2026-08-14",
                "built_with": "sonnet"}]))
check("a real digest is 'ready'",
      MS.member_state("a@x.example")["checks"]["digest"]["stage"]
      == MS.STAGE_READY)

reset()
_ROWS.append(("globus_intelligence",
              [{"digest_char_count": 120, "built_at": "2026-08-14",
                "built_with": "sonnet"}]))
check("an implausibly short digest is 'error' — that is the shape of a model "
      "refusal persisted as a member's brain",
      MS.member_state("a@x.example")["checks"]["digest"]["stage"]
      == MS.STAGE_ERROR)


# ── a zero must never come from a failed read ────────────────────────────
print("\nreading a zero honestly:")
reset()
_FAIL_MATCHING.append("globus_vault_files")
raised = False
try:
    MS.member_state("a@x.example")
except MS.StateUnavailable:
    raised = True
check("an unreadable check RAISES rather than reporting an empty vault", raised)

state = MS.member_state("a@x.example", partial=True)
check("partial=True degrades that check to 'unknown', never to a zero",
      state["checks"]["vault"]["stage"] == MS.STAGE_UNKNOWN)
check("...and 'unknown' anywhere makes the headline 'unknown', not 'ready'",
      state["stage"] == MS.STAGE_UNKNOWN)
check("...and it is reported as the OPERATOR's to look at",
      any(b["owner"] == "operator" and "could not" in b["action"]
          for b in MS.blockers(state)))
check("a failed read is never silently a 'not_connected'",
      state["checks"]["vault"]["stage"] != MS.STAGE_NOT_CONNECTED)


# ── the overall stage ────────────────────────────────────────────────────
print("\noverall stage:")
check("unknown beats everything (we cannot claim a state we did not read)",
      MS.overall_stage({"a": {"stage": MS.STAGE_READY},
                        "b": {"stage": MS.STAGE_UNKNOWN}}) == MS.STAGE_UNKNOWN)
check("error beats merely-incomplete",
      MS.overall_stage({"a": {"stage": MS.STAGE_NOT_CONNECTED},
                        "b": {"stage": MS.STAGE_ERROR}}) == MS.STAGE_ERROR)
check("a member is only as ready as their weakest connected capability",
      MS.overall_stage({"a": {"stage": MS.STAGE_READY},
                        "b": {"stage": MS.STAGE_INGESTING}}) == MS.STAGE_INGESTING)
check("an UNAVAILABLE capability does not drag the headline down — the operator "
      "chose not to offer it",
      MS.overall_stage({"a": {"stage": MS.STAGE_READY},
                        "b": {"stage": MS.STAGE_UNAVAILABLE}}) == MS.STAGE_READY)
check("all-unavailable is honestly 'unavailable', not 'ready'",
      MS.overall_stage({"a": {"stage": MS.STAGE_UNAVAILABLE}})
      == MS.STAGE_UNAVAILABLE)


# ── bridges ──────────────────────────────────────────────────────────────
print("\nbridges:")
reset()
check("no bridge key -> the bridges are unavailable, not unconnected",
      MS.member_state("a@x.example")["checks"]["whatsapp"]["stage"]
      == MS.STAGE_UNAVAILABLE)
reset()
_CFG["GLOBUS_BRIDGE_KEY"] = "k"
_ROWS.append(("globus_whatsapp_messages", [{"c": 12}]))
check("messages present -> ready",
      MS.member_state("a@x.example")["checks"]["whatsapp"]["stage"]
      == MS.STAGE_READY)


print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
if FAIL:
    for f in FAIL:
        print("  FAILED: " + f)
    sys.exit(1)
print("member-state invariants hold.")
