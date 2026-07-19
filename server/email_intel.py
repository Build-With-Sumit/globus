"""Two-tier email intelligence — triage, reason, and a heartbeat-gated digest.

WHAT THIS IS
------------
An inbox produces far more mail than a person can read, but only a little of it
needs a decision. This runs two passes over a connected Gmail mailbox:

  Tier 1 — TRIAGE   cheap model, frequent. Files recognisable mail into a flat,
                    operator-defined taxonomy using only `From`/`Subject`/snippet.
  Tier 2 — REASON   stronger model, infrequent. Reads the FULL BODY of only the
                    mail Tier 1 could not recognise, judges it against the
                    operator's business context, and records
                    {category, urgency, action} plus a "needs action" label.

  DIGEST            rolls the judgments up into one notification per day.

The economics are the whole point: listing and reading metadata is nearly free,
reasoning is not. Tier 1 exists to SHRINK Tier 2's input set. In practice a good
taxonomy files most inbound, leaving the reasoner a small tail.

THE LAYER RULE — one owner per layer, per mailbox
-------------------------------------------------
Tier 2 must never also label into Tier 1's taxonomy on the same mailbox. Two
classifiers on one inbox produce two incompatible answers to different questions,
and the mailbox ends up with both. Tier 1 owns the taxonomy; Tier 2 owns judgment
and applies at most its one action label.

Tier 2 finds "the mail Tier 1 left unfiled" WITHOUT knowing the taxonomy: Gmail
gives user-created labels ids prefixed `Label_`, while every system label
(INBOX/UNREAD/CATEGORY_*) has a reserved id. So Tier 2 asks only "does this
message carry any user label yet?" — i.e. it couples to the EXISTENCE OF A
DECISION, not to the content of the taxonomy. Tier 1 can add or rename buckets
freely, and a human filing a message by hand removes it from Tier 2's scope for
free.

NON-DESTRUCTIVE BY CONSTRUCTION
-------------------------------
Neither tier can archive, move, or delete mail: the only Gmail write available to
them is `gmail_add_labels`, which never sends `removeLabelIds` (see
google_gmail.py). Adding a label is always recoverable; archiving on a false
positive is the failure people actually notice — usually weeks later, when they
go looking for a message that was silently buried.

Nothing here ever sends email.
"""
from __future__ import annotations
import json
import os
import re
from datetime import datetime, timedelta, timezone

from db_helpers import db_read, db_write, cfg
from globus_llm import globus_call_chat
from google_gmail import (
    gmail_list_messages, gmail_get_message, gmail_headers,
    gmail_extract_body_text, gmail_list_labels, gmail_ensure_label,
    gmail_add_labels, parse_email_date,
)
from oauth_db import get_valid_access_token

AGENT = "email-intel"

# The beacon prefix. One key per mailbox — see stamp_beacon().
BEACON_PREFIX = "email_intel_last_run"


# ─────────────────────────────────────────────────────────────────────
# Config
# ─────────────────────────────────────────────────────────────────────

def envflag(name, default=False):
    """Parse a boolean env var HONESTLY.

    `bool(os.environ.get("X"))` is True for the string "0", so the obvious way
    to turn a flag off — `X=0` — turns it ON. That trap has silently frozen a
    pipeline before (a dry-run flag that could not be switched off, so nothing
    was ever delivered). Never use bare bool() on an env var."""
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() not in ("0", "false", "no", "off")


def _cfg(key, default=""):
    """config-table value, falling back to env, then the default."""
    return (cfg(key, "") or os.environ.get(key, "") or default)


def _int(key, default):
    try:
        return int(str(_cfg(key, str(default))).strip())
    except (TypeError, ValueError):
        return default


