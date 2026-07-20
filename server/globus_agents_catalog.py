"""GlobusAgents catalog metadata — replace these example agents with
your own. Each entry declares: name, role, schedule, data sources,
capabilities, and what the agent CAN / CANNOT do.

The agents UI (`/members/globus/agents`) renders one card per entry.
The chat tool `run_agent(agent)` fires the matching entry. Briefs land
in `/opt/hermes/work/{agent-name}-*.md` and are picked up by the
agent-status console.

Required fields:
  - name:        unique slug used by run_agent + brief filename
  - role:        one-line job title shown in the UI
  - summary:     paragraph shown in the catalog card
  - task_prompt: the actual prompt the agent runs against the chat
                 orchestrator. The runtime exposes only the exact tools
                 declared in this entry's tool_allowlist.
                 Write this as a clear instruction to the LLM —
                 "produce a brief that does X, Y, Z."
  - schedule:    free-text — cron line OR on-demand OR human-readable
                 (cron parsing is the cron's job, not this catalog's)
  - data_sources: list of strings shown under "Data sources" in the UI
  - capabilities: display-only capability tags (`read`, `post-to-tg`, etc.)
  - tool_allowlist: exact LLM tools this background agent may call.
                    Missing, malformed, empty, and unknown grants fail closed.
  - can_do:      bullet list — what this agent CAN do
  - cannot_do:   bullet list — explicit boundaries (be honest!)

Legacy / optional fields (only relevant if you wire a Hermes-style
external runner instead of the OSS-native agent_runner.py):
  - skill_path:  filesystem path to a Hermes SKILL.md (ignored by the
                 OSS runner — kept for backwards compat with old installs)

Optional fields:
  - name_origin: lore/why-this-name (rendered as a styled blockquote)
  - force_live:  bool — bypass the file-existence "live" detector
                 (use for agents that don't produce brief files —
                 e.g. an agent that posts directly to Telegram)

The reference implementation ships 4 built-in agents (Research / SalesDesk /
Narada / InfraWatch). The buildwithsumit.com production catalog uses
Mahabharata-named agents (Drona, Vyas, Sanjay, Kripa, etc.) — those
are NOT shipped here because they're branded for one specific team.
Define your own.
"""
from __future__ import annotations


# Map agent name → dedicated brief-viewer route (if your install has one).
# Most agents don't need a custom URL — the generic /members/globus/agents/run
# brief viewer works for all of them. Add an entry here only if you build
# a custom view for a specific agent's briefs.
_AGENT_PAGE_LINKS = {
    # "chief-of-staff": "/members/globus/chief-of-staff",
}


# Re-usable data-source string constants. Edit + extend as you wire up
# more connectors. Keeps the catalog dicts below readable.
_VAULT_SOURCES = [
    "Obsidian vault (scrubbed read-only mirror at /opt/hermes/vault)",
]
_GMAIL_SOURCE = "Gmail (via vault/auto/email/ — synced every 10 min)"
_CRM_SOURCES = [
    "CRM workspace (Freshsales — contacts, accounts, deals)",
]


