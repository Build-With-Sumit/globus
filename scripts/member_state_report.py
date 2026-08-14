#!/usr/bin/env python3
"""Is Globus actually working for each member?

Usage:
  python3 scripts/member_state_report.py                 # every active member
  python3 scripts/member_state_report.py you@example.com  # one member, detailed
  python3 scripts/member_state_report.py --json           # machine-readable

Exit codes:
  0  every member is ready, or their only gaps are theirs to close
  1  at least one thing is waiting on YOU (the operator)
  2  the state could not be read at all

WHAT THE STAGES MEAN
--------------------
  ready          data has arrived and been processed
  ingesting      data arrived, the vault has not finished building
  connected      connected, nothing has come through yet
  not_connected  available here, this member has not wired it up
  unavailable    implemented, but not configured on THIS install
  error          connected and failing
  unknown        we could not check — never rendered as a zero

`unavailable` is the one people skip past, and it is the one that matters most:
it means the capability exists in the code but this deployment never got the
credential for it. Reporting that as "the member hasn't connected Drive" blames
someone for an omission that is yours, and points them at a button that cannot
work.
"""
from __future__ import annotations
import json
import os
import sys

_ICON = {
    "ready": "ok  ", "ingesting": "... ", "connected": "... ",
    "not_connected": "--  ", "unavailable": "n/a ", "error": "FAIL",
    "unknown": "??  ",
}


def _load_env(path):
    if not os.path.isfile(path):
        return
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def _boot():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    _load_env(os.path.join(root, ".env"))
    sys.path.insert(0, os.path.join(root, "server"))
    import db_helpers
    db_helpers.configure(db_cfg={
        "host":     os.environ.get("DB_HOST", "127.0.0.1"),
        "port":     int(os.environ.get("DB_PORT", "3306")),
        "user":     os.environ.get("DB_USER", "globus"),
        "password": os.environ.get("DB_PASSWORD", ""),
        "database": os.environ.get("DB_NAME", "globus"),
    })


def _detail(check):
    bits = [f"{k}={v}" for k, v in check.items()
            if k not in ("stage", "available", "detail") and v not in (None, 0, "")]
    if check.get("detail"):
        bits.append(str(check["detail"]))
    return "  ".join(bits)


def main():
    argv = [a for a in sys.argv[1:] if a != "--json"]
    as_json = "--json" in sys.argv[1:]
    _boot()
    import member_state as MS

    try:
        caps = MS.install_capabilities()
        if argv:
            states = [MS.member_state(argv[0].strip().lower(), partial=False)]
        else:
            states = MS.roster()
    except MS.StateUnavailable as e:
        print(f"could not read member state: {e}", file=sys.stderr)
        return 2

    if as_json:
        print(json.dumps({"capabilities": caps, "members": states},
                         indent=2, default=str))
        return 0

    missing = [k for k, v in caps.items() if not v]
    if missing:
        # Printed FIRST and once, rather than repeated per member. These are
        # the operator's own gaps, and seeing them eight times next to eight
        # members' names reads as eight problems instead of one.
        print("Not configured on this install (implemented, but no credential "
              "here):\n  " + ", ".join(sorted(missing)) + "\n")

    operator_blocked = False
    for state in states:
        print(f"{state['stage'].upper():<14} {state['email']}")
        for name, check in state["checks"].items():
            stage = check.get("stage")
            if stage == "unavailable":
                continue                    # already said once, above
            detail = _detail(check)
            print(f"   {_ICON.get(stage, '?')} {name:<14} {stage:<14}"
                  + (f"  {detail}" if detail else ""))
        for b in MS.blockers(state):
            if b["owner"] == "operator" and b["action"] != "not configured on this install":
                operator_blocked = True
                print(f"   -> YOU: {b['capability']} {b['action']}")
        print()

    ready = sum(1 for s in states if s["stage"] == "ready")
    print(f"{ready}/{len(states)} member(s) fully ready")
    return 1 if operator_blocked else 0


if __name__ == "__main__":
    sys.exit(main())
