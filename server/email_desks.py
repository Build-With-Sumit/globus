"""Shared inboxes — the desk substrate: discovery, grants, drafts, lessons.

WHAT THIS IS
------------
`email_intel.py` reasons over ONE operator's own mailboxes. This module covers
the other shape: a set of SHARED inboxes ("desks") — support@, sales@, billing@ —
each owned by a different member of staff, each needing the same automation, and
none of them belonging to the person who installed Globus.

A desk is `{mailbox, owner, product}`. Everything downstream is per-desk: the
grant that switches an agent on, the credential it runs under, the draft it
leaves for review, the beacon that proves it ran, and the lessons it learned from
that desk's own human. `desk_agents.py` holds the four agents built on this;
this file is the substrate they share so there is ONE implementation of each
primitive rather than four drifting copies.

DRAFT-ONLY, WITH ONE DELIBERATE EXCEPTION
-----------------------------------------
No agent here sends email. They compose into the desk's own Gmail Drafts and the
owner reviews and sends. The single exception is the spam rescue, which moves a
message SPAM → INBOX: it is the only mailbox mutation in the module, it is opt-in
per desk like everything else, and it is recoverable (the message moves back).

THREE PROPERTIES WORTH KNOWING BEFORE YOU EDIT
----------------------------------------------
1. **Discovery is live, never hardcoded.** Desks are resolved from
   `globus_oauth_connections` on every run, keyed by the connected address. Desk
   ownership churns — inboxes get handed between staff — and a hardcoded map goes
   stale silently, which reads as "the agent stopped working for that person".

2. **A grant is required per (desk, agent).** `agent_enabled()` defaults to
   False. A discover-live loop with a default of True bulk-onboards every
   connection the moment someone connects an unrelated mailbox, and the operator
   finds out from the bill. A NEW desk therefore starts dark until someone
   switches it on deliberately.

3. **An id you stored is a claim about the past, not a fact about the present.**
   See `delete_draft_if_unsent()`. This is the most expensive lesson in the file
   and the reason the destructive path is a function rather than a bare call.

Nothing here runs at import time. Call `db_helpers.configure(...)` first.
"""
from __future__ import annotations
import base64
import json
import os
import random
import re
import time
from datetime import datetime, timezone
from email.mime.text import MIMEText
from email.utils import formatdate, make_msgid
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from db_helpers import db_read, db_write
from email_intel import _cfg, _int, envflag, mailbox_token
from google_gmail import GMAIL_API, gmail_extract_body_text

# The four agents in desk_agents.py. These strings are the grant keys stored in
# `desk_agent_config.agent` — renaming one silently revokes every grant that
# used the old name, so they are constants rather than inline literals.
AGENT_SPAM_RESCUE = "spam_rescue"
AGENT_FOLLOWUP = "followup"
AGENT_RESPONDER = "responder"
AGENT_LEARNING = "learning"
DESK_AGENTS = (AGENT_SPAM_RESCUE, AGENT_FOLLOWUP, AGENT_RESPONDER, AGENT_LEARNING)

BEACON_PREFIX = "desk_agent_last_run"

# Google access tokens live about an hour. A desk loop can outlive that, so the
# session below re-fetches on this interval. See DeskSession.
TOKEN_TTL_SEC = 2400


# ─────────────────────────────────────────────────────────────────────
# Config
# ─────────────────────────────────────────────────────────────────────

def _csv(key, default=""):
    """A comma-separated config value → a list of lowercased, stripped items."""
    return [x.strip().lower() for x in str(_cfg(key, default)).split(",") if x.strip()]


def desk_owners():
    """Addresses whose connected mailboxes are treated as desks.

    Typically the staff who own the shared inboxes. Blank means "no desks", not
    "all mailboxes" — an empty allow-list must never resolve to the whole estate.
    Set DESK_OWNERS, or list the desks explicitly with DESK_MAILBOXES."""
    return _csv("DESK_OWNERS")


def product_map():
    """{domain: product name}. The desk's DOMAIN is the stable identity — inbox
    addresses and owners churn, `support@acme.example` stays Acme. Configure as
    JSON in DESK_PRODUCTS, e.g. {"acme.example": "Acme"}. An unmapped domain
    falls back to its first label, title-cased."""
    raw = _cfg("DESK_PRODUCTS", "")
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
        return {str(k).lower(): str(v) for k, v in parsed.items()} if isinstance(parsed, dict) else {}
    except (TypeError, ValueError):
        return {}


