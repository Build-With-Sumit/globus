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

- **Ubuntu 22.04 or 24.04** (any Linux works; that's just what's tested)
- **Python 3.10+**, **MySQL 8**, **nginx** (for TLS + reverse proxy in
  prod — optional for local dev)
- **An LLM** — one of:
  - A **Claude Max subscription** + the [OAuth proxy](docs/claude-oauth-proxy.md)
    (recommended — zero per-token spend)
  - An **Anthropic API key** (direct API, pay per token)
  - A **DeepSeek API key** (cheap fallback; lower quality)
- **Optional sources** — Google OAuth client (Drive + Gmail), Microsoft
  Graph OAuth (Teams), Telethon API credentials (Telegram), an
  ElevenLabs Conversational AI agent (voice).

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
mysql -u globus -p globus <<EOF
INSERT INTO members (email, first_name, status)
VALUES ('you@example.com', 'You', 'active');
EOF
```

Then visit `/members/login` and request an OTP code. The default
sender uses `EMAIL_API_KEY` (SendGrid by default — swap to any SMTP
sender by editing `server/members_auth_html.py`).

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

## Upgrading

```bash
git pull
mysql -u globus -p globus < schema/globus_schema.sql   # idempotent — CREATE TABLE IF NOT EXISTS
pip install -r requirements.txt
sudo systemctl restart globus.service
```
