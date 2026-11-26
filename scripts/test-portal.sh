#!/usr/bin/env bash
# Testa o bootstrap e o painel global em um Postgres descartável da mesma versão.
set -euo pipefail
db_image=${1:?Informe a imagem do PostgreSQL}
portal_image=${2:?Informe a imagem do portal}
test_id="hubia-test-$(openssl rand -hex 8)"
network=$(docker network create --internal "$test_id")
db=""
cleanup() {
  if [[ -n $db ]]; then docker rm -fv "$db" >/dev/null || true; fi
  docker network rm "$network" >/dev/null || true
}
trap cleanup EXIT
export POSTGRES_PASSWORD=$(openssl rand -hex 32)
export PORTAL_SESSION_SECRET=$(openssl rand -hex 32)
db=$(docker run -d --network "$network" --network-alias postgres --name "$test_id" \
  --tmpfs /var/lib/postgresql/data:rw -e POSTGRES_DB=hubia -e POSTGRES_USER=hub_migrator -e POSTGRES_PASSWORD "$db_image")
for attempt in {1..30}; do
  if docker exec "$db" pg_isready -q -h 127.0.0.1 -U hub_migrator -d hubia; then break; fi
  sleep 1
done
docker cp packages/database/. "$db:/tmp/hubia-migrations"
docker exec "$db" psql -qX -U hub_migrator -d hubia -f /tmp/hubia-migrations/migrate.psql
export PGPASSWORD="$POSTGRES_PASSWORD"
test_portal=(docker run --rm --network "$network" -e PORTAL_SESSION_SECRET -e PGPASSWORD)
# As tabelas Django precisam pertencer ao mesmo papel que as acessa no portal.
docker exec -i "$db" psql -qX -v ON_ERROR_STOP=1 -U hub_migrator -d hubia <<'SQL'
\getenv test_password POSTGRES_PASSWORD
ALTER ROLE hub_portal PASSWORD :'test_password';
SQL
# Simula no banco descartável a atualização da versão que guardava chaves por unidade.
"${test_portal[@]}" -e PGUSER=hub_portal "$portal_image" python -m django migrate bridge 0017_platform_email --noinput --verbosity 0
docker exec -i "$db" psql -qX -v ON_ERROR_STOP=1 -U hub_migrator -d hubia <<'SQL'
INSERT INTO hub_accounts.django_migrations (app, name, applied)
VALUES ('bridge', '0016_avihub_unit_keys', now());
ALTER TABLE hub_accounts.bridge_avihubsession ADD COLUMN encrypted_unit_keys bytea NOT NULL;
SQL
"${test_portal[@]}" -e PGUSER=hub_portal "$portal_image" python -m django migrate --noinput --verbosity 0
"${test_portal[@]}" -e PGUSER=hub_migrator "$portal_image" python smoke.py
