"""
The verdict was produced correctly and then destroyed on its way out.

WHAT THE LIVE RUN PROVED
create_channel_checked worked exactly as designed. Immediately after addChannel
failed, a signed call on the SAME connection returned a real guid
(u0KP2yJ0f60a5903dac2a6a5ef1f43bf), so the session is provably valid and Rubika is
refusing addChannel for the account. Question settled.

WHAT STILL WENT WRONG
Three layers treated that verdict as an ordinary auth failure, because its message
QUOTES the platform's original INVALID_AUTH inside itself:

1. account_conn.signed_call matched is_auth_error on the text, retried on a fresh
   connection — which can only produce the same refusal — and then wrapped both
   attempts in a generic RuntimeError. The exception TYPE was lost, so the panel's
   isinstance check never matched and the worker returned 400 instead of 403.
2. The wrapper truncated each reason to 120 characters, cutting the sentence off
   at "...on the same connection ret" — removing the verdict from the middle of
   its own explanation. Even the fallback text match could not find it.
3. session_store._auth_shaped matched the same text and re-placed a perfectly
   good session, then retried an operation that cannot succeed.

So the customer saw "signed call failed on both connections" instead of "this
account cannot create channels, the session is fine".

Every test below was mutation-verified.
"""
import asyncio
import os

import pytest

import account_conn
import rubika_client as rb
import session_store

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

VERDICT = (
    "Rubika refused addChannel for this account while the session is provably "
    "valid (a signed call on the same connection returned "
    "u0KP2yJ0f60a5903dac2a6a5ef1f43bf). The account is not permitted to create a "
    "channel — usually a new or restricted number. Original: InvalidAuth: "
    "{'status': 'ERROR_GENERIC', 'status_det': 'INVALID_AUTH'}")


# --------------------------------------------------------------------------- #
# the verdict must survive signed_call untouched
# --------------------------------------------------------------------------- #
def test_signed_call_lets_the_verdict_through_unwrapped(monkeypatch):
    attempts = []

    async def _fn(client):
        attempts.append(True)
        raise rb.ChannelNotPermitted(VERDICT)

    async def _call(cid, phone, fn, *a, **k):
        return await fn(object())

    monkeypatch.setattr(account_conn, "call", _call)
    with pytest.raises(rb.ChannelNotPermitted) as caught:
        asyncio.run(account_conn.signed_call(7, "989120000001", _fn,
                                             timeout=5))
    assert "not permitted to create a channel" in str(caught.value), \
        "wrapping it in RuntimeError destroyed the one fact the probe produced"
    assert len(attempts) == 1, \
        ("a refusal that already proved the session works cannot be fixed by a "
         "second connection; retrying only doubles the load")


def test_a_real_auth_failure_is_still_retried_on_a_fresh_connection(monkeypatch):
    """The verdict short-circuit must not disable the fallback it sits next to."""
    tries = []

    async def _call(cid, phone, fn, *a, **k):
        tries.append("warm")
        raise RuntimeError("{'status_det': 'INVALID_AUTH'}")

    class _Fresh:
        async def __aenter__(self):
            tries.append("fresh")
            return object()

        async def __aexit__(self, *a):
            return False

    async def _fn(client):
        return "ok"

    monkeypatch.setattr(account_conn, "call", _call)
    monkeypatch.setattr(account_conn, "fresh_connection",
                        lambda cid, phone: _Fresh())
    got = asyncio.run(account_conn.signed_call(7, "989120000001", _fn,
                                               timeout=5))
    assert got == "ok"
    assert tries == ["warm", "fresh"]


def test_a_verdict_raised_on_the_FRESH_connection_is_also_unwrapped(monkeypatch):
    """The warm guard short-circuits, so the fresh guard needs its own test.

    Without this, deleting the fresh-path guard changes nothing that any test
    notices — the warm path never reaches it.
    """
    async def _call(cid, phone, fn, *a, **k):
        raise RuntimeError("{'status_det': 'INVALID_AUTH'}")   # a real auth error

    class _Fresh:
        async def __aenter__(self):
            return object()

        async def __aexit__(self, *a):
            return False

    async def _fn(client):
        raise rb.ChannelNotPermitted(VERDICT)

    monkeypatch.setattr(account_conn, "call", _call)
    monkeypatch.setattr(account_conn, "fresh_connection",
                        lambda cid, phone: _Fresh())
    with pytest.raises(rb.ChannelNotPermitted):
        asyncio.run(account_conn.signed_call(7, "989120000001", _fn, timeout=5))


