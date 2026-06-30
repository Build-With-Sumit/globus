#!/usr/bin/env python3
"""CLI to add (or reactivate) a member without writing SQL.

Usage:
  python3 scripts/add_member.py <email>
  python3 scripts/add_member.py <email> --name="Jane Doe"
  python3 scripts/add_member.py <email> --status=comp

Idempotent: re-running with the same email is a no-op if the member
already has the requested status; bumps the row to 'active' otherwise.

Exit codes:
  0  success — member exists at the requested status
  1  bad arguments / config error
  2  DB error
"""
from __future__ import annotations
import argparse
import os
import re
import sys


EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
VALID_STATUS = ("active", "pending", "cancelled", "comp")


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


def main():
    ap = argparse.ArgumentParser(description="Add or reactivate a Globus member.")
    ap.add_argument("email", help="member email address (lowercased)")
    ap.add_argument("--name", default="",
                    help="optional first/last name; split on first space")
    ap.add_argument("--status", default="active", choices=VALID_STATUS,
                    help="member status (default: active)")
    args = ap.parse_args()

    email = args.email.strip().lower()
    if not EMAIL_RE.match(email):
        print(f"add_member: {email!r} doesn't look like an email",
              file=sys.stderr)
        sys.exit(1)

    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    _load_env(os.path.join(repo_root, ".env"))
    sys.path.insert(0, os.path.join(repo_root, "server"))

    import db_helpers
    try:
        db_helpers.configure(db_cfg={
            "host":     os.environ.get("DB_HOST", "127.0.0.1"),
            "port":     int(os.environ.get("DB_PORT", "3306")),
            "user":     os.environ.get("DB_USER", "globus"),
            "password": os.environ.get("DB_PASSWORD", ""),
            "database": os.environ.get("DB_NAME", "globus"),
        })
    except Exception as e:
        print(f"add_member: DB config error: {type(e).__name__}: {e}",
              file=sys.stderr)
        sys.exit(1)

    first, last = "", ""
    if args.name:
        parts = args.name.strip().split(None, 1)
        first = parts[0] if parts else ""
        last = parts[1] if len(parts) > 1 else ""

    from db_helpers import db_read, db_write
    try:
        existing = db_read("SELECT status FROM members WHERE email=%s",
                            (email,)) or []
        if existing:
            cur = existing[0]["status"]
            if cur == args.status:
                print(f"add_member: {email} already exists with "
                      f"status={cur} — no change")
                sys.exit(0)
            db_write(
                "UPDATE members SET status=%s, "
                "  first_name=COALESCE(NULLIF(%s,''), first_name), "
                "  last_name=COALESCE(NULLIF(%s,''), last_name) "
                "WHERE email=%s",
                (args.status, first, last, email))
            print(f"add_member: {email} updated {cur} -> {args.status}")
        else:
            db_write(
                "INSERT INTO members (email, first_name, last_name, status) "
                "VALUES (%s, %s, %s, %s)",
                (email, first, last, args.status))
            print(f"add_member: {email} created with status={args.status}")
    except Exception as e:
        print(f"add_member: DB write failed: {type(e).__name__}: {e}",
              file=sys.stderr)
        sys.exit(2)

    sys.exit(0)


if __name__ == "__main__":
    main()
