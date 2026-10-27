"""Ask the freshly deployed portal what it can do, from INSIDE its own container (fix round #6).

deploy/ship.sh and deploy/ship-prod.sh run it after the new container is healthy:

    docker exec <container> python ops/post_deploy_check.py

It calls GET /api/admin/capabilities on the app's own port with the SERVICE_TOKEN the container
already has in its environment, so the token never leaves the box and is never printed. /healthz
cannot see a missing DDL block; this can (`scope_groups` reads the scope columns, `group_followups`
the remind-only CHECK and indexes).

Prints one line, `scope_groups=<bool> group_followups=<bool> version=<n>`, and exits
  0  both true
  3  the route answered but one of them is false (a DDL block is missing on this database)
  2  the route could not be asked (not up, unauthorized, not JSON)
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

URL = "http://127.0.0.1:8898/api/admin/capabilities"


def check(urlopen=urllib.request.urlopen, env=None) -> tuple:
    """(exit code, the line to print). Never includes the token."""
    env = os.environ if env is None else env
    token = env.get("SERVICE_TOKEN") or ""
    if not token:
        return 2, "capabilities: SERVICE_TOKEN is not set in this container"
    req = urllib.request.Request(URL, headers={"X-Service-Token": token})
    try:
        with urlopen(req, timeout=10) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return 2, "capabilities: HTTP %s" % exc.code
    except Exception as exc:  # noqa: BLE001
        return 2, "capabilities: could not ask (%s)" % type(exc).__name__
    if not isinstance(body, dict) or body.get("ok") is not True:
        return 2, "capabilities: unexpected answer"
    sg, gf = body.get("scope_groups") is True, body.get("group_followups") is True
    line = "scope_groups=%s group_followups=%s version=%s" % (
        str(sg).lower(), str(gf).lower(), body.get("version"))
    return (0 if sg and gf else 3), line


def main() -> int:
    code, line = check()
    print(line)
    return code


if __name__ == "__main__":
    sys.exit(main())
