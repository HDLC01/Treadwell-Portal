"""PDF byte-cache eviction (_pdf_cache_put / _pdf_cache_drop) — the bounded
sweep that keeps the cache from growing unbounded on the RAM-constrained VPS."""
import main


def test_cap_enforced():
    main._PDF_CACHE.clear()
    for i in range(main._PDF_CACHE_MAX + 10):
        main._pdf_cache_put(f"p{i}", b"x")
    assert len(main._PDF_CACHE) <= main._PDF_CACHE_MAX
    main._PDF_CACHE.clear()


def test_expired_entries_swept_on_put():
    main._PDF_CACHE.clear()
    main._PDF_CACHE["old"] = (0.0, b"x")     # monotonic() is always > 0 → already expired
    main._pdf_cache_put("new", b"y")
    assert "old" not in main._PDF_CACHE
    assert "new" in main._PDF_CACHE
    main._PDF_CACHE.clear()


def test_drop_removes_entry_and_is_idempotent():
    main._PDF_CACHE.clear()
    main._pdf_cache_put("p1", b"x")
    main._pdf_cache_drop("p1")
    assert "p1" not in main._PDF_CACHE
    main._pdf_cache_drop("p1")                # no KeyError on a missing key
    main._PDF_CACHE.clear()
