#!/usr/bin/env python3
"""Versioned schema migrations for Globus.

Usage:
  python3 scripts/migrate.py status              # what is applied, pending, changed
  python3 scripts/migrate.py up [--dry-run]      # apply everything pending, in order
  python3 scripts/migrate.py baseline            # mark all CURRENT files applied,
                                                 #   running none of them
Exit codes:
  0  success (including "nothing pending")
  1  bad usage, or a refusal you need to resolve by hand
  2  a migration failed

WHY THIS EXISTS
---------------
Before this, the schema was one big `CREATE TABLE IF NOT EXISTS` bootstrap and
nothing recorded what a given database had actually received. That fails in a
specific, quiet way: code deployed against a column that was never added does
not crash on startup — it crashes on the one code path that touches it, at 3am,
inside an `except` somewhere. Worse, the app's own DB helper is FAIL-SOFT by
design (`db_write` returns False on any exception), so a write against a missing
table returns False into a caller that does not check, and the feature simply
does nothing forever while every log line says it ran.

So this runner deliberately does NOT use `db_write`. It takes a raw connection
and lets exceptions out. A migration that fails must be loud.

THE HONEST LIMIT — MySQL DDL IS NOT TRANSACTIONAL
-------------------------------------------------
`CREATE TABLE` / `ALTER TABLE` implicitly commit in MySQL. A file containing
three statements that fails on the second leaves the first applied and the third
not, and NOTHING can roll that back. This runner therefore:

  * applies one file at a time, in version order, and records it only after
    every statement in it succeeded;
  * stops dead at the first failure rather than continuing, so the gap is one
    known file rather than a scatter;
  * reports exactly which file failed and which statement index within it.

That is as good as MySQL allows. The real mitigation is keeping each migration
small enough that a partial application is obvious. Do not batch six unrelated
changes into one file because it is tidier.

CHECKSUMS
---------
Each applied migration's SHA-256 is stored. `status` reports a file whose
content changed AFTER it was applied — the drift where someone edits a
migration that already ran on their machine, and every other environment
silently keeps the old shape. Editing an applied migration is never the fix;
add a new one.
"""
from __future__ import annotations
import hashlib
import os
import re
import sys

MIGRATIONS_DIRNAME = os.path.join("schema", "migrations")
VERSION_RE = re.compile(r"^(\d{4})_([A-Za-z0-9_.-]+)\.sql$")


class MigrationLayoutError(Exception):
    """Something about the migrations directory is unsafe to proceed on."""

TRACKING_TABLE = """
CREATE TABLE IF NOT EXISTS schema_migrations (
  version    VARCHAR(20)  NOT NULL PRIMARY KEY,
  name       VARCHAR(190) NOT NULL,
  checksum   CHAR(64)     NOT NULL,
  applied_at TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
"""


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


def _repo_root():
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _connect():
    """A RAW connection. Deliberately not db_helpers.db_write — see the module
    docstring. If the database is unreachable we must fail, not return False."""
    import pymysql
    return pymysql.connect(
        host=os.environ.get("DB_HOST", "127.0.0.1"),
        port=int(os.environ.get("DB_PORT", "3306")),
        user=os.environ.get("DB_USER", "globus"),
        password=os.environ.get("DB_PASSWORD", ""),
        database=os.environ.get("DB_NAME", "globus"),
        charset="utf8mb4",
        autocommit=True,
    )


def split_statements(sql):
    """Split a migration file into statements on semicolons.

    Respects single/double-quoted strings, backtick identifiers, `--` and `#`
    line comments and `/* */` blocks, so a semicolon inside any of them does not
    split a statement in half. It does NOT understand DELIMITER, so a stored
    procedure or trigger body needs its own file and its own handling — better
    to refuse that case than to silently mangle it.
    """
    statements, buf = [], []
    i, n = 0, len(sql)
    quote = None
    while i < n:
        ch = sql[i]
        nxt = sql[i + 1] if i + 1 < n else ""
        if quote:
            buf.append(ch)
            if ch == "\\" and quote in ("'", '"'):
                if nxt:
                    buf.append(nxt)
                    i += 2
                    continue
            elif ch == quote:
                quote = None
            i += 1
            continue
        if ch in ("'", '"', "`"):
            quote = ch
            buf.append(ch)
            i += 1
            continue
        if ch == "-" and nxt == "-":
            j = sql.find("\n", i)
            i = n if j < 0 else j + 1
            buf.append("\n")
            continue
        if ch == "#":
            j = sql.find("\n", i)
            i = n if j < 0 else j + 1
            buf.append("\n")
            continue
        if ch == "/" and nxt == "*":
            j = sql.find("*/", i + 2)
            i = n if j < 0 else j + 2
            continue
        if ch == ";":
            stmt = "".join(buf).strip()
            if stmt:
                statements.append(stmt)
            buf = []
            i += 1
            continue
        buf.append(ch)
        i += 1
    tail = "".join(buf).strip()
    if tail:
        statements.append(tail)
    return statements


def discover(root=None):
    """[(version, name, path, checksum)] sorted by version.

    A filename that does not match NNNN_name.sql is REFUSED rather than skipped.
    A silently ignored migration is the worst outcome available here: it looks
    exactly like a migration that ran."""
    root = root or _repo_root()
    directory = os.path.join(root, MIGRATIONS_DIRNAME)
    if not os.path.isdir(directory):
        return []
    out, bad = [], []
    for entry in sorted(os.listdir(directory)):
        if entry.startswith("."):
            continue
        m = VERSION_RE.match(entry)
        if not m:
            bad.append(entry)
            continue
        path = os.path.join(directory, entry)
        with open(path, "rb") as fh:
            checksum = hashlib.sha256(fh.read()).hexdigest()
        out.append((m.group(1), m.group(2), path, checksum))
    if bad:
        raise MigrationLayoutError(
            "refusing to run: these files are in schema/migrations/ but are not "
            "named NNNN_name.sql, so they would be silently skipped:\n  "
            + "\n  ".join(bad))
    return out


