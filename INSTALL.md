# Installing Globus

> **Status: alpha.** The reference implementation runs in production at
> buildwithsumit.com. This guide gets v0.5 running on your box — sign-in
> via OTP, vault from Obsidian zip + Google Drive + Gmail + the
> WhatsApp/Teams Chrome extension, text and voice chat (ElevenLabs;
> see [`docs/voice-setup.md`](docs/voice-setup.md)), and a working
> agents subsystem (3 sample agents that produce daily markdown briefs;
> see `/members/globus/agents`). Telegram via Telethon daemon is the
> only major source still ahead — see [ROADMAP.md](ROADMAP.md).

## What you'll need

- **Docker + Docker Compose** (the fast path; see § Quick start below).
- OR for a manual install: **Ubuntu 22.04+**, **Python 3.10+**, **MySQL 8**,
  **nginx** (for TLS + reverse proxy in prod — optional for local dev).
- **An LLM** — one of:
  - A **Claude Max subscription** + the [OAuth proxy](docs/claude-oauth-proxy.md)
    (recommended — zero per-token spend)
  - An **Anthropic API key** (direct API, pay per token)
  - A **DeepSeek API key** (cheap fallback; lower quality)
- **Optional sources** — Google OAuth client (Drive + Gmail), Microsoft
  Graph OAuth (Teams), Telethon API credentials (Telegram), an
  ElevenLabs Conversational AI agent (voice).

## Quick start (Docker — recommended)

Five commands and you have a working Globus on `http://localhost:8090`:

```bash
git clone https://github.com/Build-With-Sumit/globus.git
cd globus

cp config/.env.example .env
$EDITOR .env       # at minimum: set DB_PASSWORD + GLOBUS_FIRST_MEMBER_EMAIL
                   # (the rest can stay at defaults for local testing)

docker compose up -d
```

What the entrypoint does on first boot:
1. Waits for MySQL to accept connections.
2. Applies `schema/globus_schema.sql` (idempotent).
3. Generates `SESSION_SECRET` if you didn't supply one, persists to a
   named volume so restarts keep the same secret.
4. Seeds `GLOBUS_FIRST_MEMBER_EMAIL` as an active member if set.
5. Starts the Python server on `:8090`.

Then sign in:

```bash
open http://localhost:8090/members/login
docker compose logs -f globus | grep "OTP code for"
# pastes a 6-digit code from the dev-mode stderr fallback — no
# SendGrid/SMTP needed for local testing
```

Common ops:

```bash
docker compose logs -f globus       # follow app log
docker compose exec globus bash     # shell inside the container
docker compose exec db mysql -uglobus -p$DB_PASSWORD globus  # SQL prompt
docker compose down                 # stop (state persists in volumes)
docker compose down -v              # nuke everything including volumes
```

The image is `python:3.12-slim` + ~120 MB of dependencies (PyMySQL +
cryptography + tini + mysql-client). State persists in three named
volumes: `db_data` (MySQL), `agent_briefs`
(`/var/lib/globus/agents`), `drive_cache` (`/var/lib/globus/raw-data`).

