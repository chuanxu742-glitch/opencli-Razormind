#!/bin/bash
set -e

HAVE_CHROME=false
if command -v chromium >/dev/null 2>&1; then HAVE_CHROME=true; fi
if [ "${AGENT_HAS_CHROME:-false}" = "true" ]; then HAVE_CHROME=true; fi

PROFILE_DIR="${PROFILE_DIR:-/home/agent/.config/chromium}"
RUNTIME_HOME="${RUNTIME_HOME:-/home/agent}"
RUNTIME_CACHE_DIR="${RUNTIME_CACHE_DIR:-$RUNTIME_HOME/.cache}"
RUNTIME_STATE_DIR="${RUNTIME_STATE_DIR:-$RUNTIME_HOME/.local/state/opencli-account-runtime}"
CHROMIUM_POLICY_FILE="${CHROMIUM_POLICY_FILE:-/etc/chromium/policies/managed/opencli-account-runtime.json}"
ACCOUNT_RUNTIME_ROOT="${ACCOUNT_RUNTIME_ROOT:-$RUNTIME_STATE_DIR/sessions}"
ACCOUNT_PROFILE_ROOT="${ACCOUNT_PROFILE_ROOT:-$RUNTIME_STATE_DIR/profiles}"
ACCOUNT_RUNTIME_STATE_ROOT="${ACCOUNT_RUNTIME_STATE_ROOT:-$RUNTIME_STATE_DIR/state}"
export ACCOUNT_RUNTIME_ROOT ACCOUNT_PROFILE_ROOT ACCOUNT_RUNTIME_STATE_ROOT
validate_runtime_path() {
  local path="$1"
  local label="$2"
  if [[ "$path" != /* || "$path" == "/" || -L "$path" ]]; then
    echo "[agent] $label must be an absolute, non-root, non-symlink path" >&2
    exit 1
  fi
  mkdir -p -- "$path"
  local canonical
  canonical="$(readlink -f -- "$path" 2>/dev/null)" || {
    echo "[agent] $label cannot be canonicalized" >&2
    exit 1
  }
  if [[ "$canonical" != "$path" ]]; then
    echo "[agent] $label has a symlinked parent" >&2
    exit 1
  fi
}
validate_runtime_path "$PROFILE_DIR" PROFILE_DIR
validate_runtime_path "$RUNTIME_HOME" RUNTIME_HOME
validate_runtime_path "$RUNTIME_CACHE_DIR" RUNTIME_CACHE_DIR
validate_runtime_path "$RUNTIME_STATE_DIR" RUNTIME_STATE_DIR
if [[ "$CHROMIUM_POLICY_FILE" != /* || -L "$CHROMIUM_POLICY_FILE" || ! -f "$CHROMIUM_POLICY_FILE" ]]; then
  echo "[agent] managed Chromium policy is unavailable" >&2
  exit 1
fi
export PROFILE_DIR RUNTIME_HOME RUNTIME_CACHE_DIR RUNTIME_STATE_DIR CHROMIUM_POLICY_FILE
export HOME="$RUNTIME_HOME"
export XDG_CONFIG_HOME="${XDG_CONFIG_HOME:-$RUNTIME_HOME/.config}"
export CLOAKBROWSER_CACHE_DIR="${CLOAKBROWSER_CACHE_DIR:-$RUNTIME_CACHE_DIR}"
BROWSER_RUNTIME_BUNDLE_ROOT="${BROWSER_RUNTIME_BUNDLE_ROOT:-/opt/browser-runtime-bundles}"
BROWSER_RUNTIME_BUNDLE_MANIFEST="${BROWSER_RUNTIME_BUNDLE_MANIFEST:-$BROWSER_RUNTIME_BUNDLE_ROOT/opencli-default/2/manifest.json}"
BROWSER_RUNTIME_BUNDLE_RESOLVER="${BROWSER_RUNTIME_BUNDLE_RESOLVER:-/usr/local/bin/resolve-browser-runtime-bundle.mjs}"
BUNDLE_EXTENSION_OUTPUT="$(node "$BROWSER_RUNTIME_BUNDLE_RESOLVER" "$BROWSER_RUNTIME_BUNDLE_MANIFEST" "$BROWSER_RUNTIME_BUNDLE_ROOT")"
BUNDLE_RUNTIME_REPORT="$(node "$BROWSER_RUNTIME_BUNDLE_RESOLVER" "$BROWSER_RUNTIME_BUNDLE_MANIFEST" "$BROWSER_RUNTIME_BUNDLE_ROOT" --report)"
BROWSER_RUNTIME_REPORT_FILE="${BROWSER_RUNTIME_REPORT_FILE:-/tmp/browser-runtime-report.json}"
CHROME_CDP_TEMPLATE_FILE="${CHROME_CDP_TEMPLATE_FILE:-/etc/nginx/conf.d/cdp.conf.template}"
CHROME_CDP_CONFIG_FILE="${CHROME_CDP_CONFIG_FILE:-/etc/nginx/conf.d/cdp.conf}"
BROWSER_BRIDGE_EXTENSION_ID_FILE="${BROWSER_BRIDGE_EXTENSION_ID_FILE:-/etc/browser-bridge-extension-id}"
read_manifest_component_version() {
  node -e 'const manifest=JSON.parse(require("fs").readFileSync(process.argv[1],"utf8")); const component=manifest.components.find((item)=>item.id===process.argv[2]); if(component) process.stdout.write(component.version);' "$BROWSER_RUNTIME_BUNDLE_MANIFEST" "$1"
}
SCRIPT_HOST_VERSION="$(read_manifest_component_version opencli-script-host)"
VIOLENTMONKEY_VERSION="$(read_manifest_component_version violentmonkey)"
BUNDLE_EXTENSION_DIRS=()
if [ -n "$BUNDLE_EXTENSION_OUTPUT" ]; then mapfile -t BUNDLE_EXTENSION_DIRS <<< "$BUNDLE_EXTENSION_OUTPUT"; fi
CHROME_EXTRA_FLAGS=(--disable-extensions)
if [ "${#BUNDLE_EXTENSION_DIRS[@]}" -gt 0 ]; then
  EXTENSION_DIRS="$(IFS=,; echo "${BUNDLE_EXTENSION_DIRS[*]}")"
  CHROME_EXTRA_FLAGS=("--disable-extensions-except=$EXTENSION_DIRS" "--load-extension=$EXTENSION_DIRS")
  echo "[agent] Runtime bundle loaded from $BROWSER_RUNTIME_BUNDLE_MANIFEST"
fi
NETWORK_MODE="$(node -e 'const policy=JSON.parse(process.argv[1]||"{\"mode\":\"direct\"}"); if(!["direct","restricted"].includes(policy.mode)) process.exit(1); process.stdout.write(policy.mode);' "${BROWSER_NETWORK_POLICY:-}")"
if [ "$NETWORK_MODE" = "restricted" ]; then CHROME_EXTRA_FLAGS+=("--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE localhost"); fi
STARTUP_PAGES=()
if [ -n "${BROWSER_STARTUP_PAGES:-}" ]; then
  STARTUP_PAGE_OUTPUT="$(node -e 'const pages=JSON.parse(process.argv[1]); if(!Array.isArray(pages)||pages.length>10||pages.some((item)=>typeof item!=="string"||!/^https?:\/\//.test(item))) process.exit(1); process.stdout.write(pages.join("\n"));' "$BROWSER_STARTUP_PAGES")"
  if [ -n "$STARTUP_PAGE_OUTPUT" ]; then mapfile -t STARTUP_PAGES <<< "$STARTUP_PAGE_OUTPUT"; fi
fi
if [ "${#STARTUP_PAGES[@]}" -eq 0 ]; then STARTUP_PAGES=(https://www.doubao.com/chat); fi

verify_profile_ready() {
  if [[ "$CHROME_PROFILE" != /* || "$CHROME_PROFILE" == "/" || -L "$CHROME_PROFILE" ]]; then
    echo "[agent] active PROFILE_DIR must be absolute and non-symlink" >&2
    exit 1
  fi
  for lock_name in SingletonLock SingletonCookie SingletonSocket; do
    if [ -e "$CHROME_PROFILE/$lock_name" ]; then
      echo "[agent] profile has a singleton lock; refusing to steal it" >&2
      exit 1
    fi
  done
}
verify_chromium_policy() {
  node -e 'const p=JSON.parse(require("fs").readFileSync(process.argv[1],"utf8")); if(p.PasswordManagerEnabled!==false || (p.AutofillAddressEnabled!==undefined && p.AutofillAddressEnabled!==false) || (p.AutofillCreditCardEnabled!==undefined && p.AutofillCreditCardEnabled!==false)) process.exit(1);' "$CHROMIUM_POLICY_FILE"
}

ACCOUNT_RUNTIME_MANAGED=false
if [ "$HAVE_CHROME" = "true" ] \
  && [ "${OPENCLI_BROWSER_PROFILE_KIND:-authenticated}" = "authenticated" ] \
  && [ -n "${AGENT_NODE_ID:-}" ] \
  && [ -n "${AGENT_NODE_CREDENTIAL_ID:-}" ] \
  && [ -n "${AGENT_NODE_CREDENTIAL:-}" ]; then
  ACCOUNT_RUNTIME_MANAGED=true
fi

SHUTDOWN_REQUESTED=false
UVICORN_PID=
CHROME_SUPERVISOR_PID=
AUX_DIRECT_PIDS=()
AUX_GROUP_PIDS=()
PROCESS_GROUP_MODE=false
if command -v setsid >/dev/null 2>&1; then PROCESS_GROUP_MODE=true; fi

pid_alive() {
  local pid="${1:-}"
  [[ "$pid" =~ ^[0-9]+$ ]] && kill -0 "$pid" 2>/dev/null
}

group_alive() {
  local pid="${1:-}"
  [[ "$pid" =~ ^[0-9]+$ ]] && kill -0 -- "-$pid" 2>/dev/null
}

signal_pid() {
  local signal="$1"
  local pid="${2:-}"
  pid_alive "$pid" && kill "-$signal" "$pid" 2>/dev/null || true
}

signal_group() {
  local signal="$1"
  local pid="${2:-}"
  group_alive "$pid" && kill "-$signal" -- "-$pid" 2>/dev/null || true
}

signal_runtime_children() {
  local signal="$1"
  signal_pid "$signal" "$UVICORN_PID"
  signal_pid "$signal" "$CHROME_SUPERVISOR_PID"
  local pid
  for pid in "${AUX_GROUP_PIDS[@]}"; do signal_group "$signal" "$pid"; done
  for pid in "${AUX_DIRECT_PIDS[@]}"; do signal_pid "$signal" "$pid"; done
}

runtime_children_alive() {
  if pid_alive "$UVICORN_PID" || pid_alive "$CHROME_SUPERVISOR_PID"; then
    return 0
  fi
  local pid
  for pid in "${AUX_GROUP_PIDS[@]}"; do group_alive "$pid" && return 0; done
  for pid in "${AUX_DIRECT_PIDS[@]}"; do pid_alive "$pid" && return 0; done
  return 1
}

force_stop_runtime_children() {
  local pid
  signal_runtime_children KILL
  for pid in "${AUX_GROUP_PIDS[@]}"; do
    group_alive "$pid" && kill -KILL -- "-$pid" 2>/dev/null || true
  done
}

request_shutdown() {
  [ "$SHUTDOWN_REQUESTED" = true ] && return 0
  SHUTDOWN_REQUESTED=true
  local signal="${1:-TERM}"
  case "$signal" in TERM|INT|HUP) ;; *) signal=TERM ;; esac
  echo "[agent] Shutdown requested ($signal); stopping supervised children" >&2
  signal_runtime_children "$signal"
  for _ in $(seq 1 40); do
    runtime_children_alive || return 0
    sleep 0.1
  done
  echo "[agent] Supervised children did not exit within 4s; forcing shutdown" >&2
  force_stop_runtime_children
}

reap_pid() {
  local pid="${1:-}"
  [[ "$pid" =~ ^[0-9]+$ ]] || return 0
  wait "$pid" 2>/dev/null || true
}

trap 'request_shutdown TERM' TERM
trap 'request_shutdown INT' INT
trap 'request_shutdown HUP' HUP
trap 'request_shutdown TERM' EXIT

start_aux_process() {
  if [ "$PROCESS_GROUP_MODE" = true ]; then
    setsid "$@" &
    AUX_GROUP_PIDS+=("$!")
  else
    "$@" &
    AUX_DIRECT_PIDS+=("$!")
  fi
}

start_aux_loop() {
  local body="$1"
  if [ "$PROCESS_GROUP_MODE" = true ]; then
    setsid bash -c "$body" &
    AUX_GROUP_PIDS+=("$!")
  else
    bash -c "$body" &
    AUX_DIRECT_PIDS+=("$!")
  fi
}

if [ "$ACCOUNT_RUNTIME_MANAGED" = "true" ]; then
  [ -n "${BROWSER_RUNTIME_BUNDLE_ID:-}" ] || {
    echo "[agent] BROWSER_RUNTIME_BUNDLE_ID is required for account runtime" >&2
    exit 1
  }
  for command_name in Xvfb bbx bbx-daemon node; do
    command -v "$command_name" >/dev/null 2>&1 || {
      echo "[agent] account runtime prerequisite unavailable: $command_name" >&2
      exit 1
    }
  done
  ACCOUNT_RUNTIME_BBX_EXTENSION_ID_FILE="${ACCOUNT_RUNTIME_BBX_EXTENSION_ID_FILE:-/etc/browser-bridge-extension-id}"
  [ -s "$ACCOUNT_RUNTIME_BBX_EXTENSION_ID_FILE" ] || {
    echo "[agent] Browser Bridge extension identity is unavailable" >&2
    exit 1
  }
  export ACCOUNT_RUNTIME_BBX_EXTENSION_ID_FILE
  BROWSER_ENGINE="${BROWSER_ENGINE-chromium}"
  ACCOUNT_RUNTIME_BROWSER_BIN="$(node /usr/local/bin/resolve-browser-executable.mjs "$BROWSER_ENGINE")" || {
    echo "[agent] account runtime browser binary is unavailable" >&2
    exit 1
  }
  ACCOUNT_RUNTIME_OPENCLI_DAEMON_JS="$(npm root -g)/@jackwener/opencli/dist/src/daemon.js"
  [ -f "$ACCOUNT_RUNTIME_OPENCLI_DAEMON_JS" ] || {
    echo "[agent] account runtime OpenCLI daemon is unavailable" >&2
    exit 1
  }
  mkdir -p "$ACCOUNT_RUNTIME_ROOT" "$ACCOUNT_PROFILE_ROOT" "$ACCOUNT_RUNTIME_STATE_ROOT"
  chmod 700 "$ACCOUNT_RUNTIME_ROOT" "$ACCOUNT_PROFILE_ROOT" "$ACCOUNT_RUNTIME_STATE_ROOT"
  export ACCOUNT_RUNTIME_BROWSER_BIN ACCOUNT_RUNTIME_OPENCLI_DAEMON_JS
  verify_chromium_policy
  echo "[agent] Account allocator enabled; no shared :99/profile stack will start"
elif [ "$HAVE_CHROME" = "true" ]; then
  export OPENCLI_CDP_ENDPOINT="http://localhost:9222"
  echo "[agent] Chrome detected — starting embedded browser stack"
  CHROME_PROFILE="$PROFILE_DIR"
  if [ "${OPENCLI_BROWSER_PROFILE_KIND:-authenticated}" = "anonymous" ]; then
    CHROME_PROFILE="$(mktemp -d /tmp/opencli-anonymous-profile.XXXXXX)"
    # Keep every browser/runtime consumer on the same explicit profile path;
    # anonymous acquisition receives a fresh non-persistent profile and never
    # borrows the account volume.
    PROFILE_DIR="$CHROME_PROFILE"
    export PROFILE_DIR
    echo "[agent] Anonymous profile requested — using fresh $CHROME_PROFILE"
  fi
  mkdir -p "$CHROME_PROFILE"
  verify_profile_ready
  verify_chromium_policy
  rm -f /tmp/.X99-lock
  start_aux_process Xvfb :99 -screen 0 1280x900x24 -nolisten tcp
  export DISPLAY=:99
  sleep 1
  export CHROME_HOSTNAME="${CHROME_HOSTNAME:-${HOSTNAME:-agent-1}}"
  envsubst '${CHROME_HOSTNAME}' \
    < "$CHROME_CDP_TEMPLATE_FILE" \
    > "$CHROME_CDP_CONFIG_FILE"
  start_aux_process nginx -g 'daemon off;'
  start_aux_process x11vnc -display :99 -nopw -listen 0.0.0.0 -xkb -forever -shared
  start_aux_process websockify --web /usr/share/novnc 6080 localhost:5900

  BBX_EXTENSION_ID="$(tr -d '\r\n' < "$BROWSER_BRIDGE_EXTENSION_ID_FILE")"
  if [ -n "$BBX_EXTENSION_ID" ]; then
    bbx install "$BBX_EXTENSION_ID" --browser chromium \
      || echo "[agent] WARNING: Browser Bridge native host install failed"
  else
    echo "[agent] WARNING: Browser Bridge extension ID is missing"
  fi
  start_aux_loop 'while true; do
    bbx-daemon
    echo "[agent] BBX daemon exited, restarting in 1s..."
    sleep 1
  done'
  echo "[agent] BBX daemon started on ${BBX_TCP_HOST:-127.0.0.1}:${BBX_TCP_PORT:-19826}"


  DAEMON_JS="$(npm root -g)/@jackwener/opencli/dist/src/daemon.js"
  if [ -f "$DAEMON_JS" ]; then
    export DAEMON_JS
    start_aux_loop 'while true; do
      env -u OPENCLI_DAEMON_PORT OPENCLI_DAEMON_LISTEN=127.0.0.1 node "$DAEMON_JS"
      echo "[agent] Bridge daemon exited, restarting in 1s..."
      sleep 1
    done'
    echo "[agent] Bridge daemon started on 127.0.0.1:${OPENCLI_DAEMON_PORT:-19825}"
  else
    echo "[agent] WARNING: Bridge daemon not found at $DAEMON_JS"
  fi

  BROWSER_ENGINE="${BROWSER_ENGINE-chromium}"
  CHROME_BIN="$(node /usr/local/bin/resolve-browser-executable.mjs "$BROWSER_ENGINE")" || {
    echo "[agent] Browser engine resolution failed for $BROWSER_ENGINE" >&2
    exit 1
  }
  echo "[agent] Browser engine: $BROWSER_ENGINE"
  CHROME_SESSION_MODE="$PROCESS_GROUP_MODE"
  start_chrome() {
    verify_profile_ready
    if [ "$CHROME_SESSION_MODE" = "true" ]; then
      exec setsid "$CHROME_BIN" --remote-debugging-port=9222 --remote-debugging-address=127.0.0.1 --remote-allow-origins='*' --no-sandbox --disable-dev-shm-usage --no-first-run --no-default-browser-check --disable-session-crashed-bubble --disable-save-password-bubble --user-data-dir="$CHROME_PROFILE" --profile-directory=Default "${CHROME_EXTRA_FLAGS[@]}" --window-size=1280,900 "$@"
    else
      exec "$CHROME_BIN" --remote-debugging-port=9222 --remote-debugging-address=127.0.0.1 --remote-allow-origins='*' --no-sandbox --disable-dev-shm-usage --no-first-run --no-default-browser-check --disable-session-crashed-bubble --disable-save-password-bubble --user-data-dir="$CHROME_PROFILE" --profile-directory=Default "${CHROME_EXTRA_FLAGS[@]}" --window-size=1280,900 "$@"
    fi
  }
  stop_chrome_tree() {
    local pid="${1:-}"
    [[ "$pid" =~ ^[0-9]+$ ]] || return 0
    if [ "$CHROME_SESSION_MODE" = "true" ]; then
      signal_group TERM "$pid"
      for _ in $(seq 1 40); do
        group_alive "$pid" || break
        sleep 0.1
      done
      signal_group KILL "$pid"
    else
      signal_pid TERM "$pid"
      sleep 1
      signal_pid KILL "$pid"
    fi
  }
  stop_runtime_check() {
    local pid="${1:-}"
    [[ "$pid" =~ ^[0-9]+$ ]] || return 0
    signal_pid TERM "$pid"
    for _ in $(seq 1 20); do
      pid_alive "$pid" || break
      sleep 0.1
    done
    signal_pid KILL "$pid"
  }


  run_runtime_self_check() {
    for _ in $(seq 1 30); do
      if curl -sf http://localhost:9222/json/version >/dev/null 2>&1; then
        EXTENSION_WORKERS="$(curl -sf http://localhost:9222/json/list | node -e 'let data=""; process.stdin.on("data",(chunk)=>data+=chunk); process.stdin.on("end",()=>{const targets=JSON.parse(data); process.stdout.write(String(targets.filter((target)=>target.type==="service_worker"&&target.url.startsWith("chrome-extension://")).length));});')"
        if [ "$EXTENSION_WORKERS" -ge "${#BUNDLE_EXTENSION_DIRS[@]}" ] && { [ -z "$SCRIPT_HOST_VERSION" ] || node /usr/local/bin/ensure-script-host.mjs http://localhost:9222 >/dev/null; }; then
          READY_RUNTIME_REPORT="$BUNDLE_RUNTIME_REPORT"
          if [ -n "$VIOLENTMONKEY_VERSION" ]; then
            USER_SCRIPTS_ACCESS_CHECK="$(node /usr/local/bin/ensure-violentmonkey-userscripts-access.mjs http://localhost:9222 "$VIOLENTMONKEY_VERSION")" || { sleep 1; continue; }
            READY_RUNTIME_REPORT="$(node -e 'const report=JSON.parse(process.argv[1]); const check=JSON.parse(process.argv[2]); report.self_check={...report.self_check,violentmonkey_user_scripts_access:check}; process.stdout.write(JSON.stringify(report));' "$BUNDLE_RUNTIME_REPORT" "$USER_SCRIPTS_ACCESS_CHECK")"
          fi
          printf '%s\n' "$READY_RUNTIME_REPORT" > "$BROWSER_RUNTIME_REPORT_FILE"
          return 0
        fi
      fi
      sleep 1
    done
    echo "[agent] Chrome did not become ready; runtime report not written" >&2
    return 1
  }
  (
    trap 'stop_runtime_check "${CHECK_PID:-}"; stop_chrome_tree "${CHROME_PID:-}"; exit 143' TERM INT
    trap 'stop_runtime_check "${CHECK_PID:-}"; stop_chrome_tree "${CHROME_PID:-}"' EXIT
    while true; do
      rm -f "$BROWSER_RUNTIME_REPORT_FILE"
      stop_chrome_tree "${CHROME_PID:-}"
      start_chrome "${STARTUP_PAGES[@]}" &
      CHROME_PID=$!
      run_runtime_self_check &
      CHECK_PID=$!
      wait "$CHROME_PID" || true
      kill "$CHECK_PID" 2>/dev/null || true
      wait "$CHECK_PID" 2>/dev/null || true
      rm -f "$BROWSER_RUNTIME_REPORT_FILE"
      echo "[agent] Chrome exited, restarting in 2s..."
      sleep 2
    done
  ) &
  CHROME_SUPERVISOR_PID=$!

  echo "[agent] Waiting for Chrome runtime report..."
  for _ in $(seq 1 30); do
    [ -f "$BROWSER_RUNTIME_REPORT_FILE" ] && break
    sleep 1
  done
  if [ ! -f "$BROWSER_RUNTIME_REPORT_FILE" ]; then
    echo "[agent] Chrome did not become ready; runtime report not written" >&2
  fi
else
  echo "[agent] No embedded Chrome — connecting to host Chrome via OPENCLI_CDP_ENDPOINT / OPENCLI_DAEMON_HOST"
  echo "[agent]   CDP endpoint : ${OPENCLI_CDP_ENDPOINT:-<not set>}"
  echo "[agent]   Bridge daemon: ${OPENCLI_DAEMON_HOST:-<not set>}:${OPENCLI_DAEMON_PORT:-19825}"
fi

if [ "$SHUTDOWN_REQUESTED" = true ]; then
  reap_pid "$CHROME_SUPERVISOR_PID"
  exit 143
fi

uvicorn backend.agent_server:app --host 0.0.0.0 --port "${AGENT_PORT:-19823}" --log-level info &
UVICORN_PID=$!
UVICORN_STATUS=0
while :; do
  if wait "$UVICORN_PID"; then
    UVICORN_STATUS=$?
    break
  fi
  UVICORN_STATUS=$?
  if [ "$SHUTDOWN_REQUESTED" != true ] || ! pid_alive "$UVICORN_PID"; then
    break
  fi
done

if [ "$SHUTDOWN_REQUESTED" != true ]; then
  request_shutdown TERM
fi
reap_pid "$UVICORN_PID"
reap_pid "$CHROME_SUPERVISOR_PID"
for _pid in "${AUX_GROUP_PIDS[@]}"; do reap_pid "$_pid"; done
for _pid in "${AUX_DIRECT_PIDS[@]}"; do reap_pid "$_pid"; done
