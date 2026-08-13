"""PDF byte-cache: the bounded sweep that keeps the cache from growing unbounded on the
RAM-constrained VPS, AND the revision keying that stops a re-send from serving the previous
revision's bytes for the rest of the 10-minute TTL."""
import main


def test_cap_enforced():
    main._PDF_CACHE.clear()
    for i in range(main._PDF_CACHE_MAX + 10):
        main._pdf_cache_put(f"p{i}", 1, b"x")
    assert len(main._PDF_CACHE) <= main._PDF_CACHE_MAX
    main._PDF_CACHE.clear()


def test_expired_entries_swept_on_put():
    main._PDF_CACHE.clear()
    main._PDF_CACHE["old"] = (0.0, b"x")     # monotonic() is always > 0 → already expired
    main._pdf_cache_put("new", 1, b"y")
    assert "old" not in main._PDF_CACHE
    assert main._pdf_cache_key("new", 1) in main._PDF_CACHE
    main._PDF_CACHE.clear()


def test_drop_removes_entry_and_is_idempotent():
    main._PDF_CACHE.clear()
    main._pdf_cache_put("p1", 1, b"x")
    main._pdf_cache_drop("p1")
    assert not main._PDF_CACHE
    main._pdf_cache_drop("p1")                # no KeyError on a missing key
    main._PDF_CACHE.clear()


def test_a_new_revision_cannot_hit_the_old_bytes():
    """THE INCIDENT: staff invert the base bid and re-send inside the TTL. Keyed on the proposal
    alone, the customer kept getting revision 1's PDF."""
    main._PDF_CACHE.clear()
    main._pdf_cache_put("p1", 1, b"rev-one")
    assert main._PDF_CACHE[main._pdf_cache_key("p1", 1)][1] == b"rev-one"
    assert main._PDF_CACHE.get(main._pdf_cache_key("p1", 2)) is None
    main._PDF_CACHE.clear()


def test_drop_clears_every_revision_of_one_proposal():
    """A prefix drop, not a bare pop — otherwise each rev entry outlives the re-publish."""
    main._PDF_CACHE.clear()
    for rev in (1, 2, 3):
        main._pdf_cache_put("p1", rev, b"x")
    main._pdf_cache_put("p2", 1, b"keep")
    main._pdf_cache_drop("p1")
    assert list(main._PDF_CACHE) == [main._pdf_cache_key("p2", 1)]
    main._PDF_CACHE.clear()


def test_drop_clears_a_legacy_unkeyed_entry():
    """Entries written by the previous build are keyed on the bare pid. A restart-free deploy
    leaves them in RAM, so the drop has to reach them too."""
    main._PDF_CACHE.clear()
    main._PDF_CACHE["p1"] = (main.time.monotonic() + 600, b"legacy")
    main._pdf_cache_drop("p1")
    assert not main._PDF_CACHE


def test_drop_does_not_touch_a_proposal_sharing_a_prefix():
    main._PDF_CACHE.clear()
    main._pdf_cache_put("p1", 1, b"x")
    main._pdf_cache_put("p1-extra", 1, b"keep")
    main._pdf_cache_drop("p1")
    assert list(main._PDF_CACHE) == [main._pdf_cache_key("p1-extra", 1)]
    main._PDF_CACHE.clear()


def test_unpinned_bytes_are_never_browser_cached():
    """A legacy row (no revision) renders the LIVE draft: same URL, changing bytes. Caching that
    for 10 minutes is the browser-side half of "the PDF didn't update"."""
    assert "no-store" in main._pdf_headers(0)["Cache-Control"]
    assert "no-store" in main._pdf_headers(None)["Cache-Control"]


def test_pinned_bytes_are_cacheable():
    """A pinned revision is immutable and the viewer's URL carries ?rev=, so caching is safe —
    and is what keeps a multi-second LibreOffice render off every page view."""
    assert main._pdf_headers(2)["Cache-Control"] == "private, max-age=600"


def test_headers_always_name_the_file_inline():
    for rev in (0, 3):
        assert main._pdf_headers(rev)["Content-Disposition"] == 'inline; filename="proposal.pdf"'
