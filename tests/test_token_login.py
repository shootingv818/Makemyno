"""
Session-token login accepted tokens that could never work.

WHAT WAS WRONG
--------------
_step_token created the account, stored the blob, wrote the session file and
reported "Status: SUCCESS / Session Saved: YES" WITHOUT EVER CONNECTING. A dead,
truncated or key-less token therefore produced a perfectly healthy-looking
account that failed on its first real operation with INVALID_AUTH — and the
customer had no reason to suspect the token, because the login had said SUCCESS.
That is a large part of why the INVALID_AUTH hunt took as long as it did.

The reference (Makiioo@feat/build-from-meow, handle_session_login) verifies first
and REFUSES: locally it writes the session, connects once, calls get_me() — a
SIGNED call, so it proves the private_key is usable rather than merely present —
and reads the contacts; remotely it pushes the session write-only and asks the
worker's /account/verify.

Three smaller defects went with it:
  * only `phone` was checked, so a token with no auth or no private_key passed;
  * every failure produced the same message, "توکن باید با MMSESS: شروع شود",
    even for a correctly prefixed token that was simply incomplete;
  * the state was cleared on failure, so the customer had to navigate back before
    they could paste again.

And the reported crash: a token pasted at the PHONE prompt was stripped to its
digits and sent to Rubika as a 240-digit phone number, which answered
INVALID_INPUT and logged the entire token as the account being logged in.

Every test below was mutation-verified.
"""
import json
import os

import pytest

import db
import rubika_panel

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _function_source(filename: str, header: str, code_only: bool = False) -> str:
    """One whole function, to the next top-level definition. Never a byte window.

    `code_only` strips comments and the docstring. Needed for any check on the
    ORDER of two statements: the docstring here explains the order in prose and
    names both symbols, so a raw search found the explanation first and reported
    the code as being the wrong way round.
    """
    with open(os.path.join(ROOT, filename), encoding="utf-8") as fh:
        body = fh.read()
    start = body.index(header)
    rest = body[start + len(header):]
    end = len(rest)
    for marker in ("\nasync def ", "\ndef ", "\nclass "):
        at = rest.find(marker)
        if at != -1:
            end = min(end, at)
    section = header + rest[:end]
    if not code_only:
        return section
    out, in_doc = [], False
    for line in section.splitlines():
        stripped = line.strip()
        if stripped.startswith(('"""', "'''")):
            ticks = stripped[:3]
            if not in_doc:
                in_doc = not (stripped.endswith(ticks) and len(stripped) > 3)
                continue
            in_doc = False
            continue
        if in_doc or stripped.startswith("#"):
            continue
        out.append(line.split("#")[0])
    return "\n".join(out)


GOOD = {"phone": "989120000001", "auth": "A" * 32,
        "private_key": "-----BEGIN RSA PRIVATE KEY-----\nX\n"
                       "-----END RSA PRIVATE KEY-----",
        "guid": "u0", "user_agent": "ua"}


# --------------------------------------------------------------------------- #
# the token format itself
# --------------------------------------------------------------------------- #
def test_a_packed_token_round_trips():
    token = db.session_pack(GOOD)
    assert token.startswith("MMSESS:")
    assert db.session_unpack(token) == GOOD


def test_the_reference_prefix_is_still_accepted():
    """Tokens copied out of the older project must keep working."""
    import base64
    raw = base64.urlsafe_b64encode(json.dumps(GOOD).encode()).decode()
    assert db.session_unpack("YDSESS:" + raw) == GOOD


def test_plain_text_is_not_a_token():
    assert db.session_unpack("hello") is None
    assert db.session_unpack("") is None
    assert db.session_unpack("MMSESS:not-base64!!") is None


# --------------------------------------------------------------------------- #
# a token pasted at the phone prompt
# --------------------------------------------------------------------------- #
def test_a_token_is_recognised_at_the_phone_prompt():
    token = db.session_pack(GOOD)
    assert rubika_panel._normalize_phone_input(token) == "", \
        ("a 240-digit 'phone number' was sent to Rubika and came back "
         "INVALID_INPUT with the whole token in the log")
    assert rubika_panel._looks_like_session_token(token)


def test_the_phone_step_routes_a_token_into_the_token_flow():
    section = _function_source("rubika_panel.py", "async def _step_phone",
                               code_only=True)
    assert "_step_token(" in section, \
        ("the customer pasted a valid credential; refusing it on a technicality "
         "and telling them to find another button is what caused the crash")


