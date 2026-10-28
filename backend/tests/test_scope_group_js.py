"""What a customer READS for one scope of a multi-scope project (RJ's scope groups).

Executed through the real app.js / projects.js functions (tests/js/scope-group-harness.js), not
grepped: the claim is about the rendered words.

  * The main pricing row of a scope is named after the scope ("Lab 1"), not "Base bid" -- there is
    no base bid in a project priced as separate scopes. Every other proposal still says "Base bid"
    (and test_portal_pricing_labels.py keeps pinning that).
  * The page heading says "Acme HQ — Lab 1".
  * ONE PROJECT (Hanz, 2026-10-27): the Official proposal document card has a tab per proposal
    this customer can see, the current one selected and approved ones marked; a tab opens that
    proposal's page at the document, whose Status card and Approve button name it. The project
    list and the header switcher show the project once, by its name, from any of its proposals.
"""
import json
import pathlib
import re
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
    assert ran["titleScoped"]["p"] == ran["titleScoped"]["pv"] == "Acme HQ — Lab 1"
    assert ran["titlePlain"]["p"] == ran["titlePlain"]["pv"] == "Acme HQ"


# ══ one project, one tab per proposal (Hanz, 2026-10-27) ══════════════════════
def test_the_document_card_has_one_tab_per_proposal_the_current_one_selected(ran):
    t = ran["tabsScoped"]
    assert t["hidden"] is False
    h = t["html"]
    assert len(re.findall(r'class="scope-tab[ "]', h)) == 3, "one tab per proposal this customer can see"
    assert 'href="/p/tok-a#proposal/proposal/document"' in h
    assert 'href="/p/tok-c#proposal/proposal/document"' in h
    assert "/p/tok-b" not in h, "the selected tab links to itself"
    assert ('<span class="scope-tab is-current" aria-current="page">Lab &lt;1&gt;</span>' in h), \
        "the selected tab is unmarked or its estimator-typed label went out unescaped"
    assert h.count("is-current") == 1
    # Approved is marked on the approved proposal's tab, and only there.
    assert 'Breakroom <span class="scope-tab-flag">Approved</span></a>' in h
    assert h.count("Approved") == 1
    assert "Each proposal is approved on its own." in h


def test_no_tabs_for_an_ordinary_proposal_or_a_lone_proposal(ran):
    assert ran["tabsNone"] == {"html": "", "hidden": True}
    assert ran["tabsAlone"]["hidden"] is True, "a tab row with nothing to switch to"


def test_the_old_chip_strip_is_gone():
    html = (FRONTEND / "index.html").read_text(encoding="utf-8")
    app = (FRONTEND / "app.js").read_text(encoding="utf-8")
    assert 'id="pv-scopes"' not in html and "renderScopeStrip" not in app
    # The tab row sits INSIDE the Official proposal document card, above the document.
    card = html[html.index('id="pdf-card"'):html.index('id="options-card"')]
    assert card.index('id="pv-scope-tabs"') < card.index('id="pdf-preview"')


def test_the_status_and_approve_cards_name_their_proposal(ran):
    assert ran["titleScoped"]["status"] == "Status: Lab 1"
    assert ran["titleScoped"]["approve"] == "Approve Lab 1"
    assert ran["titlePlain"]["status"] == "Status"
    assert ran["titlePlain"]["approve"] == "Approve this proposal"


def test_switching_a_tab_shows_that_proposals_document_and_its_own_approve(ran):
    """The Lab 2 tab, followed from the href the tab row really rendered on Lab <1>'s page: the
    page that loads is Lab 2's -- its document, its Status card, its Approve button."""
    s = ran["switched"]
    assert s["token"] == "tok-c"
    assert s["pdf"].startswith("/api/portal/tok-c/pdf"), "the document is another proposal's"
    assert s["status"] == "Status: Lab 2"
    assert s["approve"] == "Approve Lab 2"
    assert s["button"] == "Sign and approve Lab 2"
    assert '<span class="scope-tab is-current" aria-current="page">Lab 2</span>' in s["tabs"]
    assert 'href="/p/tok-b#proposal/proposal/document"' in s["tabs"], "no way back to Lab 1"
    # It lands on the proposal step, at the document card, and only once (a poll re-render must
    # not move the page).
    assert ran["tabHashView"] == {"step": "proposal", "proposalView": True}
    assert s["scrolled"] == 1


def test_an_ordinary_proposal_page_is_unchanged(ran):
    p = ran["plainPage"]
    assert p["tabs"] == "" and p["status"] == "Status"
    assert p["approve"] == "Approve this proposal"
    assert p["button"] == "Sign and approve proposal"
    assert p["scrolled"] == 0


def test_the_project_list_shows_a_project_of_several_proposals_once(ran):
    h = ran["rows"]
    assert h.count('class="proj-row') == 3
    assert ">Acme &lt;HQ&gt;<" in h, "the project row is unnamed or unescaped"
    assert "Breakroom" not in h and "Lab 1" not in h, "a proposal's name in the project list"
    assert 'href="/p/t1"' in h and "/p/t1b" not in h, "the project opens its first proposal"
    assert ">Warehouse<" in h
    assert ">Old card<" in h, "a card without display_name lost its name"
    assert ">1 of 2 approved<" in h


def test_the_switcher_marks_the_project_current_from_any_of_its_proposals(ran):
    h = ran["rowsOnSecondProposal"]
    assert h.count('aria-current="page"') == 1
    assert '<a class="proj-row current" href="/p/t1" aria-current="page">' in h
    assert ran["foundFromSecond"] == "Acme <HQ>", "the header would not name the project"
    assert ran["foundSolo"] == "t2"


def test_a_projects_badge_reads_across_its_proposals(ran):
    assert ran["groupStatus"] == {"label": "1 of 2 approved", "cls": "warn"}
    assert ran["groupAllNew"] == {"label": "New proposal", "cls": "pending"}
    assert ran["groupAllApproved"] == {"label": "Deposit due", "cls": "warn"}
