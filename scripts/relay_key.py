#!/usr/bin/env python3
"""Operator CLI for notification-relay keys (POST /api/notify).

Usage:
  python3 scripts/relay_key.py create <service> <member-email>
        [--chat 123 --chat -100456 ...] [--expires "2027-01-01 00:00:00"]
        [--label rotation-2]
  python3 scripts/relay_key.py list [<member-email>]
  python3 scripts/relay_key.py revoke --id <key-id>
  python3 scripts/relay_key.py revoke --service <service>

`create` prints the key ONCE; only its SHA-256 is stored, so a lost key is
revoked and replaced, never recovered. Hand it to the service out-of-band and
never commit it. The member must have an active Telegram bot with the target
chat in its `allowed_send_chats` (Members > Telegram bot); a --chat scope on
the key narrows that further.

Exit codes:
  0  success
  1  bad arguments / config error
  2  DB error
"""
from __future__ import annotations
import argparse
import os
import re
import sys


EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


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


def main(argv=None):
    ap = argparse.ArgumentParser(description="Manage notification-relay keys.")
    sub = ap.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("create", help="mint a key (printed once)")
    c.add_argument("service", help="service name, e.g. monitoring")
    c.add_argument("member", help="member email whose Telegram bot sends")
    c.add_argument("--chat", action="append", default=None,
                   help="restrict the key to this chat_id (repeatable)")
    c.add_argument("--expires", default=None,
                   help="UTC expiry, 'YYYY-MM-DD HH:MM:SS'")
    c.add_argument("--label", default=None, help="tag for key rotation")

    ls = sub.add_parser("list", help="show keys (never the key itself)")
    ls.add_argument("member", nargs="?", default=None)

    r = sub.add_parser("revoke", help="revoke one key or a whole service")
    g = r.add_mutually_exclusive_group(required=True)
    g.add_argument("--id", type=int, dest="key_id")
    g.add_argument("--service", dest="service")

    args = ap.parse_args(argv)

    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    _load_env(os.path.join(repo_root, ".env"))
    sys.path.insert(0, os.path.join(repo_root, "server"))

    import db_helpers
    try:
        db_helpers.configure(db_cfg=db_helpers.db_cfg_from_env())
    except Exception as e:
        print(f"relay_key: DB config error: {type(e).__name__}: {e}",
              file=sys.stderr)
        return 1
    import globus_relay

    try:
        if args.cmd == "create":
            member = args.member.strip().lower()
            if not EMAIL_RE.match(member):
                print(f"relay_key: {member!r} doesn't look like an email",
                      file=sys.stderr)
                return 1
            if not (args.service or "").strip():
                print("relay_key: service name is required", file=sys.stderr)
                return 1
            key = globus_relay.create_relay_key(
                args.service.strip(), member, allowed_chats=args.chat,
                expires_at=args.expires, label=args.label)
            print(key)
            print("relay_key: shown once; only its hash is stored.",
                  file=sys.stderr)
            return 0
        if args.cmd == "list":
            member = args.member.strip().lower() if args.member else None
            for row in globus_relay.list_relay_keys(member):
                print(f"{row['id']:>4}  {row['status']:<8} "
                      f"{row['service_name']:<20} {row['member_email']:<30} "
                      f"label={row.get('label') or '-'} "
                      f"chats={row.get('allowed_chats') or 'bot-default'} "
                      f"expires={row.get('expires_at') or 'never'} "
                      f"last_used={row.get('last_used_at') or 'never'}")
            return 0
        if args.cmd == "revoke":
            globus_relay.revoke_relay_key(service_name=args.service,
                                          key_id=args.key_id)
            print("relay_key: revoked")
            return 0
    except Exception as e:
        print(f"relay_key: failed: {type(e).__name__}: {e}", file=sys.stderr)
        return 2
    return 1


if __name__ == "__main__":
    sys.exit(main())
