# Globus Mission Control and Truth Layer

Globus Truth Layer is a zero-dependency, local reliability layer for agent fleets.
Agents submit versioned run receipts; a deterministic evaluator checks the claim
against timestamps, measured counts, evidence, heartbeats, and explicit checks
before the run is allowed to look healthy.

v0.14 adds a **Consequence Firewall** around the v0.13 Mission Control core.
Four built-in background agents receive exact, deny-by-default runtime tool
grants, and high-risk actions can pause in a payload-free Approval Center.
Human approval remains subordinate to a fresh Truth verdict immediately before
a unique local execution claim.

It exists because fluent output is not the same thing as successful work. The
pre-existing Globus fleet has seen quiet pipelines report all-clear, declared
success select zero records, and an LLM refusal get persisted as if it were a
finished artifact. Truth Layer turns those incident lessons into one reusable
contract.

## Quick start

Requirements: Python 3.10 or later. There are no third-party packages and no build
step.

From the repository root:

```bash
python -m globus_truth
```

Open <http://127.0.0.1:8765>. The command creates `globus-truth.db`, appends five
de-identified scenarios with timestamped receipt IDs, and starts the dashboard/API.
It never deletes an existing receipt. Stop it with `Ctrl+C`. The explicit
`python -m globus_truth demo` command is equivalent.

Use a different database or port:

```bash
python -m globus_truth demo --db ./local-truth.db --port 9000
```

The other CLI commands are:

```bash
python -m globus_truth evaluate receipt.json
python -m globus_truth ingest --db globus-truth.db receipt.json
python -m globus_truth list --db globus-truth.db --limit 50
python -m globus_truth load-demo --db globus-truth.db
python -m globus_truth serve --db globus-truth.db
python -m globus_truth outcome-challenge --db globus-truth.db
python -m globus_truth gate --db globus-truth.db STORAGE_ID \
  --action-id review-follow-ups --policy healthy_only
python -m globus_truth approval-propose --db globus-truth.db STORAGE_ID \
  --proposal-id proposal-001 --action-id review-followup-001 \
  --action-kind queue_follow_up_review \
  --payload-sha256 0000000000000000000000000000000000000000000000000000000000000000 \
  --requested-by operator:local --expires-at 2030-01-01T00:00:00Z
python -m globus_truth approval-decide --db globus-truth.db proposal-001 \
  --outcome approved --decided-by operator:local \
  --reason-code scope_reviewed
python -m globus_truth approval-list --db globus-truth.db
python -m globus_truth approval-challenge --db globus-truth.db
```

Use `-` instead of a filename to read a receipt from standard input.

The `gate` command exits `0` when authorized, `1` when blocked, and `2` for
invalid input or an audit failure. `healthy_only` accepts only `healthy`;
`trusted_completion` accepts `healthy` or `verified_no_work`.
`outcome-challenge` prints the complete safe challenge report as JSON. Use
`--artifact-root PATH` to choose where its generated per-run destination
directories are written. It exits `0` only when `expectations_met` is exactly
true; an incomplete proof exits `1`.

`approval-challenge` without a proposal ID stages the credential-free proof and
prints the pending proposal. Resolve it only after reviewing that output:

```bash
python -m globus_truth approval-challenge --db globus-truth.db \
  --proposal-id PROPOSAL_ID --decision approved
```

The generic approval commands create, inspect, approve, and reject exact
proposals. They do not expose a generic execution command or dispatch an
arbitrary callback.

## Consequence Firewall and Approval Center

The Consequence Firewall has two independent boundaries.

First, each of the four shipped background agents receives an explicit tool
allowlist:

| Built-in agent | Runtime tools |
|---|---|
| Research | `search_files`, `read_file`, `search_content`, `list_recent_emails`, `search_whatsapp`, `search_telegram` |
| Sales Desk | `search_files`, `read_file`, `search_content`, `list_recent_emails` |
| Narada | `narada_list_campaigns`, `narada_campaign_stats`, `narada_check_replies` |
| Infra Watch | `search_files`, `read_file`, `search_content`, `list_recent_emails` |

The orchestrator advertises only the granted schemas to the model and checks the
tool name again before dispatcher invocation. Missing or empty grants fail
closed for a named background agent, and a forged disallowed tool call returns
`tool_not_allowed` without reaching the tool implementation. These runtime
grants govern the four built-in background agents only; they do not make every
entry in the 71-capability registry governed or live.

