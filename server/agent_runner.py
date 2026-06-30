"""OSS-native agent runner — no Hermes dependency.

The model: an "agent" is a catalog entry with a `task_prompt` field.
Running an agent = calling the chat orchestrator with that prompt as
the member's message, capturing the LLM's reply, writing it to disk
as a dated markdown brief, and tracking the run in `globus_agent_runs`.

The orchestrator already has every tool the member has wired up
(search_files, list_recent_emails, search_telegram, etc.), so the
agent inherits the full member context for free.

Public surface:
  - run_agent_for_member(agent_name, email) — synchronous; returns
    {ok, agent, brief_path, bytes_written} or {ok: False, error: ...}
  - agent_run_async(agent_name, email)      — fire-and-forget; spawns
    a daemon thread, returns immediately
  - agent_status(email=None)                — dict shaped for the
    chat-page console (running / recent_runs / latest_per_agent)
  - member_work_dir(email) / ensure_member_work_dir(email)
  - find_agent(name) / catalog_for_member()

Cron use:
  python3 scripts/run_agent.py <agent_name> <member_email>
"""
from __future__ import annotations
import hashlib
import os
import threading
import time
from datetime import datetime

from db_helpers import db_read, db_write
from globus_agents_catalog import GLOBUS_AGENTS_CATALOG


# Per-member agent work dir. Defaults to /var/lib/globus/agents — pick a
# different path via env if you want briefs under your install root.
AGENTS_WORK_DIR = os.environ.get(
    "GLOBUS_AGENTS_WORK_DIR", "/var/lib/globus/agents")


# ─────────────────────────────────────────────────────────────────────
# Catalog lookup
# ─────────────────────────────────────────────────────────────────────

def find_agent(name):
    """Return the catalog entry for `name`, or None."""
    n = (name or "").strip().lower()
    for entry in GLOBUS_AGENTS_CATALOG:
        if entry.get("name", "").lower() == n:
            return entry
    return None


def catalog_for_member(email):
    """Return the catalog (currently every member sees the same catalog).
    Future per-member catalogs land here — gate by email when that
    matters."""
    return list(GLOBUS_AGENTS_CATALOG)


# ─────────────────────────────────────────────────────────────────────
# Per-member work dir — briefs land at
#   {AGENTS_WORK_DIR}/{sha1(email)[:16]}/{agent}-{YYYY-MM-DD-HHMM}.md
# ─────────────────────────────────────────────────────────────────────

def _email_hash(email):
    return hashlib.sha1(email.lower().encode("utf-8")).hexdigest()[:16]


def member_work_dir(email):
    """Absolute path to the member's brief dir. Does NOT create it."""
    if not email:
        return AGENTS_WORK_DIR
    return os.path.join(AGENTS_WORK_DIR, _email_hash(email))


def ensure_member_work_dir(email):
    path = member_work_dir(email)
    try:
        os.makedirs(path, mode=0o2775, exist_ok=True)
    except OSError as e:
        print(f"[agent-runner] ensure_member_work_dir({email}): "
              f"{type(e).__name__}: {e}", flush=True)
    return path


def _brief_path(email, agent_name):
    """Build a fresh timestamped path for a new brief."""
    stamp = time.strftime("%Y-%m-%d-%H%M", time.gmtime())
    return os.path.join(member_work_dir(email),
                         f"{agent_name}-{stamp}.md")


# ─────────────────────────────────────────────────────────────────────
# Run history — globus_agent_runs CRUD
# ─────────────────────────────────────────────────────────────────────

def _insert_run(email, agent_name):
    """Insert a `running` row and return its id."""
    db_write(
        "INSERT INTO globus_agent_runs "
        "(member_email, agent_name, status, started_at) "
        "VALUES (%s, %s, 'running', NOW())",
        (email, agent_name))
    rows = db_read(
        "SELECT id FROM globus_agent_runs "
        "WHERE member_email=%s AND agent_name=%s "
        "ORDER BY id DESC LIMIT 1",
        (email, agent_name))
    return int(rows[0]["id"]) if rows else 0


def _update_run_ok(run_id, brief_path, bytes_written):
    db_write(
        "UPDATE globus_agent_runs "
        "SET status='ok', brief_path=%s, bytes_written=%s, finished_at=NOW() "
        "WHERE id=%s",
        (brief_path, bytes_written, run_id))


def _update_run_error(run_id, error_message):
    db_write(
        "UPDATE globus_agent_runs "
        "SET status='error', error_message=%s, finished_at=NOW() "
        "WHERE id=%s",
        (error_message[:1000], run_id))


# ─────────────────────────────────────────────────────────────────────
# Run an agent — call the orchestrator + write the brief
# ─────────────────────────────────────────────────────────────────────

