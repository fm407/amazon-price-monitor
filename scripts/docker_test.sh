#!/usr/bin/env bash
# Docker verification: build, start, health, mock end-to-end scenario, restart persistence, clean stop.
# Uses a disposable container and volume. It never touches the production container or its data.
set -euo pipefail
cd "$(dirname "$0")/.."

IMAGE=amazon-price-monitor:test
NAME=apm-docker-test
VOLUME=apm-docker-test-data
PORT="${TEST_PORT:-18080}"
TOKEN=docker-test-token-0001-not-a-real-secret

cleanup() { docker rm -f "$NAME" >/dev/null 2>&1 || true; docker volume rm "$VOLUME" >/dev/null 2>&1 || true; }
ok() { echo "  ok  $1"; }
fail() { echo "FAIL $1"; docker logs --tail 30 "$NAME" 2>&1 || true; exit 1; }
check() { local label="$1"; shift; if "$@" >/dev/null 2>&1; then ok "$label"; else fail "$label"; fi; }
step() { printf '\n== %s\n' "$1"; }
wait_healthy() {
  for _ in $(seq 1 60); do
    [ "$(docker inspect -f '{{.State.Health.Status}}' "$NAME" 2>/dev/null)" = "healthy" ] && return 0
    sleep 1
  done
  echo "FAIL container did not become healthy"; docker logs --tail 40 "$NAME"; exit 1
}
trap cleanup EXIT
cleanup

step "Unit and integration tests inside a Linux container"
docker build -q --target test -t amazon-price-monitor:test-suite . >/dev/null
docker run --rm amazon-price-monitor:test-suite

step "Build the runtime image"
docker build -q -t "$IMAGE" . >/dev/null
echo "  ok  image built: $(docker image inspect -f '{{.Size}}' "$IMAGE" | awk '{printf "%.0f MB", $1/1000000}')"

step "Start the container (read-only root filesystem, no capabilities, named volume)"
docker run -d --name "$NAME" --init --read-only --tmpfs /tmp --cap-drop ALL --security-opt no-new-privileges:true \
  -p "127.0.0.1:${PORT}:8080" -v "${VOLUME}:/app/data" \
  -e PROVIDER=mock -e DEMO_MODE=false -e API_TOKEN="$TOKEN" -e SCHEDULER_TICK_SECONDS=2 "$IMAGE" >/dev/null
wait_healthy
ok "container healthy"
check "health endpoint responds on the published port" sh -c "curl -fsS http://127.0.0.1:${PORT}/api/v1/health | grep -q '\"status\":\"ok\"'"
check "process runs as non-root uid 10001" sh -c "[ \"\$(docker exec $NAME id -u)\" = 10001 ]"
check "data volume is writable by the application user" docker exec "$NAME" test -w /app/data
check "database initialised on the volume" docker exec "$NAME" test -f /app/data/monitor.db
check "root filesystem is read-only" docker exec "$NAME" sh -c '! touch /app/should-fail 2>/dev/null'
check "scheduler started" sh -c "docker logs $NAME 2>&1 | grep -q '\"event\": \"scheduler started\"'"
check "generated secrets are private (mode 600)" sh -c "[ \"\$(docker exec $NAME stat -c %a /app/data/secret-key)\" = 600 ]"

step "End-to-end scenario with the mock provider"
docker exec -i "$NAME" python - before < scripts/docker_scenario.py

step "Restart the container"
docker restart -t 20 "$NAME" >/dev/null
wait_healthy
docker exec -i "$NAME" python - after < scripts/docker_scenario.py

step "Recreate the container on the same volume (simulates an image upgrade)"
docker rm -f "$NAME" >/dev/null
docker run -d --name "$NAME" --init --read-only --tmpfs /tmp --cap-drop ALL --security-opt no-new-privileges:true \
  -p "127.0.0.1:${PORT}:8080" -v "${VOLUME}:/app/data" -e PROVIDER=mock -e API_TOKEN="$TOKEN" "$IMAGE" >/dev/null
wait_healthy
check "data survived container recreation" sh -c "curl -fsS -H 'Authorization: Bearer $TOKEN' http://127.0.0.1:${PORT}/api/v1/products | grep -q '\"latest_price\":\"580.00\"'"

step "Secrets and clean shutdown"
check "API token never appears in the logs" sh -c "! docker logs $NAME 2>&1 | grep -q '$TOKEN'"
started=$(date +%s)
docker stop -t 30 "$NAME" >/dev/null
elapsed=$(( $(date +%s) - started ))
code=$(docker inspect -f '{{.State.ExitCode}}' "$NAME")
# The server finishes its graceful shutdown, then re-raises SIGTERM, so 143 is the expected exit code.
check "graceful shutdown completed before exit (code ${code}, ${elapsed}s)" sh -c "docker logs $NAME 2>&1 | grep -q '\"event\": \"application shutdown complete\"'"
check "stopped by SIGTERM within the grace period, not killed" sh -c "[ $elapsed -lt 15 ] && { [ $code = 0 ] || [ $code = 143 ]; }"

printf '\nAll Docker checks passed.\n'
