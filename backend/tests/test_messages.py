"""Chat-message serializer (main._msg) — pure shape invariants. DB-backed thread
storage + polling are covered by the staging smoke, per the repo convention."""
import datetime as dt

import main


def test_msg_shape_text():
    row = {"id": 5, "author_kind": "customer", "body": "hi", "msg_type": "text",
           "meta": None, "created_at": dt.datetime(2026, 7, 16, 12, 0, 0)}
    m = main._msg(row)
    assert m["id"] == 5
    assert m["author_kind"] == "customer"
    assert m["body"] == "hi"
    assert m["msg_type"] == "text"
    assert m["meta"] is None
    assert m["created_at"].startswith("2026-07-16T12:00:00")


def test_msg_defaults_type_when_missing():
    m = main._msg({"author_kind": "staff", "body": "x", "created_at": None})
    assert m["msg_type"] == "text"   # None/absent msg_type coerces to 'text'
    assert m["meta"] is None
    assert m["created_at"] is None
    assert m["id"] is None


def test_msg_preserves_card_type_and_meta():
    row = {"id": 9, "author_kind": "staff", "body": "Deposit", "msg_type": "deposit_request",
           "meta": {"amount": 1234.5}, "created_at": None}
    m = main._msg(row)
    assert m["msg_type"] == "deposit_request"
    assert m["meta"] == {"amount": 1234.5}
