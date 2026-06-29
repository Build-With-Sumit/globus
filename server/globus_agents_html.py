"""GlobusAgents dashboard pages — extracted from lead_server.py
2026-06-28 as refactor slice #6p. Now possible because the underlying
_ga_* helper layer (#6m) + GLOBUS_AGENTS_CATALOG metadata (#6o) +
_globus_shell chrome (#6f) all live in their own modules.

What's here:
  - _ga_sidebar_html(): right-side panel embedded inside the unified
    /members/globus chat page. Compact view of running processes,
    agent catalog, recent briefs, vault freshness. Sumit asked for
    the agent view to live INSIDE Globus, not on a separate URL —
    this is that panel.
  - globus_agents_html(email): standalone /members/globus/agents
    full-page dashboard. More detail than the sidebar: full agent
    catalog cards with capabilities/data sources/can-do/cannot-do
    grids, plus a recent-runs table.

Both pure HTML — no DB. Module deps: _globus_shell (globus_chrome),
esc (html_chrome), _ga_* helpers (globus_agents_helpers),
GLOBUS_AGENTS_CATALOG + _AGENT_PAGE_LINKS (globus_agents_catalog).
"""
from __future__ import annotations
from datetime import datetime
from html_chrome import esc
from globus_chrome import _globus_shell
from globus_agents_helpers import (
    _ga_running,
    _ga_recent_runs,
    _ga_vault_freshness,
    _ga_agent_status,
)
from globus_agents_catalog import GLOBUS_AGENTS_CATALOG, _AGENT_PAGE_LINKS