DEFAULT_TAXONOMY = [
    {"name": "Customer", "description":
     "An existing customer or user of ours writing about their account, an "
     "issue, or a request."},
    {"name": "Prospect", "description":
     "Someone evaluating us — pricing questions, demos, trials, inbound "
     "interest in buying."},
    {"name": "Vendor-Pitch", "description":
     "An unsolicited sales or marketing approach TO us, including cold "
     "outreach, agencies, and recruiters pitching services."},
    {"name": "Partnership", "description":
     "A proposed collaboration, integration, affiliate or reseller approach."},
    {"name": "Billing", "description":
     "Invoices, receipts, payment notifications, subscription and tax mail."},
    {"name": "Newsletter", "description":
     "Bulk or subscribed content: newsletters, digests, product updates, "
     "event invitations, award/programme solicitations."},
    {"name": "Other", "description":
     "Anything that does not clearly fit the buckets above."},
]


def taxonomy():
    """Operator-defined Tier-1 buckets: [{"name","description"}, ...].

    Set EMAIL_INTEL_TAXONOMY to a JSON array to replace the defaults. Keep it
    SMALL and mutually exclusive — a flat taxonomy a cheap model can apply
    consistently beats a rich one it applies differently every run."""
    raw = _cfg("EMAIL_INTEL_TAXONOMY", "")
    if raw:
        try:
            items = json.loads(raw)
            out = [{"name": str(i["name"]), "description": str(i.get("description", ""))}
                   for i in items if i.get("name")]
            if out:
                return out
        except Exception as e:
            print(f"[{AGENT}] WARN: EMAIL_INTEL_TAXONOMY is not valid JSON "
                  f"({type(e).__name__}) — using defaults", flush=True)
    return DEFAULT_TAXONOMY


def taxonomy_names():
    return [t["name"] for t in taxonomy()]


def business_context():
    """The single highest-value setting here.

    A free-text paragraph describing what the operator's business is, who
    matters, and what "urgent" means to them. Tier 2 cannot judge relevance
    without it — with no context it will flag plausible-looking noise and miss
    the mail that actually matters. Ships EMPTY on purpose: a generic default
    would be confidently wrong."""
    return _cfg("EMAIL_INTEL_CONTEXT", "").strip()


def sender_rules():
    """Optional deterministic `From:`-domain → label table, as JSON
    [["example.com", "Newsletter"], ...].

    Checked before the model. A lookup table is cheaper, is right every time,
    and — the real reason — is CONSISTENT: a model will spell the same vendor
    three different ways across runs, and the taxonomy quietly fragments."""
    raw = _cfg("EMAIL_INTEL_SENDER_RULES", "")
    if not raw:
        return []
    try:
        return [(str(d).strip().lower(), str(lb).strip())
                for d, lb in json.loads(raw) if d and lb]
    except Exception as e:
        print(f"[{AGENT}] WARN: EMAIL_INTEL_SENDER_RULES is not valid JSON "
              f"({type(e).__name__}) — ignoring", flush=True)
        return []


def action_label():
    """Name of the one label Tier 2 may apply."""
    return _cfg("EMAIL_INTEL_ACTION_LABEL", "Action-Needed")


# Caps. LIST_MAX and REASON_MAX are deliberately SEPARATE knobs:
#
#   Cap the SPEND, never the SIGHT.
#
# Gmail lists newest-first. If the listing cap is small, a wide lookback window
# only ever reaches back a fraction of that window — older mail is never fetched,
# never judged, and ages out of the window entirely. The run still succeeds, and
# still stamps a healthy heartbeat, so the digest cheerfully reports all-clear
# over mail it never looked at. List generously; bound the MODEL CALLS instead.
def list_max():
    return _int("EMAIL_INTEL_LIST_MAX", 500)


def reason_max():
    """Hard ceiling on Tier-2 model calls per run. This is what bounds cost if
    Tier 1 dies and every message suddenly looks unfiled. Overflow is announced
    and deferred to the next run, never silently dropped."""
    return _int("EMAIL_INTEL_REASON_MAX", 25)


