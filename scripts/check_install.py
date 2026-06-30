#!/usr/bin/env python3
"""Pre-flight install validator. Run BEFORE starting the server to
catch config / DB / storage problems early.

Usage:
  python3 scripts/check_install.py

Exit codes:
  0  all checks passed
  1  one or more checks failed (details printed to stderr)

What it checks (in order):
  1. .env loadable (or defaults are usable)
  2. Required env vars present (DB_PASSWORD, SESSION_SECRET)
  3. DB reachable + schema present (`SELECT 1`, `SHOW TABLES`)
  4. Schema has the expected tables (no migration drift)
  5. Storage paths writable (agents work dir + raw data cache)
  6. Fernet key parseable (if configured) + roundtrip works
  7. Persona file present (warn if still on the example)
  8. At least one active member exists (warns if none)
"""
from __future__ import annotations
import os
import sys


# ANSI colour codes — no deps, monochrome-fallback if no tty.
_COLOR = sys.stderr.isatty()
def _c(code, txt): return f"\033[{code}m{txt}\033[0m" if _COLOR else txt
RED = lambda s: _c("31", s)
GREEN = lambda s: _c("32", s)
YELLOW = lambda s: _c("33", s)
DIM = lambda s: _c("2", s)


def _load_env(path):
    if not os.path.isfile(path):
        return False
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(),
                                  v.strip().strip('"').strip("'"))
    return True


def _check(name, fn):
    """Run a check; print OK / FAIL / WARN; return (ok, fatal_failure)."""
    try:
        result = fn()
    except Exception as e:
        print(f"{RED('FAIL')}  {name}: {type(e).__name__}: {e}",
              file=sys.stderr)
        return False, True
    if isinstance(result, tuple):
        ok, msg = result
    else:
        ok, msg = bool(result), None
    if ok is True:
        if msg:
            print(f"{GREEN('OK  ')}  {name}: {DIM(msg)}")
        else:
            print(f"{GREEN('OK  ')}  {name}")
        return True, False
    if ok == "warn":
        print(f"{YELLOW('WARN')}  {name}: {msg or '(no detail)'}")
        return True, False
    print(f"{RED('FAIL')}  {name}: {msg or '(no detail)'}", file=sys.stderr)
    return False, True


def main():
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    print(f"checking install at {repo_root}", file=sys.stderr)

    any_fatal = False

    # 1. .env loadable
    env_path = os.path.join(repo_root, ".env")
    ok, fatal = _check(".env loadable", lambda: (
        (True, env_path) if _load_env(env_path)
        else ("warn", f"no .env at {env_path} — using process env + defaults")))
    any_fatal |= fatal

    # 2. Required env vars
    def _need(*keys):
        miss = [k for k in keys if not os.environ.get(k)
                or os.environ.get(k, "").startswith("replace-with")]
        if miss:
            return False, f"missing/placeholder: {', '.join(miss)}"
        return True, f"all set ({', '.join(keys)})"
    ok, fatal = _check("required env vars",
                       lambda: _need("DB_PASSWORD", "SESSION_SECRET"))
    any_fatal |= fatal

    # 3. DB reachable
    sys.path.insert(0, os.path.join(repo_root, "server"))
    try:
        import db_helpers
        db_helpers.configure(db_cfg={
            "host":     os.environ.get("DB_HOST", "127.0.0.1"),
            "port":     int(os.environ.get("DB_PORT", "3306")),
            "user":     os.environ.get("DB_USER", "globus"),
            "password": os.environ.get("DB_PASSWORD", ""),
            "database": os.environ.get("DB_NAME", "globus"),
        })
        from db_helpers import db_read, cfg
    except Exception as e:
        print(f"{RED('FAIL')}  db_helpers import: {e}", file=sys.stderr)
        sys.exit(1)
    ok, fatal = _check("DB reachable", lambda: (
        bool(db_read("SELECT 1 AS one")),
        f"{os.environ.get('DB_USER','globus')}@"
        f"{os.environ.get('DB_HOST','127.0.0.1')}:"
        f"{os.environ.get('DB_PORT','3306')}/"
        f"{os.environ.get('DB_NAME','globus')}"))
    any_fatal |= fatal

    # 4. Expected tables present
    EXPECTED = {
        "members", "auth_codes", "config", "globus_messages",
        "globus_vault_sources", "globus_vault_files",
        "globus_oauth_connections", "globus_oauth_states",
        "globus_agent_runs", "globus_agent_schedules",
        "globus_telegram_bots", "globus_telegram_bot_sends",
    }
    def _tables():
        rows = db_read("SHOW TABLES") or []
        present = {next(iter(r.values())) for r in rows}
        miss = EXPECTED - present
        if miss:
            return False, (f"missing: {', '.join(sorted(miss))}. "
                           "Run: mysql ... < schema/globus_schema.sql")
        return True, f"all {len(EXPECTED)} expected tables present"
    ok, fatal = _check("schema tables", _tables)
    any_fatal |= fatal

    # 5. Storage paths writable
    for env_key, default in [
        ("GLOBUS_AGENTS_WORK_DIR", "/var/lib/globus/agents"),
        ("GLOBUS_RAW_DATA_DIR", "/var/lib/globus/raw-data"),
    ]:
        path = os.environ.get(env_key, default)
        def _w(p=path, k=env_key):
            try:
                os.makedirs(p, exist_ok=True)
                probe = os.path.join(p, ".check_install_probe")
                with open(probe, "w") as f:
                    f.write("ok")
                os.remove(probe)
                return True, p
            except Exception as e:
                return False, f"{p}: {type(e).__name__}: {e}"
        ok, fatal = _check(f"storage writable ({env_key})", _w)
        any_fatal |= fatal

    # 6. Fernet key (warn-only if not configured)
    def _fernet():
        key = cfg("GLOBUS_OAUTH_ENCRYPTION_KEY", "")
        if not key:
            return "warn", ("GLOBUS_OAUTH_ENCRYPTION_KEY not set in DB "
                            "config — OAuth + bot tools will fail until "
                            "you add one")
        from oauth_db import encrypt_token, decrypt_token
        r = decrypt_token(encrypt_token("check-install-probe"))
        if r != "check-install-probe":
            return False, "Fernet roundtrip mismatch"
        return True, "key valid + roundtrip OK"
    ok, fatal = _check("Fernet key", _fernet)
    any_fatal |= fatal

    # 7. Persona file
    persona = os.path.join(repo_root, "config", "persona.md")
    example = os.path.join(repo_root, "config", "persona.example.md")
    def _persona():
        if os.path.isfile(persona):
            return True, "config/persona.md present"
        if os.path.isfile(example):
            return "warn", ("running on persona.example.md — copy to "
                            "persona.md and customise for your install")
        return False, "no persona file found"
    ok, _ = _check("persona", _persona)

    # 8. At least one active member
    def _members():
        rows = db_read("SELECT COUNT(*) AS n FROM members "
                       "WHERE status IN ('active', 'comp')")
        n = int(rows[0]["n"]) if rows else 0
        if n == 0:
            return "warn", ("no active members — nobody can sign in "
                            "yet. Run: python3 scripts/add_member.py "
                            "you@example.com")
        return True, f"{n} active member(s)"
    _check("active members", _members)

    print("", file=sys.stderr)
    if any_fatal:
        print(RED("install check FAILED — fix the FAIL lines above "
                  "before starting the server"), file=sys.stderr)
        sys.exit(1)
    print(GREEN("install check PASSED"), file=sys.stderr)
    sys.exit(0)


if __name__ == "__main__":
    main()
