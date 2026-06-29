"""Members landing page (/members) — extracted from lead_server.py
2026-06-28 as refactor slice #6c-part2.

The page authenticated members see right after signing in. Shows:
  - personalized welcome with member name + since-date
  - "This week's build" callout
  - categorized tools grid (Globus, Reels, Connect, WhatsApp, Agents,
    Vault Progress, Claude Skills)
  - community section (forum)
  - GitHub repo-access form (with status note)

Pure HTML output. Deps:
  - get_member from members_db (already a separate module)
  - esc + _members_shell from html_chrome
"""
from __future__ import annotations
from html_chrome import esc, _members_shell
from members_db import get_member


def members_html(email, gh_status=None):
    """Members-area landing page. Claude aesthetic, categorized tool grid,
    'This week's build' callout at the top, GitHub form below."""
    member = get_member(email) or {}
    first = (member.get("first_name") or "").strip() or "founder"
    member_since = member.get("created_at")
    since_str = member_since.strftime("%B %Y") if member_since else ""

    gh_note = {
        "added": '<p class="form-note note-ok">Added! Check your email / GitHub for an '
                 'invite to the <strong>Build-With-Sumit</strong> org — read access to all members repos.</p>',
        "pending": '<p class="form-note note-ok">Saved — you\'ll be added to the members team shortly.</p>',
        "invalid": '<p class="form-note note-err">That doesn\'t look like a valid GitHub username — try again.</p>',
    }.get(gh_status, "")

    # "What's new" — most recently shipped, member-facing
    this_week = (
        '<div class="this-week">'
        '<span class="this-week-badge">What\'s new</span>'
        '<h2>Globus remembers you now — plus your whole org, live</h2>'
        '<p class="muted" style="margin:0 0 .4rem">'
        'Globus is your private business-intelligence AI: connect your tools, '
        'then ask it anything about your business in plain English — text or '
        'voice. The latest, all live on this page right now:</p>'
        '<p style="margin:0 0 .3rem"><strong>🧠 Persistent memory</strong> — '
        'tell Globus &ldquo;remember I prefer INR pricing&rdquo; (by voice or '
        'text) and it sticks across every future chat and call. Say '
        '&ldquo;forget that one&rdquo; and it\'s gone. '
        '<a href="/members/globus">Open Globus →</a></p>'
        '<p style="margin:0 0 .3rem"><strong>🗺️ Org map</strong> — your whole '
        'team as a live graph: roles, responsibilities, and what each person '
        'did in the last 24 hours, pulled from your chats. Private to you. '
        '<a href="/members/org">View your org map →</a></p>'
        '<p style="margin:0 0 .3rem"><strong>🔌 Every connector live</strong> — '
        'Google Drive + Gmail, WhatsApp, Telegram and Microsoft Teams all flow '
        'into Globus now. Read-only, encrypted, fully per-member-private. '
        '<a href="/members/connect">Connect your sources →</a></p>'
        '<p style="margin:0"><strong>🤖 Agents on your data</strong> — your '
        'Chief of Staff briefs you daily; the sales agents watch pipeline, '
        'leads and support and draft outreach. They report and draft — you '
        'approve before anything sends. '
        '<a href="/members/globus/agents">See your agents →</a></p>'
        '<div class="row">'
        '<a class="btn btn-primary" href="/members/globus">Open Globus</a>'
        '<a class="btn" href="/members/org">View org map</a>'
        '<a class="btn" href="/members/connect">Connect sources</a>'
        '</div></div>'
    )

    # Tool grid — categorized
    ai_tools = (
        '<div class="tools-grid">'
        '<a class="tool-card" href="/members/globus">'
        '<div class="tc-head"><div class="tc-title"><span class="tc-icon">🧠</span> Globus</div>'
        '<span class="pill pill-new">New</span></div>'
        '<p class="tc-desc">Your private business-intelligence AI. Connect '
        'your tools, then ask it anything about your business — text or voice. '
        'Remembers your preferences, cites its sources. 500 msg/day.</p>'
        '<span class="tc-foot">Open Globus →</span></a>'
        '<a class="tool-card" href="/members/org">'
        '<div class="tc-head"><div class="tc-title"><span class="tc-icon">🗺️</span> Org map</div>'
        '<span class="pill pill-new">New</span></div>'
        '<p class="tc-desc">Your team as a live, interactive graph — roles, '
        'reporting lines, responsibilities, and each person\'s last-24h '
        'activity pulled from your chats. Private to you; salaries hidden by '
        'default.</p>'
        '<span class="tc-foot">View your org map →</span></a>'
        '<a class="tool-card" href="/members/reels">'
        '<div class="tc-head"><div class="tc-title"><span class="tc-icon">🔍</span> Reels Analyzer</div>'
        '<span class="pill pill-new">New</span></div>'
        '<p class="tc-desc">Scrape competitors\' IG reels → transcribe → Claude returns 5 reel scripts to shoot this week.</p>'
        '<span class="tc-foot">Open Reels Analyzer →</span></a>'
        '<a class="tool-card" href="/members/connect">'
        '<div class="tc-head"><div class="tc-title"><span class="tc-icon">🔌</span> Data sources</div>'
        '<span class="pill pill-v0">Beta</span></div>'
        '<p class="tc-desc">Connect your work tools so Globus understands your '
        'business. Google Drive + Gmail + Freshsales (up to 10 accounts), '
        'WhatsApp, Telegram and Microsoft Teams — all live. Read-only, '
        'encrypted, per-member-private.</p>'
        '<span class="tc-foot">Manage sources →</span></a>'
        '<a class="tool-card" href="/members/whatsapp">'
        '<div class="tc-head"><div class="tc-title"><span class="tc-icon">💬</span> WhatsApp bridge</div>'
        '<span class="pill pill-new">New</span></div>'
        '<p class="tc-desc">Chrome extension that mirrors your WhatsApp Web '
        'into Globus — every chat you click into flows in. Read-only, passive '
        'DOM observation. Open source.</p>'
        '<span class="tc-foot">Install & pair →</span></a>'
        '<a class="tool-card" href="/members/globus/agents">'
        '<div class="tc-head"><div class="tc-title"><span class="tc-icon">🤖</span> GlobusAgents</div>'
        '<span class="pill pill-new">New</span></div>'
        '<p class="tc-desc">Autonomous workers on your data. sumit.ai (Chief '
        'of Staff) briefs you twice daily; the Mahabharata-named sales staff '
        '(Drona, Sahadev, Sanjay, Arjun, Kunti) handle pipeline, leads, '
        'outreach drafts, CRM hygiene. Read-only — they report, never act.</p>'
        '<span class="tc-foot">See agents →</span></a>'
        '<a class="tool-card" href="/members/vault-progress">'
        '<div class="tc-head"><div class="tc-title"><span class="tc-icon">⚙️</span> Vault build progress</div>'
        '<span class="pill pill-v0">Live</span></div>'
        '<p class="tc-desc">Live counter of the background processor turning '
        'your raw Drive + Gmail scrape into Obsidian notes for Globus. ETA, '
        'throughput, notes by type. Auto-refreshes every 3 seconds.</p>'
        '<span class="tc-foot">Watch live →</span></a>'
        '<a class="tool-card" href="https://github.com/Build-With-Sumit/claude-skills" target="_blank" rel="noopener">'
        '<div class="tc-head"><div class="tc-title"><span class="tc-icon">🧩</span> Claude Skills</div>'
        '<span class="pill pill-new">New</span></div>'
        '<p class="tc-desc">A members-only library of Claude Code skills you drop '
        'into your own workflow — reel scripts, hooks, cold DMs, offers, VSLs, '
        'competitor teardowns, inbox triage, MCP servers &amp; more. 17 and growing.</p>'
        '<span class="tc-foot">Browse the skills →</span></a>'
        '</div>'
    )

    community = (
        '<div class="tools-grid">'
        '<a class="tool-card" href="/members/forum">'
        '<div class="tc-head"><div class="tc-title"><span class="tc-icon">💬</span> Community forum</div>'
        '<span class="pill pill-done">Live</span></div>'
        '<p class="tc-desc">Talk with other founders — ask questions, share wins, get unstuck. '
        'Sumit reads everything in the early days.</p>'
        '<span class="tc-foot">Open the forum →</span></a>'
        '</div>'
    )

    repo_form = (
        '<div class="panel">'
        '<h3 style="margin-bottom:.4rem">💻 Source code access</h3>'
        '<p class="muted small" style="margin-bottom:.7rem">Members get read access to the private '
        'closed-source repos in the Build-With-Sumit and Globussoft-Technologies orgs. Drop your '
        'GitHub username and we\'ll add you to the members team.</p>'
        '<form method="POST" action="/members/github" class="signup-form">'
        '<input type="text" name="github" required placeholder="your-github-username" aria-label="GitHub username">'
        '<button class="btn btn-primary" type="submit">Request repo access</button>'
        f'</form>{gh_note}</div>'
    )

    body = (
        '<span class="eyebrow">Members area</span>'
        f'<h1>Welcome back, {esc(first)} 🎉</h1>'
        f'<p class="lead">You\'re in, <code>{esc(email)}</code>. Membership active'
        + (f' since {esc(since_str)}' if since_str else '') + '.</p>'
        f'{this_week}'
        '<h3 class="category-head">AI tools</h3>'
        f'{ai_tools}'
        '<h3 class="category-head">Community</h3>'
        f'{community}'
        '<h3 class="category-head">Source code &amp; app access</h3>'
        f'{repo_form}'
        '<hr class="divider">'
        '<p class="muted small"><a href="/members/logout">Log out</a></p>'
    )
    return _members_shell("Members · The Automation Founders", body)
