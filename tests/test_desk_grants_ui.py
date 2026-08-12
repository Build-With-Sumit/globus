"""Behavioural tests for the shared-inbox grant matrix in the admin console.

The renderer is pure, but the WRITE path is an authorization boundary and is
tested as one:

  * the mailbox is validated against the CONFIGURED desks, never trusted from
    the form — otherwise an admin could post any address and start the agents
    reading (and, for the rescue agent, moving) a mailbox nobody made a desk,
  * an unknown agent key is refused rather than written,
  * each toggle submits a DESIRED END STATE, not "flip it", so a double-submit
    or two admins acting at once cannot switch an agent on by accident,
  * the section disappears entirely when no desks are configured, and a broken
    desk lookup degrades to no section rather than a 500 in the admin console.

Hermetic: no MySQL, no network.
Run with:  python tests/test_desk_grants_ui.py
"""
import os
import sys
import types

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(_HERE), "server"))

os.environ.setdefault("SESSION_SECRET", "0" * 64)
os.environ.setdefault("DB_HOST", "127.0.0.1")
os.environ.setdefault("DB_NAME", "globus")
os.environ.setdefault("DB_USER", "globus")
os.environ.setdefault("DB_PASSWORD", "x")

PASS, FAIL = [], []


def check(label, ok):
    (PASS if ok else FAIL).append(label)
    print(("  ok   " if ok else "  FAIL ") + label)


# ── the pure renderer ────────────────────────────────────────────────────
import org_portal_html as O           # noqa: E402

AGENTS = [("spam_rescue", "Spam rescue"), ("responder", "Responder"),
          ("followup", "Follow-up"), ("learning", "Learning")]

print("renderer:")
check("no desks renders NOTHING, not an empty section",
      O.org_desk_grants_html([], AGENTS) == "")

html = O.org_desk_grants_html(
    [{"mailbox": "support@acme.example", "product": "Acme",
      "owner": "staff@acme.example", "granted": ["responder"]}], AGENTS)
check("the desk is shown", "support@acme.example" in html)
check("a granted agent renders ON", ">ON<" in html)
check("an ungranted agent renders off", ">off<" in html)
check("every agent gets a control", html.count("name=\"agent\"") == 4)
check("the granted one submits enabled=0 (turn it OFF next)",
      "value=\"responder\"" in html
      and html.split("value=\"responder\"")[1].split("</form>")[0].count("value=\"0\"") == 1)
check("an ungranted one submits enabled=1 (turn it ON next)",
      html.split("value=\"followup\"")[1].split("</form>")[0].count("value=\"1\"") == 1)
check("it posts to the admin desk-agent action",
      "/members/globus/admin/desk-agent" in html)

evil = O.org_desk_grants_html(
    [{"mailbox": "<script>alert(1)</script>@x.example", "product": "\"><b>",
      "owner": "", "granted": []}], AGENTS)
check("desk fields are HTML-escaped", "<script>" not in evil and "&lt;script&gt;" in evil)


# ── the write path is an authorization boundary ──────────────────────────
print("\nwrite path:")
import globus_server as G             # noqa: E402

_writes = []
_fake = types.ModuleType("email_desks")
_fake.DESK_AGENTS = ("spam_rescue", "followup", "responder", "learning")
_fake.configured_desks = lambda: [
    {"mailbox": "support@acme.example", "product": "Acme", "owner": "s@acme.example"}]
_fake.agent_enabled = lambda mb, a, default=False: a == "responder"
_fake.grant = lambda mb, a, enabled=True: _writes.append((mb, a, enabled))
sys.modules["email_desks"] = _fake

handler = G.Handler.__new__(G.Handler)

check("a configured desk + known agent is applied",
      handler._set_desk_grant("support@acme.example", "responder", True) is True
      and _writes[-1] == ("support@acme.example", "responder", True))

before = len(_writes)
check("a mailbox that is NOT a configured desk is REFUSED",
      handler._set_desk_grant("ceo@acme.example", "responder", True) is False)
check("...and nothing was written for it", len(_writes) == before)

check("an unknown agent key is refused",
      handler._set_desk_grant("support@acme.example", "rm_-rf", True) is False)
check("...still nothing written", len(_writes) == before)

check("turning one OFF is applied too",
      handler._set_desk_grant("support@acme.example", "followup", False) is True
      and _writes[-1] == ("support@acme.example", "followup", False))


# ── degradation ──────────────────────────────────────────────────────────
print("\ndegradation:")
check("the matrix renders for a configured desk",
      "support@acme.example" in (handler._desk_grants_html() or ""))

_fake.configured_desks = lambda: []
check("no desks -> no section in the admin console",
      handler._desk_grants_html() == "")


def _boom():
    raise RuntimeError("tables not migrated yet")


_fake.configured_desks = _boom
check("a broken desk lookup degrades to no section, never a 500",
      handler._desk_grants_html() == "")
check("...and the write path refuses rather than raising",
      handler._set_desk_grant("support@acme.example", "responder", True) is False)


# ── the admin page still renders without the feature ─────────────────────
print("\nadmin page:")
page = O.org_admin_html({"slug": "acme", "name": "Acme"}, "admin@acme.example",
                        [], [], [("research", "Research")], desk_html="")
check("an install with no desks gets no Shared inboxes heading",
      "Shared inboxes" not in page)
page = O.org_admin_html({"slug": "acme", "name": "Acme"}, "admin@acme.example",
                        [], [], [("research", "Research")],
                        desk_html=O.org_desk_grants_html(
                            [{"mailbox": "support@acme.example", "product": "Acme",
                              "owner": "", "granted": []}], AGENTS))
check("...and one with desks gets the matrix inside the console",
      "Shared inboxes" in page and "support@acme.example" in page)


print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
if FAIL:
    for f in FAIL:
        print("  FAILED: " + f)
    sys.exit(1)
print("desk-grant UI invariants hold.")
