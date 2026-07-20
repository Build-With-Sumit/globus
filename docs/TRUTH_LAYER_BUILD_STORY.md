# Globus Truth Layer: What We Built

## Camera-ready write-up for the reel and YouTube video

Prepared July 20, 2026

Globus Truth Layer is the new evidence-backed reliability module built for
OpenAI Build Week using Codex with GPT-5.6. This document explains exactly what
was built, how it works, what it does not claim to do, and how to present it
accurately on camera.

> **The central idea:** An AI agent saying “done” is making a claim. Globus
> Truth Layer requires measurements, checks, timestamps, and evidence before
> that claim can receive a healthy status.

## The scope in one sentence

Globus already existed as a self-hosted private business AI platform with
cited chat, voice, connected data, and agents. For OpenAI Build Week, we used
Codex with GPT-5.6 to add a new self-contained component called the **Globus
Truth Layer**, wired it end to end into the public OSS agent runner, and
extended it in v0.13 with **Mission Control**, a source-backed capability
registry, and a fail-closed **Action Gate**. In v0.14 we added the
**Consequence Firewall**: exact runtime tool grants for four built-in
background agents plus a payload-free **Approval Center** whose human consent
remains subordinate to fresh Truth.

## Important naming

Call it the **Globus Truth Layer**, not the “Truth Model.”

It is deliberately not another language model and it is not a trained
machine-learning model. It is a deterministic verification engine. The AI
agent produces a claim and a structured receipt; ordinary Python rules evaluate
whether that claim is internally consistent, fresh, measured, and supported by
evidence.

## Why we built it

AI agents can produce fluent, confident status messages even when the actual
workflow did not succeed. Examples include:

- A pipeline saying “all clear” when it did not run.
- A successful-looking run that selected zero records because its source failed.
- An agent refusal being stored as if it were a completed artifact.
- A stale result continuing to look current.
- A failed check being hidden behind a positive summary.

The Truth Layer turns those operational failure patterns into a reusable
reliability contract.

## What exactly we built

### 1. A versioned run-receipt contract

Every receipt identifies the agent and run, and records:

- Schema version.
- Receipt, agent, and run identifiers.
- Declared status: success, no work, or failed.
- Start, finish, and heartbeat timestamps.
- Input counts: items seen and items eligible.
- Output counts: items processed and items changed.
- A human-readable summary.
- Evidence records.
- Agent-declared checks.
- An explicit no-work explanation when appropriate.
- An explicit error code and message for failures.
- Optional bounded metadata.

Supported evidence kinds are:

- Artifact.
- Database write.
- API acknowledgement.
- Checksum.
- Metric.
- Human acknowledgement.

### 2. A strict deterministic evaluator

The evaluator checks:

- The receipt uses schema version 1.0.
- Identifiers contain only safe characters and lengths.
- Timestamps are timezone-aware RFC 3339 values.
- Start, finish, heartbeat, and evidence times belong to the same run window.
- The result is not materially future-dated.
- Counts are ordinary non-negative integers, not booleans.
- The count invariant holds:

```text
items changed <= items processed <= items eligible <= items seen
```

- A declared success includes evidence and measured work.
- A no-work run has zero eligible, processed, and changed items.
- A no-work run includes a reason code and explanation.
- A failed run includes an explicit error code and message.
- Every declared agent check passed.
- The summary and evidence are not fluent refusal or error prose.
- The latest heartbeat is fresh; the default freshness limit is 24 hours.

Failure and contradiction take precedence over staleness. A broken run cannot
become merely “stale” and therefore look less serious.

### 3. Five explainable verdicts

**Healthy**

A fresh declared success has measured work, valid counts and timestamps,
evidence, and no failed checks.

**Verified no work**

The run inspected its source, proved that zero items qualified, recorded a
reason, and emitted a fresh heartbeat.

**Degraded or contradictory**

The receipt is readable, but its success or no-work claim conflicts with its
counts, timestamps, checks, evidence, or error-like prose.

