"""
The error card's forensics: the failing frame, its source line, and its locals.

WHY
---
A login failed with "'NoneType' object is not callable" pointing at

    aid = db.add_account(uid, phone, name=info.get("name") or "",

Three callables share that one statement, so the traceback named the wrong
suspect. `db.add_account` was a healthy function; `info.get` was the None,
because a function upstream returned a rubpy object instead of a dict. Diagnosing
it took three rounds. Printing the locals would have ended it in one: `info` would
have shown as a rubpy object rather than a dict.

So the error card now carries the locals — with two constraints that matter more
than completeness: only OUR frames (a frame inside telethon has huge, irrelevant
locals), and no credentials (this card goes to the log group).
"""
import asyncio

import pytest

import logbus


class _RubpyLike:
    """The actual landmine: an attribute named `get` that is not callable."""
    get = None

    def __repr__(self):
        return "<RubpyResult status=OK>"


def _boom_like_production():
    info = _RubpyLike()
    uid = 5818420346
    phone = "09120000001"
    return info.get("name")          # None("name") -> TypeError


# --------------------------------------------------------------------------- #
# It finds the frame and the values
# --------------------------------------------------------------------------- #
def test_it_reports_the_frame_that_failed():
    try:
        _boom_like_production()
    except Exception as exc:
        frame = logbus._blame(exc)
    assert frame is not None
    assert "_boom_like_production" in frame["where"]


def test_it_shows_the_type_of_every_local():
    """"Is it a dict or a rubpy object?" was the entire question in the bug."""
    try:
        _boom_like_production()
    except Exception as exc:
        frame = logbus._blame(exc)
    names = dict(frame["locals"])
    assert "_RubpyLike" in names["info"], "the type must be visible"
    assert "5818420346" in names["uid"]
    assert "09120000001" in names["phone"]


def test_credentials_are_redacted():
    """The card goes to the log group; a traceback is not a reason to print an
    auth token there."""
    def _boom():
        auth = "SECRET-AUTH-DATA"
        session_key = "SECRET-KEY"
        private_key = "SECRET-PRIV"
        harmless = "visible"
        nothing = None
        return nothing()   # same failure, no SyntaxWarning at import
    try:
        _boom()
    except Exception as exc:
        frame = logbus._blame(exc)
    values = dict(frame["locals"])
    for name in ("auth", "session_key", "private_key"):
        assert values[name] == "<redacted>", f"{name} leaked"
    assert "visible" in values["harmless"]


def test_a_broken_repr_does_not_hide_the_bug():
    class _Hostile:
        def __repr__(self):
            raise RuntimeError("no repr for you")

    def _boom():
        hostile = _Hostile()
        nothing = None
        return nothing()   # same failure, no SyntaxWarning at import
    try:
        _boom()
    except Exception as exc:
        frame = logbus._blame(exc)
    assert "unreprable" in dict(frame["locals"])["hostile"]


def test_huge_values_are_truncated():
    def _boom():
        payload = "x" * 5000
        nothing = None
        return nothing()   # same failure, no SyntaxWarning at import
    try:
        _boom()
    except Exception as exc:
        frame = logbus._blame(exc)
    assert len(dict(frame["locals"])["payload"]) < 200


def test_the_number_of_locals_is_capped():
    """One error card must not become forty lines of noise."""
    def _boom():
        a1 = a2 = a3 = a4 = a5 = a6 = a7 = a8 = 1
        b1 = b2 = b3 = b4 = b5 = b6 = b7 = b8 = 2
        nothing = None
        return nothing()   # same failure, no SyntaxWarning at import
    try:
        _boom()
    except Exception as exc:
        frame = logbus._blame(exc)
    assert len(frame["locals"]) <= 12


def test_it_picks_the_deepest_of_our_frames():
    """A helper called by a handler: the innermost OUR frame is where the bug is."""
    def _inner():
        culprit = "inner"
        nothing = None
        return nothing()   # same failure, no SyntaxWarning at import

    def _outer():
        marker = "outer"
        return _inner()
    try:
        _outer()
    except Exception as exc:
        frame = logbus._blame(exc)
    assert "_inner" in frame["where"]
    assert "culprit" in dict(frame["locals"])


def test_an_exception_with_no_traceback_is_handled():
    assert logbus._blame(ValueError("never raised")) is None


# --------------------------------------------------------------------------- #
# The card still renders
# --------------------------------------------------------------------------- #
def test_the_error_card_includes_the_forensics(monkeypatch):
    sent = {}

    async def _to_group(text, **kwargs):
        sent["text"] = text

    async def _to_pv(cid, text):
        return None
    monkeypatch.setattr(logbus, "to_group", _to_group)
    monkeypatch.setattr(logbus, "to_pv", _to_pv)

    try:
        _boom_like_production()
    except Exception as exc:
        code = asyncio.run(logbus.error(exc, context="rb login code",
                                        customer=1001, notify=False))
    assert code.startswith("E-")
    body = sent["text"]
    assert "rb login code" in body
    assert "info" in body and "_RubpyLike" in body, "locals must reach the card"
    assert "Traceback" in body, "the traceback must still be there"


def test_forensics_failure_never_breaks_error_reporting(monkeypatch):
    """The forensics are a convenience. If they blow up, the error must still be
    reported — losing the error card is far worse than losing the locals."""
    sent = {}

    async def _to_group(text, **kwargs):
        sent["text"] = text
    monkeypatch.setattr(logbus, "to_group", _to_group)

    def _explode(exc):
        raise RuntimeError("forensics broke")
    monkeypatch.setattr(logbus, "_blame", _explode)

    try:
        _boom_like_production()
    except Exception as exc:
        code = asyncio.run(logbus.error(exc, context="still reported",
                                        notify=False))
    assert code.startswith("E-"), "the error must still get a code"
    assert "still reported" in sent["text"], "the card must still be sent"
    assert "Traceback" in sent["text"]