def _ga_sidebar_html():
    """Right-side panel for the unified /members/globus page. Includes
    the full GlobusAgents dashboard content (Running now, Recent runs,
    Agents catalog, Vault freshness) — per Sumit's direction the agent
    view lives INSIDE Globus, not on a separate dashboard URL."""
    running = _ga_running()
    recent = _ga_recent_runs(limit=8)
    fresh = _ga_vault_freshness()
    fresh_str = (
        datetime.utcfromtimestamp(fresh["mtime"]).strftime("%Y-%m-%d %H:%M UTC")
        if fresh.get("ok") else "unknown")

    if running:
        run_html = "".join(
            '<div style="background:var(--surface-sunken);padding:.5rem .7rem;'
            'border-radius:6px;margin-bottom:.3rem;border-left:3px solid #2A8000;'
            'font-size:.82rem">'
            f'<strong>PID {esc(r["pid"])}</strong> '
            f'&middot; {esc(r["etime"])}<br>'
            f'<span class="muted" style="font-size:.72rem">{esc(r["cmd"][:140])}</span></div>'
            for r in running)
    else:
        run_html = '<p class="muted small" style="margin:0">Idle.</p>'

    if recent:
        recent_items = "".join(
            '<li style="margin:.25rem 0">'
            f'<a href="/members/globus/agents/run?file={esc(r["file"])}" '
            f'style="font-size:.82rem;color:var(--text)"><strong>{esc(r["agent"])}</strong> '
            f'<span class="muted" style="font-size:.72rem">'
            f'· {datetime.utcfromtimestamp(r["mtime"]).strftime("%b %d %H:%M")} '
            f'· {r["size"]:,} b</span></a></li>'
            for r in recent)
        recent_html = (
            f'<ul style="margin:0;padding-left:1.1rem">{recent_items}</ul>')
    else:
        recent_html = '<p class="muted small" style="margin:0">No briefs yet.</p>'

    agent_cards = []
    for a in GLOBUS_AGENTS_CATALOG:
        status = _ga_agent_status(a)
        if status == "live":
            badge = ('<span style="background:#2A8000;color:#fff;'
                     'font-size:.62rem;padding:.1rem .4rem;border-radius:4px;'
                     'font-weight:600;letter-spacing:.04em">LIVE</span>')
        else:
            badge = ('<span style="background:#999;color:#fff;'
                     'font-size:.62rem;padding:.1rem .4rem;border-radius:4px;'
                     'font-weight:600;letter-spacing:.04em">SOON</span>')
        href = _AGENT_PAGE_LINKS.get(a["name"])
        link_open = (f'<a href="{href}" style="text-decoration:none;color:inherit;'
                     f'display:block">' if href else '<div>')
        link_close = '</a>' if href else '</div>'
        origin_compact = a.get("name_origin") or ""
        origin_html = (
            f'<div style="font-size:.7rem;color:var(--accent);'
            f'font-style:italic;margin-top:.3rem;line-height:1.3" '
            f'title="{esc(origin_compact)}">'
            f'&mdash; {esc(origin_compact[:90] + ("…" if len(origin_compact) > 90 else ""))}'
            f'</div>' if origin_compact else '')
        agent_cards.append(
            link_open +
            '<div style="padding:.6rem .75rem;border:1px solid var(--border);'
            'border-radius:6px;margin-bottom:.4rem;background:var(--surface)">'
            '<div style="display:flex;justify-content:space-between;'
            'align-items:center;margin-bottom:.1rem">'
            f'<strong style="font-size:.88rem">{esc(a["name"])}</strong>'
            f'{badge}</div>'
            f'<div class="muted" style="font-size:.72rem;margin-bottom:.15rem">'
            f'{esc(a["role"])} &middot; {esc(a["schedule"])}</div>'
            f'<div style="font-size:.76rem;color:var(--text-soft);line-height:1.4">'
            f'{esc(a["summary"])}</div>'
            + origin_html +
            '</div>' + link_close)

    vault_html = (
        f'<p class="muted small" style="margin:0;font-size:.74rem;line-height:1.5">'
        f'Scrubbed mirror at <code>/opt/hermes/vault</code><br>'
        f'Last published <strong>{esc(fresh_str)}</strong></p>')

    run_pill = ""
    if running:
        run_pill = (f' <span style="background:#2A8000;color:#fff;'
                    f'padding:.1rem .5rem;border-radius:10px;font-size:.65rem;'
                    f'font-weight:600;vertical-align:middle">'
                    f'{len(running)} running</span>')

    panel = (
        '<div style="padding:.85rem 1rem;border:1px solid var(--border);'
        'border-radius:8px;margin-bottom:.8rem;background:var(--surface)">'
        '<div class="muted small" style="text-transform:uppercase;'
        'letter-spacing:.06em;font-size:.68rem;font-weight:600;margin-bottom:.4rem">'
        '{title}</div>{body}</div>')

    return (
        '<aside style="flex:0 0 380px;position:sticky;top:1rem;'
        'margin-top:7.5rem">'
        '<div style="display:flex;justify-content:space-between;'
        'align-items:center;margin-bottom:.6rem">'
        '<h3 style="margin:0;font-size:1.15rem">GlobusAgents</h3>'
        f'{run_pill}'
        '</div>'
        '<div class="muted small" style="margin-bottom:1rem;font-size:.8rem;'
        'line-height:1.5">Autonomous workers on your data. Each one reports — '
        'never acts. Click any agent to read its work.</div>'
        + panel.format(title="Running now", body=run_html)
        + panel.format(title="Agents", body="".join(agent_cards))
        + panel.format(title="Recent briefs", body=recent_html)
        + panel.format(title="Vault", body=vault_html) +
        '<div style="margin-top:.6rem;text-align:right">'
        '<a href="/members/globus/agents" class="muted small">'
        'Detailed agent status &amp; history &rarr;</a></div>'
        '</aside>'
    )


