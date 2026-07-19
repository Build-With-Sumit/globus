#!/usr/bin/env python3
"""Opportunity tracker CLI — record what you sent, match what came back.

Usage:
  opportunity_run.py add <member> <slug> <org> [--title T] [--url U]
                                              [--domain D] [--source S]
  opportunity_run.py scan <member> <mailbox> [--days N] [--dry-run]
  opportunity_run.py report <member> [--days N]

`scan` reads YOUR connected mailbox, matches replies to open opportunities,
and advances their stage. It never sends anything and never modifies mail.

Exit codes: 0 ok · 1 bad usage / mailbox not connected · 2 run failed

Example crontab — check for replies each morning:

  0 7 * * *  cd /opt/globus && .venv/bin/python3 \\
      scripts/opportunity_run.py scan you@example.com you@example.com \\
      >> /var/log/globus-opportunities.log 2>&1
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


def _opt(args, name, default=""):
    if name in args:
        i = args.index(name)
        if i + 1 < len(args):
            return args[i + 1]
    return default


def _fetch_mail(mailbox, days):
    """Pull recent mail from a connected Gmail account, as tracker dicts."""
    from email_intel import mailbox_token
    from google_gmail import (gmail_list_messages, gmail_get_message,
                              gmail_headers, parse_email_date)
    token, _scopes = mailbox_token(mailbox)
    stubs = gmail_list_messages(token, f"newer_than:{int(days)}d",
                                max_results=400)
    out = []
    for st in stubs:
        try:
            msg = gmail_get_message(token, st["id"])
        except Exception:
            continue
        h = gmail_headers(msg.get("payload") or {})
        out.append({"id": st["id"], "from_email": h.get("From", ""),
                    "subject": h.get("Subject", ""),
                    "snippet": msg.get("snippet", ""),
                    "received_at": parse_email_date(h.get("Date"))})
    return out


def main():
    args = sys.argv[1:]
    if not args or args[0] not in ("add", "scan", "report"):
        print(__doc__.strip(), file=sys.stderr)
        return 1
    mode = args[0]
    _boot()
    import opportunity_tracker as T

    if mode == "add":
        pos = [a for a in args[1:] if not a.startswith("--")]
        # strip option VALUES out of the positional list
        for flag in ("--title", "--url", "--domain", "--source"):
            v = _opt(args, flag)
            if v in pos:
                pos.remove(v)
        if len(pos) < 3:
            print("usage: opportunity_run.py add <member> <slug> <org> "
                  "[--title T] [--url U] [--domain D] [--source S]",
                  file=sys.stderr)
            return 1
        member, slug, org = pos[0].strip().lower(), pos[1], " ".join(pos[2:])
        T.add_opportunity(member, slug, org,
                          title=_opt(args, "--title"),
                          url=_opt(args, "--url"),
                          domain=_opt(args, "--domain"),
                          source=_opt(args, "--source"))
        print(f"[opportunity] recorded {org} ({slug})")
        return 0

    if mode == "report":
        pos = [a for a in args[1:] if not a.startswith("--")]
        if len(pos) != 1:
            print("usage: opportunity_run.py report <member> [--days N]",
                  file=sys.stderr)
            return 1
        days = _opt(args, "--days")
        print(T.report_text(pos[0].strip().lower(),
                            int(days) if days.isdigit() else None))
        return 0

    # scan
    pos = [a for a in args[1:] if not a.startswith("--")]
    days_opt = _opt(args, "--days", "14")
    if days_opt in pos:
        pos.remove(days_opt)
    if len(pos) != 2:
        print("usage: opportunity_run.py scan <member> <mailbox> "
              "[--days N] [--dry-run]", file=sys.stderr)
        return 1
    member, mailbox = pos[0].strip().lower(), pos[1].strip().lower()
    dry = "--dry-run" in args or T.envflag("OPP_DRYRUN", False)
    try:
        msgs = _fetch_mail(mailbox, int(days_opt) if days_opt.isdigit() else 14)
    except RuntimeError as e:
        T.stamp_beacon("error", str(e))
        print(f"[opportunity] {mailbox}: {e}", file=sys.stderr)
        return 1
    except Exception as e:
        T.stamp_beacon("error", f"{type(e).__name__}: {e}")
        print(f"[opportunity] mail fetch failed: {type(e).__name__}: {e}",
              file=sys.stderr)
        return 2
    try:
        res = T.scan(member, msgs, dry_run=dry)
    except Exception as e:
        T.stamp_beacon("error", f"{type(e).__name__}: {e}")
        print(f"[opportunity] scan failed: {type(e).__name__}: {e}",
              file=sys.stderr)
        return 2
    if not dry:
        T.stamp_beacon("ok", f"matched={res['matched']} "
                             f"advanced={res['advanced']}")
    print(f"[opportunity] {res['messages']} msgs · matched {res['matched']} · "
          f"advanced {res['advanced']} · unmatched {res['unmatched']}"
          + (" (dry run)" if dry else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
