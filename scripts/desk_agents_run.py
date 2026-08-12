#!/usr/bin/env python3
"""Cron entry point for the shared-inbox desk agents.

Usage:
  python3 scripts/desk_agents_run.py rescue    [<mailbox>]
  python3 scripts/desk_agents_run.py respond   [<mailbox>]
  python3 scripts/desk_agents_run.py followup  [<mailbox>]
  python3 scripts/desk_agents_run.py learn     [<mailbox>]
  python3 scripts/desk_agents_run.py digest            # roll up + notify
  python3 scripts/desk_agents_run.py desks             # list what is configured
  python3 scripts/desk_agents_run.py grant <mailbox> <agent> [on|off]

With no mailbox, an agent runs across every configured desk. Naming one scopes
the run to it — which is how you try an agent on a single desk before granting
it more widely.

Exit codes:
  0  ran (including "nothing to do" and "not granted")
  1  bad usage / nothing configured
  2  the run failed

NOTHING RUNS UNTIL IT IS GRANTED. Every agent is off for every desk until
someone switches it on with `grant`. Discovery is live, so an install that
defaulted to on would start working mailboxes the moment an unrelated account
was connected — and the operator would learn about it from the invoice.

Set DESK_DRYRUN=1 to classify, compose and print without touching a mailbox:
no drafts, no moves, no rows, and no heartbeat. A dry run must never forge
proof of life, or the beacon starts reporting a pipeline that has not run.

Example crontab — one desk per line keeps a dead credential on one desk from
taking the others down, and staggers the load:

  # Rescue business mail out of Spam, twice an hour.
  5,35 * * * *  cd /opt/globus && flock -n /tmp/desk-rescue.lock \\
      .venv/bin/python3 scripts/desk_agents_run.py rescue \\
      >> /var/log/globus-desk-agents.log 2>&1

  # Draft replies to people waiting on us, every 30 minutes.
  15,45 * * * * cd /opt/globus && flock -n /tmp/desk-respond.lock \\
      .venv/bin/python3 scripts/desk_agents_run.py respond \\
      >> /var/log/globus-desk-agents.log 2>&1

  # Nudge quiet threads once a day, in the morning.
  20 4 * * *    cd /opt/globus && flock -n /tmp/desk-followup.lock \\
      .venv/bin/python3 scripts/desk_agents_run.py followup \\
      >> /var/log/globus-desk-agents.log 2>&1

  # Learn from what the humans did, once a night, after they have acted.
  40 21 * * *   cd /opt/globus && flock -n /tmp/desk-learn.lock \\
      .venv/bin/python3 scripts/desk_agents_run.py learn \\
      >> /var/log/globus-desk-agents.log 2>&1

  # One roll-up a day: what happened, and what is still waiting on a human.
  0 3 * * *     cd /opt/globus && .venv/bin/python3 \\
      scripts/desk_agents_run.py digest \\
      >> /var/log/globus-desk-agents.log 2>&1
"""
from __future__ import annotations
import json
import os
import sys

MODES = {"rescue": "spam_rescue", "respond": "responder",
         "followup": "followup", "learn": "learning"}


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
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    _load_env(os.path.join(repo_root, ".env"))
    sys.path.insert(0, os.path.join(repo_root, "server"))
    import db_helpers
    db_helpers.configure(db_cfg={
        "host":     os.environ.get("DB_HOST", "127.0.0.1"),
        "port":     int(os.environ.get("DB_PORT", "3306")),
        "user":     os.environ.get("DB_USER", "globus"),
        "password": os.environ.get("DB_PASSWORD", ""),
        "database": os.environ.get("DB_NAME", "globus"),
    })


def _desks(scope):
    import email_desks as D
    desks = D.configured_desks()
    if scope:
        desks = [d for d in desks if d["mailbox"].lower() == scope]
    return desks


