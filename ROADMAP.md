# Globus roadmap

## v0.1 — repo skeleton + module library

Shipped:

- ✅ 26 server modules extracted from the buildwithsumit reference
  implementation (chrome, page builders, LLM dispatcher, DB layer,
  agents subsystem, voice helpers).
- ✅ Full MySQL schema (`schema/globus_schema.sql`) — 18 tables, all
  per-member-isolated.
- ✅ Config templates (`.env.example`, `persona.example.md`).
- ✅ Generic example agents catalog (`globus_agents_catalog.py` ships
  Research / Sales Desk / Infra Watch — replace with yours).
- ✅ Install docs (`INSTALL.md`) covering MySQL, .env, LLM provider
  choice, systemd unit.
- ✅ Architecture docs (`ARCHITECTURE.md`) — module map, data flow,
  refactor history.

## v0.2 (current) — fully working text chat

Shipped:

- ✅ **Chat orchestrator** — `globus_orchestrator.py` ports
  `_globus_run_tools_loop` + `globus_chat_send` (~430 lines): the
  tool-use loop, persona loading with priority chain
  (`config/persona.md` → `persona.example.md` → default), injection
  detection, daily-cap accounting, forced-synth fallback, empty-search
  backstop, markup-leak recovery.
- ✅ **Member auth flow** — `globus_auth.py` ships email-OTP via
  SendGrid (with stderr fallback for dev), 6-digit codes hashed with
  HMAC-SHA256, 5/hour rate limit, 10-minute expiry, plus cookie
  parsing for the `bws_member` session.
- ✅ **Vault upload endpoint** — `POST /members/globus/upload` handles
  Obsidian zips (base64) + paste, wired to `globus_extract_md_from_zip`
  + `globus_upsert_source`.
- ✅ **Routes** — `globus_server.py` rewritten as a real entrypoint:
  `/`, `/globus`, `/api/health`, `/members/login` (GET+POST),
  `/members/login/code` (GET+POST), `/members/logout`, `/members`,
  `/members/globus`, `/members/globus/setup`,
  `/members/globus/upload` (POST), `/members/globus/chat` (POST),
  `/members/vault-progress`, `/api/globus/vault-progress`,
  `/api/globus/agent-status` (stub), `/api/globus/client-error`.
- ✅ **Disk-cache `read_file`** — v0.2 reads `extracted_path` files
  directly; Drive/Gmail downloads are deferred to v0.3.
- ✅ **`mark_chat_resolved` tool** — silences stale alerts;
  no-op-safe (returns clear error if `sanjay_alerts` table is absent).
- ✅ **Explicit `_V03_TOOLS` set** — `list_recent_emails`,
  `send_telegram_via_bot`, `run_agent` return a clear "not registered
  in v0.2" error if the LLM calls them.

After v0.2, a fresh installer can: sign up via OTP, upload an Obsidian
zip, and have a working text-chat conversation with Globus over their
own data — no voice, no Drive/Gmail, no WA/TG yet.

What's intentionally NOT in v0.2:
- Google OAuth login (Drive/Gmail sync is v0.3 — login stays OTP-only)
- Voice (ElevenLabs custom-LLM endpoint is v0.4)
- Agents subsystem (v0.5)

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
