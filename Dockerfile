# KELP chat server image (KEL-16, M2-F).
#
# One process, one writer, one replica. The image holds code and dependencies
# only: every secret arrives at run time through the environment (compose
# `env_file` or a systemd `EnvironmentFile`), and every mutable path lives under
# the `/data` volume. Build args carry the release identity that `/api/healthz`
# and `/api/readyz` report, so a probe proves which commit is running.
#
#   docker build --build-arg KELP_GIT_COMMIT=$(git rev-parse --short HEAD) -t kelp:<tag> .
#
# This is the only build path: `docker-compose.yml` has no `build:` section and
# the systemd unit runs `up --no-build`, so a deploy cannot silently rebuild and
# lose the tag it claims to run. Record the tag in `/opt/kelp/.env` afterwards
# (docs/DEPLOY.md §4).
#
# Runtime contract:
#   user/group  10001:10001 (kelp), no shell, no home outside /data
#   volume      /data  (SQLite, WAL, lock, workspace, artifacts, index, audit, usage)
#   port        8765  (plain HTTP; terminate TLS in a reverse proxy)
#   env         see docs/DEPLOY.md; no secret has a default or is baked in
#   health      /api/healthz via deploy/healthcheck.py, every 30s

ARG PYTHON_VERSION=3.12

# --- builder: resolve pyproject dependencies into a throwaway venv -----------
FROM python:${PYTHON_VERSION}-slim AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /build

# pyproject.toml is the single source of dependency truth; nothing is pinned a
# second time here. Wheels only: the runtime stage has no compiler.
COPY pyproject.toml ./
RUN python -m venv /opt/venv \
 && /opt/venv/bin/python -c "import subprocess, sys, tomllib; \
deps = tomllib.loads(open('pyproject.toml', encoding='utf-8').read())['project']['dependencies']; \
subprocess.check_call([sys.executable, '-m', 'pip', 'install', *deps])"

# --- runtime ----------------------------------------------------------------
FROM python:${PYTHON_VERSION}-slim AS runtime

ARG KELP_VERSION=0.0.0
ARG KELP_GIT_COMMIT=unknown

LABEL org.opencontainers.image.title="kelp-assistant" \
      org.opencontainers.image.description="Always-on private assistant: chat server, single replica" \
      org.opencontainers.image.version="${KELP_VERSION}" \
      org.opencontainers.image.revision="${KELP_GIT_COMMIT}" \
      org.opencontainers.image.source="https://github.com/Anshieee/kelp" \
      org.opencontainers.image.licenses="UNLICENSED"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    VIRTUAL_ENV=/opt/venv \
    PATH="/opt/venv/bin:${PATH}" \
    KELP_HOST=0.0.0.0 \
    KELP_PORT=8765 \
    KELP_DB_PATH=/data/chat.db \
    KELP_WORKSPACE_ROOT=/data/workspace \
    KELP_ARTIFACT_DIR=/data/workspace/var/artifacts \
    KELP_AUDIT_DIR=/data/audit \
    KELP_USAGE_DIR=/data/usage \
    KELP_LOG_LEVEL=info \
    KELP_VERSION="${KELP_VERSION}" \
    KELP_GIT_COMMIT="${KELP_GIT_COMMIT}"

# Non-root from the first writable moment. `/data` is created here so a fresh
# named volume inherits uid 10001 instead of root.
RUN groupadd --gid 10001 --system kelp \
 && useradd --uid 10001 --gid 10001 --system --no-create-home \
            --home-dir /data --shell /usr/sbin/nologin kelp \
 && install -d -o 10001 -g 10001 -m 0700 /data

WORKDIR /app

COPY --from=builder /opt/venv /opt/venv
COPY --chown=0:0 chat /app/chat
COPY --chown=0:0 kernel /app/kernel
COPY --chown=0:0 openshell /app/openshell
COPY --chown=0:0 skills /app/skills
COPY --chown=0:0 payments /app/payments
COPY --chown=0:0 deploy/healthcheck.py /app/deploy/healthcheck.py

USER 10001:10001

VOLUME ["/data"]
EXPOSE 8765

# Liveness only: unauthenticated, local, and cheap enough for a 30s loop.
# Readiness stays an operator check because it needs the owner token and a
# provider round-trip; see docs/DEPLOY.md.
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
    CMD ["python", "/app/deploy/healthcheck.py", "--live"]

CMD ["python", "-m", "chat.server"]
