"""GlobusAgents helper layer — extracted from lead_server.py
2026-06-28 as refactor slice #6m. The structural enabler for
carving _ga_sidebar_html, globus_agents_html, globus_agent_run_html,
and globus_sumit_ai_html out of lead_server.

What's here (all hermes/agent-subsystem helpers, NOT the GA = Google
Analytics helpers _ga_totals / _ga_top which live elsewhere):

  - _ga_running():        live hermes processes via `ps -fu hermes`.
  - _ga_recent_runs(n):   newest /opt/hermes/work/ brief files first.
  - _ga_vault_freshness(): newest mtime on /opt/hermes/vault.
  - _ga_agent_status(a):  "live"|"planned" — agent has produced a
                          brief file? force_live override supported.
  - _ga_render_markdown(t): subset-markdown -> HTML for brief pages.
  - _ga_safe_workfile(f): filename validator + path builder
                          (prevents traversal).

All pure functions over filesystem state. No DB, no module config —
just esc from html_chrome for HTML safety in the markdown renderer.
"""
from __future__ import annotations
import os
import re
import subprocess
from html_chrome import esc


_GA_WORK_DIR = "/opt/hermes/work"

_AGENT_RUN_RE = re.compile(
    r"^(?P<slug>.+?)-(?P<date>\d{4}-\d{2}-\d{2})(?P<extras>.*)\."
    r"(?P<ext>md|txt|json)$", re.IGNORECASE)

_MD_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
_MD_INLINE_CODE_RE = re.compile(r"`([^`]+)`")


def _ga_running():
    """Live hermes-user processes. Empty list when nothing running."""
    try:
        out = subprocess.run(
            ["ps", "-fu", "hermes", "-o", "pid,etime,cmd", "--no-headers"],
            capture_output=True, text=True, timeout=5)
        if out.returncode != 0:
            return []
    except Exception:
        return []
    rows = []
    for line in out.stdout.strip().splitlines():
        parts = line.split(None, 2)
        if len(parts) < 3:
            continue
        pid, etime, cmd = parts
        lc = cmd.lower()
        if "hermes" in lc or "skill" in lc:
            rows.append({"pid": pid, "etime": etime, "cmd": cmd[:200]})
    return rows


def _ga_recent_runs(limit=20):
    """Files in /opt/hermes/work/ newest first."""
    try:
        entries = os.listdir(_GA_WORK_DIR)
    except OSError:
        return []
    out = []
    for f in entries:
        p = f"{_GA_WORK_DIR}/{f}"
        try:
            st = os.stat(p)
            agent, date = "(unknown)", ""
            m = _AGENT_RUN_RE.match(f)
            if m:
                agent = m.group("slug")
                date = m.group("date")
            out.append({"file": f, "agent": agent, "date": date,
                        "mtime": st.st_mtime, "size": st.st_size})
        except OSError:
            continue
    out.sort(key=lambda x: x["mtime"], reverse=True)
    return out[:limit]


def _ga_vault_freshness():
    """Stat /opt/hermes/vault — newest publish time."""
    try:
        st = os.stat("/opt/hermes/vault")
        return {"mtime": st.st_mtime, "ok": True}
    except OSError:
        return {"ok": False}


def _ga_agent_status(agent):
    """An agent is 'live' if it has produced at least one brief in
    /opt/hermes/work/. The previous check (os.path.isdir on skill_path
    under /opt/hermes/.hermes/skills/) failed under www-data because
    /opt/hermes/.hermes/ is mode 700 hermes-only — so every agent
    rendered as SOON even after deploy. /opt/hermes/work/ is mode 775,
    world-readable, so this check works regardless of which user the
    web server runs as.

    Brief filenames use the SKILL folder name (chief-of-staff, drona,
    sahadev, ...), not the catalog display name (sumit.ai, Dron, ...).
    Match against basename(skill_path).

    Agents that don't write briefs (e.g. Kripa posts straight to TG)
    can set 'force_live': True in their catalog entry to override the
    file-based detector."""
    if agent.get("force_live"):
        return "live"
    skill_path = agent.get("skill_path") or ""
    skill_name = os.path.basename(skill_path) if skill_path else ""
    if not skill_name:
        return "planned"
    try:
        prefix = f"{skill_name}-"
        for entry in os.listdir(_GA_WORK_DIR):
            if entry.startswith(prefix) and entry.endswith(".md"):
                return "live"
    except OSError:
        pass
    return "planned"


def _ga_render_markdown(text):
    """Lightweight markdown -> HTML for GlobusAgents briefs. Handles
    headers, bullets, bold, inline code, paragraphs. Not a real markdown
    parser — just the subset our briefs actually use."""
    if not text:
        return ""
    out = []
    in_list = False
    for raw in text.split("\n"):
        line = raw.rstrip()
        if line.startswith("### "):
            if in_list:
                out.append("</ul>"); in_list = False
            out.append(f"<h4>{esc(line[4:])}</h4>")
        elif line.startswith("## "):
            if in_list:
                out.append("</ul>"); in_list = False
            out.append(f"<h3>{esc(line[3:])}</h3>")
        elif line.startswith("# "):
            if in_list:
                out.append("</ul>"); in_list = False
            out.append(f"<h2>{esc(line[2:])}</h2>")
        elif line.lstrip().startswith(("- ", "* ", "+ ")):
            if not in_list:
                out.append("<ul>"); in_list = True
            indent = len(line) - len(line.lstrip())
            item = line.lstrip()[2:]
            style = f' style="margin-left:{indent*8}px"' if indent else ""
            item_html = esc(item)
            item_html = _MD_BOLD_RE.sub(r"<strong>\1</strong>", item_html)
            item_html = _MD_INLINE_CODE_RE.sub(r"<code>\1</code>", item_html)
            out.append(f"<li{style}>{item_html}</li>")
        elif not line.strip():
            if in_list:
                out.append("</ul>"); in_list = False
            out.append("")
        else:
            if in_list:
                out.append("</ul>"); in_list = False
            html = esc(line)
            html = _MD_BOLD_RE.sub(r"<strong>\1</strong>", html)
            html = _MD_INLINE_CODE_RE.sub(r"<code>\1</code>", html)
            out.append(f"<p>{html}</p>")
    if in_list:
        out.append("</ul>")
    return "\n".join(out)


def _ga_safe_workfile(filename):
    """Validate a /opt/hermes/work/ filename. Prevents path traversal."""
    if not filename or "/" in filename or "\\" in filename or ".." in filename:
        return None
    if not filename.endswith((".md", ".txt")):
        return None
    return f"{_GA_WORK_DIR}/{filename}"
