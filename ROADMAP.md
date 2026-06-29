# Globus roadmap

## v0.1 (current) — repo skeleton + module library

Shipped:

- ✅ 26 server modules extracted from the buildwithsumit reference
  implementation (chrome, page builders, LLM dispatcher, DB layer,
  agents subsystem, voice helpers).
- ✅ Full MySQL schema (`schema/globus_schema.sql`) — 18 tables, all
  per-member-isolated.
- ✅ Config templates (`.env.example`, `persona.example.md`).
- ✅ Generic example agents catalog (`globus_agents_catalog.py` ships
  Research / Sales Desk / Infra Watch — replace with yours).
- ✅ Minimal `globus_server.py` entrypoint — boots all modules
  correctly, serves the public `/globus` landing + `/api/health`,
  returns a friendly "v0.2 coming" placeholder for members routes.
- ✅ Install docs (`INSTALL.md`) covering MySQL, .env, LLM provider
  choice, systemd unit.
- ✅ Architecture docs (`ARCHITECTURE.md`) — module map, data flow,
  refactor history.

What you can do today:
- Install it, see the public landing, exercise the DB schema.
- Read the source as a reference for building your own AI assistant
  on the same shape.
- Fork it and port the v0.2 work below — PRs welcome.

## v0.2 (next) — fully working text chat

Port the remaining functions out of the buildwithsumit reference
`lead_server.py` and into this repo. The pieces:

- [ ] **Chat orchestrator** — `_globus_run_tools_loop` + `globus_chat_send`
  (~420 lines). The actual tool-use loop the LLM runs.
- [ ] **Heavier tool implementations** — `globus_read_file` (needs Drive
  download helpers), `globus_list_recent_emails` (needs gmail-delta
  sync), `globus_send_telegram_via_bot` (TG bot API + per-bot
  allow-list checks).
- [ ] **Member auth flow** — OTP email, Google OAuth login, cookie
  session lifecycle. Pieces are in place (`members_db`, `auth_cookies`,
  `members_auth_html`), just need the route handlers in
  `globus_server.py`.
- [ ] **Vault upload endpoint** — `/members/globus/upload` (zip + paste).
  Wires up `globus_extract_md_from_zip` + `globus_save_vault`.
- [ ] **Routes** — `/members`, `/members/globus`, `/members/globus/setup`,
  `/members/login`, `/members/login/google/start`, `/members/login/google/callback`.

After v0.2, a member can sign up, upload an Obsidian zip, and have a
working text-chat conversation with Globus over their data — no voice
or external connectors yet.

## v0.3 — vault sources (Drive, Gmail, Telegram, WhatsApp)

- [ ] **Google OAuth + Drive/Gmail sync** — `/members/connect/google/*`
  routes, OAuth callback, encrypted refresh token storage, background
  sync worker. The members_connect_html page is shipped already; need
  the OAuth flow + sync worker.
- [ ] **Background sync worker** — `start_background_sync_worker()` +
  the `sync_drive_connection`, `sync_gmail_connection`,
  `sync_gmail_delta` workers from lead_server. Largest single
  extraction — ~880 lines.
- [ ] **Telegram bridge** — Telethon daemon + `globus_telegram_messages`
  ingest. Existing reference at
  https://github.com/Build-With-Sumit/telegram-bridge.
- [ ] **WhatsApp bridge** — Chrome extension. Existing reference at
  https://github.com/Build-With-Sumit/whatsapp-bridge.
- [ ] **Microsoft Teams** — Graph API cron, OAuth, `globus_teams_messages`.

## v0.4 — voice

- [ ] **ElevenLabs custom-LLM endpoint** — `/api/globus/voice-llm/chat/completions`
  (OpenAI-shape, SSE streaming). Reference implementation:
  `_voice_build_context` + `globus_voice_llm_call` + the keepalive
  thread pattern. ~280 lines.
- [ ] **Voice token** — short-lived HMAC for `/api/globus/voice-token`.
  Modules are shipped (`voice_helpers`); needs the route + the chat
  page wiring.
- [ ] **Voice setup doc** — `docs/voice-setup.md` (port from
  buildwithsumit `docs/GLOBUS_VOICE_SETUP.md`).

## v0.5 — agents

- [ ] **Per-member agent dirs** — `agents_runtime` plumbing already
  shipped; needs the `/members/globus/agents/configure` route + the
  schedule writer.
- [ ] **Agent execution model** — separate process (not in-server)
  triggered by cron. Reference uses Hermes (`/opt/hermes/bin/run-agent.sh`)
  but it's not strictly required — anything that produces `*-{date}.md`
  files in `/opt/hermes/work/` works.
- [ ] **Sample agents** — ship 3 working example agents (research,
  sales-desk, infra-watch) that match the catalog entries. Today the
  catalog is just metadata.

## v1.0 — production-ready

- [ ] **Docker compose** — `docker-compose up` gets you a working
  Globus + MySQL + Claude OAuth proxy in one command.
- [ ] **Migration framework** — proper versioned schema migrations
  instead of "re-run the .sql, it's idempotent" pattern.
- [ ] **Plugin architecture** — pip-installable extensions for
  custom data sources + custom tools (see ADR-007 in the
  buildwithsumit reference docs).
- [ ] **Voice cost rebuild** — ElevenLabs is fine for v0.4 but rough on
  margin at scale. Migrate to Cartesia (TTS) + Deepgram (STT) +
  optional LiveKit (transport). This is also the buildwithsumit prod
  roadmap.
- [ ] **First-class public preview** — bring `public_globus_html` to a
  usable end-to-end demo with an allow-list / rate-limit story.

## Contributing

Pick an item from v0.2 or v0.3 and open an issue saying you're working
on it. The reference implementation in `Globussoft-Technologies/
buildwithsumit` (private — request access) is the source of truth for
how each piece behaves in production today; you can model your port on
it.

PRs should:
- Stay minimal (no new frameworks; use stdlib + the existing deps)
- Keep per-member isolation intact (every new DB query scoped by email)
- Add a section to `ARCHITECTURE.md` if you introduce a new module
- Update `INSTALL.md` if you add a config knob or env var