def own_domains():
    """Domains that are US. A sender at one of these is internal — never an
    external customer. Defaults to the domains in DESK_PRODUCTS plus anything
    added explicitly in DESK_OWN_DOMAINS."""
    return set(product_map()) | set(_csv("DESK_OWN_DOMAINS"))


def product_for(mailbox):
    domain = (mailbox or "").split("@")[-1].lower()
    return product_map().get(domain) or (domain.split(".")[0].title() if domain else "Support")


def lessons_dir():
    """Where the per-desk lesson files live.

    This must be a path the agents can WRITE. If your deployment publishes a
    read-only, scrubbed copy of a vault for agents to read, do not point this at
    it — the next publish silently discards every lesson written since the last
    one."""
    return _cfg("DESK_LESSONS_DIR", os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "desk_lessons"))


# `no-reply@`-style senders never want a reply and must never be nudged.
NOREPLY_RE = re.compile(
    r"(no-?reply|do-?not-?reply|mailer-daemon|postmaster|bounce|"
    r"notifications?|automated|donotreply)@", re.I)


# ─────────────────────────────────────────────────────────────────────
# Desk discovery
# ─────────────────────────────────────────────────────────────────────

def discover_desks(owners=None, mailboxes=None, exclude=None):
    """Every Gmail-scoped desk, as [{mailbox, owner, product}].

    Two ways to select: by OWNER (every mailbox those people have connected —
    the usual case, so adding a desk needs no code change) or by an explicit
    MAILBOXES allow-list, which wins when given. `exclude` removes addresses
    from either.

    Returns [] when nothing is configured. A caller must be able to tell "no
    desks are set up" from "every mailbox on the install" — so this never
    silently widens to the latter."""
    exclude = {e.strip().lower() for e in (exclude or []) if e and e.strip()}
    mailboxes = [m.strip().lower() for m in (mailboxes or []) if m and m.strip()]
    owners = [o.strip().lower() for o in (owners or []) if o and o.strip()]

    if mailboxes:
        placeholders = ",".join(["%s"] * len(mailboxes))
        rows = db_read(
            "SELECT provider_account, email FROM globus_oauth_connections "
            "WHERE provider='google' AND needs_reconnect=0 "
            "AND scopes LIKE '%%gmail%%' "
            f"AND LOWER(provider_account) IN ({placeholders}) "
            "ORDER BY provider_account", tuple(mailboxes)) or []
    elif owners:
        placeholders = ",".join(["%s"] * len(owners))
        rows = db_read(
            "SELECT provider_account, email FROM globus_oauth_connections "
            "WHERE provider='google' AND needs_reconnect=0 "
            "AND scopes LIKE '%%gmail%%' "
            f"AND LOWER(email) IN ({placeholders}) "
            "ORDER BY provider_account", tuple(owners)) or []
    else:
        return []

    desks, seen = [], set()
    for r in rows:
        account = (r.get("provider_account") or "").lower()
        if not account or account in exclude or account in seen:
            continue
        seen.add(account)
        desks.append({"mailbox": r["provider_account"],
                      "owner": r.get("email") or "",
                      "product": product_for(account)})
    return desks


def configured_desks():
    """The desks this install is configured for, honouring every knob."""
    return discover_desks(owners=desk_owners(),
                          mailboxes=_csv("DESK_MAILBOXES"),
                          exclude=_csv("DESK_EXCLUDE"))


# ─────────────────────────────────────────────────────────────────────
# Grants — one switch per (desk, agent)
# ─────────────────────────────────────────────────────────────────────

def agent_config(mailbox, agent):
    """The `desk_agent_config` row for this pair, or None."""
    rows = db_read(
        "SELECT enabled, model, settings_json FROM desk_agent_config "
        "WHERE mailbox=%s AND agent=%s LIMIT 1", (mailbox, agent))
    return rows[0] if rows else None


def agent_enabled(mailbox, agent, default=False):
    """Is `agent` switched on for `mailbox`?

    A MISSING row falls back to `default` (False for every agent shipped here).
    An EXISTING row with enabled=0 always wins, even where a caller passes
    default=True: an explicit opt-OUT must survive a later change of default,
    or turning a feature on by default silently overrides everyone who turned it
    off on purpose."""
    row = agent_config(mailbox, agent)
    if row is None:
        return bool(default)
    return bool(row.get("enabled"))


def agent_model(mailbox, agent, fallback):
    """Per-desk model override, else `fallback`."""
    row = agent_config(mailbox, agent) or {}
    return (row.get("model") or "").strip() or fallback


