# syntax=docker/dockerfile:1.7

# Default verified on 2026-08-12. Release CI passes the same immutable index
# explicitly and records the selected platform manifest in its provenance.
ARG PYTHON_BASE_IMAGE=docker.io/library/python@sha256:4766d8b510c428e595d74b9cc5bbb2fae8e26316fffb4adc89908d79aacd58a2
FROM ${PYTHON_BASE_IMAGE} AS dependency-builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /build

# Build into a detached prefix.  The source tree and test files stay in this
# disposable stage and therefore cannot be recovered from a runtime OCI layer.
COPY finance_agent/packages/finance_agent_core /build/finance_agent_core
COPY finance_agent/requirements/constraints.txt /build/core-constraints.txt
COPY fastapi_backend/requirements.txt /build/backend-requirements.txt
RUN python -m pip install \
        --no-compile \
        --prefix /install \
        --constraint /build/core-constraints.txt \
        --requirement /build/backend-requirements.txt \
        /build/finance_agent_core

FROM ${PYTHON_BASE_IMAGE} AS runtime

ARG PYTHON_BASE_IMAGE
ARG FINANCE_SOURCE_COMMIT=development

LABEL org.opencontainers.image.title="Mirae Asset Finance Agent Backend" \
      org.opencontainers.image.description="FastAPI transport layer for the finance product agent" \
      org.opencontainers.image.source="https://github.com/Hyunhoim/gaeng3" \
      org.opencontainers.image.revision="${FINANCE_SOURCE_COMMIT}" \
      org.opencontainers.image.base.name="${PYTHON_BASE_IMAGE}"

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

# Copy only the installed runtime prefix.  Never copy the builder source and
# remove it in a later layer: OCI whiteouts would leave those bytes recoverable.
COPY --from=dependency-builder /install/ /usr/local/
RUN python -m pip check

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
