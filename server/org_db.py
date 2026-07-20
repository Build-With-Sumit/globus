"""Organization (multi-tenant) auth + membership — the data layer for the
optional ORG-ONLY employee portals ("Globus for Organizations").

An org portal is a separate workspace served on its own host (e.g.
globus.acme.com), where employees of one company self-enroll with their
company email and each chats with Globus grounded strictly on their OWN
connected data. It is entirely opt-in: with no `organizations` rows, this
module is dormant and the server behaves as a normal single-tenant install.

Mirrors members_db.py: DB access (db_read/db_write) is injected via configure()
to avoid a circular import with the server. cfg (for the fail-closed
ORG_PORTAL_HOSTS fallback) comes from db_helpers, which has no server deps.

ISOLATION MODEL (see the host gate in globus_server.do_GET/do_POST):
  Authorization on an org host = (arrival Host -> org_id) INTERSECT
  (email in org_members WHERE status='active'). It NEVER uses the customer
  member check. Everything fails CLOSED: db_read returns None on error, which
  every predicate here maps to "deny".

CROSS-SURFACE SAFETY: auto_enroll writes ONLY org_members. It deliberately does
NOT upsert the single-tenant `members` table — doing so would make an employee
pass the customer membership check. Employees are org-scoped only; a members
row appears for them only if they separately become a paying customer.
"""
from __future__ import annotations

import re
from ipaddress import IPv4Address, IPv6Address, ip_address

from db_helpers import cfg


# Module state injected by the server at startup.
_DB_READ = None
_DB_WRITE = None

# Sentinel: "this IS a recognised org-portal host, but the org can't be resolved
# right now (DB error / suspended)". The gate treats it as deny-by-default —
# serve the org login/unavailable page, NEVER fall through to the customer site.
DENY = object()
_DNS_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")


def configure(*, db_read, db_write):
    """Wire in db_read/db_write callables (invoked at call time, never import)."""
    global _DB_READ, _DB_WRITE
    if not callable(db_read) or not callable(db_write):
        raise TypeError("db_read and db_write must be callable")
    _DB_READ = db_read
    _DB_WRITE = db_write


def _domain_of(email):
    email = (email or "").strip().lower()
    return email.rsplit("@", 1)[-1] if "@" in email else ""


def normalize_host_header(value):
    """Return one canonical host without a port, or ``""`` when malformed.

    Bracketed IPv6 literals must have a closing bracket and, when present, a
    numeric 1-65535 port. DNS names use strict ASCII labels; a single trailing
    root dot is canonicalized away. Unbracketed IPv6 and userinfo are rejected.
    """
    if not isinstance(value, str):
        return ""
    raw = value.strip().lower()
    if not raw or len(raw) > 320:
        return ""

    if raw.startswith("["):
        matched = re.fullmatch(r"\[([^\[\]]+)\](?::([0-9]+))?", raw)
        if matched is None:
            return ""
        address_text, port_text = matched.groups()
        if "%" in address_text:
            return ""
        try:
            address = ip_address(address_text)
        except ValueError:
            return ""
        if not isinstance(address, IPv6Address):
            return ""
        if port_text is not None and not 1 <= int(port_text) <= 65535:
            return ""
        return f"[{address.compressed}]"

    if raw.count(":") > 1:
        return ""
    if ":" in raw:
        host, port_text = raw.rsplit(":", 1)
        if not port_text.isdigit() or not 1 <= int(port_text) <= 65535:
            return ""
    else:
        host = raw
    host = host.removesuffix(".")
    if not host or len(host) > 253 or "@" in host:
        return ""

    try:
        address = ip_address(host)
    except ValueError:
        address = None
    if isinstance(address, IPv4Address):
        return address.compressed
    if address is not None:
        return ""

    labels = host.split(".")
    if any(not _DNS_LABEL.fullmatch(label) for label in labels):
        return ""
    return host


