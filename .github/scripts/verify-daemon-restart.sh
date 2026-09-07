#!/usr/bin/env bash
set -Eeuo pipefail

compose=(docker compose --env-file .env.docker.example -f docker-compose.yml)
case "${OPENCLI_DAEMON_GATE_INCLUDE_BUILD_OVERRIDE:-1}" in
  1)
    compose+=(-f docker-compose.build.yml)
    ;;
  0)
    ;;
  *)
    echo "OPENCLI_DAEMON_GATE_INCLUDE_BUILD_OVERRIDE must be 0 or 1." >&2
    exit 2
    ;;
esac
services=(api frontend agent-1)
sentinel="daemon-restart-${GITHUB_RUN_ID:-local}-${GITHUB_RUN_ATTEMPT:-0}"
local_password="ci-daemon-restart-password"
api_token="${API_AUTH_TOKEN:?API_AUTH_TOKEN is required}"
bootstrap_token="${BOOTSTRAP_ADMIN_TOKEN:?BOOTSTRAP_ADMIN_TOKEN is required}"

declare -A container_ids=()

diagnostics() {
  echo "Docker daemon restart gate failed. Service and volume diagnostics follow." >&2
  "${compose[@]}" ps >&2 || true
  for service in "${services[@]}"; do
    container_id="${container_ids[$service]:-}"
    if [[ -z "$container_id" ]]; then
      echo "$service: container id unavailable" >&2
      continue
    fi
    docker inspect --format \
      '{{.Name}} state={{.State.Status}} health={{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}} restart={{.HostConfig.RestartPolicy.Name}}' \
      "$container_id" >&2 || true
    docker inspect --format '{{range .Mounts}}{{println .Name "->" .Destination}}{{end}}' \
      "$container_id" >&2 || true
  done
}

on_exit() {
  status=$?
  if [[ $status -ne 0 ]]; then
    diagnostics
  fi
  exit "$status"
}
trap on_exit EXIT
stage() {
  echo "[daemon-gate] $1"
}

stage "resolve compose containers"

for service in "${services[@]}"; do
  container_ids[$service]="$("${compose[@]}" ps -q "$service")"
  [[ -n "${container_ids[$service]}" ]]
done
stage "read database sentinel"

api_id="${container_ids[api]}"
agent_id="${container_ids[agent-1]}"

# Database sentinel: retain the migrated revision and prove the SQLite file is
# still readable after the daemon returns. The adjacent marker proves the
# db_data named volume itself retained a new value without changing app schema.
db_revision_before="$(
  docker exec "$api_id" python -c \
    'import sqlite3; db=sqlite3.connect("/data/opencli_admin.db"); print(db.execute("SELECT version_num FROM alembic_version").fetchone()[0])'
)"
stage "initialize authentication sentinel"
docker exec "$api_id" python -c \
  'from pathlib import Path; import sys; Path("/data/.ci-daemon-restart-sentinel").write_text(sys.argv[1], encoding="utf-8")' \
  "$sentinel"

# Authentication sentinel: initialize the same durable password state used by
# the public installers, verify it once, and compare the file after restart.
printf '%s' "$local_password" | "${compose[@]}" exec -T api python -c \
  'import sys; from backend.security.local_auth import hash_password, initialize_password_hash; initialize_password_hash(hash_password(sys.stdin.read().strip()), "/data/local-admin-password.hash")'
stage "hash authentication sentinel"
auth_digest_before="$(
  docker exec "$api_id" python -c \
    'from hashlib import sha256; from pathlib import Path; state=Path("/data/local-admin-password.hash"); marker=Path("/data/local-admin-password.hash.initialized"); print(f"{sha256(state.read_bytes()).hexdigest()}:{sha256(marker.read_bytes()).hexdigest()}")'
)"
stage "verify local login"

login_token="$(
  curl --fail --silent --show-error \
    -H "Content-Type: application/json" \
    -H "X-API-Token: $api_token" \
    --data "{\"username\":\"admin\",\"password\":\"$local_password\"}" \
    http://localhost:8031/api/v1/auth/login |
    python -c 'import json,sys; print(json.load(sys.stdin)["data"]["access_token"])'
)"
stage "verify authenticated identity"
curl --fail --silent --show-error \
  -H "Authorization: Bearer $login_token" \
  -H "X-API-Token: $api_token" \
  http://localhost:8031/api/v1/auth/me >/dev/null
stage "inspect browser profile volume"

