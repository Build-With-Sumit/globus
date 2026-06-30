#!/bin/bash
# Globus container entrypoint.
#
# Order of operations:
#   1. Wait for MySQL to accept connections (up to ~60s).
#   2. Apply schema if it hasn't been applied yet (idempotent — the
#      .sql is all CREATE TABLE IF NOT EXISTS).
#   3. Bootstrap SESSION_SECRET: if not provided via env, generate
#      one and persist to /app/.state/session_secret.hex so subsequent
#      restarts reuse the same secret (otherwise every restart would
#      invalidate every session cookie + voice + bridge token).
#   4. Optionally seed an initial member from GLOBUS_FIRST_MEMBER_EMAIL.
#   5. exec python3 server/globus_server.py.
#
# Exits non-zero on any setup failure so docker compose marks the
# container unhealthy + restarts per policy.

set -eu

log() { printf '[entrypoint] %s\n' "$*" >&2; }

# ─────────────────────────────────────────────────────────────────────
# 1. Wait for MySQL
# ─────────────────────────────────────────────────────────────────────

DB_HOST="${DB_HOST:-db}"
DB_PORT="${DB_PORT:-3306}"
DB_USER="${DB_USER:-globus}"
DB_NAME="${DB_NAME:-globus}"
# DB_PASSWORD is required — fail fast if it's missing.
if [ -z "${DB_PASSWORD:-}" ]; then
    log "ERROR: DB_PASSWORD not set in env"
    exit 1
fi

log "waiting for MySQL at ${DB_HOST}:${DB_PORT}..."
for i in $(seq 1 60); do
    if mysqladmin ping -h"${DB_HOST}" -P"${DB_PORT}" \
            -u"${DB_USER}" -p"${DB_PASSWORD}" --silent 2>/dev/null; then
        log "  MySQL is up after ${i}s"
        break
    fi
    if [ "${i}" -eq 60 ]; then
        log "ERROR: MySQL not reachable after 60s — check the db service"
        exit 1
    fi
    sleep 1
done

# ─────────────────────────────────────────────────────────────────────
# 2. Apply schema (idempotent — all statements are CREATE/ALTER IF
#    NOT EXISTS). We re-run it on every boot so schema bumps in
#    git pulls land automatically.
# ─────────────────────────────────────────────────────────────────────

if [ -f /app/schema/globus_schema.sql ]; then
    log "applying schema/globus_schema.sql..."
    if mysql -h"${DB_HOST}" -P"${DB_PORT}" \
            -u"${DB_USER}" -p"${DB_PASSWORD}" \
            "${DB_NAME}" < /app/schema/globus_schema.sql 2>&1 \
            | grep -v 'Using a password' >&2; then
        log "  schema OK"
    else
        log "ERROR: schema apply failed"
        exit 1
    fi
fi

# ─────────────────────────────────────────────────────────────────────
# 3. SESSION_SECRET bootstrap. If the caller provided one via env,
#    we use it. Otherwise we generate a fresh 32-byte hex on first
#    boot and persist to /app/.state/session_secret.hex so subsequent
#    restarts reuse it (a fresh secret each restart would invalidate
#    every session cookie + voice token + bridge token).
# ─────────────────────────────────────────────────────────────────────

STATE_DIR="${GLOBUS_STATE_DIR:-/app/.state}"
SECRET_FILE="${STATE_DIR}/session_secret.hex"
mkdir -p "${STATE_DIR}"

if [ -n "${SESSION_SECRET:-}" ] && [ "${SESSION_SECRET}" != "replace-with-32-byte-hex" ]; then
    log "SESSION_SECRET supplied via env"
elif [ -f "${SECRET_FILE}" ]; then
    SESSION_SECRET="$(cat "${SECRET_FILE}")"
    export SESSION_SECRET
    log "SESSION_SECRET loaded from ${SECRET_FILE}"
else
    SESSION_SECRET="$(python3 -c 'import secrets; print(secrets.token_hex(32))')"
    export SESSION_SECRET
    printf '%s\n' "${SESSION_SECRET}" > "${SECRET_FILE}"
    chmod 0600 "${SECRET_FILE}"
    log "SESSION_SECRET generated + persisted to ${SECRET_FILE}"
    log "  (delete that file to rotate; will invalidate all sessions)"
fi

# ─────────────────────────────────────────────────────────────────────
# 4. Optionally seed an initial member from GLOBUS_FIRST_MEMBER_EMAIL.
#    Idempotent — uses INSERT IGNORE so a re-set of the env on a
#    later restart is safe.
# ─────────────────────────────────────────────────────────────────────

if [ -n "${GLOBUS_FIRST_MEMBER_EMAIL:-}" ]; then
    log "seeding first member: ${GLOBUS_FIRST_MEMBER_EMAIL}"
    mysql -h"${DB_HOST}" -P"${DB_PORT}" \
        -u"${DB_USER}" -p"${DB_PASSWORD}" \
        "${DB_NAME}" -e \
        "INSERT IGNORE INTO members (email, status) VALUES \
         ('${GLOBUS_FIRST_MEMBER_EMAIL}', 'active');" 2>&1 \
        | grep -v 'Using a password' >&2 || true
fi

# ─────────────────────────────────────────────────────────────────────
# 5. Exec the server. tini is PID 1 (set as Dockerfile ENTRYPOINT);
#    `exec` here makes python3 the only child it has to reap.
# ─────────────────────────────────────────────────────────────────────

log "starting globus server on ${GLOBUS_HOST:-0.0.0.0}:${GLOBUS_PORT:-8090}"
cd /app
exec python3 server/globus_server.py
