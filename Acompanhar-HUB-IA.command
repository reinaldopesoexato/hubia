#!/bin/bash
set -uo pipefail
umask 077
log=$(mktemp "${TMPDIR:-/tmp}/hubia-monitor.XXXXXXXX") || exit 1
printf 'Monitor do HUB IA | root@hubia.psxt.io\nLog: %s\nCtrl+C encerra a sessão.\n' "$log"
ssh -T -o BatchMode=no -o StrictHostKeyChecking=ask \
  -o PreferredAuthentications=keyboard-interactive,password -o PubkeyAuthentication=no \
  -o ServerAliveInterval=15 -o ServerAliveCountMax=3 root@hubia.psxt.io 'bash -s' <<'REMOTE' 2>&1 | tee -a "$log"
base="$HOME/.local/share/hubia"
while true; do
  printf '\n%s\n' "$(date -Is)"
  if locks=$(lslocks -n --notruncate -o PATH); then
    if grep -Fxq "$base/deploy.lock" <<< "$locks"; then
      echo 'Deploy em execução'
    else
      echo 'Nenhum deploy com o instalador em execução'
    fi
  else
    echo 'Estado do deploy indisponível'
  fi
  printf 'Release ativa: '; readlink "$base/current" || echo 'nenhuma'
  printf 'Nginx: '; systemctl is-active nginx
  docker ps -a --filter label=com.docker.compose.project=hubia-prod --format '{{.Names}}: {{.Status}}'
  printf 'HTTPS /health/edge: '
  curl --fail --silent --show-error --max-time 5 --noproxy '*' \
    --resolve hubia.psxt.io:443:127.0.0.1 https://hubia.psxt.io/health/edge || echo 'indisponível'
  sleep 10
done
REMOTE
result=$?
printf 'Sessão SSH encerrada (código %s).\n' "$result"
exit "$result"
