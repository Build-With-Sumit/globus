"""Narada dashboard pages.

Three pages (all member-cookie auth):
  - /members/narada                 — campaign list + create button
  - /members/narada/credentials     — per-tool API-key setup
  - /members/narada/<id>            — campaign detail (prospects, copy review,
                                       send queue, live stats, replies)

Pure render functions. Caller (globus_server.py) handles POST + the
?msg= / ?kind= banner round-trip.
"""
from __future__ import annotations
import json

from html_chrome import esc, _members_shell
from narada_plugins import list_plugins, list_available_for_member
from narada_plugins.types import AuthMethod, PluginCategory
from narada_platform_catalog import NARADA_PLATFORM_CATALOG


def _campaign_from_addr(campaign):
    """Pull sender_config.from_addr off a campaign row — the column comes
    back from MySQL as a JSON string (or None)."""
    sc = campaign.get("sender_config")
    if isinstance(sc, str) and sc.strip():
        try:
            sc = json.loads(sc)
        except Exception:
            sc = None
    return (sc.get("from_addr") or "") if isinstance(sc, dict) else ""


def _banner(message, kind):
    if not message:
        return ""
    cls = "note-err" if kind == "error" else "note-ok"
    return (f'<div class="panel" style="margin-bottom:1.4rem">'
            f'<p class="form-note {cls}" style="margin:0">'
            f'{esc(message)}</p></div>')


# ─────────────────────────────────────────────────────────────────────
# /members/narada — campaign list
# ─────────────────────────────────────────────────────────────────────

def narada_dashboard_html(email, campaigns, message=None, kind=None):
    if campaigns:
        rows = []
        for c in campaigns:
            stats = c.get("stats") or {}
            if isinstance(stats, str):
                try: stats = json.loads(stats)
                except Exception: stats = {}
            sent = (stats.get("sends_by_status") or {}).get("sent", 0)
            replied = (stats.get("sends_by_status") or {}).get("replied", 0)
            rows.append(
                f'<tr>'
                f'<td><a href="/members/narada/{int(c["id"])}">'
                f'{esc(c.get("name") or "(unnamed)")}</a></td>'
                f'<td>{esc(c.get("product") or "")}</td>'
                f'<td>{esc(c.get("sender") or "")}</td>'
                f'<td>{esc(c.get("lead_source") or "")}</td>'
                f'<td><span class="pill pill-'
                f'{"done" if c.get("status")=="done" else "v0" if c.get("status")=="sending" else "soon"}">'
                f'{esc(c.get("status") or "")}</span></td>'
                f'<td>{int(sent)} sent / {int(replied)} replied</td>'
                f'<td class="muted small">{esc(str(c.get("created_at") or "")[:16])}</td>'
                f'</tr>')
        list_html = (
            '<table class="table" style="width:100%;font-size:.92rem">'
            '<thead><tr><th>Name</th><th>Product</th>'
            '<th>Sender</th><th>Lead source</th>'
            '<th>Status</th><th>Stats</th>'
            '<th>Created</th></tr></thead>'
            f'<tbody>{"".join(rows)}</tbody></table>')
    else:
        list_html = ('<p class="muted">No campaigns yet. '
                     'Click "New campaign" or ask Globus in chat — '
                     '<em>"Narada, run a 50-lead campaign for X."</em></p>')

    available_senders = list_available_for_member(
        PluginCategory.SENDER, email)
    available_sources = list_available_for_member(
        PluginCategory.LEAD_SOURCE, email)
    if not (available_senders and available_sources):
        setup_warning = (
            '<div class="panel" style="background:#FFF5EC;'
            'border-color:#F0CB95;margin-bottom:1rem">'
            '<h3 style="margin:0 0 .3rem">Setup needed</h3>'
            '<p class="muted small" style="margin:0">'
            'Narada needs at least one '
            f'{"sender" if not available_senders else ""}'
            f'{" and " if not available_senders and not available_sources else ""}'
            f'{"lead source" if not available_sources else ""} '
            'connected before you can run a campaign. '
            '<a href="/members/narada/credentials">Set them up &rarr;</a></p>'
            '</div>')
    else:
        setup_warning = ""

    body = (
        '<a class="back-link" href="/members/globus">'
        '&larr; Back to Globus</a>'
        '<span class="eyebrow">Globus &middot; Narada (Outbound Agent)</span>'
        '<h1>Narada</h1>'
        '<p class="lead">End-to-end cold outreach. Pick your tools, '
        'describe your ICP, review the copy, hit send. Replies auto-'
        'classify and pipe to your CRM.</p>'
        + _banner(message, kind)
        + setup_warning +
        '<div style="display:flex;justify-content:space-between;'
        'align-items:center;margin-bottom:1rem">'
        '<h2 style="margin:0">Campaigns</h2>'
        '<a href="/members/narada/new" class="btn btn-primary">'
        '+ New campaign</a>'
        '</div>'
        + list_html +
        '<p class="muted small" style="margin-top:1.5rem">'
        'Or fire Narada from chat: <em>"Narada, create a campaign for '
        'VideoraIQ targeting fintech CMOs."</em> Same backend, '
        'less clicking.</p>')
    return _members_shell("Narada · Globus", body)