def grace_minutes():
    """Ignore mail younger than this, so Tier 1 (on its own schedule) has had a
    chance to file it first. Must exceed the gap between the two schedules."""
    return _int("EMAIL_INTEL_GRACE_MIN", 45)


def lookback_query():
    """Gmail search window for Tier 2. Set several times WIDER than the cadence:
    mail that ages out of this window is never judged, and nothing reports it."""
    return _cfg("EMAIL_INTEL_LOOKBACK", "newer_than:7d")


def reason_anyway_labels():
    """Tier-1 buckets still worth judging despite being filed, as a JSON array
    of label names. Empty by default."""
    raw = _cfg("EMAIL_INTEL_REASON_LABELS", "")
    if not raw:
        return set()
    try:
        return {str(x).strip() for x in json.loads(raw) if str(x).strip()}
    except Exception:
        return set()


def triage_model():
    return _cfg("EMAIL_TRIAGE_MODEL", "haiku")


def reason_model():
    return _cfg("EMAIL_REASON_MODEL", "sonnet")


TRIAGE_BATCH = 25          # messages per Tier-1 model call
BODY_CHARS = 6000          # per-message body budget handed to Tier 2


# ─────────────────────────────────────────────────────────────────────
# Store
# ─────────────────────────────────────────────────────────────────────

def seen_ids(account, ids):
    """Which of `ids` already have a judgment row for this mailbox."""
    if not ids:
        return set()
    marks = ",".join(["%s"] * len(ids))
    rows = db_read(f"SELECT msg_id FROM email_intel WHERE account=%s "
                   f"AND msg_id IN ({marks})", tuple([account] + list(ids)))
    return {r["msg_id"] for r in (rows or [])}


def upsert_judgment(account, msg_id, *, thread_id=None, category=None,
                    urgency="none", action_summary=None, reasoning=None,
                    sender=None, subject=None, received_at=None):
    """Write one judgment. The (account, msg_id) unique key makes this an
    upsert, so a re-run refreshes rather than duplicating."""
    return db_write(
        "INSERT INTO email_intel (account, msg_id, thread_id, category, "
        " urgency, action_summary, reasoning, sender, subject, received_at) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
        "ON DUPLICATE KEY UPDATE thread_id=VALUES(thread_id), "
        " category=VALUES(category), urgency=VALUES(urgency), "
        " action_summary=VALUES(action_summary), reasoning=VALUES(reasoning), "
        " sender=VALUES(sender), subject=VALUES(subject), "
        " received_at=VALUES(received_at)",
        (account, msg_id, thread_id, category, urgency, action_summary,
         reasoning, (sender or "")[:320], (subject or "")[:500], received_at))


def open_items(accounts, lookback_hours):
    """Unresolved medium/high-urgency judgments in the window."""
    if not accounts:
        return []
    marks = ",".join(["%s"] * len(accounts))
    return db_read(
        f"SELECT id, account, msg_id, category, urgency, action_summary, "
        f" sender, subject, received_at FROM email_intel "
        f"WHERE account IN ({marks}) AND resolved_at IS NULL "
        f"  AND urgency IN ('medium','high') "
        f"  AND processed_at > (NOW() - INTERVAL %s HOUR) "
        f"ORDER BY FIELD(urgency,'high','medium'), received_at DESC",
        tuple(list(accounts) + [lookback_hours])) or []


def resolve_ids(ids):
    """Mark rows delivered. Called per DELIVERED CHUNK, never per run — if the
    third chunk of a digest fails, the first two must stay resolved and only
    the remainder is retried."""
    if not ids:
        return True
    marks = ",".join(["%s"] * len(ids))
    return db_write(f"UPDATE email_intel SET resolved_at=NOW() "
                    f"WHERE id IN ({marks})", tuple(ids))


