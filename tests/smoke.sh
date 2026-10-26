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
KEY="$(rand)"; SKEY="$(rand)"; APW="$(rand)"; SPORT=$((PORT+3))
VDIR="$(mktemp -d)"; cp -r examples/vault/. "$VDIR/"   # throwaway vault: the smoke never writes into the repo
sed -e "s/^POSTGRES_PASSWORD=.*/POSTGRES_PASSWORD=$(rand)/" \
    -e "s/^ADMIN_API_KEY=.*/ADMIN_API_KEY=${KEY}/" \
    -e "s/^SINAPSIS_API_KEY=.*/SINAPSIS_API_KEY=${SKEY}/" \
    -e "s/^SINAPSIS_ADMIN_PASSWORD=.*/SINAPSIS_ADMIN_PASSWORD=${APW}/" \
    -e "s/^SINAPSIS_PUBLISH=.*/SINAPSIS_PUBLISH=127.0.0.1:${SPORT}/" \
    -e "s|^VAULT_DIR=.*|VAULT_DIR=${VDIR}|" \
    -e "s/^GATEWAY_PUBLISH=.*/GATEWAY_PUBLISH=127.0.0.1:${PORT}/" \
    -e "s/^MCP_PUBLISH=.*/MCP_PUBLISH=127.0.0.1:$((PORT+1))/" \
    -e "s/^MCP_SSE_PUBLISH=.*/MCP_SSE_PUBLISH=127.0.0.1:$((PORT+2))/" \
    .env.example > "$ENV_FILE"
cp "$ENV_FILE" .env   # compose services read env_file: .env
echo "SINAPSIS_ALLOWED_ORIGINS=http://127.0.0.1:${SPORT}" >> .env; echo "SINAPSIS_ALLOWED_ORIGINS=http://127.0.0.1:${SPORT}" >> "$ENV_FILE"
( umask 077; mkdir -p .secrets; printf '%s' "$SKEY" > .secrets/sinapsis_api_key )

dc() { docker compose -p "$PROJECT" --env-file "$ENV_FILE" "$@"; }
cleanup() { if [[ "${1:-}" == "--down" ]]; then dc down -v --remove-orphans; rm -rf "$ENV_FILE" .env .secrets "$VDIR"; fi; }
trap 'echo "SMOKE: FAIL (line $LINENO)"; dc logs --tail 40 gateway sinapsis || true; cleanup "${1:-}"' ERR

# A previous interrupted run of THIS throwaway project would keep its old database password.
dc down -v --remove-orphans >/dev/null 2>&1 || true

echo "== up (first run downloads images and the embedding model)"
dc up -d --build --wait gateway sinapsis

BASE="http://127.0.0.1:${PORT}"
H=(-H "${AUTH_HEADER}: ${KEY}" -H "Content-Type: application/json")
MARK="smoke-$(date +%s)"

echo "== health";  curl -fsS "$BASE/health"; echo
echo "== write";   curl -fsS "${H[@]}" -X POST "$BASE/memory/add" \
  -d "{\"text\":\"The lighthouse keeper logs the tide every dawn (${MARK}).\",\"source_path\":\"memoria/admin/${MARK}\",\"category\":\"general\"}"; echo
echo "== search";  OUT="$(curl -fsS "${H[@]}" -X POST "$BASE/memory/search" \
  -d '{"query":"who records the tide in the morning?","top_k":5}')"
echo "$OUT" | head -c 600; echo
echo "$OUT" | grep -q "$MARK" || { echo "SMOKE: FAIL (memory not found)"; exit 1; }
echo "== memory: OK (write -> semantic search found ${MARK})"

echo "== task with a NEW project (core schema seed check)"
TBODY="$(mktemp)"
TASK="$(curl -sS -o "$TBODY" -w '%{http_code}' "${H[@]}" -X POST "$BASE/task/create" \
  -d "{\"title\":\"Smoke task ${MARK}\",\"project\":\"proj-${MARK}\"}")"
head -c 300 "$TBODY"; echo; rm -f "$TBODY"
[[ "$TASK" == 2* ]] || { echo "SMOKE: FAIL (task with new project -> HTTP ${TASK})"; exit 1; }
echo "== task: OK (HTTP ${TASK}, new project accepted)"

SBASE="http://127.0.0.1:${SPORT}"
echo "== sinapsis home"; curl -fsS -o /dev/null -w '%{http_code}\n' "$SBASE/"
echo "== sinapsis login"; TOK="$(curl -fsS -H 'Content-Type: application/json' -X POST "$SBASE/login" \
  -d "{\"username\":\"owner\",\"password\":\"${APW}\"}" | python3 -c 'import json,sys;print(json.load(sys.stdin)["token"])')"
SH=(-H "Authorization: Bearer ${TOK}" -H "Content-Type: application/json")
echo "== sinapsis me"; curl -fsS "${SH[@]}" "$SBASE/me" | head -c 300; echo
echo "== sinapsis memory search (authenticated)"
SBODY="$(mktemp)"
SCODE="$(curl -sS "${SH[@]}" --get "$SBASE/api/search" \
  --data-urlencode "q=lighthouse keeper tide" -o "$SBODY" -w '%{http_code}')"
printf 'HTTP %s\n' "$SCODE"
[[ "$SCODE" == "200" ]] || { cat "$SBODY"; rm -f "$SBODY"; echo "SMOKE: FAIL (Sinapsis memory search -> HTTP ${SCODE})"; exit 1; }
python3 -c 'import json,sys; p=json.load(open(sys.argv[1])); assert any(sys.argv[2] in str(r.get("path",""))+str(r.get("snippet","")) for r in p.get("results",[])), "Sinapsis search did not recover the unique smoke memory"' "$SBODY" "$MARK"
rm -f "$SBODY"
echo "== sinapsis search: OK (HTTP 200, unique memory recovered)"
echo "== sinapsis note"; curl -fsS "${SH[@]}" -X POST "$SBASE/api/file_create" \
  -d "{\"path\":\"notes/${MARK}\",\"content\":\"# ${MARK}\\nCreated by the smoke test.\"}"; echo
curl -fsS "${SH[@]}" "$SBASE/api/tree" | grep -q "${MARK}" && echo "SMOKE: OK (memory, task with new project, Sinapsis panel: home, login, search, note ${MARK})"
cleanup "${1:-}"
