#!/usr/bin/env bash
# Creates .env (and .secrets/) from .env.example with fresh random secrets. Never overwrites.
set -euo pipefail
cd "$(dirname "$0")"
[[ -e .env ]] && { echo ".env already exists; leaving it untouched."; exit 0; }
rand() { openssl rand -hex 24; }
SINAPSIS_KEY="$(rand)"; ADMIN_PW="$(openssl rand -base64 18 | tr -d '/+=')"
sed -e "s/^POSTGRES_PASSWORD=.*/POSTGRES_PASSWORD=$(rand)/" \
    -e "s/^ADMIN_API_KEY=.*/ADMIN_API_KEY=$(rand)/" \
    -e "s/^SINAPSIS_API_KEY=.*/SINAPSIS_API_KEY=${SINAPSIS_KEY}/" \
    -e "s/^SINAPSIS_ADMIN_PASSWORD=.*/SINAPSIS_ADMIN_PASSWORD=${ADMIN_PW}/" .env.example > .env
chmod 600 .env
umask 077; mkdir -p .secrets; printf '%s' "$SINAPSIS_KEY" > .secrets/sinapsis_api_key
echo "Created .env and .secrets/ (mode 600)."
echo "Sinapsis login: user 'owner', password in .env as SINAPSIS_ADMIN_PASSWORD (you will be asked to change it)."
echo "Next: docker compose up -d --wait   then open http://127.0.0.1:8080"