def grant(mailbox, agent, enabled=True, model=None):
    """Switch an agent on or off for a desk (what an admin UI writes)."""
    return db_write(
        "INSERT INTO desk_agent_config (mailbox, agent, enabled, model) "
        "VALUES (%s,%s,%s,%s) ON DUPLICATE KEY UPDATE "
        "enabled=VALUES(enabled), model=COALESCE(VALUES(model), model)",
        (mailbox, agent, 1 if enabled else 0, model))


# ─────────────────────────────────────────────────────────────────────
# Gmail access
# ─────────────────────────────────────────────────────────────────────

def gapi(token, method, path, params=None, body=None):
    """One call against `/gmail/v1/users/me/<path>`, returning parsed JSON.

    The narrowly-scoped write helpers in google_gmail.py are add-only by
    design, which is right for the labelling agents. The desk agents genuinely
    need drafts, threads and a Spam move, so they get this general client —
    kept here rather than widening the add-only surface everything else relies
    on."""
    url = f"{GMAIL_API}/users/me/{path}"
    if params:
        from urllib.parse import urlencode
        pairs = []
        for k, v in params.items():
            if isinstance(v, (list, tuple)):
                pairs.extend((k, str(i)) for i in v)
            else:
                pairs.append((k, str(v)))
        url += "?" + urlencode(pairs)
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Authorization": "Bearer " + token}
    if data:
        headers["Content-Type"] = "application/json"
    req = Request(url, data=data, method=method, headers=headers)
    with urlopen(req, timeout=30) as r:
        raw = r.read()
    return json.loads(raw.decode()) if raw else {}


class DeskSession:
    """A desk's access token, kept fresh across a long loop.

    Fetching once and reusing for the whole run is the obvious implementation
    and it fails in a way that is hard to read: a token lives about an hour, a
    busy desk takes longer, and every call after expiry returns 401. Because the
    401 lands inside a per-message `except`, each one is logged as an ordinary
    skip — so the run reports a tidy "nothing to do" while having processed
    nothing since the first hour. One incident of this shape ran for ~14 hours.

    Freshness is the caller's job, so it is held by an object the caller passes
    around rather than left to a module-level variable."""

    def __init__(self, mailbox, ttl=TOKEN_TTL_SEC):
        self.mailbox = mailbox
        self.ttl = ttl
        self._token, self.scopes = mailbox_token(mailbox)
        self._fetched = time.time()

    @property
    def token(self):
        if time.time() - self._fetched > self.ttl:
            try:
                self._token, self.scopes = mailbox_token(self.mailbox)
                self._fetched = time.time()
            except Exception:
                pass          # keep the old token; the next call surfaces a real error
        return self._token

    def can_modify(self):
        """Whether this desk's grant permits moving mail (the Spam rescue)."""
        s = self.scopes or ""
        return "gmail.modify" in s or "https://mail.google.com/" in s

    def can_draft(self):
        """Whether this desk's grant permits creating drafts.

        `gmail.send` is NOT enough: composing a draft needs compose or modify.
        A capability check is only valid against the requirement that existed
        when it was run — an install that moved from sending to drafting has to
        re-check, and usually has to re-consent."""
        s = self.scopes or ""
        return ("gmail.compose" in s or "gmail.modify" in s
                or "https://mail.google.com/" in s)


def decode_body(payload):
    """First text/plain part of a message (HTML stripped as a fallback)."""
    return gmail_extract_body_text(payload or {}) or ""


def bare_email(field):
    m = re.search(r"[\w.+-]+@[\w.-]+", field or "")
    return m.group(0) if m else ""


def from_domain(field):
    return (bare_email(field).split("@")[-1] or "").lower()


def is_external(sender, ours=None):
    """A sender to treat as an external correspondent — not us, not a robot."""
    if not sender:
        return False
    if NOREPLY_RE.search(sender):
        return False
    return from_domain(sender) not in (ours if ours is not None else own_domains())


def reply_subject(original):
    original = original or ""
    return original if original.lower().startswith("re:") else f"Re: {original}"


