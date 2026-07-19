"""Behavioural tests for org_db — the multi-tenant isolation layer.

These cover the security-critical properties, all of which must FAIL CLOSED:
  * an org host never falls through to the single-tenant site,
  * a suspended org and a DB error both DENY,
  * domain matching is exact (no suffix/subdomain confusion),
  * membership and agent grants are private by default,
  * a Google login is refused unless the email is verified AND the domain
    is registered to exactly this org.

Hermetic: `db_helpers` is stubbed in sys.modules, so no MySQL, no PyMySQL,
and no network. Run with:  python tests/test_org_db.py
"""
import os
import sys
import types

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(_HERE), "server"))

# ── Stub db_helpers BEFORE importing org_db (it does `from db_helpers import cfg`)
_CFG = {}
_stub = types.ModuleType("db_helpers")
_stub.cfg = lambda key, default="": _CFG.get(key, default)
sys.modules["db_helpers"] = _stub

import org_db  # noqa: E402


class FakeDB:
    """Scriptable db_read/db_write. `reads` maps a substring of the SQL to the
    result; None means 'DB error' (what db_read returns on failure)."""

    def __init__(self, reads=None, write_ok=True):
        self.reads = reads or {}
        self.write_ok = write_ok
        self.writes = []

    def read(self, sql, params=None):
        for needle, result in self.reads.items():
            if needle in sql:
                return result(params) if callable(result) else result
        return []

    def write(self, sql, params=None):
        self.writes.append((sql, params))
        return self.write_ok


def wire(reads=None, write_ok=True):
    db = FakeDB(reads, write_ok)
    org_db.configure(db_read=db.read, db_write=db.write)
    return db


def domains(mapping):
    """A faithful org_domains fixture: resolves the ACTUAL domain being queried,
    so an unregistered domain returns [] the way the real table would. Without
    this, a blanket fixture makes every domain look registered and silently
    defeats the hosted-domain / look-alike checks."""
    def _read(params):
        d = (params or (None,))[0]
        return [{"org_id": mapping[d]}] if d in mapping else []
    return _read


PASS, FAIL = [], []


def check(name, cond):
    (PASS if cond else FAIL).append(name)
    print(("  ok   " if cond else "  FAIL ") + name)


# ── org_for_host: the deny-by-default host gate ───────────────────────────
print("org_for_host (host gate):")

wire({"FROM organizations": [{"id": 7, "slug": "acme", "name": "Acme",
                              "status": "active"}]})
check("active org host resolves to the org",
      org_db.org_for_host("globus.acme.com") == {"id": 7, "slug": "acme",
                                                 "name": "Acme"})

wire({"FROM organizations": [{"id": 7, "slug": "acme", "name": "Acme",
                              "status": "suspended"}]})
check("SUSPENDED org DENIES (never falls through to single-tenant)",
      org_db.org_for_host("globus.acme.com") is org_db.DENY)

wire({"FROM organizations": []})
check("unknown host is a normal single-tenant host (None)",
      org_db.org_for_host("example.com") is None)

# DB error + host listed in ORG_PORTAL_HOSTS -> must NOT fall through.
_CFG["ORG_PORTAL_HOSTS"] = "globus.acme.com:acme"
wire({"WHERE portal_host": None, "WHERE slug": []})
check("DB ERROR on a recognised org host DENIES (fail closed)",
      org_db.org_for_host("globus.acme.com") is org_db.DENY)

wire({"WHERE portal_host": None,
      "WHERE slug": [{"id": 7, "slug": "acme", "name": "Acme"}]})
check("DB error + config fallback resolves the org",
      org_db.org_for_host("globus.acme.com") == {"id": 7, "slug": "acme",
                                                 "name": "Acme"})

wire({"WHERE portal_host": None})
check("DB error on an UNKNOWN host stays single-tenant (None)",
      org_db.org_for_host("random.example.org") is None)
_CFG.clear()

check("empty host -> None", org_db.org_for_host("") is None)

# ── domain matching: exact, never suffix ──────────────────────────────────
print("domain_matches_org (exact match only):")

wire({"FROM org_domains": [{"org_id": 7}]})
check("registered domain matches its org",
      org_db.domain_matches_org("bob@acme.com", 7) is True)
check("registered domain does NOT match a different org",
      org_db.domain_matches_org("bob@acme.com", 9) is False)