# ─────────────────────────────────────────────────────────────────────
# /members/narada/credentials — per-tool API-key setup
# ─────────────────────────────────────────────────────────────────────

def narada_credentials_html(email, configured_tools,
                             message=None, kind=None):
    """`configured_tools` is the set of tool slugs the member has
    credentials for (from narada_creds.list_member_credentials)."""
    plugins_by_category = {}
    for p in list_plugins():
        info = p.info()
        plugins_by_category.setdefault(info.category, []).append(info)

    sections = []
    for cat in [PluginCategory.SENDER, PluginCategory.LEAD_SOURCE,
                PluginCategory.VERIFIER, PluginCategory.CRM,
                PluginCategory.LINKEDIN]:
        infos = plugins_by_category.get(cat, [])
        if not infos:
            continue
        cards = []
        for info in infos:
            is_set = info.name in configured_tools
            badge = ('<span class="pill pill-done">connected</span>'
                     if is_set else
                     '<span class="pill pill-soon">not connected</span>')
            if info.auth_method == AuthMethod.COMPOSIO:
                action = (
                    '<a class="btn" href="/members/composio">'
                    'Manage via Composio &rarr;</a>')
            elif info.auth_method == AuthMethod.API_KEY:
                # Inline form per tool
                inputs = []
                for cred in info.requires_credentials:
                    label = cred.replace("_", " ").title()
                    inputs.append(
                        f'<label style="display:block;margin-bottom:.6rem">'
                        f'<span style="display:block;font-weight:600;'
                        f'margin-bottom:.2rem">{esc(label)}</span>'
                        f'<input type="text" name="{esc(cred)}" '
                        f'{"required" if not is_set else ""}'
                        f' placeholder="{esc(label)}" '
                        f'style="width:100%;font-family:ui-monospace,'
                        f'Menlo,monospace;font-size:.85rem;padding:.5rem;'
                        f'border:1px solid var(--line);border-radius:6px;'
                        f'background:var(--surface-sunken)"></label>')
                action = (
                    f'<form method="POST" action="/members/narada/credentials/save">'
                    f'<input type="hidden" name="tool" value="{esc(info.name)}">'
                    f'{"".join(inputs)}'
                    f'<button type="submit" class="btn btn-primary">'
                    f'{"Update" if is_set else "Save"}</button>'
                    + (f' <button type="submit" formaction="/members/narada/credentials/delete" '
                       f'class="btn">Delete</button>' if is_set else "")
                    + '</form>')
            else:
                action = ('<p class="muted small">'
                           'Custom OAuth — wire from server-side config.</p>')
            cards.append(
                '<div class="panel" style="margin-bottom:1rem">'
                '<div style="display:flex;justify-content:space-between;'
                'align-items:flex-start;gap:1rem;flex-wrap:wrap">'
                '<div style="min-width:0;flex:1">'
                f'<h3 style="margin:0 0 .3rem">{esc(info.display_name)} '
                f'{badge}</h3>'
                f'<p class="muted small" style="margin:.15rem 0">'
                f'<code>{esc(info.name)}</code></p>'
                f'<p style="margin:.6rem 0;font-size:.92rem">'
                f'{esc(info.description)}</p>'
                + (f'<p class="muted small" style="margin:.3rem 0">'
                   f'<a href="{esc(info.homepage)}" target="_blank" '
                   f'rel="noopener">{esc(info.homepage)}</a></p>'
                   if info.homepage else "")
                + '</div></div>'
                f'<div style="margin-top:.8rem">{action}</div>'
                '</div>')
        sections.append(
            f'<h2 style="margin-top:2rem">{cat.value.replace("_"," ").title()}'
            f'</h2>'
            + "".join(cards))

    # ── Full platform catalog — every planned integration, so ANY member
    #    can paste their own API key (encrypted, per-member). Tools already
    #    shown above as live plugins are skipped to avoid duplication. ──
    live = [p.info() for p in list_plugins()]
    live_slugs = {i.name.lower() for i in live}
    live_names = {i.display_name.lower() for i in live}
    by_cat = {}
    for t in NARADA_PLATFORM_CATALOG:
        if (t.get("slug", "").lower() in live_slugs
                or t.get("display_name", "").lower() in live_names):
            continue
        by_cat.setdefault(t.get("category") or "OTHER", []).append(t)
    catalog_sections = []
    for cat in sorted(by_cat):
        tools = by_cat[cat]
        cards = []
        for t in tools:
            slug = t.get("slug", "")
            is_set = slug in configured_tools
            badge = ('<span class="pill pill-done">saved</span>' if is_set
                     else '<span class="pill pill-soon">no plugin yet</span>')
            home = t.get("homepage") or ""
            home_html = (f'<a href="{esc(home)}" target="_blank" rel="noopener">'
                         f'{esc(home)}</a>' if home.startswith("http")
                         else esc(home))
            hint = t.get("key_location_hint") or ""
            cards.append(
                '<div class="panel" style="margin-bottom:.8rem">'
                '<div style="display:flex;justify-content:space-between;'
                'align-items:baseline;gap:1rem;flex-wrap:wrap">'
                f'<h3 style="margin:0 0 .2rem">'
                f'{esc(t.get("display_name") or slug)} {badge}</h3>'
                f'<span class="muted small">{esc(t.get("priority") or "")}</span>'
                '</div>'
                f'<p class="muted small" style="margin:.1rem 0">'
                f'<code>{esc(slug)}</code></p>'
                f'<p style="margin:.5rem 0;font-size:.9rem">'
                f'{esc(t.get("description") or "")}</p>'
                + (f'<p class="muted small" style="margin:.2rem 0">Signup: '
                   f'{home_html}</p>' if home else "")
                + (f'<p class="muted small" style="margin:.2rem 0">Key: '
                   f'{esc(hint)}</p>' if hint else "")
                + '<form method="POST" action="/members/narada/credentials/save" '
                  'style="margin-top:.5rem">'
                  f'<input type="hidden" name="tool" value="{esc(slug)}">'
                  '<input type="text" name="api_key" placeholder="Paste API key" '
                  'style="width:100%;max-width:440px;font-family:ui-monospace,'
                  'Menlo,monospace;font-size:.85rem;padding:.5rem;border:1px solid '
                  'var(--line);border-radius:6px;background:var(--surface-sunken)">'
                  ' <button type="submit" class="btn" style="margin-top:.4rem">'
                  f'{"Update" if is_set else "Save key"}</button>'
                  '</form>'
                '</div>')
        catalog_sections.append(
            f'<h2 style="margin-top:2rem">'
            f'{esc(str(cat).replace("_", " ").title())} '
            f'<span class="muted small">({len(tools)})</span></h2>'
            + "".join(cards))

    body = (
        '<a class="back-link" href="/members/narada">'
        '&larr; Back to Narada</a>'
        '<span class="eyebrow">Globus &middot; Narada credentials</span>'
        '<h1>Credentials</h1>'
        '<p class="lead">Wire the tools Narada uses for outbound. '
        'Each is per-member; nobody else can use your credentials.</p>'
        + _banner(message, kind)
        + "".join(sections)
        + '<hr style="margin:2.6rem 0;border:none;'
          'border-top:1px solid var(--line)">'
        '<h1 style="margin:0">Full platform catalog</h1>'
        '<p class="lead">Every integration Narada plans to support. Paste your '
        'own API key for any tool &mdash; stored encrypted, per-member. Tools '
        'with a live plugin (above) work today; keys saved here are pre-staged '
        'for when their plugin ships.</p>'
        + "".join(catalog_sections))
    return _members_shell("Narada credentials · Globus", body)


