# Globus — architecture

> Read this before proposing structural changes. The module shape is
> the result of a ~3-week extraction from a 12K-line monolith — there
> are reasons most of the seams sit where they do.

## Big picture

Globus is one Python process that serves HTTP on `127.0.0.1:8090`,
backed by MySQL on the same box. nginx terminates TLS + reverse-
proxies in production. That's it — no Flask, no Django, no ORM, no
JavaScript framework. The standard-library `http.server` does the work.

```
        ┌──────────────────────────────────────────────────────────────┐
        │                  nginx (TLS, reverse proxy)                  │
        └──────────────────────────┬───────────────────────────────────┘
                                   │
        ┌──────────────────────────▼───────────────────────────────────┐
        │     globus_server.py (ThreadingHTTPServer, port 8090)        │
        │                                                              │
        │  ┌──────────────────────────────────────────────────────┐    │
        │  │  Page builders (pure HTML, no DB calls beyond what's │    │
        │  │  passed in)                                          │    │
        │  │   - public_globus_html                               │    │
        │  │   - globus_chat_html, globus_setup_html              │    │
        │  │   - globus_agents_html, globus_briefs_html           │    │
        │  │   - members_auth_html, members_landing_html          │    │
        │  │   - members_connect_html, connectors_html            │    │
        │  │   - vault_progress_html                              │    │
        │  └──────────────────────────────────────────────────────┘    │
        │                                                              │
        │  ┌──────────────────────────────────────────────────────┐    │
        │  │  Chrome / shared HTML primitives                     │    │
        │  │   - html_chrome  (esc, _page, _members_shell, CSS)   │    │
        │  │   - globus_chrome (globus-specific CSS + shell)      │    │
        │  └──────────────────────────────────────────────────────┘    │
        │                                                              │
        │  ┌──────────────────────────────────────────────────────┐    │
        │  │  Globus chat orchestrator                            │    │
        │  │   - globus_chat_helpers  (capabilities + tool docs   │    │
        │  │                           + markup scrubber)         │    │
        │  │   - globus_tools_schema  (LLM-facing tool list)      │    │
        │  │   - globus_search        (4 pure-DB LLM tools)       │    │
        │  │   - globus_llm           (claude-oauth/anthropic/    │    │
        │  │                           deepseek dispatcher)       │    │
        │  └──────────────────────────────────────────────────────┘    │
        │                                                              │
        │  ┌──────────────────────────────────────────────────────┐    │
        │  │  Agents subsystem                                    │    │
        │  │   - agents_runtime       (per-member dir + run_async)│    │
        │  │   - globus_agents_catalog (your agent definitions)   │    │
        │  │   - globus_agents_helpers (_ga_* filesystem helpers) │    │
        │  └──────────────────────────────────────────────────────┘    │
        │                                                              │
        │  ┌──────────────────────────────────────────────────────┐    │
        │  │  Data layer                                          │    │
        │  │   - db_helpers      (db_read/write + cfg())          │    │
        │  │   - members_db      (member CRUD)                    │    │
        │  │   - auth_cookies    (session HMAC)                   │    │
        │  │   - voice_helpers   (voice-token mint/verify)        │    │
        │  │   - globus_vault_db (vault sources + chat history    │    │
        │  │                      CRUD; 3 cap constants)          │    │
        │  │   - vault_stats     (memoized vault progress stats)  │    │
        │  └──────────────────────────────────────────────────────┘    │
        └──────────────────────────────────────────────────────────────┘
                                   │
       ┌───────────────────────────┼───────────────────────────────┐
       ▼                           ▼                               ▼
  ┌─────────┐         ┌──────────────────────┐         ┌────────────────────┐
  │ MySQL 8 │         │  Claude OAuth proxy  │         │  ElevenLabs        │
  │ globus  │         │  127.0.0.1:8787      │         │  (voice STT/TTS +  │
  │  DB     │         │  (operator-supplied  │         │   custom-LLM       │
  │         │         │   loopback bridge —  │         │   webhook to our   │
  │ 35      │         │   operator-owned)    │         │   /api/globus/     │
  │ tables  │         └──────────────────────┘         │   voice-llm)       │
  └─────────┘                                          └────────────────────┘
```

