#!/bin/bash
set -o pipefail
umask 077
cd "$(dirname "$0")" || exit 1
log=$(mktemp "${TMPDIR:-/tmp}/hubia-deploy.XXXXXXXX") || exit 1
printf 'Log desta instalação: %s\n' "$log"
printf 'Deploy em root@hubia.psxt.io\n'
./scripts/deploy.sh 2>&1 | tee -a "$log"
result=$?
printf '\nResultado do deploy: %d\n' "$result" >> "$log"
printf '\nCódigo de saída: %d. Pressione Enter para fechar.' "$result"
IFS= read -r _
exit "$result"
