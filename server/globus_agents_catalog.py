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
  - skill_path:  filesystem path to the agent's working directory
                 (the agent runtime checks `basename(skill_path)-*.md`
                 in /opt/hermes/work/ to detect "live" vs "planned")
  - schedule:    free-text — cron line OR on-demand OR human-readable
  - data_sources: list of strings shown under "Data sources" in the UI
  - capabilities: list of capability tags (`read`, `post-to-tg`, etc.)
  - can_do:      bullet list — what this agent CAN do
  - cannot_do:   bullet list — explicit boundaries (be honest!)

Optional fields:
  - name_origin: lore/why-this-name (rendered as a styled blockquote)
  - force_live:  bool — bypass the file-existence "live" detector
                 (use for agents that don't produce brief files —
                 e.g. an agent that posts directly to Telegram)

The reference impl ships 3 example agents (Research / SalesDesk /
InfraWatch). The buildwithsumit.com production catalog uses
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
        "skill_path": "/opt/hermes/.hermes/skills/research",
        "schedule": "08:00 daily (cron)",
        "data_sources": _VAULT_SOURCES + [
            _GMAIL_SOURCE,
            "Strategic notes (vault/auto/decision/, /strategy/)",
            "Incidents (vault/auto/incident/, /bug/)",
        ],
        "capabilities": ["read"],
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
        "skill_path": "/opt/hermes/.hermes/skills/sales-desk",
        "schedule": "08:30 daily (cron)",
        "data_sources": _VAULT_SOURCES + _CRM_SOURCES + [
            "Deal notes (vault/auto/deal/)",
            "Contact records (vault/auto/person/)",
            _GMAIL_SOURCE,
        ],
        "capabilities": ["read"],
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
    # Example 3: Infra Watch — health monitor with TG alert capability.
    # ─────────────────────────────────────────────────────────────────────
    {
        "name": "infra-watch",
        "role": "Infrastructure watcher (alerts to TG)",
        "summary": "Watches servers, services, deploys, crons. Surfaces "
                   "degraded services before they become outages. "
                   "Posts alerts to a Telegram channel (allow-listed).",
        "skill_path": "/opt/hermes/.hermes/skills/infra-watch",
        "schedule": "every 30 min (cron)",
        "data_sources": _VAULT_SOURCES + [
            "Server health + uptime endpoints",
            "Deploy logs (vault/auto/event/, /incident/)",
            "Cron run status",
        ],
        "capabilities": ["read", "post-to-tg"],
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
