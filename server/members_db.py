"""Member-table CRUD + active-status check.

What's here:
  - upsert_member():          INSERT ... ON DUPLICATE KEY UPDATE on members
  - db_member_active():       quick "is row status active/comp?" check
  - is_active_member():       db_member_active OR (optional Stripe-fallback)
  - get_member():             fetch one members row by email

DB access (db_read / db_write) is injected via configure() to avoid
circular imports with the main server.

Stripe is OPTIONAL. The reference impl auto-upserts members from a
Stripe subscription check; the open-source default doesn't ship that
integration. To enable it, write a `stripe_api` module that exports
`email_has_active_subscription(email) -> bool` and put it on PYTHONPATH
— `members_db` will pick it up automatically at import time.
"""
from __future__ import annotations

try:
    from stripe_api import email_has_active_subscription  # type: ignore
except ImportError:
    # No Stripe integration installed — members are active iff the
    # `members` table says so. This is the default for the OSS install.
    def email_has_active_subscription(email):
        return False


# Module state injected by lead_server at startup.
_DB_READ = None
_DB_WRITE = None


def configure(*, db_read, db_write):
    """Wire in db_read/db_write callables. Both must be callable —
    they're invoked at member-CRUD call time, never at import."""
    global _DB_READ, _DB_WRITE
    if not callable(db_read) or not callable(db_write):
        raise TypeError("db_read and db_write must be callable")
    _DB_READ = db_read
    _DB_WRITE = db_write


def upsert_member(email, status="active", source="stripe", first_name=None,
                  last_name=None, phone=None, country=None,
                  stripe_customer_id=None):
    _DB_WRITE(
        "INSERT INTO members (email,first_name,last_name,phone,country,stripe_customer_id,status,source) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s) "
        "ON DUPLICATE KEY UPDATE "
        "  first_name=COALESCE(VALUES(first_name),first_name), "
        "  last_name=COALESCE(VALUES(last_name),last_name), "
        "  phone=COALESCE(VALUES(phone),phone), "
        "  country=COALESCE(VALUES(country),country), "
        "  stripe_customer_id=COALESCE(VALUES(stripe_customer_id),stripe_customer_id), "
        "  status=IF(members.status='comp','comp',VALUES(status))",
        (email, first_name, last_name, phone, country, stripe_customer_id, status, source))


def db_member_active(email):
    rows = _DB_READ("SELECT status FROM members WHERE email=%s LIMIT 1", (email,))
    return bool(rows) and rows[0].get("status") in ("active", "comp")


def is_active_member(email):
    if db_member_active(email):
        return True
    if email_has_active_subscription(email):
        upsert_member(email, status="active", source="stripe")
        return True
    return False


def get_member(email):
    rows = _DB_READ("SELECT * FROM members WHERE email=%s LIMIT 1", (email,))
    return rows[0] if rows else None