# ─────────────────────────────────────────────────────────────────────
# Heartbeat
# ─────────────────────────────────────────────────────────────────────
# A monitor that cannot tell "nothing to report" from "I never ran" will
# eventually lie. Left ungated, a digest reading an empty table emits a
# confident daily all-clear over a pipeline that has been dead for weeks —
# an empty SELECT and a dead pipeline are byte-identical to it.
#
# So every reasoner run stamps a per-mailbox beacon, and the digest refuses to
# report all-clear for any mailbox whose beacon is stale or absent.

def beacon_key(account):
    """PER-MAILBOX on purpose. With one shared beacon, a healthy mailbox keeps
    it fresh and masks another whose reasoner has been dead for a week — the
    exact false-negative this exists to kill, reintroduced one level up.

    `config.name` is VARCHAR(80), so the address is truncated to fit. The
    authoritative account name is stored in the beacon's JSON payload and read
    back from there — two very long addresses sharing a truncated key would
    otherwise silently overwrite each other's proof-of-life."""
    return f"{BEACON_PREFIX}:{account}"[:80]


def stamp_beacon(account, note="", reasoned=0, flagged=0):
    """Record proof-of-life for this mailbox.

    MUST be called on every completed run INCLUDING the boring ones (empty
    inbox, nothing new). A legitimately quiet mailbox that never stamps is
    reported DOWN every single day — and a daily false alarm is not harmless:
    it teaches the operator to ignore the warning, which destroys the one
    mechanism that stops the digest lying. Cry wolf once and the wolf gets in
    free.

    The stored value is an explicit ISO timestamp, and freshness is read from
    that value rather than from the row's updated_at — an unchanged payload
    would make the UPDATE a no-op and freeze the timestamp silently."""
    payload = json.dumps({
        "at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "account": account, "reasoned": reasoned, "flagged": flagged,
        "note": note})
    return db_write(
        "INSERT INTO config (name, value) VALUES (%s, %s) "
        "ON DUPLICATE KEY UPDATE value=VALUES(value)",
        (beacon_key(account), payload))


def beacon_ages(accounts):
    """{account: hours_since_last_run}. None means NEVER RAN — a state that is
    distinct from "ran a long time ago" and must be reported differently."""
    out = {a: None for a in accounts}
    rows = db_read("SELECT name, value FROM config WHERE name LIKE %s",
                   (BEACON_PREFIX + ":%",)) or []
    now = datetime.now(timezone.utc)
    for r in rows:
        try:
            payload = json.loads(r["value"])
            acct = str(payload.get("account") or "")
            if acct not in out:
                continue
            dt = datetime.fromisoformat(payload["at"])
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            out[acct] = max(0.0, (now - dt).total_seconds() / 3600.0)
        except Exception:
            continue                  # unparseable == no proof of life
    return out


# ─────────────────────────────────────────────────────────────────────
# Mailbox access
# ─────────────────────────────────────────────────────────────────────

def mailbox_connection(account):
    """The newest Google connection for this mailbox address, or None."""
    rows = db_read(
        "SELECT * FROM globus_oauth_connections WHERE provider='google' "
        "AND provider_account=%s AND needs_reconnect=0 "
        "ORDER BY id DESC LIMIT 1", (account,))
    return rows[0] if rows else None


def mailbox_token(account):
    """(access_token, scopes) for a connected mailbox.

    Raises if the mailbox is not connected — a dead credential is exactly the
    condition the beacon should expose, so we do NOT stamp proof-of-life on the
    way out."""
    conn = mailbox_connection(account)
    if not conn:
        raise RuntimeError(f"{account} is not a connected Google account "
                           f"(or needs reconnecting)")
    return get_valid_access_token(conn), (conn.get("scopes") or "")


def can_modify(scopes):
    """Whether the granted scopes permit labelling. If not, we still reason and
    still record — we just say so plainly rather than claiming to have labelled."""
    s = scopes or ""
    return ("gmail.modify" in s or "gmail.labels" in s
            or "https://mail.google.com/" in s)


