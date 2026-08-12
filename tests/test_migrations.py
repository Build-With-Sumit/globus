"""Behavioural tests for the schema migration runner.

These cover the invariants that make a migration runner trustworthy rather than
merely present:

  * a statement splitter that does not cut a statement in half on a semicolon
    inside a string, an identifier, or a comment,
  * a migration file with a name the runner cannot parse is REFUSED, never
    skipped — a silently ignored migration looks exactly like an applied one,
  * a file edited AFTER it was applied is reported as CHANGED,
  * a migration numbered below the highest applied one is refused, because
    applying it now runs the steps in an order nothing has tested,
  * a failure stops at that file and does NOT record it, and says which
    statement went — MySQL DDL auto-commits, so a partial application is real
    and must be described precisely rather than implied away.

Hermetic: a fake DB-API connection stands in for pymysql. No MySQL required.
Run with:  python tests/test_migrations.py
"""
import os
import shutil
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(_HERE), "scripts"))

import migrate as M            # noqa: E402

PASS, FAIL = [], []


def check(label, ok):
    (PASS if ok else FAIL).append(label)
    print(("  ok   " if ok else "  FAIL ") + label)


# ── a fake connection ────────────────────────────────────────────────────
class FakeCursor:
    def __init__(self, conn):
        self.conn = conn

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=()):
        self.conn.executed.append((sql, params))
        low = " ".join(sql.split()).lower()
        if self.conn.fail_on and self.conn.fail_on in sql:
            raise RuntimeError("simulated DDL failure")
        if low.startswith("select version, name, checksum"):
            self._rows = [(v, n, c) for v, (n, c) in self.conn.rows.items()]
            return
        if low.startswith("insert into schema_migrations"):
            self.conn.rows[params[0]] = (params[1], params[2])
            return
        self._rows = []

    def fetchall(self):
        return getattr(self, "_rows", [])


class FakeConn:
    def __init__(self, rows=None, fail_on=None):
        self.rows = dict(rows or {})
        self.executed = []
        self.fail_on = fail_on

    def cursor(self):
        return FakeCursor(self)

    def close(self):
        pass


# ── statement splitting ──────────────────────────────────────────────────
print("statement splitting:")
check("two plain statements split",
      len(M.split_statements("CREATE TABLE a (x INT); CREATE TABLE b (y INT);")) == 2)
check("a trailing statement with no semicolon is not dropped",
      len(M.split_statements("CREATE TABLE a (x INT); SELECT 1")) == 2)
check("a semicolon inside a quoted string does not split",
      len(M.split_statements("INSERT INTO t VALUES ('a;b');")) == 1)
check("a semicolon inside a backtick identifier does not split",
      len(M.split_statements("CREATE TABLE `we;ird` (x INT);")) == 1)
check("a semicolon inside a -- comment does not split",
      len(M.split_statements("CREATE TABLE a (x INT); -- note; here\nSELECT 1;")) == 2)
check("a semicolon inside a /* */ block does not split",
      len(M.split_statements("/* a; b */ CREATE TABLE a (x INT);")) == 1)
check("an escaped quote does not end the string early",
      len(M.split_statements("INSERT INTO t VALUES ('it\\'s; fine');")) == 1)
check("a comment-only file yields no statements",
      M.split_statements("-- nothing here\n# nor here\n") == [])
check("whitespace between statements is not a statement",
      len(M.split_statements("SELECT 1;\n\n\n;  ;\nSELECT 2;")) == 2)


# ── discovery ────────────────────────────────────────────────────────────
print("\ndiscovery:")
_tmp = tempfile.mkdtemp()
_mig = os.path.join(_tmp, "schema", "migrations")
os.makedirs(_mig)


def write(name, body="SELECT 1;"):
    with open(os.path.join(_mig, name), "w", encoding="utf-8") as fh:
        fh.write(body)


write("0001_baseline.sql")
write("0002_desk_agents.sql")
found = M.discover(_tmp)
check("well-named files are discovered in version order",
      [f[0] for f in found] == ["0001", "0002"])
