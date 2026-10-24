"""The ship scripts' safety checks (fix round #6), RUN with every outside command stubbed.

deploy/ship-prod.sh and deploy/ship.sh are executed for real, from a scratch copy of the repo layout,
with `git`, `docker`, `ssh`, `scp`, `curl` and friends replaced by small bash stubs on PATH. The
ssh stub runs the remote half locally in a scratch "box" directory, so the checks that happen on
the VPS (keep the previous image, read the container log, ask the capability route) run too.
Nothing reaches a network, a docker engine or a server.
"""
import os
import pathlib
import re
import shlex
import stat
import subprocess
import sys
import urllib.error

import pytest

from test_no_infra_in_repo import _bash

ROOT = pathlib.Path(__file__).resolve().parents[2]
HEAD = "0123456789abcdef0123456789abcdef01234567"
OTHER = "fedcba9876543210fedcba9876543210fedcba98"
GOOD_LOGS = ("INFO:portal:startup ok (schema_apply=False)\\n"
             "INFO:portal:[followup] automation disabled — worker not started\\n"
             "INFO:     Application startup complete.")

STUBS = {
    "git": r'''
echo "git $*" >> "$STUB_LOG"
case "$1" in
  status) printf '%s' "$STUB_DIRTY" ;;
  fetch) exit "${STUB_FETCH_RC:-0}" ;;
  rev-parse)
    if [ "$2" = origin/main ]; then echo "$STUB_MAIN"
    elif [ "$2" = --short=12 ]; then echo "${STUB_HEAD:0:12}"
    else echo "$STUB_HEAD"; fi ;;
esac
''',
    "docker": r'''
echo "docker $*" >> "$STUB_LOG"
case "$1" in
  save) echo image-bytes ;;
  load) cat >/dev/null; echo "Loaded image" ;;
  image) exit "${STUB_PREV_RC:-0}" ;;
  logs) printf '%b\n' "$STUB_LOGS" ;;
  exec) printf '%s\n' "$STUB_CAPS"; exit "${STUB_EXEC_RC:-0}" ;;
esac
exit 0
''',
    "ssh": r'''
echo "ssh $*" >> "$STUB_LOG"
cmd="${@: -1}"
case "$cmd" in "cat > "*) cat >/dev/null; exit 0 ;; esac
cd "$REMOTE_DIR"
exec bash -c 'cd() { :; }; '"$cmd"
''',
    "scp": r'''
echo "scp $*" >> "$STUB_LOG"
src="${@: -2:1}"; dst="${@: -1}"
cp "$src" "$REMOTE_DIR/$(basename "${dst#*:}")"
''',
    "gzip": "cat\n",
    "gunzip": "exit 0\n",
    "curl": 'echo "curl $*" >> "$STUB_LOG"; exit "${STUB_CURL_RC:-0}"\n',
    "sleep": "exit 0\n",
}