**Failed**

The agent declared failure, omitted required failure details, or could not
satisfy the versioned receipt structure.

**Stale**

The run was otherwise valid, but its latest completion or heartbeat is older
than the configured freshness threshold.

Every result includes exact reason codes and a list of checks showing what
passed and what failed.

### 4. Immutable SQLite history

The module stores receipts and verdicts in a local SQLite database:

- Receipt JSON is canonicalized before storage.
- Receipts are immutable by ID.
- Retrying the exact same receipt is idempotent.
- Reusing an existing receipt ID with different content is rejected.
- Explicit ingests and verdict transitions are preserved in verdict history.
- Trusted stored receipts automatically age into stale verdicts when read after
  their freshness deadline.
- Polling records a new history row only when the verdict actually changes.
- Queries use parameterized SQL.
- The database uses WAL mode for file-backed operation.

### 5. A command-line interface

The complete safe demonstration starts with:

```bash
python -m globus_truth
```

That command creates a local database, loads five de-identified scenarios, and
starts the dashboard and API at:

```text
http://127.0.0.1:8765
```

Other commands evaluate a receipt without saving it, ingest and store a
receipt, list stored runs, load demo scenarios, or start the service without
loading fixtures.

### 6. A local JSON API

The API supports:

- Fleet summary totals.
- Paginated receipts with their latest verdict.
- Fetching one receipt.
- Ingesting a receipt.
- Previewing the safe scenarios.
- Loading the safe scenarios.
- Running the credential-free live artifact tamper challenge.

The local service:

- Binds only to loopback.
- Does not grant CORS access.
- Limits JSON bodies to 64 KiB.
- Rejects duplicate JSON keys and non-finite numbers.
- Validates the Host header for the local threat boundary.
- Sends restrictive browser security headers.
- Renders untrusted receipt text as text, never as dashboard HTML.

### 7. A responsive dashboard

The dashboard shows:

- Total trusted receipts.
- Runs requiring attention.
- Number of represented agents.
- Receipt history.
- Color-coded verdicts.
- Exact reason codes.
- Every evaluator check.
- Receipt measurements and evidence.
- An ingest form for testing a receipt.
- One-click loading of the five safe scenarios.
- A human Approval Center with an exact-scope changed/exact/replay proof.
- A 60-second Evidence Lab that writes, verifies, changes, and re-verifies
  actual local bytes.

#### The live one-byte challenge

Judge Mode is not another pre-written fixture. It creates a unique local
manifest with exclusive-create semantics and calls the same artifact read-back
primitive used by the production AgentRunner adapter.

The intact phase records the exact size and SHA-256 and persists a healthy
receipt. Judge Mode then appends exactly one controlled byte and performs a
second verification against the original measurements. That second immutable
receipt becomes contradictory because both the size and digest checks fail.
The two phase receipts commit in one SQLite transaction, so a persistence
failure cannot leave only half of the challenge in the dashboard.

The first receipt remains an honest point-in-time observation; the demo never
claims that historical receipts change automatically. It needs no LLM, MySQL,
credential, Docker runtime, or external call, and its API response contains
only generated IDs, a relative filename, measurements, hashes, and verdicts.

### 8. A real Globus agent-runner bridge

Every public OSS `AgentRunner` run that obtains a durable ledger ID follows the
complete verification path:

1. Globus records the start time and resolves the named built-in agent's exact
   non-empty tool grant.
2. The orchestrator advertises only those schemas, rechecks returned tool names
   before dispatch, and runs the real vault-aware task.
3. The runner writes the generated Markdown brief as exact bytes.
4. The adapter reopens the artifact and verifies its byte count and SHA-256.
5. It checks the actual model reply for empty, too-short, refusal-like, and
   error-like output without copying that private reply into the Truth database.
6. It emits a receipt identified by an install-keyed HMAC member pseudonym.
7. The deterministic evaluator returns and persists the verdict.
8. The existing Agents dashboard and chat activity console show that verdict
   separately from the process runner’s status.