def thread_senders(token, thread_id):
    """[(epoch_ms, from_header)] for every NON-DRAFT message in a thread, oldest
    first. Drafts are excluded because our own pending draft would otherwise
    read as the newest message and make every thread look answered."""
    try:
        th = gapi(token, "GET", f"threads/{thread_id}",
                  {"format": "metadata", "metadataHeaders": ["From"]})
    except Exception:
        return []
    out = []
    for m in (th.get("messages") or []):
        if "DRAFT" in (m.get("labelIds") or []):
            continue
        headers = {h["name"].lower(): (h.get("value") or "")
                   for h in ((m.get("payload") or {}).get("headers") or [])}
        out.append((int(m.get("internalDate") or 0), headers.get("from", "")))
    out.sort(key=lambda x: x[0])
    return out


def customer_is_last_responder(token, thread_id):
    """True iff the newest non-draft message is EXTERNAL — the ball is ours.
    Fails safe: an unreadable thread returns False and is left alone."""
    msgs = thread_senders(token, thread_id)
    return bool(msgs) and is_external(msgs[-1][1])


def we_spoke_last(token, thread_id):
    """True iff the newest non-draft message is OURS — the inverse case, where a
    proactive nudge may be appropriate. Not simply `not customer_is_last_
    responder(...)`: an empty or unreadable thread must be False in BOTH
    directions rather than defaulting one of them to True."""
    msgs = thread_senders(token, thread_id)
    return bool(msgs) and not is_external(msgs[-1][1])


# ─────────────────────────────────────────────────────────────────────
# Spam rescue — the one mutation
# ─────────────────────────────────────────────────────────────────────

def rescue_from_spam(token, message_id):
    """Move one message SPAM → INBOX.

    Deliberately its own helper rather than a general "modify labels" call.
    Spam rescue is the only mutation the desk agents perform, and keeping it a
    named function with exactly one behaviour means no future caller can reach
    for a general mover and archive or trash something instead."""
    return gapi(token, "POST", f"messages/{message_id}/modify",
                body={"removeLabelIds": ["SPAM"], "addLabelIds": ["INBOX"]})


# ─────────────────────────────────────────────────────────────────────
# Drafts
# ─────────────────────────────────────────────────────────────────────

def create_draft(token, from_addr, to, subject, body, thread_id=None,
                 in_reply_to=None):
    """A threaded draft in this desk's own Drafts. No Cc: a reply-all cascade
    from an automated draft is hard to unwind and impossible to apologise for
    convincingly, so the address list stays exactly as narrow as it was."""
    msg = MIMEText(body, _charset="utf-8")
    msg["From"] = from_addr
    msg["To"] = to
    msg["Subject"] = subject
    msg["Message-ID"] = make_msgid(domain=from_addr.split("@")[-1])
    msg["Date"] = formatdate(localtime=False)
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
        msg["References"] = in_reply_to
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("ascii")
    message = {"raw": raw}
    if thread_id:
        message["threadId"] = thread_id
    return gapi(token, "POST", "drafts", body={"message": message})


def delete_draft_if_unsent(token, draft_id):
    """Delete a draft ONLY while it is still an unsent draft. True if deleted.

    🔴 THE MOST EXPENSIVE LESSON IN THIS MODULE. When a human sends one of our
    drafts FROM THE GMAIL WEB UI, Gmail keeps the draft id resolvable and
    re-points it at the now-SENT message. `DELETE /drafts/<id>` is documented as
    deleting "immediately and permanently" — it does not trash. So deleting that
    id destroys a reply that was really delivered to a customer, leaving no copy
    in Trash, in Sent, or in any mailbox the operator owns. The customer keeps
    their copy and keeps replying to a message that, on our side, no longer
    exists. It reads to staff as "the automation is eating my replies", and they
    are exactly right.

    🔴 THE API SEND PATH DOES NOT REPRODUCE IT — which is why this kind of bug
    survives review. `drafts.send` consumes the id, so a later DELETE 404s
    harmlessly, and a positive control driven through the API says the message
    survives. ONLY the human's UI send leaves the id alive. If you test this,
    test it by sending from the Gmail interface.

    So: re-read the object before destroying it. An id you stored is a claim
    about the past, not a fact about the present. Fails CLOSED — where we cannot
    PROVE the draft is unsent we do not delete, because a stale extra draft is a
    nuisance someone can bin in a second and a destroyed reply is gone."""
    if not draft_id:
        return False
    try:
        d = gapi(token, "GET", f"drafts/{draft_id}", {"format": "minimal"})
    except HTTPError as e:
        if e.code == 404:
            return False      # consumed by an API send, or already gone
        raise
    message = d.get("message") or {}
    labels = set(message.get("labelIds") or [])
    if not labels and message.get("id"):
        try:
            labels = set(gapi(token, "GET", f"messages/{message['id']}",
                              {"format": "minimal"}).get("labelIds") or [])
        except Exception:
            return False                  # cannot prove it is unsent → hands off
    if "SENT" in labels or "DRAFT" not in labels:
        return False                      # a human already sent it
    gapi(token, "DELETE", f"drafts/{draft_id}")
    return True