@pytest.fixture
def ship(tmp_path):
    """Run a ship script from a scratch repo copy with stubs. Returns (rc, output, log lines)."""
    bash = _bash()
    repo, box, bin_ = tmp_path / "repo", tmp_path / "box", tmp_path / "bin"
    for d in (repo / "deploy", box, bin_):
        d.mkdir(parents=True)
    for name in ("ship.sh", "ship-prod.sh", "ship-checks.sh"):
        text = (ROOT / "deploy" / name).read_text(encoding="utf-8")
        (repo / "deploy" / name).write_bytes(text.replace("\r\n", "\n").encode("utf-8"))
    for name in ("docker-compose.prod.yml", "docker-compose.staging.yml"):
        (repo / name).write_text("services: {}\n", encoding="utf-8")
    (box / ".env").write_text("SERVICE_TOKEN=never-printed-7f3a\n", encoding="utf-8")
    for name, body in STUBS.items():
        p = bin_ / name
        p.write_bytes(("#!/usr/bin/env bash\n" + body.lstrip("\n")).encode("utf-8"))
        p.chmod(p.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    log = tmp_path / "calls.log"

    def run(script, *args, dirty="", main=HEAD, fetch_rc=0, logs=GOOD_LOGS,
            caps="scope_groups=true group_followups=true version=1", exec_rc=0, curl_rc=0,
            prev_rc=0):
        env = {"STUB_DIRTY": dirty, "STUB_MAIN": main, "STUB_HEAD": HEAD,
               "STUB_FETCH_RC": str(fetch_rc), "STUB_LOGS": logs, "STUB_CAPS": caps,
               "STUB_EXEC_RC": str(exec_rc), "STUB_CURL_RC": str(curl_rc),
               "STUB_PREV_RC": str(prev_rc), "CHECK_TRIES": "2"}
        lines = ["to_posix() { if command -v cygpath >/dev/null 2>&1; then cygpath -u \"$1\"; "
                 "else printf '%s' \"$1\"; fi; }",
                 "unset VPS_HOST VPS_USER SSH_KEY",
                 "export PATH=\"$(to_posix %s):$PATH\"" % shlex.quote(str(bin_)),
                 "export STUB_LOG=\"$(to_posix %s)\"" % shlex.quote(str(log)),
                 "export REMOTE_DIR=\"$(to_posix %s)\"" % shlex.quote(str(box))]
        lines += ["export %s=%s" % (k, shlex.quote(v)) for k, v in env.items()]
        lines += ["cd \"$(to_posix %s)\"" % shlex.quote(str(repo)),
                  "bash deploy/%s %s" % (script, " ".join(shlex.quote(a) for a in args))]
        if log.exists():
            log.unlink()
        proc = subprocess.run([bash, "-s"], input="\n".join(lines).encode("utf-8"),
                              capture_output=True, timeout=120)
        out = (proc.stdout + proc.stderr).decode("utf-8", "replace").replace("\r", "")
        calls = log.read_text(encoding="utf-8").splitlines() if log.exists() else []
        return proc.returncode, out, calls

    return run


def _built(calls):
    return [c for c in calls if c.startswith("docker build")]


@pytest.mark.parametrize("script", ["ship.sh", "ship-prod.sh"])
def test_a_dirty_tree_is_refused_before_anything_is_built(ship, script):
    rc, out, calls = ship(script, dirty=" M backend/main.py\n?? scratch.txt\n")
    assert rc == 1 and "not clean" in out and "scratch.txt" in out
    assert _built(calls) == [] and not any(c.startswith("ssh") for c in calls)


def test_prod_refuses_a_head_that_is_not_origin_main(ship):
    rc, out, calls = ship("ship-prod.sh", main=OTHER)
    assert rc == 1 and "is not origin/main" in out and _built(calls) == []
    assert "git fetch --quiet origin main" in calls


def test_prod_refuses_when_origin_main_cannot_be_fetched(ship):
    rc, out, calls = ship("ship-prod.sh", fetch_rc=1)
    assert rc == 1 and "could not fetch origin/main" in out and _built(calls) == []


def test_the_override_ships_anyway_and_says_so_loudly(ship):
    rc, out, calls = ship("ship-prod.sh", "--allow-not-main", main=OTHER)
    assert rc == 0, out
    assert "#####" in out and "WHICH IS NOT origin/main" in out and len(_built(calls)) == 1


def test_staging_does_not_require_origin_main(ship):
    rc, out, calls = ship("ship.sh", main=OTHER)
    assert rc == 0, out
    assert not any(c.startswith("git fetch") for c in calls)


@pytest.mark.parametrize("script,image,port", [
    ("ship-prod.sh", "treadwell-portal", 8898),
    ("ship.sh", "treadwell-portal-staging", 8899)])
def test_a_good_ship_tags_the_commit_keeps_the_previous_image_and_checks_the_box(
        ship, script, image, port):
    rc, out, calls = ship(script)
    assert rc == 0, out
    sha = HEAD[:12]
    build = _built(calls)
    assert len(build) == 1
    assert "--label org.opencontainers.image.revision=%s" % HEAD in build[0]
    assert "-t %s:latest -t %s:%s" % (image, image, sha) in build[0]
    assert "docker save %s:latest %s:%s" % (image, image, sha) in calls
    tag = [i for i, c in enumerate(calls)
           if re.fullmatch(r"docker tag %s:latest %s:pre-%s-\d{8}T\d{4}" % (image, image, sha), c)]
    load = [i for i, c in enumerate(calls) if c == "docker load"]
    assert tag and load and tag[0] < load[0], "the previous image must be tagged BEFORE the load"
    assert "curl -fsS http://localhost:%d/healthz" % port in calls
    assert "docker exec %s python ops/post_deploy_check.py" % image in calls
    assert "follow-up worker: INFO:portal:[followup] automation disabled" in out
    assert "capabilities: scope_groups=true group_followups=true version=1" in out
    assert "is on %s" % sha in out and "docker tag %s:pre-%s-" % (image, sha) in out
    assert "never-printed-7f3a" not in out


@pytest.mark.parametrize("case,kwargs,why", [
    ("no startup line", {"logs": "INFO:portal:[followup] worker started (tick=900s)"},
     "no 'startup ok' line"),
    ("startup failed", {"logs": "ERROR:portal:startup failed: relation does not exist\\n"
                                "INFO:portal:startup ok"}, "STARTUP FAILED"),
    ("no worker line", {"logs": "INFO:portal:startup ok (schema_apply=False)"},
     "no follow-up worker line"),
    ("ddl missing", {"caps": "scope_groups=true group_followups=false version=1",
                     "exec_rc": 3}, "capabilities check failed"),
    ("route unreachable", {"caps": "capabilities: HTTP 401", "exec_rc": 2},
     "capabilities check failed"),
    ("healthz down", {"curl_rc": 7}, "post-deploy healthcheck failed"),
])
def test_a_failed_check_exits_non_zero_and_prints_the_rollback(ship, case, kwargs, why):
    _failed_check(ship, "ship-prod.sh", case, kwargs, why)


def test_staging_shares_the_checks(ship):
    _failed_check(ship, "ship.sh", "ddl missing", {"exec_rc": 3}, "capabilities check failed")


def _failed_check(ship, script, case, kwargs, why):
    rc, out, _calls = ship(script, **kwargs)
    assert rc == 1, (case, out)
    assert why in out, (case, out)
    assert "POST-DEPLOY CHECK FAILED" in out
    rollback = [line for line in out.splitlines() if "docker tag" in line and ":pre-" in line
                and "up -d" in line]
    assert rollback, out
    assert "cp -f docker-compose" in rollback[0] and ".bak" in rollback[0]


@pytest.mark.parametrize("script", ["ship.sh", "ship-prod.sh"])
def test_missing_ddl_can_be_allowed_on_purpose_and_is_still_flagged(ship, script):
    rc, out, _calls = ship(script, "--allow-missing-ddl",
                           caps="scope_groups=false group_followups=false version=1", exec_rc=3)
    assert rc == 0, out
    assert "WARNING: a DDL block is missing" in out
    # Only a MISSING DDL is downgraded; a route that cannot be asked still fails.
    rc, out, _calls = ship(script, "--allow-missing-ddl", caps="capabilities: HTTP 500",
                           exec_rc=2)
    assert rc == 1


@pytest.mark.parametrize("script", ["ship.sh", "ship-prod.sh"])
def test_an_unknown_option_is_refused(ship, script):
    rc, out, calls = ship(script, "--force")
    assert rc == 2 and "unknown option" in out and _built(calls) == []


# ── the in-container capability check ────────────────────────────────────────────
sys.path.insert(0, str(ROOT / "backend" / "ops"))
import post_deploy_check  # noqa: E402


class _Resp:
    def __init__(self, body):
        self.body = body

    def read(self):
        return self.body.encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _opener(body=None, exc=None, seen=None):
    def urlopen(req, timeout=None):
        if seen is not None:
            seen.append(req)
        if exc:
            raise exc
        return _Resp(body)
    return urlopen


def test_the_post_deploy_check_asks_with_the_containers_token_and_never_prints_it():
    seen = []
    code, line = post_deploy_check.check(
        _opener('{"ok": true, "scope_groups": true, "group_followups": true, "version": 1}', seen=seen),
        {"SERVICE_TOKEN": "tok-secret-9"})
    assert code == 0 and line == "scope_groups=true group_followups=true version=1"
    assert seen[0].full_url == "http://127.0.0.1:8898/api/admin/capabilities"
    assert seen[0].get_header("X-service-token") == "tok-secret-9"
    assert "tok-secret-9" not in line


@pytest.mark.parametrize("body,code", [
    ('{"ok": true, "scope_groups": true, "group_followups": false, "version": 1}', 3),
    ('{"ok": true, "scope_groups": false, "group_followups": false, "version": 1}', 3),
    ('{"ok": false, "error": "unauthorized"}', 2),
    ("not json", 2),
])
def test_the_post_deploy_check_codes(body, code):
    assert post_deploy_check.check(_opener(body), {"SERVICE_TOKEN": "t"})[0] == code


def test_the_post_deploy_check_without_a_token_or_a_route():
    assert post_deploy_check.check(_opener("{}"), {})[0] == 2
    err = urllib.error.HTTPError("u", 401, "no", {}, None)
    code, line = post_deploy_check.check(_opener(exc=err), {"SERVICE_TOKEN": "t"})
    assert code == 2 and "401" in line


def test_the_scripts_run_the_check_from_the_images_workdir():
    """The image's WORKDIR is /app/backend and backend/ is copied whole, so ops/ is there."""
    docker = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "WORKDIR /app/backend" in docker and "COPY backend/ /app/backend/" in docker
    assert (ROOT / "backend" / "ops" / "post_deploy_check.py").is_file()
    assert os.path.basename(post_deploy_check.__file__) == "post_deploy_check.py"