def configured_org_portals(value):
    """Parse ``host:slug`` pairs with the same host rules as live requests."""
    result = []
    for raw_pair in (value or "").split(","):
        host_text, separator, slug = raw_pair.strip().rpartition(":")
        host = normalize_host_header(host_text)
        slug = slug.strip()
        if separator and host and slug and len(slug) <= 128:
            result.append((host, slug))
    return result


def org_for_host(host):
    """Resolve an arrival Host header to an org.
      -> {"id","slug"}  when host is an active org portal
      -> DENY           when host is a recognised org portal but unresolved
                        (DB error or org suspended) — gate must deny, not fall through
      -> None           when host is a plain single-tenant host (unchanged behaviour)
    """
    host = normalize_host_header(host)
    if not host:
        return None
    rows = _DB_READ("SELECT id, slug, name, status FROM organizations "
                    "WHERE portal_host=%s LIMIT 1", (host,))
    if rows:                                        # known org portal
        r = rows[0]
        if (r.get("status") or "") == "active":
            return {"id": r["id"], "slug": r["slug"], "name": r.get("name")}
        return DENY                               # suspended org -> deny
    if rows is None:                                # DB ERROR -> fail closed via config
        for configured_host, slug in configured_org_portals(
            cfg("ORG_PORTAL_HOSTS", "")
        ):
            if configured_host == host:
                r2 = _DB_READ("SELECT id, slug, name FROM organizations "
                              "WHERE slug=%s AND status='active' LIMIT 1",
                              (slug,))
                if r2:
                    return {"id": r2[0]["id"], "slug": r2[0]["slug"],
                            "name": r2[0].get("name")}
                return DENY                       # recognised host, unresolved -> deny
        return None                                 # unknown host, DB down -> single-tenant
    return None                                     # rows == [] : genuine single-tenant host


def domain_org_id(domain):
    """Which org owns this bare email domain? Global-unique -> at most one."""
    domain = (domain or "").strip().lower()
    if not domain:
        return None
    rows = _DB_READ("SELECT org_id FROM org_domains WHERE domain=%s LIMIT 1", (domain,))
    return rows[0]["org_id"] if rows else None


def domain_matches_org(email, org_id):
    """True iff the email's domain is a registered domain of exactly this org.
    Exact equality (not suffix) — 'acme.com.evil.com' and unregistered
    subdomains do not match."""
    return bool(org_id) and domain_org_id(_domain_of(email)) == org_id


def org_member_active(email, org_id):
    """True iff email is an ACTIVE member of this org. Fail-closed."""
    if not (email and org_id):
        return False
    rows = _DB_READ("SELECT 1 AS ok FROM org_members "
                    "WHERE org_id=%s AND email=%s AND status='active' LIMIT 1",
                    (org_id, email))
    return bool(rows)                               # None (error) or [] -> False -> deny


def auto_enroll(email, org_id, domain, role="employee", department=None):
    """Create/re-activate an org_members row for a verified employee. Re-asserts
    the domain owns the org (defense in depth). Writes ONLY org_members — never
    the single-tenant members table (see module docstring). Returns True on success."""
    email = (email or "").strip().lower()
    if not (email and org_id):
        return False
    if domain_org_id((domain or "").strip().lower()) != org_id:
        return False
    ok = _DB_WRITE(
        "INSERT INTO org_members (org_id, email, role, department, status) "
        "VALUES (%s, %s, %s, %s, 'active') "
        "ON DUPLICATE KEY UPDATE status='active', updated_at=CURRENT_TIMESTAMP",
        (org_id, email, role, department))
    return bool(ok)


# ── Roles, teams, and default-private agent grants ─────────────────────────
# The employee portal is DEFAULT-PRIVATE: agent_grants_for() returns the empty
# set unless an admin has explicitly granted an agent to the employee (via
# 'all' / their 'department' / their 'member' email). Everything fails closed.

def org_member_role(email, org_id):
    if not (email and org_id):
        return None
    rows = _DB_READ("SELECT role FROM org_members WHERE org_id=%s AND email=%s "
                    "AND status='active' LIMIT 1", (org_id, email))
    return rows[0]["role"] if rows else None


def is_org_admin(email, org_id):
    return org_member_role(email, org_id) == "admin"


