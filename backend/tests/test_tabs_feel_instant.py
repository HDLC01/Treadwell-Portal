"""Switching between the proposals of one project: still a real page load, but it feels instant.

Hanz, 2026-10-29, on the tabs above the official proposal document: "why does the whole page need to
reload when transferring from one proposal to the other". He chose to keep each tab a REAL load of
that proposal's /p/<token> (the Approve button, the signature latch, the deposit, the chat, the PDF
mounts and the poll all hang off the page's one TOKEN, so a fresh load is what guarantees Approve
can only approve the proposal on screen) and to make the switch feel instant:

  1. no white flash: a cross-document view transition, held by reveal.js until the new page has
     drawn, landing the document card at the height it was clicked at;
  2. a faster load: the shell's JS/CSS cached for a year under a content-hash URL (they were
     re-downloaded on every load), the font stylesheet requested beside styles.css instead of
     @imported from it, and one /api/me/proposals instead of two;
  3. the sibling tabs' page documents prefetched in the background.

THE ONE THING THAT MUST NEVER HAPPEN is a prefetch counting as the customer opening a proposal.
The only "viewed" write is POST /api/portal/{token}/viewed, sent by page JS (signalProposalViewed),
which a prefetch never runs: that is why the rules are prefetch and never prerender. The one write a
plain GET of /p/<token> makes is mark_link_clicked (the board's soft "link followed" signal), and a
speculative GET is refused that too. Executed against the real app and the real frontend files.
"""
import hashlib
import json
import pathlib
import re
import shutil
import subprocess

import pytest
from fastapi.testclient import TestClient

import db
import main

FRONTEND = pathlib.Path(__file__).resolve().parents[2] / "frontend"
HARNESS = pathlib.Path(__file__).resolve().parent / "js" / "tabs-instant-harness.js"


@pytest.fixture
def client():
    return TestClient(main.app)


@pytest.fixture
def writes(monkeypatch):
    """Every write a page load could make, recorded instead of run."""
    seen = []
    monkeypatch.setattr(db, "get_proposal_by_token",
                        lambda t: {"proposal_id": "pid-" + t, "token": t} if t.startswith("tok") else None)
    monkeypatch.setattr(db, "mark_link_clicked", lambda pid: seen.append(("link_clicked", pid)))
    monkeypatch.setattr(db, "mark_viewed", lambda pid: seen.append(("viewed", pid)))
    monkeypatch.setattr(db, "record_view", lambda pid, who: seen.append(("record_view", pid)))
    return seen


# ══ 3. the prefetch can never count as opening the proposal ═══════════════════
@pytest.mark.parametrize("headers", [
    {"Sec-Purpose": "prefetch"},                 # Chromium speculation rules / <link rel=prefetch>
    {"Sec-Purpose": "prefetch;prerender"},       # a prerender, should anything ever ask for one
    {"Purpose": "prefetch"},                     # the older spelling
    {"X-Moz": "prefetch"},                       # Firefox's
])
def test_a_prefetched_tab_records_nothing(client, writes, headers):
    r = client.get("/p/tok-b", headers=headers)
    assert r.status_code == 200
    assert writes == [], "fetching a tab nobody clicked wrote %s" % writes


def test_a_person_opening_the_link_is_still_recorded(client, writes):
    """The complement: without it the test above passes on a route that records nothing at all."""
    r = client.get("/p/tok-b")
    assert r.status_code == 200
    assert writes == [("link_clicked", "pid-tok-b")]


def test_the_page_load_itself_never_marks_viewed(client, writes):
    """Only POST /viewed does, from page JS a prefetch never runs."""
    client.get("/p/tok-b")
    client.get("/p/tok-b", headers={"Sec-Purpose": "prefetch"})
    client.get("/speculation-rules.json")
    assert not [w for w in writes if w[0] != "link_clicked"]


def test_the_rules_are_prefetch_only_and_name_only_the_tabs(client, writes):
    r = client.get("/p/tok-b")
    assert r.headers["speculation-rules"] == '"/speculation-rules.json"'
    rules = client.get("/speculation-rules.json")
    assert rules.headers["content-type"].startswith("application/speculationrules+json")
    body = rules.json()
    assert set(body) == {"prefetch"}, "anything but prefetch would run the page's JS: %s" % set(body)
    assert [x["where"] for x in body["prefetch"]] == [{"selector_matches": "a.scope-tab[href^='/p/']"}]


