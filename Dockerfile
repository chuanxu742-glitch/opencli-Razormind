# ── Stage 1: builder ──────────────────────────────────────────────────────────
ARG REGISTRY=
FROM ${REGISTRY}python:3.13-slim AS builder

WORKDIR /app

# Install build deps
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# Install Python deps into a prefix so we can copy them cleanly
COPY pyproject.toml .
RUN pip install --upgrade pip \
    && pip install --prefix=/install .

# ── Stage 2: runtime ──────────────────────────────────────────────────────────
ARG REGISTRY=
FROM ${REGISTRY}python:3.13-slim AS runtime

WORKDIR /app

# Runtime system deps (psycopg2 needs libpq, opencli needs Node.js 26+)
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq5 curl ca-certificates git \
    && curl -fsSL https://deb.nodesource.com/setup_26.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

# Install opencli globally — available as 'opencli' on PATH
ARG OPENCLI_VERSION=1.8.7
ARG IMAGE_TAG=latest
COPY scripts/patch-opencli.js /tmp/patch-opencli.js
ENV PATH="/opt/opencli-runtime/bin:${PATH}"
RUN test "${OPENCLI_VERSION}" = "1.8.7" \
    && npm uninstall -g --prefix /opt/opencli-runtime --ignore-scripts @jackwener/opencli \
    && test ! -e /opt/opencli-runtime/lib/node_modules/@jackwener/opencli \
    && npm install -g --prefix /opt/opencli-runtime --ignore-scripts --registry=https://registry.npmjs.org @jackwener/opencli@1.8.7 \
    && npm install -g --prefix /opt/opencli-runtime @larksuite/cli@1.0.91 \
    && node /tmp/patch-opencli.js /opt/opencli-runtime \
    && rm /tmp/patch-opencli.js \
    && rm -rf /root/.npm

# Copy installed packages from builder
COPY --from=builder /install /usr/local

# Copy application source
COPY backend/ ./backend/
COPY scripts/patch-opencli.js ./scripts/patch-opencli.js
COPY scripts/install-agent.sh ./scripts/install-agent.sh
COPY scripts/install-opencli-adapters.mjs ./scripts/install-opencli-adapters.mjs
COPY integrations/opencli/adapter-pack.json integrations/opencli/LICENSE.opencli ./integrations/opencli/
COPY integrations/opencli/amazon/ ./integrations/opencli/amazon/
COPY integrations/opencli/taobao/ ./integrations/opencli/taobao/
COPY integrations/opencli/coupang/ ./integrations/opencli/coupang/
COPY integrations/opencli/ebay/ ./integrations/opencli/ebay/
COPY alembic.ini .

# Entrypoint handles migrations
COPY entrypoint.sh /entrypoint.sh
RUN sed -i 's/\r$//' /entrypoint.sh && chmod +x /entrypoint.sh

# Non-root user for security; pre-create /data so the SQLite volume is writable
RUN useradd -m -u 1000 appuser && \
    mkdir -p /data && \
    chown -R appuser:appuser /app /data
USER appuser
ENV HOME=/home/appuser
RUN node /app/scripts/install-opencli-adapters.mjs \
    --source /app/integrations/opencli --prefix /opt/opencli-runtime --home /home/appuser

ENV PYTHONPATH=/app \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
# Bake the image tag so the system config API can serve it to clients.
ARG IMAGE_TAG=latest
ENV IMAGE_TAG=${IMAGE_TAG}

# Acceptance-only image: production runtime plus the independently pinned III
# engine and deterministic source-only CLI fixture. It is never the default
# Compose image.
FROM iiidev/iii:0.19.4@sha256:14ed48b463d8a2e0d3583512acf106b3514f406c5e9965a5854710ff936e1e86 AS iii-engine

FROM runtime AS non-bypass-acceptance

USER root
COPY --from=iii-engine /app/iii /opt/iii/iii
COPY tests/acceptance/fixtures/opencli-proof /opt/non-bypass/opencli-proof
COPY tests/acceptance/non_bypass_vertical.py ./tests/acceptance/non_bypass_vertical.py

COPY tests/acceptance/fixtures/opencli-proof.sha256 /opt/non-bypass/opencli-proof.sha256
RUN chmod 0555 /opt/iii/iii /opt/non-bypass/opencli-proof \
    && cd /opt/non-bypass && sha256sum -c opencli-proof.sha256 \
    && test "$(/opt/iii/iii --version)" = "0.19.4" \
    && chown -R appuser:appuser /opt/non-bypass
ENV III_CLI_PATH=/opt/iii/iii \
    OPENCLI_BIN=/opt/non-bypass/opencli-proof
USER appuser

EXPOSE 8000

ENTRYPOINT ["/entrypoint.sh"]
CMD ["uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "8000", "--access-log", "--log-level", "info"]
