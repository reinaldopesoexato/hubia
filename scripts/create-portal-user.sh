#!/usr/bin/env bash
set -euo pipefail
base="$HOME/.local/share/hubia"
cd "$base/current"
# A senha do migrador é fornecida somente ao processo privado de provisionamento.
set -a
source "$base/deploy.env"
set +a
export HUBIA_CONNECTORS_DIR="$base/connectors"
export PGPASSWORD="$POSTGRES_PASSWORD"
docker compose -p hubia-prod --env-file "$base/deploy.env" -f compose.yaml -f deploy/compose.production.yaml \
  run --rm --no-deps -e PGPASSWORD -e PGUSER=hub_migrator portal python create_user.py "$@"