def test_the_page_is_never_stored_by_a_shared_cache(client, writes):
    """The shell carries nothing per-customer, but its URL carries the customer's token."""
    cc = client.get("/p/tok-b").headers["cache-control"]
    assert "private" in cc and "no-cache" in cc


# ══ 2. the shell's assets: cached for a year under their content hash ══════════
def _refs(html):
    return dict(re.findall(r'(?:src|href)="/([\w.-]+\.(?:js|css))\?v=([0-9a-f]{12})"', html))


def test_every_shell_asset_is_named_by_its_content_hash(client, writes):
    for page, names in (("/p/tok-b", {"styles.css", "reveal.js", "auth.js", "projects.js",
                                      "shell.js", "app.js"}),
                        ("/", {"styles.css", "auth.js", "projects.js", "shell.js", "login.js"})):
        refs = _refs(client.get(page).text)
        assert set(refs) == names, page
        for name, v in refs.items():
            assert v == hashlib.sha256((FRONTEND / name).read_bytes()).hexdigest()[:12], name


def test_only_the_current_hash_is_cached_for_a_year(client, writes):
    v = _refs(client.get("/p/tok-b").text)["app.js"]
    assert "immutable" in client.get("/app.js?v=" + v).headers["cache-control"]
    # A page fetched before a deploy asks for the OLD hash: it must get today's bytes revalidated,
    # never a year-long copy of something that is no longer that version.
    for stale in ("/app.js?v=000000000000", "/app.js"):
        cc = client.get(stale).headers["cache-control"]
        assert "no-cache" in cc and "immutable" not in cc, stale


def test_a_changed_file_gets_a_new_url(monkeypatch, tmp_path, client, writes):
    """What makes the year-long cache safe across a deploy."""
    for f in ("index.html", "app.js", "styles.css", "auth.js", "projects.js", "shell.js", "reveal.js"):
        shutil.copy(FRONTEND / f, tmp_path / f)
    monkeypatch.setattr(main, "FRONTEND_DIR", tmp_path)
    before = _refs(client.get("/p/tok-b").text)["app.js"]
    (tmp_path / "app.js").write_text("// the next deploy\n", encoding="utf-8")
    after = _refs(client.get("/p/tok-b").text)["app.js"]
    assert before != after
    assert "immutable" not in client.get("/app.js?v=" + before).headers["cache-control"]


def test_an_unchanged_page_is_a_304(client, writes):
    r = client.get("/p/tok-b")
    again = client.get("/p/tok-b", headers={"If-None-Match": r.headers["etag"]})
    assert again.status_code == 304 and not again.content
    assert "no-cache" in again.headers["cache-control"]


def test_the_font_stylesheet_is_not_chained_behind_styles_css():
    css = re.sub(r"/\*.*?\*/", "", (FRONTEND / "styles.css").read_text(encoding="utf-8"), flags=re.S)
    assert "@import" not in css, "an @import is only found after styles.css arrives"
    for page in ("index.html", "login.html"):
        html = (FRONTEND / page).read_text(encoding="utf-8")
        head = html[:html.index("</head>")]
        assert 'href="https://fonts.googleapis.com/css2?family=Inter' in head, page


# ══ 1. the cross-fade ══════════════════════════════════════════════════════════
def test_pages_opt_into_the_cross_fade_and_reduced_motion_opts_out():
    css = re.sub(r"/\*.*?\*/", "", (FRONTEND / "styles.css").read_text(encoding="utf-8"), flags=re.S)
    assert re.search(r"@view-transition\s*\{\s*navigation:\s*auto;?\s*\}", css)
    rm = re.search(r"@media \(prefers-reduced-motion: reduce\)\s*\{\s*@view-transition\s*\{\s*"
                   r"navigation:\s*none;?\s*\}\s*\}", css)
    assert rm and rm.start() > css.index("@view-transition"), "reduced motion must come after, to win"
    for sel, name in ((".site-header", "tw-header"), ("#pdf-card", "tw-doc-card"),
                      ("#pv-scope-tabs", "tw-scope-tabs")):
        assert re.search(re.escape(sel) + r"\s*\{\s*view-transition-name:\s*" + name, css), sel