def run_agent_for_member(agent_name, email):
    """Synchronous run. Returns dict — never raises (errors are
    captured into the run row + returned in `error` key).

    The chat history logged into globus_messages is the agent's task
    + reply, so the same brief is visible in the member's chat history
    too — handy for "what did Drona send me at 8 AM" follow-up Qs."""
    agent = find_agent(agent_name)
    if not agent:
        return {"ok": False, "agent": agent_name,
                "error": f"agent {agent_name!r} not in catalog"}
    task = agent.get("task_prompt", "").strip()
    if not task:
        return {"ok": False, "agent": agent_name,
                "error": f"agent {agent_name!r} has no task_prompt "
                          "in the catalog"}

    ensure_member_work_dir(email)
    run_id = _insert_run(email, agent_name)
    try:
        # Local import to keep the module-load cycle small. Orchestrator
        # imports a lot — agents may load on cron with no other code.
        from globus_orchestrator import globus_chat_send
        reply, _usage = globus_chat_send(email, task)
        body = (reply or "").strip() or "(empty reply)"
        path = _brief_path(email, agent_name)
        with open(path, "w", encoding="utf-8") as fh:
            header = (
                f"# {agent.get('role', agent_name)} brief\n\n"
                f"_agent: {agent_name} · member: {email} · "
                f"generated: {datetime.utcnow().isoformat()}Z_\n\n---\n\n"
            )
            fh.write(header + body + "\n")
        n_bytes = os.path.getsize(path)
        _update_run_ok(run_id, path, n_bytes)
        print(f"[agent-runner] {agent_name} for {email}: "
              f"{n_bytes} bytes -> {path}", flush=True)
        return {"ok": True, "agent": agent_name, "brief_path": path,
                "bytes_written": n_bytes}
    except Exception as e:
        err = f"{type(e).__name__}: {e}"
        _update_run_error(run_id, err)
        print(f"[agent-runner] {agent_name} for {email} FAILED: {err}",
              flush=True)
        return {"ok": False, "agent": agent_name, "error": err}


def agent_run_async(agent_name, email):
    """Fire-and-forget. Returns immediately — caller doesn't wait for
    the brief. Used by the run_agent LLM tool so chat stays responsive
    during long agent runs."""
    if not find_agent(agent_name):
        return {"ok": False, "agent": agent_name,
                "error": f"agent {agent_name!r} not in catalog"}
    # Double-fire guard: if there's already a 'running' row for this
    # (email, agent) we silently skip and report it.
    rows = db_read(
        "SELECT id, started_at FROM globus_agent_runs "
        "WHERE member_email=%s AND agent_name=%s AND status='running' "
        "ORDER BY id DESC LIMIT 1",
        (email, agent_name))
    if rows:
        return {"ok": True, "agent": agent_name,
                "already_running": True,
                "message": f"{agent_name} is already running for you "
                            f"(started {rows[0].get('started_at')})"}
    threading.Thread(
        target=run_agent_for_member, args=(agent_name, email),
        name=f"agent-{agent_name}-{email[:20]}", daemon=True).start()
    return {"ok": True, "agent": agent_name,
            "message": f"{agent_name} run started; brief lands in "
                        f"~30s in the agent activity console"}


# ─────────────────────────────────────────────────────────────────────
# Status snapshot — drives the chat-page activity console
# ─────────────────────────────────────────────────────────────────────

def agent_status(email=None):
    """Return {running, recent_runs, latest_per_agent} for the
    chat-page console. Scoped to one member if `email` is set, else
    the whole install. Memoised for 3 seconds to absorb burst polls."""
    cache = getattr(agent_status, "_cache", None)
    key = (email or "_all",)
    if cache and cache[0] == key and (time.time() - cache[1]) < 3.0:
        return cache[2]

    where = "WHERE member_email=%s " if email else ""
    args = (email,) if email else ()

    running = db_read(
        f"SELECT id, agent_name, member_email, started_at, "
        f"  TIMESTAMPDIFF(SECOND, started_at, NOW()) AS runtime_sec "
        f"FROM globus_agent_runs {where}"
        f"  {'AND' if where else 'WHERE'} status='running' "
        f"ORDER BY started_at DESC LIMIT 20",
        args) or []

    recent = db_read(
        f"SELECT agent_name, status, brief_path, bytes_written, "
        f"  finished_at AS ts "
        f"FROM globus_agent_runs {where}"
        f"  {'AND' if where else 'WHERE'} finished_at IS NOT NULL "
        f"ORDER BY finished_at DESC LIMIT 15",
        args) or []

    latest_per_agent = {}
    latest_rows = db_read(
        f"SELECT agent_name, brief_path, bytes_written, finished_at AS ts "
        f"FROM globus_agent_runs r1 {where}"
        f"  {'AND' if where else 'WHERE'} status='ok' "
        f"  AND finished_at = ("
        f"    SELECT MAX(r2.finished_at) FROM globus_agent_runs r2 "
        f"    WHERE r2.agent_name=r1.agent_name AND r2.status='ok' "
        + ("    AND r2.member_email=r1.member_email " if email else "")
        + f"  )",
        args) or []
    for r in latest_rows:
        latest_per_agent[r["agent_name"]] = {
            "ts": str(r["ts"]) if r["ts"] else None,
            "bytes": int(r["bytes_written"] or 0),
            "brief_path": r["brief_path"] or "",
        }

    result = {
        "running": [
            {"id": r["id"], "agent": r["agent_name"],
             "runtime_sec": int(r["runtime_sec"] or 0),
             "started_at": str(r["started_at"]) if r["started_at"] else None}
            for r in running
        ],
        "recent_runs": [
            {"agent": r["agent_name"], "status": r["status"],
             "ts": str(r["ts"]) if r["ts"] else None,
             "bytes": int(r["bytes_written"] or 0),
             "brief_path": r["brief_path"] or ""}
            for r in recent
        ],
        "latest_per_agent": latest_per_agent,
        "snapshot_at": time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime()),
    }
    agent_status._cache = (key, time.time(), result)
    return result