## Module dependency graph

```
db_helpers      ◄─── (everything that touches MySQL)
   ▲
   ├── members_db
   ├── globus_vault_db
   ├── vault_stats
   ├── globus_search
   ├── globus_chat_helpers
   └── (etc.)

html_chrome     ◄─── (every page builder)
   ▲
   ├── globus_chrome
   │      ▲
   │      ├── globus_setup_html
   │      ├── globus_chat_html
   │      ├── globus_briefs_html
   │      └── globus_agents_html
   ├── members_auth_html
   ├── members_landing_html
   ├── members_connect_html
   ├── connectors_html
   ├── vault_progress_html
   ├── agents_html
   └── public_globus_html

voice_helpers   ◄─── (voice path + chat-page voice token)
auth_cookies    ◄─── (members area routes)
globus_llm      ◄─── (chat orchestrator + voice path + agents)
globus_tools_schema ◄─── (chat orchestrator)
globus_tool_policy ◄─── (schema filtering + dispatch authorization)
agents_runtime  ◄─── (run_agent tool + agents dashboard)
globus_agents_catalog ◄─── (agents UI + run_agent tool + exact grants)
globus_agents_helpers ◄─── (agents UI + brief viewer)

agent_runner    ◄─── (OSS orchestrator + MySQL run row)
   │
   └── globus_truth.agent_adapter
          ├── evaluator      (deterministic receipt verdict)
          ├── service        (ingest + stale-aware reads)
          ├── storage        (immutable receipt, verdict, approval + claim history)
          ├── action_gate    (fail-closed policy authorization)
          ├── approval_center (payload-free consent + fresh-Truth execution)
          ├── platform_registry (validated source-backed capability map)
          ├── judge_mode     (credential-free real-byte challenge)
          ├── outcome_challenge (read-back → receipt → gate proof)
          └── approval_challenge (changed/exact/replay local proof)
```

## Data flow — one chat turn

1. Member POSTs `{message: "..."}` to `/api/globus/chat-send`.
2. Handler authenticates the HMAC session cookie for the exact normalized
   arrival host → `email`; an org-portal session cannot be replayed on the
   single-tenant host or another portal.
3. Handler loads `vault = globus_vault_db.globus_get_vault(email)` —
   either the pre-built intelligence digest (preferred, cheap) or a
   raw aggregation of `globus_vault_sources` rows.
4. Handler appends user message to `globus_messages`, retrieves last
   N turns for context.
5. Handler builds the system prompt:
   `persona + capabilities_block(email) + tools_instructions() + vault + member_preferences`.
6. Handler calls `_globus_run_tools_loop(system, msgs, email)`:
   - loops up to `GLOBUS_CHAT_MAX_TOOL_ITERATIONS` (default 8) times
   - each iteration: `globus_llm.globus_call_chat(system, msgs, tools=GLOBUS_TOOLS)`
   - if response includes tool_calls: dispatch each to the right
     implementation (`globus_search_files`, `globus_read_file`,
     `globus_search_telegram`, …)
   - tool results appended to msgs; next iteration
   - exit when LLM returns plain text (no tool_calls)
   - empty-search backstop kicks in after 3 consecutive 0-hit search
     iterations → forced synth with `tools=None`
   - member chat keeps its normal tool surface, but `run_agent` targets are
     checked against the organization's granted agent slugs before delegation
7. Handler scrubs DSML/tool-call markup from the final text
   (`_strip_tool_markup`).
8. Handler logs assistant response to `globus_messages`, returns
   `{reply: "...", usage: {...}}`.

## Data flow — one verified OSS agent run

1. `agent_runner.run_agent_for_member()` creates a durable, member-scoped
   MySQL run row and records an aware UTC start time.
2. The runner resolves that named built-in agent's explicit non-empty
   `tool_allowlist`. A missing or empty grant fails closed before the model
   call.
3. `globus_tool_policy` filters the advertised schemas to that exact set. The
   orchestrator rechecks every returned tool name before dispatch, so a forged
   disallowed call cannot reach its implementation.