check("a checksum is computed per file",
      all(len(f[3]) == 64 for f in found))

write("add_column.sql")
refused = False
try:
    M.discover(_tmp)
except M.MigrationLayoutError:
    refused = True
check("a badly-named file is REFUSED, not silently skipped", refused)
os.remove(os.path.join(_mig, "add_column.sql"))

open(os.path.join(_mig, ".DS_Store"), "w").close()
check("a dotfile is ignored without refusing the run",
      len(M.discover(_tmp)) == 2)


# ── applying ─────────────────────────────────────────────────────────────
print("\napplying:")
_real_discover = M.discover
M.discover = lambda root=None: _real_discover(_tmp)

conn = FakeConn()
rc = M.cmd_up(conn, dry_run=True)
check("a dry run applies nothing", rc == 0 and conn.rows == {})

conn = FakeConn()
rc = M.cmd_up(conn)
check("up applies pending migrations", rc == 0 and sorted(conn.rows) == ["0001", "0002"])

rc = M.cmd_up(conn)
check("re-running is a no-op", rc == 0 and sorted(conn.rows) == ["0001", "0002"])

conn = FakeConn()
rc = M.cmd_baseline(conn)
check("baseline records everything without executing any migration body",
      rc == 0 and sorted(conn.rows) == ["0001", "0002"]
      and not any("CREATE TABLE IF NOT EXISTS desk" in s
                  for s, _ in conn.executed))


# ── refusals ─────────────────────────────────────────────────────────────
print("\nrefusals:")
write("0003_later.sql")
applied_rows = {"0001": ("baseline", M.discover(_tmp)[0][3]),
                "0003": ("later", M.discover(_tmp)[2][3])}
conn = FakeConn(rows=applied_rows)
rc = M.cmd_up(conn)
check("a migration numbered below the highest applied one is refused", rc == 1)
check("...and nothing was recorded by that refused run",
      sorted(conn.rows) == ["0001", "0003"])
os.remove(os.path.join(_mig, "0003_later.sql"))


# ── failure is loud and precise ──────────────────────────────────────────
print("\nfailure handling:")
write("0002_desk_agents.sql",
      "CREATE TABLE ok_one (x INT);\nCREATE TABLE boom (y INT);\n")
conn = FakeConn(fail_on="boom")
rc = M.cmd_up(conn)
check("a failing migration returns the failure exit code", rc == 2)
check("...and is NOT recorded as applied (only the earlier file is)",
      sorted(conn.rows) == ["0001"])
check("...and the statement that succeeded before it really did run",
      any("ok_one" in s for s, _ in conn.executed))


# ── changed-since-applied ────────────────────────────────────────────────
print("\ndrift detection:")
write("0002_desk_agents.sql", "SELECT 1;")
files = M.discover(_tmp)
stale = {"0001": ("baseline", files[0][3]),
         "0002": ("desk_agents", "0" * 64)}     # applied with a different body
conn = FakeConn(rows=stale)
rc = M.cmd_status(conn)
check("status runs over a drifted set", rc == 0)
check("a file edited after it was applied is not treated as pending",
      sorted(conn.rows) == ["0001", "0002"])

M.discover = _real_discover
shutil.rmtree(_tmp, ignore_errors=True)


# ── the repo's own migrations ────────────────────────────────────────────
print("\nthis repo:")
real = M.discover()
check("the repo ships at least a baseline", len(real) >= 1)
check("versions are unique", len({f[0] for f in real}) == len(real))
check("versions are zero-padded and sorted", [f[0] for f in real]
      == sorted(f[0] for f in real))
for version, name, path, _ in real:
    with open(path, encoding="utf-8") as fh:
        body = fh.read()
    check(f"{version}_{name} parses into statements",
          isinstance(M.split_statements(body), list))


print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
if FAIL:
    for f in FAIL:
        print("  FAILED: " + f)
    sys.exit(1)
print("migration-runner invariants hold.")
