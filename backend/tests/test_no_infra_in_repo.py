"""The public repo must not publish how to reach the server.

The repos stay public (Hanz, 2026-09-28). An address, a login name or a key filename in a
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
    ])
    assert list(_findings("x.md", text)) == [
        "x.md:1 root@ login", "x.md:2 public IP address", "x.md:3 SSH key file path"]


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