Second, `ApprovalCenter` binds a consequential proposal to its Truth receipt,
action, policy, action kind, requester, expiry, and payload SHA-256. The
proposal and human decision are immutable. The database stores those IDs,
hashes, metadata, and timestamps—not the raw action payload.

An approval is necessary but not sufficient. On execution, the center:

1. Rejects a changed payload hash.
2. Rejects expired, missing, or rejected human consent.
3. Asks the Action Gate for a fresh persisted Truth decision and reads that
   exact decision back.
4. Atomically rechecks the current Truth state and creates a unique execution
   claim.
5. Lets only the creator of that claim invoke the bounded callback.

A person cannot override failed, contradictory, stale, or otherwise
policy-ineligible Truth. Replays do not invoke the callback again. This is
at-most-once execution inside the local SQLite coordinator; it is not an
external exactly-once guarantee. Real provider integrations still need
provider-side idempotency keys, acknowledgement read-back, and reconciliation.

The dashboard's **Stage generated approval request** proof uses only generated
local data. Before review, the local outbox contains zero actions. On approval,
the proof tries a changed payload (blocked), the exact payload after a fresh
Truth check (executed once), and an exact replay (blocked). Independent read-back
ends with one local outbox row. Rejecting the proposal executes nothing. The
proof needs no LLM, MySQL, credential, provider account, Docker runtime, or
external call.

## Verified business-outcome challenge

Mission Control's **Run verified business workflow** button demonstrates the
full evidence-to-action boundary using generated, de-identified local data:

1. Create three follow-up rows in a separate per-run SQLite destination.
2. Reopen that destination through an independent connection, sort and
   canonicalize its rows, and measure the count and SHA-256.
3. Persist a receipt for the 3 claimed → 3 observed result. It is `healthy`.
4. Ask the Action Gate to apply `healthy_only`. The gate reads the persisted
   verdict itself, audits an allow decision, and only then permits one bounded
   local outbox insert.
5. Delete exactly one generated destination row.
6. Reopen and measure the destination again. The 3 claimed → 2 observed receipt
   is `degraded_contradictory`.
7. The gate audits a block decision and the second action callback is never
   invoked. The outbox still contains exactly one row.

The challenge uses no LLM, MySQL, external database, API key, provider account,
Docker runtime, or network call. Its response contains generated IDs, a
relative destination path, counts, hashes, verdicts, gate decisions, and action
counts—not destination row payloads or absolute paths. The two receipts are
honest sequential observations because the controlled action and row deletion
occur between them.

## 60-second Evidence Lab

The dashboard's **Run live tamper challenge** button performs a controlled
experiment against real local bytes:

1. Write a small generated manifest with exclusive-create semantics.
2. Reopen it with the same read-back primitive used by the OSS AgentRunner.
3. Persist a `healthy` point-in-time receipt with the measured size and SHA-256.
4. Append exactly one byte.
5. Reopen the file against the original expected measurements.
6. Persist a new `degraded_contradictory` receipt whose failed size and hash
   checks explain exactly what changed.

This is a new re-verification, not a claim that the first immutable receipt
changes after the fact. The generated artifact and receipts contain no member
data. The response exposes only safe generated IDs, a relative filename,
measurements, hashes, and verdicts. It needs no MySQL server, LLM, API key,
Docker runtime, or external network call. Both phase receipts are committed in
one SQLite transaction; if either persistence fails, neither phase appears.

## Run the tests

The hermetic Truth/Mission Control suite includes adversarial authorization,
concurrency, privacy, HTTP, CLI, and real-runner coverage.

```bash
python -m compileall -q globus_truth
python -m unittest discover -s globus_truth/tests -v
```

The suite uses only temporary files and loopback HTTP. It does not contact an
external network or service.

From the repository root, one command runs the Truth suite plus every broader
Globus behavioural check in isolated interpreters:

```bash
python scripts/test_all.py
```

## Receipt format

The current contract is schema version `1.0`. The machine-readable JSON Schema is
[`receipt-schema-v1.json`](receipt-schema-v1.json); the evaluator in
[`evaluator.py`](evaluator.py) is authoritative because it also enforces semantic
and cross-field invariants.

