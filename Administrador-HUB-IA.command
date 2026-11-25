#!/bin/bash
set -eu -o pipefail
umask 077
cd "$(dirname "$0")"
log=$(mktemp "${TMPDIR:-/tmp}/hubia-admin.XXXXXXXX")
control=$(mktemp -d /tmp/hubia-ssh.XXXXXXXX)
export HUBIA_DEPLOY_SOCKET="$control/socket"
ssh_options=(-S "$HUBIA_DEPLOY_SOCKET" -o StrictHostKeyChecking=ask)
if [[ -n ${HUBIA_SSH_PORT:-} ]]; then ssh_options+=(-p "$HUBIA_SSH_PORT"); fi
finish() {
  result=$?
  ssh "${ssh_options[@]}" -O exit root@hubia.psxt.io >/dev/null 2>&1 || true
  rmdir "$control" 2>/dev/null || true
  printf '\nResultado da configuração: %d\n' "$result" | tee -a "$log"
}
trap finish EXIT
printf 'Log da instalação: %s\n' "$log"
ssh "${ssh_options[@]}" -o ControlMaster=yes -fN root@hubia.psxt.io
./scripts/deploy.sh 2>&1 | tee -a "$log"
ssh "${ssh_options[@]}" -t root@hubia.psxt.io 'bash "$HOME/.local/share/hubia/current/scripts/create-portal-user.sh" --administrador'
