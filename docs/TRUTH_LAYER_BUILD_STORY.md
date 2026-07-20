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
Truth Layer**, then wired it end to end into the public OSS agent runner.

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

The local service:

- Binds to loopback by default.
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

### 8. A real Globus agent-runner bridge

Every public OSS `AgentRunner` run that obtains a durable ledger ID follows the
complete verification path:

1. Globus records the start time and calls the real vault-aware orchestrator.
2. The runner writes the generated Markdown brief as exact bytes.
3. The adapter reopens the artifact and verifies its byte count and SHA-256.
4. It checks the actual model reply for empty, too-short, refusal-like, and
   error-like output without copying that private reply into the Truth database.
5. It emits a receipt identified by an install-keyed HMAC member pseudonym.
6. The deterministic evaluator returns and persists the verdict.
7. The existing Agents dashboard and chat activity console show that verdict
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

### 10. Automated tests

The Truth Layer suite has 55 passing tests:

- 18 evaluator tests.
- 10 HTTP and dashboard tests.
- 14 storage and automatic-aging tests.
- 2 command-line tests.
- 11 real AgentRunner adapter and isolation tests.

They test malformed receipts, impossible counts, future timestamps, stale
heartbeats, missing evidence, failed checks, refusal text, strict JSON,
security headers, SQL metacharacters, immutable receipt IDs, idempotent retries,
pagination, automatic persisted-receipt aging, artifact read-back and hashes,
tenant-isolated status lookup, atomic concurrent stale transitions, large-fleet
summaries, failed ledger commits, non-overwriting evidence files, persistence
failures, and the complete five-verdict demo.

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
- The responsive dashboard.
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
> Once the public Globus runner has a durable run ID, it writes the brief, reads
> it back, verifies its SHA-256, and stores the receipt before the ledger can
> become green. Identity or receipt-persistence failures fail closed.
>
> It is not another AI judging an AI. It is a deterministic reliability
> contract for AI agents.

### Reel screen cues

1. Open on camera: “An agent saying done is making a claim.”
2. Run one real public Globus agent.
3. Show its brief, SHA-256 evidence, and Truth Layer badge.
4. Cut to one receipt in JSON.
5. Show the five verdict badges.
6. Open one contradictory run and show its failed checks.
7. Show the complete test suite passing in the terminal.
8. Close on camera with the final reliability-contract line.

<!-- pagebreak -->

## YouTube video script

### Opening

> Globus is a self-hosted private business AI platform. It brings together
> cited chat, voice, connected business data, and specialized agents.
>
> But the part I built specifically for OpenAI Build Week is the Globus Truth
> Layer, using Codex with GPT-5.6.
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
> system accepted or rejected it.

**Screen cue:** Show the dashboard detail panel, then briefly show an API JSON
response.

### The live Globus integration

> The submitted public runner uses this contract after durable run creation.
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
> and visible verdict rendering.

**Screen cue:** Show the relevant Codex session, then run the test command and
end on the complete passing summary.

### Honest scope

> The broader Globus platform existed before Build Week and remains
> Claude-native at runtime.
>
> The new Build Week work is the Truth Layer and its public OSS runner
> integration. In the included runner, an `ok` ledger state requires a trusted
> persisted receipt; failures before receipt creation remain explicitly
> non-green. I am not claiming that every private production workflow already
> uses the same adapter.
>
> It also is not a universal oracle that independently proves every external
> event. It verifies the receipt’s structure, measurements, timing, declared
> checks, and supplied evidence metadata. Stronger destination verification is
> the next step.

**Screen cue:** Return to camera for this section. The scope disclosure is more
credible when spoken directly.

### Closing

> The idea is straightforward: AI agents should not receive a green status
> because they wrote a confident paragraph.
>
> They should earn that status with measurements, evidence, and checks.
>
> That is what the Globus Truth Layer adds: a small, inspectable reliability
> contract between an agent’s claim and the operator who has to trust it.

## Recommended visual shot list

1. Globus product overview.
2. Existing Agents dashboard.
3. Truth Layer dashboard overview.
4. Healthy receipt.
5. Verified no-work receipt and its reason.
6. Contradictory receipt with failed count or evidence checks.
7. Failure receipt with explicit error detail.
8. Stale receipt and freshness check.
9. SQLite receipt records and latest verdicts, shown through the dashboard.
10. JSON API response.
11. Codex/GPT-5.6 build session.
12. Terminal running the complete test suite.
13. Public repository and one-command quick start.

## Accuracy guide

### Safe statements

- “I used Codex with GPT-5.6 to build the Globus Truth Layer.”
- “The broader Globus platform existed before Build Week.”
- “The evaluator is deterministic; it does not ask another AI for the verdict.”
- “A successful run requires measured work and evidence.”
- “The module has five explainable verdicts.”
- “The safe demo has no third-party packages, credentials, or outbound calls.”
- “The full hermetic test command passes.”
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