def test_reveal_js_loads_in_the_head():
    """`pagereveal` fires before the first frame; a listener added by the body scripts misses it."""
    html = (FRONTEND / "index.html").read_text(encoding="utf-8")
    assert html.index('<script src="/reveal.js"></script>') < html.index("</head>")


@pytest.fixture(scope="module")
def ran():
    if shutil.which("node") is None:
        pytest.skip("node is not installed")
    proc = subprocess.run(["node", str(HARNESS), str(FRONTEND)],
                          capture_output=True, text=True, encoding="utf-8", timeout=120)
    assert proc.returncode == 0, (
        "the harness itself failed — read this before assuming a product bug:\n" + proc.stderr)
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_the_fade_is_held_until_the_page_is_drawn(ran):
    r = ran["reveal_hold"]
    assert r["listened"]
    assert r["atReveal"] == [[1, 0], [1, 0], [0, 0]], "the fade's animations were not paused"
    assert r["after"] == [[1, 1], [1, 1], [0, 0]], "done() did not play them"
    assert r["afterSecondDone"] == r["after"]


def test_the_hold_is_capped(ran):
    assert ran["reveal_never"]["after"] == [[1, 0], [1, 0], [0, 0]], "nothing should play by itself"
    assert ran["reveal_timeout"]["timers"] == [1500]
    assert ran["reveal_timeout"]["after"] == [[1, 1], [1, 1], [0, 0]], "the cap did not release it"


@pytest.mark.parametrize("scenario", ["no-vt", "drawn-first", "done-before-ready"])
def test_nothing_is_held_when_there_is_nothing_to_wait_for(ran, scenario):
    """No transition (old browser, reduced motion), a back/forward restore of a drawn page, or a
    page that drew before the transition was ready."""
    r = ran["reveal_" + scenario]
    assert r["atReveal"] == [[0, 0]] * 3 and r["after"] == [[0, 0]] * 3 and r["timers"] == []


def test_every_way_out_of_boot_releases_the_fade(ran):
    assert ran["bootOk"] == 1 and ran["bootThrows"] == 1


def test_a_tab_lands_the_card_where_it_was_clicked(ran):
    """Card at 640px in the new page, clicked at 212px: scroll by the difference, and read the
    note once."""
    s = ran["landSaved"]
    assert s["scrollBy"] == [[0, 428]] and s["intoView"] == 0 and s["left"] == {}


@pytest.mark.parametrize("case", ["landNone", "landJunk"])
def test_without_a_usable_note_it_lands_at_the_cards_top(ran, case):
    assert ran[case]["scrollBy"] == [] and ran[case]["intoView"] == 1


def test_only_the_document_landing_reads_the_note(ran):
    r = ran["landOtherHash"]
    assert r["scrollBy"] == [] and r["intoView"] == 0 and r["left"] == {"tw_doc_top": "212"}


def test_a_plain_click_on_a_tab_writes_the_note_and_nothing_else_does(ran):
    assert ran["rememberPlain"] == {"tw_doc_top": "118"}
    for case in ("rememberCtrl", "rememberMiddle", "rememberOffTab"):
        assert ran[case] == {}, case


# ══ 2. nothing fetched twice ═══════════════════════════════════════════════════
def test_the_project_list_is_fetched_once_per_page(ran):
    assert ran["projectsOnce"] == {"calls": 1, "emailFirst": "c@x.com", "list": 1}


@pytest.mark.parametrize("case", ["projectsFailRetry", "projectsSignedOutRetry", "projectsRejectRetry"])
def test_a_failed_or_signed_out_answer_is_asked_again(ran, case):
    assert ran[case]["calls"] == 2 and ran[case]["list"] == 1


def test_the_shell_shares_the_one_request_and_does_not_slide_the_page(ran):
    s = ran["shell"]
    assert s["me"] == 1 and s["fetchedMine"] == 0, "the sidebar fetched /api/me/proposals itself"
    assert s["ops"] == ["add:shell-booting", "toggle:shell-open=true", "layout",
                        "remove:shell-booting"], s["ops"]
    css = (FRONTEND / "shell.js").read_text(encoding="utf-8")
    assert ".shell-booting body{transition:none}" in css