# ─────────────────────────────────────────────────────────────────────
# Tier 1 — triage
# ─────────────────────────────────────────────────────────────────────

_TRIAGE_SYSTEM = """You sort incoming email into exactly one bucket.

Buckets:
{buckets}

Rules:
- Choose the SINGLE best bucket for each message.
- Judge from the sender, subject and snippet only.
- If no bucket clearly fits, answer "Other".
- If you genuinely cannot tell, answer "skip" and it will be left alone.

{context}
Answer with ONE JSON object per line, nothing else:
{{"n": 1, "label": "<bucket>"}}
{{"n": 2, "label": "skip"}}
"""


def _buckets_block():
    return "\n".join(f"- {t['name']}: {t['description']}" for t in taxonomy())


def _context_block():
    ctx = business_context()
    return (f"Business context (use it to judge relevance):\n{ctx}\n\n"
            if ctx else "")


def _parse_jsonl(text):
    """{n: label} from a JSON-lines reply. A malformed line is skipped, never
    guessed at — an unparseable response must produce NO action, never a wrong
    one. The "no label" state is also exactly the retry state, so a failed call
    is self-healing: the message is simply picked up next run."""
    out = {}
    for line in (text or "").splitlines():
        line = line.strip().strip("`").strip()
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
            n, label = int(obj["n"]), str(obj["label"]).strip()
        except Exception:
            continue
        out[n] = label
    return out


def classify_batch(msgs):
    """[{from,subject,snippet}] → {index: label}. Returns {} on any failure, so
    the whole batch is simply retried next run."""
    if not msgs:
        return {}
    system = _TRIAGE_SYSTEM.format(buckets=_buckets_block(),
                                   context=_context_block())
    lines = []
    for i, m in enumerate(msgs, 1):
        lines.append(f"{i}. from={m.get('from','')!r} "
                     f"subject={m.get('subject','')!r} "
                     f"snippet={(m.get('snippet') or '')[:300]!r}")
    try:
        resp = globus_call_chat(system, [{"role": "user",
                                          "content": "\n".join(lines)}],
                                max_tokens=1200, model=triage_model())
        text = (resp.get("choices") or [{}])[0].get("message", {}).get("content", "")
    except Exception as e:
        print(f"[{AGENT}] triage batch failed ({type(e).__name__}: {e}) — "
              f"{len(msgs)} message(s) left for the next run", flush=True)
        return {}
    return _parse_jsonl(text)


def triage_mailbox(account, lookback_hours=24, dry_run=False):
    """Tier 1. Label unfiled INBOX mail into the taxonomy. Never archives."""
    token, scopes = mailbox_token(account)
    allowed = set(taxonomy_names())
    rules = sender_rules()

    query = f"in:inbox newer_than:{max(1, int(lookback_hours))}h"
    stubs = gmail_list_messages(token, query, max_results=list_max())
    print(f"[{AGENT}] triage {account}: {len(stubs)} listed", flush=True)

    label_cache = {}
    pending, meta = [], []
    for st in stubs:
        try:
            msg = gmail_get_message(token, st["id"])
        except Exception:
            continue
        # Already filed (by us, another agent, or a human)? Leave it alone.
        if any(str(l).startswith("Label_") for l in (msg.get("labelIds") or [])):
            continue
        h = gmail_headers(msg.get("payload") or {})
        item = {"id": st["id"], "from": h.get("From", ""),
                "subject": h.get("Subject", ""), "snippet": msg.get("snippet", "")}
        # Deterministic sender rules first — cheaper and consistent.
        dom = (item["from"].split("@")[-1].strip(" >").lower()
               if "@" in item["from"] else "")
        hit = next((lb for d, lb in rules
                    if dom == d or dom.endswith("." + d)), None)
        if hit and hit in allowed:
            meta.append((item["id"], hit))
        else:
            pending.append(item)

    for i in range(0, len(pending), TRIAGE_BATCH):
        chunk = pending[i:i + TRIAGE_BATCH]
        got = classify_batch(chunk)
        for n, label in got.items():
            if 1 <= n <= len(chunk) and label in allowed:
                meta.append((chunk[n - 1]["id"], label))

    labelled = 0
    if not dry_run and can_modify(scopes):
        # Re-fetch the token before the write phase: a long classify loop can
        # outlive an access token, and every write would then 401 with all the
        # classification work already spent. Freshness is the caller's job.
        token, _ = mailbox_token(account)
        for msg_id, label in meta:
            try:
                lid = gmail_ensure_label(token, label, label_cache)
                if lid and gmail_add_labels(token, msg_id, [lid]):
                    labelled += 1
            except Exception as e:
                print(f"[{AGENT}] label {msg_id} -> {label} failed "
                      f"({type(e).__name__}: {e})", flush=True)
    elif meta and not can_modify(scopes):
        print(f"[{AGENT}] scopes do not permit labelling — classified "
              f"{len(meta)} message(s) but applied nothing", flush=True)

    print(f"[{AGENT}] triage {account}: classified={len(meta)} "
          f"labelled={labelled}{' (dry run)' if dry_run else ''}", flush=True)
    return {"listed": len(stubs), "classified": len(meta), "labelled": labelled}


