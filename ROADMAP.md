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

## v0.6 (current) — Narada, the Outbound Agent

Globus's first marketer-facing product feature. End-to-end cold-
outreach platform with a plugin architecture — any tool in the 12
categories from the PRD plugs in by implementing one of 5 protocols.
See [`docs/prd/narada-outbound-agent.md`](docs/prd/narada-outbound-agent.md).

### Phase 0 shipped — plugin spine

- ✅ `server/narada_plugins/` package — protocols (LeadSource /
  Verifier / Sender / CRM / LinkedInChannel), types (dataclasses for
  Lead, ICPFilters, VerifyResult, SendResult, Reply, …), registry
  with auto-loader that walks the package and self-registers every
  plugin.
- ✅ `server/narada_core.py` — campaign + prospect + send state
  machine. Suppression check on every queue_send (default-deny).
  Per-member ownership via WHERE clauses on every read.
- ✅ `server/narada_creds.py` — per-member, per-tool Fernet-encrypted
  credential vault. Reuses `GLOBUS_OAUTH_ENCRYPTION_KEY` (one key
  per install).
- ✅ `server/narada_copy.py` — LLM-driven 3-variant copy gen with
  safety-rule system prompt + winning-angle memory + JSON-output
  parsing + fallback variant on LLM fail.
- ✅ Schema — 6 new tables: `globus_narada_campaigns / _prospects /
  _sends / _suppression / _credentials / _angle_memory`.

### Phase 1 shipped — first-wave plugins + UI + LLM tools

- ✅ **Gmail sender** (`narada_plugins/gmail_composio.py`) — sends
  via member's own Gmail/Workspace through Composio's managed OAuth.
  Native reply detection via the same connection. ~1500/day cap.
- ✅ **Prospeo** (`narada_plugins/prospeo.py`) — LeadSource AND
  Verifier from the same subscription. People search + email find +
  verify, all 1-credit-per-call.
- ✅ **Freshsales CRM** (`narada_plugins/freshsales.py`) — upserts
  contacts + creates deals + logs activities. Per-member subdomain +
  API key.
- ✅ **Dashboard** (`server/narada_html.py`) at `/members/narada`:
  campaign list, credentials manager, campaign builder, campaign
  detail with state-machine action buttons (find leads / verify /
  draft / send / check replies).
- ✅ **8 LLM tools** wired into the chat orchestrator:
  `narada_create_campaign`, `narada_find_leads`, `narada_draft_copy`,
  `narada_send_campaign`, `narada_check_replies`,
  `narada_campaign_stats`, `narada_list_campaigns`,
  `narada_list_plugins`. `_V03_TOOLS` not-registered set is back to
  empty.
- ✅ **Agent catalog entry** — Narada appears at
  `/members/globus/agents` as a card; "Run now" produces a brief of
  current campaign state + suggested next moves.
- ✅ **Members landing tile** — direct link from `/members`.

### Phase 2+ — queued (each plugin ships as you provide API keys)

Remaining plugins from the PRD's top-10 lists, ordered by likely
unlock first:

- Smartlead, Lemlist, EmailBison, Instantly, Apollo Sequences,
  Reply.io, Mailshake, custom SMTP — sender expansion
- Apollo, Hunter, Clay, ZoomInfo, RocketReach, FindyMail —
  lead source + enrichment expansion
- NeverBounce, ZeroBounce, MillionVerifier, BriteVerify — verifier
  expansion
- HubSpot, Salesforce, Pipedrive, Close, Attio (mostly via Composio
  — fast to land)
- Heyreach, Dripify, Expandi, Lemlist (LinkedIn), Phantombuster —
  LinkedIn outbound (TOS-disclaimer-gated)
- Smartlead/Lemwarm/Warmup-Inbox warmup plugins
- WhatsApp (Twilio/Wati/AiSensy/Meta Cloud API)
- Phone/voice (AirCall/Dialpad/Orum/JustCall)

Each is ~1-2 hours of plugin work once credentials are in. No
architecture changes after Phase 0 + 1.

## v0.7 (in progress) — Globus for Organizations

Optional multi-tenant **employee portals**: one company gets its own host
(e.g. `globus.acme.com`) where employees self-enroll with their company
email, and each chats with Globus grounded strictly on their OWN connected
data. Entirely opt-in — with no `organizations` rows the server behaves
exactly as the single-tenant install it is today.

