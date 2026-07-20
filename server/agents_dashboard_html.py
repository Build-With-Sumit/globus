"""Minimal OSS agents dashboard at /members/globus/agents.

Reads from the OSS-native agent_runner (which uses the
globus_agent_runs table + per-member work dir on disk) — no
Hermes / /opt/hermes/* dependency.

The legacy globus_agents_html shipped in v0.2 is Hermes-bound; left
in place for installs that wire the Hermes adapter explicitly. This
module is what /members/globus/agents serves by default.
"""
from __future__ import annotations
import os

from html_chrome import _members_shell, esc


_TRUTH_LABELS = {
    "healthy": "Healthy",
    "verified_no_work": "Verified no work",
    "degraded_contradictory": "Contradictory",
    "failed": "Failed",
    "stale": "Stale",
}

_TRUTH_COLORS = {
    "healthy": ("#e8f7ed", "#146c2e"),
    "verified_no_work": ("#e8f4ff", "#075985"),
    "degraded_contradictory": ("#fff4db", "#8a4b08"),
    "failed": ("#feecec", "#a11a1a"),
    "stale": ("#f1efff", "#5b3aa4"),
}


def _truth_badge(truth):
    """Render one compact, explainable Truth Layer verdict."""
    if not isinstance(truth, dict) or not truth.get("verdict"):
        return (
            '<span class="muted small" title="No Truth Layer receipt '
            'was recorded for this run">not verified</span>'
        )
    verdict = str(truth["verdict"])
    label = _TRUTH_LABELS.get(verdict, verdict.replace("_", " ").title())
    background, color = _TRUTH_COLORS.get(verdict, ("#f3f4f6", "#374151"))
    reasons = truth.get("reason_codes") or []
    explanation = ", ".join(str(reason) for reason in reasons)
    title = f"Truth Layer: {label}"
    if explanation:
        title += f" — {explanation}"
    return (
        f'<span title="{esc(title)}" style="display:inline-block;'
        f'padding:.16rem .48rem;border-radius:999px;white-space:nowrap;'
        f'font-size:.75rem;font-weight:650;background:{background};'
        f'color:{color}">{esc(label)}</span>'
    )


