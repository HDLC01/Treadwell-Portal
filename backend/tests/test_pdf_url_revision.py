"""A re-send actually changes the document the customer sees.

Hanz, 2026-08-13, after inverting the base bid on a test project and re-sending:

    "I tried to resend and inversed the bids it doesnt update in the portal the new PDF"
    "Not the same?"   (portal PDF screenshot beside the proposal editor)

The page had already been pinned to a revision snapshot that morning. The PDF had not caught up,
for two reasons that both live in this repo:

1. `_PDF_CACHE` was keyed on the PROPOSAL. A re-send inside the 10-minute TTL hit the previous
   revision's rendered bytes. (Covered in test_pdf_cache.py.)
2. Every viewer URL was `/api/portal/{token}/pdf` — one URL for every revision, served with
   `private, max-age=600`. `resetPdfMounts()` dutifully tore the iframes down on a revision
   change and remounted them at the SAME url, so the browser answered from its own cache. The
   reset machinery was correct and could not possibly work.

EXECUTED, NOT GREPPED: a search for "rev=" in app.js would have passed with two of the three sites
fixed. The harness runs `renderPdf`, `mountPdf` and `mountInlinePdf` for real and reports the src
each one set, which is the only thing a browser acts on.

?rev IS A CACHE-BUSTER, NOT A SELECTOR. The server picks the revision from the proposal row. If it
honoured the client's number, a customer could fetch a superseded revision's document by editing
their address bar — priced work we have already replaced.
"""
import inspect
import json
import pathlib
import shutil
import subprocess

import pytest

import main

FRONTEND = pathlib.Path(__file__).resolve().parents[2] / "frontend"
HARNESS = pathlib.Path(__file__).resolve().parent / "js" / "pdf-url-harness.js"

needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")


@pytest.fixture(scope="module")
def ran():
    if shutil.which("node") is None:
        pytest.skip("node is not installed")
    proc = subprocess.run(["node", str(HARNESS), str(FRONTEND)],
                          capture_output=True, text=True, encoding="utf-8", timeout=120)
    assert proc.returncode == 0, (
        "the harness itself failed — read this before assuming a product bug:\n" + proc.stderr)
    return json.loads(proc.stdout.strip().splitlines()[-1])


def _urls(case):
    """Every URL the page handed the browser for one render: links and iframe srcs."""
    return [case["link"], case["modalLink"], *case["frameSrcs"]]


# ── the client half ──────────────────────────────────────────────────────────
@needs_node
def test_every_pdf_url_site_carries_the_revision(ran):
    """Three sites build these URLs (download link, popup frame, inline preview frame). One of
    them keeping a bare URL is enough to show the customer a stale document."""
    for url in _urls(ran["rev2"]):
        assert "rev=2" in url, url


@needs_node
def test_a_new_revision_produces_different_urls(ran):
    """The claim: after a re-send the browser CANNOT answer from its cache."""
    before, after = set(_urls(ran["rev2"])), set(_urls(ran["rev3"]))
    assert not (before & after), before & after


@needs_node
def test_the_viewer_fragment_survives_the_query_string(ran):
    """#view=FitH etc. must stay after ?rev= — a fragment before the query would make the
    revision part of the fragment and never reach the server, and the native viewer would open
    at its unreadable default zoom."""
    frames = ran["rev2"]["frameSrcs"]
    assert any(f.endswith("#view=FitH") for f in frames), frames
    assert any("#toolbar=0" in f for f in frames), frames
    for f in frames:
        assert f.index("?rev=") < f.index("#"), f


@needs_node
def test_a_remount_after_a_revision_lands_uses_the_new_url(ran):
    """resetPdfMounts() already existed and was inert against a cached response. Pairing it with
    a versioned URL is what makes it do anything."""
    srcs = ran["remount"]["frameSrcs"]
    assert srcs, "the reset never remounted anything"
    for s in srcs:
        assert "rev=5" in s, s


@needs_node
def test_a_legacy_row_still_gets_a_wellformed_url(ran):
    """Rows that predate revisions have `revision_no: null`. They render the LIVE draft, so the
    server sends no-store for them — but the URL still has to be valid."""
    for url in _urls(ran["legacy"]):
        assert "?rev=0" in url, url


@needs_node
def test_the_client_never_asks_the_server_to_select_a_revision(ran):
    """Only `rev` travels. A `revision_no=` param would read like a selector and invite somebody
    to wire it up on the server, which is the customer-reads-a-superseded-document bug."""
    for qs in ran["paramNames"]:
        assert qs.startswith("rev="), qs
        assert "revision_no" not in qs, qs


# ── the customer never has to reload ─────────────────────────────────────────
# Hanz, 2026-08-13: "is there a way where we just wouldnt need to reload the page for the new PDF
# file to load in? It just automatically loads the pdf file after sending"
#
# The machinery already existed and could not work. Polling carries `revision_no` in its status key
# (app.js statusKey), a change re-fetches the view model, and renderPortal's revision branch calls
# `resetPdfMounts()`. But that function cleared an element id that HAS NEVER EXISTED in index.html
# — "pdf-wrap", where the real wrapper is "pdf-frame-wrap". So the popup's stale iframe was never
# removed: the latch flipped, a second iframe was appended over the superseded one, and the customer
# kept reading the revision they had open. Paired with `?rev=` on the URL, the reset now actually
# replaces the document.
@needs_node
def test_a_resend_swaps_the_preview_with_no_reload(ran):
    """Popup closed — the common case. The inline preview is the one on screen."""
    r = ran["resendClosed"]
    assert r["first"]["link"].endswith("rev=2")
    assert r["link"].endswith("rev=3"), "the download link still points at the old revision"
    assert r["live"], "nothing was mounted after the revision landed"
    for src in r["live"]:
        assert "rev=3" in src, src
    assert r["removed"], "the superseded iframe was never removed — this was the bug"
    for src in r["removed"]:
        assert "rev=2" in src, src