```json
{
  "schema_version": "1.0",
  "receipt_id": "indexer-20260719-001",
  "agent_id": "document-indexer",
  "run_id": "20260719T120000Z",
  "declared_status": "success",
  "started_at": "2026-07-19T12:00:00Z",
  "finished_at": "2026-07-19T12:02:00Z",
  "heartbeat_at": "2026-07-19T12:02:00Z",
  "input": {
    "items_seen": 12,
    "items_eligible": 4
  },
  "output": {
    "items_processed": 4,
    "items_changed": 4
  },
  "summary": "Indexed four eligible records and verified the manifest.",
  "evidence": [
    {
      "kind": "checksum",
      "ref": "artifact:index-manifest-v1",
      "observed_at": "2026-07-19T12:02:00Z",
      "detail": "Manifest contains four records.",
      "sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    }
  ],
  "checks": [
    {
      "name": "manifest_count",
      "passed": true,
      "detail": "Manifest count equals items_changed."
    }
  ],
  "metadata": {
    "environment": "local"
  }
}
```

Identifiers accept letters, numbers, `.`, `_`, `:`, and `-`, up to 128 characters.
Timestamps must be timezone-aware RFC 3339 values. Counts must be ordinary
non-negative integers, not booleans, with this invariant:

```text
items_changed <= items_processed <= items_eligible <= items_seen
```

Supported evidence kinds are `artifact`, `database_write`, `api_ack`, `checksum`,
`metric`, and `human_ack`. Evidence timestamps must belong to the run window.
Unknown top-level or nested contract fields are rejected by the evaluator so schema
drift cannot pass silently.

A no-work receipt uses `declared_status: "no_work"`, zero eligible/processed/changed
counts, a fresh heartbeat, and:

```json
{
  "no_work": {
    "reason_code": "no_eligible_records",
    "reason": "Eight records were inspected; none met the deterministic age rule."
  }
}
```

A failed receipt uses `declared_status: "failed"` and:

```json
{
  "error": {
    "code": "destination_timeout",
    "message": "The destination did not acknowledge the write."
  }
}
```

## Verdicts

| Verdict | Meaning |
|---|---|
| `healthy` | A fresh declared success has measured work, evidence, valid timestamps/counts, and no failed checks. |
| `verified_no_work` | A fresh quiet run proves it inspected input, records zero eligible work, supplies a reason, and emitted a heartbeat. |
| `degraded_contradictory` | The receipt is structurally readable but its claim conflicts with evidence, counts, timestamps, checks, or refusal/error prose. |
| `failed` | The agent declared failure, or the receipt cannot satisfy the versioned structure. |
| `stale` | The latest otherwise-valid completion/heartbeat is older than the configured threshold (24 hours by default). |

Failure and contradiction take precedence over staleness. A declared success without
evidence can never be `healthy`. Common fluent refusal and error forms—such as asking
for missing source material—cannot become healthy output merely because they are
well-written.

Truth Layer validates the receipt and its evidence metadata; it does not fetch an
external artifact or independently prove that an upstream system told the truth.
For stronger provenance, producers should emit destination acknowledgements and
content hashes that a downstream verifier controls.

## Fail-closed Action Gate

`ActionGate` converts a persisted, current Truth verdict into an auditable
authorization decision. Its interface intentionally does not accept a
caller-supplied verdict:

```python
decision = service.authorize_action(
    storage_id="persisted-receipt-id",
    action_id="review-follow-ups",
    policy_id="healthy_only",
)
if decision["authorized"]:
    perform_bounded_action()
```

The gate reads through `TruthService.get_run()`, so freshness is reevaluated
before each decision. It binds the decision to the receipt storage ID, action
ID, and policy. For an allow, the audit transaction rechecks the latest stored
verdict and freshness deadline before inserting the immutable record. If
another writer has already made that evidence stale or contradictory, the
decision fails closed instead of recording a stale allow.
There are two policies:

| Policy | Allowed current verdicts |
|---|---|
| `healthy_only` | `healthy` |
| `trusted_completion` | `healthy`, `verified_no_work` |

Missing records, malformed or unavailable reads, `failed`,
`degraded_contradictory`, and `stale` all block. `verified_no_work` blocks
under `healthy_only`. If the decision cannot be durably audited, the gate
raises an error and cannot authorize the action.

The included outcome workflow also reads the returned decision back from the
audit table and requires an exact field match before invoking its bounded
action. A plausible-looking but unpersisted allow therefore cannot execute.

An action decision contains only its generated decision ID, bound storage ID,
action ID, policy, observed verdict, boolean authorization, reason codes, and
timestamp. Exact retries are idempotent; conflicting content under the same
decision ID is rejected, and database triggers prevent decision updates or
deletes.

