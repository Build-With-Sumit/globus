"""END-TO-END test of the /api/globus/member-state privacy gate.

test_member_state.py tests the state logic directly. This one boots the real
`globus_server.Handler` on a socket, because the thing most likely to be wrong
is not the logic — it is whether the gate sits where the request actually flows.

The boundary under test: this endpoint names members and the accounts they have
connected, so unlike `/api/health?deep=1` it must never be reachable without a
session, and one member must never be able to read another's state.

  * signed out            -> 401
  * signed in, own state  -> 200
  * signed in, someone else's -> 404 (not 403: the existence of a roster is not
    something a regular member needs to learn, matching the admin console)
  * signed in, ?all=1     -> 404 for a normal member
  * the install owner     -> 200 for both
  * state unreadable      -> 503, never a cheerful empty answer

The database is an in-memory shim; the request path is what is under test.
Run with:  python tests/test_member_state_http.py
"""
import http.client
import json
import os
import sys
import threading
import types
from http.server import ThreadingHTTPServer

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(_HERE), "server"))

os.environ.setdefault("SESSION_SECRET", "b" * 64)
os.environ.setdefault("SITE", "https://example.com")
os.environ["EMAIL_API_KEY"] = ""
# The install owner. agents_runtime reads this at import time, so it must be set
# before globus_server pulls it in.
os.environ["AGENTS_OWNER_EMAIL"] = "owner@example.com"

OWNER = "owner@example.com"
MEMBER = "bob@example.com"
_FAIL = {"on": False}


def db_read(sql, params=()):
    if _FAIL["on"]:
        return None                      # fail-soft failure, as in prod
    if "FROM members" in sql and "status IN" in sql:
        return [{"email": OWNER}, {"email": MEMBER}]
    if "globus_vault_files" in sql:
        return [{"files": 10, "processed": 10, "skipped": 0}]
    if "globus_intelligence" in sql:
        return [{"digest_char_count": 40000, "built_at": None,
                 "built_with": "sonnet"}]
    return []


_dbh = types.ModuleType("db_helpers")
_dbh.db_read = db_read
_dbh.db_write = lambda sql, params=(): True
_dbh.db_insert = lambda sql, params=(): 1
_dbh.cfg = lambda k, d="": {"AGENTS_OWNER_EMAIL": OWNER}.get(k, d)
_dbh.configure = lambda **kw: None
sys.modules["db_helpers"] = _dbh

import globus_server as G           # noqa: E402
from auth_cookies import make_cookie  # noqa: E402

PASS, FAIL = [], []


def check(label, ok):
    (PASS if ok else FAIL).append(label)
    print(("  ok   " if ok else "  FAIL ") + label)


srv = ThreadingHTTPServer(("127.0.0.1", 0), G.Handler)
threading.Thread(target=srv.serve_forever, daemon=True).start()
PORT = srv.server_address[1]


def get(path, cookie=None):
    conn = http.client.HTTPConnection("127.0.0.1", PORT, timeout=10)
    headers = {"Host": "example.com"}
    if cookie:
        headers["Cookie"] = cookie
    conn.request("GET", path, headers=headers)
    r = conn.getresponse()
    raw = r.read().decode("utf-8", "replace")
    conn.close()
    try:
        return r.status, json.loads(raw)
    except ValueError:
        return r.status, raw


# The session cookie is bound to an exact host audience, so it must match the
# Host header these requests send.
HOST = "example.com"
member_ck = make_cookie(MEMBER, audience=HOST)
owner_ck = make_cookie(OWNER, audience=HOST)

print("privacy gate:")
status, _ = get("/api/globus/member-state")
check("signed out is 401, not a leak", status == 401)

status, body = get("/api/globus/member-state", member_ck)
check("a signed-in member gets their OWN state (200)",
      status == 200 and isinstance(body, dict)
      and body.get("email") == MEMBER)
check("...including what is blocking them", "blockers" in body)
check("...and a stage", bool(body.get("stage")))

status, _ = get(f"/api/globus/member-state?email={OWNER}", member_ck)
check("a member reading SOMEONE ELSE is 404, not 403 and not 200", status == 404)

status, _ = get("/api/globus/member-state?all=1", member_ck)
check("a member requesting the whole roster is 404", status == 404)

status, body = get("/api/globus/member-state?all=1", owner_ck)
check("the install owner CAN read the roster (200)",
      status == 200 and isinstance(body.get("members"), list))
check("...and it covers the members", len(body.get("members") or []) == 2)

status, body = get(f"/api/globus/member-state?email={MEMBER}", owner_ck)
check("the owner can read one member by address",
      status == 200 and body.get("email") == MEMBER)

status, _ = get(f"/api/globus/member-state?email={MEMBER.upper()}", member_ck)
check("case does not smuggle a member past their own-email check",
      status == 200)

print("\nunreadable state:")
_FAIL["on"] = True
status, body = get("/api/globus/member-state", member_ck)
check("an unreadable database is 503, NEVER a cheerful empty state",
      status == 503)
check("...and says so", "unavailable" in json.dumps(body).lower())
_FAIL["on"] = False

print("\nthe shallow probe is untouched:")
status, body = get("/api/health")
check("/api/health stays open and cheap for load balancers",
      status == 200 and body.get("ok") is True)
check("...and does NOT carry member data", "members" not in json.dumps(body))

srv.shutdown()

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
if FAIL:
    for f in FAIL:
        print("  FAILED: " + f)
    sys.exit(1)
print("member-state endpoint invariants hold.")