def org_member_department(email, org_id):
    if not (email and org_id):
        return ""
    rows = _DB_READ("SELECT department FROM org_members WHERE org_id=%s AND email=%s "
                    "AND status='active' LIMIT 1", (org_id, email))
    return (rows[0].get("department") if rows else "") or ""


def agent_grants_for(email, org_id):
    """Set of agent slugs THIS employee may see/run — granted via 'all', their
    department, or their own email. Empty by default (private) and empty on any
    error (fail closed)."""
    if not (email and org_id):
        return set()
    dept = org_member_department(email, org_id)
    rows = _DB_READ(
        "SELECT DISTINCT agent_slug FROM org_agent_grants WHERE org_id=%s AND ("
        "  audience_type='all'"
        "  OR (audience_type='department' AND audience_value=%s AND %s<>'')"
        "  OR (audience_type='member' AND audience_value=%s))",
        (org_id, dept, dept, email))
    return {r["agent_slug"] for r in rows} if rows else set()


def list_grants(org_id):
    if not org_id:
        return []
    return _DB_READ("SELECT id, agent_slug, audience_type, audience_value, "
                    "created_by, created_at FROM org_agent_grants "
                    "WHERE org_id=%s ORDER BY agent_slug, audience_type, audience_value",
                    (org_id,)) or []


def grant_agent(org_id, agent_slug, audience_type, audience_value, created_by):
    if audience_type not in ("all", "department", "member"):
        return False
    if not (org_id and agent_slug):
        return False
    if audience_type == "all":
        audience_value = ""
    elif not audience_value:
        return False
    return bool(_DB_WRITE(
        "INSERT INTO org_agent_grants (org_id, agent_slug, audience_type, "
        "audience_value, created_by) VALUES (%s,%s,%s,%s,%s) "
        "ON DUPLICATE KEY UPDATE created_by=VALUES(created_by)",
        (org_id, agent_slug, audience_type, audience_value, created_by)))


def revoke_grant(org_id, grant_id):
    if not (org_id and grant_id):
        return False
    return bool(_DB_WRITE("DELETE FROM org_agent_grants WHERE org_id=%s AND id=%s",
                          (org_id, grant_id)))


def list_org_members(org_id):
    if not org_id:
        return []
    return _DB_READ("SELECT email, role, department, status, joined_at "
                    "FROM org_members WHERE org_id=%s ORDER BY email", (org_id,)) or []


def set_member_department(org_id, email, dept):
    if not (org_id and email):
        return False
    return bool(_DB_WRITE("UPDATE org_members SET department=%s "
                          "WHERE org_id=%s AND email=%s",
                          ((dept or None), org_id, email)))


def set_member_role(org_id, email, role):
    if role not in ("admin", "employee") or not (org_id and email):
        return False
    return bool(_DB_WRITE("UPDATE org_members SET role=%s WHERE org_id=%s AND email=%s",
                          (role, org_id, email)))


def try_org_login(email, org_id, email_verified, hd=None, domain=None):
    """Gate + auto-enroll a Google login on an org host.
    Returns (ok: bool, reason: str). On ok, the org_members row is guaranteed
    active. On failure NOTHING is enrolled."""
    email = (email or "").strip().lower()
    if not email or "@" not in email:
        return False, "Couldn't read your email — try again."
    # email_verified: require strictly truthy; missing/false -> unverified (fail closed)
    if email_verified not in (True, "true", "True", 1, "1"):
        return False, f"Your Google email ({email}) isn't verified by Google."
    dom = (domain or _domain_of(email)).strip().lower()
    if domain_org_id(dom) != org_id:                # exact match against org's domains
        return False, (f"{dom} isn't a registered domain for this workspace — "
                       "sign in with your company Google account.")
    hd = (hd or "").strip().lower()
    if hd and domain_org_id(hd) != org_id:          # corroborate when Google sends it
        return False, "Your Google hosted-domain doesn't match this workspace."
    if not auto_enroll(email, org_id, dom):
        return False, "Couldn't complete enrollment — try again in a moment."
    return True, ""