## Source-backed capability registry

[`platform-registry-v1.json`](platform-registry-v1.json) is the versioned
Mission Control inventory. It contains 71 capability entries:

| Kind | Current inventory |
|---|---:|
| Built-in agents | 4 |
| LLM-facing tools | 20 |
| Implemented/setup-required provider adapters | 33 |
| Other connector, channel, and model-route entries | 14 |

The 33 provider adapters are grouped into 9 lead-source, 8 verification,
6 sender, and 10 CRM implementations. Each registry entry includes a source
path/symbol, setup requirement, risk, approval mode, and read-back mode.

The status is part of the contract:

- `native`: implemented here and usable without configuring an external
  provider for that capability itself.
- `implemented/setup_required`: adapter code exists, but the operator must
  configure the external account or service.
- `bridge/catalog`: an integration seam or catalog entry exists; Globus does
  not bundle the external bridge/service as a native connected capability.
- `planned`: roadmap only, with no executable integration in this release.

This is an honest capability map, not a connected-account screen and not a
claim of OpenClaw feature parity. Loading and validation are data-only: they
do not import provider modules, read credentials, or contact services.

## JSON API

The server binds only to loopback (`127.0.0.1` by default) and sends no CORS
permission. JSON request bodies are limited to 64 KiB, must be UTF-8
`application/json`, reject duplicate keys/non-finite numbers, and use a fixed
contract. Receipt text is displayed with DOM `textContent`, not HTML, and
responses include restrictive browser security headers.

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/v1/summary` | Fleet totals grouped by latest verdict. |
| `GET` | `/api/v1/runs?limit=100&offset=0` | List persisted receipts and their latest evaluations. |
| `GET` | `/api/v1/runs/{receipt_id}` | Fetch one persisted receipt/evaluation. |
| `POST` | `/api/v1/receipts` | Evaluate and persist one receipt. |
| `GET` | `/api/v1/samples` | Preview five safe, freshly timestamped sample receipts. |
| `POST` | `/api/v1/samples/load` | Load samples; body must be `{}`. |
| `POST` | `/api/v1/judge/challenge` | Run the credential-free real-byte tamper challenge; body must be `{}`. |
| `POST` | `/api/v1/judge/outcome-gate` | Run the credential-free 3 → 3 allow/execute, then 3 → 2 block/prevent workflow; body must be `{}`. |
| `GET` | `/api/v1/platform/capabilities` | Read the validated capability summary, entries, and graph. |
| `GET` | `/api/v1/gate/decisions/{decision_id}` | Fetch one immutable Action Gate decision. |
| `GET` | `/api/v1/approvals?limit=100` | List privacy-safe approval proposals and derived states. |
| `GET` | `/api/v1/approvals/{proposal_id}` | Fetch one exact proposal, decision, and execution state. |
| `POST` | `/api/v1/approvals` | Create one exact payload-free proposal using the fixed approval envelope. |
| `POST` | `/api/v1/approvals/{proposal_id}/decision` | Immutably approve or reject a proposal. |
| `POST` | `/api/v1/judge/approval-center/stage` | Stage the credential-free pending approval proof; body must be `{}`. |
| `POST` | `/api/v1/judge/approval-center/{proposal_id}/approve` | Approve and resolve the bounded generated local proof; body must be `{}`. |
| `POST` | `/api/v1/judge/approval-center/{proposal_id}/reject` | Reject the bounded generated local proof; body must be `{}`. |

Example:

```bash
curl -X POST http://127.0.0.1:8765/api/v1/receipts \
  -H "Content-Type: application/json" \
  --data-binary @receipt.json