# ─────────────────────────────────────────────────────────────────────
# /members/narada/new — campaign builder
# ─────────────────────────────────────────────────────────────────────

def narada_new_campaign_html(email, message=None, kind=None,
                               send_from_accounts=None):
    senders = list_available_for_member(PluginCategory.SENDER, email)
    sources = list_available_for_member(PluginCategory.LEAD_SOURCE, email)
    verifiers = list_available_for_member(PluginCategory.VERIFIER, email)
    crms = list_available_for_member(PluginCategory.CRM, email)

    def _select(name, plugins, required=True, default_first=False):
        opts = ['<option value="">— none —</option>']
        for i, p in enumerate(plugins):
            info = p.info()
            selected = " selected" if (default_first and i == 0) else ""
            opts.append(
                f'<option value="{esc(info.name)}"{selected}>'
                f'{esc(info.display_name)}</option>')
        req = "required" if required else ""
        return (f'<select name="{esc(name)}" {req} '
                f'style="width:100%;padding:.5rem;border:1px solid var(--line);'
                f'border-radius:6px;background:var(--surface)">'
                + "".join(opts) + '</select>')

    body = (
        '<a class="back-link" href="/members/narada">'
        '&larr; Back to Narada</a>'
        '<span class="eyebrow">Globus &middot; new campaign</span>'
        '<h1>New campaign</h1>'
        + _banner(message, kind) +
        '<form method="POST" action="/members/narada/new" '
        'style="max-width:680px">'

        '<label style="display:block;margin-bottom:1rem">'
        '<span style="display:block;font-weight:600;margin-bottom:.3rem">'
        'Campaign name</span>'
        '<input type="text" name="name" required '
        'placeholder="VideoraIQ → fintech CMOs (July test)" '
        'style="width:100%;padding:.5rem;border:1px solid var(--line);'
        'border-radius:6px"></label>'

        '<label style="display:block;margin-bottom:1rem">'
        '<span style="display:block;font-weight:600;margin-bottom:.3rem">'
        'Product</span>'
        '<input type="text" name="product" required '
        'placeholder="VideoraIQ" '
        'style="width:100%;padding:.5rem;border:1px solid var(--line);'
        'border-radius:6px"></label>'

        '<label style="display:block;margin-bottom:1rem">'
        '<span style="display:block;font-weight:600;margin-bottom:.3rem">'
        'ICP description (free text)</span>'
        '<textarea name="icp_description" rows="4" required '
        'placeholder="Fintech CMOs at companies 50-500 employees who '
        'recently posted on LinkedIn about video / Loom alternatives. '
        'US + UK + IN." '
        'style="width:100%;padding:.5rem;border:1px solid var(--line);'
        'border-radius:6px;font-family:inherit"></textarea></label>'

        '<div style="display:grid;grid-template-columns:1fr 1fr;'
        'gap:1rem;margin-bottom:1rem">'
        '<label><span style="display:block;font-weight:600;margin-bottom:.3rem">'
        'Lead source</span>'
        f'{_select("lead_source", sources, default_first=True)}</label>'
        '<label><span style="display:block;font-weight:600;margin-bottom:.3rem">'
        'Verifier <span class="muted small">(optional)</span></span>'
        f'{_select("verifier", verifiers, required=False)}</label>'
        '<label><span style="display:block;font-weight:600;margin-bottom:.3rem">'
        'Sender</span>'
        f'{_select("sender", senders, default_first=True)}</label>'
        '<label><span style="display:block;font-weight:600;margin-bottom:.3rem">'
        'CRM <span class="muted small">(optional)</span></span>'
        f'{_select("crm", crms, required=False)}</label>'
        '</div>'

        '<label style="display:block;margin-bottom:1rem">'
        '<span style="display:block;font-weight:600;margin-bottom:.3rem">'
        'Send from <span class="muted small">(which of your connected '
        'mailboxes sends — Gmail sender only)</span></span>'
        '<select name="send_from" style="width:100%;padding:.5rem;'
        'border:1px solid var(--line);border-radius:6px;'
        'background:var(--surface)">'
        '<option value="" selected>— my default account —</option>'
        + "".join(f'<option value="{esc(a)}">{esc(a)}</option>'
                  for a in (send_from_accounts or []))
        + '</select></label>'

        '<label style="display:block;margin-bottom:1rem">'
        '<span style="display:block;font-weight:600;margin-bottom:.3rem">'
        'Send mode</span>'
        '<select name="send_mode" style="width:100%;padding:.5rem;'
        'border:1px solid var(--line);border-radius:6px">'
        '<option value="approve_each" selected>Approve every email '
        '(safer; recommended for new marketers)</option>'
        '<option value="autopilot">Autopilot (fire all approved drafts '
        'on send button)</option>'
        '</select></label>'

        '<button type="submit" class="btn btn-primary btn-lg">'
        'Create campaign</button>'
        '</form>'

        '<p class="muted small" style="margin-top:1.5rem;max-width:680px">'
        '<strong>What happens next:</strong> we create the campaign in '
        'draft state. From the detail page you\'ll fetch leads, '
        'verify+enrich them, draft personalised copy, review/approve, '
        'and finally send. Each step is a button click; or ask Globus '
        'in chat to do the next step for you.</p>')
    return _members_shell("New campaign · Narada", body)


