# Globus

**Your private AI assistant that knows everything happening across your
business — every email, every CRM record, every WhatsApp and Telegram
message, every Google Drive doc, every customer conversation.**

Text chat + JARVIS-style voice. Cites every claim. Per-member-private:
nobody can read anyone else's data. Self-hosted on your own server.

Open source under [AGPL-3.0](LICENSE). The managed version lives inside
[The Automation Founders community at buildwithsumit.com](https://buildwithsumit.com/community.html).

> ⚠️ **Alpha — opinionated reference implementation.** Globus is the
> exact code running production at buildwithsumit.com. It's not yet a
> turn-key SaaS-in-a-box; expect to read the source, edit the persona,
> and wire up the integrations you care about. See [INSTALL.md](INSTALL.md)
> for the realistic install path and [ARCHITECTURE.md](ARCHITECTURE.md)
> for the module map.

## What it does

You give it:
- **Your data sources** (any subset, all optional): Google Drive,
  Gmail, Microsoft Teams (Graph API), WhatsApp Web (via a Chrome
  extension), Telegram (via Telethon), Freshsales CRM, Google
  Analytics, Obsidian zip uploads, raw markdown paste.
- **Your members** (the people who get an account on your install).

It runs three surfaces:

1. **Members text chat** — `/members/globus`. The member asks anything
   ("what should I respond to today?", "where are we on the Acme
   deal?", "what did the team decide about Q3 hiring?"). Globus calls
   tools (`search_files`, `read_file`, `search_content`,
   `list_recent_emails`, `search_whatsapp`, `search_telegram`) over
   the member's vault and answers with citations.
2. **Members voice** — same page, JARVIS-style orb. Hands-free voice
   conversation via ElevenLabs. Same brain, same tools, same data —
   just out loud. Vault-aware.
3. **Public preview** — `/globus`. Cheap public chat (Claude Haiku
   built into ElevenLabs) for visitors. No vault access. Allowlist-
   gated so an abuser can't run up your bill.

Plus a **background agent fleet** (`/members/globus/agents`) that runs
on schedules and produces briefs you read at 8 AM. Each agent
declares what data it reads + what it can and cannot do; nothing acts
without your sign-off.

## The brain

| Surface | Default LLM | Why |
|---|---|---|
| Members text chat + voice | **Claude Sonnet** via a local [OAuth proxy](scripts/claude_oauth_proxy.md) (your Claude Max subscription, zero per-token spend) | Best tradeoff of quality and speed. Falls back to Anthropic API direct (still Claude) if the proxy is down. |
| Public preview chat | **Claude Haiku 4.5** (built into ElevenLabs) | Cheap. No vault. Abuse-capped via allowlist. |
| Background vault builder | **DeepSeek-V3** (direct API) | Bulk markdown classification; Claude rate limits made batch ingestion painful. |

All swappable via the config table (see [INSTALL.md](INSTALL.md)).

## Architecture in one diagram

```
                         ┌──────────────────────────┐
                         │   Member's browser       │
                         │   (text chat + orb UI)   │
                         └────────────┬─────────────┘
                                      │
            ┌─────────────────────────┼──────────────────────────┐
            │                         │                          │
            ▼                         ▼                          ▼
  ┌─────────────────┐       ┌──────────────────┐      ┌──────────────────┐
  │  /members/      │       │  ElevenLabs      │      │  /members/       │
  │  globus  (HTML) │       │  agent (voice)   │      │  globus/agents   │
  └────────┬────────┘       └────────┬─────────┘      └────────┬─────────┘
           │                         │                         │
           │           ┌─────────────┴─────────────┐           │
           │           │  Globus server (Python)   │           │
           └──────────►│  - chat orchestrator      │◄──────────┘
                       │  - tool-use loop          │
                       │  - per-member vault       │
                       └─────────────┬─────────────┘
                                     │
            ┌────────────────────────┼─────────────────────────┐
            ▼                        ▼                         ▼
   ┌────────────────┐      ┌────────────────┐        ┌────────────────┐
   │  MySQL         │      │  Claude OAuth  │        │  Vault sources │
   │  (globus_*     │      │  proxy         │        │  (Drive/Gmail/ │
   │   tables)      │      │  127.0.0.1:8787│        │   WA/TG/CRM/   │
   └────────────────┘      └────────────────┘        │   Obsidian)    │
                                                     └────────────────┘
```

Full module map + data flow in [ARCHITECTURE.md](ARCHITECTURE.md).

## Quick start (rough)

```bash
git clone https://github.com/Build-With-Sumit/globus.git
cd globus

# 1. Install
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 2. Database (MySQL 8)
mysql -uroot -e 'CREATE DATABASE globus; CREATE USER globus IDENTIFIED BY "change-me"; GRANT ALL ON globus.* TO globus;'
mysql -uglobus -pchange-me globus < schema/globus_schema.sql

# 3. Config — copy template + fill in DB + LLM keys
cp config/.env.example .env
$EDITOR .env   # ANTHROPIC_API_KEY, DB_HOST, …

# 4. Brain — start the Claude OAuth proxy (zero-per-token via your Claude Max sub)
#    OR set GLOBUS_LLM_PROVIDER=anthropic + ANTHROPIC_API_KEY for direct.
./scripts/claude-oauth-proxy.sh  # see docs/claude-oauth-proxy.md

# 5. Run
python3 server/globus_server.py   # http://127.0.0.1:8090
```

Full install — incl. ElevenLabs voice, OAuth setup for Drive/Gmail,
WhatsApp/Telegram bridges, nginx reverse proxy — in [INSTALL.md](INSTALL.md).

## What you'll want to customize

Globus is opinionated. Bring your own:

| Thing | Where | Why |
|---|---|---|
| **Brand / persona** | `config/persona.example.md` → `config/persona.md` | The system prompt voice. Default is the buildwithsumit.com voice (frank, founder-to-founder). |
| **Agents catalog** | `server/globus_agents_catalog.py` | The reference impl ships 3 generic example agents. Replace with yours. The buildwithsumit production catalog (Mahabharata names: Drona, Vyas, Sanjay, Kripa, etc.) is intentionally NOT shipped — it's branded for Sumit's team. |
| **Capabilities block** | `server/globus_chat_helpers.py::_globus_capabilities_block` | The "what Globus IS / what it can do / what it CANNOT do" injected into every system prompt. Edit to match your install's data sources and policies. |
| **Members area chrome** | `server/html_chrome.py` + `members/body.html` | Default styling. Replace if you want a different theme. |
| **Authentication** | `server/members_auth_html.py` + `server/auth_cookies.py` | Default is email-OTP + Google OAuth. Plug in SSO / SAML / whatever via the same `is_active_member(email)` gate. |

## Status

- **v0.4 (current)** — text **and voice** chat work on a fresh install:
  sign in via OTP, vault from any combo of Obsidian zip / Google Drive /
  Gmail, chat by typing or by tapping the orb (JARVIS-style ElevenLabs
  voice over the same brain + same vault). See
  [`docs/voice-setup.md`](docs/voice-setup.md) for the ElevenLabs
  agent setup. v0.3c bridges (Telegram/WhatsApp/Teams) and v0.5 agents
  are still ahead — see [ROADMAP.md](ROADMAP.md).
- **Alpha** — works in production at buildwithsumit.com but every
  install will need hands-on setup. No managed-installer yet.
- **Roadmap** is in [ROADMAP.md](ROADMAP.md). Voice cost/latency rebuild
  (ElevenLabs → Cartesia + Deepgram + LiveKit) is the biggest v1.0 item.

## Contributing

PRs welcome. Read [CONTRIBUTING.md](CONTRIBUTING.md) first.

The project memory + design rationale lives in [docs/](docs/) — read
the ADRs (`docs/architecture/`) before proposing structural changes.

## Built with

[Claude](https://www.anthropic.com/), [DeepSeek](https://www.deepseek.com/),
[ElevenLabs](https://elevenlabs.io/), [Telethon](https://github.com/LonamiWebs/Telethon),
[PyMySQL](https://github.com/PyMySQL/PyMySQL). All standard library
otherwise — no Flask, no Django, no SQLAlchemy. The Python `http.server`
is doing the work.

## License

[AGPL-3.0](LICENSE). If you run Globus as a service for others
(SaaS), you must release your modifications under the same license.
For a managed version you don't have to host, join [The Automation
Founders](https://buildwithsumit.com/community.html).