# --------------------------------------------------------------------------- #
# validation happens BEFORE anything is written
# --------------------------------------------------------------------------- #
def test_verification_precedes_account_creation():
    section = _function_source("rubika_panel.py", "async def _step_token",
                               code_only=True)
    assert section.index("_verify_session_token") < section.index("db.add_account"), \
        ("creating the row first is how a dead token became a healthy-looking "
         "account that failed days later")


def test_every_required_field_is_checked_not_just_the_phone():
    section = _function_source("rubika_panel.py", "async def _step_token")
    assert '"phone", "auth", "private_key"' in section, \
        ("a token with no private_key can READ but never SIGN, so it would be "
         "accepted and then refuse every channel creation and every send")


def test_a_failed_paste_keeps_the_customer_in_the_step():
    section = _function_source("rubika_panel.py", "async def _step_token")
    assert '_state[uid] = {"step": "rb_token"}' in section, \
        "clearing the state forced the customer to navigate back to retry"


def test_the_missing_field_is_named_in_the_message():
    section = _function_source("rubika_panel.py", "async def _step_token")
    assert 'cards.kv("Missing"' in section, \
        ("one message for every possible problem meant the message never "
         "matched what was actually wrong")


def test_a_duplicate_phone_is_refused_before_verifying():
    section = _function_source("rubika_panel.py", "async def _step_token",
                               code_only=True)
    assert section.index("get_account_by_phone") < \
        section.index("_verify_session_token"), \
        "no point connecting for an account that already exists"


# --------------------------------------------------------------------------- #
# the verifier itself
# --------------------------------------------------------------------------- #
def test_the_local_path_makes_a_signed_call():
    section = _function_source("rubika_panel.py",
                               "async def _verify_session_token",
                               code_only=True)
    assert "get_me()" in section, \
        ("get_me is SIGNED, so it proves the private_key is usable — checking "
         "that the file merely exists is what let unusable sessions through")
    assert "import_session" in section


def test_the_remote_path_asks_the_worker_and_refuses_a_dead_session():
    section = _function_source("rubika_panel.py",
                               "async def _verify_session_token",
                               code_only=True)
    assert "/account/verify" in section
    assert 'verdict.get("dead")' in section, \
        "a worker that says the session is dead must stop the login"


def test_the_remote_path_pushes_write_only_before_verifying():
    section = _function_source("rubika_panel.py",
                               "async def _verify_session_token",
                               code_only=True)
    assert section.index("push_session") < section.index("/account/verify"), \
        "there is nothing to verify until the session is on the worker"


def test_the_verifier_raises_rather_than_returning_a_flag():
    """The caller must not be able to ignore a failed verification."""
    section = _function_source("rubika_panel.py",
                               "async def _verify_session_token")
    assert "raise RuntimeError" in section


# --------------------------------------------------------------------------- #
# push_session must not throw the reason away
# --------------------------------------------------------------------------- #
def test_push_session_records_why_it_failed(monkeypatch):
    import asyncio

    import worker

    async def _boom(*a, **k):
        raise RuntimeError("tunnel closed")

    monkeypatch.setattr(worker, "api_call", _boom)
    ok = asyncio.run(worker.push_session({"tag": "wk-1"}, 7, "989120000001",
                                         GOOD))
    assert ok is False
    assert "tunnel closed" in worker.last_push_error(), \
        ("a bare False is how 'Session Saved: NO' ended up on a card with "
         "nothing to act on")


def test_push_session_refuses_a_key_less_session():
    import asyncio

    import worker

    values = dict(GOOD)
    values.pop("private_key")
    ok = asyncio.run(worker.push_session({"tag": "wk-1"}, 7, "989120000001",
                                         values))
    assert ok is False
    assert "private_key" in worker.last_push_error(), \
        ("writing a session that can only read would refuse every signed call "
         "later, with nothing pointing back to here")


def test_push_session_clears_the_reason_on_success(monkeypatch):
    import asyncio

    import worker

    async def _ok(*a, **k):
        return {"ok": True}

    worker.LAST_PUSH_ERROR["reason"] = "stale"
    monkeypatch.setattr(worker, "api_call", _ok)
    assert asyncio.run(worker.push_session({"tag": "wk-1"}, 7, "989120000001",
                                           GOOD)) is True
    assert worker.last_push_error() == "", \
        "a stale reason would be reported against the next successful push"


# --------------------------------------------------------------------------- #
# the success card tells the truth
# --------------------------------------------------------------------------- #
def test_the_success_card_reports_what_was_verified():
    section = _function_source("rubika_panel.py", "async def _step_token")
    assert 'cards.kv("Verified", "YES")' in section, \
        "SUCCESS on a login that never connected is the defect itself"
    assert 'cards.kv("Contacts"' in section, \
        "a contact count is proof the session really read something"
