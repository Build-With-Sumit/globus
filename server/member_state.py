"""Per-member installation state — what is implemented, connected, and working.

WHAT THIS IS
------------
An operator running Globus for a team needs to answer one question repeatedly:
"is it actually working for this person?" That question has three different
answers hiding inside it, and collapsing them is how support time gets burned:

  IMPLEMENTED  the code ships this capability
  AVAILABLE    THIS install has it configured (credentials, keys, bridge)
  CONNECTED    this member has wired their own account to it
  WORKING      data has actually arrived and been processed

A capability can be implemented and unavailable (the operator never set up
Google OAuth), available and unconnected (the member never clicked connect),
connected and broken (the refresh token was revoked last Tuesday), or connected
and simply empty (nothing has synced yet). Those five states need five different
actions, and four of them are not the member's fault.

So `stage()` never returns a bare boolean. The roadmap phrases the requirement as
"keeping implemented separate from connected", and the distinction that does the
most work in practice is the first one: reporting "hasn't connected Drive" for a
member on an install with no Google client configured blames the member for the
operator's omission, and sends them to a button that cannot work.

READING A ZERO HONESTLY
----------------------
`db_helpers.db_read` is FAIL-SOFT: it returns None when the query fails and an
empty sequence when the query succeeds and matched nothing. The idiomatic
`db_read(...) or []` collapses those into the same value — so with the database
unreachable, every member would be confidently reported as brand new with
nothing connected. That is the worst possible output for this module: it is a
health surface, and it would be lying precisely when something is wrong.

`_rows()` therefore raises on None. Callers that want a partial answer say so
explicitly and get `unknown`, which renders as "could not check" — never as a
zero. See [[fail-soft-none-vs-empty]].
"""
from __future__ import annotations
import os

from db_helpers import cfg, db_read

# Stages, worst to best. Ordered so `min()` over a member's capabilities gives
# the honest overall stage: a member is only as set up as their weakest link.
STAGE_UNKNOWN = "unknown"            # we could not check — never a zero
STAGE_UNAVAILABLE = "unavailable"    # implemented, but not configured HERE
STAGE_NOT_CONNECTED = "not_connected"
STAGE_ERROR = "error"                # connected and failing
STAGE_CONNECTED = "connected"        # connected, nothing has arrived yet
STAGE_INGESTING = "ingesting"        # data arrived, not yet processed
STAGE_READY = "ready"

_RANK = {
    STAGE_UNKNOWN: 0,
    STAGE_ERROR: 1,
    STAGE_UNAVAILABLE: 2,
    STAGE_NOT_CONNECTED: 3,
    STAGE_CONNECTED: 4,
    STAGE_INGESTING: 5,
    STAGE_READY: 6,
}


class StateUnavailable(RuntimeError):
    """We could not read the state. Distinct from "there is no state"."""


def _rows(sql, params=()):
    """Rows, or raise. NEVER `or []`.

    An unreadable database and an empty result must not produce the same
    answer here — one means "this member has connected nothing", the other
    means "we have no idea", and showing the first when the second is true
    turns a health surface into a source of false confidence."""
    out = db_read(sql, params)
    if out is None:
        raise StateUnavailable("database read failed")
    return list(out)


def _one(sql, params=(), default=0):
    rows = _rows(sql, params)
    if not rows:
        return default
    row = rows[0]
    if isinstance(row, dict):
        return list(row.values())[0]
    return row[0]


# ─────────────────────────────────────────────────────────────────────
# What THIS install can offer
# ─────────────────────────────────────────────────────────────────────

def install_capabilities():
    """{capability: available_bool} for this deployment.

    This is the "implemented vs available" line, and it is read from
    CONFIGURATION rather than from a member's rows. A member with no Drive
    connection looks identical whether the operator forgot to set up Google
    OAuth or the member simply never clicked the button — but the fix, and who
    has to make it, are completely different."""
    google = bool(cfg("GOOGLE_OAUTH_CLIENT_ID", "")
                  and cfg("GOOGLE_OAUTH_CLIENT_SECRET", ""))
    return {
        "google_drive": google,
        "gmail": google,
        # The bridges are pushed to by an extension or a daemon rather than
        # pulled, so availability is "is the ingest endpoint usable" — which is
        # true whenever a relay key exists for the install.
        "whatsapp": bool(cfg("GLOBUS_BRIDGE_KEY", "")),
        "teams": bool(cfg("GLOBUS_BRIDGE_KEY", "")),
        "telegram": bool(cfg("GLOBUS_TELEGRAM_BOT_TOKEN", "")
                         or cfg("TELEGRAM_BOT_TOKEN", "")),
        # Always available: they need no third-party credential.
        "vault": True,
        "digest": True,
        "agents": True,
    }


