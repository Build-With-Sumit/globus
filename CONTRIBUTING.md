# Contributing to Globus

Thanks for considering it. A few orienting points before you write code.

## What we want

- **Port work from [ROADMAP.md](ROADMAP.md)** — especially v0.2 (chat
  orchestrator + heavier tool impls + member-auth routes). Open an
  issue saying you're working on it so we don't double-up.
- **New data sources** — e.g. a Notion bridge, a Slack bridge, an
  IMAP bridge for non-Gmail email. Follow the
  `globus_vault_sources` shape and the per-member isolation rules
  in [ARCHITECTURE.md](ARCHITECTURE.md).
- **Custom agents** — fork `server/globus_agents_catalog.py`. We're
  happy to link to interesting agent collections from the README.
- **Doc improvements** — install paths that broke for you, .env vars
  we forgot to document, things that surprised you.

## What we don't want (yet)

- **Big framework swaps** — no Flask / FastAPI / SQLAlchemy /
  Pydantic v3 / Pythonpathic-purity-of-the-month rewrites. The stdlib
  choice is on purpose (see ARCHITECTURE.md § "Why no Flask").
- **Embeddings / vector DB integration** — there's an explicit ADR
  about this (`004-no-embeddings-yet-for-globus.md`). Read it before
  proposing one.
- **Tests that don't add value** — we'd rather have integration tests
  that boot the full server + hit endpoints than 200 unit tests
  mocking `db_read`. If you add tests, make them the former.

## Setup for dev

```bash
git clone https://github.com/Build-With-Sumit/globus.git
cd globus
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp config/.env.example .env && $EDITOR .env
mysql -u root -p <<EOF
CREATE DATABASE globus_dev;
CREATE USER 'globus'@'localhost' IDENTIFIED BY 'dev';
GRANT ALL ON globus_dev.* TO 'globus'@'localhost';
EOF
mysql -u globus -pdev globus_dev < schema/globus_schema.sql
python3 server/globus_server.py
```

If you don't have a Claude Max subscription, set
`GLOBUS_LLM_PROVIDER=deepseek` + `DEEPSEEK_API_KEY=...` — DeepSeek is
the cheapest path for dev iteration (~$0.0001 per 1K tokens).

## House rules

- **Stdlib first.** New runtime deps need a reason in the PR description.
- **No emoji in source code or comments** unless the PR is specifically
  about the user-facing UI text. They render inconsistently across
  terminals + IDE configs.
- **Per-member isolation is sacred.** Every new query that reads or
  writes member data goes through `email = %s` scoping. Defense in
  depth: app layer also checks owner_id where applicable.
- **Cite specific files + line numbers** in PR descriptions ("changes
  `server/globus_search.py:62` to handle …") so reviewers can navigate.
- **One concern per PR.** A bg-sync port + a tools-loop fix + a doc
  cleanup is three PRs.

## Commit message style

```
<area>: <short imperative>

<longer explanation if needed — the WHY, not the WHAT.
Reference any issue numbers and reproducible steps.>
```

Areas: `server`, `schema`, `docs`, `config`, `agents`, `ops`.

Examples (from the reference repo):
```
refactor: extract globus_vault_db.py — vault sources + chat history CRUD
bhishma: add videoraiq daily tracker mode + 10:00 IST cron
docs: handover update — globussoft.ai WordPress live + globus next
```

## Reviewing

PRs go through one reviewer. If you don't hear back in a week, ping
the issue. We don't squash on merge — your commit history shows up in
`git log`, so write them like they'll be read.

## Code of conduct

Be direct, be kind, be specific. We don't have a formal CoC because
this project is small enough that the [Contributor
Covenant](https://www.contributor-covenant.org/) applies in spirit
without a formal adoption. If something feels off, email
sumit@globussoft.com.

## License

By submitting a PR you agree your contribution is licensed under
[AGPL-3.0](LICENSE) — the same as the rest of the project. We don't
ask for CLA signatures; the AGPL inbound = AGPL outbound model is
sufficient.