If the model call throws, Globus emits an explicit failed receipt. If the
process completed but its output or artifact checks fail, the run becomes
contradictory. If receipt persistence itself fails, the runner fails closed
instead of showing an unverified green state.

### 9. Five safe demonstration scenarios

The fixtures cover:

- Healthy success.
- Verified no-work.
- Contradictory success.
- Explicit failure.
- Stale completion.

They are de-identified, freshly timestamped, and require no credentials.

### 10. Mission Control capability registry

The versioned registry gives operators an honest inventory of what the public
repository contains. Its 71 source-backed entries include:

- 4 built-in agents.
- 20 LLM-facing tools.
- 33 implemented/setup-required provider adapters: 9 lead-source, 8
  verification, 6 sender, and 10 CRM adapters.
- 14 additional connector, channel, and model-route entries.

Every entry declares its repository source, setup requirements, risk, approval
mode, and read-back behavior. Availability is explicit: `native`,
`implemented/setup_required`, `bridge/catalog`, or `planned`. Implemented does
not mean the operator has connected or configured the external account.

This is not a claim of OpenClaw parity. It is the start of an evidence-first
control plane that exposes the gap between code that exists and a capability
that is actually ready to use.

### 11. A fail-closed Action Gate

The Action Gate turns a persisted current Truth verdict into a bound,
immutable authorization decision. The caller supplies a receipt storage ID,
stable action ID, and policy—not a verdict.

The gate reads the receipt through the service so freshness is reevaluated,
applies one of two policies, and writes the audit record before it can return
an allowed decision. The insert transaction rechecks an allow against the
latest persisted verdict and freshness deadline, closing the read-to-audit
race:

- `healthy_only` accepts only healthy.
- `trusted_completion` accepts healthy or verified no-work.

Missing, malformed, unavailable, failed, contradictory, and stale evidence all
block. Verified no-work blocks under `healthy_only`. If the audit write or
latest-verdict recheck fails, authorization fails closed.

### 12. A verified business-outcome challenge

Mission Control includes a stronger, credential-free proof than a static
fixture:

1. Generate three de-identified follow-up rows in a separate local SQLite
   destination.
2. Reopen that destination through an independent connection, sort and
   canonicalize its rows, and measure count plus SHA-256.
3. Persist a 3 claimed → 3 observed receipt. It evaluates healthy.
4. The `healthy_only` gate audits an allow decision. The workflow reads that
   exact decision back from the audit table, then exactly one bounded local
   outbox insert executes.
5. Delete exactly one generated destination row.
6. Reopen and measure again. The 3 claimed → 2 observed receipt evaluates
   contradictory.
7. The gate audits a block decision, the second action callback is not
   invoked, and the outbox remains at exactly one row.

The challenge uses no LLM, MySQL, external database, API key, provider account,
Docker runtime, or network call. It returns safe generated IDs, a relative
path, measurements, hashes, verdicts, decisions, and action counts—not row
payloads or absolute paths.

### 13. The v0.14 Consequence Firewall and Approval Center

The Consequence Firewall adds two enforceable boundaries between an agent's
plan and a consequential effect.

First, the four shipped background agents have exact, deny-by-default runtime
grants:

- Research: `search_files`, `read_file`, `search_content`,
  `list_recent_emails`, `search_whatsapp`, and `search_telegram`.
- Sales Desk: `search_files`, `read_file`, `search_content`, and
  `list_recent_emails`.
- Narada: `narada_list_campaigns`, `narada_campaign_stats`, and
  `narada_check_replies`.
- Infra Watch: `search_files`, `read_file`, `search_content`, and
  `list_recent_emails`.

The model sees only the granted tool schemas, and the orchestrator checks the
tool name again before dispatch. A forged disallowed call therefore cannot
reach the tool implementation. These grants cover those four built-in
background agents; they do not mean all 71 registry entries are governed,
connected, or live.

