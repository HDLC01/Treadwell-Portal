# Shared by deploy/ship.sh and deploy/ship-prod.sh (fix round #6). Sourced AFTER the script has
# cd'd to the repo root; it defines functions only and runs nothing.
#
# Why each check exists:
#   * the image is built from THIS working tree, so a dirty tree ships code no commit holds and
#     nobody can identify afterwards;
#   * prod must be origin/main, not whatever branch the checkout happens to be on;
#   * `docker load` re-points :latest, which leaves the previous image untagged - one
#     `docker image prune` from gone - so it is tagged pre-<sha>-<date> first;
#   * /healthz is green with a failed startup (main._startup logs and swallows it) and with a
#     missing DDL block, so the container log and GET /api/admin/capabilities are checked too.

# refuse_unless_clean: no modified, staged or untracked files.
refuse_unless_clean() {
  local dirty
  if ! dirty="$(git status --porcelain --untracked-files=normal)"; then
    echo "ERROR: git status failed - ship from a git checkout" >&2
    exit 1
  fi
  if [ -n "$dirty" ]; then
    echo "ERROR: the working tree is not clean, so the image would match no commit:" >&2
    printf '%s\n' "$dirty" | head -20 >&2
    echo "Commit or remove these, then ship again." >&2
    exit 1
  fi
}

loud() {
  echo "############################################################################" >&2
  echo "##  $1" >&2
  echo "############################################################################" >&2
}

# refuse_unless_origin_main <allow 0|1>: HEAD must be exactly origin/main, fetched just now.
refuse_unless_origin_main() {
  local allow="$1" head main
  if ! git fetch --quiet origin main; then
    if [ "$allow" = 1 ]; then
      loud "--allow-not-main: could not fetch origin/main; shipping HEAD unverified"
      return 0
    fi
    echo "ERROR: could not fetch origin/main to compare HEAD against it" >&2
    exit 1
  fi
  head="$(git rev-parse HEAD)"
  main="$(git rev-parse origin/main)"
  if [ "$head" != "$main" ]; then
    if [ "$allow" = 1 ]; then
      loud "--allow-not-main: SHIPPING $head, WHICH IS NOT origin/main ($main)"
      return 0
    fi
    echo "ERROR: HEAD ($head) is not origin/main ($main)." >&2
    echo "Prod ships origin/main only. Merge first, or pass --allow-not-main on purpose." >&2
    exit 1
  fi
}

# post_check_script <container> <host port> <allow missing ddl 0|1>: the remote bash that runs
# after `docker compose up -d`. Printed, not run: the caller embeds it in its ssh command.
post_check_script() {
  local tpl
  tpl="$(cat <<'EOF'
  healthy=0
  for i in $(seq 1 @TRIES@); do
    if curl -fsS http://localhost:@PORT@/healthz >/dev/null; then healthy=1; break; fi
    sleep 5
  done
  if [ "$healthy" != 1 ]; then echo '   post-deploy healthcheck failed'; exit 1; fi
  echo '   healthz ok'
  logs="$(docker logs @CONTAINER@ 2>&1 || true)"
  # Here-strings, never `printf | grep -q`: under pipefail a log bigger than the pipe buffer makes
  # printf die of SIGPIPE when grep -q stops at its first match, and the pipeline reads as no match.
  if grep -q 'startup failed' <<<"$logs"; then
    echo '   STARTUP FAILED:'
    printf '%s\n' "$logs" | grep 'startup failed' | tail -3
    exit 1
  fi
  if ! grep -q 'startup ok' <<<"$logs"; then
    echo "   no 'startup ok' line in the container log"
    exit 1
  fi
  worker="$(printf '%s\n' "$logs" | grep -E '\[followup\] (worker started|automation disabled)' | tail -1 || true)"
  if [ -z "$worker" ]; then
    echo '   no follow-up worker line (worker started / automation disabled) in the container log'
    exit 1
  fi
  echo "   follow-up worker: $worker"
  set +e
  caps="$(docker exec @CONTAINER@ python ops/post_deploy_check.py)"
  rc=$?
  set -e
  echo "   capabilities: $caps"
  if [ "$rc" = 3 ] && [ @ALLOW_DDL@ = 1 ]; then
    echo '   WARNING: a DDL block is missing on this database (allowed by --allow-missing-ddl)'
  elif [ "$rc" != 0 ]; then
    echo '   capabilities check failed'
    exit 1
  fi
EOF
)"
  tpl="${tpl//@CONTAINER@/$1}"
  tpl="${tpl//@PORT@/$2}"
  tpl="${tpl//@ALLOW_DDL@/$3}"
  tpl="${tpl//@TRIES@/${CHECK_TRIES:-24}}"
  printf '%s\n' "$tpl"
}
