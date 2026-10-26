#!/usr/bin/env bash
# Clean-room smoke test: fresh .env -> docker compose up -> write a memory -> search it -> OK.
# Usage: tests/smoke.sh            (keeps the stack up)
#        tests/smoke.sh --down     (removes containers AND volumes at the end)
# Uses its own compose project and ports (28765-28767), so it does not collide with another install.
set -euo pipefail
cd "$(dirname "$0")/.."
PROJECT="${SMOKE_PROJECT:-cerebro-smoke}"
PORT="${SMOKE_PORT:-28765}"
AUTH_HEADER="${AUTH_HEADER:-X-Merlin-Key}"   # contract with src/: auth header name
ENV_FILE=".env.smoke"

rand() { openssl rand -hex 24; }
KEY="$(rand)"
sed -e "s/^POSTGRES_PASSWORD=.*/POSTGRES_PASSWORD=$(rand)/" \
    -e "s/^ADMIN_API_KEY=.*/ADMIN_API_KEY=${KEY}/" \
    -e "s/^GATEWAY_PUBLISH=.*/GATEWAY_PUBLISH=127.0.0.1:${PORT}/" \
    -e "s/^MCP_PUBLISH=.*/MCP_PUBLISH=127.0.0.1:$((PORT+1))/" \
    -e "s/^MCP_SSE_PUBLISH=.*/MCP_SSE_PUBLISH=127.0.0.1:$((PORT+2))/" \
    .env.example > "$ENV_FILE"
cp "$ENV_FILE" .env   # compose services read env_file: .env

dc() { docker compose -p "$PROJECT" --env-file "$ENV_FILE" "$@"; }
cleanup() { if [[ "${1:-}" == "--down" ]]; then dc down -v --remove-orphans; rm -f "$ENV_FILE" .env; fi; }
trap 'echo "SMOKE: FAIL (line $LINENO)"; dc logs --tail 40 gateway || true' ERR

echo "== up (first run downloads images and the embedding model)"
dc up -d --build --wait gateway

BASE="http://127.0.0.1:${PORT}"
H=(-H "${AUTH_HEADER}: ${KEY}" -H "Content-Type: application/json")
MARK="smoke-$(date +%s)"

echo "== health";  curl -fsS "$BASE/health"; echo
echo "== write";   curl -fsS "${H[@]}" -X POST "$BASE/memory/add" \
  -d "{\"text\":\"The lighthouse keeper logs the tide every dawn (${MARK}).\",\"source_path\":\"memoria/admin/${MARK}\",\"category\":\"general\"}"; echo
echo "== search";  OUT="$(curl -fsS "${H[@]}" -X POST "$BASE/memory/search" \
  -d '{"query":"who records the tide in the morning?","top_k":5}')"
echo "$OUT" | head -c 600; echo
echo "$OUT" | grep -q "$MARK" && echo "SMOKE: OK (write -> semantic search found ${MARK})"
cleanup "${1:-}"