Second, the Approval Center stores an immutable exact proposal and human
decision bound to a payload SHA-256. It stores identifiers, hashes, policy
metadata, and timestamps—not the raw action payload. Human consent is
necessary but not sufficient: immediately before a bounded callback, the
center obtains and reads back a fresh Action Gate decision, atomically
rechecks current Truth, and creates a unique local execution claim. Failed,
contradictory, stale, or otherwise policy-ineligible Truth still blocks.

The credential-free generated-local proof pauses with zero actions before
review. After approval, a changed payload is blocked, the exact approved
payload executes once behind fresh Truth, and an exact replay is blocked.
Independent read-back ends with one local outbox row and zero external calls.
Rejection executes nothing.

The guarantee is deliberately narrow. The unique claim provides at-most-once
callback invocation inside the local SQLite coordinator, not external
exactly-once delivery. Real providers still need idempotency keys,
acknowledgement read-back, and reconciliation. The generic API and CLI create,
inspect, approve, and reject proposals but do not dispatch arbitrary
callbacks. `requested_by` and `decided_by` are local audit labels rather than
cryptographic identities; operating-system/process access to localhost is the
trust boundary.

### 14. Automated tests

The v0.14 hermetic Truth/Mission Control suite covers:

- Receipt evaluation and verdict precedence.
- HTTP, dashboard, and strict JSON behavior.
- Immutable storage and automatic aging.
- Command-line interfaces.
- Real AgentRunner adapter and member isolation.
- Real-byte Evidence Lab verification.
- Action Gate policy, binding, audit, idempotency, and failure behavior.
- Exact background-agent grants, schema filtering, and dispatch rechecks.
- Approval proposal/decision immutability, privacy, concurrency, fresh-Truth
  enforcement, and local at-most-once claims.
- The changed/exact/replay approval proof and loopback-only HTTP boundary.
- Capability-registry schema, count, source, and secret-safety checks.
- Verified-outcome success, contradiction, non-invocation, confinement, and
  concurrent-run behavior.

The suite exercises malformed receipts, impossible counts, future timestamps,
stale heartbeats, missing evidence, failed checks, refusal text, strict JSON,
security headers, SQL metacharacters, immutable IDs, idempotent retries,
pagination, automatic aging, artifact read-back and hashes, tenant-isolated
status, atomic concurrent transitions, persistence failures, the complete
five-verdict demo, a real one-byte mutation, and a destination mismatch that
must prevent a second bounded action.

The repository-level command also runs the wider Globus behavioural checks,
visible-verdict rendering tests, public-asset smoke tests, and Python
compilation in isolated processes.

## What Codex with GPT-5.6 contributed

The human contribution was the operational problem, the real failure patterns,
the product boundary, and the requirement that the result remain honest and
inspectable.

Codex with GPT-5.6 helped translate those lessons into:

- The versioned receipt schema.
- Deterministic invariants and verdict precedence.
- Explicit reason codes.
- The Python evaluator.
- SQLite storage and audit behavior.
- Automatic persisted-receipt aging.
- The service and command-line interface.
- The JSON API.
- The Mission Control dashboard.
- The versioned, source-backed capability registry.
- The immutable, fail-closed Action Gate.
- The exact runtime grants for four built-in background agents.
- The payload-free Approval Center and changed/exact/replay proof.
- The credential-free verified business-outcome challenge.
- The credential-free real-byte Evidence Lab.
- The real OSS AgentRunner adapter and visible verdict integration.
- Safe fixtures.
- Adversarial tests.
- A single hermetic repository test command and CI workflow.
- Documentation and the judge quick-start path.

The broader Globus platform and its Claude-native runtime predate Build Week.
They should not be represented as having been built with Codex or GPT-5.6.

<!-- pagebreak -->

## Reel script — approximately 60 seconds

