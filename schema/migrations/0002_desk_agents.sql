-- 0002_desk_agents — tables for the shared-inbox desk agents (v0.16).
--
-- Extracted from globus_schema.sql so an install already running v0.15 can take
-- the feature without re-running the whole bootstrap. Every statement is
-- CREATE TABLE IF NOT EXISTS, so applying it to a database that already has
-- them (a fresh install that just ran the bootstrap) is a safe no-op.

-- ─────────────────────────────────────────────────────────────────────
-- Shared inboxes — desk agents (see server/email_desks.py, desk_agents.py)
-- ─────────────────────────────────────────────────────────────────────
-- A "desk" is a shared mailbox (support@, sales@) owned by a member of staff
-- rather than by the operator. Desks themselves are NOT stored: they are
-- resolved live from globus_oauth_connections on every run, because desk
-- ownership churns and a hardcoded map goes stale silently. What IS stored is
-- everything an agent must not forget between runs.

-- One switch per (desk, agent). A missing row means OFF: discovery is live, so
-- a default of ON would start working every mailbox the moment an unrelated
-- account is connected.
CREATE TABLE IF NOT EXISTS desk_agent_config (
  id            BIGINT AUTO_INCREMENT PRIMARY KEY,
  mailbox       VARCHAR(320) NOT NULL,
  agent         VARCHAR(40)  NOT NULL,   -- spam_rescue|responder|followup|learning
  enabled       TINYINT(1)   NOT NULL DEFAULT 0,
  model         VARCHAR(40)  NULL,       -- per-desk model override
  settings_json TEXT         NULL,
  created_at    TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at    TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
                ON UPDATE CURRENT_TIMESTAMP,
  UNIQUE KEY uniq_desk_agent (mailbox, agent)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- Every spam verdict, including "left in spam". Recording only the rescues
-- would make each run re-classify the whole Spam folder, so the agent's cost
-- would grow with the age of the folder rather than with new mail.
CREATE TABLE IF NOT EXISTS desk_spam_rescues (
  id          BIGINT AUTO_INCREMENT PRIMARY KEY,
  msg_id      VARCHAR(64)  NOT NULL,
  thread_id   VARCHAR(64)  NULL,
  mailbox     VARCHAR(320) NOT NULL,
  product     VARCHAR(120) NULL,
  owner_email VARCHAR(320) NULL,
  sender      VARCHAR(320) NULL,
  subject     VARCHAR(500) NULL,
  snippet     TEXT         NULL,
  verdict     VARCHAR(32)  NULL,       -- business|spam, plus a short reason
  action      ENUM('moved','left') NOT NULL,
  created_at  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE KEY uniq_msg (msg_id),
  KEY ix_mailbox_action (mailbox, action, created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- One row per thread we drafted into. `mode` is the lifecycle:
--   draft       our draft is live and waiting for a human
--   superseded  we withdrew an unsent draft a human had overtaken
--   sent        the human had ALREADY SENT our draft — nothing was withdrawn
-- The last two must stay distinct. Marking a send as "superseded" would have
-- the log claim a withdrawal that never happened, which is how a destructive
-- bug hides in plain sight. See email_desks.delete_draft_if_unsent.
CREATE TABLE IF NOT EXISTS desk_replies (
  id             BIGINT AUTO_INCREMENT PRIMARY KEY,
  msg_id         VARCHAR(64)  NOT NULL,   -- the message we replied to
  thread_id      VARCHAR(64)  NOT NULL,
  mailbox        VARCHAR(320) NOT NULL,
  product        VARCHAR(120) NULL,
  owner_email    VARCHAR(320) NULL,
  customer_email VARCHAR(320) NULL,
  subject        VARCHAR(500) NULL,
  category       VARCHAR(40)  NULL,
  confidence     FLOAT        NULL,
  draft_id       VARCHAR(64)  NULL,      -- the Gmail draft; re-read before deleting
  customer_body  MEDIUMTEXT   NULL,
  draft_body     MEDIUMTEXT   NULL,      -- what WE wrote, for the edit diff
  mode           ENUM('draft','superseded','sent') NOT NULL DEFAULT 'draft',
  created_at     TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at     TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
                 ON UPDATE CURRENT_TIMESTAMP,
  UNIQUE KEY uniq_msg (msg_id),
  KEY ix_mailbox_thread (mailbox, thread_id),
  KEY ix_mailbox_mode (mailbox, mode, created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- One nudge per thread, ever. The UNIQUE key is the guarantee, not the code
-- path that checks it first: a retry, a parallel run or a crash between the
-- draft and the row must not be able to produce a second nudge.
CREATE TABLE IF NOT EXISTS desk_followups (
  id            BIGINT AUTO_INCREMENT PRIMARY KEY,
  mailbox       VARCHAR(320) NOT NULL,
  thread_id     VARCHAR(64)  NOT NULL,
  product       VARCHAR(120) NULL,
  owner_email   VARCHAR(320) NULL,
  correspondent VARCHAR(320) NULL,
  subject       VARCHAR(500) NULL,
  draft_id      VARCHAR(64)  NULL,
  created_at    TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE KEY uniq_mailbox_thread (mailbox, thread_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- The learning agent's dedup ledger, so a correction is distilled once.
-- The lesson text itself lives in a markdown file per (desk, agent) — the
-- people who own these desks should be able to read and edit what their agent
-- believes, and a row in a table nobody opens is not a feedback loop.
CREATE TABLE IF NOT EXISTS desk_lessons_seen (
  id         BIGINT AUTO_INCREMENT PRIMARY KEY,
  source     VARCHAR(32)  NOT NULL,     -- draft_edit|rescue_reversed
  source_id  VARCHAR(64)  NOT NULL,
  mailbox    VARCHAR(320) NOT NULL,
  agent      VARCHAR(40)  NOT NULL,
  lesson     TEXT         NULL,
  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE KEY uniq_source (source, source_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
