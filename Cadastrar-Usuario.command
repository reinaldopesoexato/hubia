#!/bin/bash
set -euo pipefail
ssh -t -o StrictHostKeyChecking=ask root@hubia.psxt.io \
  'bash "$HOME/.local/share/hubia/current/scripts/create-portal-user.sh"'