4. The real Globus orchestrator runs the catalog task over that member’s vault.
5. The runner writes the Markdown brief as exact bytes and computes the
   expected byte count and SHA-256.
6. `globus_truth.agent_adapter` reopens the artifact and independently measures
   the bytes and digest.
7. The adapter checks the actual model reply for empty, too-short,
   refusal-like, or error-like output. Private reply text is not copied into
   the Truth database.
8. It emits a versioned receipt using an install-keyed HMAC member pseudonym
   and a receipt ID deterministically bound to the durable MySQL run ID.
9. The evaluator returns one of five explainable verdicts and stores the
   immutable receipt plus verdict history in SQLite.
10. The MySQL row becomes `ok` only for a trusted verdict. The status API sends
   only compact verdict metadata to the Agents dashboard and chat activity
   console.
11. Later reads automatically age an otherwise trusted receipt to `stale` after
   its freshness deadline; polling records history only if the verdict changes.

## Data flow — credential-free Evidence Lab

1. A default-loopback `POST /api/v1/judge/challenge` creates a unique controlled
   manifest beside the Truth database using exclusive-create semantics.
2. `judge_mode` calls the same `verify_artifact_readback()` primitive used by
   the production AgentRunner adapter and persists a healthy receipt.
3. It appends exactly one byte to the manifest.
4. A new verification compares the changed file with the original size and
   SHA-256.
5. Both phase receipts commit in one SQLite transaction; either both become
   visible or neither does.
6. The dashboard shows both point-in-time observations. The response contains
   generated IDs, a relative filename, measurements, hashes, and verdicts—no
   member data or absolute filesystem paths.

## Data flow — Mission Control Action Gate

The Action Gate is deliberately downstream of persisted evidence. Callers do
not provide the verdict they want evaluated.

1. The caller supplies a persisted `storage_id`, stable `action_id`, and policy
   ID (`healthy_only` or `trusted_completion`).
2. `action_gate.ActionGate` reads the run through `TruthService.get_run()`.
   This service read reevaluates aging, so an old healthy receipt can become
   stale before authorization.
3. The gate rejects a missing, malformed, unavailable, failed, contradictory,
   or stale record. `healthy_only` also rejects verified no-work;
   `trusted_completion` accepts it.
4. The decision is bound to receipt, action, and policy. Inside the same
   immediate SQLite transaction used to insert it, an allow is rechecked
   against the latest stored verdict and freshness deadline. A concurrent
   stale or contradictory transition therefore makes the insert fail closed.
5. Only after the immutable `action_decisions` audit write succeeds does an
   allowed decision return to the caller. Audit failure raises and blocks.
6. The business workflow remains responsible for invoking its bounded,
   idempotent action only when `authorized` is true.

Decision-table update and delete triggers protect the audit trail. Exact
decision retries are idempotent; conflicting reuse of a decision ID fails.

## Data flow — Consequence Firewall approval execution

`approval_center.ApprovalCenter` adds exact human consent without letting
consent replace machine-verifiable Truth:

1. A caller proposes one action bound to a persisted receipt, action ID,
   policy, action kind, requester, risk, expiry, and payload SHA-256.
2. The repository stores that payload hash and a canonical proposal hash with
   identifiers, policy metadata, and timestamps. It never stores the raw
   action payload.
3. A person writes one immutable `approved` or `rejected` decision bound to the
   proposal hash. Approval does not authorize execution by itself.
4. An execution attempt supplies the proposal ID and payload hash. A changed
   hash, missing/rejected/expired consent, or existing claim blocks before the
   callback.
5. For an approved exact attempt, the center asks the Action Gate for a fresh
   decision and reads the exact persisted decision back. Failed,
   contradictory, stale, or policy-ineligible Truth still blocks.
6. In one immediate SQLite transaction, storage rechecks the approval
   preconditions and current Truth state while inserting a unique execution
   claim. Only the process that created that claim may invoke the bounded
   callback.
7. Completion or failure is audited without copying the payload into the
   Approval Center. A replay sees the existing claim and never invokes the
   callback again.

This is an at-most-once local coordinator. If a process dies after the external
effect but before recording completion, the claim remains indeterminate and is
not retried automatically. External exactly-once effects therefore still
require provider idempotency keys, acknowledgement read-back, and
reconciliation.

