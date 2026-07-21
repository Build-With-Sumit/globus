# Globus — single-stage Python image based on python:3.12-slim.
# Built for `docker compose up`. Runs as non-root user `globus` (uid 1000).
# Schema is applied by docker/entrypoint.sh on first boot (idempotent).
FROM python:3.12-slim AS app

# Build-time hint so the image carries the version we shipped from.
ARG GLOBUS_VERSION=0.15.0
LABEL org.opencontainers.image.title="globus"
LABEL org.opencontainers.image.version="${GLOBUS_VERSION}"
LABEL org.opencontainers.image.source="https://github.com/Build-With-Sumit/globus"
LABEL org.opencontainers.image.licenses="AGPL-3.0"

# tini is a small init that reaps zombies + propagates signals — the
# orchestrator + sync workers spawn child processes, and the default
# Python interpreter as PID 1 doesn't reap them.
# default-mysql-client gives the entrypoint a `mysql` binary for
# wait-for-db + schema apply on first boot.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        tini default-mysql-client ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Non-root user — keeps any escape blast radius minimal. /app + the
# state dirs below get chowned to this uid on copy.
ARG GLOBUS_UID=1000
ARG GLOBUS_GID=1000
RUN groupadd -g ${GLOBUS_GID} globus \
    && useradd -l -u ${GLOBUS_UID} -g globus -d /app -s /bin/bash globus

WORKDIR /app

# Install pip deps in their own layer so app-only changes don't bust
# the deps cache. requirements.txt is intentionally small (pymysql +
# cryptography); everything else is stdlib.
COPY requirements.txt requirements-optional.txt /app/
RUN pip install --no-cache-dir -r /app/requirements.txt

# Copy the rest of the app. .dockerignore keeps __pycache__, .git, .env
# out of the image.
COPY --chown=globus:globus server/   /app/server/
COPY --chown=globus:globus scripts/  /app/scripts/
COPY --chown=globus:globus schema/   /app/schema/
COPY --chown=globus:globus config/   /app/config/
COPY --chown=globus:globus public/   /app/public/
COPY --chown=globus:globus globus_truth/ /app/globus_truth/
COPY --chown=globus:globus docs/     /app/docs/
COPY --chown=globus:globus docker/entrypoint.sh /app/docker/entrypoint.sh
RUN sed -i 's/\r$//' /app/docker/entrypoint.sh /app/scripts/run_agent.py \
    && chmod 0755 /app/docker/entrypoint.sh /app/scripts/run_agent.py

# Persistent state lives in three volumes so re-creating the container
# (e.g. on image upgrade) keeps briefs, Drive-cached files, session state,
# and the Truth Layer SQLite database intact.
#   /var/lib/globus/agents   — agent_runner brief output dir
#   /var/lib/globus/raw-data — google_drive disk cache
#   /app/.state              — SESSION_SECRET + globus-truth.db
RUN mkdir -p /var/lib/globus/agents /var/lib/globus/raw-data /app/.state \
    && chown -R globus:globus /var/lib/globus /app/.state
VOLUME ["/var/lib/globus/agents", "/var/lib/globus/raw-data", "/app/.state"]

# Env defaults targeting the compose-network MySQL service (db:3306).
# Override any of these with `docker compose ... -e KEY=val` or .env.
ENV GLOBUS_HOST=0.0.0.0 \
    GLOBUS_PORT=8090 \
    DB_HOST=db \
    DB_PORT=3306 \
    DB_USER=globus \
    DB_NAME=globus \
    GLOBUS_AGENTS_WORK_DIR=/var/lib/globus/agents \
    GLOBUS_RAW_DATA_DIR=/var/lib/globus/raw-data \
    GLOBUS_STATE_DIR=/app/.state \
    GLOBUS_TRUTH_DB=/app/.state/globus-truth.db \
    PYTHONPATH=/app \
    PYTHONUNBUFFERED=1

EXPOSE 8090

USER globus

# tini reaps zombies; entrypoint.sh handles wait-for-db, schema apply,
# SESSION_SECRET bootstrap, then execs python3 server/globus_server.py.
ENTRYPOINT ["/usr/bin/tini", "--", "/app/docker/entrypoint.sh"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python3 -c "import urllib.request, sys; \
        r = urllib.request.urlopen('http://127.0.0.1:8090/api/health', timeout=3); \
        sys.exit(0 if r.status == 200 else 1)" || exit 1
