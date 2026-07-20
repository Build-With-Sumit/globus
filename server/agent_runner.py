"""OSS-native agent runner — no Hermes dependency.

The model: an "agent" is a catalog entry with a `task_prompt` field.
Running an agent = calling the chat orchestrator with that prompt as
the member's message, capturing the LLM's reply, writing it to disk
as a dated markdown brief, and tracking the run in `globus_agent_runs`.

The orchestrator keeps the member's data context, but each agent receives only
the exact tools in its catalog ``tool_allowlist``. Missing or malformed grants
fail closed before a model call.

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
import sys
import threading
import time
from datetime import datetime, timezone

import db_helpers
from db_helpers import db_read, db_write
from globus_agents_catalog import GLOBUS_AGENTS_CATALOG
from globus_tool_policy import ToolPolicyError, agent_tool_allowlist


# Source installs start with ``python3 server/globus_server.py``, which puts
# server/ rather than the repository root on sys.path.  Keep the Truth package
# import lazy (so a missing package cannot prevent the HTTP server booting), but
# make the source-tree package discoverable when an agent is actually run.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# Per-member agent work dir. Defaults to /var/lib/globus/agents — pick a
# different path via env if you want briefs under your install root.
AGENTS_WORK_DIR = os.environ.get(
    "GLOBUS_AGENTS_WORK_DIR", "/var/lib/globus/agents")


def _truth_adapter():
    """Load the optional-at-boot, required-at-run Truth adapter."""
    if _REPO_ROOT not in sys.path:
        sys.path.insert(0, _REPO_ROOT)
    try:
        from globus_truth import agent_adapter
    except Exception as exc:
        raise RuntimeError(
            "Globus Truth Layer is unavailable; install/copy the "
            "repo-root globus_truth package"
        ) from exc
    return agent_adapter


def _truth_service():
    return _truth_adapter().get_truth_service(work_dir=AGENTS_WORK_DIR)


def _utcnow():
    return datetime.now(timezone.utc)


def _truth_error(verdict):
    reasons = ", ".join(verdict.get("reason_codes") or []) or "unverified"
    return f"truth verdict {verdict.get('verdict') or 'unknown'}: {reasons}"


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


def _brief_path(email, agent_name, run_id):
    """Build a durable-run-specific path that can never replace prior evidence."""
    stamp = time.strftime("%Y-%m-%d-%H%M%S", time.gmtime())
    return os.path.join(member_work_dir(email),
                         f"{agent_name}-{stamp}-run-{int(run_id)}.md")


# ─────────────────────────────────────────────────────────────────────
# Run history — globus_agent_runs CRUD
# ─────────────────────────────────────────────────────────────────────

def _insert_run(email, agent_name):
    """Atomically insert a `running` row and return its connection-local id."""
    insert = getattr(db_helpers, "db_insert", None)
    if not callable(insert):
        return 0
    raw_id = insert(
        "INSERT INTO globus_agent_runs "
        "(member_email, agent_name, status, started_at) "
        "VALUES (%s, %s, 'running', NOW())",
        (email, agent_name))
    try:
        run_id = int(raw_id or 0)
    except (TypeError, ValueError):
        return 0
    return run_id if run_id > 0 else 0


def _confirmed_run_state(run_id, expected_status, *, brief_path=None,
                         bytes_written=None):
    rows = db_read(
        "SELECT status, brief_path, bytes_written "
        "FROM globus_agent_runs WHERE id=%s LIMIT 1",
        (run_id,)) or []
    if len(rows) != 1 or rows[0].get("status") != expected_status:
        return False
    if brief_path is not None and rows[0].get("brief_path") != brief_path:
        return False
    if (
        bytes_written is not None
        and int(rows[0].get("bytes_written") or 0) != int(bytes_written)
    ):
        return False
    return True


def _update_run_ok(run_id, brief_path, bytes_written):
    wrote = db_write(
        "UPDATE globus_agent_runs "
        "SET status='ok', brief_path=%s, bytes_written=%s, finished_at=NOW() "
        "WHERE id=%s AND status='running'",
        (brief_path, bytes_written, run_id))
    return bool(
        wrote
        and _confirmed_run_state(
            run_id,
            "ok",
            brief_path=brief_path,
            bytes_written=bytes_written,
        )
    )


def _update_run_error(run_id, error_message):
    wrote = db_write(
        "UPDATE globus_agent_runs "
        "SET status='error', error_message=%s, finished_at=NOW() "
        "WHERE id=%s AND status='running'",
        (error_message[:1000], run_id))
    return bool(wrote and _confirmed_run_state(run_id, "error"))


def _with_ledger_result(run_id, error_message):
    """Mark a run failed and make a lost ledger transition visible to callers."""
    if _update_run_error(run_id, error_message):
        return error_message
    return f"{error_message}; runner ledger transition failed"


# ─────────────────────────────────────────────────────────────────────
# Run an agent — call the orchestrator + write the brief
# ─────────────────────────────────────────────────────────────────────

def run_agent_for_member(agent_name, email):
    """Synchronous run. Returns dict — never raises (errors are
    captured into the run row + returned in `error` key). Completed runs
    include a compact ``truth`` object:
    ``{storage_id, verdict, valid, reason_codes}``.

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
    try:
        allowed_tools = agent_tool_allowlist(agent)
    except ToolPolicyError as exc:
        return {
            "ok": False,
            "agent": agent_name,
            "error": f"AgentToolPolicyError: {exc}",
        }

    ensure_member_work_dir(email)
    run_id = _insert_run(email, agent_name)
    if run_id <= 0:
        # Without a durable, install-unique runner ID there is no safe
        # deterministic receipt identity. Reusing a synthetic "rowless" ID
        # would make later runs conflict with the first immutable receipt.
        err = (
            "AgentRunPersistenceError: MySQL did not return a durable run ID"
        )
        print(f"[agent-runner] {agent_name} for {email} FAILED: {err}",
              flush=True)
        return {"ok": False, "agent": agent_name, "error": err}
    # A durable MySQL row gives this run a deterministic Truth receipt ID.
    # The status endpoint can therefore do one stale-aware point read per row,
    # with no cross-member/global receipt scan.
    run_key = f"runner-{run_id}"
    started_at = _utcnow()

    # Fail closed before spending a model call. The package import is lazy so a
    # container missing globus_truth can still boot and report the deployment
    # problem, but it cannot produce an unverified green agent run.
    try:
        truth_service = _truth_service()
        truth_adapter = _truth_adapter()
        # Validate the install-scoped pseudonym key before spending a model
        # call. The adapter repeats this when constructing the receipt.
        truth_adapter.member_scope_hash(email)
    except Exception as e:
        err = f"{type(e).__name__}: {e}"
        err = _with_ledger_result(run_id, err)
        print(f"[agent-runner] {agent_name} for {email} FAILED: {err}",
              flush=True)
        return {"ok": False, "agent": agent_name, "error": err}

    path = ""
    n_bytes = 0
    try:
        # Local import to keep the module-load cycle small. Orchestrator
        # imports a lot — agents may load on cron with no other code.
        from globus_orchestrator import globus_chat_send
        reply, _usage = globus_chat_send(
            email,
            task,
            allowed_tools=allowed_tools,
            agent_name=agent_name,
        )
        body = (reply or "").strip() or "(empty reply)"
        path = _brief_path(email, agent_name, run_id)
        header = (
            f"# {agent.get('role', agent_name)} brief\n\n"
            f"_agent: {agent_name} · member: {email} · "
            f"generated: {_utcnow().isoformat()}_\n\n---\n\n"
        )
        artifact_bytes = (header + body + "\n").encode("utf-8")
        expected_sha256 = hashlib.sha256(artifact_bytes).hexdigest()
        # Binary mode makes the bytes hashed before the write exactly the bytes
        # the adapter reopens, including on Windows development hosts.
        with open(path, "xb") as fh:
            fh.write(artifact_bytes)
        n_bytes = os.path.getsize(path)
        finished_at = _utcnow()

        try:
            truth = truth_adapter.record_successful_agent_run(
                email=email,
                agent_name=agent_name,
                runner_run_id=run_id,
                run_key=run_key,
                started_at=started_at,
                finished_at=finished_at,
                model_reply=(reply or ""),
                artifact_path=path,
                expected_sha256=expected_sha256,
                expected_bytes=len(artifact_bytes),
                service=truth_service,
            )
        except Exception as truth_exc:
            # The work artifact is preserved for diagnosis, but a receipt that
            # could not be persisted is never represented as a successful run.
            err = (
                "TruthPersistenceError: "
                f"{type(truth_exc).__name__}: {truth_exc}"
            )
            err = _with_ledger_result(run_id, err)
            print(f"[agent-runner] {agent_name} for {email} FAILED: {err}",
                  flush=True)
            return {
                "ok": False,
                "agent": agent_name,
                "error": err,
                "brief_path": path,
                "bytes_written": n_bytes,
            }

        if not truth.get("valid"):
            err = _truth_error(truth)
            err = _with_ledger_result(run_id, err)
            print(f"[agent-runner] {agent_name} for {email} UNVERIFIED: "
                  f"{err}; artifact kept at {path}", flush=True)
            return {
                "ok": False,
                "agent": agent_name,
                "error": err,
                "brief_path": path,
                "bytes_written": n_bytes,
                "truth": truth,
            }

        if not _update_run_ok(run_id, path, n_bytes):
            # The Truth receipt is already immutable and accurately proves the
            # artifact. Do not contradict it with a second receipt; instead,
            # report the cross-store partial commit and refuse overall success.
            err = _with_ledger_result(
                run_id,
                "AgentRunPersistenceError: verified artifact could not be "
                "committed to the runner ledger",
            )
            print(f"[agent-runner] {agent_name} for {email} FAILED: {err}",
                  flush=True)
            return {
                "ok": False,
                "agent": agent_name,
                "error": err,
                "brief_path": path,
                "bytes_written": n_bytes,
                "truth": truth,
            }
        print(f"[agent-runner] {agent_name} for {email}: "
              f"{n_bytes} bytes -> {path}", flush=True)
        return {"ok": True, "agent": agent_name, "brief_path": path,
                "bytes_written": n_bytes, "truth": truth}
    except Exception as e:
        finished_at = _utcnow()
        # Provider exceptions can echo prompts, data, tokens, or member
        # identifiers. Persist only the exception class and a generic message.
        err = f"{type(e).__name__}: agent execution failed"
        truth = None
        try:
            truth = truth_adapter.record_failed_agent_run(
                email=email,
                agent_name=agent_name,
                runner_run_id=run_id,
                run_key=run_key,
                started_at=started_at,
                finished_at=finished_at,
                error_code=type(e).__name__,
                error_message=str(e),
                service=truth_service,
            )
        except Exception as truth_exc:
            err += (
                "; TruthPersistenceError: "
                f"{type(truth_exc).__name__}"
            )
        err = _with_ledger_result(run_id, err)
        print(f"[agent-runner] {agent_name} for {email} FAILED: {err}",
              flush=True)
        result = {"ok": False, "agent": agent_name, "error": err}
        if path:
            result["brief_path"] = path
            result["bytes_written"] = n_bytes
        if truth is not None:
            result["truth"] = truth
        return result


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
        f"SELECT id, agent_name, member_email, status, brief_path, bytes_written, "
        f"  finished_at AS ts "
        f"FROM globus_agent_runs {where}"
        f"  {'AND' if where else 'WHERE'} finished_at IS NOT NULL "
        f"ORDER BY finished_at DESC, id DESC LIMIT 15",
        args) or []

    latest_per_agent = {}
    latest_rows = db_read(
        f"SELECT id, agent_name, member_email, brief_path, bytes_written, "
        f"  finished_at AS ts "
        f"FROM globus_agent_runs r1 {where}"
        f"  {'AND' if where else 'WHERE'} status='ok' "
        f"  AND NOT EXISTS ("
        f"    SELECT 1 FROM globus_agent_runs r2 "
        f"    WHERE r2.agent_name=r1.agent_name AND r2.status='ok' "
        + ("    AND r2.member_email=r1.member_email " if email else "")
        + f"      AND (r2.finished_at > r1.finished_at "
        f"        OR (r2.finished_at = r1.finished_at AND r2.id > r1.id))"
        f"  )",
        args) or []

    # Truth reads are isolated behind the adapter so the status surface never
    # sees receipt payloads. A status poll remains available if the auxiliary
    # SQLite database is temporarily unreadable; completed runs simply omit the
    # compact `truth` field until it recovers.
    members = {
        str(r.get("member_email") or email or "").strip().lower()
        for r in [*recent, *latest_rows]
        if r.get("member_email") or email
    }
    truth_by_member = {}
    for member in members:
        try:
            member_rows = [
                {"id": row.get("id"), "agent_name": row.get("agent_name")}
                for row in [*recent, *latest_rows]
                if (
                    str(row.get("member_email") or email or "")
                    .strip()
                    .lower()
                    == member
                )
            ]
            truth_by_member[member] = _truth_adapter().truth_status_for_member(
                member, member_rows, service=_truth_service())
        except Exception as exc:
            print(f"[agent-runner] truth status unavailable for member scope "
                  f"{_email_hash(member)}: {type(exc).__name__}: {exc}",
                  flush=True)

    for r in latest_rows:
        item = {
            "ts": str(r["ts"]) if r["ts"] else None,
            "bytes": int(r["bytes_written"] or 0),
            "brief_path": r["brief_path"] or "",
        }
        member = str(r.get("member_email") or email or "").strip().lower()
        indexes = truth_by_member.get(member) or {}
        truth = (indexes.get("by_runner_run_id") or {}).get(str(r.get("id")))
        if truth:
            item["truth"] = truth
        latest_per_agent[r["agent_name"]] = item

    recent_public = []
    for r in recent:
        item = {
            "agent": r["agent_name"],
            "status": r["status"],
            "ts": str(r["ts"]) if r["ts"] else None,
            "bytes": int(r["bytes_written"] or 0),
            "brief_path": r["brief_path"] or "",
        }
        member = str(r.get("member_email") or email or "").strip().lower()
        indexes = truth_by_member.get(member) or {}
        truth = (indexes.get("by_runner_run_id") or {}).get(str(r.get("id")))
        if truth:
            item["truth"] = truth
        recent_public.append(item)

    result = {
        "running": [
            {"id": r["id"], "agent": r["agent_name"],
             "runtime_sec": int(r["runtime_sec"] or 0),
             "started_at": str(r["started_at"]) if r["started_at"] else None}
            for r in running
        ],
        "recent_runs": recent_public,
        "latest_per_agent": latest_per_agent,
        "snapshot_at": time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime()),
    }
    agent_status._cache = (key, time.time(), result)
    return result