def applied(conn):
    """{version: (name, checksum)} already recorded."""
    with conn.cursor() as cur:
        cur.execute(TRACKING_TABLE)
        cur.execute("SELECT version, name, checksum FROM schema_migrations")
        return {r[0]: (r[1], r[2]) for r in cur.fetchall()}


def record(conn, version, name, checksum):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO schema_migrations (version, name, checksum) "
            "VALUES (%s, %s, %s) ON DUPLICATE KEY UPDATE "
            "name=VALUES(name), checksum=VALUES(checksum)",
            (version, name, checksum))


def cmd_status(conn):
    have = applied(conn)
    files = discover()
    pending = [f for f in files if f[0] not in have]
    changed = [f for f in files
               if f[0] in have and have[f[0]][1] != f[3]]
    orphans = [v for v in have if v not in {f[0] for f in files}]

    print(f"applied: {len(have)}   pending: {len(pending)}   "
          f"changed-since-applied: {len(changed)}")
    for version, name, _, checksum in files:
        if version not in have:
            mark = "  PENDING"
        elif have[version][1] != checksum:
            mark = "  CHANGED"
        else:
            mark = "  ok     "
        print(f"{mark} {version}_{name}")
    for version in sorted(orphans):
        # Recorded but the file is gone. Not fatal — a file may legitimately be
        # renamed — but it means this database's history cannot be replayed from
        # this checkout, which someone should know before trusting a rebuild.
        print(f"  ORPHAN  {version} (recorded, but no file in schema/migrations/)")
    if changed:
        print("\nA CHANGED migration has been edited since it was applied here. "
              "Other environments still have the OLD shape. Do not edit an "
              "applied migration — add a new one.")
    return 0


def cmd_up(conn, dry_run=False):
    have = applied(conn)
    files = discover()
    pending = [f for f in files if f[0] not in have]
    if not pending:
        print("nothing pending")
        return 0

    # Refuse a migration numbered BELOW something already applied. It means two
    # branches numbered independently and merged; applying it now runs the steps
    # in an order no one has ever tested, and on some other machine it will run
    # in the other order.
    highest = max(have) if have else ""
    out_of_order = [f for f in pending if f[0] < highest]
    if out_of_order:
        print("refusing to run: these are numbered below the highest applied "
              f"migration ({highest}), so applying them now would run your "
              "migrations in an order no environment has tested:\n  "
              + "\n  ".join(f"{v}_{n}" for v, n, _, _ in out_of_order)
              + "\nRenumber them above " + highest + " and re-run.",
              file=sys.stderr)
        return 1

    for version, name, path, checksum in pending:
        with open(path, encoding="utf-8") as fh:
            statements = split_statements(fh.read())
        if dry_run:
            print(f"  would apply {version}_{name} ({len(statements)} statement(s))")
            continue
        print(f"  applying {version}_{name} ({len(statements)} statement(s))",
              flush=True)
        for idx, stmt in enumerate(statements, 1):
            try:
                with conn.cursor() as cur:
                    cur.execute(stmt)
            except Exception as e:
                # MySQL DDL auto-commits, so statements 1..idx-1 ARE applied and
                # cannot be rolled back. Say so precisely rather than implying a
                # clean failure.
                print(f"\nFAILED on {version}_{name}, statement {idx} of "
                      f"{len(statements)}: {type(e).__name__}: {e}\n"
                      f"Statements 1-{idx - 1} of this file are already applied "
                      f"and MySQL cannot roll DDL back. Fix the file, apply the "
                      f"remainder by hand, then record it with:\n"
                      f"  python3 scripts/migrate.py baseline",
                      file=sys.stderr)
                return 2
        record(conn, version, name, checksum)
    print("done" if not dry_run else "dry run — nothing applied")
    return 0


def cmd_baseline(conn):
    """Mark every current migration applied WITHOUT running it.

    For a database that already has the schema — either it predates migrations,
    or it just ran globus_schema.sql. Running the files against it would be at
    best a no-op and at worst destructive, but leaving them pending means the
    next `up` tries exactly that."""
    have = applied(conn)
    files = discover()
    newly = [f for f in files if f[0] not in have]
    for version, name, _, checksum in newly:
        record(conn, version, name, checksum)
    print(f"baselined {len(newly)} migration(s); {len(files)} now recorded as "
          f"applied (none were executed)")
    return 0


def main():
    argv = sys.argv[1:]
    if not argv or argv[0] not in ("status", "up", "baseline"):
        print("usage: migrate.py status | up [--dry-run] | baseline",
              file=sys.stderr)
        return 1
    _load_env(os.path.join(_repo_root(), ".env"))
    try:
        conn = _connect()
    except Exception as e:
        print(f"cannot reach the database: {type(e).__name__}: {e}",
              file=sys.stderr)
        return 2
    try:
        if argv[0] == "status":
            return cmd_status(conn)
        if argv[0] == "baseline":
            return cmd_baseline(conn)
        return cmd_up(conn, dry_run="--dry-run" in argv)
    except MigrationLayoutError as e:
        print(e, file=sys.stderr)
        return 1
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