# ─────────────────────────────────────────────────────────────────────
# Tier 2 — reason
# ─────────────────────────────────────────────────────────────────────

_REASON_SYSTEM = """You triage one email for a busy operator and decide whether
it needs their attention.

{context}Answer with a SINGLE JSON object and nothing else:
{{"category": "<short noun phrase>",
  "urgency": "none|low|medium|high",
  "action": "<one imperative line, or empty if none>",
  "reasoning": "<2-3 sentences>"}}

Urgency is defined by response time, not by tone:
  high   — needs action today; a missed deadline, an at-risk deal or customer,
           anything time-boxed that expires soon.
  medium — needs action this week.
  low    — worth reading, no action.
  none   — no action ever; bulk, automated or purely informational mail.

Marketing urgency ("act now", "final hours") is not urgency. Judge by
consequence to the operator, not by the sender's insistence.
"""


def _extract_json(text):
    """Parse the reply, tolerating code fences and surrounding prose.
    Returns None when it cannot be parsed — the caller MUST treat None as
    "try again later", never as "nothing to do here"."""
    if not text:
        return None
    t = re.sub(r"^```(?:json)?|```$", "", text.strip(),
               flags=re.MULTILINE).strip()
    try:
        return json.loads(t)
    except Exception:
        pass
    m = re.search(r"\{.*\}", t, re.DOTALL)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except Exception:
        return None


def reason_one(subject, sender, body):
    """One message → judgment dict, or None if the model could not be parsed."""
    ctx = business_context()
    system = _REASON_SYSTEM.format(
        context=(f"Business context:\n{ctx}\n\n" if ctx else ""))
    user = (f"From: {sender}\nSubject: {subject}\n\n"
            f"{(body or '')[:BODY_CHARS]}")
    try:
        resp = globus_call_chat(system, [{"role": "user", "content": user}],
                                max_tokens=700, model=reason_model())
        text = (resp.get("choices") or [{}])[0].get("message", {}).get("content", "")
    except Exception as e:
        print(f"[{AGENT}] reason call failed ({type(e).__name__}: {e})",
              flush=True)
        return None
    obj = _extract_json(text)
    if not isinstance(obj, dict):
        return None
    urg = str(obj.get("urgency", "none")).strip().lower()
    if urg not in ("none", "low", "medium", "high"):
        urg = "none"
    return {"category": str(obj.get("category") or "")[:120],
            "urgency": urg,
            "action": str(obj.get("action") or "")[:500],
            "reasoning": str(obj.get("reasoning") or "")}