> AI agents have a dangerous habit: they can confidently say “done” even when
> nothing actually happened.
>
> Globus already gives businesses private AI chat, voice, connected data, and
> agent workflows. For OpenAI Build Week, I used Codex with GPT-5.6 to build
> the Globus Truth Layer.
>
> It does not trust an agent’s final sentence. It asks for a structured
> receipt: timestamps, measured counts, checks, and evidence.
>
> Then a deterministic evaluator checks whether the numbers make sense,
> whether the evidence belongs to that run, whether any check failed, and
> whether the result is still fresh.
>
> The outcome is one of five explainable verdicts: healthy, verified no-work,
> contradictory, failed, or stale.
>
> In v0.14, the Consequence Firewall gives each built-in background agent an
> exact tool grant. Its fastest proof pauses one generated high-risk action for
> human review. After approval, a changed payload stays blocked, the exact
> payload executes once behind a fresh Truth check, and a replay stays blocked.
> No LLM, API key, or external call.
>
> Once the public Globus runner has a durable run ID, it writes the brief, reads
> it back, verifies its SHA-256, and stores the receipt before the ledger can
> become green. Identity or receipt-persistence failures fail closed.
>
> It is not another AI judging an AI. It is a deterministic reliability
> contract for AI agents.

### Reel screen cues

1. Open on camera: “An agent saying done is making a claim.”
2. Click **Stage generated approval request**.
3. Show the exact scope paused with zero actions before approval.
4. Approve it, then show changed blocked, exact executed once, and replay
   blocked.
5. Briefly show one real public Globus agent and its Truth badge.
6. Show the 71-capability registry disclosure: implemented is not connected.
7. Show the complete hermetic test command passing in the terminal.
8. Close on camera with the final reliability-contract line.

<!-- pagebreak -->

## YouTube video script

### Opening

> Globus is a self-hosted private business AI platform. It brings together
> cited chat, voice, connected business data, and specialized agents.
>
> The work I built specifically for OpenAI Build Week is the Globus Truth
> Layer, v0.13 Mission Control and Action Gate, and the v0.14 Consequence
> Firewall and Approval Center, using Codex with GPT-5.6.
>
> The problem is simple: an agent saying “done” is only making a claim. A
> polished answer does not prove that a database was updated, a file was
> created, an API accepted a request, or that the agent even found work to
> process.

**Screen cue:** Show Globus briefly, then move immediately to the Truth Layer
dashboard.

### The receipt contract

> The Truth Layer changes that by requiring every participating agent run to
> produce a versioned receipt.
>
> That receipt identifies the agent and run, records its start, finish, and
> heartbeat timestamps, and includes measured counts: how many items it saw,
> how many were eligible, how many it processed, and how many it changed.
>
> It can also include evidence such as an artifact, database write, API
> acknowledgement, checksum, metric, or human acknowledgement.
>
> If there was genuinely no work, the agent must say why. If it failed, it must
> provide an explicit error code and message.

**Screen cue:** Scroll slowly through a healthy receipt and highlight the
timestamps, counts, checks, and evidence.

### Deterministic evaluation

> The evaluator is deterministic. It does not call another language model to
> decide whether the first model was truthful.
>
> It verifies that timestamps are properly formatted and logically ordered. It
> enforces the rule that changed items cannot exceed processed items, processed
> items cannot exceed eligible items, and eligible items cannot exceed the
> number seen.
>
> A declared success must include evidence and measured work. A no-work result
> must show zero eligible, processed, and changed items and provide an explicit
> reason.
>
> A failed declared check cannot be hidden behind a positive summary. The
> evaluator also detects common refusal or error language being presented as
> completed output.
>
> Finally, it checks freshness. By default, an otherwise valid receipt becomes
> stale when its latest run signal exceeds 24 hours.

**Screen cue:** Open a contradictory receipt. Show the impossible count or
failed check and the evaluator’s reason code.

### The five outcomes

> The result is one of five verdicts.
>
> Healthy means the success claim is fresh, measured, internally consistent,
> and supported by evidence.
>
> Verified no-work means the agent really checked and proved that nothing
> qualified.
>
> Contradictory means the receipt is readable, but its claim conflicts with its
> counts, timestamps, checks, or evidence.
>
> Failed means the run declared failure or could not satisfy the receipt
> contract.
>
> Stale means an otherwise valid result is too old to remain trustworthy.
>
> Each verdict includes exact reason codes and every check that passed or
> failed.