# ─────────────────────────────────────────────────────────────────────
# /members/narada/<id> — campaign detail
# ─────────────────────────────────────────────────────────────────────

def narada_campaign_detail_html(email, campaign, prospects, stats,
                                  message=None, kind=None):
    name = campaign.get("name") or "(unnamed)"
    cid = int(campaign["id"])

    # Status row
    status_pills = []
    for k, v in (stats.get("prospects_by_status") or {}).items():
        status_pills.append(
            f'<span class="stat-chip"><strong>{esc(k)}</strong> '
            f'{int(v)}</span>')
    if not status_pills:
        status_pills = ['<span class="stat-chip stat-empty">'
                         'no prospects yet</span>']

    send_pills = []
    for k, v in (stats.get("sends_by_status") or {}).items():
        send_pills.append(
            f'<span class="stat-chip"><strong>{esc(k)}</strong> '
            f'{int(v)}</span>')

    # Prospect table
    if prospects:
        rows = []
        for p in prospects[:50]:
            variants = p.get("copy_variants") or []
            if isinstance(variants, str):
                try: variants = json.loads(variants)
                except Exception: variants = []
            approved = p.get("approved_variant_idx")
            preview = ""
            if variants and approved is not None and 0 <= approved < len(variants):
                preview = (variants[approved].get("subject") or "")[:60]
            elif variants:
                preview = f"{len(variants)} drafts"
            rows.append(
                f'<tr><td>{esc(str(p.get("email") or ""))[:50]}</td>'
                f'<td>{esc(str(p.get("first_name") or "") + " " + str(p.get("last_name") or ""))[:40]}</td>'
                f'<td class="muted small">{esc(str(p.get("company") or ""))[:30]}</td>'
                f'<td><span class="pill pill-soon">{esc(p.get("status") or "")}</span></td>'
                f'<td class="muted small">{esc(preview)}</td></tr>')
        prospects_html = (
            '<table class="table" style="width:100%;font-size:.9rem">'
            '<thead><tr><th>Email</th><th>Name</th><th>Company</th>'
            '<th>Status</th><th>Copy</th></tr></thead>'
            f'<tbody>{"".join(rows)}</tbody></table>')
    else:
        prospects_html = ('<p class="muted">No prospects yet. '
                           'Click "Find leads" to fetch some.</p>')

    # Action buttons (state-machine driven)
    status = campaign.get("status") or "draft"
    actions = []
    if status in ("draft", "reviewing"):
        actions.append(
            f'<form method="POST" action="/members/narada/{cid}/find-leads" '
            'style="display:inline-block;margin-right:.5rem">'
            '<input type="number" name="count" value="50" min="1" max="500" '
            'style="width:80px;padding:.4rem;margin-right:.3rem">'
            '<button type="submit" class="btn">Find leads</button></form>')
        actions.append(
            f'<form method="POST" action="/members/narada/{cid}/verify" '
            'style="display:inline-block;margin-right:.5rem">'
            '<button type="submit" class="btn">Verify emails</button></form>')
        actions.append(
            f'<form method="POST" action="/members/narada/{cid}/draft" '
            'style="display:inline-block;margin-right:.5rem">'
            '<button type="submit" class="btn">Draft copy</button></form>')
    if status in ("reviewing", "draft"):
        actions.append(
            f'<form method="POST" action="/members/narada/{cid}/send" '
            'style="display:inline-block;margin-right:.5rem">'
            '<button type="submit" class="btn btn-primary">Send approved</button></form>')
    actions.append(
        f'<form method="POST" action="/members/narada/{cid}/check-replies" '
        'style="display:inline-block;margin-right:.5rem">'
        '<button type="submit" class="btn">Check replies</button></form>')

    body = (
        '<a class="back-link" href="/members/narada">'
        '&larr; Back to Narada</a>'
        '<span class="eyebrow">Globus &middot; Narada campaign</span>'
        f'<h1>{esc(name)}</h1>'
        f'<p class="muted small">'
        f'<code>{esc(campaign.get("product") or "")}</code> · '
        f'sender <code>{esc(campaign.get("sender") or "")}</code>'
        + (f' (from <code>{esc(_campaign_from_addr(campaign))}</code>)'
           if _campaign_from_addr(campaign) else "") +
        f' · lead source <code>{esc(campaign.get("lead_source") or "")}</code> · '
        f'status <span class="pill pill-soon">{esc(status)}</span></p>'
        + _banner(message, kind) +
        '<div class="panel">'
        '<h3 style="margin-top:0">Prospect status</h3>'
        f'<div class="stat-row">{"".join(status_pills)}</div>'
        '</div>'
        '<div class="panel">'
        '<h3 style="margin-top:0">Send status</h3>'
        f'<div class="stat-row">'
        + ("".join(send_pills) if send_pills else
            '<span class="stat-chip stat-empty">no sends yet</span>')
        + '</div></div>'
        '<div class="panel">'
        '<h3 style="margin-top:0">Actions</h3>'
        + "".join(actions) +
        '</div>'
        '<div class="panel">'
        '<h3 style="margin-top:0">Add leads <span class="muted small">'
        '(paste your own)</span></h3>'
        '<p class="muted small" style="margin-top:-.4rem">One per line &mdash; '
        'a bare email, <code>First Last &lt;email&gt;</code>, or '
        '<code>email, First, Last, Company, Title</code>. Deduped '
        'automatically; suppressed addresses are skipped.</p>'
        f'<form method="POST" action="/members/narada/{cid}/import">'
        '<textarea name="leads" rows="5" '
        'placeholder="jane@acme.com, Jane, Doe, Acme, Head of Marketing&#10;'
        'john@globex.com&#10;Sam Lee &lt;sam@initech.com&gt;" '
        'style="width:100%;padding:.6rem;border:1px solid var(--line);'
        'border-radius:6px;font-family:ui-monospace,Menlo,monospace;'
        'font-size:.85rem"></textarea>'
        '<button type="submit" class="btn btn-primary" '
        'style="margin-top:.5rem">Import leads</button></form>'
        '</div>'
        '<h2 style="margin-top:1.5rem">Prospects '
        f'<span class="muted small">({len(prospects)} shown)</span></h2>'
        + prospects_html)
    return _members_shell(f"Narada · {name}", body)
