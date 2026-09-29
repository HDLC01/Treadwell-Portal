"""The public repo must not publish how to reach the server.

The repos stay public (Hanz, 2026-09-28). An address, a login, a port or a key filename in a
committed file hands anyone scanning GitHub the box and the account to try. Those details live
in the owner's SSH config under the alias `treadwell-vps`, and `deploy/ship.sh` and
`deploy/ship-prod.sh` point at the alias. Git history still holds the old values; these tests
pin the files, not the past.

The scan covers the files people write infrastructure down in (docs, scripts, CI, compose).
Python sources are left out: test fixtures carry real mail-server addresses on purpose.
"""
import ipaddress
import pathlib
import re
import shlex
import shutil
import subprocess

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
SCRIPTS = ["ship.sh", "ship-prod.sh"]
ALIAS = "treadwell-vps"

SUFFIXES = {".md", ".sh", ".yml", ".yaml", ".conf", ".txt", ".ps1", ".example", ".toml",
            ".ini", ".cfg", ".env"}
NO_SUFFIX_DIRS = ("deploy", "ops")
# Four dotted numbers, not part of a longer dotted run (a five-part version string).
IPV4 = re.compile(r"(?<![\w.])(\d{1,3}(?:\.\d{1,3}){3})(?!\.?\w)")
ROOT_LOGIN = re.compile(r"\broot@")
# A key file under ~/.ssh names the key. `~/.ssh/config` is where the alias lives, so it may be named.
KEY_PATH = re.compile(r"\.ssh/(?!config\b)[\w.-]+")

# The SSH port and a root login take more shapes than an address. Port 22 is every server's
# default and gives nothing away, so it may be written (deploy.yml falls back to it).
SSH_WORD = re.compile(r"\b(?:ssh|scp|sftp|rsync)\b", re.I)
# Anywhere: `ssh://box:2222`, `VPS_PORT="${VPS_PORT:-2222}"`, "SSH listens on port 2222".
PORT_ANYWHERE = (
    re.compile(r"\bssh://[^\s/]*:(\d+)", re.I),
    re.compile(r"\b(?:SSH|VPS)_PORT\b[^\n]{0,20}?(?:[=:]-?|\|\|)\s*[\"']?(\d+)"),
    re.compile(r"\bssh\b[^\n]{0,30}?(?<!-)\bport\s*[:=]?\s*(\d+)", re.I),
)
# On a line that runs ssh or scp: `-p 2222`, `-P 2222`, `-o Port=2222`. A docker `-p 8888:8888`
# run over ssh publishes a container port, so a number followed by a colon is not one.
PORT_FLAG = re.compile(r"(?:^|\s)-(?:[pP]\s*|o\s*Port[=\s]\s*)(\d+)(?![\d:])")
# In a file about SSH: an ssh_config `Port 2222` line or a deploy action's `port: 2222` key.
# A shell script's upper-case `PORT=8898` is the app's port, so that spelling is left alone.
PORT_KEY = re.compile(r"^\s*(?!PORT\b)(?i:port)(?:\s*[:=]\s*[\"']?|\s+)(\d+)[\"']?\s*(?:#.*)?$")
# A root login without the `@`: `VPS_USER="${VPS_USER:-root}"` anywhere, `ssh -l root` on an ssh
# line, and in a file about SSH an ssh_config `User root` or a deploy action's `username: root`.
# A Dockerfile's upper-case `USER root` is the container's user, not a login.
ROOT_VAR = re.compile(r"\b(?:SSH|VPS)_USER\b[^\n]{0,20}?[=:]-?\s*[\"']?root\b")
ROOT_FLAG = re.compile(r"(?:^|\s)-l\s*root\b")
ROOT_KEY = re.compile(r"^\s*(?!USER\b)(?i:user(?:name)?)(?:\s*[:=]\s*[\"']?|\s+)root\b")


