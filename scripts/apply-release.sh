#!/usr/bin/env bash
set -euo pipefail
umask 077
base=${1:?Diretório persistente obrigatório}
for required in docker openssl flock curl; do
  command -v "$required" >/dev/null || { echo "Falta o comando $required no servidor." >&2; exit 1; }
done
exec 9>"$base/deploy.lock"
flock -n 9 || { echo 'Outro deploy está em andamento.' >&2; exit 1; }
docker info --format '{{.ServerVersion}}' >/dev/null

envfile="$base/deploy.env"
if [[ ! -f $envfile ]]; then
  if docker volume inspect hubia-prod_hubia-db >/dev/null 2>&1; then
    echo 'O volume do banco já existe. Restaure deploy.env antes de continuar.' >&2
    exit 1
  fi
  password=$(openssl rand -hex 32)
  temp_env=$(mktemp "$base/.env.XXXXXXXX")
  printf 'POSTGRES_PASSWORD=%s\n' "$password" > "$temp_env"
  mv "$temp_env" "$envfile"
  unset password
fi

# Atualize a configuração de forma atômica, preservando os segredos dos redeploys.
temp_env=$(mktemp "$base/.portal-env.XXXXXXXX")
cp "$envfile" "$temp_env"
for setting in PORTAL_DB_PASSWORD PORTAL_SESSION_SECRET; do
  if ! grep -q "^${setting}=" "$temp_env"; then
    printf '%s=%s\n' "$setting" "$(openssl rand -hex 32)" >> "$temp_env"
  fi
done
mv "$temp_env" "$envfile"
set -a
source "$envfile"
set +a

export HUBIA_CONNECTORS_DIR="$base/connectors"
mkdir -p "$HUBIA_CONNECTORS_DIR"
chmod 755 "$HUBIA_CONNECTORS_DIR"

compose=(docker compose -p hubia-prod --env-file "$envfile" -f compose.yaml -f deploy/compose.production.yaml)
"${compose[@]}" config --quiet
"${compose[@]}" pull postgres
"${compose[@]}" up -d --wait postgres
"${compose[@]}" build portal avihub-pdf
db_image=$(docker inspect --format '{{.Image}}' "$("${compose[@]}" ps -q postgres)")
bash scripts/test-portal.sh "$db_image" hubia-prod-portal
"${compose[@]}" exec -T postgres mkdir -p /tmp/hubia-migrations
"${compose[@]}" cp packages/database/. postgres:/tmp/hubia-migrations/
"${compose[@]}" exec -T postgres psql -X -U hub_migrator -d hubia -f /tmp/hubia-migrations/migrate.psql
"${compose[@]}" exec -T -e PORTAL_DB_PASSWORD postgres psql -X -v ON_ERROR_STOP=1 -U hub_migrator -d hubia <<'SQL'
\getenv portal_password PORTAL_DB_PASSWORD
ALTER ROLE hub_portal PASSWORD :'portal_password';
SQL
"${compose[@]}" run --rm --no-deps -T portal python -m django migrate --noinput
PGPASSWORD="$POSTGRES_PASSWORD" "${compose[@]}" run --rm --no-deps -T -e PGUSER=hub_migrator -e PGPASSWORD portal python smoke.py
# Configuração institucional inicial. Preserva as decisões do administrador nos próximos deploys.
"${compose[@]}" exec -T postgres psql -X -v ON_ERROR_STOP=1 -U hub_migrator -d hubia <<'SQL'
INSERT INTO hub_accounts.bridge_mailprovider (tenant, domain, enabled)
SELECT tenant_id, 'pesoexatobalancas.com.br', true FROM hub_tenancy.institutions
WHERE tenant_id='e7eb1e21-00c7-4abd-8ada-641ce20a91b3'
ON CONFLICT (tenant) DO NOTHING;
SQL
# Ativação específica desta instalação. O conector aceita qualquer instituição.
PGPASSWORD="$POSTGRES_PASSWORD" "${compose[@]}" run --rm --no-deps -T -e PGUSER=hub_migrator -e PGPASSWORD portal python -m django provision_avihub --if-present --tenant e7eb1e21-00c7-4abd-8ada-641ce20a91b3 --origin https://avihub.pesoexato.com
"${compose[@]}" exec -T postgres psql -X -v ON_ERROR_STOP=1 -U hub_migrator -d hubia -f /tmp/hubia-migrations/provision-client-workspace.sql
"${compose[@]}" up -d --wait portal events library-sync iaves-alerts avihub-reports
if command -v systemctl >/dev/null && systemctl is-active --quiet nginx; then
  echo 'Configurando hubia.psxt.io no Nginx existente.'
  admin=()
  if (( EUID != 0 )); then admin=(sudo -n); fi
  "${admin[@]}" bash scripts/install-nginx.sh
  ln -sfn "$PWD" "$base/current.new"
  mv -Tf "$base/current.new" "$base/current"
  echo "Deploy verificado. HTTPS público da Let’s Encrypt ativo no Nginx de hubia.psxt.io."
  exit 0
fi
"${compose[@]}" pull caddy
"${compose[@]}" run --rm --no-deps caddy caddy adapt --config /etc/caddy/Caddyfile --adapter caddyfile >/dev/null
if ! "${compose[@]}" up -d caddy; then
  echo 'Falha ao iniciar o HTTPS do HUB. Verificando quem escuta na porta 443/TCP:' >&2
  if command -v ss >/dev/null; then ss -ltnp 'sport = :443' >&2; fi
  docker ps --filter publish=443 --format '{{.Names}} {{.Ports}}' >&2
  exit 1
fi

# Verifica SNI, cadeia e nome do certificado diretamente no servidor.
ca_file="$base/root.crt.new"
for attempt in {1..30}; do
  if "${compose[@]}" exec -T caddy cat /data/caddy/pki/authorities/local/root.crt > "$ca_file" 2>/dev/null &&
     [[ $(curl --silent --fail --max-time 4 --noproxy '*' --cacert "$ca_file" \
       --resolve hubia.psxt.io:443:127.0.0.1 https://hubia.psxt.io/health/edge) == ok ]]; then
    mv "$ca_file" "$base/root.crt"
    ln -sfn "$PWD" "$base/current.new"
    mv -Tf "$base/current.new" "$base/current"
    openssl x509 -in "$base/root.crt" -noout -sha256 -fingerprint
    echo "Deploy verificado. Certificado público da CA: $base/root.crt"
    exit 0
  fi
  sleep 1
done
echo 'Falha na verificação HTTPS de hubia.psxt.io. Consulte docker compose logs caddy.' >&2
exit 1
