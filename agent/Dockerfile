ARG REGISTRY=
FROM ${REGISTRY}node:22-bookworm-slim AS browser-bridge-extension

ARG BBX_EXTENSION_REPOSITORY=https://github.com/koltyakov/browser-bridge.git
ARG BBX_EXTENSION_COMMIT=5d9ab7993abef13ace6a893b58b0a333f5fb2e61

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates git openssl zip \
    && rm -rf /var/lib/apt/lists/*

RUN git clone --depth 1 "${BBX_EXTENSION_REPOSITORY}" /src/browser-bridge \
    && cd /src/browser-bridge \
    && git fetch --depth 1 origin "${BBX_EXTENSION_COMMIT}" \
    && git checkout "${BBX_EXTENSION_COMMIT}" \
    && node scripts/package-extension.mjs

COPY agent/inject-extension-key.mjs /tmp/inject-extension-key.mjs
COPY agent/headless-auto-enable.js /tmp/headless-auto-enable.js
COPY agent/patch-browser-bridge-autostart.mjs /tmp/patch-browser-bridge-autostart.mjs
RUN openssl genrsa -out /tmp/browser-bridge-extension-key.pem 2048 \
    && openssl rsa -in /tmp/browser-bridge-extension-key.pem -pubout -outform DER \
       -out /tmp/browser-bridge-extension-public.der 2>/dev/null \
    && node /tmp/inject-extension-key.mjs \
       /src/browser-bridge/dist/browser-bridge-extension/manifest.json \
       /tmp/browser-bridge-extension-public.der \
       /src/browser-bridge/dist/browser-bridge-extension-id \
    && node /tmp/patch-browser-bridge-autostart.mjs \
       /src/browser-bridge/dist/browser-bridge-extension/packages/extension/src/background.js \
       /tmp/headless-auto-enable.js \
       /src/browser-bridge/dist/browser-bridge-extension/packages/extension/src/headless-auto-enable.js

FROM ${REGISTRY}python:3.13-slim

ARG INSTALL_CHROME=false
ARG BBX_VERSION=1.11.1
ARG BROWSER_ENGINE=chromium
ARG CLOAKBROWSER_VERSION=0.5.10

# Base system deps (always installed)
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl ca-certificates git procps unzip util-linux \
    && curl -fsSL https://deb.nodesource.com/setup_26.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && if ! command -v npm >/dev/null 2>&1; then \
        apt-get install -y --no-install-recommends npm; \
    fi \
    && rm -rf /var/lib/apt/lists/*
RUN mkdir -p /etc/chromium/policies/managed \
    && printf '%s\n' '{"PasswordManagerEnabled":false,"AutofillAddressEnabled":false,"AutofillCreditCardEnabled":false}' \
       > /etc/chromium/policies/managed/opencli-account-runtime.json \
    && chmod 0644 /etc/chromium/policies/managed/opencli-account-runtime.json

# ── Optional: Chromium + VNC + virtual display ────────────────────────────────
# Set INSTALL_CHROME=true at build time to embed Chrome in the image.
# Default: false — connect to Chrome running on the host machine instead.
# With Chrome:    ~1.2 GB image, self-contained (no host Chrome required)
# Without Chrome: ~200 MB image, host provides Chrome via OPENCLI_CDP_ENDPOINT
RUN if [ "$INSTALL_CHROME" = "true" ]; then \
    apt-get update && apt-get install -y --no-install-recommends \
        chromium xvfb fonts-noto-cjk x11vnc novnc websockify nginx gettext-base \
    && rm -rf /var/lib/apt/lists/*; \
fi
RUN if [ "$BROWSER_ENGINE" = "cloakbrowser" ]; then \
      mkdir -p /opt/cloakbrowser \
      && npm install --prefix /opt/cloakbrowser --omit=dev \
           "cloakbrowser@${CLOAKBROWSER_VERSION}" "playwright-core@^1.53.0" \
      && env -u CLOAKBROWSER_VERSION CLOAKBROWSER_CACHE_DIR=/opt/cloakbrowser/cache node --input-type=module -e \
           'import {ensureBinary} from "/opt/cloakbrowser/node_modules/cloakbrowser"; await ensureBinary();' \
      ; \
    elif [ "$BROWSER_ENGINE" != "chromium" ]; then \
      echo "Unsupported BROWSER_ENGINE: $BROWSER_ENGINE" >&2; exit 1; \
    fi

# Install opencli globally — available as 'opencli' on PATH
ARG OPENCLI_VERSION=1.8.7
ARG PI_VERSION=0.83.0
COPY scripts/patch-opencli.js /tmp/patch-opencli.js
RUN npm install -g @jackwener/opencli@${OPENCLI_VERSION} \
        @earendil-works/pi-coding-agent@${PI_VERSION} \
        @browserbridge/bbx@${BBX_VERSION} \
    && node /tmp/patch-opencli.js \
    && rm /tmp/patch-opencli.js \
    && rm -rf /root/.npm

# Python deps for agent_server only — intentionally minimal
RUN pip install --no-cache-dir \
    fastapi \
    "uvicorn[standard]" \
    httpx \
    pyyaml \
    websockets

# Default bundle is immutable image content. Browser profiles only store site
# state and can be mounted independently without changing capabilities.
COPY chrome/runtime-bundles/ /opt/browser-runtime-bundles/
COPY chrome/extension-src/manifest.json /opt/browser-runtime-bundles/opencli-default/1/extensions/opencli-browser-bridge/manifest.json
COPY chrome/extension-src/icons/ /opt/browser-runtime-bundles/opencli-default/1/extensions/opencli-browser-bridge/icons/
COPY chrome/extension-src/dist/background.js /opt/browser-runtime-bundles/opencli-default/1/extensions/opencli-browser-bridge/dist/background.js
COPY chrome/script-host/ /opt/browser-runtime-bundles/opencli-default/1/extensions/opencli-script-host/
COPY chrome/extension-src/manifest.json /opt/browser-runtime-bundles/opencli-default/2/extensions/opencli-browser-bridge/manifest.json
COPY chrome/extension-src/icons/ /opt/browser-runtime-bundles/opencli-default/2/extensions/opencli-browser-bridge/icons/
COPY chrome/extension-src/dist/background.js /opt/browser-runtime-bundles/opencli-default/2/extensions/opencli-browser-bridge/dist/background.js
COPY chrome/script-host/ /opt/browser-runtime-bundles/opencli-default/2/extensions/opencli-script-host/
RUN set -eux; \
    curl -fsSL -o /tmp/violentmonkey.zip https://github.com/violentmonkey/violentmonkey/releases/download/v2.48.0/Violentmonkey-mv3-v2.48.0.zip; \
    echo "583ac595bb698a926eadb6064431fce1108dc2f2adb966ed984738824d2d5a54  /tmp/violentmonkey.zip" | sha256sum -c -; \
    mkdir -p /opt/browser-runtime-bundles/opencli-default/2/extensions/violentmonkey; \
    unzip -q /tmp/violentmonkey.zip -d /opt/browser-runtime-bundles/opencli-default/2/extensions/violentmonkey; \
    curl -fsSL -o /tmp/violentmonkey-LICENSE https://raw.githubusercontent.com/violentmonkey/violentmonkey/v2.48.0/LICENSE; \
    echo "f996dce5391963ed6badd93ad9e2ce2f957b2ad4e587112ea63b1f26317dfb57  /tmp/violentmonkey-LICENSE" | sha256sum -c -; \
    install -m 0644 /tmp/violentmonkey-LICENSE /opt/browser-runtime-bundles/opencli-default/2/extensions/violentmonkey/LICENSE-MIT.txt; \
    printf '%s\n' https://github.com/violentmonkey/violentmonkey/tree/v2.48.0 > /opt/browser-runtime-bundles/opencli-default/2/extensions/violentmonkey/SOURCE_URL; \
    printf '%s\n' https://raw.githubusercontent.com/violentmonkey/violentmonkey/v2.48.0/LICENSE > /opt/browser-runtime-bundles/opencli-default/2/extensions/violentmonkey/LICENSE_SOURCE_URL; \
    rm /tmp/violentmonkey.zip /tmp/violentmonkey-LICENSE
COPY scripts/resolve-browser-runtime-bundle.mjs /usr/local/bin/resolve-browser-runtime-bundle.mjs
COPY scripts/ensure-script-host.mjs /usr/local/bin/ensure-script-host.mjs
COPY scripts/ensure-violentmonkey-userscripts-access.mjs /usr/local/bin/ensure-violentmonkey-userscripts-access.mjs
COPY --from=browser-bridge-extension /src/browser-bridge/dist/browser-bridge-extension /opt/browser-bridge-extension
COPY --from=browser-bridge-extension /src/browser-bridge/dist/browser-bridge-extension-id /etc/browser-bridge-extension-id
COPY chrome/nginx-cdp.conf.template /etc/nginx/conf.d/cdp.conf.template
COPY scripts/resolve-browser-executable.mjs /usr/local/bin/resolve-browser-executable.mjs
RUN chmod +x /usr/local/bin/resolve-browser-executable.mjs

# Agent server source plus the runtime adapters it imports at startup.
WORKDIR /app
COPY backend/agent_server.py ./backend/agent_server.py
COPY backend/browser_account_runtime.py ./backend/browser_account_runtime.py
COPY backend/agent_runtime_dispatch.py ./backend/agent_runtime_dispatch.py
COPY backend/agent_runtimes/ ./backend/agent_runtimes/
COPY backend/miniflow/ ./backend/miniflow/
COPY backend/security/ ./backend/security/
RUN touch ./backend/__init__.py

COPY agent/entrypoint.sh /entrypoint.sh
RUN sed -i 's/\r$//' /entrypoint.sh && chmod +x /entrypoint.sh

RUN useradd -m -u 1000 agent \
    && mkdir -p /home/agent/.config/chromium /opt/cloakbrowser/cache \
       /home/agent/.local/state/opencli-account-runtime /var/lib/opencli/account-runtime \
    && if [ "$INSTALL_CHROME" = "true" ]; then \
        mkdir -p /etc/nginx/conf.d /var/lib/nginx /var/log/nginx /run \
        && rm -f /etc/nginx/sites-enabled/default 2>/dev/null || true \
        && sed -i 's/user www-data;//' /etc/nginx/nginx.conf \
        && sed -i 's|pid /run/nginx.pid|pid /tmp/nginx.pid|' /etc/nginx/nginx.conf \
        && chown -R agent:agent /etc/nginx /var/lib/nginx /var/log/nginx /run; \
    fi \
    && chown -R agent:agent /home/agent /app /opt/cloakbrowser /var/lib/opencli

ENV PYTHONPATH=/app \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    # MiniFlow executes only workflows packaged under this allowlisted root.
    MINIFLOW_WORKFLOW_ROOT=/app/workflows \
    # Bridge daemon port (used when Chrome is embedded OR daemon runs on host)
    OPENCLI_DAEMON_PORT=19825 \
    # Browser Bridge native host transport stays loopback-only inside the VNC agent.
    BBX_TCP_PORT=19826 \
    BBX_TCP_HOST=127.0.0.1 \
    BBX_TCP_BIND_HOST=127.0.0.1 \
    # Baked at build time so agent_server knows whether Chrome is bundled.
    # true  → connect to container-local Chrome (localhost)
    # false → connect to host Chrome (host.docker.internal)
    AGENT_HAS_CHROME=${INSTALL_CHROME} \
    BROWSER_ENGINE=${BROWSER_ENGINE} \
    PROFILE_DIR=/home/agent/.config/chromium \
    RUNTIME_STATE_DIR=/home/agent/.local/state/opencli-account-runtime \
    CHROMIUM_POLICY_FILE=/etc/chromium/policies/managed/opencli-account-runtime.json \
    CLOAKBROWSER_CACHE_DIR=/opt/cloakbrowser/cache

USER agent

# Unified VNC Agent ports: noVNC, CDP proxy, Agent WS/HTTP, and legacy OpenCLI.
EXPOSE 6080 19222 19823 19825

ENTRYPOINT ["/entrypoint.sh"]
