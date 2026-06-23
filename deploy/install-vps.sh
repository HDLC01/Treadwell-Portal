#!/usr/bin/env bash
#
# Treadwell Customer Proposal Portal — VPS deploy (Ubuntu, shared box).
# Run from inside the app dir (the repo, extracted to e.g. /opt/treadwell-portal-staging).
#
#   bash deploy/install-vps.sh <domain> <port> <compose-file>
#   e.g. bash deploy/install-vps.sh staging.portal.wetreadwell.com 8899 docker-compose.staging.yml
#
# Safe to re-run. Only adds OUR nginx site (never touches other sites' configs).
set -euo pipefail

DOMAIN="${1:?usage: install-vps.sh <domain> <port> <compose-file>}"
PORT="${2:?missing port}"
COMPOSE="${3:?missing compose file}"
SITE="treadwell-portal-${DOMAIN//./-}"

echo "=== Treadwell Portal -> https://$DOMAIN (127.0.0.1:$PORT, $COMPOSE) ==="

echo "[1/4] Ensuring nginx + certbot..."
apt-get update -y >/dev/null
apt-get install -y nginx certbot python3-certbot-nginx >/dev/null

echo "[2/4] Building + starting the stack..."
docker compose -f "$COMPOSE" up -d --build
echo "  waiting for health on 127.0.0.1:$PORT ..."
for i in $(seq 1 40); do
  if curl -fsS "http://127.0.0.1:$PORT/healthz" >/dev/null 2>&1; then echo "  ✓ healthy"; break; fi
  sleep 2
  [ "$i" -eq 40 ] && { echo "  ✗ not healthy — docker compose -f $COMPOSE logs"; exit 1; }
done

echo "[3/4] nginx reverse proxy for $DOMAIN..."
cat > "/etc/nginx/sites-available/$SITE" <<EOF
server {
    listen 80;
    listen [::]:80;
    server_name $DOMAIN;
    location /.well-known/acme-challenge/ { root /var/www/html; }
    location / {
        proxy_pass http://127.0.0.1:$PORT;
        proxy_http_version 1.1;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_read_timeout 60s;
        client_max_body_size 12M;
    }
}
EOF
ln -sf "/etc/nginx/sites-available/$SITE" "/etc/nginx/sites-enabled/$SITE"
nginx -t && systemctl reload nginx

echo "[4/4] Let's Encrypt cert for $DOMAIN..."
certbot --nginx -d "$DOMAIN" --non-interactive --agree-tos -m hanz@wetreadwell.com --redirect
systemctl reload nginx

echo "=== ✓ deployed: https://$DOMAIN ==="
echo "  update: cd $(pwd) && git pull && docker compose -f $COMPOSE up -d --build"
