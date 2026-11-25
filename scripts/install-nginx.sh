#!/usr/bin/env bash
set -euo pipefail
umask 077
domain=hubia.psxt.io
target=/etc/nginx/conf.d/zz-hubia.conf
challenge=/etc/nginx/conf.d/zz-hubia-acme.conf
webroot=/var/www/hubia-acme
for file in "$target" "$challenge"; do
  if [[ -e $file ]] && ! grep -qxF '# Managed by HUB IA' "$file"; then
    echo "Arquivo Nginx externo encontrado em $file. Instalação interrompida." >&2
    exit 1
  fi
done
config=$(nginx -T 2>/dev/null)
if ! grep -Eq '^[[:space:]]*include[[:space:]]+/etc/nginx/conf\.d/\*\.conf;' <<< "$config"; then
  echo 'O Nginx usa outro diretório de virtual hosts.' >&2
  exit 1
fi
if awk -v own="$target:" -v http="$challenge:" '
  /^# configuration file / {file=$4}
  file != own && file != http && /^[[:space:]]*server_name[[:space:]]/ && /[[:space:]]hubia\.psxt\.io[;[:space:]]/ {found=1}
  END {exit !found}
' <<< "$config"; then
  echo 'Já existe um virtual host externo para hubia.psxt.io.' >&2
  exit 1
fi
if ! command -v certbot >/dev/null; then
  apt-get update
  DEBIAN_FRONTEND=noninteractive apt-get install -y certbot
fi

timer=certbot.timer
if [[ $(systemctl show -p LoadState --value "$timer") != loaded ]]; then
  timer=snap.certbot.renew.timer
fi
systemctl enable --now "$timer"
systemctl is-active --quiet "$timer"

backup=$(mktemp -d)
for file in "$target" "$challenge"; do
  if [[ -f $file ]]; then cp -p "$file" "$backup/"; fi
done
finish() {
  status=$?
  trap - EXIT
  if (( status != 0 )); then
    for file in "$target" "$challenge"; do
      original="$backup/${file##*/}"
      if [[ -f $original ]]; then cp -p "$original" "$file"; else rm -f "$file"; fi
    done
    nginx -t && systemctl reload nginx
  fi
  rm -rf -- "$backup"
  exit "$status"
}
trap finish EXIT

install -d -m 0755 "$webroot" "$webroot/.well-known" "$webroot/.well-known/acme-challenge"
install -m 0644 deploy/nginx-acme.conf "$challenge.new"
mv -T "$challenge.new" "$challenge"
nginx -t && systemctl reload nginx || exit 1

# Keep the current HTTPS certificate in service while HTTP-01 validates the domain.
certbot certonly --non-interactive --agree-tos --register-unsafely-without-email \
  --server https://acme-v02.api.letsencrypt.org/directory --webroot -w "$webroot" \
  --cert-name "$domain" --keep-until-expiring -d "$domain"
install -d -m 0755 /var/www/hubia-public
site_dir=$(mktemp -d /var/www/hubia-public/site.XXXXXXXX)
install -m 0644 apps/web/public/{index.html,hub-ia.png,hub-ia-64.png,favicon.ico,apple-touch-icon.png} "$site_dir/"
chmod 0755 "$site_dir"
sed "s|root /var/www/hubia-public;|root $site_dir;|" deploy/nginx-hubia.conf > "$target.new"
chmod 0644 "$target.new"
mv -T "$target.new" "$target"
nginx -t && systemctl reload nginx || exit 1
health=$(curl -fsS --retry 5 --retry-all-errors --max-time 5 --noproxy '*' \
  --resolve "$domain:443:127.0.0.1" "https://$domain/health/edge")
[[ $health == ok ]] || { echo 'Resposta HTTPS inesperada' >&2; exit 1; }

# Reload is graceful; a worker with the previous page may finish an in-flight request.
for attempt in 1 2 3 4 5; do
  if curl -fsS --max-time 5 --noproxy '*' --resolve "$domain:443:127.0.0.1" \
      "https://$domain/" -o "$backup/home.html" && cmp -s apps/web/public/index.html "$backup/home.html"; then
    break
  fi
  if (( attempt == 5 )); then echo 'Página inicial publicada diverge da versão enviada.' >&2; exit 1; fi
  sleep 1
done

curl -fsS --retry 4 --retry-all-errors --max-time 8 --noproxy '*' \
  --resolve "$domain:443:127.0.0.1" "https://$domain/conta/entrar/" -o "$backup/login.html"
grep -q 'name="password"' "$backup/login.html" || { echo 'Formulário de login indisponível' >&2; exit 1; }
for asset in hub-ia.png hub-ia-64.png favicon.ico apple-touch-icon.png; do
  for attempt in 1 2 3 4 5; do
    if curl -fsS --max-time 8 --noproxy '*' --resolve "$domain:443:127.0.0.1" \
        "https://$domain/$asset" -o "$backup/$asset" && cmp -s "apps/web/public/$asset" "$backup/$asset"; then
      break
    fi
    if (( attempt == 5 )); then echo "Arquivo público indisponível ou divergente: /$asset" >&2; exit 1; fi
    sleep 1
  done
done

install -d -m 0755 /etc/letsencrypt/renewal-hooks/deploy
hook=/etc/letsencrypt/renewal-hooks/deploy/hubia-reload-nginx
cat > "$hook.new" <<'HOOK'
#!/bin/sh
set -eu
[ "${RENEWED_LINEAGE:-}" = /etc/letsencrypt/live/hubia.psxt.io ] || exit 0
nginx -t && systemctl reload nginx
HOOK
chmod 0755 "$hook.new"
mv -T "$hook.new" "$hook"
if systemctl is-active --quiet hubia-cert.timer || systemctl is-enabled --quiet hubia-cert.timer; then
  systemctl disable --now hubia-cert.timer
fi
openssl x509 -in "/etc/letsencrypt/live/$domain/fullchain.pem" -noout -issuer -enddate
