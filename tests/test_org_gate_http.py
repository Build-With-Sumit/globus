"""END-TO-END test of the org host gate over a real HTTP server.

test_org_gate.py calls the handler methods directly. This one boots the actual
`globus_server.Handler` on a real socket and sends real requests, because the
things most likely to be wrong are the things a direct call skips: whether the
module even imports and configures in order, whether the gate is placed where
the request actually flows, whether cookies and status codes come back as
intended, and whether a normal single-tenant host is genuinely untouched.

The database is an in-memory shim — the DB is not what's under test here, the
request path is. No network: the only socket is a loopback listener.

Run with:  python tests/test_org_gate_http.py
"""
import base64
import hashlib
import hmac
import http.client
import json
import os
import sys
import threading
import time
import types
from http.server import ThreadingHTTPServer

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(_HERE), "server"))

os.environ.setdefault("SESSION_SECRET", "a" * 64)
os.environ.setdefault("SITE", "https://example.com")
os.environ.setdefault("ORG_GOOGLE_LOGIN_ENABLED", "0")
# FORCE the dev OTP path. This test drives the real sign-in flow, so on an
# install that has a mail provider configured it would otherwise send actual
# email to the fixture addresses. Assigned, not setdefault: it must win over
# whatever is in .env.
os.environ["EMAIL_API_KEY"] = ""

# ── in-memory DB shim, installed BEFORE globus_server imports ────────────
ORGS = [{"id": 7, "slug": "acme", "name": "Acme Inc",
         "portal_host": "globus.acme.com", "status": "active"},
        {"id": 8, "slug": "gone", "name": "Gone Ltd",
         "portal_host": "globus.gone.com", "status": "suspended"}]
DOMAINS = {"acme.com": 7}
MEMBERS = {("bob@acme.com", 7): {"role": "employee", "department": "sales"},
           ("boss@acme.com", 7): {"role": "admin", "department": "sales"}}
WRITES = []
CODE_ROWS = []          # filled in below, once _code_hash is importable


def db_read(sql, params=()):
    p = params or ()
    if "FROM organizations" in sql:
        if "portal_host" in sql:
            return [o for o in ORGS if o["portal_host"] == p[0]]
        return [o for o in ORGS if o["slug"] == p[0] and o["status"] == "active"]
    if "FROM org_domains" in sql:
        oid = DOMAINS.get((p[0] or "").lower())
        return [{"org_id": oid}] if oid else []
    if "FROM org_members" in sql:
        rec = MEMBERS.get((p[1], p[0])) if len(p) > 1 else None
        if not rec:
            return []
        if "SELECT 1" in sql:
            return [{"ok": 1}]
        if "SELECT role" in sql:
            return [{"role": rec["role"]}]
        if "SELECT department" in sql:
            return [{"department": rec["department"]}]
        return [rec]
    if "FROM auth_codes" in sql:
        if "COUNT(*)" in sql:
            return [{"c": 0}]
        return [r for r in CODE_ROWS if r["email"] == p[0]]
    return []


def db_write(sql, params=()):
    WRITES.append((sql, params))
    if "INSERT INTO org_members" in sql:
        MEMBERS[(params[1], params[0])] = {"role": params[2],
                                           "department": params[3]}
    return True


_dbh = types.ModuleType("db_helpers")
_dbh.db_read = db_read
_dbh.db_write = db_write
_dbh.cfg = lambda k, d="": os.environ.get(k, d)
_dbh.configure = lambda **kw: None
sys.modules["db_helpers"] = _dbh

import globus_server as G       # noqa: E402  — the real thing
import auth_cookies as auth_cookie_module  # noqa: E402
from globus_auth import _code_hash  # noqa: E402
from auth_cookies import make_cookie  # noqa: E402

CODE_ROWS.append({"id": 1, "email": "bob@acme.com",
                  "code_hash": _code_hash("123456"), "used_at": None})

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  ok   " if cond else "  FAIL ") + name
          + (f"   [{detail}]" if detail and not cond else ""))


# ── boot the real server ────────────────────────────────────────────────
srv = ThreadingHTTPServer(("127.0.0.1", 0), G.Handler)
PORT = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()
print(f"server up on 127.0.0.1:{PORT}\n")


def req(
    method,
    path,
    host="localhost",
    cookie=None,
    body=None,
    content_type=None,
):
    c = http.client.HTTPConnection("127.0.0.1", PORT, timeout=10)
    headers = {"Host": host}
    if cookie:
        headers["Cookie"] = cookie
    if body is not None:
        headers["Content-Type"] = (
            content_type or "application/x-www-form-urlencoded"
        )
    c.request(method, path, body=body, headers=headers)
    r = c.getresponse()
    data = r.read().decode("utf-8", "replace")
    out = (r.status, data, r.getheader("Set-Cookie") or "",
           r.getheader("Location") or "")
    c.close()
    return out