**Screen cue:** Show all five safe scenario rows together.

### Mission Control and the capability map

> Mission Control adds a source-backed map of the platform we actually ship:
> four built-in agents, twenty LLM-facing tools, and thirty-three implemented
> provider adapters, plus connector, channel, and model-route entries.
>
> The full registry contains seventy-one capabilities. But the status labels
> matter more than the number. Native, implemented but setup-required, bridge
> or catalog, and planned are separate states. Implemented does not mean an
> external account is connected.

**Screen cue:** Show the Mission Control counts, then the disclosure beside
the capability map.

### The Action Gate and verified outcome

> A receipt is useful for monitoring, but a control plane should also stop a
> bad action.
>
> So the Action Gate reads a persisted current verdict itself. The caller
> cannot supply a preferred verdict. Under the healthy-only policy, only a
> healthy receipt can authorize the bound action. Missing, malformed, failed,
> contradictory, or stale evidence blocks. The decision is written to an
> immutable audit table before an allow can return, and the same transaction
> rechecks that its verdict is still current.
>
> The credential-free workflow creates three generated follow-up rows in a
> separate local destination and reopens that database through an independent
> connection. Three claimed and three observed produces a healthy receipt, so
> the workflow reads back the exact gate audit and exactly one local outbox
> action executes.
>
> Then the challenge removes one generated row and reads the destination
> again. Three claimed and two observed is contradictory. The gate blocks, the
> second callback is never invoked, and the outbox remains at one action.

**Screen cue:** Click **Run verified business workflow**. Hold first on 3 → 3
healthy/allowed/executed, then on 3 → 2 contradictory/blocked/prevented. Open
one gate decision and point out the receipt, action, policy, verdict, and
reason-code binding.

### The Consequence Firewall and exact human approval

> v0.14 narrows what an agent can even attempt. Research, Sales Desk, Narada,
> and Infra Watch each receive an exact tool allowlist. The model sees only
> those schemas, and the orchestrator checks every returned tool name again
> before dispatch.
>
> For a high-risk action, the Approval Center pauses an exact payload
> fingerprint for a person to approve or reject. Approval cannot override
> Truth: execution still requires a fresh gate decision and a unique local
> claim.
>
> The generated proof makes this visible. Before the click, nothing has
> executed. After approval, a changed payload is blocked, the exact payload
> executes once, and a replay is blocked. The local outbox ends with one row
> and there are zero external calls.

**Screen cue:** Click **Stage generated approval request**, show the pending
exact scope, approve it, and hold on the three changed/exact/replay result
cards.

### The 60-second Evidence Lab

> A judge should not need our LLM key or MySQL configuration to see the core
> verification path work.
>
> So Judge Mode performs a real local experiment. It writes a generated
> manifest, reopens the exact bytes, and records a healthy receipt with its
> size and SHA-256.
>
> It then appends exactly one byte and performs a new verification against the
> original measurements. The size and hash checks fail, so the second receipt
> becomes contradictory.
>
> This reuses the production AgentRunner's artifact read-back primitive. It
> makes zero external calls and stores no member data.

**Screen cue:** Click the Evidence Lab button. Show the four steps, both hash
values, and then open the healthy and contradictory receipts.

### Storage, API, and dashboard

> We store receipts in SQLite as immutable records. An exact retry is
> idempotent, while reusing a receipt ID with different content creates a
> conflict instead of silently rewriting history.
>
> Explicit ingests and verdict transitions are kept in verdict history;
> unchanged polling does not manufacture duplicate audit events.
>
> We also built a local JSON API and a responsive dashboard where an operator
> can inspect fleet totals, open a receipt, and understand exactly why the
> system accepted or rejected it. Mission Control also exposes the validated
> capability inventory, the verified-outcome challenge, and immutable gate
> decisions through the same loopback-only service. The Approval Center API
> can create, inspect, approve, and reject fixed-envelope proposals; it does
> not execute an arbitrary callback.

