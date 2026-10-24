"""Portal CI runs the real-Postgres tests (fix round #21).

tests/test_pg_*.py skip unless PORTAL_TEST_PG_URL is set, and CI never set it: a later change to a
jsonb predicate or an ON CONFLICT in db.py would pass CI and surface as a 500 on staging. The
workflow now has a `postgres` job with a service container; these pin it, and pin that the
existing `test` job is untouched.
"""
import pathlib
from urllib.parse import urlsplit

import pytest

import pg_fixture

yaml = pytest.importorskip("yaml")

ROOT = pathlib.Path(__file__).resolve().parents[2]
CI = ROOT / ".github" / "workflows" / "ci.yml"


def _workflow() -> dict:
    return yaml.safe_load(CI.read_text(encoding="utf-8"))


def test_the_existing_test_job_is_unchanged():
    job = _workflow()["jobs"]["test"]
    assert job == {
        "runs-on": "ubuntu-latest",
        "steps": [
            {"uses": "actions/checkout@v4"},
            {"uses": "actions/setup-python@v5", "with": {"python-version": "3.11"}},
            {"name": "Install dependencies", "working-directory": "backend",
             "run": "pip install -r requirements-dev.txt"},
            {"name": "Run tests", "working-directory": "backend",
             "run": "python -m pytest -q -n auto --dist loadfile"},
        ],
    }


def test_a_postgres_job_runs_every_pg_test_against_a_local_service():
    job = _workflow()["jobs"]["postgres"]
    service = job["services"]["postgres"]
    assert service["image"].startswith("postgres:15")
    assert "pg_isready" in service["options"], "the job must wait for the server"
    url = job["env"][pg_fixture.ENV]
    parts = urlsplit(url)
    # pg_fixture refuses anything that is not a local throwaway server.
    assert parts.hostname in ("localhost", "127.0.0.1")
    assert parts.password == service["env"]["POSTGRES_PASSWORD"]
    assert any(p.endswith(":%d" % parts.port) for p in service["ports"])
    run = [s for s in job["steps"] if "test_pg_" in str(s.get("run", ""))]
    assert len(run) == 1, "exactly one step runs the pg tests"
    script = run[0]["run"]
    assert run[0]["working-directory"] == "backend"
    assert "tests/test_pg_*.py" in script
    assert "set -o pipefail" in script, "tee would hide a failing pytest"
    assert "skipped" in script and "exit 1" in script, "a skipped pg test must fail the job"
    # Every file that glob reaches is a real-Postgres test gated on the same variable.
    files = sorted((ROOT / "backend" / "tests").glob("test_pg_*.py"))
    assert len(files) >= 2
    for f in files:
        assert "needs_pg" in f.read_text(encoding="utf-8"), f.name