ORG_HOST, DEAD_HOST, PLAIN = "globus.acme.com", "globus.gone.com", "localhost"


def legacy_cookie(email):
    payload = email + "|" + str(int(time.time()) + 60)
    mac = hmac.new(
        auth_cookie_module._SESSION_SECRET,
        payload.encode(),
        hashlib.sha256,
    ).hexdigest()
    token = (
        base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")
        + "."
        + mac
    )
    return "bws_member=" + token


print("the server boots and answers at all:")
st, body, _, _ = req("GET", "/api/health", host=PLAIN)
check("single-tenant /api/health is 200", st == 200, f"got {st}")

print("\nsingle-tenant host is UNTOUCHED by the gate:")
st, body, _, _ = req("GET", "/", host=PLAIN)
check("plain host serves the public landing", st == 200 and "Sign in to" not in body,
      f"{st}")
st, body, _, loc = req("GET", "/members/globus", host=PLAIN)
check("plain host still redirects an anonymous member to login",
      st in (302, 303) and "/members/login" in loc, f"{st} {loc}")

print("\norg host — pre-auth:")
st, body, _, _ = req("GET", "/", host=ORG_HOST)
check("org host serves the ORG login, not the public landing",
      st == 200 and "Sign in to" in body and "Acme Inc" in body, f"{st}")
st, body, _, _ = req("GET", "/privacy", host=ORG_HOST)
check("/privacy is served pre-auth (Google consent links it)",
      st == 200 and "Acme Inc" in body, f"{st}")
check("...and carries no leaked operator identity",
      "globussoft" not in body.lower())
st, body, _, _ = req("GET", "/terms", host=ORG_HOST)
check("/terms is served pre-auth", st == 200)
st, body, _, _ = req("GET", "/api/globus/vault-progress", host=ORG_HOST)
check("an unauthenticated API call gets 401", st == 401, f"{st}")

print("\norg host — the isolation property (real requests):")
# NOTE: /members/globus/agents is deliberately NOT in this list — it is an
# allow-listed org route (grant-filtered; see the shared-agents section below).
for leak in ("/members/narada", "/members/globus/setup",
             "/members/vault-progress", "/members/telegram/bot",
             "/members/whatsapp", "/members/globus/upload"):
    st, body, _, _ = req("GET", leak, host=ORG_HOST,
                         cookie=make_cookie(
                             "bob@acme.com", audience=ORG_HOST))
    check(f"{leak} 404s on an org host", st == 404, f"got {st}")

print("\norg host — authenticated:")
ck = make_cookie("bob@acme.com", audience=ORG_HOST)
st, body, _, _ = req("GET", "/", host=ORG_HOST, cookie=ck)
check("an active member gets the org home", st == 200 and "Welcome" in body,
      f"{st}")
check("...and it is NOT the single-tenant members landing",
      "Reels" not in body and "Narada" not in body)

st, body, _, _ = req(
    "GET",
    "/",
    host=ORG_HOST,
    cookie=legacy_cookie("bob@acme.com"),
)
check(
    "a valid-HMAC legacy unbound session fails closed",
    st == 200 and "Sign in to" in body,
    f"{st}",
)

cross_host_calls = []
real_agent_run_async = G.agent_run_async
G.agent_run_async = lambda name, email: cross_host_calls.append((name, email))
try:
    st, _, _, loc = req(
        "POST",
        "/members/globus/agents/run",
        host=PLAIN,
        cookie=ck,
        body="agent=infra-watch",
    )
finally:
    G.agent_run_async = real_agent_run_async
check(
    "an org session replayed to the plain host cannot run an agent",
    st == 401 and not cross_host_calls,
    f"{st} {loc} {cross_host_calls!r}",
)

st, body, _, _ = req("GET", "/members/globus/chat", host=ORG_HOST, cookie=ck)
check("chat page renders", st == 200)
st, body, _, _ = req("GET", "/members/connect", host=ORG_HOST, cookie=ck)
check("connect page renders", st == 200)
st, body, _, _ = req("GET", "/members/globus/admin", host=ORG_HOST, cookie=ck)
check("a NON-admin gets 404 on the admin console", st == 404, f"{st}")
st, body, _, _ = req("GET", "/members/globus/admin", host=ORG_HOST,
                     cookie=make_cookie(
                         "boss@acme.com", audience=ORG_HOST))
