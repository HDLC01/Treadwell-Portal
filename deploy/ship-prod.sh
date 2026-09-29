#!/usr/bin/env bash
# Off-box deploy for the Treadwell Portal (PRODUCTION — portal.wetreadwell.com,
# container treadwell-portal on port 8898, shared Supabase DB via docker-compose.prod.yml).
#
# Same off-box model as ship.sh (staging): the VPS is 1 core / 2 GB and building on
# it browns out every site. Build the image HERE, ship it over SSH, push the compose
# file (the VPS dir is NOT a git checkout), then load + restart (NO --build).
#
# The prod .env (DATABASE_URL / SERVICE_TOKEN / RESEND_API_KEY / EMAIL_FROM) is
# managed ONLY on the VPS at $APP_DIR/.env (chmod 600) and is never shipped from here.
#
# Prereqs: local Docker engine running; a `Host treadwell-vps` entry in your SSH config
#          (the address, login, port and key live there, not in this public repo); the
#          VPS already has $APP_DIR/.env in place.
# Usage:   bash deploy/ship-prod.sh
#          VPS_HOST, VPS_USER and SSH_KEY still override it (another box, another key).
set -euo pipefail

VPS_HOST="${VPS_HOST:-treadwell-vps}"
VPS_USER="${VPS_USER:-}"   # empty: the User from the SSH config entry
SSH_KEY="${SSH_KEY:-}"     # empty: the IdentityFile from the SSH config entry
APP_DIR="/opt/treadwell-portal"
IMAGE="treadwell-portal:latest"
COMPOSE="docker-compose.prod.yml"
TARGET="${VPS_USER:+$VPS_USER@}$VPS_HOST"
SSH=(ssh -o ConnectTimeout=20)
SCP=(scp)
if [ -n "$SSH_KEY" ]; then SSH+=(-i "$SSH_KEY"); SCP+=(-i "$SSH_KEY"); fi
SSH+=("$TARGET")

cd "$(dirname "$0")/.."

echo "==> Building $IMAGE locally (off the prod box)…"
docker build --platform linux/amd64 -t "$IMAGE" .

echo "==> Shipping image + compose over SSH…"
docker save "$IMAGE" | gzip | "${SSH[@]}" "cat > /tmp/portal-prod.tar.gz"
"${SCP[@]}" "$COMPOSE" "$TARGET:$APP_DIR/$COMPOSE.new"

echo "==> Load + restart on the VPS (NO build)…"
"${SSH[@]}" "set -euo pipefail
  cd $APP_DIR
  if [ ! -f .env ]; then echo '   ERROR: $APP_DIR/.env is missing — create it first'; exit 1; fi
  cp -f $COMPOSE $COMPOSE.bak 2>/dev/null || true
  mv -f $COMPOSE.new $COMPOSE
  gunzip -c /tmp/portal-prod.tar.gz | docker load
  rm -f /tmp/portal-prod.tar.gz
  docker compose -f $COMPOSE up -d
  for i in \$(seq 1 24); do
    if curl -fsS http://localhost:8898/healthz >/dev/null; then echo '   portal-prod healthy'; exit 0; fi
    sleep 5
  done
  echo '   post-deploy healthcheck failed'; exit 1
"
echo "==> Done — portal.wetreadwell.com is on the freshly-shipped image."
