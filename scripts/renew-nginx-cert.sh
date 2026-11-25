#!/usr/bin/env bash
set -euo pipefail
umask 077
domain=hubia.psxt.io
dir=/etc/hubia/tls
install -d -m 700 "$dir"
cd "$dir"
exec 8>.lock
flock 8

if [[ ! -d ca ]]; then
  tmp_ca=$(mktemp -d ca.XXXXXXXX)
  openssl req -x509 -newkey rsa:3072 -nodes -sha256 -days 3650 \
    -subj '/CN=HUB IA Private Root' -addext 'basicConstraints=critical,CA:TRUE,pathlen:0' \
    -addext 'keyUsage=critical,keyCertSign,cRLSign' -keyout "$tmp_ca/key.pem" -out "$tmp_ca/root.crt"
  mv -T "$tmp_ca" ca
fi
if [[ -s live/cert.pem ]] && openssl x509 -checkend 2592000 -noout -in live/cert.pem &&
   openssl verify -CAfile ca/root.crt -verify_hostname "$domain" live/cert.pem; then
  exit 0
fi

release=$(mktemp -d cert.XXXXXXXX)
openssl req -new -newkey rsa:2048 -nodes -subj "/CN=$domain" -keyout "$release/key.pem" -out "$release/request.pem"
cat > "$release/extensions" <<EXT
basicConstraints=critical,CA:FALSE
keyUsage=critical,digitalSignature,keyEncipherment
extendedKeyUsage=serverAuth
subjectAltName=DNS:$domain
EXT
openssl x509 -req -in "$release/request.pem" -CA ca/root.crt -CAkey ca/key.pem \
  -set_serial "0x$(openssl rand -hex 16)" -days 90 -sha256 -extfile "$release/extensions" -out "$release/cert.pem"
openssl verify -CAfile ca/root.crt -verify_hostname "$domain" "$release/cert.pem"
previous=$(readlink live || true)
ln -sfn "$release" live.new
mv -Tf live.new live
if [[ -f /etc/nginx/conf.d/zz-hubia.conf ]]; then
  if ! nginx -t || ! systemctl reload nginx; then
    if [[ -n $previous ]]; then ln -sfn "$previous" live.new; mv -Tf live.new live; fi
    exit 1
  fi
fi
if [[ $previous == cert.* && $previous != */* ]]; then
  rm -f -- "$previous/key.pem" "$previous/request.pem" "$previous/extensions" "$previous/cert.pem"
  rmdir -- "$previous" || true
fi
