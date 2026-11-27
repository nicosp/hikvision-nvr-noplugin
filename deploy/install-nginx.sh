#!/bin/bash
# Installs the nginx site that serves the NVR web UI with go2rtc live view, and
# nvr-localservice.service (localservice.py, the stand-in for Hikvision's Local
# Service that the UI's video windows talk to).
#
#   sudo deploy/install-nginx.sh
#
# Run deploy/install.sh first (go2rtc, and the page script in /usr/local/share/nvr-live).
# The site name is PROXY_DOMAIN in .env; the NVR is NVR_HOST.
#
# TLS, Debian layout: /etc/ssl/certs/<domain>.pem (certificate, then any
# intermediate CA) and /etc/ssl/private/<domain>.key. If the key doesn't exist
# yet, this creates it with a certificate request (.csr) for your CA to sign,
# and stops. Put the signed certificate in place and run it again. Browsers
# need the name in subjectAltName, not just the CN.
set -euo pipefail

[[ $EUID -eq 0 ]] || { echo "Run with sudo: sudo $0" >&2; exit 1; }

REPO=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
WEBROOT=/usr/local/share/nvr-live

env_get() {  # env_get NAME DEFAULT: value from .env, without quotes
    local v
    v=$(sed -n "s/^[[:space:]]*$1[[:space:]]*=[[:space:]]*//p" "$REPO/.env" | tail -n1)
    v=${v%\"}; v=${v#\"}; v=${v%\'}; v=${v#\'}
    echo "${v:-$2}"
}
[[ -f $REPO/.env ]] || { echo "$REPO/.env not found" >&2; exit 1; }
DOMAIN=$(env_get PROXY_DOMAIN "")
NVR=$(env_get NVR_HOST "")
[[ $DOMAIN =~ ^[A-Za-z0-9.-]+$ ]] || { echo "bad or missing PROXY_DOMAIN in .env" >&2; exit 1; }
[[ $NVR =~ ^[A-Za-z0-9.:-]+$ ]] || { echo "bad or missing NVR_HOST" >&2; exit 1; }
[[ -f $WEBROOT/nvr-ls.js && -f $WEBROOT/streams.json ]] ||
    { echo "$WEBROOT is missing: run sudo deploy/install.sh first" >&2; exit 1; }

CERT=/etc/ssl/certs/$DOMAIN.pem
KEY=/etc/ssl/private/$DOMAIN.key
CSR=/etc/ssl/private/$DOMAIN.csr
SITE=/etc/nginx/sites-available/$DOMAIN
LINK=/etc/nginx/sites-enabled/$DOMAIN

# 1. TLS key and certificate.
if [[ ! -f $KEY ]]; then
    (umask 077; openssl genrsa -out "$KEY" 2048 2>/dev/null)
    chown root:ssl-cert "$KEY" 2>/dev/null && chmod 0640 "$KEY"
    openssl req -new -key "$KEY" -out "$CSR" -subj "/CN=$DOMAIN" \
        -addext "subjectAltName=DNS:$DOMAIN"
    echo "Created $KEY and the certificate request $CSR."
fi
if [[ ! -f $CERT ]]; then
    cat >&2 <<EOF

No certificate yet. Sign $CSR with your CA, for example:

  openssl x509 -req -in $CSR -CA ca.crt -CAkey ca.key -CAcreateserial \\
      -days 825 -copy_extensions copy -out $DOMAIN.crt

then: sudo install -m 0644 $DOMAIN.crt $CERT && sudo $0
EOF
    exit 1
fi
openssl x509 -in "$CERT" -noout -ext subjectAltName 2>/dev/null | grep -qF "DNS:$DOMAIN" ||
    { echo "$CERT has no subjectAltName DNS:$DOMAIN - browsers will reject it. Re-sign it with" \
           "subjectAltName=DNS:$DOMAIN (e.g. openssl x509 -req ... -copy_extensions copy, from the .csr)." >&2; exit 1; }
[[ $(openssl x509 -in "$CERT" -noout -pubkey) == $(openssl pkey -in "$KEY" -pubout) ]] ||
    { echo "$CERT does not belong to $KEY" >&2; exit 1; }

# 2. The Local Service stand-in (runs as a throwaway system user, DynamicUser=).
install -d -m 0755 /usr/local/lib/nvr-live
install -m 0644 "$REPO/localservice.py" /usr/local/lib/nvr-live/localservice.py
install -m 0644 "$REPO/deploy/nvr-localservice.service" /etc/systemd/system/nvr-localservice.service
systemctl daemon-reload
systemctl enable nvr-localservice.service >/dev/null 2>&1
systemctl restart nvr-localservice.service
echo "Installed and (re)started nvr-localservice.service"

# 3. Site.
tmp=$(mktemp)
trap 'rm -f "$tmp" "$tmp.old"' EXIT
sed -e "s|@DOMAIN@|$DOMAIN|g" -e "s|@NVR@|$NVR|g" -e "s|@WEBROOT@|$WEBROOT|g" \
    -e "s|@CERT@|$CERT|g" -e "s|@KEY@|$KEY|g" "$REPO/deploy/nginx-site.conf" > "$tmp"
had_site=0; [[ -f $SITE ]] && { had_site=1; cp -p "$SITE" "$tmp.old"; }
install -m 0644 "$tmp" "$SITE"
ln -sfn "../sites-available/$DOMAIN" "$LINK"

# 4. Check and reload; put things back if nginx rejects the config.
if ! nginx -t; then
    if (( had_site )); then mv "$tmp.old" "$SITE"; else rm -f "$LINK" "$SITE"; fi
    echo "nginx rejected the config; left it as it was." >&2
    exit 1
fi
rm -f "$tmp.old"
systemctl reload nginx
echo "Installed $SITE. Point $DOMAIN at this machine in DNS, then open https://$DOMAIN/"