wire({"FROM org_domains": []})
check("look-alike domain (acme.com.evil.com) is refused",
      org_db.domain_matches_org("bob@acme.com.evil.com", 7) is False)

wire({"FROM org_domains": None})          # DB error
check("DB error -> domain match fails closed",
      org_db.domain_matches_org("bob@acme.com", 7) is False)

# ── membership: private + fail closed ─────────────────────────────────────
print("org_member_active (fail closed):")

wire({"FROM org_members": [{"ok": 1}]})
check("active member is active", org_db.org_member_active("bob@acme.com", 7) is True)

wire({"FROM org_members": []})
check("non-member is denied", org_db.org_member_active("eve@evil.com", 7) is False)

wire({"FROM org_members": None})
check("DB error -> membership denied (fail closed)",
      org_db.org_member_active("bob@acme.com", 7) is False)

check("missing org_id -> denied", org_db.org_member_active("bob@acme.com", None) is False)

# ── auto_enroll: re-asserts domain ownership ──────────────────────────────
print("auto_enroll (defense in depth):")

db = wire({"FROM org_domains": [{"org_id": 7}]})
check("enrolls a domain-matched employee",
      org_db.auto_enroll("bob@acme.com", 7, "acme.com") is True and len(db.writes) == 1)

db = wire({"FROM org_domains": [{"org_id": 99}]})   # domain owned by ANOTHER org
check("refuses when the domain does not own the org (no write)",
      org_db.auto_enroll("bob@acme.com", 7, "acme.com") is False and not db.writes)

# ── agent grants: default-private ─────────────────────────────────────────
print("agent_grants_for (default private):")

wire({"FROM org_members": [{"department": "sales"}],
      "FROM org_agent_grants": []})
check("no grants -> employee sees NOTHING", org_db.agent_grants_for("bob@acme.com", 7) == set())

wire({"FROM org_members": [{"department": "sales"}],
      "FROM org_agent_grants": [{"agent_slug": "researcher"}]})
check("a granted agent is visible",
      org_db.agent_grants_for("bob@acme.com", 7) == {"researcher"})

wire({"FROM org_members": [{"department": "sales"}],
      "FROM org_agent_grants": None})               # DB error
check("DB error -> no agents (fail closed)",
      org_db.agent_grants_for("bob@acme.com", 7) == set())

print("grant/role validation:")
db = wire()
check("grant_agent rejects a bogus audience_type",
      org_db.grant_agent(7, "researcher", "everyone", "", "a@acme.com") is False
      and not db.writes)
db = wire()
org_db.grant_agent(7, "researcher", "all", "ignored", "a@acme.com")
check("audience_type 'all' forces an empty audience_value",
      db.writes and db.writes[0][1][3] == "")
db = wire()
check("set_member_role rejects an unknown role",
      org_db.set_member_role(7, "bob@acme.com", "superuser") is False and not db.writes)

# ── try_org_login: the Google gate ────────────────────────────────────────
print("try_org_login (Google gate):")

wire({"FROM org_domains": domains({"acme.com": 7})})
ok, why = org_db.try_org_login("bob@acme.com", 7, True)
check("verified + registered domain logs in", ok is True)

ok, why = org_db.try_org_login("bob@acme.com", 7, False)
check("UNVERIFIED Google email is refused", ok is False and "verified" in why)

ok, why = org_db.try_org_login("bob@acme.com", 7, None)
check("missing email_verified is refused (fail closed)", ok is False)

ok, why = org_db.try_org_login("bob@unregistered.com", 7, True)
check("unregistered domain is refused", ok is False)

wire({"FROM org_domains": domains({"acme.com": 7, "other.com": 99})})
ok, why = org_db.try_org_login("bob@other.com", 7, True)
check("domain registered to ANOTHER org is refused", ok is False)

wire({"FROM org_domains": domains({"acme.com": 7})})
ok, why = org_db.try_org_login("bob@acme.com", 7, True, hd="evil.com")
check("mismatched Google hosted-domain is refused",
      ok is False and "hosted-domain" in why)

ok, why = org_db.try_org_login("bob@acme.com", 7, True, hd="acme.com")
check("matching Google hosted-domain still logs in", ok is True)

ok, why = org_db.try_org_login("not-an-email", 7, True)
check("malformed email is refused", ok is False)

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
if FAIL:
    for f in FAIL:
        print("  FAILED: " + f)
    sys.exit(1)
print("org_db isolation properties hold.")
