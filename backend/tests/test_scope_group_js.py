"""What a customer READS for one scope of a multi-scope project (RJ's scope groups).

Executed through the real app.js / projects.js functions (tests/js/scope-group-harness.js), not
grepped: the claim is about the rendered words.

  * The main pricing row of a scope is named after the scope ("Lab 1"), not "Base bid" -- there is
    no base bid in a project priced as separate scopes. Every other proposal still says "Base bid"
    (and test_portal_pricing_labels.py keeps pinning that).
  * The header says "Acme HQ — Lab 1", and lists the other scopes as links, current one marked.
  * The project list prints the display name the server sends.
"""
import json
import pathlib
import shutil
import subprocess

import pytest

FRONTEND = pathlib.Path(__file__).resolve().parents[2] / "frontend"
HARNESS = pathlib.Path(__file__).resolve().parent / "js" / "scope-group-harness.js"


@pytest.fixture(scope="module")
def ran():
    if shutil.which("node") is None:
        pytest.skip("node is not installed")
    proc = subprocess.run(["node", str(HARNESS), str(FRONTEND)],
                          capture_output=True, text=True, encoding="utf-8", timeout=120)
    assert proc.returncode == 0, (
        "the harness itself failed — read this before assuming a product bug:\n" + proc.stderr)
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_a_scope_names_its_main_row_after_the_scope(ran):
    rows = ran["kindsScoped"]
    assert [r["kind"] for r in rows] == ["Lab 1", "Option"]
    assert rows[0]["cls"] == "is-base"
    assert "approved on its own" in rows[0]["title"]


def test_an_ordinary_proposal_still_says_base_bid(ran):
    assert [r["kind"] for r in ran["kindsPlain"]] == ["Base bid", "Option"]


def test_the_header_names_the_scope_only_for_a_scope(ran):
    assert ran["titleScoped"] == {"p": "Acme HQ — Lab 1", "pv": "Acme HQ — Lab 1"}
    assert ran["titlePlain"] == {"p": "Acme HQ", "pv": "Acme HQ"}


def test_the_sibling_strip_links_the_other_scopes(ran):
    s = ran["stripScoped"]
    assert s["hidden"] is False
    h = s["html"]
    assert 'href="/p/tok-a"' in h and 'href="/p/tok-c"' in h
    assert 'href="/p/tok-b"' not in h, "the scope you are on links to itself"
    assert 'aria-current="page">Lab &lt;1&gt;<' in h, "the current scope is unmarked or unescaped"
    assert "Breakroom · approved" in h
    assert "3 separate scopes" in h


def test_no_strip_for_an_ordinary_proposal_or_a_lone_scope(ran):
    assert ran["stripNone"] == {"html": "", "hidden": True}
    assert ran["stripAlone"]["hidden"] is True


def test_the_project_list_prints_the_display_name(ran):
    h = ran["rows"]
    assert ">Acme HQ — Breakroom<" in h and "is-scope" in h
    assert ">Warehouse<" in h
    assert ">Old card<" in h, "a card without display_name lost its name"
    assert h.count("is-scope") == 1