def globus_agents_html(email):
    """Standalone /members/globus/agents dashboard — fuller than the
    sidebar. Shows running-now + recent-runs table + full agent catalog
    cards with capabilities/data/can/cannot grids."""
    running = _ga_running()
    recent = _ga_recent_runs(limit=15)
    fresh = _ga_vault_freshness()
    fresh_str = (
        datetime.utcfromtimestamp(fresh["mtime"]).strftime("%Y-%m-%d %H:%M UTC")
        if fresh.get("ok") else "unknown")

    if running:
        run_html = "".join(
            '<div style="background:var(--surface-sunken);padding:.7rem 1rem;'
            'border-radius:8px;margin-bottom:.5rem;border-left:3px solid #2A8000">'
            f'<strong>PID {esc(r["pid"])}</strong> '
            f'&middot; running {esc(r["etime"])}<br>'
            f'<span class="muted small">{esc(r["cmd"])}</span></div>'
            for r in running)
    else:
        run_html = ('<p class="muted small">No GlobusAgent is running '
                    'right now.</p>')

    if recent:
        rows = []
        for r in recent:
            when = datetime.utcfromtimestamp(r["mtime"]).strftime(
                "%Y-%m-%d %H:%M UTC")
            sz = f"{r['size']:,}"
            rows.append(
                '<tr style="border-bottom:1px solid var(--border)">'
                f'<td style="padding:.5rem .4rem"><strong>{esc(r["agent"])}</strong></td>'
                f'<td style="padding:.5rem .4rem">{esc(when)}</td>'
                f'<td style="padding:.5rem .4rem">{sz} b</td>'
                f'<td style="padding:.5rem .4rem"><span class="muted small">{esc(r["file"])}</span></td>'
                '</tr>')
        recent_html = (
            '<table style="width:100%;border-collapse:collapse;font-size:.93rem">'
            '<thead><tr style="text-align:left;color:var(--text-muted);'
            'border-bottom:1px solid var(--border)">'
            '<th style="padding:.5rem .4rem">Agent</th>'
            '<th style="padding:.5rem .4rem">When</th>'
            '<th style="padding:.5rem .4rem">Size</th>'
            '<th style="padding:.5rem .4rem">File</th></tr></thead>'
            '<tbody>' + "".join(rows) + '</tbody></table>')
    else:
        recent_html = ('<p class="muted small">No agent runs yet. '
                       'Once sumit.ai is scheduled or a sales agent is '
                       'invoked, briefs land in <code>/opt/hermes/work/</code>.</p>')

    cat_rows = []
    for a in GLOBUS_AGENTS_CATALOG:
        status = _ga_agent_status(a)
        if status == "live":
            badge = ('<span class="tag" style="background:#2A8000;color:#fff;'
                     'font-size:.7rem;padding:.15rem .5rem;border-radius:5px;'
                     'font-weight:600;letter-spacing:.04em">LIVE</span>')
        else:
            badge = ('<span class="tag" style="background:#999;color:#fff;'
                     'font-size:.7rem;padding:.15rem .5rem;border-radius:5px;'
                     'font-weight:600;letter-spacing:.04em">SOON</span>')
        sources = "".join(
            f'<li style="margin:.15rem 0">{esc(s)}</li>'
            for s in (a.get("data_sources") or []))
        caps = a.get("capabilities") or []
        caps_html = "".join(
            (f'<span style="display:inline-block;background:#E5F4E0;'
             f'color:#1F5A0A;padding:.15rem .5rem;border-radius:5px;'
             f'font-size:.78rem;font-weight:600;margin-right:.3rem;'
             f'text-transform:uppercase;letter-spacing:.04em">{esc(c)}</span>')
            for c in caps)
        can_html = "".join(
            f'<li style="margin:.2rem 0">{esc(item)}</li>'
            for item in (a.get("can_do") or []))
        cannot_html = "".join(
            f'<li style="margin:.2rem 0;color:#8a5a00">{esc(item)}</li>'
            for item in (a.get("cannot_do") or []))
        sumit_link = _AGENT_PAGE_LINKS.get(a["name"])
        sumit_link_html = (
            f'<a href="{sumit_link}" class="btn" style="font-size:.85rem;'
            f'padding:.4rem .9rem">Read latest brief &rarr;</a>'
            if sumit_link else '')
        origin = a.get("name_origin") or ""
        origin_html = (
            f'<blockquote style="margin:.5rem 0 1rem;padding:.6rem .9rem;'
            f'border-left:3px solid var(--accent);background:var(--accent-soft);'
            f'border-radius:0 6px 6px 0;font-size:.88rem;line-height:1.55;'
            f'color:var(--text-soft);font-style:italic">'
            f'<strong style="color:var(--accent);font-style:normal">'
            f'Why we named him {esc(a["name"])}:</strong> '
            f'{esc(origin)}</blockquote>'
            if origin else '')
        cat_rows.append(
            '<div style="padding:1.4rem 1.6rem;border:1px solid var(--border);'
            'border-radius:12px;margin-bottom:1.2rem;background:var(--surface)">'
            '<div style="display:flex;justify-content:space-between;'
            'align-items:flex-start;margin-bottom:.4rem">'
            '<div>'
            f'<h3 style="margin:0;font-size:1.25rem">{esc(a["name"])} '
            f'<span class="muted" style="font-size:.85rem;font-weight:500">'
            f'&middot; {esc(a["role"])}</span></h3>'
            '<p class="muted small" style="margin:.2rem 0 0">'
            f'Schedule: {esc(a["schedule"])}</p>'
            '</div>'
            f'<div>{badge}</div>'
            '</div>'
            f'<p style="margin:.6rem 0 1rem;line-height:1.5">{esc(a["summary"])}</p>'
            + origin_html +
            '<div style="margin-bottom:1rem">'
            '<div class="muted small" style="text-transform:uppercase;'
            'letter-spacing:.06em;font-size:.7rem;font-weight:600;margin-bottom:.3rem">'
            'Capabilities</div>'
            f'<div>{caps_html}</div>'
            '</div>'
            '<div style="display:grid;grid-template-columns:1fr 1fr 1fr;'
            'gap:1rem;margin-top:1rem">'
            '<div>'
            '<div class="muted small" style="text-transform:uppercase;'
            'letter-spacing:.06em;font-size:.7rem;font-weight:600;margin-bottom:.3rem">'
            'Data sources</div>'
            f'<ul style="margin:0;padding-left:1rem;font-size:.84rem;line-height:1.4">{sources}</ul>'
            '</div>'
            '<div>'
            '<div class="muted small" style="text-transform:uppercase;'
            'letter-spacing:.06em;font-size:.7rem;font-weight:600;margin-bottom:.3rem">'
            'Can do</div>'
            f'<ul style="margin:0;padding-left:1rem;font-size:.84rem;line-height:1.4">{can_html}</ul>'
            '</div>'
            '<div>'
            '<div class="muted small" style="text-transform:uppercase;'
            'letter-spacing:.06em;font-size:.7rem;font-weight:600;margin-bottom:.3rem">'
            'Cannot do</div>'
            f'<ul style="margin:0;padding-left:1rem;font-size:.84rem;line-height:1.4">{cannot_html}</ul>'
            '</div>'
            '</div>'
            + (f'<div style="margin-top:1.2rem;text-align:right">{sumit_link_html}</div>' if sumit_link_html else '')
            + '</div>')

    body = (
        '<meta http-equiv="refresh" content="10">'
        '<a class="back-link" href="/members/globus">&larr; Back to Globus</a>'
        '<span class="eyebrow">Globus &middot; agents</span>'
        '<h1>GlobusAgents</h1>'
        '<p class="lead">Autonomous workers running on your data. '
        '<strong>sumit.ai</strong> is the Chief of Staff; '
        '<strong>Globus Sales</strong> runs the sales desk; '
        'Athena, Argus, Iris, Hestia are the sales staff. '
        '<span class="muted small">Auto-refreshes every 10 seconds.</span></p>'
        '<div class="panel">'
        '<h3 style="margin-top:0">Running now</h3>'
        + run_html +
        '</div>'
        '<div class="panel">'
        '<h3 style="margin-top:0">Recent runs</h3>'
        + recent_html +
        '</div>'
        '<div class="panel">'
        '<h3 style="margin-top:0">Agents</h3>'
        + "".join(cat_rows) +
        '</div>'
        '<div class="panel">'
        '<h3 style="margin-top:0">Vault</h3>'
        '<p class="muted small">Scrubbed read-only mirror at '
        '<code>/opt/hermes/vault</code> &middot; '
        f'last published {esc(fresh_str)}. '
        'Per-record CRM data is in <code>globus_vault_files</code>; '
        'consolidated briefs at <code>vault/briefs/freshsales-*.md</code>.</p>'
        '</div>'
    )
    return _globus_shell("GlobusAgents · Globus", body)