check("an admin gets the console", st == 200 and "Sharing" in body, f"{st}")

print("\norg host — a session from another surface:")
st, body, setck, _ = req("GET", "/", host=ORG_HOST,
                         cookie=make_cookie(
                             "outsider@example.com", audience=ORG_HOST))
check("a non-member's valid cookie does NOT authenticate them here",
      st == 200 and "Sign in to" in body, f"{st}")
check(
    "...and the stale cookie is cleared",
    setck.startswith("bws_member=;") and "Max-Age=0" in setck,
    f"set-cookie={setck!r}",
)

print("\norg host — DENY (suspended / unresolvable):")
st, body, _, _ = req("GET", "/", host=DEAD_HOST)
check("a suspended org DENIES with 503", st == 503, f"{st}")
check("...and never falls through to the single-tenant site",
      "unavailable" in body.lower() and "Sign in to" not in body)
st, body, _, _ = req("GET", "/members/narada", host=DEAD_HOST)
check("every path on a denied host is a dead end", st == 503, f"{st}")

for malformed_host in ("[::1]evil", "[::1]:notaport", "[::1]:8090:extra"):
    st, body, _, _ = req("GET", "/", host=malformed_host)
    check(
        f"malformed Host {malformed_host!r} fails closed",
        st == 503 and "unavailable" in body.lower(),
        f"{st}",
    )

print("\norg host — sign-in (POST):")
WRITES.clear()
st, body, _, _ = req("POST", "/members/login", host=ORG_HOST,
                     body="email=eve%40evil.com")
issued = [w for w in WRITES if "INSERT INTO auth_codes" in w[0]]
check("an UNREGISTERED domain gets no code", not issued)
check("...and an identical-looking response (no tenant enumeration)",
      st == 200 and "Check your email" in body, f"{st}")

WRITES.clear()
st, body, _, _ = req("POST", "/members/login", host=ORG_HOST,
                     body="email=bob%40acme.com")
check("a registered domain does get a code",
      any("INSERT INTO auth_codes" in w[0] for w in WRITES))

st, body, setck, loc = req("POST", "/members/verify", host=ORG_HOST,
                           body="email=bob%40acme.com&code=123456")
check("a good code redirects into the portal",
      st in (302, 303) and "/members/globus" in loc, f"{st} {loc}")
check(
    "...and sets a hardened session cookie",
    setck.startswith("bws_member=")
    and "; HttpOnly" in setck
    and "; Secure" in setck
    and "; SameSite=Lax" in setck,
)
same_status, same_body, _, _ = req(
    "GET",
    "/",
    host=ORG_HOST,
    cookie=setck,
)
cross_status, _, _, cross_location = req(
    "GET",
    "/members/globus",
    host=PLAIN,
    cookie=setck,
)
check(
    "...and that minted cookie works only on its issuing host",
    same_status == 200
    and "Welcome" in same_body
    and cross_status in (302, 303)
    and "/members/login" in cross_location,
)

st, body, _, _ = req("POST", "/members/verify", host=ORG_HOST,
                     body="email=bob%40acme.com&code=999999")
check("a wrong code does not sign you in", st == 200 and "wrong" in body.lower(),
      f"{st}")

st, body, _, _ = req("POST", "/members/verify", host=ORG_HOST,
                     body="email=eve%40evil.com&code=123456")
check("an unregistered domain cannot verify even with a real code",
      st == 200 and "wrong" in body.lower(), f"{st}")

print("\norg host — admin writes are admin-only (POST):")
st, _, _, _ = req("POST", "/members/globus/admin/grant", host=ORG_HOST,
                  cookie=ck, body="agent=research&audience=all%3A")
check("a non-admin cannot grant (404)", st == 404, f"{st}")
st, _, _, loc = req("POST", "/members/globus/admin/grant", host=ORG_HOST,
                    cookie=make_cookie(
                        "boss@acme.com", audience=ORG_HOST),
                    body="agent=research&audience=all%3A")
check("an admin can grant", st in (302, 303), f"{st}")
st, _, _, _ = req("POST", "/members/narada/credentials/save", host=ORG_HOST,
                  cookie=ck, body="x=1")
check("a single-tenant POST route 404s on an org host", st == 404, f"{st}")

print("\norg host — shared agents (grant-filtered):")
GRANTS = []          # rows returned for org_agent_grants


def _db_read_with_grants(sql, params=()):
    if "FROM org_agent_grants" in sql:
        return list(GRANTS)
    return db_read(sql, params)