def reason_mailbox(account, dry_run=False):
    """Tier 2. Judge the mail Tier 1 left unfiled. Never archives."""
    token, scopes = mailbox_token(account)
    stubs = gmail_list_messages(token, f"in:inbox {lookback_query()}",
                                max_results=list_max())
    ids = [s["id"] for s in stubs]
    already = seen_ids(account, ids)
    todo = [i for i in ids if i not in already]

    # The boring exit paths stamp too — see stamp_beacon(). A quiet mailbox
    # that never proves it ran gets reported DOWN daily, and a daily false
    # alarm trains the operator to ignore the one alarm that matters.
    if not ids:
        if not dry_run:
            stamp_beacon(account, note="empty-inbox")
        print(f"[{AGENT}] reason {account}: inbox empty", flush=True)
        return {"listed": 0, "reasoned": 0, "flagged": 0}
    if not todo:
        if not dry_run:
            stamp_beacon(account, note="nothing-new")
        print(f"[{AGENT}] reason {account}: nothing new "
              f"({len(already)} already judged)", flush=True)
        return {"listed": len(ids), "reasoned": 0, "flagged": 0}

    cutoff = datetime.now(timezone.utc) - timedelta(minutes=grace_minutes())
    reason_anyway = reason_anyway_labels()
    label_names = {}
    try:
        label_names = {lb["id"]: lb.get("name", "")
                       for lb in gmail_list_labels(token) if lb.get("id")}
    except Exception:
        pass

    budget = reason_max()
    reasoned = flagged = filed = skipped_young = 0
    action_lid = None
    cache = {}

    for msg_id in todo:
        if reasoned >= budget:
            print(f"[{AGENT}] reason {account}: hit REASON_MAX={budget}; "
                  f"{len(todo) - (reasoned + filed + skipped_young)} deferred "
                  f"to the next run", flush=True)
            break
        try:
            msg = gmail_get_message(token, msg_id)
        except Exception:
            continue

        # Grace window: Tier 1 may not have seen this yet. Deliberately NOT
        # bookmarked, so the next run comes back to it.
        received = parse_email_date(
            gmail_headers(msg.get("payload") or {}).get("Date"))
        if received and received.replace(tzinfo=timezone.utc) > cutoff:
            skipped_young += 1
            continue

        user_labels = [l for l in (msg.get("labelIds") or [])
                       if str(l).startswith("Label_")]
        if user_labels:
            names = {label_names.get(l, l) for l in user_labels}
            if not (names & reason_anyway):
                # Tier 1 already decided. Bookmark it as "looked, no action"
                # so the working set shrinks — otherwise every run re-lists and
                # re-fetches the same filed mail forever.
                if not dry_run:
                    upsert_judgment(account, msg_id, category="filed",
                                    urgency="none")
                filed += 1
                continue

        h = gmail_headers(msg.get("payload") or {})
        body = gmail_extract_body_text(msg.get("payload") or {})
        judgment = reason_one(h.get("Subject", ""), h.get("From", ""), body)
        reasoned += 1
        if judgment is None:
            # A failed call must NOT be bookmarked. Storing "no action" for a
            # call that never succeeded hides the message forever.
            continue
        if dry_run:
            print(f"[{AGENT}] would record {msg_id}: {judgment['urgency']} "
                  f"{judgment['category']}", flush=True)
            continue
        upsert_judgment(
            account, msg_id, thread_id=msg.get("threadId"),
            category=judgment["category"], urgency=judgment["urgency"],
            action_summary=judgment["action"], reasoning=judgment["reasoning"],
            sender=h.get("From", ""), subject=h.get("Subject", ""),
            received_at=received)
        if judgment["urgency"] in ("medium", "high"):
            flagged += 1
            if can_modify(scopes):
                try:
                    action_lid = action_lid or gmail_ensure_label(
                        token, action_label(), cache)
                    gmail_add_labels(token, msg_id, [action_lid])
                except Exception as e:
                    print(f"[{AGENT}] action label failed ({type(e).__name__}: "
                          f"{e})", flush=True)

    # If a whole window produced nothing that Tier 1 had touched, that is
    # evidence about Tier 1, not about the mail.
    if filed == 0 and len(todo) > 20:
        print(f"[{AGENT}] WARNING: none of {len(todo)} candidates carried a "
              f"triage label — Tier 1 may not be running on {account}",
              flush=True)

    if not dry_run:
        stamp_beacon(account, note="ok", reasoned=reasoned, flagged=flagged)
    print(f"[{AGENT}] reason {account}: reasoned={reasoned} flagged={flagged} "
          f"filed={filed} young={skipped_young}"
          f"{' (dry run)' if dry_run else ''}", flush=True)
    return {"listed": len(ids), "reasoned": reasoned, "flagged": flagged}