The generic CLI and `/api/v1/approvals` HTTP routes create, inspect, approve,
and reject fixed-envelope proposals. They do not accept callback code, callback
URLs, or arbitrary execution requests. Only the built-in judge endpoints own a
bounded generated local callback. The HTTP service refuses non-loopback binds;
`requested_by` and `decided_by` are local audit labels, not cryptographic
identities, so operating-system/process access to localhost is the trust
boundary.

## Data flow — credential-free Approval Center proof

`approval_challenge` exercises that boundary without credentials or external
services:

1. Stage creates one generated target in a separate local SQLite destination,
   independently reads it back, persists healthy Truth evidence, and creates a
   short-lived high-risk proposal. The outbox remains empty.
2. Rejecting the proposal ends the proof with zero actions.
3. Approving it first attempts a changed payload hash; the center returns
   `approval_scope_mismatch` and does not invoke the callback.
4. The exact payload then passes fresh Truth and creates the unique claim. One
   bounded local outbox insert succeeds.
5. Replaying the exact request returns `approval_already_consumed` and does not
   invoke the callback.
6. Independent destination read-back confirms exactly one outbox row and zero
   external calls.

## Data flow — credential-free verified business outcome

`outcome_challenge` demonstrates the whole evidence-to-action chain without
provider credentials:

1. It creates a unique challenge directory and a separate local
   `destination.sqlite` containing three generated, de-identified follow-up
   rows.
2. A new read-only SQLite connection selects and sorts those destination rows,
   canonicalizes them, and measures count plus SHA-256.
3. The 3 claimed → 3 observed receipt is persisted and evaluates `healthy`.
4. `healthy_only` authorizes one bounded insert into a local outbox. Before
   invoking it, the challenge reads the gate decision back and requires an
   exact match to the returned receipt/action/policy/verdict decision.
5. The challenge removes exactly one generated destination row.
6. Another independent read-back observes 2 rows and a changed canonical hash.
   The 3 claimed → 2 observed receipt evaluates
   `degraded_contradictory`.
7. The second gate decision is blocked; its action callback is not invoked, so
   the local outbox count remains one.

The phases are sequential by design: the first receipt, allowed action, row
deletion, and second receipt describe a real chronology. Generated row payloads
and absolute paths stay local. The HTTP response exposes only safe IDs,
relative path, counts, hashes, verdicts, decisions, and action counts.

## Mission Control capability graph

`platform-registry-v1.json` is a data-only inventory that points each
capability back to a repository path and symbol. Validation checks the schema,
allowed status/setup/risk/approval/read-back values, duplicate IDs, declared
counts, source paths, and accidental secret-like content before data reaches
the UI/API.

The v0.14 registry has 71 entries: 4 built-in agents, 20 LLM-facing tools,
33 implemented/setup-required provider adapters (9 lead-source, 8
verification, 6 sender, and 10 CRM), plus connector, channel, and model-route
entries. Registry status is not connection status:

- `native` means the implementation is part of this repository.
- `implemented/setup_required` means code exists but external setup is needed.
- `bridge/catalog` means only the integration seam/catalog is present.
- `planned` is roadmap-only.

Registry loading does not import provider modules, read environment variables
or credentials, or contact external services. The four built-in agent-to-tool
relations mirror their enforced runtime grants and are covered by a drift test;
the remaining inventory is not thereby claimed as governed, connected, or
live.

## Data flow — one voice turn (ElevenLabs custom-LLM)

ElevenLabs handles ASR (speech → text) + TTS (text → speech). Our
server is the **brain** — ElevenLabs hits our `/api/globus/voice-llm/
chat/completions` (OpenAI-shape) for each user turn.

1. Member taps voice orb → ElevenLabs SDK opens WebSocket to ElevenLabs.
2. Member speaks → ElevenLabs transcribes → calls our endpoint with
   `{messages: [...], model: "globus", stream: true}`.
3. Our endpoint verifies HMAC bearer token (`GLOBUS_VOICE_LLM_SECRET`).
4. The same chat orchestrator as text runs steps 5–7 above. The OSS route
   buffers the answer, then returns it in the OpenAI-compatible response shape.
   It does not implement the production-only per-turn keepalive or word-level
   streaming path.
