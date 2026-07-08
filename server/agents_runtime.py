"""GlobusAgents runtime — extracted from lead_server.py 2026-06-28 as
refactor slice #6z. The "fire agents + observe what they're doing +
per-member work/vault dir plumbing" layer.

What's here:
  - Constants:
      _AGENT_RUN_LOGS, _AGENT_WORK_DIR, _AGENT_ALLOWED,
      _AGENTS_OWNER_EMAIL, _AGENT_VAULT_PARENT
  - _is_agents_owner(email): cheap email-equality check used as the
    permission gate everywhere agents are surfaced.
  - member_work_dir(email) / ensure_member_work_dir(email):
    per-member Hermes work dir for agent briefs. Owner keeps the
    legacy flat /opt/hermes/work/ for cron compat; new members get
    a hashed subdir.
  - member_vault_dir(email) / ensure_member_vault_dir(email):
    per-member vault dir (mirrors the work-dir naming so the bash
    side can derive the same path from the email alone).
  - globus_agent_status(): live snapshot of agent activity
    (running pids, recent log-tail runs, latest brief per agent)
    — polled every 5s by the chat-console UI, memoized 3s.
  - globus_agent_run_async(agent_name, member_email): fire
    run-agent.sh as the hermes user, in background. Owner-only,
    allow-list-gated, no-double-fire check.

Module deps: cfg (db_helpers), os + subprocess + re + glob + time +
hashlib (stdlib). configure() loaded the config table eagerly so
the cfg() call below executes at import-time without race.
"""
from __future__ import annotations
import os
from db_helpers import cfg


_AGENT_RUN_LOGS = [
    "/var/log/sumit-ai.log",
    "/var/log/globus-agents.log",
    # On-demand runs (fired by globus_agent_run_async from lead_server,
    # which runs as www-data and cannot write to /var/log/sumit-ai.log
    # or /var/log/globus-agents.log — those are owned by root and written
    # by cron). This file is pre-created with www-data:www-data 664.
    "/var/log/globus-agent-ondemand.log",
]
_AGENT_WORK_DIR = "/opt/hermes/work"
_AGENT_VAULT_PARENT = "/opt/hermes/vaults"

# Allow-list — agents the chat / run_agent tool may trigger
_AGENT_ALLOWED = frozenset({
    "chief-of-staff", "drona", "nakul", "vidur", "vyas",
    # Sanjay — EmpMonitor customer-chat silence watcher (cron every 15m)
    "sanjay",
})

# Owner of the Hermes agents in this single-tenant deployment. Today's
# Drona/Sanjay/Vidur/Vyas/Nakul/Chief-of-staff all read Sumit's vault
# and post to Sumit's TG groups — so non-owner members must NOT be able
# to trigger them, read their briefs, or see their live status. Once
# per-member agent copies exist, this constant becomes per-row data.
# (Sumit 2026-06-26.)
# Config-driven so the open-source build carries no personal default:
# set AGENTS_OWNER_EMAIL (or reuse GLOBUS_FIRST_MEMBER_EMAIL, the seed
# member) to nominate the single-tenant owner. If neither is set, no one
# is treated as owner — owner-only agents stay locked until configured.
_AGENTS_OWNER_EMAIL = (cfg("AGENTS_OWNER_EMAIL", "")
                       or cfg("GLOBUS_FIRST_MEMBER_EMAIL", "")).lower()


def _is_agents_owner(email):
    return bool(email) and email.lower() == _AGENTS_OWNER_EMAIL


def member_work_dir(email):
    """Per-member Hermes work dir for agent briefs. Owner keeps the
    legacy flat /opt/hermes/work/ for backwards compat with the
    existing cron + scripts; new members get a hashed subdir so
    their briefs don't intermix with the owner's history.

    Returns the absolute path (does NOT create it — call
    ensure_member_work_dir() for that).
    """
    if not email:
        return _AGENT_WORK_DIR
    if _is_agents_owner(email):
        return _AGENT_WORK_DIR
    import hashlib as _h
    h = _h.sha1(email.lower().encode("utf-8")).hexdigest()[:16]
    return os.path.join(_AGENT_WORK_DIR, h)