Landed:

- ✅ **Isolation data layer** — `server/org_db.py`. Authorization on an org
  host is `(arrival Host → org_id) INTERSECT (email is an active
  org_member)`; it never consults the single-tenant member check. Every
  predicate **fails closed** (a DB error denies), a suspended org denies
  rather than falling through to the single-tenant site, domain matching is
  exact (no `acme.com.evil.com` confusion), and `auto_enroll` writes only
  `org_members` so an employee never becomes a single-tenant member.
- ✅ **Schema** — `organizations`, `org_domains`, `org_members`,
  `org_agent_grants`, shipped data-free.
- ✅ **Default-private agent sharing** — an employee sees an agent only once
  an admin grants it to everyone, their department, or them personally.
- ✅ **Tests** — `tests/test_org_db.py` covers the isolation properties
  above (31 checks, hermetic: no DB or network).
- ✅ **Config knobs** — `ORG_PORTAL_HOSTS` (fail-closed host→slug fallback),
  optional separate `ORG_GOOGLE_OAUTH_*` client, `ORG_GOOGLE_LOGIN_ENABLED`,
  legal-page identity.

- ✅ **The portal itself** — host gate + routes + UI. Email one-time-code
  sign-in (domain-gated, and it answers identically for an unregistered
  domain so it can't be used to enumerate tenants or addresses), optional
  "Continue with Google", employee chat, self-connect (Drive/Gmail), the
  admin console (sharing grants + team/roles), and pre-auth legal pages
  whose operator identity comes from config, defaulting to the org's name.
- ✅ **Allow-list routing** — on an org host only the org plane answers;
  anything not explicitly allow-listed 404s, so an org host can never serve
  the operator's own single-tenant surfaces. A small set of already
  per-email-scoped routes (chat API, Google connect callback/sync) is shared
  rather than duplicated, and only after the employee is confirmed active.
- ✅ **Tests** — `tests/test_org_gate.py`, 40 checks against the real handler
  with stubbed I/O: deny-by-default, no-fall-through, non-admin 404s, no
  tenant enumeration, and that the gate is a **no-op on a plain
  single-tenant install** (including when the org tables don't exist).
- 📄 See INSTALL.md → "Enable an org portal".

- ✅ **Shared agents for orgs — the grant model now has a consumer.** The
  reconciliation turned out to be simpler than feared: agent runs are already
  keyed by `member_email`, so employees are isolated for free, and the
  dashboard renders from a catalog + status. So the org surface is the normal
  one with the catalog **filtered by the employee's grant set**.
  - **The run route re-authorizes.** Filtering the dashboard is presentation,
    not access control — the single-tenant run handler knows nothing about
    grants, so falling through to it would let any employee run any agent in
    the catalog by posting its slug. `/members/globus/agents/run` checks the
    grant set itself and 404s otherwise.
  - **Admins see the whole catalog** without granting it to themselves,
    otherwise the first admin of a new org faces an empty page and no way to
    fill it.
  - **No grants yet gets an explanation, not an empty page** — an empty
    dashboard reads as "this is broken"; the page says the workspace is
    private by default and who can change that. The home page hides the
    Agents link entirely until something is shared.

## v0.8 (current) — Email intelligence

Two cron'd passes over a connected Gmail mailbox, plus a daily roll-up.
An inbox produces far more mail than anyone can read, but only a little of
it needs a decision — so a cheap pass files what it recognises, and an
expensive pass reads only what's left.

- ✅ **Tier 1 — triage** (`email_intel.triage_mailbox`): a cheap model files
  mail into an operator-defined taxonomy from `From`/`Subject`/snippet only,
  in batches. Optional deterministic sender→label rules run first, because a
  lookup table is cheaper, always right, and — the real reason — *consistent*:
  a model spells the same vendor three ways across runs and the taxonomy
  quietly fragments.
- ✅ **Tier 2 — reason** (`email_intel.reason_mailbox`): a stronger model reads
  the full body of only the mail Tier 1 left unfiled, judges it against the
  operator's business context, and records `{category, urgency, action}` plus
  one "needs action" label. Urgency is defined by *response time*, not tone —
  marketing urgency is not urgency.
- ✅ **The layer rule.** Tier 2 finds unfiled mail by asking whether a message
  carries **any** Gmail user label (`Label_*` ids) — it couples to the
  *existence of a decision*, never to Tier 1's taxonomy. Buckets can be added
  or renamed freely, and a human filing something by hand removes it from
  Tier 2's scope for free.
- ✅ **Non-destructive by construction.** The only Gmail write available is
  add-label; there is no archive/move/delete helper for these agents to reach
  for. Nothing here ever sends email.
- ✅ **Heartbeat-gated digest.** Every run stamps a per-mailbox beacon —
  *including* the runs that find nothing — and the digest refuses to report
  all-clear for any mailbox whose beacon is stale or absent, naming the dead
  one and how long it's been. Without this, an empty table and a dead pipeline
  are indistinguishable, and the digest sends a confident daily all-clear over
  a pipeline that stopped weeks ago.
- ✅ **Cost bounded under upstream failure.** The listing cap and the
  model-call cap are separate knobs — *cap the spend, never the sight* — so a
  small budget can't silently shorten the lookback window. Overflow is
  announced and deferred, never dropped.
- ✅ **Tests** — `tests/test_email_intel.py`, 41 hermetic checks (no DB, no
  network, no LLM) over the heartbeat gate, parse-failure handling, digest
  chunking, and env-flag parsing.
- 📄 See INSTALL.md → "Enable email intelligence".

Next:

- A pluggable notifier interface (today: Telegram, else stdout).
- Optional archiving as a separate, explicitly opted-in capability — never a
  side effect of a taxonomy bucket.

## v0.9 (current) — Sales desk

A daily ranked call list, a short priorities brief, and a pipeline hygiene
report — pushed into chat rather than a dashboard nobody opens. **Read-only:
it never writes to a CRM and never sends email.**

The shape: `gather → bound → dedup → rank (batched LLM, global indices) →
band-merge → fail open to a deterministic sort → chunk → deliver → beacon`.
Deterministic code does only what is *not* judgment — eligibility, dedup,
bounding, arithmetic. The model decides what matters now and what the next
step is, because a status-lookup sort can't tell you that a lead who replied
"send me pricing" three days ago outranks a fresher one who never answered.

- ✅ **Batched ranking with global indices.** Ranking is batched because the
  reliability ceiling is on OUTPUT rows: ask for one line per lead and the
  model drops rows long before the input runs out — and a truncated ranking
  looks exactly like a complete one. Indices are global so batches merge, and
  the four priority bands are ABSOLUTE (never "rank these relative to each
  other") so independently-ranked batches stay comparable.
- ✅ **Nothing is silently dropped.** A lead the model skipped is appended with
  a derived band and flagged, never omitted. A hallucinated index outside the
  batch is rejected; an unknown band is coerced rather than dropping the lead.
- ✅ **A partial ranking is discarded, not shipped.** Below an 85% parse floor
  the whole ranking is thrown away and the deterministic order is used — a
  short list reads as a complete one to whoever gets it.
- ✅ **Fails open everywhere.** Ranking down → deterministic order. Brief down →
  list posts without it. A broken lead source → the others still run. A sales
  team that doesn't get its list has no fallback; they just don't call anyone.
- ✅ **Fails closed on an empty feed.** An empty call list is indistinguishable
  from a quiet day, so it raises loudly and alerts the operator instead.
- ✅ **Never posts an error as content.** A model failure returns empty, and an
  implausibly short brief is treated as a failure — a fluent refusal is
  well-formed and would otherwise sail into the team channel as the brief.
- ✅ **Stage names are data.** `{status: {callable, weight, terminal}}` in
  config; no status string appears in logic, and an unknown stage stays
  callable. Timezone is config too — never an inline offset.
- ✅ **Beacon on every completion** + failure DM to an ops target, so "the desk
  stopped" is queryable and the team channel never sees a stack trace.
- ✅ **Tests** — `tests/test_sales_desk.py`, 36 hermetic checks.
- 📄 See INSTALL.md → "Enable the sales desk".

Known limit — **reading a CRM is an open extension point.** The bundled CRM
plugins implement a WRITE protocol (upsert/create/log) and deliberately do not
read, so the built-in source reads this install's own outbound pipeline
instead. Adding a per-vendor read (pagination, field hydration, rate limits) is
real work and is left as a documented seam rather than faked.

## v0.10 (current) — Opportunity tracker

Any pursuit you send into the world and then lose track of: job applications,
partnership pitches, grant or CFP submissions, sponsorship asks. You send
dozens, replies trickle back over weeks from addresses that look nothing like
where you sent them, and "where does this stand?" quietly becomes "no idea".

Records each outbound opportunity, reads **your** mailbox to match replies
back to the right one, classifies what kind of reply it is, and reports the
funnel plus what's gone quiet. **Read-only on mail; it never sends anything.**

- ✅ **The distinction that carries the whole thing:** an automated screener or
  assessment is a *response*, but it is **not** a human conversation. It's
  checked FIRST, because the wording deliberately overlaps ("complete a short
  assessment", "your video interview") and folding them together inflates the
  one number you actually care about.
- ✅ **Marketing is excluded outright.** Job alerts, newsletters and "we're
  hiring!" blasts match a sender domain perfectly and would otherwise register
  as replies from that company.
- ✅ **Stages only move FORWARD.** Replies arrive out of order; a templated
  acknowledgement landing after an interview invitation must not rewind the
  funnel. Re-running over the same mail is a no-op.
- ✅ **Conservative matching.** Sender domain is authoritative; name matching
  requires two distinctive tokens (one only if that's all the org has), and
  all-generic names like "The Solutions Company" never match. A wrong match
  silently rewrites an unrelated opportunity's history — a miss just reads as
  "no response yet", which is at least honest.
- ✅ **Model fallback is OFF by default** — the patterns handle the vast
  majority, and a tracker that silently costs money per inbound message is a
  bad default. When on, an unparseable answer leaves the message unclassified
  rather than guessing it into the funnel.
- ✅ **Stale list, not auto-chasing.** Chasing is a judgment call, so it hands
  you the candidates and stops. If you want outbound, Narada already does it
  properly with suppression and per-send caps.
- ✅ **Tests** — `tests/test_opportunity_tracker.py`, 46 hermetic checks.
- 📄 See INSTALL.md → "Enable the opportunity tracker".

**Deliberately not included.** The private system this is derived from also
automates *acquiring* and *submitting* opportunities — logged-in scraping of a
job board via a throwaway account, and driving third-party application forms
end to end (including fetching emailed verification codes). Those are omitted
on purpose: they violate the platforms' terms, the scraping half relies on a
burner account precisely because it gets banned, and mass-submitting forms
without a human reading them is not a thing this project should hand out. What
generalises — and what's here — is the record-keeping and the reply
classification.

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

- ✅ **Operator quality-of-life** (v1.0d) — `scripts/add_member.py`
  (CLI member creation, no SQL needed), `scripts/check_install.py`
  (pre-flight validator with colour OK/WARN/FAIL output), and a
  deep-mode `GET /api/health?deep=1` JSON endpoint with per-subsystem
  ok/error status (DB / storage / Fernet / persona / LLM provider).
  The shallow `/api/health` stays cheap for load balancer probes.
- [ ] **Migration framework** — proper versioned schema migrations
  instead of "re-run the .sql, it's idempotent" pattern.
- [ ] **Plugin architecture** — pip-installable extensions for
  custom data sources + custom tools (see ADR-007 in the
  buildwithsumit reference docs).
- [ ] **Voice cost rebuild** — ElevenLabs is fine for v0.4 but rough on
  margin at scale. Migrate to Cartesia (TTS) + Deepgram (STT) +
  optional LiveKit (transport).
- ✅ **First-class public preview** (v1.0e) — opt-in anonymous demo
  chat at the bottom of `/globus`. Enabled by setting
  `GLOBUS_PUBLIC_CHAT_ENABLED=1` in config or env. Strict guardrails:
  no vault access, no tools, no member data; per-IP sliding window
  (5/hour) + per-IP DB-backed daily cap (25/day) + install-wide
  daily cap (`GLOBUS_PUBLIC_CHAT_MAX_DAILY`, default 500); 500-char
  input, 600-token output; X-Forwarded-For aware for reverse-proxy
  installs; every request audited in `globus_public_chat_log`
  (ip + status + char counts, no message body — PII-conscious).
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