def _tracked_text_files():
    try:
        proc = subprocess.run(["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        pytest.skip("git is not available")
    if proc.returncode != 0:
        pytest.skip("not a git checkout")
    for rel in filter(None, proc.stdout.decode("utf-8").split("\0")):
        p = pathlib.PurePosixPath(rel)
        if p.suffix.lower() in SUFFIXES or p.name.startswith("Dockerfile") or p.parts[0] in NO_SUFFIX_DIRS:
            yield rel


def _findings(rel, text):
    about_ssh = bool(SSH_WORD.search(text))
    for number, line in enumerate(text.splitlines(), 1):
        for m in IPV4.finditer(line):
            try:
                public = ipaddress.ip_address(m.group(1)).is_global
            except ValueError:
                continue
            if public:
                yield "%s:%d public IP address" % (rel, number)
        if ROOT_LOGIN.search(line):
            yield "%s:%d root@ login" % (rel, number)
        if KEY_PATH.search(line):
            yield "%s:%d SSH key file path" % (rel, number)
        ssh_line = bool(SSH_WORD.search(line))
        ports = [p for rx in PORT_ANYWHERE for p in rx.findall(line)]
        if ssh_line:
            ports += PORT_FLAG.findall(line)
        if about_ssh:
            ports += PORT_KEY.findall(line)
        if any(int(p) != 22 for p in ports):
            yield "%s:%d SSH port" % (rel, number)
        if (ROOT_VAR.search(line) or (ssh_line and ROOT_FLAG.search(line))
                or (about_ssh and ROOT_KEY.search(line))):
            yield "%s:%d root login" % (rel, number)


def test_no_tracked_doc_or_script_says_how_to_reach_the_server():
    scanned, found = 0, []
    for rel in _tracked_text_files():
        path = ROOT / rel
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        scanned += 1
        found.extend(_findings(rel, text))
    # The scan has to have read the files it exists for, or an empty `found` means nothing.
    assert scanned > 8, "the scan read almost nothing"
    assert not found, "use the `treadwell-vps` SSH alias instead:\n" + "\n".join(found)


def test_the_scan_flags_each_kind_of_detail():
    """A green scan only means something if it can go red on the shapes it looks for."""
    text = "\n".join([
        "ssh root@<vps-ip>",
        'VPS_HOST="${VPS_HOST:-8.8.8.8}"',
        'SSH_KEY="$HOME/.ssh/some_key"',
        "fine: 127.0.0.1, 10.0.0.5, 203.0.113.9, v1.2.3.4.5, ~/.ssh/config, ssh treadwell-vps",
        "ssh -p 2222 treadwell-vps",
        'VPS_PORT="${VPS_PORT:-2222}"',
        "ssh -l root -p 2222 treadwell-vps",
        "scp -P 2222 docker-compose.yml treadwell-vps:/opt/app/",
        "    Port 2222",
        "    User root",
        "          port: 2222",
        "          username: root",
        'VPS_USER="${VPS_USER:-root}"',
        "SSH listens on port 2222 now",
        "rsync -e 'ssh -o Port=2222' out/ treadwell-vps:",
        "git remote add vps ssh://git@treadwell-vps:2222/app.git",
        # Not a detail: port 22, the CI fallback, app and database ports, a container's user.
        "          port: ${{ secrets.VPS_PORT || '22' }}   # SSH moved off 22 on 2026-09-28",
        "ssh -p 22 treadwell-vps",
        'ssh treadwell-vps "docker run -p 8888:8888 app"',
        'ssh treadwell-vps "uvicorn app --port 8888"',
        "PORT=8898",
        "USER root",
        "ls -l root",
        "psql -h db -p 5432",
    ])
    assert list(_findings("x.md", text)) == [
        "x.md:1 root@ login", "x.md:2 public IP address", "x.md:3 SSH key file path",
        "x.md:5 SSH port", "x.md:6 SSH port", "x.md:7 SSH port", "x.md:7 root login",
        "x.md:8 SSH port", "x.md:9 SSH port", "x.md:10 root login", "x.md:11 SSH port",
        "x.md:12 root login", "x.md:13 root login", "x.md:14 SSH port", "x.md:15 SSH port",
        "x.md:16 SSH port"]
    # In a file that never mentions SSH, a service's `user: root` and `port:` are the container's.
    compose = "services:\n  db:\n    user: root\n    port: 5432\n"
    assert list(_findings("docker-compose.yml", compose)) == []


def _bash():
    """A bash that can run a script. On Windows the first `bash` on PATH is often the WSL
    stub, which fails without a distro, so Git for Windows' own bash is tried as well."""
    candidates = [shutil.which("bash")]
    git = shutil.which("git")
    if git:
        git_root = pathlib.Path(git).resolve().parents[1]
        candidates += [str(git_root / "bin" / "bash.exe"), str(git_root / "usr" / "bin" / "bash.exe")]
    for exe in filter(None, candidates):
        try:
            probe = subprocess.run([exe, "-c", "echo ok"], capture_output=True, timeout=30)
        except (OSError, subprocess.TimeoutExpired):
            continue
        if probe.returncode == 0 and probe.stdout.strip() == b"ok":
            return exe
    pytest.skip("no bash that can run a script here")


def _ship_argv(script, **env):
    """Run a ship script's own settings block (everything before it cds to the repo root) and
    return the ssh command, the scp command and the scp target it would use. Values are set
    inside the script, not through the process environment, so a bash that does not inherit
    Windows variables still sees them."""
    text = (ROOT / "deploy" / script).read_text(encoding="utf-8")  # CRLF checkout reads as LF
    head, sep, _ = text.partition('\ncd "$(dirname "$0")/.."')
    assert sep, "%s no longer cds to the repo root, so its settings cannot be isolated" % script
    lines = ["unset VPS_HOST VPS_USER SSH_KEY"]
    lines += ["export %s=%s" % (k, shlex.quote(v)) for k, v in env.items()]
    body = ("\n".join(lines) + "\n" + head + "\n"
            + 'printf "%s\\n" "${SSH[@]}"; echo --; printf "%s\\n" "${SCP[@]}"; echo --; echo "$TARGET"\n')
    proc = subprocess.run([_bash(), "-s"], input=body.encode("utf-8"), capture_output=True, timeout=60)
    assert proc.returncode == 0, proc.stderr.decode("utf-8", "replace")
    ssh, scp, target = proc.stdout.decode("utf-8").replace("\r", "").split("--\n")
    return ssh.splitlines(), scp.splitlines(), target.strip()


@pytest.mark.parametrize("script", SCRIPTS)
def test_the_deploy_reaches_the_box_through_the_ssh_alias(script):
    # Only the alias: no user, port or key on the command line, so ~/.ssh/config supplies them.
    ssh, scp, target = _ship_argv(script)
    assert ssh == ["ssh", "-o", "ConnectTimeout=20", ALIAS]
    assert scp == ["scp"]
    assert target == ALIAS


@pytest.mark.parametrize("script", SCRIPTS)
def test_the_deploy_still_takes_an_explicit_box_user_and_key(script):
    ssh, scp, target = _ship_argv(script, VPS_HOST="203.0.113.9", VPS_USER="deploy",
                                  SSH_KEY="/keys/a key")
    assert ssh == ["ssh", "-o", "ConnectTimeout=20", "-i", "/keys/a key", "deploy@203.0.113.9"]
    assert scp == ["scp", "-i", "/keys/a key"]
    assert target == "deploy@203.0.113.9"


@pytest.mark.parametrize("script", SCRIPTS)
def test_the_compose_copy_goes_to_the_same_box_as_the_ssh_steps(script):
    """scp is the one step outside the SSH array, so it is the one that could drift back to a
    hard-coded login."""
    text = (ROOT / "deploy" / script).read_text(encoding="utf-8")
    copies = [line.strip() for line in text.splitlines() if "COMPOSE.new" in line and "scp" in line.lower()]
    assert copies == ['"${SCP[@]}" "$COMPOSE" "$TARGET:$APP_DIR/$COMPOSE.new"']
