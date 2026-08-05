# syntax=docker/dockerfile:1.7

FROM python:3.12-slim-bookworm

LABEL org.opencontainers.image.title="Mirae Asset Finance Agent Backend" \
      org.opencontainers.image.description="FastAPI transport layer for the finance product agent" \
      org.opencontainers.image.source="https://github.com/Hyunhoim/gaeng3" \
      org.opencontainers.image.base.name="docker.io/library/python:3.12-slim-bookworm"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PORT=8000

WORKDIR /app

ARG APP_UID=10001
ARG APP_GID=10001

RUN groupadd --gid "${APP_GID}" app \
    && useradd --uid "${APP_UID}" --gid "${APP_GID}" --create-home \
        --shell /usr/sbin/nologin app

# Install the local deterministic Agent core and HTTP dependencies in one resolver run.
# The core is deliberately installed from this repository, not from an unrelated PyPI package.
COPY finance_agent/packages/finance_agent_core /tmp/finance_agent_core
COPY finance_agent/requirements/constraints.txt /tmp/core-constraints.txt
COPY fastapi_backend/requirements.txt /tmp/backend-requirements.txt
RUN python -m pip install \
        --constraint /tmp/core-constraints.txt \
        --requirement /tmp/backend-requirements.txt \
        /tmp/finance_agent_core \
    && rm -rf \
        /tmp/finance_agent_core \
        /tmp/core-constraints.txt \
        /tmp/backend-requirements.txt

# Copy only runtime files. Source worktrees may use a private umask (0700/0600),
# so normalize image permissions explicitly for the host-UID runtime override.
# Local .env files, tests and deployment helpers never enter the image.
COPY --chown=app:app fastapi_backend/app /app/app
COPY --chown=app:app fastapi_backend/start.sh /app/start.sh
RUN find /app/app -type d -exec chmod 0755 {} + \
    && find /app/app -type f -exec chmod 0644 {} + \
    && chmod 0755 /app/start.sh

USER app

EXPOSE 8000

CMD ["./start.sh"]
