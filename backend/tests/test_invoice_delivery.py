"""Delivery of the deposit invoice: the Resend attachment payload, the email body,
and the customer-facing PDF endpoint (auth-gated, 404 before an invoice exists)."""
import base64

import pytest

import config
import email_sender
import main


# ── _send: attachments ───────────────────────────────────────────────────────
@pytest.fixture
def captured(monkeypatch):
    """Force the real send path (an API key is set) and capture the HTTP payload."""
    box = {}

    class _Resp:
        status_code = 200
        def raise_for_status(self): pass

    monkeypatch.setattr(config, "RESEND_API_KEY", "re_test")
    monkeypatch.setattr(email_sender.httpx, "post",
                        lambda url, headers=None, json=None, timeout=None: (
                            box.update(payload=json) or _Resp()))
    return box


def test_send_encodes_attachments_base64(captured):
    ok = email_sender._send(["a@x.com"], "S", "<p>hi</p>",
                            attachments=[("Invoice.pdf", b"%PDF-1.4 fake")])
    assert ok
    att = captured["payload"]["attachments"]
    assert len(att) == 1
    assert att[0]["filename"] == "Invoice.pdf"
    assert base64.b64decode(att[0]["content"]) == b"%PDF-1.4 fake"


def test_send_omits_attachments_key_when_none(captured):
    email_sender._send(["a@x.com"], "S", "<p>hi</p>")
    assert "attachments" not in captured["payload"]


def test_send_skips_empty_blobs(captured):
    email_sender._send(["a@x.com"], "S", "<p>hi</p>", attachments=[("Empty.pdf", b"")])
    assert captured["payload"]["attachments"] == []


def test_deposit_email_names_invoice_and_attaches(captured):
    email_sender.send_deposit_request("a@x.com", "https://p/x", "Westport", 3316.25,
                                      invoice_no="TW-INV-01001", invoice_pdf=b"%PDF-1.4",
                                      invoice_filename="Treadwell-Invoice-TW-INV-01001.pdf",
                                      reference="TW-ABC123")
    p = captured["payload"]
    # The subject is the PROJECT now, one per thread (2026-08-11). The invoice number moved
    # into the body, where it still has to be: a customer paying by check writes it on the
    # memo line, so losing it from the subject must not mean losing it entirely.
    assert p["subject"] == "Your Treadwell proposal — Westport"
    assert "TW-INV-01001" in p["html"]
    assert "$3,316.25" in p["html"]
    assert "TW-ABC123" in p["html"]
    assert "attached as a PDF" in p["html"]
    assert p["attachments"][0]["filename"] == "Treadwell-Invoice-TW-INV-01001.pdf"


def test_deposit_email_without_pdf_falls_back(captured):
    """No PDF (e.g. render failed upstream) → still a valid email, old wording."""
    email_sender.send_deposit_request("a@x.com", "https://p/x", "Westport", 100.0)
    p = captured["payload"]
    assert p["subject"] == "Your Treadwell proposal — Westport"
    assert "Deposit invoice" in p["html"], (
        "the subject stopped naming the event, so the heading has to")
    assert "will follow shortly" in p["html"]
    assert "attachments" not in p


def test_deposit_email_escapes_project_name(captured):
    email_sender.send_deposit_request("a@x.com", "https://p/x", "<script>x</script>", 10.0)
    assert "<script>x</script>" not in captured["payload"]["html"]
    assert "&lt;script&gt;" in captured["payload"]["html"]


# ── GET /api/portal/{token}/deposit-invoice.pdf ──────────────────────────────
@pytest.fixture
def client(monkeypatch):
    from fastapi.testclient import TestClient
    # The document is rendered by the proposal tool over HTTP; stub that seam so
    # the suite never makes a real call (it would hang on the 90s timeout).
    monkeypatch.setattr(main.invoice, "render_invoice_pdf", lambda payload, **k: b"%PDF-1.4 stub")
    monkeypatch.setattr(main.db, "get_draft_data", lambda pid: {})
    return TestClient(main.app)


_ROW = {"proposal_id": "p1", "token": "tok", "project_name": "Westport",
        "approved_total": 13265.0, "deposit_amount": 3316.25,
        "deposit_invoice_no": "TW-INV-01001", "customer_email": "c@x.com"}


def test_invoice_pdf_requires_session(client, monkeypatch):
    monkeypatch.setattr(main, "_require", lambda request, token: None)
    r = client.get("/api/portal/tok/deposit-invoice.pdf")
    assert r.status_code == 401


def test_invoice_pdf_404_before_issued(client, monkeypatch):
    monkeypatch.setattr(main, "_require",
                        lambda request, token: {**_ROW, "deposit_invoice_no": None})
    r = client.get("/api/portal/tok/deposit-invoice.pdf")
    assert r.status_code == 404
    assert r.json()["error"] == "no_invoice"


def test_invoice_pdf_served_with_filename(client, monkeypatch):
    monkeypatch.setattr(main, "_require", lambda request, token: _ROW)
    r = client.get("/api/portal/tok/deposit-invoice.pdf")
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/pdf"
    assert "Treadwell-Invoice-TW-INV-01001.pdf" in r.headers["content-disposition"]
    assert r.content.startswith(b"%PDF-")


# ── static asset caching ─────────────────────────────────────────────────────
def test_assets_and_shell_always_revalidate(client):
    """Regression: these carried only an ETag, so browsers heuristically cached
    them and customers kept running the previous deploy's app.js (the old deposit
    card, with no invoice buttons). Every shell/asset response must tell the
    browser to revalidate."""
    for path in ("/app.js", "/styles.css", "/projects.js", "/login.js", "/auth.js"):
        r = client.get(path)
        assert r.status_code == 200, path
        assert "no-cache" in r.headers.get("cache-control", ""), path
    for path in ("/", "/p/sometoken"):
        r = client.get(path)
        assert r.status_code == 200, path
        assert "no-cache" in r.headers.get("cache-control", ""), path