For voice + OAuth providers, follow §§ "Google OAuth" / "ElevenLabs"
in the [Bootstrap config](#3-bootstrap-config) section below — those
env vars work identically in Docker (set in `.env`).

If you'd rather not use Docker, skip to § 1 below.

## 1. Clone + Python env

```bash
git clone https://github.com/Build-With-Sumit/globus.git
cd globus

python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## 2. MySQL

```bash
sudo mysql <<EOF
CREATE DATABASE globus CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE USER 'globus'@'localhost' IDENTIFIED BY 'change-this-password';
GRANT ALL PRIVILEGES ON globus.* TO 'globus'@'localhost';
FLUSH PRIVILEGES;
EOF

mysql -u globus -p globus < schema/globus_schema.sql
```

Verify:
```bash
mysql -u globus -p globus -e 'SHOW TABLES;'
# should list ~18 tables: members, auth_codes, config, globus_*, etc.
```

## 3. Bootstrap config

```bash
cp config/.env.example .env
$EDITOR .env
```

Fill in **at minimum**:
- `DB_PASSWORD` (the one you set above)
- `SESSION_SECRET` — generate with `python3 -c 'import secrets; print(secrets.token_hex(32))'`
- `SITE` — the public URL where Globus will be served (e.g. `https://globus.example.com`)

### Google OAuth (optional — needed for Drive + Gmail sync)

Drive and Gmail sync are opt-in. To enable, create an OAuth client in
[Google Cloud Console](https://console.cloud.google.com/apis/credentials):

1. Make a new project (or reuse one).
2. Enable the **Google Drive API** and **Gmail API** (APIs & Services →
   Library) — enable just Drive if you only want Drive sync.
3. OAuth consent screen → External → add `drive.readonly`,
   `gmail.readonly` (skip if Drive-only), `userinfo.email`,
   `userinfo.profile`, `openid` scopes.
4. Credentials → Create OAuth client ID → Web application. Add
   `https://<your-site>/members/connect/google/callback` as an authorised
   redirect URI.
5. Generate a Fernet key for at-rest token encryption:
   ```bash
   python3 -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'
   ```
6. Insert all three into the `config` table:
   ```sql
   INSERT INTO config (name, value) VALUES
     ('GOOGLE_OAUTH_CLIENT_ID',     '<client-id>.apps.googleusercontent.com'),
     ('GOOGLE_OAUTH_CLIENT_SECRET', '<client-secret>'),
     ('GLOBUS_OAUTH_ENCRYPTION_KEY','<fernet-key>');
   ```
7. Restart `globus.service`. Boot log should now say
   `bg-sync: enabled (Google OAuth configured)`.

Members can then connect a Google account at `/members/connect`. The
first sync fires immediately in the background; subsequent syncs run
hourly when the connection is older than 1h.

For other providers (Microsoft Teams, ElevenLabs voice), see their
dedicated docs — those land in v0.3b+ ([ROADMAP.md](ROADMAP.md)).

## 4. Pick your LLM

### Option A — Claude OAuth proxy (recommended, zero per-token cost)

If you have a Claude Max subscription, run the OAuth proxy on the same
box. It wraps `claude --print` and exposes an OpenAI-compatible
endpoint at `127.0.0.1:8787`.

See [`docs/claude-oauth-proxy.md`](docs/claude-oauth-proxy.md) for the
full setup (a one-time `claude` CLI login + a systemd unit). Defaults
in `.env`:

```bash
GLOBUS_LLM_PROVIDER=claude-oauth
GLOBUS_OAUTH_MODEL=sonnet
```

### Option B — Anthropic API direct (pay per token)

```bash
GLOBUS_LLM_PROVIDER=anthropic
ANTHROPIC_API_KEY=sk-ant-...
```

### Option C — DeepSeek (cheap, OpenAI-compatible)

```bash
GLOBUS_LLM_PROVIDER=deepseek
DEEPSEEK_API_KEY=sk-...
```

## 5. Customize your persona + agents

Globus's tone, capabilities block, and agents catalog are
intentionally bring-your-own:

```bash
cp config/persona.example.md config/persona.md
$EDITOR config/persona.md       # rewrite for YOUR audience

$EDITOR server/globus_agents_catalog.py   # replace the 3 example agents
```

The reference impl uses Mahabharata-named agents (Drona, Vyas, Sanjay,
Kripa, etc.) — those are NOT shipped here because they're branded for
one specific team. Define your own.

## 6. Run

```bash
python3 server/globus_server.py
# globus/0.3 booting on 127.0.0.1:8090
#   site:     https://globus.example.com
#   db:       globus@127.0.0.1:3306/globus
#   llm:      claude-oauth
#   bg-sync:  disabled (set GOOGLE_OAUTH_CLIENT_ID + SECRET to enable Drive sync)
```

Open <http://127.0.0.1:8090/globus> — you should see the public
landing page.

In production: put nginx in front (TLS, reverse-proxy `127.0.0.1:8090`).
Sample nginx block in [`docs/nginx-globus.conf`](docs/nginx-globus.conf).

## 7. (optional) systemd unit

```ini
# /etc/systemd/system/globus.service
[Unit]
Description=Globus - private AI assistant
After=network.target mysql.service

[Service]
User=globus
Group=globus
WorkingDirectory=/opt/globus
EnvironmentFile=/opt/globus/.env
ExecStart=/opt/globus/.venv/bin/python3 server/globus_server.py
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now globus.service
sudo systemctl status globus.service
```

## 8. First member

```bash
# Easiest path — CLI, no SQL:
python3 scripts/add_member.py you@example.com --name="Your Name"

# Equivalent SQL if you prefer:
mysql -u globus -p globus -e \
  "INSERT INTO members (email, first_name, status) \
   VALUES ('you@example.com', 'You', 'active');"
```

Then visit `/members/login` and request an OTP code. The default
sender uses `EMAIL_API_KEY` (SendGrid by default — swap to any SMTP
sender by editing `server/members_auth_html.py`). If `EMAIL_API_KEY`
isn't set, the OTP code is logged to stderr — fine for local dev.

## Pre-flight check

Before starting the server (or after any config / schema change),
run:

```bash
python3 scripts/check_install.py
```

It validates: `.env` loads, required env vars set, DB reachable,
expected tables present, storage paths writable, Fernet key
round-trips, persona file present, at least one active member.
Prints OK / WARN / FAIL per check with colour, exits 1 on any
fatal failure.

Equivalent live probe — once the server is running:

```bash
curl http://localhost:8090/api/health?deep=1
# Returns JSON with per-subsystem ok/error status. The shallow
# /api/health (no ?deep=1) stays cheap for load balancer probes.
```

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| `pymysql.err.OperationalError 2003` | MySQL not reachable. Check `DB_HOST` / `DB_PORT`. |
| `pymysql.err.OperationalError 1045` | Wrong DB password. |
| Login OTP email never arrives | `EMAIL_API_KEY` missing or wrong. The dev path logs codes to stderr. |
| Cryptography ImportError | `pip install cryptography>=42.0` (some old prebuilt wheels are missing Fernet) |
| Drive sync silently does nothing | Check the boot banner — `bg-sync: disabled` means `GOOGLE_OAUTH_CLIENT_ID` isn't set. Inserting it into `config` requires a service restart (cfg is cached at boot). |
| OAuth flow fails with "GLOBUS_OAUTH_ENCRYPTION_KEY not configured" | Generate a Fernet key and add to `config` table. See [§ Google OAuth](#3-bootstrap-config) above. |
| Drive sync silently stalls mid-run after a restart | Should auto-recover — the worker resets stale `running` rows on boot. If not, manually run `UPDATE globus_oauth_connections SET sync_status='idle' WHERE sync_status='running';` then restart. |

## 9. (optional) Schedule agents

Each agent in `server/globus_agents_catalog.py` declares a `schedule`
(e.g. `"08:00 daily"`) but the OSS runner doesn't parse cron
expressions — your crontab does. Wire your agents like this:

```bash
# /etc/cron.d/globus-agents — fire at 8 AM IST (= 02:30 UTC)
30 2 * * * globus  cd /opt/globus && /opt/globus/.venv/bin/python3 \
    scripts/run_agent.py research you@example.com \
    >> /var/log/globus-agents.log 2>&1
0  3 * * * globus  cd /opt/globus && /opt/globus/.venv/bin/python3 \
    scripts/run_agent.py sales-desk you@example.com \
    >> /var/log/globus-agents.log 2>&1
*/30 * * * * globus  cd /opt/globus && /opt/globus/.venv/bin/python3 \
    scripts/run_agent.py infra-watch you@example.com \
    >> /var/log/globus-agents.log 2>&1
```

Briefs land at `$GLOBUS_AGENTS_WORK_DIR/<sha1(email)[:16]>/` (default
`/var/lib/globus/agents/`) and surface in the chat-page activity
console + at `/members/globus/agents`.

To fire on demand without cron: tap "Run now" on
`/members/globus/agents`, or just ask Globus in chat ("run research").

## 10. (optional) Enable an org portal

"Globus for Organizations" gives one company its own host where employees
**self-enroll with their company email** and each chats over their *own*
connected data. It is entirely opt-in: with no `organizations` rows, none
of this runs and your install stays exactly as it is.

On an org host only the org pages are served — the single-tenant surfaces
(`/members/narada`, `/members/globus/agents`, …) return 404 there.

```sql
-- 1. the org, and the host that serves its portal
INSERT INTO organizations (slug, name, portal_host)
VALUES ('acme', 'Acme Inc', 'globus.acme.com');

-- 2. the email domain(s) that authorize self-enrollment.
--    Matching is EXACT — 'acme.com' does not admit 'acme.com.evil.com',
--    and each domain may belong to only one org.
INSERT INTO org_domains (org_id, domain)
VALUES ((SELECT id FROM organizations WHERE slug='acme'), 'acme.com');

-- 3. seed one admin so somebody can reach the sharing console.
--    Everyone else is created automatically on their first sign-in.
INSERT INTO org_members (org_id, email, role)
VALUES ((SELECT id FROM organizations WHERE slug='acme'),
        'admin@acme.com', 'admin');
```

Then point DNS + your reverse proxy for `globus.acme.com` at the same
Globus process (no second deployment) and restart. Employees go to
`https://globus.acme.com`, enter their `@acme.com` address, and get a
6-digit code.

Optional `.env` knobs (all documented in `config/.env.example`):

- `ORG_PORTAL_HOSTS=globus.acme.com:acme` — a fail-closed fallback so a
  recognised org host still refuses rather than falling through to the
  single-tenant site during a DB blip. Recommended.
- `ORG_GOOGLE_LOGIN_ENABLED=1` — show "Continue with Google". Only turn
  this on if the tenant really is on Google Workspace; otherwise the
  email-code flow is the correct (and default) path.
- `ORG_GOOGLE_OAUTH_CLIENT_ID` / `_SECRET` — a separate OAuth client for
  org sign-in. Falls back to your main client when unset.
- `ORG_LEGAL_ENTITY` / `ORG_LEGAL_CONTACT` / `ORG_LEGAL_UPDATED` — shown on
  the pre-auth `/privacy` and `/terms` pages that the Google consent screen
  links. Entity defaults to the org's name; blank contact/date are omitted.
  The shipped wording is a plain baseline — have your own counsel review it.

Sharing is **private by default**: a new employee sees no shared agents
until an admin grants one at `/members/globus/admin` (to everyone, a team,
or one person). Verify the isolation rules with:

```bash
python tests/test_org_db.py     # membership + domain + grant rules
python tests/test_org_gate.py   # routing: deny-by-default, no fall-through
```

## 11. (optional) Enable email intelligence

Two passes over a mailbox you've already connected under
`/members/connect` (Gmail source). A cheap **triage** pass files mail into
your taxonomy; a **reason** pass reads the full body of only what triage
couldn't recognise and records a judgment. A daily **digest** rolls it up.

Neither pass can archive, move or delete mail — the only Gmail write they
can reach is "add label" — and nothing here ever sends email.

**Write `EMAIL_INTEL_CONTEXT` first.** It is a free-text paragraph saying
what your business is, who matters, and what counts as urgent. It ships
empty on purpose: with no context the reasoner flags plausible-looking
noise and misses the mail that actually matters. Everything else has a
working default.

```bash
# Try it against a real mailbox without writing anything — no labels,
# no rows, no heartbeat.
EMAIL_INTEL_DRYRUN=1 python3 scripts/email_intel_run.py reason you@example.com
```

Then wire the crons. **One line per mailbox** — a separate process per
mailbox means a dead OAuth token on one can't take the others down, and
each stamps its own proof-of-life so the digest can name exactly which one
stopped:

```cron
# Tier 1 — cheap, every 30 min. The lookback is WIDER than the interval, so a
# run skipped by lock contention is recovered by the next one.
0,30 * * * * cd /opt/globus && flock -n /tmp/eintel-t1-a.lock \
    .venv/bin/python3 scripts/email_intel_run.py triage you@example.com \
    >> /var/log/globus-email-intel.log 2>&1

# Tier 2 — hourly, on a minute offset clear of Tier 1 so the grace window
# holds (EMAIL_INTEL_GRACE_MIN must exceed the gap between the two slots).
20 * * * *   cd /opt/globus && flock -n /tmp/eintel-t2-a.lock \
    .venv/bin/python3 scripts/email_intel_run.py reason you@example.com \
    >> /var/log/globus-email-intel.log 2>&1

# Digest — once a day.
30 2 * * *   cd /opt/globus && .venv/bin/python3 \
    scripts/email_intel_run.py digest \
    >> /var/log/globus-email-intel.log 2>&1
```

Set `EMAIL_INTEL_ACCOUNTS` to exactly the mailboxes your `reason` crons
cover. If it lists a mailbox no cron feeds, the digest will correctly
report that mailbox as **PIPELINE DOWN** rather than quietly implying all
is well — that gating is the point, so fix the list or the cron rather
than muting it.

Delivery goes to Telegram when `EMAIL_INTEL_TELEGRAM_MEMBER` +
`EMAIL_INTEL_TELEGRAM_CHAT_ID` are set (see `server/telegram_bot.py`);
otherwise the digest prints to stdout and cron captures it to the log.

```bash
python tests/test_email_intel.py   # heartbeat gate, parse failures, chunking
```

## 12. (optional) Enable the sales desk

A daily ranked call list, a short priorities brief, and a hygiene report.
**Read-only** — it never writes to a CRM and never sends email.

Write `SALES_DESK_CONTEXT` first (what you sell, to whom, what counts as
urgent). It ships empty, and without it the model ranks confidently and
wrongly. Then preview it — **posting is opt-in**, so a first run can't
surprise a team channel:

```bash
# prints, delivers nothing
python3 scripts/sales_desk_run.py you@example.com

# no model calls at all — pure deterministic ordering
python3 scripts/sales_desk_run.py you@example.com --no-llm
```

Tune `SALES_DESK_STATUS_RULES` to your pipeline. Stage names are **data**,
not code: `{status: {callable, weight, terminal}}`. Mark the dead stages
`terminal` and give the live ones weights — the weights are the fallback
ordering used whenever the model is unavailable, so they're worth getting
roughly right. An unknown stage stays callable, so a stage someone adds in
your CRM shows up rather than silently vanishing.

```cron
30 8 * * 1-5  cd /opt/globus && flock -n /tmp/sales-desk.lock \
    .venv/bin/python3 scripts/sales_desk_run.py you@example.com --post \
    >> /var/log/globus-sales-desk.log 2>&1
```

Set `SALES_DESK_CHAT_ID` (team) and `SALES_DESK_OPS_CHAT_ID` (you) —
failures go to the ops target, so the team channel never receives a stack
trace. Without a transport configured the desk prints and cron logs it.

If the desk finds **no callable leads it refuses to post** and alerts you
instead: an empty call list looks exactly like a quiet day, and that is the
one thing it must never imply. Check `SALES_DESK_SOURCES`, that the source
has data for that member, and that your status rules aren't marking every
stage terminal.

**Reading your CRM.** The built-in `pipeline` source reads this install's own
outbound prospects and their latest engagement. The bundled CRM plugins are
write-only (upsert/create/log), so there is no honest CRM read yet — register
your own source to add one:

```python
import sales_desk
sales_desk.register_source("mycrm", lambda member_email, limit: [
    {"id": ..., "name": ..., "email": ..., "company": ..., "title": ...,
     "status": ..., "owner": ..., "days_since": ..., "note": ..., "link": ...},
])
```

```bash
python tests/test_sales_desk.py
```

## 13. (optional) Enable the opportunity tracker

Track what you sent out — job applications, pitches, grants, CFPs — and let
replies find their way back to the right record. It reads a mailbox you've
already connected; it never sends mail and never modifies it.

```bash
# record what you sent. --domain is the strongest reply matcher, so set it
# when you know it.
python3 scripts/opportunity_run.py add you@example.com acme-staff-eng \
    "Acme Corp" --title "Staff Engineer" --domain acme.com --source referral

# match replies from the last 14 days. --dry-run shows what WOULD change.
python3 scripts/opportunity_run.py scan you@example.com you@example.com --dry-run
python3 scripts/opportunity_run.py scan you@example.com you@example.com

# funnel + what's gone quiet
python3 scripts/opportunity_run.py report you@example.com
```

```cron
0 7 * * *  cd /opt/globus && .venv/bin/python3 \
    scripts/opportunity_run.py scan you@example.com you@example.com \
    >> /var/log/globus-opportunities.log 2>&1
```

Notes worth knowing before you trust the numbers:

- **A screener is not an interview.** Automated assessments and one-way video
  steps are counted as `screener`, separately from `interview`, because
  merging them inflates the number you actually care about.
- **Stages only move forward.** A "thanks for applying" arriving after an
  interview invite won't rewind anything, so ordering of replies doesn't
  matter and re-running is safe.
- **Matching prefers a miss to a wrong guess.** Set `--domain` where you can;
  without it, an org whose name is entirely generic words won't match at all —
  by design, since it would otherwise claim half the inbox.
- `OPP_LLM_FALLBACK=1` adds a model pass for messages the patterns can't
  place. Off by default so the tracker costs nothing per message.

```bash
python tests/test_opportunity_tracker.py
```

## Upgrading

```bash
git pull
mysql -u globus -p globus < schema/globus_schema.sql   # idempotent — CREATE TABLE IF NOT EXISTS
pip install -r requirements.txt
sudo systemctl restart globus.service
```