@needs_node
def test_a_resend_swaps_the_full_size_viewer_the_customer_is_reading(ran):
    """Popup OPEN. `mountPdf` is lazy — it only runs when the popup is opened — so a revision
    landing mid-read would otherwise leave an empty frame until they closed and reopened it."""
    r = ran["resendOpen"]
    live = r["live"]
    assert any("#view=FitH" in s and "toolbar" not in s for s in live), \
        f"the full-size viewer did not remount: {live}"
    assert any("#toolbar=0" in s for s in live), f"the inline preview did not remount: {live}"
    for src in live:
        assert "rev=3" in src, src
    assert len(r["removed"]) == 2, f"both stale frames should be gone: {r['removed']}"


@needs_node
def test_no_stale_frame_survives_the_swap(ran):
    """The specific failure mode of the wrong id: the latch says torn down, the DOM still holds the
    old iframe, and a second one is appended on top of it."""
    for case in ("resendClosed", "resendOpen"):
        for src in ran[case]["live"]:
            assert "rev=2" not in src, f"{case} kept a superseded frame: {src}"


# ── the server half: the route's own cache read ───────────────────────────────
@pytest.fixture
def served(monkeypatch):
    """GET the customer's PDF with auth and the upstream render stubbed, and report where the
    bytes came from. Executes the ROUTE, because `_pdf_cache_key` being correct is worthless if
    api_pdf looks the entry up by proposal id — the mutation that does exactly that survived a
    helper-only test suite."""
    from fastapi.testclient import TestClient

    import config

    rendered = []

    def go(rev, cache_seed=None):
        main._PDF_CACHE.clear()
        if cache_seed:
            for k, v in cache_seed.items():
                main._PDF_CACHE[k] = (main.time.monotonic() + 600, v)
        monkeypatch.setattr(main, "_require", lambda request, token: {
            "proposal_id": "p1", "current_revision_no": rev, "project_name": "Westport"})
        monkeypatch.setattr(config, "PROPOSAL_TOOL_URL", "http://tool")
        monkeypatch.setattr(config, "SERVICE_TOKEN", "tok")

        class _R:
            status_code = 200
            content = b"%PDF-freshly-rendered"

        def _get(url, **kw):
            rendered.append(kw.get("params"))
            return _R()

        monkeypatch.setattr(main.httpx, "get", _get)
        r = TestClient(main.app).get("/api/portal/tok-123/pdf")
        return r, rendered

    return go


def test_the_route_reads_the_cache_by_revision(served):
    """A hit only counts when it belongs to the revision being served."""
    r, rendered = served(2, {main._pdf_cache_key("p1", 2): b"%PDF-rev-two"})
    assert r.status_code == 200
    assert r.content == b"%PDF-rev-two"
    assert rendered == [], "a valid cache entry was ignored and re-rendered"


def test_a_superseded_entry_is_not_served_after_a_resend(served):
    """THE INCIDENT, at the route: revision 2's bytes are in RAM, staff sent revision 3."""
    r, rendered = served(3, {main._pdf_cache_key("p1", 2): b"%PDF-rev-two"})
    assert r.content == b"%PDF-freshly-rendered"
    assert rendered == [{"draft_id": "p1", "revision_no": 3}]


def test_a_legacy_bare_pid_entry_is_never_served(served):
    """Entries written by the previous build are keyed on the proposal alone. A deploy that
    doesn't restart the process leaves them in RAM, where they would answer for EVERY revision —
    the staleness bug preserved in amber."""
    r, rendered = served(3, {"p1": b"%PDF-from-the-old-build"})
    assert r.content == b"%PDF-freshly-rendered"
    assert rendered == [{"draft_id": "p1", "revision_no": 3}]


def test_the_render_is_cached_under_the_revision_it_rendered(served):
    r, _ = served(4)
    assert r.content == b"%PDF-freshly-rendered"
    assert main._PDF_CACHE[main._pdf_cache_key("p1", 4)][1] == b"%PDF-freshly-rendered"
    main._PDF_CACHE.clear()


def test_an_unpinned_row_renders_the_live_draft_uncached_by_the_browser(served):
    """No revision → the tool renders the live draft, whose bytes change under one URL."""
    r, rendered = served(None)
    assert rendered == [{"draft_id": "p1"}], "a revision_no was invented for a legacy row"
    assert "no-store" in r.headers["cache-control"]
    main._PDF_CACHE.clear()


def test_a_pinned_row_is_served_cacheable(served):
    r, _ = served(2)
    assert r.headers["cache-control"] == "private, max-age=600"
    main._PDF_CACHE.clear()


def test_the_server_ignores_the_clients_rev():
    """Belt and braces on the sentence above: the render params are built from the ROW."""
    src = inspect.getsource(main.api_pdf)
    body = "\n".join(l for l in src.splitlines() if not l.strip().startswith("#"))
    assert 'p.get("current_revision_no")' in body
    assert 'query_params' not in body and '"rev"' not in body, (
        "api_pdf started reading the client's rev — it must only ever bust the cache")