5. ElevenLabs turns the returned text into speech for the member.
6. ElevenLabs speaks the answer back to the member.

## Per-member isolation (load-bearing)

EVERY table that holds member data is scoped by `email` (or
`member_email` where the column was added later). The chat / voice /
agent paths NEVER read across members. Defense in depth:

- DB-level: every SELECT includes `WHERE email = %s`.
- App-level: the `globus_read_file` tool refuses files belonging to a
  different `email` even if the `file_id` is guessed.
- Audit: every send-message attempt audited in
  `globus_telegram_bot_sends` with the initiator + email.

If you fork Globus to add a new data source, the audit checklist is:
- Add the table with `email` (or `member_email`) as the lead column
- Every SELECT/UPDATE filters on it
- The corresponding LLM tool wrapper enforces it again at the app layer

## Config — DB first, env fallback

`db_helpers.cfg(key, default="")` reads in this order:
1. MySQL `config` table (preferred — DB-rotated, no deploy needed)
2. Process env (`.env`)
3. The default arg

Secrets that NEVER live in the repo (or .env in prod) live in the
`config` table:
- `GLOBUS_VOICE_LLM_SECRET`
- `ANTHROPIC_API_KEY` (when not using OAuth proxy)
- `DEEPSEEK_API_KEY`
- `EMAIL_API_KEY`
- OAuth client secrets
- `GLOBUS_OAUTH_ENCRYPTION_KEY` (Fernet, encrypts OAuth refresh tokens at rest)

## Refactor history (why the seams sit here)

The reference impl evolved from a single 11.5K-line `lead_server.py`
in the buildwithsumit repo. Across one stretch (June 2026, 23 commits
labeled #6d–#6z), it was carved into 23 modules. The key enablers:

- `db_helpers.py` (#6j) — extracted MySQL wrappers + `cfg()` → unlocked
  every subsequent module that needed DB access without dragging
  lead_server with it.
- `html_chrome.py` (refactor #6) — extracted shared HTML primitives →
  unlocked all the page-builder carves.
- `globus_chrome.py` (#6f) — Globus-specific CSS + shell wrapper →
  unlocked every globus page builder.
- `globus_agents_helpers.py` (#6m) + `globus_agents_catalog.py` (#6o)
  — the agents infrastructure → unlocked the agents UI carves.

The pattern that worked: each new module gained 1–2 signature params
so it stays pure-HTML or pure-DB-CRUD. The caller does the fetch and
passes results in. Example: `globus_chat_html(email, vault, messages,
daily_used, daily_cap, vault_stats)` — `vault_stats` is pre-computed by
the caller so `globus_chat_html` has no DB dependency at all.

## Why no Flask / Django / ORM

Three reasons:

1. **Self-host friction.** Globus is meant to be installable on
   any Linux box by a non-Python-shop developer. Standard library
   removes one whole class of "which Python framework do you know"
   questions.
2. **Latency.** Each chat turn already spends 1-3 seconds in an LLM
   call. The HTTP layer needs to add ~zero. `http.server` does that;
   Django middleware doesn't.
3. **Auditability.** ~3,000 lines of stdlib `http.server` is readable
   end-to-end by one engineer in an afternoon. ~30,000 lines of Django
   + Flask + Celery + SQLAlchemy is not.

Trade-off: no built-in CSRF (we hand-roll session cookies +
content-type guards), no migration framework (SQL files run by hand —
all CREATE TABLE IF NOT EXISTS so re-running is safe). If you want
those, fork and add them; we'd love to see it.

## Architecture decision records

Long-form rationale for the big calls lives in the buildwithsumit
reference repo under `docs/memory/architecture/`. The ones most
relevant to Globus:

- **001** — config table as source of truth (vs env-only)
- **003** — Globus two-agent split (text vs voice)
- **004** — no embeddings yet for Globus (why we use tool-use + full
  vault context instead of vector search)
- **005** — open-core via AGPL
- **007** — Globus plugin architecture (where this is headed)

This public file is the current source of truth until those longer ADRs are
published here.
