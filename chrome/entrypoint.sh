#!/bin/bash
set -e

PROFILE_DIR="${PROFILE_DIR:-/home/chrome/.config/chromium}"
RUNTIME_HOME="${RUNTIME_HOME:-/home/chrome}"
RUNTIME_CACHE_DIR="${RUNTIME_CACHE_DIR:-$RUNTIME_HOME/.cache}"
RUNTIME_STATE_DIR="${RUNTIME_STATE_DIR:-$RUNTIME_HOME/.local/state/opencli-account-runtime}"
CHROMIUM_POLICY_FILE="${CHROMIUM_POLICY_FILE:-/etc/chromium/policies/managed/opencli-account-runtime.json}"
validate_runtime_path() {
  local path="$1"
  local label="$2"
  if [[ "$path" != /* || "$path" == "/" || -L "$path" ]]; then
    echo "[entrypoint] $label must be an absolute, non-root, non-symlink path" >&2
    exit 1
  fi
  mkdir -p -- "$path"
  local canonical
  canonical="$(readlink -f -- "$path" 2>/dev/null)" || {
    echo "[entrypoint] $label cannot be canonicalized" >&2
    exit 1
  }
  if [[ "$canonical" != "$path" ]]; then
    echo "[entrypoint] $label has a symlinked parent" >&2
    exit 1
  fi
}
validate_runtime_path "$PROFILE_DIR" PROFILE_DIR
validate_runtime_path "$RUNTIME_HOME" RUNTIME_HOME
validate_runtime_path "$RUNTIME_CACHE_DIR" RUNTIME_CACHE_DIR
validate_runtime_path "$RUNTIME_STATE_DIR" RUNTIME_STATE_DIR
if [[ "$CHROMIUM_POLICY_FILE" != /* || -L "$CHROMIUM_POLICY_FILE" || ! -f "$CHROMIUM_POLICY_FILE" ]]; then
  echo "[entrypoint] managed Chromium policy is unavailable" >&2
  exit 1
fi
export PROFILE_DIR RUNTIME_HOME RUNTIME_CACHE_DIR RUNTIME_STATE_DIR CHROMIUM_POLICY_FILE
export HOME="$RUNTIME_HOME"
export XDG_CONFIG_HOME="${XDG_CONFIG_HOME:-$RUNTIME_HOME/.config}"
export CLOAKBROWSER_CACHE_DIR="${CLOAKBROWSER_CACHE_DIR:-$RUNTIME_CACHE_DIR}"

verify_profile_ready() {
  if [[ "$PROFILE_DIR" != /* || "$PROFILE_DIR" == "/" || -L "$PROFILE_DIR" ]]; then
    echo "[entrypoint] active PROFILE_DIR must be absolute and non-symlink" >&2
    exit 1
  fi
  for lock_name in SingletonLock SingletonCookie SingletonSocket; do
    if [ -e "$PROFILE_DIR/$lock_name" ]; then
      echo "[entrypoint] profile has a singleton lock; refusing to steal it" >&2
      exit 1
    fi
  done
}
verify_chromium_policy() {
  node -e 'const p=JSON.parse(require("fs").readFileSync(process.argv[1],"utf8")); if(p.PasswordManagerEnabled!==false || (p.AutofillAddressEnabled!==undefined && p.AutofillAddressEnabled!==false) || (p.AutofillCreditCardEnabled!==undefined && p.AutofillCreditCardEnabled!==false)) process.exit(1);' "$CHROMIUM_POLICY_FILE"
}

verify_profile_ready
verify_chromium_policy
RUNTIME_DISPLAY="${DISPLAY:-:99}"
if [[ ! "$RUNTIME_DISPLAY" =~ ^:[0-9]+$ ]]; then
  echo "[entrypoint] DISPLAY must be a numeric X display" >&2
  exit 1
fi
DISPLAY_NUMBER="${RUNTIME_DISPLAY#:}"
if [ -e "/tmp/.X${DISPLAY_NUMBER}-lock" ] || [ -e "/tmp/.X11-unix/X${DISPLAY_NUMBER}" ]; then
  echo "[entrypoint] requested X display is already owned" >&2
  exit 1
fi
Xvfb "$RUNTIME_DISPLAY" -screen 0 1280x900x24 -nolisten tcp &
export DISPLAY="$RUNTIME_DISPLAY"
sleep 1

export CHROME_HOSTNAME="${CHROME_HOSTNAME:-${HOSTNAME:-chrome}}"
envsubst '${CHROME_HOSTNAME}' < /etc/nginx/conf.d/cdp.conf.template > /etc/nginx/conf.d/cdp.conf
nginx -g 'daemon off;' &
x11vnc -display :99 -nopw -listen 0.0.0.0 -xkb -forever -shared &
websockify --web /usr/share/novnc 6080 localhost:5900 &

DAEMON_JS="$(npm root -g)/@jackwener/opencli/dist/src/daemon.js"
if [ -f "$DAEMON_JS" ]; then
  (while true; do
    OPENCLI_DAEMON_LISTEN=0.0.0.0 node "$DAEMON_JS"
    echo "[entrypoint] Browser Bridge daemon exited, restarting in 1s..."
    sleep 1
  done) &
  echo "[entrypoint] Browser Bridge daemon started on 0.0.0.0:${OPENCLI_DAEMON_PORT:-19825}"
else
  echo "[entrypoint] WARNING: Browser Bridge daemon not found at $DAEMON_JS"
fi

# The profile is writable user/site state only. Every capability comes from a
# read-only, versioned runtime bundle and Chromium is given exactly the
# allowlisted extension directories below.
BROWSER_RUNTIME_BUNDLE_ROOT="${BROWSER_RUNTIME_BUNDLE_ROOT:-/opt/browser-runtime-bundles}"
BROWSER_RUNTIME_BUNDLE_MANIFEST="${BROWSER_RUNTIME_BUNDLE_MANIFEST:-$BROWSER_RUNTIME_BUNDLE_ROOT/opencli-default/2/manifest.json}"
BUNDLE_EXTENSION_OUTPUT="$(node /usr/local/bin/resolve-browser-runtime-bundle.mjs "$BROWSER_RUNTIME_BUNDLE_MANIFEST" "$BROWSER_RUNTIME_BUNDLE_ROOT")"
BUNDLE_RUNTIME_REPORT="$(node /usr/local/bin/resolve-browser-runtime-bundle.mjs "$BROWSER_RUNTIME_BUNDLE_MANIFEST" "$BROWSER_RUNTIME_BUNDLE_ROOT" --report)"
read_manifest_component_version() {
  node -e 'const manifest=JSON.parse(require("fs").readFileSync(process.argv[1],"utf8")); const component=manifest.components.find((item)=>item.id===process.argv[2]); if(component) process.stdout.write(component.version);' "$BROWSER_RUNTIME_BUNDLE_MANIFEST" "$1"
}
SCRIPT_HOST_VERSION="$(read_manifest_component_version opencli-script-host)"
VIOLENTMONKEY_VERSION="$(read_manifest_component_version violentmonkey)"

BUNDLE_EXTENSION_DIRS=()
if [ -n "$BUNDLE_EXTENSION_OUTPUT" ]; then
  mapfile -t BUNDLE_EXTENSION_DIRS <<< "$BUNDLE_EXTENSION_OUTPUT"
fi
CHROME_EXTRA_FLAGS=(--disable-extensions)
if [ "${#BUNDLE_EXTENSION_DIRS[@]}" -gt 0 ]; then
  EXTENSION_DIRS="$(IFS=,; echo "${BUNDLE_EXTENSION_DIRS[*]}")"
  CHROME_EXTRA_FLAGS=("--disable-extensions-except=$EXTENSION_DIRS" "--load-extension=$EXTENSION_DIRS")
  echo "[entrypoint] Runtime bundle loaded from $BROWSER_RUNTIME_BUNDLE_MANIFEST"
fi
NETWORK_MODE="$(node -e 'const policy=JSON.parse(process.argv[1]||"{\"mode\":\"direct\"}"); if(!["direct","restricted"].includes(policy.mode)) process.exit(1); process.stdout.write(policy.mode);' "${BROWSER_NETWORK_POLICY:-}")"
if [ "$NETWORK_MODE" = "restricted" ]; then
  CHROME_EXTRA_FLAGS+=("--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE localhost")
fi
STARTUP_PAGES=()
if [ -n "${BROWSER_STARTUP_PAGES:-}" ]; then
  STARTUP_PAGE_OUTPUT="$(node -e 'const pages=JSON.parse(process.argv[1]); if(!Array.isArray(pages)||pages.length>10||pages.some((item)=>typeof item!=="string"||!/^https?:\/\//.test(item))) process.exit(1); process.stdout.write(pages.join("\n"));' "$BROWSER_STARTUP_PAGES")"
  if [ -n "$STARTUP_PAGE_OUTPUT" ]; then mapfile -t STARTUP_PAGES <<< "$STARTUP_PAGE_OUTPUT"; fi
fi

BROWSER_ENGINE="${BROWSER_ENGINE-chromium}"
CHROME_BIN="$(node /usr/local/bin/resolve-browser-executable.mjs "$BROWSER_ENGINE")" || {
  echo "[entrypoint] Browser engine resolution failed for $BROWSER_ENGINE" >&2
  exit 1
}
CDP_PORT="${OPENCLI_CDP_PORT:-9222}"
if [[ ! "$CDP_PORT" =~ ^[0-9]+$ ]] || [ "$CDP_PORT" -lt 1024 ] || [ "$CDP_PORT" -gt 65535 ]; then
  echo "[entrypoint] OPENCLI_CDP_PORT is invalid" >&2
  exit 1
fi
echo "[entrypoint] Browser engine: $BROWSER_ENGINE"
CHROME_SESSION_MODE=false
if command -v setsid >/dev/null 2>&1; then CHROME_SESSION_MODE=true; fi
start_chrome() {
  verify_profile_ready
  if [ "$CHROME_SESSION_MODE" = "true" ]; then
    exec setsid "$CHROME_BIN" --remote-debugging-port="$CDP_PORT" --remote-debugging-address=127.0.0.1 --remote-allow-origins='*' --no-sandbox --disable-dev-shm-usage --disable-save-password-bubble --user-data-dir="$PROFILE_DIR" --window-size=1280,900 "${CHROME_EXTRA_FLAGS[@]}" "$@"
  else
    exec "$CHROME_BIN" --remote-debugging-port="$CDP_PORT" --remote-debugging-address=127.0.0.1 --remote-allow-origins='*' --no-sandbox --disable-dev-shm-usage --disable-save-password-bubble --user-data-dir="$PROFILE_DIR" --window-size=1280,900 "${CHROME_EXTRA_FLAGS[@]}" "$@"
  fi
}
stop_chrome_tree() {
  local pid="${1:-}"
  [[ "$pid" =~ ^[0-9]+$ ]] || return 0
  if [ "$CHROME_SESSION_MODE" = "true" ]; then
    kill -TERM -- "-$pid" 2>/dev/null || true
    for _ in $(seq 1 40); do
      kill -0 -- "-$pid" 2>/dev/null || break
      sleep 0.1
    done
    kill -KILL -- "-$pid" 2>/dev/null || true
  else
    kill -TERM "$pid" 2>/dev/null || true
    sleep 1
    kill -KILL "$pid" 2>/dev/null || true
  fi
}

run_runtime_self_check() {
  for _ in $(seq 1 30); do
    if curl -sf "http://127.0.0.1:${CDP_PORT}/json/version" >/dev/null 2>&1; then
      EXTENSION_WORKERS="$(curl -sf "http://127.0.0.1:${CDP_PORT}/json/list" | node -e 'let data=""; process.stdin.on("data",(chunk)=>data+=chunk); process.stdin.on("end",()=>{const targets=JSON.parse(data); process.stdout.write(String(targets.filter((target)=>target.type==="service_worker"&&target.url.startsWith("chrome-extension://")).length));});')"
      if [ "$EXTENSION_WORKERS" -ge "${#BUNDLE_EXTENSION_DIRS[@]}" ] && { [ -z "$SCRIPT_HOST_VERSION" ] || node /usr/local/bin/ensure-script-host.mjs "http://127.0.0.1:${CDP_PORT}" >/dev/null; }; then
        READY_RUNTIME_REPORT="$BUNDLE_RUNTIME_REPORT"
        if [ -n "$VIOLENTMONKEY_VERSION" ]; then
          USER_SCRIPTS_ACCESS_CHECK="$(node /usr/local/bin/ensure-violentmonkey-userscripts-access.mjs "http://127.0.0.1:${CDP_PORT}" "$VIOLENTMONKEY_VERSION")" || { sleep 1; continue; }
          READY_RUNTIME_REPORT="$(node -e 'const report=JSON.parse(process.argv[1]); const check=JSON.parse(process.argv[2]); report.self_check={...report.self_check,violentmonkey_user_scripts_access:check}; process.stdout.write(JSON.stringify(report));' "$BUNDLE_RUNTIME_REPORT" "$USER_SCRIPTS_ACCESS_CHECK")"
        fi
        printf '%s\n' "$READY_RUNTIME_REPORT" > /tmp/browser-runtime-report.json
        return 0
      fi
    fi
    sleep 1
  done
  echo "[entrypoint] Chromium did not become ready; runtime report not written" >&2
  return 1
}

trap 'stop_chrome_tree "${CHROME_PID:-}"; exit 143' TERM INT
trap 'stop_chrome_tree "${CHROME_PID:-}"' EXIT
while true; do
  # A report is only valid for the Chromium process that produced it.
  rm -f /tmp/browser-runtime-report.json
  stop_chrome_tree "${CHROME_PID:-}"
  start_chrome "${STARTUP_PAGES[@]}" &
  CHROME_PID=$!
  run_runtime_self_check &
  CHECK_PID=$!
  wait "$CHROME_PID" || true
  kill "$CHECK_PID" 2>/dev/null || true
  wait "$CHECK_PID" 2>/dev/null || true
  rm -f /tmp/browser-runtime-report.json
  stop_chrome_tree "$CHROME_PID"
  echo "[entrypoint] Chromium exited, restarting in 2s..."
  sleep 2
done