```

Successful ingestion returns the storage ID, whether the immutable receipt was newly
created, and the complete verdict/check explanation. An exact retry is idempotent
and adds an evaluation-history entry. Reusing a receipt ID for different content
returns HTTP `409`; audit history is never silently rewritten.

The API is intentionally unauthenticated and refuses non-loopback binds. Its
trust boundary is operating-system/process access to localhost and the local
SQLite files. `requested_by` and `decided_by` are audit labels supplied by that
trusted local caller, not cryptographically authenticated identities.

The generic approval API records proposals and human decisions only. It does
not accept callback code, callback URLs, or an arbitrary execution request.
Execution in the judge endpoints is limited to their built-in generated local
workflow.

## Included Globus agent integration

The public OSS runner in [`server/agent_runner.py`](../server/agent_runner.py)
uses [`agent_adapter.py`](agent_adapter.py) after it obtains a durable run ID:

1. The runner resolves the named built-in agent's explicit non-empty tool
   allowlist before any model call; a missing or empty grant fails closed.
2. The orchestrator advertises only those tool schemas and checks each tool
   name again before dispatcher invocation.
3. The runner records an aware UTC start time and calls the real Globus
   orchestrator.
4. It writes the dated Markdown brief as exact bytes.
5. The adapter reopens those bytes and verifies both size and SHA-256.
6. It checks the actual model reply for empty, too-short, refusal-like, and
   error-like output without copying private reply text into SQLite.
7. It emits and ingests a member-scoped receipt containing an install-keyed
   HMAC pseudonym rather than a raw email address.
8. The MySQL runner row is marked successful only when the Truth verdict is
   trusted.
9. Compact verdicts appear in the existing Agents dashboard and chat activity
   console; receipt payloads never enter those status APIs.

Exceptions after durable run creation produce explicit failed receipts. An artifact whose process
completed but whose content or read-back check failed becomes contradictory,
not green. Failure to persist the receipt also fails closed for runner status
while preserving any artifact that was already written for diagnosis.

Set `GLOBUS_TRUTH_DB` to choose the SQLite path. Source installs otherwise keep
`globus-truth.db` under the configured agent work directory. Docker stores it
at `/app/.state/globus-truth.db`, which is backed by the existing persistent
state volume.

To inspect receipts generated by the full Globus runner:

```bash
python -m globus_truth serve --db /path/to/globus-truth.db
```

Trusted persisted receipts are reevaluated when they are read. They
automatically transition to `stale` after the configured freshness deadline;
failed and contradictory verdicts retain precedence, and repeated UI polling
does not manufacture duplicate history rows.

## Integrate another agent

An agent should emit its receipt only after measuring its inputs and checking the
destination. Keep business logic in the agent and use Truth Layer as the shared
judge:

```python
import json
import urllib.request

receipt = {
    # Build the complete v1.0 receipt shown above.
}
request = urllib.request.Request(
    "http://127.0.0.1:8765/api/v1/receipts",
    data=json.dumps(receipt).encode("utf-8"),
    headers={"Content-Type": "application/json"},
    method="POST",
)
with urllib.request.urlopen(request, timeout=5) as response:
    verdict = json.load(response)["evaluation"]["verdict"]
```

For in-process integration:

```python
from globus_truth import TruthRepository, TruthService

service = TruthService(TruthRepository("globus-truth.db"))
result = service.ingest(receipt)
print(result["evaluation"]["verdict"])
```

Recommended fleet pattern:

1. Count inputs at the source.
2. Perform work.
3. Read back or acknowledge the destination.
4. Record evidence and deterministic checks.
5. Emit a receipt on every path, including no-work and failure.
6. Alert on `degraded_contradictory`, `failed`, or `stale`; do not turn them green
   from narrative text.

SQLite writes use parameterized queries and short connections. Receipts are immutable
by ID, latest verdicts power the dashboard, and explicit ingests plus verdict
transitions remain in the verdict history. Repeated unchanged reads do not add
duplicate audit events.

## Supported platforms

The component uses Python's `datetime`, `sqlite3`, `http.server`, and other standard
library modules only. It is designed for Python 3.10+ on Windows, macOS, and Linux.
The dashboard works in current browsers with JavaScript enabled. No Docker, Node.js,
database server, cloud account, API key, or outbound network access is required.

## Built during OpenAI Build Week with Codex and GPT-5.6

**Globus Truth Layer, Mission Control, Action Gate, Consequence Firewall,
Approval Center, and the public OSS AgentRunner integration are the new work
built during OpenAI Build Week with Codex and GPT-5.6.** The work includes the
v1 receipt contract, strict evaluator, SQLite receipt/decision/approval audit
repositories, local dashboard/API, source-backed capability registry, exact
runtime grants for four built-in background agents, safe fixtures, real
artifact verification, the credential-free Evidence Lab, business-outcome
challenge, and changed/exact/replay approval proof, visible verdict badges,
adversarial tests, and this documentation.

The broader Globus platform and its existing agent fleet predate this Build Week
work. They were not built with Codex or GPT-5.6, and this repository does not claim
otherwise. Truth Layer remains independently runnable. In the included public
AgentRunner, only a trusted persisted receipt can produce an `ok` ledger state;
receipt identity and persistence failures fail closed.
