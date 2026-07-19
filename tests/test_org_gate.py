"""Behavioural tests for the org-host request gate in globus_server.

The property under test is the isolation one: on an org host the request is
served ONLY by the org plane. A route that is not explicitly allow-listed must
404 — it must never fall through and serve the single-tenant site — and an
unresolvable org must dead-end rather than leak it.

Runs the REAL Handler methods with stubbed I/O (no socket, no DB, no network).
Run with:  python tests/test_org_gate.py
"""
import os
import sys
from urllib.parse import urlparse

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(_HERE), "server"))

os.environ.setdefault("SESSION_SECRET", "0" * 64)
os.environ.setdefault("DB_HOST", "127.0.0.1")
os.environ.setdefault("DB_NAME", "globus")
os.environ.setdefault("DB_USER", "globus")
os.environ.setdefault("DB_PASSWORD", "x")

import globus_server as G  # noqa: E402

ORG = {"id": 7, "slug": "acme", "name": "Acme Inc"}
PASS, FAIL = [], []


def check(name, cond):
    (PASS if cond else FAIL).append(name)
    print(("  ok   " if cond else "  FAIL ") + name)


class Stub(G.Handler):
    """A Handler with every I/O primitive replaced, so the real routing code
    runs but nothing touches a socket."""

    def __init__(self, cookie_email=None, form=None):
        self._cookie_email = cookie_email
        self._form_data = form or {}
        self.sent = None

    # stubbed I/O
    def _send_html(self, code, body, extra_headers=None):
        self.sent = ("html", code, body, extra_headers or [])
        return self.sent

    def _send_json(self, code, obj, extra_headers=None):
        self.sent = ("json", code, obj, extra_headers or [])
        return self.sent

    def _redirect(self, location, extra_headers=None):
        self.sent = ("redirect", 302, location, extra_headers or [])
        return self.sent

    def _member_email(self):
        return self._cookie_email

    def _form(self):
        return self._form_data


def get(route, cookie_email=None, org=ORG, query=""):
    h = Stub(cookie_email=cookie_email)
    return h, h._org_do_GET(org, urlparse(route + (("?" + query) if query else "")), route)


def post(route, cookie_email=None, form=None, org=ORG):
    h = Stub(cookie_email=cookie_email, form=form)
    return h, h._org_do_POST(org, urlparse(route), route)


# ── stub out everything the gate reads, so decisions are deterministic ────
_state = {"member": True, "admin": False, "domain_ok": True, "codes": [],
          "enrolled": [], "verify_ok": True}

G.cfg = lambda k, d="": {"ORG_GOOGLE_LOGIN_ENABLED": "1"}.get(k, d)
G.org_member_active = lambda e, o: bool(e) and _state["member"]
G.is_org_admin = lambda e, o: _state["admin"]
G.domain_matches_org = lambda e, o: _state["domain_ok"]
G.request_org_code = lambda e, o: _state["codes"].append(e) or True
G.verify_code = lambda e, c: _state["verify_ok"]
G.auto_enroll = lambda e, o, d: _state["enrolled"].append(e) or True
G.list_org_members = lambda o: []
G.list_grants = lambda o: []
G.list_oauth_connections_with_stats = lambda e: []
G.make_cookie = lambda e: "bws_member=stub"


# ── GET: the deny-by-default plane ───────────────────────────────────────
print("GET — deny-by-default:")

_, r = get("/", org=G.ORG_DENY)
check("unresolvable org DENIES with 503 (never falls through)",
      r[0] == "html" and r[1] == 503 and "unavailable" in r[2].lower())

_, r = get("/styles.css")
check("shared static asset falls through (sentinel False)", r is False)

_state["member"] = False
_, r = get("/")
check("unauthenticated employee gets the org login page",
      r[0] == "html" and r[1] == 200 and "Sign in to" in r[2])

_, r = get("/api/globus/vault-progress")
check("unauthenticated API call gets 401 JSON", r[0] == "json" and r[1] == 401)

h, r = get("/", cookie_email="outsider@example.com")
check("a session from another surface is NOT an org identity, and is cleared",
      r[1] == 200 and any("Set-Cookie" == k for k, _ in r[3]))

_state["member"] = True
print("GET — authenticated surfaces:")

_, r = get("/", cookie_email="bob@acme.com")
check("home renders", r[0] == "html" and r[1] == 200 and "Welcome" in r[2])

_, r = get("/members/globus/chat", cookie_email="bob@acme.com")
check("chat page renders", r[0] == "html" and r[1] == 200)

_, r = get("/members/connect", cookie_email="bob@acme.com")
check("connect page renders", r[0] == "html" and r[1] == 200)

_, r = get("/members/globus/admin", cookie_email="bob@acme.com")
check("NON-admin gets 404 on the admin console (not 403 — unlisted)",
      r[0] == "html" and r[1] == 404)

_state["admin"] = True
_, r = get("/members/globus/admin", cookie_email="a@acme.com")
check("admin gets the console", r[0] == "html" and r[1] == 200)
_state["admin"] = False

