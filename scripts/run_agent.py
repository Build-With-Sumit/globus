#!/usr/bin/env python3
"""Cron-friendly agent runner — fires one agent for one member and exits.

Usage:
  python3 scripts/run_agent.py <agent_name> <member_email>

Exit codes:
  0  success — brief written
  1  agent not in catalog OR member not active
  2  agent run failed (LLM error, vault read error, etc.)

Example crontab line — fire research agent at 8 AM IST daily:
  30 2 * * *  cd /opt/globus && /opt/globus/.venv/bin/python3 \\
              scripts/run_agent.py research you@example.com \\
              >> /var/log/globus-agents.log 2>&1

Briefs land in $GLOBUS_AGENTS_WORK_DIR/<sha1(email)[:16]>/<agent>-<date>.md
(default /var/lib/globus/agents/<hash>/).
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


def main():
    if len(sys.argv) != 3:
        print("usage: run_agent.py <agent_name> <member_email>",
              file=sys.stderr)
        sys.exit(1)
    agent_name = sys.argv[1]
    email = sys.argv[2].strip().lower()

    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    _load_env(os.path.join(repo_root, ".env"))
    sys.path.insert(0, os.path.join(repo_root, "server"))

    # Configure the same way globus_server does — same boot order,
    # minus the HTTP server itself.
    import db_helpers
    db_helpers.configure(db_cfg={
        "host":     os.environ.get("DB_HOST", "127.0.0.1"),
        "port":     int(os.environ.get("DB_PORT", "3306")),
        "user":     os.environ.get("DB_USER", "globus"),
        "password": os.environ.get("DB_PASSWORD", ""),
        "database": os.environ.get("DB_NAME", "globus"),
    })

    # SESSION_SECRET only needed if the agent's tool loop touches
    # cookie/voice helpers — safe to wire anyway.
    secret_hex = os.environ.get("SESSION_SECRET", "")
    if secret_hex:
        session_secret = bytes.fromhex(secret_hex)
        import voice_helpers, globus_auth, bridge_ingest
        voice_helpers.configure(session_secret=session_secret)
        globus_auth.configure(session_secret=session_secret)
        bridge_ingest.configure(session_secret=session_secret)

    import voice_providers
    from db_helpers import cfg
    voice_providers.configure(
        deepseek_api_key_getter=lambda: (cfg("DEEPSEEK_API_KEY", "") or "").strip(),
        default_model=cfg("VOICE_DEFAULT_MODEL", "claude-sonnet-4-6"))

    from members_db import is_active_member
    import members_db
    from db_helpers import db_read, db_write
    members_db.configure(db_read=db_read, db_write=db_write)
    if not is_active_member(email):
        print(f"run_agent: {email} is not an active member", file=sys.stderr)
        sys.exit(1)

    from agent_runner import find_agent, run_agent_for_member
    if not find_agent(agent_name):
        print(f"run_agent: unknown agent {agent_name!r}", file=sys.stderr)
        sys.exit(1)

    result = run_agent_for_member(agent_name, email)
    if result.get("ok"):
        print(f"run_agent: {agent_name} for {email} ok — "
              f"{result.get('bytes_written',0):,} bytes -> "
              f"{result.get('brief_path','')}")
        sys.exit(0)
    print(f"run_agent: {agent_name} for {email} FAILED: "
          f"{result.get('error','')}", file=sys.stderr)
    sys.exit(2)


if __name__ == "__main__":
    main()