def ensure_member_work_dir(email):
    """Make sure the member's work dir exists with group-rwx so the
    hermes-user-run agents AND the www-data web server can both read +
    write. Idempotent."""
    path = member_work_dir(email)
    if path == _AGENT_WORK_DIR:
        return path  # legacy flat dir, already exists + perms set
    try:
        if not os.path.isdir(path):
            os.makedirs(path, mode=0o2775, exist_ok=True)
        # Best-effort group + perms — if running as www-data we may
        # not be able to chgrp, but the parent dir is hermes:hermes
        # 2775 (setgid), so newly created subdirs inherit the group.
    except Exception:
        pass
    return path


def member_vault_dir(email):
    """Per-member Hermes vault dir. Owner uses the legacy shared
    /opt/hermes/vault/; non-owners get
    /opt/hermes/vaults/<sha1(email)[:16]>/ which mirrors the work-dir
    naming so the bash side can derive the same path from the email
    alone. Does NOT create anything — call ensure_member_vault_dir()."""
    if not email or _is_agents_owner(email):
        return "/opt/hermes/vault"
    import hashlib as _h
    h = _h.sha1(email.lower().encode("utf-8")).hexdigest()[:16]
    return os.path.join(_AGENT_VAULT_PARENT, h)


def ensure_member_vault_dir(email):
    """Create the per-member vault dir with a README so run-agent.sh's
    existence check passes. The vault is intentionally thin — the
    *-member SKILL.md files read live via globus-tool, so we don't seed
    any curated content. Idempotent. Owner is a no-op (uses the legacy
    shared vault).

    Permissions: relies on /opt/hermes/vaults/ being hermes:hermes 2775
    (setgid). With www-data in the hermes group, subdir creation +
    group inheritance Just Works. If the parent dir is missing or
    permission-denied, we log and return — the run-agent.sh gate will
    refuse the run so the failure is surfaced loudly anyway."""
    path = member_vault_dir(email)
    if path == "/opt/hermes/vault":
        return path
    try:
        if not os.path.isdir(path):
            os.makedirs(path, mode=0o2775, exist_ok=True)
        readme = os.path.join(path, "README.md")
        if not os.path.exists(readme):
            with open(readme, "w") as f:
                f.write(
                    "# Your per-member Hermes vault\n\n"
                    "This vault is intentionally thin. Your agents read live data\n"
                    "via the `globus-tool` shell command (Freshsales / Gmail /\n"
                    "WhatsApp / Drive) — there is no curated file mirror here.\n\n"
                    f"Member: {email}\n"
                )
    except Exception as e:
        print(f"[ensure_member_vault_dir] {email}: "
              f"{type(e).__name__}: {e}", flush=True)
    return path