print("GET — the isolation property:")
# `/members` is an allow-listed ALIAS for the org home, so it must be served by
# the org plane (never the single-tenant members landing).
_, r = get("/members", cookie_email="bob@acme.com")
check("/members is the ORG home, not the single-tenant landing",
      r is not False and r[1] == 200 and "Welcome" in r[2])

for leak in ("/members/narada", "/members/globus/agents",
             "/members/globus/setup", "/members/vault-progress",
             "/api/globus/agent-status", "/members/telegram/bot",
             "/members/whatsapp", "/members/globus/upload", "/admin"):
    _, r = get(leak, cookie_email="bob@acme.com")
    check(f"single-tenant route {leak} 404s on an org host (no fall-through)",
          r is not False and r[1] == 404)

_, r = get("/members/connect/google/callback", cookie_email="bob@acme.com")
check("allow-listed shared route falls through for an authed member", r is False)


# ── POST ─────────────────────────────────────────────────────────────────
print("POST — sign-in:")

_state["codes"].clear()
_, r = post("/members/login", form={"email": "not-an-email"})
check("invalid email re-renders login, sends nothing",
      r[1] == 200 and not _state["codes"])

_state["domain_ok"] = True
_, r = post("/members/login", form={"email": "bob@acme.com"})
check("registered domain gets a code", _state["codes"] == ["bob@acme.com"])

_state["codes"].clear()
_state["domain_ok"] = False
_, r = post("/members/login", form={"email": "eve@evil.com"})
check("UNREGISTERED domain: no code sent",
      not _state["codes"])
check("...and the response is identical (no account/tenant enumeration)",
      r[0] == "html" and r[1] == 200 and "Check your email" in r[2])

print("POST — verify:")
_state["enrolled"].clear()
_state["domain_ok"] = False
_, r = post("/members/verify", form={"email": "eve@evil.com", "code": "123456"})
check("verify re-asserts the domain — unregistered is refused, nothing enrolled",
      r[1] == 200 and not _state["enrolled"])

_state["domain_ok"] = True
_state["verify_ok"] = False
_, r = post("/members/verify", form={"email": "bob@acme.com", "code": "123456"})
check("a wrong code enrolls nothing", not _state["enrolled"])

_state["verify_ok"] = True
_, r = post("/members/verify", form={"email": "bob@acme.com", "code": "123456"})
check("a good code enrolls + sets the session cookie",
      r[0] == "redirect" and _state["enrolled"] == ["bob@acme.com"]
      and any(k == "Set-Cookie" for k, _ in r[3]))

print("POST — admin + isolation:")
_state["admin"] = False
_, r = post("/members/globus/admin/grant", cookie_email="bob@acme.com",
            form={"agent": "researcher", "audience": "all:"})
check("NON-admin cannot grant (404)", r[1] == 404)

_, r = post("/members/narada/credentials/save", cookie_email="bob@acme.com", form={})
check("single-tenant POST route 404s on an org host", r is not False and r[1] == 404)

_, r = post("/members/globus/chat", cookie_email="bob@acme.com", form={})
check("allow-listed shared POST falls through", r is False)

_, r = post("/", org=G.ORG_DENY)
check("POST on an unresolvable org DENIES (503)", r[1] == 503)

# ── the gate must be a NO-OP for a normal single-tenant install ──────────
# This runs on every request, so the biggest regression risk of the whole
# feature is it accidentally intercepting a plain install.
print("host resolution — single-tenant must be untouched:")


class HostStub(Stub):
    def __init__(self, host):
        super().__init__()
        self.headers = {"Host": host}


for raw, want in (("globus.acme.com", "globus.acme.com"),
                  ("globus.acme.com:8090", "globus.acme.com"),
                  ("EXAMPLE.COM", "example.com"),
                  ("[::1]:8090", "[::1]"),
                  ("", "")):
    check(f"_req_host({raw!r}) -> {want!r}", HostStub(raw)._req_host() == want)

_orig = G.org_for_host

# The org tables may not even exist on a plain install: org_for_host raising
# must leave the request on the single-tenant path, not deny it.
G.org_for_host = lambda h: (_ for _ in ()).throw(RuntimeError("no org tables"))
G.cfg = lambda k, d="": d          # ORG_PORTAL_HOSTS unset
check("org lookup failure on a plain host stays single-tenant (None)",
      HostStub("example.com")._org_for_req() is None)

# ...but a host the operator NAMED as an org portal must still deny.
G.cfg = lambda k, d="": ("globus.acme.com:acme" if k == "ORG_PORTAL_HOSTS" else d)
check("org lookup failure on a NAMED org host denies (fail closed)",
      HostStub("globus.acme.com")._org_for_req() is G.ORG_DENY)

G.org_for_host = lambda h: None
check("a plain host resolves to None -> gate no-ops",
      HostStub("example.com")._org_for_req() is None)

G.org_for_host = _orig

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
if FAIL:
    for f in FAIL:
        print("  FAILED: " + f)
    sys.exit(1)
print("org host gate holds: allow-list only, deny by default.")
