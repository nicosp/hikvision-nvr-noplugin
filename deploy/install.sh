#!/bin/bash
# Installs or updates go2rtc as a Podman Quadlet system service.
#
#   sudo deploy/install.sh
#
# - creates the go2rtc system user and group (no login, no home)
# - generates the config from .env (asks the NVR which channels it has) into
#   /etc/go2rtc, with hikdecrypt.py (undoes Hikvision's stream encryption):
#   directory 0700, files 0600, owned by go2rtc
# - installs /etc/containers/systemd/go2rtc.container and (re)starts go2rtc
# - installs the page script for the nginx site (deploy/install-nginx.sh) in
#   /usr/local/share/nvr-live: nvr-ls.js and streams.json (no secrets)
#
# Run it again after changing .env or adding or removing cameras.
set -euo pipefail

[[ $EUID -eq 0 ]] || { echo "Run with sudo: sudo $0" >&2; exit 1; }

REPO=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
CONF_DIR=/etc/go2rtc
UNIT=/etc/containers/systemd/go2rtc.container
WEBROOT=/usr/local/share/nvr-live
NAME=go2rtc

[[ -f $REPO/.env ]] || { echo "$REPO/.env not found: copy .env.example to .env and fill it in." >&2; exit 1; }

# 1. User and group.
if ! getent group "$NAME" >/dev/null; then
    groupadd --system "$NAME"
    echo "Created group $NAME"
fi
if ! getent passwd "$NAME" >/dev/null; then
    useradd --system --gid "$NAME" --no-create-home --home-dir /nonexistent \
        --shell /usr/sbin/nologin --comment "go2rtc camera streams" "$NAME"
    echo "Created user $NAME"
fi
UID_=$(id -u "$NAME")
GID_=$(getent group "$NAME" | cut -d: -f3)

# 2. Config, generated in a private temporary directory, then installed.
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
python3 -I "$REPO/setup.py" --env "$REPO/.env" --out "$tmp"
install -d -o "$NAME" -g "$NAME" -m 0700 "$CONF_DIR"
install -o "$NAME" -g "$NAME" -m 0600 "$tmp/go2rtc.yaml" "$tmp/go2rtc.env" "$REPO/hikdecrypt.py" "$CONF_DIR/"
echo "Installed $CONF_DIR/go2rtc.yaml, go2rtc.env and hikdecrypt.py"
install -d -m 0755 "$WEBROOT"
install -m 0644 "$REPO/web/nvr-ls.js" "$tmp/streams.json" "$WEBROOT/"
echo "Installed the page script in $WEBROOT"

# 3. Quadlet unit.
sed -e "s/@UID@/$UID_/" -e "s/@GID@/$GID_/" "$REPO/deploy/go2rtc.container" > "$tmp/go2rtc.container"
install -o root -g root -m 0644 "$tmp/go2rtc.container" "$UNIT"
echo "Installed $UNIT (uid $UID_, gid $GID_)"

# 4. Start (or restart, to pick up new config).
systemctl daemon-reload
echo "Starting go2rtc.service (pulls the image the first time)..."
systemctl restart go2rtc.service

for _ in $(seq 30); do
    if curl -fsS -o /dev/null http://127.0.0.1:1984/api/streams 2>/dev/null; then
        echo "go2rtc is up: http://127.0.0.1:1984"
        exit 0
    fi
    sleep 1
done
echo "go2rtc did not answer on 127.0.0.1:1984 - check: journalctl -u go2rtc.service" >&2
exit 1