def upsert_thread_draft(token, from_addr, to, subject, body, thread_id,
                        in_reply_to, previous_draft_id=None):
    """Leave exactly ONE pending draft per thread.

    Two drafts on one thread is not a cosmetic problem: a human scanning their
    Drafts sends whichever they see first, and if the two were composed at
    different points in a negotiation they carry different numbers. Replacing
    goes through delete_draft_if_unsent(), never a bare delete."""
    if previous_draft_id:
        try:
            delete_draft_if_unsent(token, previous_draft_id)
        except Exception:
            pass              # a leftover draft is survivable; failing the new one is not
    return create_draft(token, from_addr, to, subject, body, thread_id, in_reply_to)


def withdraw_stale_drafts(session, mailbox):
    """Withdraw our pending draft on any thread a human has since answered.

    A human answering by hand makes our draft obsolete AND dangerous — sending
    it afterwards hands the customer a second, contradictory reply from the same
    desk.

    🔴 The test is THE LAST NON-DRAFT MESSAGE: if it is from us, the customer is
    not waiting and the draft should go. It is deliberately NOT "the thread
    contains any outbound from us", which is true of every ongoing conversation
    and would withdraw every live draft on the desk.

    🔴 And the trigger is not a sufficient guard on its own. "The last message is
    from us" is reached by two paths that look identical from here and demand
    opposite actions: the human REPLIED BY HAND (withdraw our unsent draft) or
    the human SENT OUR DRAFT (the id now points at their delivered reply —
    touching it destroys it). delete_draft_if_unsent() is what tells them apart,
    and rows in the second case are marked 'sent' rather than 'superseded' so
    the log stops claiming a withdrawal that never happened."""
    withdrawn = kept = 0
    rows = db_read(
        "SELECT thread_id, draft_id FROM desk_replies "
        "WHERE mailbox=%s AND draft_id IS NOT NULL AND draft_id<>'' "
        "AND (mode IS NULL OR mode='draft')", (mailbox,)) or []
    for r in rows:
        try:
            if not we_spoke_last(session.token, r["thread_id"]):
                continue                      # customer still waiting → draft is live
            pulled = delete_draft_if_unsent(session.token, r["draft_id"])
            db_write("UPDATE desk_replies SET mode=%s "
                     "WHERE mailbox=%s AND thread_id=%s",
                     ("superseded" if pulled else "sent", mailbox, r["thread_id"]))
            withdrawn += 1 if pulled else 0
            kept += 0 if pulled else 1
        except Exception:
            continue
    return withdrawn, kept


# ─────────────────────────────────────────────────────────────────────
# Heartbeats
# ─────────────────────────────────────────────────────────────────────

def beacon_key(agent, mailbox):
    """PER (agent, desk). One shared beacon lets a healthy desk mask a dead one,
    which is the exact false-negative a beacon exists to prevent, reintroduced
    one level up. `config.name` is VARCHAR(80); the authoritative pair is stored
    inside the JSON payload and read back from there."""
    return f"{BEACON_PREFIX}:{agent}:{mailbox}"[:80]


