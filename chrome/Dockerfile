ARG REGISTRY=
FROM ${REGISTRY}debian:bookworm-slim
ARG BROWSER_ENGINE=chromium
ARG CLOAKBROWSER_VERSION=0.5.10

# OPENCLI_VERSION: pin the opencli release; patch-opencli.js adds DAEMON_HOST/LISTEN env hooks
ARG OPENCLI_VERSION=1.8.7
COPY scripts/patch-opencli.js /tmp/patch-opencli.js
RUN apt-get update && apt-get install -y --no-install-recommends \
    chromium x11vnc xvfb novnc websockify procps nginx \
    fonts-noto-cjk gettext-base \
    curl ca-certificates unzip \
    && curl -fsSL https://deb.nodesource.com/setup_26.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && npm install -g @jackwener/opencli@${OPENCLI_VERSION} \
    && node /tmp/patch-opencli.js \
    && rm /tmp/patch-opencli.js \
    && rm -rf /root/.npm /var/lib/apt/lists/*
RUN mkdir -p /etc/chromium/policies/managed \
    && printf '%s\n' '{"PasswordManagerEnabled":false,"AutofillAddressEnabled":false,"AutofillCreditCardEnabled":false}' \
       > /etc/chromium/policies/managed/opencli-account-runtime.json \
    && chmod 0644 /etc/chromium/policies/managed/opencli-account-runtime.json
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

COPY chrome/nginx-cdp.conf.template /etc/nginx/conf.d/cdp.conf.template

RUN useradd -m -u 1000 chrome \
    && rm /etc/nginx/sites-enabled/default 2>/dev/null || true \
    && chown -R chrome:chrome /var/lib/nginx /var/log/nginx /run /etc/nginx/conf.d \
    && sed -i 's/user www-data;//' /etc/nginx/nginx.conf \
    && sed -i 's|pid /run/nginx.pid|pid /tmp/nginx.pid|' /etc/nginx/nginx.conf \
    && mkdir -p /tmp/.X11-unix /opt/cloakbrowser/cache \
       /home/chrome/.local/state/opencli-account-runtime /var/lib/opencli/account-runtime \
    && chown -R chrome:chrome /opt/cloakbrowser /var/lib/opencli \
    && chmod 1777 /tmp/.X11-unix \
    && ln -sf /usr/share/novnc/vnc_lite.html /usr/share/novnc/index.html

COPY chrome/entrypoint.sh /entrypoint.sh
RUN sed -i 's/\r$//' /entrypoint.sh && chmod +x /entrypoint.sh

# Default bundle is immutable image content. Deployments may mount another
# versioned directory read-only, but never place components in the Profile.
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
COPY scripts/resolve-browser-executable.mjs /usr/local/bin/resolve-browser-executable.mjs
RUN chmod +x /usr/local/bin/resolve-browser-executable.mjs

ENV BROWSER_ENGINE=${BROWSER_ENGINE} \
    PROFILE_DIR=/home/chrome/.config/chromium \
    RUNTIME_STATE_DIR=/home/chrome/.local/state/opencli-account-runtime \
    CHROMIUM_POLICY_FILE=/etc/chromium/policies/managed/opencli-account-runtime.json \
    CLOAKBROWSER_CACHE_DIR=/opt/cloakbrowser/cache
EXPOSE 6080 19222 19825

USER chrome
# Pre-create profile directory as chrome user so Docker initialises new
# named volumes with the correct ownership (avoids "Permission denied" on
# first run when a fresh volume is mounted over a root-owned path).
RUN mkdir -p /home/chrome/.config/chromium

ENTRYPOINT ["/entrypoint.sh"]
