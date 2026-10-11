-- 0003_relay_keys — keys for the outbound notification relay (POST /api/notify).
--
-- Extracted from globus_schema.sql so an install already on 0002 can take the
-- feature without re-running the whole bootstrap. CREATE TABLE IF NOT EXISTS,
-- so applying it to a database that already has the table is a safe no-op.

-- One row per relay key. Only the SHA-256 of the key is stored; the plaintext is
-- shown once, when scripts/relay_key.py mints it. A missing table or an empty
-- table means /api/notify answers 401 to everything.
CREATE TABLE IF NOT EXISTS globus_relay_keys (
  id            INT AUTO_INCREMENT PRIMARY KEY,
  key_hash      CHAR(64)     NOT NULL UNIQUE,
  service_name  VARCHAR(64)  NOT NULL,
  member_email  VARCHAR(255) NOT NULL,
  status        ENUM('active','revoked') NOT NULL DEFAULT 'active',
  created_at    TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  last_used_at  TIMESTAMP NULL,
  allowed_chats JSON NULL,               -- NULL = the bot's allowed_send_chats only
  expires_at    TIMESTAMP NULL,
  label         VARCHAR(64) NULL,
  KEY idx_relay_service (service_name),
  KEY idx_relay_member (member_email)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
