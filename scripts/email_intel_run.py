#!/usr/bin/env python3
"""Cron entry point for the two-tier email intelligence agent.

Usage:
  python3 scripts/email_intel_run.py triage <mailbox>   # Tier 1 — label
  python3 scripts/email_intel_run.py reason <mailbox>   # Tier 2 — judge
  python3 scripts/email_intel_run.py digest             # roll up + notify

Exit codes:
  0  ran (including "nothing to do")
  1  bad usage / mailbox not connected
  2  the run failed

ONE PROCESS PER MAILBOX — deliberately. There is no loop over mailboxes here,
because a separate process per mailbox buys two things for free: a dead OAuth
token on one mailbox cannot take the others down, and each mailbox stamps its
OWN proof-of-life, so the digest can name exactly which one stopped.

Example crontab — note the per-mailbox lock and the staggered minutes:

  # Tier 1: cheap, every 30 min. Lookback is WIDER than the interval so a run
  # skipped by lock contention is recovered by the next one.
  0,30 * * * *  cd /opt/globus && flock -n /tmp/eintel-t1-a.lock \\
      .venv/bin/python3 scripts/email_intel_run.py triage you@example.com \\
      >> /var/log/globus-email-intel.log 2>&1

  # Tier 2: hourly, offset clear of Tier 1 so the grace window holds.
  20 * * * *    cd /opt/globus && flock -n /tmp/eintel-t2-a.lock \\
      .venv/bin/python3 scripts/email_intel_run.py reason you@example.com \\
      >> /var/log/globus-email-intel.log 2>&1

  # Digest: once a day.
  30 2 * * *    cd /opt/globus && .venv/bin/python3 \\
      scripts/email_intel_run.py digest \\
      >> /var/log/globus-email-intel.log 2>&1

Set EMAIL_INTEL_DRYRUN=1 to classify/judge and log without writing anything —
no labels, no rows, and no heartbeat (a dry run must never forge proof of life).
"""
from __future__ import annotations
import os
import sys


def _load_env(path):
    if not os.path.isfile(path):
        return
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(),
                                  v.strip().strip('"').strip("'"))


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


def _notify(text):
    """Deliver one digest chunk. Returns True only on a CONFIRMED send.

    Falls back to stdout when no chat transport is configured — cron captures
    it to the log, which is honest. It must NOT return True for a delivery that
    did not happen: rows are marked resolved on the strength of this, and a
    false success loses them silently."""
    from db_helpers import cfg
    chat_id = cfg("EMAIL_INTEL_TELEGRAM_CHAT_ID", "") or ""
    owner = cfg("EMAIL_INTEL_TELEGRAM_MEMBER", "") or ""
    if chat_id and owner:
        try:
            from telegram_bot import send_via_member_bot
            res = send_via_member_bot(owner, chat_id, text,
                                      initiator="email-intel") or {}
            if not res.get("ok"):
                print(f"[email-intel] telegram send failed: "
                      f"{res.get('error')}", flush=True)
            return bool(res.get("ok"))
        except Exception as e:
            print(f"[email-intel] telegram send error: "
                  f"{type(e).__name__}: {e}", flush=True)
            return False
    print(text, flush=True)
    return True


def main():
    argv = sys.argv[1:]
    if not argv or argv[0] not in ("triage", "reason", "digest"):
        print(__doc__.strip().splitlines()[2], file=sys.stderr)
        print("usage: email_intel_run.py triage|reason <mailbox> | digest",
              file=sys.stderr)
        return 1
    mode = argv[0]
    _boot()
    import email_intel as E

    dry = E.envflag("EMAIL_INTEL_DRYRUN", False)

    if mode in ("triage", "reason"):
        if len(argv) != 2:
            print(f"usage: email_intel_run.py {mode} <mailbox>",
                  file=sys.stderr)
            return 1
        mailbox = argv[1].strip().lower()
        try:
            if mode == "triage":
                lookback = int(os.environ.get("EMAIL_INTEL_TRIAGE_HOURS", "24"))
                E.triage_mailbox(mailbox, lookback_hours=lookback, dry_run=dry)
            else:
                E.reason_mailbox(mailbox, dry_run=dry)
        except RuntimeError as e:
            # Includes "not connected" and a revoked refresh token. Do NOT
            # stamp a heartbeat on the way out — a dead credential is exactly
            # what the digest's PIPELINE DOWN warning exists to surface.
            print(f"[email-intel] {mailbox}: {e}", file=sys.stderr)
            return 1
        except Exception as e:
            print(f"[email-intel] {mailbox} failed: {type(e).__name__}: {e}",
                  file=sys.stderr)
            return 2
        return 0

    # digest
    accounts = E.digest_accounts()
    if not accounts:
        print("[email-intel] EMAIL_INTEL_ACCOUNTS is empty — nothing to "
              "report on. Set it to the mailboxes the reasoner covers.",
              file=sys.stderr)
        return 1
    try:
        chunks = E.build_digest(
            accounts,
            lookback_hours=int(os.environ.get("EMAIL_INTEL_DIGEST_HOURS", "72")),
            stale_hours=int(os.environ.get("EMAIL_INTEL_STALE_HOURS", "26")))
    except Exception as e:
        print(f"[email-intel] digest build failed: {type(e).__name__}: {e}",
              file=sys.stderr)
        return 2

    for text, row_ids in chunks:
        if dry:
            print(text, flush=True)
            continue
        # Resolve per DELIVERED chunk, never per run: if chunk 3 fails, chunks
        # 1-2 stay delivered and only the remainder is retried tomorrow.
        if _notify(text) and row_ids:
            E.resolve_ids(row_ids)
    return 0


if __name__ == "__main__":
    sys.exit(main())