def _notify(text):
    """Deliver one digest chunk. Returns True only on a CONFIRMED send.

    Falls back to stdout when no chat transport is configured — cron captures
    that to the log, which is honest. It must never return True for a delivery
    that did not happen."""
    from db_helpers import cfg
    chat_id = cfg("DESK_TELEGRAM_CHAT_ID", "") or ""
    owner = cfg("DESK_TELEGRAM_MEMBER", "") or ""
    if chat_id and owner:
        try:
            from telegram_bot import send_via_member_bot
            res = send_via_member_bot(owner, chat_id, text,
                                      initiator="desk-agents") or {}
            if not res.get("ok"):
                print(f"[desk-agents] telegram send failed: {res.get('error')}",
                      flush=True)
            return bool(res.get("ok"))
        except Exception as e:
            print(f"[desk-agents] telegram send error: "
                  f"{type(e).__name__}: {e}", flush=True)
            return False
    print(text, flush=True)
    return True


def main():
    argv = sys.argv[1:]
    if not argv or argv[0] not in (*MODES, "desks", "grant", "digest"):
        print("usage: desk_agents_run.py rescue|respond|followup|learn "
              "[<mailbox>] | digest | desks | grant <mailbox> <agent> [on|off]",
              file=sys.stderr)
        return 1
    mode = argv[0]
    _boot()
    import email_desks as D
    import desk_agents as A

    if mode == "digest":
        desks = D.configured_desks()
        if not desks:
            print("No desks configured — nothing to report on.", file=sys.stderr)
            return 1
        try:
            chunks = A.build_desk_digest(
                desks,
                lookback_hours=int(os.environ.get("DESK_DIGEST_HOURS", "24")),
                stale_hours=int(os.environ.get("DESK_STALE_HOURS", "26")))
        except Exception as e:
            print(f"[desk-agents] digest build failed: {type(e).__name__}: {e}",
                  file=sys.stderr)
            return 2
        for (text,) in chunks:
            if D.envflag("DESK_DRYRUN", False):
                print(text, flush=True)
            else:
                _notify(text)
        return 0

    if mode == "desks":
        desks = D.configured_desks()
        if not desks:
            print("No desks configured. Set DESK_OWNERS (staff whose connected "
                  "mailboxes are desks) or DESK_MAILBOXES (an explicit list).",
                  file=sys.stderr)
            return 1
        ages = D.beacon_ages()
        for d in desks:
            grants = [a for a in D.DESK_AGENTS if D.agent_enabled(d["mailbox"], a)]
            print(f"{d['mailbox']:<38} {d['product']:<18} owner={d['owner']}")
            for a in D.DESK_AGENTS:
                age = ages.get((a, d["mailbox"]))
                when = "never run" if age is None else f"{age:.1f}h ago"
                state = "ON " if a in grants else "off"
                print(f"    {state} {a:<14} {when}")
        return 0

    if mode == "grant":
        if len(argv) < 3:
            print("usage: desk_agents_run.py grant <mailbox> <agent> [on|off]",
                  file=sys.stderr)
            return 1
        mailbox, agent = argv[1].strip().lower(), argv[2].strip()
        if agent not in D.DESK_AGENTS:
            print(f"unknown agent {agent!r}; one of: {', '.join(D.DESK_AGENTS)}",
                  file=sys.stderr)
            return 1
        on = (argv[3].lower() not in ("off", "0", "false")) if len(argv) > 3 else True
        D.grant(mailbox, agent, enabled=on)
        print(f"{agent} {'enabled' if on else 'disabled'} for {mailbox}")
        return 0

    scope = argv[1].strip().lower() if len(argv) > 1 else ""
    desks = _desks(scope)
    if not desks:
        print(f"No desks matched{' ' + scope if scope else ''}. "
              f"Run `desk_agents_run.py desks` to see what is configured.",
              file=sys.stderr)
        return 1

    dry = D.envflag("DESK_DRYRUN", False)
    run = A.AGENTS[MODES[mode]]
    failed = False
    for desk in desks:
        try:
            stats = run(desk, dry_run=dry)
            print(f"[{mode}] {desk['mailbox']}: {json.dumps(stats, default=str)}",
                  flush=True)
        except RuntimeError as e:
            # A dead or revoked credential. Deliberately NOT fatal to the loop:
            # one broken desk must not stop the others, and the missing beacon
            # is what surfaces it rather than this message scrolling past.
            print(f"[{mode}] {desk['mailbox']}: {e}", file=sys.stderr)
            failed = True
        except Exception as e:
            print(f"[{mode}] {desk['mailbox']} failed: {type(e).__name__}: {e}",
                  file=sys.stderr)
            failed = True
    return 2 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
