#!/usr/bin/env bash
# Off-box deploy for the Treadwell Portal (STAGING — the only portal stack
# currently deployed: container treadwell-portal-staging via the staging compose).
#
# The VPS is 1 core / 2 GB; building on it browns out every site. Build the
# image HERE, ship it over SSH, push the compose file (the VPS dir is NOT a git
# checkout), then load + restart (NO --build).
#
# Prereqs: local Docker engine running; SSH key at ~/.ssh/treadwell_vps.
# Usage:   bash deploy/ship.sh
set -euo pipefail

VPS_HOST="${VPS_HOST:-50.6.110.215}"
VPS_USER="${VPS_USER:-root}"
SSH_KEY="${SSH_KEY:-$HOME/.ssh/treadwell_vps}"
APP_DIR="/opt/treadwell-portal-staging"
IMAGE="treadwell-portal-staging:latest"
COMPOSE="docker-compose.staging.yml"
SSH=(ssh -i "$SSH_KEY" -o ConnectTimeout=20 "${VPS_USER}@${VPS_HOST}")

cd "$(dirname "$0")/.."

echo "==> Building $IMAGE locally (off the prod box)…"
docker build --platform linux/amd64 -t "$IMAGE" .

echo "==> Shipping image + compose over SSH…"
docker save "$IMAGE" | gzip | "${SSH[@]}" "cat > /tmp/portal-staging.tar.gz"
scp -i "$SSH_KEY" "$COMPOSE" "${VPS_USER}@${VPS_HOST}:$APP_DIR/$COMPOSE.new"

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