_dbh.db_read = _db_read_with_grants
import org_db as _org_db  # noqa: E402
_org_db.configure(db_read=_db_read_with_grants, db_write=db_write)

# nothing granted yet
st, body, _, _ = req("GET", "/members/globus/agents", host=ORG_HOST, cookie=ck)
check("with no grants the employee gets an explanatory page, not an empty one",
      st == 200 and "No shared agents yet" in body, f"{st}")
st, body, _, _ = req("GET", "/", host=ORG_HOST, cookie=ck)
check("...and the home page hides the Agents link",
      st == 200 and "/members/globus/agents" not in body)

# the real catalog's first agent, granted to everyone
AGENT_SLUG = (G.GLOBUS_AGENTS_CATALOG[0] or {}).get("name")
GRANTS.append({"agent_slug": AGENT_SLUG})
st, body, _, _ = req("GET", "/members/globus/agents", host=ORG_HOST, cookie=ck)
check("a granted agent appears on the dashboard",
      st == 200 and AGENT_SLUG in body, f"{st}")
st, body, _, _ = req("GET", "/", host=ORG_HOST, cookie=ck)
check("...and the home page now shows the Agents link",
      "/members/globus/agents" in body)

print("  — the run route is access control, not decoration:")
st, _, _, loc = req("POST", "/members/globus/agents/run", host=ORG_HOST,
                    cookie=ck, body=f"agent={AGENT_SLUG}")
check("an employee CAN run an agent granted to them",
      st in (302, 303), f"{st}")

print("  - org grants also reach the chat run_agent dispatcher:")
CHAT_CALLS = []
_real_chat_send = G.globus_chat_send


def _capture_chat(email, message, **kwargs):
    CHAT_CALLS.append((email, message, kwargs))
    return "ok", {}


G.globus_chat_send = _capture_chat
try:
    st, _, _, _ = req(
        "POST",
        "/members/globus/chat",
        host=ORG_HOST,
        cookie=ck,
        body=json.dumps({"message": "run my shared agent"}),
        content_type="application/json",
    )
finally:
    G.globus_chat_send = _real_chat_send
check(
    "org chat passes the employee's exact agent grants into dispatch",
    st == 200
    and len(CHAT_CALLS) == 1
    and CHAT_CALLS[0][2].get("allowed_agent_names") == {AGENT_SLUG},
    f"{st} {CHAT_CALLS!r}",
)

ungranted = None
for a in G.GLOBUS_AGENTS_CATALOG[1:]:
    if a.get("name") and a["name"] != AGENT_SLUG:
        ungranted = a["name"]
        break
if ungranted:
    st, _, _, _ = req("POST", "/members/globus/agents/run", host=ORG_HOST,
                      cookie=ck, body=f"agent={ungranted}")
    check("posting an UNGRANTED slug directly is refused (404), not run",
          st == 404, f"{st}")
    st, body, _, _ = req("GET", "/members/globus/agents", host=ORG_HOST,
                         cookie=ck)
    check("...and it never appears on their dashboard",
          ungranted not in body)

# admins see the whole catalog without granting it to themselves
GRANTS.clear()
st, body, _, _ = req("GET", "/members/globus/agents", host=ORG_HOST,
                     cookie=make_cookie(
                         "boss@acme.com", audience=ORG_HOST))
check("an admin sees the catalog without a self-grant",
      st == 200 and AGENT_SLUG in body, f"{st}")
st, _, _, _ = req("POST", "/members/globus/agents/run", host=ORG_HOST,
                  cookie=make_cookie(
                      "boss@acme.com", audience=ORG_HOST),
                  body=f"agent={AGENT_SLUG}")
check("an admin can run it", st in (302, 303), f"{st}")

# a non-member still gets nothing, even with grants present
GRANTS.append({"agent_slug": AGENT_SLUG})
st, body, _, _ = req("GET", "/members/globus/agents", host=ORG_HOST,
                     cookie=make_cookie(
                         "outsider@example.com", audience=ORG_HOST))
check("a non-member cannot reach the agents page at all",
      st == 200 and "Sign in to" in body, f"{st}")
st, _, _, _ = req("POST", "/members/globus/agents/run", host=ORG_HOST,
                  cookie=make_cookie(
                      "outsider@example.com", audience=ORG_HOST),
                  body=f"agent={AGENT_SLUG}")
check("a non-member cannot run anything",
      st in (302, 303, 401, 404), f"{st}")

srv.shutdown()
print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
if FAIL:
    for f in FAIL:
        print("  FAILED: " + f)
    sys.exit(1)
print("org portal verified over real HTTP.")
