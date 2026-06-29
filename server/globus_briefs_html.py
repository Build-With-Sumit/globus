"""Single-brief page builders for the GlobusAgents area — extracted
from lead_server.py 2026-06-28 as refactor slice #6n. Reads brief
files from /opt/hermes/work/ and renders them with the lightweight
markdown helper.

What's here:
  - globus_agent_run_html(email, filename): generic brief viewer.
    /members/globus/agents/run?file=<filename>.
  - globus_sumit_ai_html(email): dedicated sumit.ai page —
    latest brief inline + an "earlier briefs" list.
    /members/globus/sumit-ai.

Pure I/O: read brief file, render markdown, wrap in _globus_shell.
Module deps: globus_chrome._globus_shell, html_chrome.esc, and
globus_agents_helpers._ga_render_markdown + _ga_safe_workfile.
No DB, no configure() needed.
"""
from __future__ import annotations
import os
from datetime import datetime
from html_chrome import esc
from globus_chrome import _globus_shell
from globus_agents_helpers import _ga_render_markdown, _ga_safe_workfile


def globus_agent_run_html(email, filename):
    """Render a single GlobusAgent brief file from /opt/hermes/work/."""
    path = _ga_safe_workfile(filename)
    if not path:
        body = ('<a class="back-link" href="/members/globus/agents">'
                '&larr; GlobusAgents</a>'
                '<h1>Brief not found</h1>'
                '<p class="muted">That brief filename is invalid.</p>')
        return _globus_shell("Not found · GlobusAgents", body)
    try:
        with open(path, "r", encoding="utf-8") as fh:
            content = fh.read()
    except OSError:
        body = ('<a class="back-link" href="/members/globus/agents">'
                '&larr; GlobusAgents</a>'
                '<h1>Brief not found</h1>'
                f'<p class="muted">No brief at {esc(filename)}.</p>')
        return _globus_shell("Not found · GlobusAgents", body)
    try:
        st = os.stat(path)
        when = datetime.utcfromtimestamp(st.st_mtime).strftime(
            "%Y-%m-%d %H:%M UTC")
        size = st.st_size
    except OSError:
        when, size = "unknown", 0
    body = (
        '<a class="back-link" href="/members/globus/agents">'
        '&larr; GlobusAgents</a>'
        f'<span class="eyebrow">GlobusAgents &middot; brief</span>'
        f'<h1 style="font-size:1.5rem;margin-bottom:.3rem">{esc(filename)}</h1>'
        f'<p class="muted small">{esc(when)} &middot; {size:,} bytes</p>'
        '<div class="panel" style="line-height:1.65">'
        + _ga_render_markdown(content) +
        '</div>'
    )
    return _globus_shell(filename + " · GlobusAgents", body)


def globus_sumit_ai_html(email):
    """Dedicated sumit.ai page — latest brief inline + earlier briefs list."""
    try:
        all_files = sorted(
            (f for f in os.listdir("/opt/hermes/work")
             if f.startswith("sumit-ai-")
             and f.endswith((".md", ".txt"))),
            reverse=True)
    except OSError:
        all_files = []
    header = (
        '<a class="back-link" href="/members/globus/agents">'
        '&larr; GlobusAgents</a>'
        '<span class="eyebrow">GlobusAgents &middot; sumit.ai</span>'
        '<h1>sumit.ai</h1>'
        '<p class="lead">Your Chief of Staff. Runs twice daily '
        '(08:00 + 18:00 IST). Reviews CRMs, vault, emails, finance. '
        'Surfaces what needs your attention. Routes work to specialist '
        'agents (Iris, Argus, Athena, Hestia).</p>'
    )
    if not all_files:
        body = header + (
            '<div class="panel">'
            '<p class="muted">No brief produced yet. The first scheduled '
            'run will fire at the next 02:30 or 12:30 UTC tick — or run it '
            'on demand via <code>sudo -u hermes -H '
            '/opt/hermes/bin/run-sumit-ai.sh</code>.</p>'
            '</div>')
        return _globus_shell("sumit.ai · GlobusAgents", body)
    latest = all_files[0]
    try:
        with open(f"/opt/hermes/work/{latest}", "r", encoding="utf-8") as fh:
            latest_content = fh.read()
        st = os.stat(f"/opt/hermes/work/{latest}")
        when = datetime.utcfromtimestamp(st.st_mtime).strftime(
            "%Y-%m-%d %H:%M UTC")
    except OSError:
        latest_content = "(could not read brief)"
        when = "?"
    prior = all_files[1:11]
    prior_html = ""
    if prior:
        items = "".join(
            '<li style="margin:.25rem 0">'
            f'<a href="/members/globus/agents/run?file={esc(f)}">'
            f'{esc(f)}</a></li>'
            for f in prior)
        prior_html = (
            '<div class="panel">'
            '<h3>Earlier briefs</h3>'
            f'<ul style="margin:0;padding-left:1.2rem">{items}</ul>'
            '</div>')
    body = header + (
        '<div class="panel">'
        f'<p class="muted small" style="margin-top:0">Latest brief: '
        f'<strong>{esc(latest)}</strong> &middot; {esc(when)}</p>'
        '<div style="line-height:1.65;border-top:1px solid var(--border);'
        'padding-top:.8rem;margin-top:.6rem">'
        + _ga_render_markdown(latest_content) +
        '</div></div>'
        + prior_html
    )
    return _globus_shell("sumit.ai · GlobusAgents", body)