# Browser-profile sentinel: write through the running container into the
# agent_profile_1 named volume, then read the same marker after daemon restart.
agent_profile_dir="$(docker exec "$agent_id" printenv PROFILE_DIR)"
[[ "$agent_profile_dir" == /* && "$agent_profile_dir" != / ]]
profile_volume_name() {
  docker inspect --format '{{json .Mounts}}' "$agent_id" |
    python -c 'import json,sys; mounts=[m for m in json.load(sys.stdin) if m["Destination"] == sys.argv[1] and m["Type"] == "volume" and m["RW"]]; assert len(mounts) == 1, "Expected one writable persistent profile volume"; print(mounts[0]["Name"])' "$agent_profile_dir"
}
agent_profile_volume_before="$(profile_volume_name)"
[[ -n "$agent_profile_volume_before" ]]
stage "write browser profile sentinel"
docker exec "$agent_id" sh -c \
  'test -d "$PROFILE_DIR" && test -r "$PROFILE_DIR" && test -w "$PROFILE_DIR"'
docker exec "$agent_id" sh -c \
  'printf %s "$1" > "$PROFILE_DIR/.ci-daemon-restart-sentinel"' \
  sh "$sentinel"
stage "restart Docker daemon"

echo "Restarting the Docker daemon; no Compose recovery command will be run."
sudo systemctl restart docker

daemon_timeout="${OPENCLI_DAEMON_RETURN_TIMEOUT_SECONDS:-180}"
service_timeout="${OPENCLI_SERVICE_RECOVERY_TIMEOUT_SECONDS:-180}"
[[ "$daemon_timeout" =~ ^[1-9][0-9]*$ ]] || { echo "Invalid daemon return timeout: $daemon_timeout" >&2; exit 2; }
[[ "$service_timeout" =~ ^[1-9][0-9]*$ ]] || { echo "Invalid service recovery timeout: $service_timeout" >&2; exit 2; }
daemon_deadline=$((SECONDS + daemon_timeout))
until docker info >/dev/null 2>&1; do
  if (( SECONDS >= daemon_deadline )); then
    echo "Docker daemon did not return within $daemon_timeout seconds." >&2
    exit 1
  fi
  sleep 2
done

for service in "${services[@]}"; do
  container_id="${container_ids[$service]}"
  service_deadline=$((SECONDS + service_timeout))
  while true; do
    state="$(docker inspect --format '{{.State.Status}}' "$container_id" 2>/dev/null || true)"
    health="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$container_id" 2>/dev/null || true)"
    if [[ "$state" == "running" && "$health" == "healthy" ]]; then
      break
    fi
    if (( SECONDS >= service_deadline )); then
      echo "$service did not return healthy within $service_timeout seconds (state=$state health=$health)." >&2
      exit 1
    fi
    sleep 3
  done
done

curl --fail --silent --show-error http://localhost:3010/login >/dev/null
curl --fail --silent --show-error http://localhost:8031/health >/dev/null
curl --fail --silent --show-error \
  -H "Authorization: Bearer $bootstrap_token" \
  -H "X-API-Token: $api_token" \
  http://localhost:8031/api/v1/auth/me >/dev/null

db_revision_after="$(
  docker exec "$api_id" python -c \
    'import sqlite3; db=sqlite3.connect("/data/opencli_admin.db"); assert db.execute("PRAGMA quick_check").fetchone()[0] == "ok"; print(db.execute("SELECT version_num FROM alembic_version").fetchone()[0])'
)"
[[ "$db_revision_after" == "$db_revision_before" ]]
[[ "$(docker exec "$api_id" python -c 'from pathlib import Path; print(Path("/data/.ci-daemon-restart-sentinel").read_text(encoding="utf-8"))')" == "$sentinel" ]]

auth_digest_after="$(
  docker exec "$api_id" python -c \
    'from hashlib import sha256; from pathlib import Path; state=Path("/data/local-admin-password.hash"); marker=Path("/data/local-admin-password.hash.initialized"); print(f"{sha256(state.read_bytes()).hexdigest()}:{sha256(marker.read_bytes()).hexdigest()}")'
)"
[[ "$auth_digest_after" == "$auth_digest_before" ]]

login_token="$(
  curl --fail --silent --show-error \
    -H "Content-Type: application/json" \
    -H "X-API-Token: $api_token" \
    --data "{\"username\":\"admin\",\"password\":\"$local_password\"}" \
    http://localhost:8031/api/v1/auth/login |
    python -c 'import json,sys; print(json.load(sys.stdin)["data"]["access_token"])'
)"
curl --fail --silent --show-error \
  -H "Authorization: Bearer $login_token" \
  -H "X-API-Token: $api_token" \
  http://localhost:8031/api/v1/auth/me >/dev/null

[[ "$(docker exec "$agent_id" printenv PROFILE_DIR)" == "$agent_profile_dir" ]]
agent_profile_volume_after="$(profile_volume_name)"
[[ "$agent_profile_volume_after" == "$agent_profile_volume_before" ]]
docker exec "$agent_id" sh -c \
  'test -d "$PROFILE_DIR" && test -r "$PROFILE_DIR"'
[[ "$(docker exec "$agent_id" sh -c 'cat "$PROFILE_DIR/.ci-daemon-restart-sentinel"')" == "$sentinel" ]]

trap - EXIT
echo "Docker daemon restart gate passed: services recovered and database, authentication, and browser-profile sentinels persisted."