def agents_dashboard_html(email, catalog, status):
    """Render the dashboard: catalog cards + running/recent runs.

    Args:
      email   — member email (rendered as the page subtitle)
      catalog — list of catalog entry dicts
                (agent_runner.catalog_for_member(email))
      status  — agent_runner.agent_status(email) snapshot dict
    """
    running = status.get("running") or []
    recent = status.get("recent_runs") or []
    latest = status.get("latest_per_agent") or {}

    # --- Running now panel ---
    if running:
        running_rows = "".join(
            f'<div class="panel" style="margin-bottom:.5rem;'
            f'border-left:3px solid #2A8000">'
            f'<strong>{esc(r["agent"])}</strong> · running '
            f'{int(r["runtime_sec"])}s '
            f'<span class="muted small">(started '
            f'{esc(str(r.get("started_at") or ""))})</span>'
            f'</div>'
            for r in running)
    else:
        running_rows = ('<p class="muted small">No agent is running '
                        'right now.</p>')

    # --- Recent runs table ---
    if recent:
        recent_rows = "".join(
            f'<tr>'
            f'<td>{esc(r["agent"])}</td>'
            f'<td>{esc(str(r.get("ts") or ""))}</td>'
            f'<td><span class="pill pill-{("done" if r["status"]=="ok" else "new")}">'
            f'{esc(r["status"])}</span></td>'
            f'<td>{_truth_badge(r.get("truth"))}</td>'
            f'<td class="muted small">{int(r.get("bytes") or 0):,} bytes</td>'
            f'</tr>'
            for r in recent)
        recent_html = (
            '<table class="table" style="width:100%;font-size:.92rem">'
            '<thead><tr><th>Agent</th><th>Finished</th>'
            '<th>Runner</th><th>Truth Layer</th><th>Brief size</th>'
            '</tr></thead>'
            f'<tbody>{recent_rows}</tbody></table>')
    else:
        recent_html = ('<p class="muted small">No agent runs yet. '
                       'Fire one from chat ("run research") or '
                       'schedule via cron.</p>')

    # --- Catalog cards ---
    cards = []
    for a in catalog:
        name = a.get("name", "")
        role = a.get("role", "")
        summary = a.get("summary", "")
        schedule = a.get("schedule", "on-demand")
        sources = a.get("data_sources", []) or []
        can = a.get("can_do", []) or []
        cannot = a.get("cannot_do", []) or []
        latest_for = latest.get(name)
        latest_chip = ""
        if latest_for:
            latest_chip = (
                '<span class="pill pill-done" style="margin-left:.4rem">'
                f'last brief {esc(str(latest_for.get("ts") or ""))}</span>'
                f'<span style="margin-left:.35rem">'
                f'{_truth_badge(latest_for.get("truth"))}</span>')
        else:
            latest_chip = ('<span class="pill pill-soon" '
                           'style="margin-left:.4rem">never run</span>')
        cards.append(
            '<div class="panel" style="margin-bottom:1rem">'
            f'<h3 style="margin:0 0 .3rem">{esc(role)} {latest_chip}</h3>'
            f'<p class="muted small" style="margin:.15rem 0"><code>{esc(name)}</code> '
            f'· schedule: {esc(schedule)}</p>'
            f'<p style="margin:.6rem 0">{esc(summary)}</p>'
            '<div style="display:grid;grid-template-columns:1fr 1fr;'
            'gap:1rem;margin-top:.7rem">'
            '<div><p style="margin:0 0 .3rem;font-weight:600">Data sources</p>'
            '<ul style="margin:0;padding-left:1.2rem;font-size:.85rem">'
            + "".join(f"<li>{esc(s)}</li>" for s in sources)
            + '</ul></div>'
            '<div>'
            '<p style="margin:0 0 .3rem;font-weight:600">Can do</p>'
            '<ul style="margin:0;padding-left:1.2rem;font-size:.85rem">'
            + "".join(f"<li>{esc(c)}</li>" for c in can)
            + '</ul>'
            '<p style="margin:.6rem 0 .3rem;font-weight:600;color:#b00020">'
            'Cannot do</p>'
            '<ul style="margin:0;padding-left:1.2rem;font-size:.85rem">'
            + "".join(f"<li>{esc(c)}</li>" for c in cannot)
            + '</ul></div></div>'
            '<form method="POST" action="/members/globus/agents/run" '
            'style="margin-top:.9rem">'
            f'<input type="hidden" name="agent" value="{esc(name)}">'
            '<button type="submit" class="btn btn-primary">Run now</button>'
            '</form>'
            '</div>')

    body = (
        '<a class="back-link" href="/members/globus">&larr; Back to Globus</a>'
        '<span class="eyebrow">Globus &middot; Agents</span>'
        f'<h1>Agents</h1>'
        f'<p class="lead">Long-running tasks that read your vault and '
        f'produce dated markdown briefs. A run earns a trusted Truth Layer '
        f'verdict only from persisted, measured evidence.</p>'
        '<div class="panel">'
        '<h3 style="margin-top:0">Running now</h3>'
        f'{running_rows}'
        '</div>'
        '<div class="panel">'
        '<h3 style="margin-top:0">Recent runs</h3>'
        f'{recent_html}'
        '</div>'
        '<h2 style="margin-top:2rem">Catalog</h2>'
        + "".join(cards)
        + '<p class="muted small" style="margin-top:1rem">'
        'Cron schedules go in your crontab — see '
        '<code>scripts/run_agent.py</code>. Briefs land in '
        '<code>$GLOBUS_AGENTS_WORK_DIR/&lt;email-hash&gt;/</code> '
        '(default <code>/var/lib/globus/agents/</code>).</p>')
    return _members_shell("Agents · Globus", body)
