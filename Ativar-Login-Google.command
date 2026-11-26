#!/bin/bash
set -euo pipefail
umask 077

# O SSH usa o terminal para autenticação e recebe as credenciais por stdin.
ssh_options=(-T -o StrictHostKeyChecking=ask -o ConnectTimeout=12)
if [[ -n ${HUBIA_SSH_PORT:-} ]]; then ssh_options+=(-p "$HUBIA_SSH_PORT"); fi
if [[ -n ${HUBIA_DEPLOY_SOCKET:-} ]]; then ssh_options+=(-S "$HUBIA_DEPLOY_SOCKET"); fi

credentials() {
  python3 - <<'PY'
import json
import re
from pathlib import Path

matches = set()
for path in (Path.home() / "Downloads").glob("client_secret*.json"):
    try:
        client = json.loads(path.read_text())["web"]
        if client.get("project_id") != "fit-freehold-515901-h8":
            continue
        if "https://hubia.psxt.io/conta/google/retorno/" not in client.get("redirect_uris", []):
            continue
        values = (client["client_id"], client["client_secret"])
        if all(isinstance(v, str) and re.fullmatch(r"[A-Za-z0-9_.-]+", v) for v in values):
            matches.add(values)
    except (KeyError, ValueError, OSError, TypeError):
        continue
if len(matches) != 1:
    raise SystemExit("É necessário um único cliente Web do HUB IA com o retorno correto em Downloads.")
client_id, secret = matches.pop()
print("GOOGLE_CLIENT_ID=" + client_id)
print("GOOGLE_CLIENT_SECRET=" + secret)
PY
}

if [[ ${1:-} == --verificar-json ]]; then
  credentials >/dev/null
  echo 'JSON do HUB IA validado.'
  exit
fi

credentials | ssh "${ssh_options[@]}" root@hubia.psxt.io 'bash -c '\''
set -euo pipefail
umask 077
base="$HOME/.local/share/hubia"
cd "$base/current"
exec 9>"$base/deploy.lock"
flock -w 20 9 || { echo "Outro deploy em andamento."; exit 1; }
envfile="$base/deploy.env"
test -f "$envfile"
grep -q "GOOGLE_CLIENT_SECRET:" deploy/compose.production.yaml || { echo "O Compose publicado ainda precisa da versão com suporte ao Google."; exit 1; }
IFS= read -r id
IFS= read -r secret
[[ $id =~ ^GOOGLE_CLIENT_ID=[a-zA-Z0-9_.-]+$ && $secret =~ ^GOOGLE_CLIENT_SECRET=[a-zA-Z0-9_.-]+$ ]]
temp=$(mktemp -d "$base/.oauth.XXXXXXXX")
trap "rm -rf -- \"$temp\"" EXIT
cp "$envfile" "$temp/previous"
sed -E "/^GOOGLE_CLIENT_(ID|SECRET)=/d" "$envfile" > "$temp/new"
printf "\n%s\n%s\n" "$id" "$secret" >> "$temp/new"
unset id secret
export HUBIA_CONNECTORS_DIR="$base/connectors"
compose=(docker compose -p hubia-prod -f compose.yaml -f deploy/compose.production.yaml)
"${compose[@]}" --env-file "$temp/new" config --quiet
mv "$temp/new" "$envfile"
portal() { "${compose[@]}" --env-file "$envfile" up -d --no-deps --no-build --wait portal; }
if portal && "${compose[@]}" --env-file "$envfile" exec -T portal python -c "import django; django.setup(); from django.test import Client; from urllib.parse import urlsplit; r=Client(HTTP_HOST=\"hubia.psxt.io\").post(\"/conta/google/\",secure=True); assert r.status_code==302 and urlsplit(r.url).hostname==\"accounts.google.com\", r.status_code"; then
  echo "Login ativado. O portal redirecionou a solicitação para accounts.google.com."
else
  echo "A verificação falhou. Restaurando a configuração anterior."
  mv "$temp/previous" "$envfile"
  portal
  exit 1
fi
'\'''