def test_the_type_guard_stands_alone(monkeypatch):
    """A verdict whose text lacks the phrase must still be recognised.

    The type check and the text check are deliberately redundant — the class
    survives in-process, the text survives an HTTP boundary — so each needs a case
    the other cannot cover, or a mutation to either passes unnoticed.
    """
    bare = rb.ChannelNotPermitted("Original: INVALID_AUTH")
    assert "NOT PERMITTED" not in str(bare).upper()
    assert session_store._auth_shaped(bare) is False


def test_the_both_failed_message_keeps_whole_sentences():
    src = open(os.path.join(ROOT, "account_conn.py"), encoding="utf-8").read()
    start = src.index("async def signed_call")
    body = src[start:src.index("\nasync def ", start + 10)]
    assert "[:120]" not in body, \
        ("120 characters cut the reason off at '...on the same connection ret', "
         "removing the verdict from the middle of its own explanation")
    assert "[:400]" in body


# --------------------------------------------------------------------------- #
# a verdict must not trigger a session repair
# --------------------------------------------------------------------------- #
def test_the_verdict_is_not_auth_shaped():
    assert session_store._auth_shaped(rb.ChannelNotPermitted(VERDICT)) is False, \
        ("re-placing a session that was just proved healthy, then retrying an "
         "operation that cannot succeed, is pure waste")


def test_a_real_auth_error_is_still_auth_shaped():
    assert session_store._auth_shaped(
        RuntimeError("{'status_det': 'INVALID_AUTH'}")) is True
    assert session_store._auth_shaped(RuntimeError("AUTH_FROM_ANOTHER")) is True


def test_the_verdict_text_alone_is_enough_to_recognise_it():
    """Across an HTTP boundary the class is gone; only the text survives."""
    assert session_store._auth_shaped(RuntimeError(VERDICT)) is False


def test_an_ordinary_failure_is_not_auth_shaped():
    assert session_store._auth_shaped(RuntimeError("TOO_REQUESTS")) is False


def test_run_with_repair_does_not_retry_a_verdict(monkeypatch):
    """place() must be forced to succeed, or the test proves nothing.

    With no stored session blob, place() returns False and run_with_repair
    re-raises anyway — so a missing guard looks identical to a working one. The
    first version of this test had exactly that hole.
    """
    calls = []
    placed = []

    async def _place(customer_id, acc):
        placed.append(True)
        return True                     # a repair that "worked"

    async def _op():
        calls.append(True)
        raise rb.ChannelNotPermitted(VERDICT)

    monkeypatch.setattr(session_store, "place", _place)
    with pytest.raises(rb.ChannelNotPermitted):
        asyncio.run(session_store.run_with_repair(7, {"id": 1, "phone": "0912"},
                                                  _op))
    assert len(calls) == 1, "a refusal is not repaired by rewriting the session"
    assert not placed, \
        "the session was just proved healthy; rewriting it is pure waste"


# --------------------------------------------------------------------------- #
# the panel recognises it however it arrives
# --------------------------------------------------------------------------- #
def _code(filename, name, kind="async def"):
    src = open(os.path.join(ROOT, filename), encoding="utf-8").read()
    start = src.index(f"{kind} {name}")
    body = src[start:]
    for marker in ("\nasync def ", "\ndef ", "\nclass "):
        at = body.find(marker, 10)
        if at != -1:
            body = body[:at]
    out, in_doc, delim = [], False, None
    for line in body.splitlines():
        stripped = line.strip()
        if in_doc:
            if delim in stripped:
                in_doc = False
            continue
        if stripped.startswith('"""') or stripped.startswith("'''"):
            delim = stripped[:3]
            if delim in stripped[3:]:
                continue
            in_doc = True
            continue
        if stripped.startswith("#"):
            continue
        out.append(line.split("#")[0])
    return "\n".join(out)


def test_the_panel_recognises_a_verdict_from_a_worker():
    code = _code("rubika_panel.py", "_channel_flow")
    assert '"ChannelNotPermitted" in text' in code, \
        ("from a worker the verdict arrives inside a WorkerAPIError string — an "
         "HTTP boundary cannot carry a Python class")
    assert 'isinstance(exc, rb.ChannelNotPermitted)' in code, \
        "and locally it is the real exception"