**Screen cue:** Show the dashboard detail panel, then briefly show an API JSON
response.

### The live Globus integration

> The submitted public runner uses this contract after durable run creation.
>
> Before a built-in background-agent model call, the runner resolves that
> agent's exact non-empty tool grant. Only those schemas are advertised, and a
> second check happens before dispatch.
>
> After the orchestrator returns, the runner writes the Markdown brief as exact
> bytes. The adapter reopens the file, checks its size and SHA-256, and scans
> the actual reply for empty or refusal-like output without copying the private
> reply into the Truth database.
>
> It then stores an install-scoped member receipt and returns a compact verdict to the
> existing Agents dashboard and chat activity console.
>
> A process can therefore complete while its output is still marked
> contradictory. The MySQL runner status and the Truth Layer verdict are
> deliberately separate. If the Truth receipt cannot be stored, the run fails
> closed rather than appearing green.

**Screen cue:** Run the public research agent, then show the new badge in both
the Agents dashboard and chat activity console. Open the matching receipt and
show the artifact hash check.

### How Codex and GPT-5.6 were used

> I brought the operational problem, the real failure patterns, and the product
> constraints.
>
> Codex with GPT-5.6 helped turn those lessons into a concrete schema,
> deterministic invariants, reason codes, storage behavior, API routes, a
> dashboard, fixtures, and an automated test suite.
>
> We repeatedly tested adversarial cases: invalid timestamps, impossible
> counts, missing evidence, duplicate JSON keys, future-dated runs, stale
> heartbeats, changed retries, and fluent refusal text masquerading as
> successful output.
>
> The final suite covers the evaluator, storage, automatic aging,
> command-line interface, HTTP surface, real runner adapter, member isolation,
> visible verdict rendering, exact runtime permissions, immutable approvals,
> fresh-Truth execution, and replay blocking.

**Screen cue:** Show the relevant Codex session, then run the test command and
end on the complete passing summary.

### Honest scope

> The broader Globus platform existed before Build Week and remains
> Claude-native at runtime.
>
> The new Build Week work is the Truth Layer, Mission Control, Action Gate,
> Consequence Firewall, Approval Center, and the public OSS runner integration.
> In the included runner, an `ok` ledger state requires a trusted persisted
> receipt; failures before receipt creation remain explicitly non-green. The
> exact runtime grants currently govern four built-in background agents, not
> every registry capability or private production workflow.
>
> It also is not a universal oracle that independently proves every external
> event. It verifies the receipt's structure, measurements, timing, declared
> checks, and supplied evidence metadata. The local outcome challenge does
> independently read back its controlled destination, but that does not imply
> every provider adapter is connected or every external service has the same
> proof today. The local unique execution claim is at-most-once coordination,
> not external exactly-once delivery; real providers still need their own
> idempotency and reconciliation.

**Screen cue:** Return to camera for this section. The scope disclosure is more
credible when spoken directly.

### Closing

> The idea is straightforward: AI agents should not receive a green status
> because they wrote a confident paragraph.
>
> They should earn that status with measurements, evidence, and checks.
>
> That is what Globus Mission Control adds: an inspectable reliability contract
> between an agent's claim and the operator who has to trust it, plus a gate
> that can stop the next action when the evidence does not match. The v0.14
> Consequence Firewall also limits the tools a background agent can attempt and
> keeps human approval subordinate to fresh Truth.

## Recommended visual shot list

1. Globus product overview.
2. Existing Agents dashboard.
3. Mission Control capability counts and setup-state disclosure.
4. Approval Center paused with the exact scope and zero actions.
5. Consequence Firewall changed/exact/replay proof.
6. Verified workflow 3 → 3 allowed and 3 → 2 blocked.
7. One immutable Action Gate decision.
8. Evidence Lab one-byte result and before/after hashes.
9. Healthy and verified-no-work receipts.
10. Contradictory, failure, and stale reason codes.
11. SQLite receipt records and latest verdicts in the dashboard.
12. JSON API response.
13. Codex/GPT-5.6 build session.
14. Terminal running the complete hermetic test command.
15. Public repository and one-command quick start.