# ─────────────────────────────────────────────────────────────────────
# One member
# ─────────────────────────────────────────────────────────────────────

def _oauth_stage(email, source_type, available):
    if not available:
        return STAGE_UNAVAILABLE, {}
    rows = _rows(
        "SELECT sync_status, needs_reconnect, last_synced_at, last_sync_error, "
        "source_types, drive_files, gmail_files FROM globus_oauth_connections "
        "WHERE email=%s AND provider='google'", (email,))
    mine = [r for r in rows if source_type in (r.get("source_types") or "")]
    if not mine:
        return STAGE_NOT_CONNECTED, {}
    # A broken connection outranks a healthy one in what the operator needs to
    # see: two connections where one is revoked is a problem, not a success.
    broken = [r for r in mine
              if r.get("needs_reconnect") or r.get("sync_status") == "error"]
    if broken:
        return STAGE_ERROR, {
            "connections": len(mine),
            "detail": (broken[0].get("last_sync_error")
                       or "needs reconnect")[:200]}
    key = "drive_files" if source_type == "drive" else "gmail_files"
    files = sum(int(r.get(key) or 0) for r in mine)
    detail = {"connections": len(mine), "files": files,
              "last_synced_at": max((r.get("last_synced_at") for r in mine),
                                    default=None)}
    return (STAGE_CONNECTED if files == 0 else STAGE_READY), detail


def _bridge_stage(email, table, available):
    if not available:
        return STAGE_UNAVAILABLE, {}
    count = _one(f"SELECT COUNT(*) FROM {table} WHERE member_email=%s", (email,))
    if not count:
        return STAGE_NOT_CONNECTED, {}
    return STAGE_READY, {"messages": int(count)}


def _vault_stage(email):
    rows = _rows(
        "SELECT COUNT(*) AS files, "
        "SUM(vault_processed_at IS NOT NULL) AS processed, "
        "SUM(skip_reason IS NOT NULL) AS skipped "
        "FROM globus_vault_files WHERE email=%s", (email,))
    row = rows[0] if rows else {}
    files = int(row.get("files") or 0)
    processed = int(row.get("processed") or 0)
    skipped = int(row.get("skipped") or 0)
    detail = {"files": files, "processed": processed, "skipped": skipped}
    if not files:
        return STAGE_NOT_CONNECTED, detail
    if not processed:
        return STAGE_INGESTING, detail
    if processed < files - skipped:
        # Partially built. Deliberately still "ingesting": a vault that is 40%
        # processed answers questions confidently from the 40% it has, which is
        # exactly when a member concludes the assistant does not know things it
        # in fact holds.
        return STAGE_INGESTING, detail
    return STAGE_READY, detail


def _digest_stage(email):
    rows = _rows("SELECT digest_char_count, built_at, built_with "
                 "FROM globus_intelligence WHERE email=%s", (email,))
    if not rows:
        return STAGE_NOT_CONNECTED, {}
    row = rows[0]
    chars = int(row.get("digest_char_count") or 0)
    detail = {"chars": chars, "built_at": row.get("built_at"),
              "built_with": row.get("built_with")}
    # A digest row that exists but holds almost nothing is the shape of a failed
    # build that got persisted anyway — a model's fluent refusal written to the
    # table as if it were the member's brain. Report it as broken, not ready.
    if chars < 200:
        return STAGE_ERROR, dict(detail, detail="digest is implausibly short")
    return STAGE_READY, detail


