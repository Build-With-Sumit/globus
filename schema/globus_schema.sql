-- ─────────────────────────────────────────────────────────────────────
-- Globus schema — MySQL 8 / utf8mb4. Load once into your `globus` DB:
--
--   mysql -u globus -p globus < schema/globus_schema.sql
--
-- Per-member isolation: EVERY user-data table is scoped by `email`
-- (or `member_email` where the column was added later). The chat /
-- voice / agent paths NEVER read across members.
-- ─────────────────────────────────────────────────────────────────────

SET NAMES utf8mb4;
SET sql_mode = 'STRICT_TRANS_TABLES,NO_ZERO_DATE,NO_ZERO_IN_DATE,ERROR_FOR_DIVISION_BY_ZERO';

-- ─────────────────────────────────────────────────────────────────────
-- Members + auth
-- ─────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS members (
  id              BIGINT AUTO_INCREMENT PRIMARY KEY,
  email           VARCHAR(320) NOT NULL UNIQUE,
  first_name      VARCHAR(120),
  last_name       VARCHAR(120),
  country         VARCHAR(120),
  status          ENUM('active','pending','cancelled','comp') NOT NULL DEFAULT 'pending',
  comp            TINYINT(1) NOT NULL DEFAULT 0,
  source          VARCHAR(80),
  stripe_customer_id VARCHAR(64),
  onboarded_at    TIMESTAMP NULL,
  created_at      TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  KEY ix_status (status),
  KEY ix_created (created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS auth_codes (
  id              BIGINT AUTO_INCREMENT PRIMARY KEY,
  email           VARCHAR(320) NOT NULL,
  code_hash       VARCHAR(128) NOT NULL,
  expires_at      TIMESTAMP NOT NULL,
  used_at         TIMESTAMP NULL,
  created_at      TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  KEY ix_email_expires (email, expires_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- ─────────────────────────────────────────────────────────────────────
-- Config — runtime secrets / model picks (cfg() reads this BEFORE .env)
-- ─────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS config (
  name            VARCHAR(80) PRIMARY KEY,
  value           TEXT,
  updated_at      TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
                  ON UPDATE CURRENT_TIMESTAMP
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- ─────────────────────────────────────────────────────────────────────
-- Vault sources — the per-member raw context Globus reads from
-- ─────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS globus_vault_sources (
  id                  BIGINT AUTO_INCREMENT PRIMARY KEY,
  email               VARCHAR(320) NOT NULL,
  source_type         VARCHAR(80)  NOT NULL,
  source_identifier   VARCHAR(255) NOT NULL DEFAULT '',
  source_label        VARCHAR(255),
  content             MEDIUMTEXT,
  char_count          INT NOT NULL DEFAULT 0,
  file_count          INT,
  last_synced_at      TIMESTAMP NULL,
  updated_at          TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
                      ON UPDATE CURRENT_TIMESTAMP,
  UNIQUE KEY uniq_email_type_ident (email, source_type, source_identifier),
  KEY ix_email_type (email, source_type)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- Pre-built per-member intelligence digest (built offline; cheap to read)
CREATE TABLE IF NOT EXISTS globus_intelligence (
  email               VARCHAR(320) PRIMARY KEY,
  content             MEDIUMTEXT,
  source_summary      TEXT,
  built_with          VARCHAR(80),
  raw_char_count      INT,
  digest_char_count   INT,
  built_at            TIMESTAMP NULL,
  updated_at          TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
                      ON UPDATE CURRENT_TIMESTAMP
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- Per-file vault index (Drive, Gmail, Obsidian uploads, etc.)
CREATE TABLE IF NOT EXISTS globus_vault_files (
  id                  BIGINT AUTO_INCREMENT PRIMARY KEY,
  email               VARCHAR(320) NOT NULL,
  connection_id       BIGINT,
  provider_account    VARCHAR(320),
  source_type         VARCHAR(80) NOT NULL,
  external_id         VARCHAR(255),
  filename            VARCHAR(512),
  mime_type           VARCHAR(120),
  size_bytes          BIGINT,
  modified_at         TIMESTAMP NULL,
  extracted_path      VARCHAR(1024),
  extracted_chars     INT,
  extracted           TINYINT(1) NOT NULL DEFAULT 0,
  skip_reason         VARCHAR(255),
  vault_processed_at  TIMESTAMP NULL,
  metadata            JSON,
  created_at          TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at          TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
                      ON UPDATE CURRENT_TIMESTAMP,
  UNIQUE KEY uniq_email_source_external (email, source_type, external_id),
  KEY ix_email_source (email, source_type),
  KEY ix_email_filename (email, filename(120)),
  KEY ix_email_extracted (email, extracted, vault_processed_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- ─────────────────────────────────────────────────────────────────────
-- Chat history
-- ─────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS globus_messages (
  id              BIGINT AUTO_INCREMENT PRIMARY KEY,
  email           VARCHAR(320) NOT NULL,
  role            ENUM('user','assistant','system','tool') NOT NULL,
  content         MEDIUMTEXT,
  created_at      TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  KEY ix_email_created (email, created_at),
  KEY ix_email_role_created (email, role, created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- Per-member directives — saved via the save_preference tool, replayed
-- into every chat/voice system prompt.
CREATE TABLE IF NOT EXISTS globus_member_preferences (
  id              BIGINT AUTO_INCREMENT PRIMARY KEY,
  email           VARCHAR(320) NOT NULL,
  rule_text       TEXT NOT NULL,
  source          VARCHAR(80),
  created_at      TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  KEY ix_email (email)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- Security events — prompt-injection / jailbreak audit
CREATE TABLE IF NOT EXISTS globus_security_events (
  id              BIGINT AUTO_INCREMENT PRIMARY KEY,
  email           VARCHAR(320),
  surface         VARCHAR(20) NOT NULL,
  pattern         VARCHAR(255),
  preview         VARCHAR(512),
  created_at      TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  KEY ix_email_created (email, created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- ─────────────────────────────────────────────────────────────────────
-- OAuth connections (Google Drive, Gmail, Microsoft Graph)
-- Refresh tokens are Fernet-encrypted (see config.GLOBUS_OAUTH_ENCRYPTION_KEY).
-- ─────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS globus_oauth_connections (
  id                  BIGINT AUTO_INCREMENT PRIMARY KEY,
  email               VARCHAR(320) NOT NULL,
  provider            VARCHAR(40) NOT NULL,
  provider_account    VARCHAR(320) NOT NULL,
  source_types        VARCHAR(255),
  scopes              TEXT,
  access_token_enc    BLOB,
  refresh_token_enc   BLOB,
  expires_at          TIMESTAMP NULL,
  user_info           JSON,
  drive_folder_ids    TEXT,
  gmail_query         VARCHAR(255),
  sync_status         ENUM('idle','running','error','disabled')
                      NOT NULL DEFAULT 'idle',
  last_synced_at      TIMESTAMP NULL,
  last_sync_error     TEXT,
  needs_reconnect     TINYINT(1) NOT NULL DEFAULT 0,
  drive_files         INT,
  drive_bytes         BIGINT,
  gmail_files         INT,
  gmail_bytes         BIGINT,
  created_at          TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at          TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
                      ON UPDATE CURRENT_TIMESTAMP,
  UNIQUE KEY uniq_email_provider_account (email, provider, provider_account),
  KEY ix_email_provider (email, provider),
  KEY ix_sync_status (sync_status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS globus_oauth_states (
  state_token     VARCHAR(64) PRIMARY KEY,
  email           VARCHAR(320) NOT NULL,
  provider        VARCHAR(40) NOT NULL,
  source_types    VARCHAR(255),
  redirect_after  VARCHAR(1024),
  expires_at      TIMESTAMP NOT NULL,
  created_at      TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  KEY ix_expires (expires_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- One row per sync run (kept for the connect dashboard's "recent runs" widget).
-- Truncate periodically if it grows unbounded; no FK so deletes cascade is manual.
CREATE TABLE IF NOT EXISTS globus_sync_runs (
  id              BIGINT AUTO_INCREMENT PRIMARY KEY,
  connection_id   BIGINT NOT NULL,
  email           VARCHAR(320) NOT NULL,
  source_type     VARCHAR(80) NOT NULL,
  status          ENUM('success','error') NOT NULL,
  items_count     INT,
  chars_written   BIGINT,
  error_message   TEXT,
  started_at      TIMESTAMP NULL,
  finished_at     TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  KEY ix_conn_finished (connection_id, finished_at),
  KEY ix_email_finished (email, finished_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- API-key connections (Freshsales etc.)
CREATE TABLE IF NOT EXISTS globus_apikey_connections (
  id                  BIGINT AUTO_INCREMENT PRIMARY KEY,
  email               VARCHAR(320) NOT NULL,
  provider            VARCHAR(50) NOT NULL,
  provider_account    VARCHAR(320) NOT NULL,
  subdomain           VARCHAR(255) NOT NULL,
  api_key_enc         BLOB NOT NULL,
  source_types        VARCHAR(255),
  product_scope       VARCHAR(500),
  metadata            JSON,
  last_synced_at      TIMESTAMP NULL,
  last_sync_error     TEXT,
  sync_status         ENUM('idle','running','error','disabled')
                      NOT NULL DEFAULT 'idle',
  sync_interval_sec   INT NOT NULL DEFAULT 600,
  needs_reconnect     TINYINT(1) NOT NULL DEFAULT 0,
  created_at          TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at          TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
                      ON UPDATE CURRENT_TIMESTAMP,
  KEY ix_email_provider (email, provider),
  KEY ix_sync_status (sync_status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- ─────────────────────────────────────────────────────────────────────
-- Bridged messaging — WhatsApp, Telegram, Microsoft Teams
-- ─────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS globus_whatsapp_messages (
  id              BIGINT AUTO_INCREMENT PRIMARY KEY,
  member_email    VARCHAR(320) NOT NULL,
  chat_name       VARCHAR(255),
  sender          VARCHAR(255),
  body            TEXT,
  direction       ENUM('in','out','unknown') NOT NULL DEFAULT 'unknown',
  wa_ts           VARCHAR(64),
  fingerprint     VARCHAR(64),
  received_at     TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE KEY uniq_email_fp (member_email, fingerprint),
  KEY ix_email_received (member_email, received_at),
  KEY ix_email_chat (member_email, chat_name)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS globus_telegram_messages (
  id              BIGINT AUTO_INCREMENT PRIMARY KEY,
  member_email    VARCHAR(320) NOT NULL,
  tg_chat_id      BIGINT,
  tg_message_id   BIGINT,
  chat_name       VARCHAR(255),
  chat_type       VARCHAR(40),
  sender          VARCHAR(255),
  sender_username VARCHAR(120),
  body            TEXT,
  direction       ENUM('in','out','unknown') NOT NULL DEFAULT 'unknown',
  tg_ts           TIMESTAMP NULL,
  received_at     TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  KEY ix_email_received (member_email, received_at),
  KEY ix_email_tgchat (member_email, tg_chat_id),
  UNIQUE KEY uniq_email_chat_msg (member_email, tg_chat_id, tg_message_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS globus_telegram_bots (
  id                  BIGINT AUTO_INCREMENT PRIMARY KEY,
  member_email        VARCHAR(320) NOT NULL,
  bot_username        VARCHAR(120),
  bot_token_enc       BLOB NOT NULL,
  allowed_send_chats  JSON,
  allowed_actions     JSON,
  status              ENUM('active','disabled') NOT NULL DEFAULT 'active',
  created_at          TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  KEY ix_email_status (member_email, status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS globus_telegram_bot_sends (
  id                  BIGINT AUTO_INCREMENT PRIMARY KEY,
  member_email        VARCHAR(320) NOT NULL,
  bot_id              BIGINT,
  target_chat_id      VARCHAR(40),
  target_chat_name    VARCHAR(255),
  tg_message_id       BIGINT,
  initiator           VARCHAR(80),
  status              ENUM('sent','failed','denied') NOT NULL DEFAULT 'sent',
  error               TEXT,
  body_preview        VARCHAR(512),
  created_at          TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  KEY ix_email_created (member_email, created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS globus_teams_messages (
  id              BIGINT AUTO_INCREMENT PRIMARY KEY,
  member_email    VARCHAR(320) NOT NULL,
  ms_chat_id      VARCHAR(255),
  ms_message_id   VARCHAR(255),
  chat_name       VARCHAR(255),
  chat_type       VARCHAR(40),
  sender          VARCHAR(255),
  sender_user_id  VARCHAR(120),
  body            TEXT,
  body_type       VARCHAR(20),
  ms_ts           TIMESTAMP NULL,
  fingerprint     VARCHAR(64),
  received_at     TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE KEY uniq_email_fp (member_email, fingerprint),
  KEY ix_email_received (member_email, received_at),
  KEY ix_email_chat (member_email, ms_chat_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- ─────────────────────────────────────────────────────────────────────
-- Agent schedules (per-member overrides for the catalog defaults)
-- ─────────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS globus_agent_schedules (
  id              BIGINT AUTO_INCREMENT PRIMARY KEY,
  member_email    VARCHAR(320) NOT NULL,
  agent_name      VARCHAR(80) NOT NULL,
  cadence         VARCHAR(40),
  enabled         TINYINT(1) NOT NULL DEFAULT 0,
  config_json     JSON,
  created_at      TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at      TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
                  ON UPDATE CURRENT_TIMESTAMP,
  UNIQUE KEY uniq_email_agent (member_email, agent_name)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- One row per agent run (success + failure). Powers the chat-page
-- agent-activity console + the brief viewer at /members/globus/agents/run.
CREATE TABLE IF NOT EXISTS globus_agent_runs (
  id              BIGINT AUTO_INCREMENT PRIMARY KEY,
  member_email    VARCHAR(320) NOT NULL,
  agent_name      VARCHAR(80) NOT NULL,
  status          ENUM('running','ok','error') NOT NULL DEFAULT 'running',
  brief_path      VARCHAR(1024),
  bytes_written   INT,
  error_message   TEXT,
  started_at      TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  finished_at     TIMESTAMP NULL,
  KEY ix_email_agent_started (member_email, agent_name, started_at),
  KEY ix_email_started (member_email, started_at),
  KEY ix_status (status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