def stamp_beacon(agent, mailbox, note="", **counts):
    """Proof of life. Stamp on EVERY completed run, including the boring ones.

    A quiet desk that only stamps when it acts is reported down every day it has
    nothing to do — and a daily false alarm is not harmless. It teaches the
    operator to ignore the alert, which destroys the one mechanism that stops a
    digest from lying."""
    payload = json.dumps({
        "at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "agent": agent, "mailbox": mailbox, "note": note, **counts})
    return db_write(
        "INSERT INTO config (name, value) VALUES (%s, %s) "
        "ON DUPLICATE KEY UPDATE value=VALUES(value)",
        (beacon_key(agent, mailbox), payload))


def beacon_ages():
    """{(agent, mailbox): hours_since_last_run}. A missing pair never ran, which
    is a different state from "ran a long time ago" and must be shown as one."""
    out, now = {}, datetime.now(timezone.utc)
    rows = db_read("SELECT name, value FROM config WHERE name LIKE %s",
                   (BEACON_PREFIX + ":%",)) or []
    for r in rows:
        try:
            payload = json.loads(r["value"])
            dt = datetime.fromisoformat(payload["at"])
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            out[(payload.get("agent"), payload.get("mailbox"))] = max(
                0.0, (now - dt).total_seconds() / 3600.0)
        except Exception:
            continue                      # unparseable == no proof of life
    return out


def throttle(seconds=2.0, jitter=1.0):
    """A short, jittered pause between mailbox writes. Reputation, not rate
    limits: a burst of identical automated drafts in one second is a pattern
    providers notice."""
    time.sleep(seconds + random.uniform(0, jitter))


# ─────────────────────────────────────────────────────────────────────
# The lesson store
# ─────────────────────────────────────────────────────────────────────
# Plain markdown, one file per (desk, agent), because the humans who own these
# desks should be able to read and hand-edit what their agent believes. A row in
# a table nobody opens is not a feedback loop.

def _lesson_path(mailbox, agent):
    safe = re.sub(r"[^A-Za-z0-9._@-]", "_", (mailbox or "unknown"))
    return os.path.join(lessons_dir(), safe, f"{agent}.md")


def load_lessons(mailbox, agent, max_chars=2000):
    """This desk's lessons, for injection into that agent's prompt.

    Reads the TAIL when truncating so the freshest lessons survive. Fail-soft:
    a problem with the lesson store must never take the agent down with it —
    an agent with no lessons is the agent we shipped on day one."""
    try:
        with open(_lesson_path(mailbox, agent), encoding="utf-8") as fh:
            text = fh.read().strip()
        return ("…\n" + text[-max_chars:]) if len(text) > max_chars else text
    except Exception:
        return ""


def append_lesson(mailbox, agent, lesson, date_label=None, keep=60):
    """Append one dated lesson, bounding the file to the last `keep` bullets.

    Bounded because this file is read back into a prompt on every run: an
    unbounded lesson store quietly becomes an unbounded prompt, and the cost
    shows up as a slow drift nobody attributes to a feedback feature."""
    if not (lesson or "").strip():
        return False
    date_label = date_label or datetime.now(timezone.utc).date().isoformat()
    path = _lesson_path(mailbox, agent)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        try:
            with open(path, encoding="utf-8") as fh:
                existing = fh.read()
        except OSError:
            existing = ""
        bullets = [ln for ln in existing.splitlines() if ln.startswith("- ")]
        bullets.append(f"- [{date_label}] {lesson.strip()}")
        header = (f"# Learned corrections — {agent} @ {mailbox}\n\n"
                  "_Distilled from what the human actually did with this agent's "
                  "output. The agent reads these back into its prompt on every "
                  "run. You may hand-edit this file._\n")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(header + "\n" + "\n".join(bullets[-keep:]) + "\n")
        return True
    except Exception:
        return False


# ─────────────────────────────────────────────────────────────────────
# Shared LLM plumbing
# ─────────────────────────────────────────────────────────────────────

def content_of(response):
    """Assistant text from a chat completion.

    `.get("content") or ""`, never `.get("content", "")`: a model may return an
    explicit null content, which the default-argument form passes straight
    through as None for the caller to trip over."""
    if not response:
        return ""
    choices = response.get("choices") or [{}]
    return ((choices[0].get("message") or {}).get("content") or "")


def parse_json(response):
    """Parse a JSON object out of a model reply, or None.

    None means "the model did not answer in the required shape". It must NEVER
    be an empty dict or a fabricated default: a made-up verdict is
    indistinguishable from a measured one once it has been written to a table,
    and every downstream reader will trust it."""
    text = content_of(response).strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.S)
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        pass
    m = re.search(r"\{[\s\S]+\}", text)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except (TypeError, ValueError):
        return None


__all__ = [
    "AGENT_SPAM_RESCUE", "AGENT_FOLLOWUP", "AGENT_RESPONDER", "AGENT_LEARNING",
    "DESK_AGENTS", "DeskSession", "agent_enabled", "agent_model", "grant",
    "append_lesson", "beacon_ages", "bare_email", "configured_desks",
    "content_of", "create_draft", "customer_is_last_responder", "decode_body",
    "delete_draft_if_unsent", "discover_desks", "envflag", "from_domain",
    "gapi", "is_external", "load_lessons", "lessons_dir", "own_domains",
    "parse_json", "product_for", "reply_subject", "rescue_from_spam",
    "stamp_beacon", "throttle", "thread_senders", "upsert_thread_draft",
    "we_spoke_last", "withdraw_stale_drafts", "_cfg", "_int",
]
