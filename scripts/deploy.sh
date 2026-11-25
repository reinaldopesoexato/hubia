#!/usr/bin/env bash
set -euo pipefail

project=$(cd "$(dirname "$0")/.." && pwd)
cd "$project"
if [[ $# -eq 1 && ${1:-} == --help ]]; then
  echo 'Uso: scripts/deploy.sh (destino root@hubia.psxt.io, porta opcional em HUBIA_SSH_PORT)'
  exit 0
fi
if [[ $# -gt 1 || ${1-root} != root ]]; then
  echo 'O deploy usa root@hubia.psxt.io. Execute ./scripts/deploy.sh sem argumentos.' >&2
  exit 2
fi

ssh_options=(-T -o BatchMode=no -o StrictHostKeyChecking=ask)
if [[ -n ${HUBIA_DEPLOY_SOCKET:-} ]]; then
  ssh_options+=(-S "$HUBIA_DEPLOY_SOCKET")
fi
if [[ -n ${HUBIA_SSH_PORT:-} ]]; then
  if [[ ! $HUBIA_SSH_PORT =~ ^[1-9][0-9]{0,4}$ ]] || (( HUBIA_SSH_PORT > 65535 )); then
    echo 'Porta SSH inválida.' >&2
    exit 2
  fi
  ssh_options+=(-p "$HUBIA_SSH_PORT")
fi

# O SSH lê credenciais de /dev/tty. O stdin transporta somente os arquivos do deploy.
export COPYFILE_DISABLE=1
tar --no-xattrs --exclude=__pycache__ -czf - apps/portal scripts/create-portal-user.sh apps/web/public compose.yaml deploy/Caddyfile deploy/compose.production.yaml \
  deploy/nginx-hubia.conf deploy/nginx-acme.conf scripts/test-portal.sh scripts/apply-release.sh scripts/install-nginx.sh packages/database |
  ssh "${ssh_options[@]}" root@hubia.psxt.io '
    set -eu
    umask 077
    base="$HOME/.local/share/hubia"
    mkdir -p "$base/releases"
    release=$(mktemp -d "$base/releases/release.XXXXXXXX")
    tar -xzf - -C "$release"
    cd "$release"
    exec bash scripts/apply-release.sh "$base"
  '