# ─────────────────────────────────────────────────────────────────────
# Digest — heartbeat gated
# ─────────────────────────────────────────────────────────────────────

def digest_accounts():
    raw = _cfg("EMAIL_INTEL_ACCOUNTS", "")
    return [a.strip().lower() for a in raw.split(",") if a.strip()]


def _msg_link(account, msg_id):
    """Deep link into the right mailbox. `authuser` matters: without it a
    multi-mailbox digest opens every link in whichever account happens to be
    signed in first."""
    return f"https://mail.google.com/mail/u/?authuser={account}#all/{msg_id}"


def build_digest(accounts, lookback_hours=72, stale_hours=26,
                 max_chars=3500):
    """→ [(text, [row_ids])]. One entry per deliverable chunk.

    Chunking is not cosmetic: most chat transports hard-reject an oversized
    message, and rows are only marked delivered once a chunk actually lands.
    Without chunking, a digest that outgrows the limit fails, returns tomorrow
    BIGGER, and is then silently dead forever while the reasoners keep looking
    healthy."""
    ages = beacon_ages(accounts)
    live = [a for a in accounts
            if ages.get(a) is not None and ages[a] <= stale_hours]
    down = [a for a in accounts if a not in live]

    header = (f"📥 Email intelligence — {len(live)}/{len(accounts)} "
              f"mailbox(es) reporting")
    warn = ""
    if down:
        lines = []
        for a in down:
            age = ages.get(a)
            lines.append(f"  • {a} — last run: "
                         + ("never" if age is None else f"{age:.0f}h ago"))
        warn = ("\n\n🔴 PIPELINE DOWN — these mailboxes have not reported:\n"
                + "\n".join(lines)
                + "\nNo judgments can come from them; check the reasoner "
                  "schedule and its log.")

    rows = open_items(live, lookback_hours) if live else []

    if not rows:
        if not live:
            # Never an all-clear when nothing looked. An empty result is only
            # good news if something actually ran.
            return [(header + warn, [])]
        freshest = min((ages[a] for a in live), default=None)
        fresh_txt = f" (freshest run {freshest:.0f}h ago)" if freshest is not None else ""
        return [(header + warn + f"\n\n✅ No medium/high-urgency mail in the "
                 f"reporting mailbox(es){fresh_txt}.", [])]

    chunks, cur, cur_ids = [], header + warn + "\n", []
    for r in rows:
        icon = "🔴" if r["urgency"] == "high" else "🟠"
        block = (f"\n{icon} {r['urgency'].upper()} · {r['account']}\n"
                 f"   {(r.get('subject') or '(no subject)')[:120]}\n"
                 f"   from {(r.get('sender') or '?')[:80]}\n"
                 f"   → {(r.get('action_summary') or '').strip()[:200]}\n"
                 f"   {_msg_link(r['account'], r['msg_id'])}\n")
        if len(cur) + len(block) > max_chars and cur_ids:
            chunks.append((cur, cur_ids))
            cur, cur_ids = header + " (cont.)\n", []
        cur += block
        cur_ids.append(r["id"])
    chunks.append((cur, cur_ids))
    return chunks
