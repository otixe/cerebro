#!/usr/bin/env bash
# Creates .env from .env.example with fresh random secrets. Never overwrites an existing .env.
set -euo pipefail
cd "$(dirname "$0")"
[[ -e .env ]] && { echo ".env already exists; leaving it untouched."; exit 0; }
rand() { openssl rand -hex 24; }
ADMIN="$(rand)"
sed -e "s/^POSTGRES_PASSWORD=.*/POSTGRES_PASSWORD=$(rand)/" \
    -e "s/^ADMIN_API_KEY=.*/ADMIN_API_KEY=${ADMIN}/" .env.example > .env
chmod 600 .env
echo "Created .env (mode 600). Your admin key is in .env as ADMIN_API_KEY."
echo "Next: docker compose up -d --wait"
