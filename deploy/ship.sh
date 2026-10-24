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
# Usage:   bash deploy/ship.sh [--allow-missing-ddl]
#          VPS_HOST, VPS_USER and SSH_KEY still override it (another box, another key).
#
# Refuses to build unless the tree is clean. The image is tagged with its commit; the previous
# image is kept on the box as treadwell-portal-staging:pre-<sha>-<date>. After the restart it
# checks /healthz, the container log ('startup ok' and the follow-up worker line) and
# GET /api/admin/capabilities (asked from inside the container, so the service token never leaves
# it). A failed check exits non-zero and prints the rollback command. See deploy/ship-checks.sh.
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
# shellcheck source=deploy/ship-checks.sh
. deploy/ship-checks.sh

ALLOW_MISSING_DDL=0
for arg in "$@"; do
  case "$arg" in
    --allow-missing-ddl) ALLOW_MISSING_DDL=1 ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done
CONTAINER="treadwell-portal-staging"

echo "==> Checking the tree…"
refuse_unless_clean
SHA="$(git rev-parse --short=12 HEAD)"
FULL_SHA="$(git rev-parse HEAD)"
SHA_TAG="${IMAGE%:*}:$SHA"
PREV_TAG="${IMAGE%:*}:pre-$SHA-$(date -u +%Y%m%dT%H%M)"

echo "==> Building $IMAGE ($SHA) locally (off the prod box)…"
docker build --platform linux/amd64 --label "org.opencontainers.image.revision=$FULL_SHA"   -t "$IMAGE" -t "$SHA_TAG" .

echo "==> Shipping image + compose over SSH…"
docker save "$IMAGE" "$SHA_TAG" | gzip | "${SSH[@]}" "cat > /tmp/portal-staging.tar.gz"
"${SCP[@]}" "$COMPOSE" "$TARGET:$APP_DIR/$COMPOSE.new"

ROLLBACK="${SSH[*]} 'cd $APP_DIR && cp -f $COMPOSE.bak $COMPOSE && docker tag $PREV_TAG $IMAGE && docker compose -f $COMPOSE up -d'"
echo "==> Load + restart on the VPS (NO build)…"
if ! "${SSH[@]}" "set -euo pipefail
  cd $APP_DIR
  cp -f $COMPOSE $COMPOSE.bak 2>/dev/null || true
  mv -f $COMPOSE.new $COMPOSE
  if docker image inspect $IMAGE >/dev/null 2>&1; then
    docker tag $IMAGE $PREV_TAG
    echo '   previous image kept as $PREV_TAG'
  else
    echo '   no previous image to keep'
  fi
  gunzip -c /tmp/portal-staging.tar.gz | docker load
  rm -f /tmp/portal-staging.tar.gz
  docker compose -f $COMPOSE up -d
$(post_check_script "$CONTAINER" 8899 "$ALLOW_MISSING_DDL")
"; then
  echo "==> POST-DEPLOY CHECK FAILED on $SHA. To roll back to the previous image:" >&2
  echo "    $ROLLBACK" >&2
  exit 1
fi
echo "==> Done — staging.portal.wetreadwell.com is on $SHA. Rollback, if ever needed:"
echo "    $ROLLBACK"