## Accuracy guide

### Safe statements

- “I used Codex with GPT-5.6 to build the Globus Truth Layer.”
- “The broader Globus platform existed before Build Week.”
- “The evaluator is deterministic; it does not ask another AI for the verdict.”
- “A successful run requires measured work and evidence.”
- “The module has five explainable verdicts.”
- “The safe demo has no third-party packages, credentials, or outbound calls.”
- “The full hermetic test command passes.”
- “Judge Mode appends exactly one byte and catches it on a new verification.”
- “The credential-free Evidence Lab uses the production artifact read-back primitive.”
- “The outcome challenge independently reads a controlled local destination.”
- “The healthy 3 → 3 receipt permits one bounded local action.”
- “The contradictory 3 → 2 receipt blocks, so the second callback is not invoked.”
- “Four built-in background agents have exact deny-by-default runtime tool
  grants, with schema filtering and a dispatch recheck.”
- “Human approval is bound to a payload hash and cannot override fresh Truth.”
- “The generated approval proof blocks a changed payload, executes the exact
  payload once locally, and blocks replay.”
- “The Approval Center stores hashes and audit metadata, not action payloads.”
- “The registry describes 71 source-backed capabilities; setup-required does
  not mean connected.”
- “The public OSS AgentRunner cannot mark a run `ok` without a trusted,
  persisted Truth receipt.”
- “The existing Globus agent UI shows runner status and Truth verdict
  separately.”

### Statements to avoid

- “GPT-5.6 powers all of Globus.”
- “Codex built the entire Globus platform.”
- “The Truth Layer proves every external event happened.”
- “It independently verifies every evidence reference.”
- “Every private production Globus workflow already emits receipts.”
- “It eliminates hallucinations.”
- “It is a trained truth model.”
- “Globus has OpenClaw parity.”
- “All 33 provider adapters are connected and ready on this install.”
- “All 71 registry capabilities are governed by runtime grants.”
- “Every consequential Globus action is already controlled by Action Gate.”
- “Human approval guarantees an external action happens exactly once.”
- “The `requested_by` and `decided_by` labels cryptographically authenticate a
  person.”

## Demonstration commands

Start the complete safe demo:

```bash
python -m globus_truth
```

Run the tests:

```bash
python scripts/test_all.py
```

Evaluate one receipt without storing it:

```bash
python -m globus_truth evaluate receipt.json
```

Ingest and store one receipt:

```bash
python -m globus_truth ingest --db globus-truth.db receipt.json
```

Run the business-outcome challenge without the browser:

```bash
python -m globus_truth outcome-challenge --db globus-truth.db
```

Stage the generated-local Approval Center proof, then resolve it only after
reviewing the returned proposal:

```bash
python -m globus_truth approval-challenge --db globus-truth.db
python -m globus_truth approval-challenge --db globus-truth.db \
  --proposal-id PROPOSAL_ID --decision approved
```

Audit one decision against a persisted receipt:

```bash
python -m globus_truth gate --db globus-truth.db STORAGE_ID \
  --action-id review-follow-ups --policy healthy_only
```

## Public links

- Globus: https://buildwithsumit.com/globus
- Source repository: https://github.com/Build-With-Sumit/globus
- Truth Layer source: https://github.com/Build-With-Sumit/globus/tree/main/globus_truth
- OpenAI Build Week demo: https://youtu.be/d2UQVgz_dm0

## Final on-camera reminder

The most credible story is not “AI can determine truth.” The credible story is:

> “An AI agent’s narrative is not proof. We built a deterministic contract that
> makes the agent expose its measurements, evidence, and checks so an operator
> can see exactly why a run is—or is not—trusted.”