def globus_agent_status():
    """Snapshot of agent activity for the chat-side console. Returns:
      {
        running:        [{pid, agent, started_at, runtime_sec}],
        recent_runs:    [{ts, agent, bytes, brief_path, status}],  # last 15
        latest_per_agent: {agent: {ts, bytes, brief_path}},
      }
    Cheap: just reads log tails + ps + directory listing. Polled every 5s
    by the console UI, so memoize for 3s to absorb burst polls."""
    import time as _time, subprocess as _sp, glob as _glob, re as _re
    cached = getattr(globus_agent_status, "_cache", None)
    if cached and (_time.time() - cached[0]) < 3.0:
        return cached[1]

    running = []
    try:
        out = _sp.check_output(
            ["pgrep", "-af", "/opt/hermes/bin/run-agent.sh "],
            text=True, stderr=_sp.DEVNULL, timeout=2).strip()
        for line in out.splitlines():
            parts = line.split(None, 1)
            if len(parts) != 2: continue
            pid, cmd = parts
            if cmd.startswith("bash -c ") or cmd.startswith("sudo "):
                continue
            m = _re.search(r"run-agent\.sh\s+(\S+)", cmd)
            agent = m.group(1) if m else "?"
            if agent not in _AGENT_ALLOWED:
                continue
            try:
                started = _sp.check_output(
                    ["ps", "-o", "etimes=", "-p", pid],
                    text=True, timeout=1).strip()
                runtime = int(started) if started.isdigit() else 0
            except Exception:
                runtime = 0
            running.append({"pid": int(pid), "agent": agent,
                            "runtime_sec": runtime})
    except Exception:
        pass

    recent = []
    line_re = _re.compile(
        r"\[run-agent:([^\]]+)\]\s+(\S+\s+\S+\s+\S+)\s+exit=(\d+)\s+"
        r"(?:wrote=(\S+)\s+bytes=(\d+))?")
    for log_path in _AGENT_RUN_LOGS:
        try:
            out = _sp.check_output(
                ["tail", "-n", "40", log_path],
                text=True, stderr=_sp.DEVNULL, timeout=2)
        except Exception:
            continue
        for line in out.splitlines():
            m = line_re.search(line)
            if not m:
                continue
            agent, ts, exit_code, brief, nbytes = m.groups()
            recent.append({
                "ts": ts,
                "agent": agent,
                "exit": int(exit_code),
                "bytes": int(nbytes) if nbytes else 0,
                "brief_path": brief or "",
                "status": "ok" if exit_code == "0" else "fail",
            })
    seen = set()
    uniq = []
    for r in sorted(recent, key=lambda x: x["ts"], reverse=True):
        if r["agent"] not in _AGENT_ALLOWED:
            continue
        key = (r["ts"], r["agent"])
        if key in seen: continue
        seen.add(key)
        uniq.append(r)
    recent = uniq[:15]

    latest = {}
    try:
        for path in _glob.glob(f"{_AGENT_WORK_DIR}/*-2026-*.md"):
            base = path.rsplit("/", 1)[-1]
            m = _re.match(r"^([a-z][a-z0-9\-]+?)-\d{4}-\d{2}-\d{2}-\d{4}\.md$", base)
            if not m: continue
            agent = m.group(1)
            if agent not in _AGENT_ALLOWED:
                continue
            try:
                st = os.stat(path)
            except OSError:
                continue
            existing = latest.get(agent)
            if not existing or st.st_mtime > existing["mtime"]:
                latest[agent] = {
                    "ts": _time.strftime("%Y-%m-%d %H:%M:%S UTC",
                                         _time.gmtime(st.st_mtime)),
                    "mtime": st.st_mtime,
                    "bytes": st.st_size,
                    "brief_path": path,
                }
    except Exception:
        pass
    latest_pub = {k: {"ts": v["ts"], "bytes": v["bytes"],
                      "brief_path": v["brief_path"]}
                  for k, v in latest.items()}

    result = {
        "running": running,
        "recent_runs": recent,
        "latest_per_agent": latest_pub,
        "snapshot_at": _time.strftime("%Y-%m-%d %H:%M:%S UTC", _time.gmtime()),
    }
    globus_agent_status._cache = (_time.time(), result)
    return result


def globus_agent_run_async(agent_name, member_email):
    """Fire run-agent.sh <agent> as the hermes user, in background. Returns
    dict {ok, agent, message, pid?}. Allow-list enforced.

    Owner-only until per-member agents land: non-owner members get a
    'not on your tier yet' reply rather than triggering an agent that
    reads the owner's vault data."""
    import subprocess as _sp
    if not _is_agents_owner(member_email):
        return {"ok": False, "agent": agent_name,
                "message": "Hermes agents aren't on your tier yet — "
                           "per-member agent copies are being built. "
                           "(Today's agents read the owner's data, so "
                           "they can't be safely shared.)"}
    a = (agent_name or "").strip().lower()
    if a not in _AGENT_ALLOWED:
        return {"ok": False, "agent": a,
                "error": f"agent '{a}' not in allow-list "
                         f"({sorted(_AGENT_ALLOWED)})"}
    try:
        out = _sp.check_output(
            ["pgrep", "-af", "/opt/hermes/bin/run-agent.sh "],
            text=True, stderr=_sp.DEVNULL, timeout=2).strip()
        for line in out.splitlines():
            parts = line.split(None, 1)
            if len(parts) != 2: continue
            _pid, cmd = parts
            if cmd.startswith("bash -c ") or cmd.startswith("sudo "):
                continue
            if f"/opt/hermes/bin/run-agent.sh {a}" in cmd:
                return {"ok": True, "agent": a, "already_running": True,
                        "message": f"{a} is already running"}
    except Exception:
        pass
    log = "/var/log/globus-agent-ondemand.log"
    cmd = (f"nohup sudo -u hermes -H /opt/hermes/bin/run-agent.sh "
           f"{a} >> {log} 2>&1 &")
    try:
        _sp.Popen(["bash", "-c", cmd],
                  stdin=_sp.DEVNULL, stdout=_sp.DEVNULL,
                  stderr=_sp.DEVNULL, close_fds=True, start_new_session=True)
    except Exception as e:
        return {"ok": False, "agent": a,
                "error": f"spawn failed: {type(e).__name__}: {e}"}
    print(f"[agent-run] member={member_email} fired {a}", flush=True)
    return {"ok": True, "agent": a, "message": f"{a} run started"}
