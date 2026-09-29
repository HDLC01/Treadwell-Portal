#!/usr/bin/env bash
# Off-box deploy for the Treadwell Portal (STAGING — the only portal stack
# currently deployed: container treadwell-portal-staging via the staging compose).
#
# The VPS is 1 core / 2 GB; building on it browns out every site. Build the
# image HERE, ship it over SSH, push the compose file (the VPS dir is NOT a git
# checkout), then load + restart (NO --build).
#
# Prereqs: local Docker engine running; a `Host treadwell-vps` entry in your SSH config.
#          The address, login, port and key live in that entry, not in this public repo.
# Usage:   bash deploy/ship.sh
#          VPS_HOST, VPS_USER and SSH_KEY still override it (another box, another key).
set -euo pipefail

VPS_HOST="${VPS_HOST:-treadwell-vps}"
VPS_USER="${VPS_USER:-}"   # empty: the User from the SSH config entry
SSH_KEY="${SSH_KEY:-}"     # empty: the IdentityFile from the SSH config entry
APP_DIR="/opt/treadwell-portal-staging"
IMAGE="treadwell-portal-staging:latest"
COMPOSE="docker-compose.staging.yml"
TARGET="${VPS_USER:+$VPS_USER@}$VPS_HOST"
SSH=(ssh -o ConnectTimeout=20)
SCP=(scp)
if [ -n "$SSH_KEY" ]; then SSH+=(-i "$SSH_KEY"); SCP+=(-i "$SSH_KEY"); fi
SSH+=("$TARGET")

cd "$(dirname "$0")/.."

echo "==> Building $IMAGE locally (off the prod box)…"
docker build --platform linux/amd64 -t "$IMAGE" .

echo "==> Shipping image + compose over SSH…"
docker save "$IMAGE" | gzip | "${SSH[@]}" "cat > /tmp/portal-staging.tar.gz"
"${SCP[@]}" "$COMPOSE" "$TARGET:$APP_DIR/$COMPOSE.new"

echo "==> Load + restart on the VPS (NO build)…"
"${SSH[@]}" "set -euo pipefail
  cd $APP_DIR
  cp -f $COMPOSE $COMPOSE.bak 2>/dev/null || true
  mv -f $COMPOSE.new $COMPOSE
  gunzip -c /tmp/portal-staging.tar.gz | docker load
  rm -f /tmp/portal-staging.tar.gz
  docker compose -f $COMPOSE up -d
  for i in \$(seq 1 24); do
    if curl -fsS http://localhost:8899/healthz >/dev/null; then echo '   portal-staging healthy'; exit 0; fi
    sleep 5
  done
  echo '   post-deploy healthcheck failed'; exit 1
"
echo "==> Done — staging.portal.wetreadwell.com is on the freshly-shipped image."
