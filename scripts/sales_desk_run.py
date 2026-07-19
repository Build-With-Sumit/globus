#!/usr/bin/env python3
"""Cron entry point for the sales desk — a daily ranked call list.

Usage:
  python3 scripts/sales_desk_run.py <member_email> [--post] [--no-llm]

  --post     deliver to the configured chat transport. WITHOUT it the desk
             only prints, so a first run can never surprise a team channel.
  --no-llm   deterministic ranking only (no model calls at all).

Exit codes:
  0  built (and delivered, if --post)
  1  bad usage, or no callable leads (a broken feed, reported loudly)
  2  the run failed

Example crontab — every weekday at 08:30 local:

  30 8 * * 1-5  cd /opt/globus && flock -n /tmp/sales-desk.lock \\
      .venv/bin/python3 scripts/sales_desk_run.py you@example.com --post \\
      >> /var/log/globus-sales-desk.log 2>&1

Every run stamps a beacon (config key `sales_desk_last_run`) whatever the
outcome, so "the desk stopped" is queryable rather than looking like a quiet
day. On failure it also DMs the operator — not the team channel: a stack trace
is an ops event, not a sales briefing.
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


def _send(target_key, text):
    """Deliver one chunk. Returns True only on a CONFIRMED send."""
    from db_helpers import cfg
    from telegram_bot import send_via_member_bot
    chat_id = cfg(target_key, "") or ""
    owner = cfg("SALES_DESK_TELEGRAM_MEMBER", "") or ""
    if not (chat_id and owner):
        print(text, flush=True)
        return True
    try:
        res = send_via_member_bot(owner, chat_id, text,
                                  initiator="sales-desk") or {}
        if not res.get("ok"):
            print(f"[sales-desk] send failed: {res.get('error')}", flush=True)
        return bool(res.get("ok"))
    except Exception as e:
        print(f"[sales-desk] send error: {type(e).__name__}: {e}", flush=True)
        return False


def main():
    args = [a for a in sys.argv[1:]]
    post = "--post" in args
    no_llm = "--no-llm" in args
    positional = [a for a in args if not a.startswith("--")]
    if len(positional) != 1:
        print("usage: sales_desk_run.py <member_email> [--post] [--no-llm]",
              file=sys.stderr)
        return 1
    member = positional[0].strip().lower()

    _boot()
    import sales_desk as S

    try:
        chunks, meta = S.run(member, use_llm=not no_llm)
    except RuntimeError as e:
        # Empty/broken feed — deliberately loud. Beacon it and tell the
        # operator; do NOT post a cheerful empty list to the team.
        S.stamp_beacon("empty", str(e))
        print(f"[sales-desk] {e}", file=sys.stderr)
        _send("SALES_DESK_OPS_CHAT_ID", f"⚠️ Sales desk did not run: {e}")
        return 1
    except Exception as e:
        S.stamp_beacon("error", f"{type(e).__name__}: {e}")
        print(f"[sales-desk] failed: {type(e).__name__}: {e}", file=sys.stderr)
        _send("SALES_DESK_OPS_CHAT_ID",
              f"🔴 Sales desk failed: {type(e).__name__}: {e}")
        return 2

    if meta["fell_back"]:
        print("[sales-desk] NOTE: ranking fell back to deterministic order",
              flush=True)

    if not post:
        for c in chunks:
            print(c, flush=True)
        S.stamp_beacon("preview", f"pool={meta['pool']}")
        print("[sales-desk] preview only — pass --post to deliver", flush=True)
        return 0

    delivered = 0
    for c in chunks:
        if _send("SALES_DESK_CHAT_ID", c):
            delivered += 1
    S.stamp_beacon("posted" if delivered == len(chunks) else "partial",
                   f"pool={meta['pool']} chunks={delivered}/{len(chunks)}"
                   + (" fallback" if meta["fell_back"] else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