GLOBUS_AGENTS_CATALOG = [
    # ─────────────────────────────────────────────────────────────────────
    # Example 1: Research agent — reads, summarizes, never writes.
    # ─────────────────────────────────────────────────────────────────────
    {
        "name": "research",
        "role": "Research Agent (read-only)",
        "summary": "Daily research brief — scans your vault + latest "
                   "emails + CRM and produces a markdown digest of "
                   "what changed and what needs your attention. "
                   "Read-only; never sends or modifies anything.",
        "name_origin": "The simplest possible agent shape. Use this as "
                       "your template when adding new agents.",
        "task_prompt": (
            "Produce my morning research brief.\n\n"
            "1. List_recent_emails (last 7 days) and surface the 3-5 "
            "threads that most need a response from me.\n"
            "2. Search_files for anything in my vault tagged with "
            "decision/, strategy/, or incident/ that was modified in "
            "the last week.\n"
            "3. If I have telegram or whatsapp data, search for any "
            "open questions directed at me in the last 3 days.\n\n"
            "Output a single markdown brief with three sections "
            "(Inbox, Vault changes, Open questions). Be specific — "
            "cite the file/email/chat each item came from. Keep it "
            "scannable: bullets, no fluff. Do NOT take any action; "
            "this is read-only."
        ),
        "schedule": "08:00 daily (cron)",
        "data_sources": _VAULT_SOURCES + [
            _GMAIL_SOURCE,
            "Strategic notes (vault/auto/decision/, /strategy/)",
            "Incidents (vault/auto/incident/, /bug/)",
        ],
        "capabilities": ["read"],
        "tool_allowlist": [
            "search_files",
            "read_file",
            "search_content",
            "list_recent_emails",
            "search_whatsapp",
            "search_telegram",
        ],
        "can_do": [
            "Read across all connected data sources",
            "Identify what changed since last brief",
            "Rank items by recency + apparent priority",
            "Draft replies to your inbox (DRAFT only — never sends)",
            "Produce a structured markdown brief to /opt/hermes/work/",
        ],
        "cannot_do": [
            "Send any message (email, SMS, Telegram, Slack)",
            "Modify or write to the vault",
            "Auto-dispatch other agents (recommendations only)",
            "Take any action on your behalf",
        ],
    },

    # ─────────────────────────────────────────────────────────────────────
    # Example 2: Sales Desk — pipeline review + outreach drafts.
    # ─────────────────────────────────────────────────────────────────────
    {
        "name": "sales-desk",
        "role": "Sales Desk (read-only, draft outreach)",
        "summary": "Daily sales-desk audit — pipeline health, stalled "
                   "deals, today's ranked action list, draft follow-up "
                   "messages, CRM hygiene flags. Drafts but never sends.",
        "task_prompt": (
            "Audit my sales pipeline for the day.\n\n"
            "1. Search_files for the most recent CRM exports / pipeline "
            "trackers / deal notes in my vault.\n"
            "2. List_recent_emails (last 14 days) with sender_filter "
            "scoped to anyone outside my own domain (likely "
            "prospects/customers).\n"
            "3. Identify: deals that look stalled (no activity >7d), "
            "deals where I owe the next move, any new inbound that "
            "hasn't been triaged.\n\n"
            "Output a markdown brief with: (a) top 5 ranked actions "
            "for today with a one-line draft response each, (b) CRM "
            "hygiene flags (missing fields, possible duplicates), (c) "
            "stalled deals worth a nudge. Draft language only — never "
            "send anything."
        ),
        "schedule": "08:30 daily (cron)",
        "data_sources": _VAULT_SOURCES + _CRM_SOURCES + [
            "Deal notes (vault/auto/deal/)",
            "Contact records (vault/auto/person/)",
            _GMAIL_SOURCE,
        ],
        "capabilities": ["read"],
        "tool_allowlist": [
            "search_files",
            "read_file",
            "search_content",
            "list_recent_emails",
        ],
        "can_do": [
            "Daily pipeline review across all CRM workspaces",
            "Surface stalled / closing-soon / demo-no-close deals",
            "Triage every new inbound lead",
            "Draft ready-to-paste follow-up messages (DRAFT only)",
            "Flag CRM hygiene issues (duplicates, missing fields)",
        ],
        "cannot_do": [
            "Send any outreach",
            "Modify CRM data",
            "Move deals between stages",
            "Auto-merge duplicates (flags only — humans approve)",
        ],
    },

    # ─────────────────────────────────────────────────────────────────────
    # Narada — the Outbound Agent. Drives the full plugin platform at
    # /members/narada. Run-now produces a brief of current campaign
    # status + suggested next moves; the actual campaign lifecycle
    # (create / find leads / draft / send / check replies) is driven
    # via chat tools or the dashboard, not this catalog entry.
    # ─────────────────────────────────────────────────────────────────────
    {
        "name": "narada",
        "role": "Narada — Outbound Agent",
        "summary": (
            "End-to-end cold outreach. Picks lead sources, drafts "
            "personalised copy, sends via your chosen mailbox, "
            "classifies replies, pipes hot ones to your CRM. "
            "Pluggable across 120+ tools (Prospeo + Gmail + Freshsales "
            "in v1; Smartlead / Apollo / Lemlist / Hubspot / Heyreach "
            "and more land as you provide credentials). Full UI at "
            "/members/narada."),
        "name_origin": (
            "Narada Muni is the Mahabharata's celestial messenger — "
            "the original go-between who plants ideas and connects "
            "people who'd otherwise never meet. Outbound, basically."),
        "task_prompt": (
            "Give me a brief summary of my Narada outbound state right "
            "now.\n\n"
            "1. Call narada_list_campaigns to list every campaign.\n"
            "2. For each non-'done' campaign, call narada_campaign_stats "
            "to get live prospect/send/reply counts.\n"
            "3. Call narada_check_replies on any 'sending' campaign to "
            "pull fresh replies.\n\n"
            "Output a tight markdown brief: campaign table (name, "
            "status, sent/replied counts), then any prospects needing "
            "copy approval, then suggested next moves ('campaign X has "
            "12 drafts waiting review at /members/narada/X', etc.). "
            "Be specific; cite numbers; never invent data."
        ),
        "schedule": "08:30 daily (cron) — recommended",
        "data_sources": [
            "globus_narada_campaigns / _prospects / _sends / _replies "
            "tables",
            "Live plugin calls (Gmail reply detection, etc.)",
        ],
        "capabilities": ["read", "draft-copy", "send-on-approval"],
        # Run-now produces a status brief. Interactive chat owns campaign
        # creation, drafting, and sending, so this runtime grant excludes them.
        "tool_allowlist": [
            "narada_list_campaigns",
            "narada_campaign_stats",
            "narada_check_replies",
        ],
        "can_do": [
            "Survey all campaigns + surface anything needing attention",
            "Pull fresh replies via the campaign's sender plugin",
            "Suggest concrete next moves (links to dashboard pages)",
            "Run the full ICP→leads→copy→send pipeline via chat tools",
        ],
        "cannot_do": [
            "Send emails autonomously without member trigger",
            "Modify campaign settings without member request",
            "Bypass per-member suppression list",
            "Use credentials another member set up",
        ],
    },

    # ─────────────────────────────────────────────────────────────────────
    # Example 3: Infra Watch — health monitor with TG alert capability.
    # ─────────────────────────────────────────────────────────────────────
    {
        "name": "infra-watch",
        "role": "Infrastructure watcher (alerts to TG)",
        "summary": "Watches servers, services, deploys, crons. Surfaces "
                   "degraded services before they become outages. "
                   "Posts alerts to a Telegram channel (allow-listed).",
        "task_prompt": (
            "Run my infrastructure health check.\n\n"
            "1. Search_files for any incident notes, deploy logs, or "
            "monitoring digests modified in the last 24 hours.\n"
            "2. List_recent_emails (last 1 day) with subject_filter "
            "'alert' OR 'down' OR 'failed' OR 'incident' to surface "
            "any alerting that hit my inbox.\n\n"
            "Output a short markdown brief: GREEN (all clear) / YELLOW "
            "(watching) / RED (act now). Be specific about what you "
            "saw and where. If there's nothing to flag, say so in one "
            "line — don't pad. This catalog entry COULD post to TG via "
            "the send_telegram_via_bot tool, but the OSS default is "
            "draft-only — paste the alert text and let the operator "
            "decide whether to actually send."
        ),
        "schedule": "every 30 min (cron)",
        "data_sources": _VAULT_SOURCES + [
            "Server health + uptime endpoints",
            "Deploy logs (vault/auto/event/, /incident/)",
            "Cron run status",
        ],
        "capabilities": ["read", "post-to-tg"],
        # The OSS task is draft-only. A health-check run cannot turn the
        # display label above into Telegram write permission.
        "tool_allowlist": [
            "search_files",
            "read_file",
            "search_content",
            "list_recent_emails",
        ],
        "can_do": [
            "Monitor server uptime + service health",
            "Flag failed deploys, cron misses, db slowness",
            "Aggregate alerts from per-project monitors into one view",
            "Post alerts to a Telegram channel (your allow-list)",
        ],
        "cannot_do": [
            "Restart services or kill processes",
            "Modify config or deploy code",
            "Auto-page anyone (drafts alerts; you triage)",
        ],
    },
]