def _agents_stage(email):
    rows = _rows(
        "SELECT status, COUNT(*) AS n, MAX(finished_at) AS last FROM "
        "globus_agent_runs WHERE member_email=%s GROUP BY status", (email,))
    if not rows:
        return STAGE_NOT_CONNECTED, {}
    by = {r.get("status"): r for r in rows}
    ok = int((by.get("ok") or {}).get("n") or 0)
    failed = int((by.get("error") or {}).get("n") or 0)
    detail = {"ok": ok, "failed": failed,
              "last_run": max((r.get("last") for r in rows if r.get("last")),
                              default=None)}
    if ok:
        return STAGE_READY, detail
    if failed:
        return STAGE_ERROR, detail
    return STAGE_CONNECTED, detail


def member_state(email, partial=False):
    """Full installation state for one member.

    `partial=True` degrades an unreadable check to `unknown` instead of raising,
    for surfaces that must render something. It is opt-in because the default
    has to be loud: an admin page quietly showing every member as unconfigured
    during a database blip is worse than an error banner."""
    caps = install_capabilities()
    checks = {}

    probes = (
        ("google_drive", lambda: _oauth_stage(email, "drive", caps["google_drive"])),
        ("gmail", lambda: _oauth_stage(email, "gmail", caps["gmail"])),
        ("whatsapp", lambda: _bridge_stage(email, "globus_whatsapp_messages",
                                           caps["whatsapp"])),
        ("teams", lambda: _bridge_stage(email, "globus_teams_messages",
                                        caps["teams"])),
        ("telegram", lambda: _bridge_stage(email, "globus_telegram_messages",
                                           caps["telegram"])),
        ("vault", lambda: _vault_stage(email)),
        ("digest", lambda: _digest_stage(email)),
        ("agents", lambda: _agents_stage(email)),
    )
    for name, probe in probes:
        try:
            stage, detail = probe()
        except StateUnavailable:
            if not partial:
                raise
            stage, detail = STAGE_UNKNOWN, {}
        checks[name] = dict(detail, stage=stage, available=caps.get(name, True))

    return {"email": email, "stage": overall_stage(checks), "checks": checks}


def overall_stage(checks):
    """The member's honest headline stage.

    Only the capabilities that are AVAILABLE and CONNECTABLE count toward it.
    An install with no Teams bridge should not drag every member to
    'not_connected' forever over a capability the operator chose not to offer —
    that turns the headline into noise and trains people to ignore it."""
    relevant = [c["stage"] for c in checks.values()
                if c.get("stage") not in (STAGE_UNAVAILABLE,)]
    if not relevant:
        return STAGE_UNAVAILABLE
    if STAGE_UNKNOWN in relevant:
        return STAGE_UNKNOWN            # never claim a state we could not read
    if STAGE_ERROR in relevant:
        return STAGE_ERROR              # broken outranks incomplete
    # A member is only as set up as their weakest connected capability, but a
    # capability nobody connected should not read as an error.
    return min(relevant, key=lambda s: _RANK.get(s, 0))


def blockers(state):
    """The actionable items, and WHOSE they are.

    Separating these is the point of the module: an operator scanning a list of
    members needs to know which rows are waiting on them and which are waiting
    on the member. Mixing the two produces a list nobody can act on."""
    out = []
    for name, check in (state.get("checks") or {}).items():
        stage = check.get("stage")
        if stage == STAGE_UNAVAILABLE:
            out.append({"capability": name, "owner": "operator",
                        "action": "not configured on this install"})
        elif stage == STAGE_ERROR:
            out.append({"capability": name, "owner": "member",
                        "action": check.get("detail") or "connection failing"})
        elif stage == STAGE_NOT_CONNECTED and name not in ("digest", "agents"):
            out.append({"capability": name, "owner": "member",
                        "action": "not connected yet"})
        elif stage == STAGE_UNKNOWN:
            out.append({"capability": name, "owner": "operator",
                        "action": "could not be checked"})
    return out


def roster(emails=None, limit=200, partial=True):
    """State for many members, for an operator view.

    Defaults to `partial=True`: a roster that renders nothing because one
    member's check failed is less useful than one that renders and marks that
    row unknown."""
    if emails is None:
        rows = _rows("SELECT email FROM members WHERE status IN "
                     "('active','comp') ORDER BY created_at DESC LIMIT %s",
                     (int(limit),))
        emails = [r.get("email") for r in rows if r.get("email")]
    return [member_state(e, partial=partial) for e in emails]
