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

## v0.3a (current) — Google Drive vault

Shipped:

- ✅ **Google OAuth core** (`server/google_oauth.py`) — state CSRF,
  authorize URL builder, code exchange, refresh token, userinfo, revoke.
  Wired to the `cfg()` config table so credentials live in MySQL, not env.
- ✅ **OAuth connection storage** (`server/oauth_db.py`) — Fernet-encrypted
  refresh + access tokens at rest (`GLOBUS_OAUTH_ENCRYPTION_KEY` config),
  per-member CRUD, `get_valid_access_token()` with auto-refresh +
  needs_reconnect flagging on `invalid_grant`.
- ✅ **Drive API + extractors** (`server/google_drive.py`) — paginated
  list/export/download, mime classification (Docs→md, Sheets→XLSX,
  Slides→txt, plain text passthrough), full XLSX flattener that
  preserves every tab (Drive's CSV export drops everything past sheet 1),
  per-member-isolated disk cache at `RAW_DATA_DIR/{email}/{account}/...`.
- ✅ **Sync orchestrator + bg worker** (`server/sync_drive.py`) —
  5-pass sync (discover, classify, parallel-download 24-worker pool,
  index, aggregate), connection dispatcher, daemon background loop
  with stale-`running` reclaim on service start (per Sumit's prod
  gotcha — a mid-sync restart froze the CRM connector for 5 days
  before we noticed it).
- ✅ **Routes** wired into `globus_server.py`: GET `/members/connect`,
  GET `/members/connect/google/start?drive=1`, GET
  `/members/connect/google/callback`, POST
  `/members/connect/google/sync`, POST
  `/members/connect/google/disconnect`.
- ✅ **On-demand `read_file`** — when an indexed Drive file has no
  cached extract yet, the orchestrator downloads + extracts + caches
  on the fly so chat never has to wait for the full sync to complete.
- ✅ **Schema additions** — `globus_oauth_states.state_token` +
  `expires_at` + `redirect_after`, `globus_oauth_connections.user_info` +
  `drive_folder_ids` + `gmail_query`, `globus_vault_files.skip_reason` +
  `updated_at` + UNIQUE KEY on (email, source_type, external_id),
  new `globus_sync_runs` history table.

## v0.3b (current) — Gmail vault

Shipped:

- ✅ **Gmail API + body extractor** (`server/google_gmail.py`) — paginated
  message list (50K message ceiling), per-message GET (`format=full`),
  recursive text/plain → text/html-with-tag-strip fallback body extractor,
  RFC-2822 → naive UTC date parser so `modified_at` is a real TIMESTAMP
  PyMySQL can write.
- ✅ **Gmail sync workers** (`server/sync_gmail.py`):
  - `sync_gmail_connection(conn)` — full crawl, default query
    `newer_than:90d -in:spam -in:trash`, 24-worker parallel pool, per-
    message disk cache + globus_vault_files index + top-100-recent
    aggregated row.
  - `sync_gmail_delta(conn, query, max_wall_sec)` — incremental: lists
    IDs in window, dedups against vault, fetches only NEW ones with
    20-second wall-clock cap.
  - `globus_freshen_gmail(email, background=...)` — per-member
    cooldown-throttled (1/min) delta sync hook used inline by
    list_recent_emails; voice path passes `background=True` to avoid
    blowing ElevenLabs' per-turn budget.
- ✅ **Dispatcher** — `sync_drive.sync_connection` now fans out to
  `sync_gmail_connection` for the `gmail` source. Sources sorted
  fast-first (Gmail before Drive).
- ✅ **`list_recent_emails` tool** registered in the orchestrator when
  `sync_gmail` imports cleanly; `_V03_TOOLS` now only holds
  `send_telegram_via_bot` + `run_agent`. Calls `globus_freshen_gmail`
  inline so chat answers from fresh inbox state.
- ✅ **Connect-flow checkbox** — `/members/connect/google/start` accepts
  `?gmail=1` (alone or combined with `?drive=1`); error message says
  "Pick at least one source (Drive or Gmail)".
- ✅ **Bug fix in `_globus_capabilities_block`** — was crashing chat with
  `TypeError: sequence item 0: expected str instance, NoneType found`
  when a vault row had `provider_account=NULL`. Now skips those rows.

## v0.3c (current, partial) — Telegram / WhatsApp / Teams bridges

Shipped:

- ✅ **WhatsApp + Teams Chrome-extension ingest** (`server/bridge_ingest.py`):
  one 90-day HMAC token covers both endpoints. POST
  `/api/globus/whatsapp/ingest` and POST `/api/globus/teams/ingest`
  accept JSON batches of up to 500 messages (4 MB max), bulk-insert
  into `globus_whatsapp_messages` / `globus_teams_messages` with
  fingerprint-based dedup (resending the same message is a no-op).
  GET `/members/whatsapp` renders the existing setup page with a
  freshly-minted token on every load.
- ✅ **Schema deltas** — `fingerprint VARCHAR(64)` + `UNIQUE KEY
  uniq_email_fp` on both message tables. Teams gets `ms_message_id`,
  `chat_type`, `sender_user_id`, `body_type`, `ms_ts` columns the
  extension already populates.
- ✅ **Members landing tile** — new "Teams & WhatsApp" tile points
  at `/members/whatsapp`.

Outstanding:

- [ ] **Chrome extension itself** — lives in the separate
  [Build-With-Sumit/whatsapp-bridge](https://github.com/Build-With-Sumit/whatsapp-bridge)
  repo (per the existing connectors_html setup page). One extension,
  two scrapers (WA Web + teams.live.com). Sumit's reference
  implementation is the upstream — fork + customise UI as needed.
- [ ] **Telegram (Telethon daemon)** — lives in
  [Build-With-Sumit/telegram-bridge](https://github.com/Build-With-Sumit/telegram-bridge).
  The Globus server's read path (`search_telegram` tool) is already
  shipped; you just need the daemon writing into `globus_telegram_messages`.
- [ ] **Microsoft Teams via Graph API** (server-side OAuth + cron sync) —
  alternative to the Chrome-extension Teams ingest. Mirrors the
  Drive/Gmail OAuth shape. ~400 lines if you want it; the extension
  path is the lighter-weight option.

## v0.4 (current) — voice

Shipped:

- ✅ **ElevenLabs custom-LLM endpoint** — POST
  `/api/globus/voice-llm/chat/completions`. Accepts OpenAI-shape chat
  completions requests from EL's cloud, verifies the voice token,
  drops ASR-noise inputs (Whisper hallucinations like "thanks for
  watching"), runs through the chat orchestrator (same brain as text
  chat), returns either JSON or SSE stream. ~150 lines in
  `server/voice_route.py`.
- ✅ **Voice token route** — GET `/api/globus/voice-token` (cookie-
  authed) issues a fresh 6h HMAC token for long-session refresh.
  The chat page also embeds a token at render time so most loads
  never need to call this.
- ✅ **Setup doc** — `docs/voice-setup.md` walks through ElevenLabs
  agent creation, custom LLM wiring, allowlist + voice token security,
  and what's intentionally NOT in OSS v0.4 (per-turn keepalive,
  DeepSeek fallback chain, word-by-word streaming).

What's intentionally NOT in v0.4 OSS:
- Per-turn keepalive thread (prod emits filler audio during long tool
  calls so EL doesn't time out). The OSS path relies on tool calls
  finishing in under EL's per-turn budget (~25s). Keep `read_file`
  paths fast.
- True progressive SSE token streaming. OSS sends the full reply as
  one chunk + a finish chunk; EL starts TTS as soon as it arrives.
  Replace `voice_llm_sse_chunks()` if you need word-by-word.
- DeepSeek-V3 fallback chain. The chat orchestrator already handles
  provider switching via `GLOBUS_LLM_PROVIDER` — no separate voice
  routing needed.

## v0.5 (current) — agents

Shipped:

- ✅ **OSS-native agent runner** (`server/agent_runner.py`, ~250 lines)
  — no Hermes dependency. An agent is a catalog entry with a
  `task_prompt`; running it = call the chat orchestrator with that
  prompt as the member's question, write the LLM reply to disk as
  a dated markdown brief, track the run in `globus_agent_runs`.
- ✅ **`globus_agent_runs` history table** — one row per run (success
  + failure + still-running). Drives the chat-page activity console
  + the /members/globus/agents dashboard.
- ✅ **Sample agents** — `research`, `sales-desk`, `infra-watch`
  now ship with actual `task_prompt` fields. Adapt as you like;
  these run unmodified on any v0.5 install.
- ✅ **`run_agent` LLM tool** registered when `agent_runner` imports
  cleanly. Fires the agent fire-and-forget; brief lands ~30s later
  in the activity console. Member can also click "Run now" on the
  /members/globus/agents dashboard.
- ✅ **`/members/globus/agents` dashboard** (`agents_dashboard_html.py`)
  — running-now panel + recent-runs table + catalog cards with
  per-agent "last brief" badges.
- ✅ **`/api/globus/agent-status` endpoint** — JSON, polled by the
  chat-page console every 5s. Per-member scoped.
- ✅ **`scripts/run_agent.py`** — cron-friendly CLI:
  `python3 scripts/run_agent.py <agent> <member-email>`. Exits 0 on
  success / 1 on catalog-or-member error / 2 on run failure.
- ✅ **Per-member work dir** — briefs land at
  `$GLOBUS_AGENTS_WORK_DIR/<sha1(email)[:16]>/<agent>-<YYYY-MM-DD-HHMM>.md`
  (default `/var/lib/globus/agents/...`). One member can never read
  another member's briefs (FS-level isolation via path).

Reference Hermes adapter — `server/agents_runtime.py` is still shipped
for installs that already use the Hermes runner (multi-tenant agent
fleet with `/opt/hermes/bin/run-agent.sh`). The OSS-native runner is
the default; wire the Hermes adapter into the route handler if you
prefer that execution model.

## v1.0 — production-ready

Shipped:

- ✅ **Docker compose** (v1.0a) — `docker compose up` gets you a
  working Globus + MySQL in one command. Single-stage
  `python:3.12-slim` image, non-root user, named volumes for state,
  healthchecks, entrypoint handles wait-for-db + idempotent schema
  apply + SESSION_SECRET bootstrap + optional first-member seed.
- ✅ **`send_telegram_via_bot` LLM tool** (v1.0b) — closes the last
  gap in the tool dispatch chain. Member adds a TG bot via SQL
  (Fernet-encrypted token + allow-list of permitted chat_ids); LLM
  can post on member's behalf with default-deny on the allow-list +
  full audit in `globus_telegram_bot_sends`. `_V03_TOOLS` not-registered
  set is now empty (was previously holding 3 tools).

Still ahead:

- [ ] **Migration framework** — proper versioned schema migrations
  instead of "re-run the .sql, it's idempotent" pattern.
- [ ] **Plugin architecture** — pip-installable extensions for
  custom data sources + custom tools (see ADR-007 in the
  buildwithsumit reference docs).
- [ ] **Voice cost rebuild** — ElevenLabs is fine for v0.4 but rough on
  margin at scale. Migrate to Cartesia (TTS) + Deepgram (STT) +
  optional LiveKit (transport).
- [ ] **First-class public preview** — bring `public_globus_html` to a
  usable end-to-end demo with an allow-list / rate-limit story.
- ✅ **Telegram bot setup UI** (v1.0c) — `/members/telegram/bot` lets
  the member paste a BotFather token + comma-separated allow-list of
  chat_ids. Server validates the token by calling Telegram's `getMe`
  before saving (so typos / revoked tokens get caught up-front), then
  Fernet-encrypts and inserts. Page also lists existing bots and
  recent send attempts from the audit log.

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
