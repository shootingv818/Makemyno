"""
Three things the production logs proved, and one thing they disproved.

DISPROVED: addChannel's INVALID_AUTH is NOT an auth problem.
   The same account, on the same session, showed "Progress: 5/1376" — it was
   sending successfully — while every /channel/create came back INVALID_AUTH.
   Every api_version-6 request in rubpy is signed identically, so a session that
   cannot sign cannot send either. Three rounds of work went into auth, connection
   shape and session placement on the strength of that error string alone.
   create_channel_checked now settles it in one cheap signed call: if get_me
   succeeds on the SAME client, the refusal belongs to addChannel and nothing
   else, and it is reported as such.

PROVED, AND FIXED:
1. The Rubika multi-account send had NO live card. _run_multi called
   _prepare_and_send with event=None — the branch that skips creating a message —
   so the customer got "⏳ شروع شد" and then silence until the whole run ended.
2. Token login printed "Contacts: 0" from a hardcoded zero on the remote path,
   which never counts contacts. The same account showed 1376 recipients minutes
   later. The customer reported it as a bug and was right.
3. It also printed "Verified: YES" when the worker's probe had returned
   skipped=True, i.e. no conclusion at all — the same false confidence that let a
   dead token look like a healthy account.

Every test below was mutation-verified.
"""
import asyncio
import os

import pytest

import rubika_client as rb
import rubika_panel

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _code(filename, name, kind="async def"):
    src = open(os.path.join(ROOT, filename), encoding="utf-8").read()
    start = src.index(f"{kind} {name}")
    body = src[start:]
    for marker in ("\nasync def ", "\ndef ", "\nclass "):
        at = body.find(marker, 10)
        if at != -1:
            body = body[:at]
    # A docstring's CLOSING quotes usually sit at the end of a text line, not on
    # a line of their own. The first version of this helper only noticed a closing
    # delimiter when the line STARTED with it, so it never left "inside docstring"
    # state and threw away the entire function body — then reported a card that is
    # plainly there as missing.
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
                continue                      # single-line docstring
            in_doc = True
            continue
        if stripped.startswith("#"):
            continue
        out.append(line.split("#")[0])
    return "\n".join(out)


# --------------------------------------------------------------------------- #
# the channel verdict
# --------------------------------------------------------------------------- #
class _Client:
    def __init__(self, self_guid="u_self"):
        self.self_guid = self_guid


@pytest.fixture
def denied(monkeypatch):
    """create_channel refuses with INVALID_AUTH; the session is fine."""
    async def _create(client, title, description=None):
        raise RuntimeError("{'status': 'ERROR_GENERIC', "
                           "'status_det': 'INVALID_AUTH'}")

    async def _self_guid(_client):
        return "u_self"

    monkeypatch.setattr(rb, "create_channel", _create)
    monkeypatch.setattr(rb, "get_self_guid", _self_guid)


def test_a_healthy_session_turns_invalid_auth_into_a_permission_verdict(denied):
    with pytest.raises(rb.ChannelNotPermitted) as caught:
        asyncio.run(rb.create_channel_checked(_Client(), "T"))
    message = str(caught.value)
    assert "not permitted to create a channel" in message, \
        ("the whole point: distinguish 'Rubika refuses this operation' from "
         "'the session is broken', which read identically before")
    assert "u_self" in message, \
        "the proof — a signed call that worked — must be in the message"


def test_a_broken_session_keeps_the_original_error(monkeypatch):
    """When the session really is dead, do NOT claim a permission problem."""
    async def _create(client, title, description=None):
        raise RuntimeError("{'status_det': 'INVALID_AUTH'}")

    async def _dead(_client):
        raise RuntimeError("{'status_det': 'INVALID_AUTH'}")

    monkeypatch.setattr(rb, "create_channel", _create)
    monkeypatch.setattr(rb, "get_self_guid", _dead)
    with pytest.raises(RuntimeError) as caught:
        asyncio.run(rb.create_channel_checked(_Client(), "T"))
    assert not isinstance(caught.value, rb.ChannelNotPermitted), \
        "a dead session must not be excused as a permission problem"


def test_a_non_auth_failure_is_not_probed(monkeypatch):
    probes = []

    async def _create(client, title, description=None):
        raise RuntimeError("TOO_REQUESTS")

    async def _self_guid(_client):
        probes.append(True)
        return "u_self"

    monkeypatch.setattr(rb, "create_channel", _create)
    monkeypatch.setattr(rb, "get_self_guid", _self_guid)
    with pytest.raises(RuntimeError):
        asyncio.run(rb.create_channel_checked(_Client(), "T"))
    assert not probes, "only an auth-shaped failure is ambiguous"


def test_success_passes_straight_through(monkeypatch):
    async def _create(client, title, description=None):
        return "c_new"

    monkeypatch.setattr(rb, "create_channel", _create)
    assert asyncio.run(rb.create_channel_checked(_Client(), "T")) == "c_new"


def test_both_channel_paths_use_the_checked_wrapper():
    assert "create_channel_checked" in _code("worker_api.py", "channel_create")
    assert "create_channel_checked" in _code("rubika_panel.py", "_channel_flow")


def test_the_worker_answers_403_for_a_refusal_not_400():
    code = _code("worker_api.py", "channel_create")
    # The handler AND the code together, in that order. Asserting only that "403"
    # appears somewhere let a mutation that removed the ChannelNotPermitted
    # handler pass, because the number was still sitting in the file.
    assert "except rb.ChannelNotPermitted as exc:" in code, \
        "a refusal must have its own handler, not fall into the generic one"
    assert code.index("except rb.ChannelNotPermitted") < code.index("403") < \
        code.index("status_code=400"), \
        ("a 400 carrying an INVALID_AUTH string is indistinguishable from a real "
         "fault, which is exactly how this was misdiagnosed")


def test_the_customer_is_told_the_account_cannot_make_channels():
    code = _code("rubika_panel.py", "_channel_flow")
    assert "اجازهٔ ساخت کانال ندارد" in code, \
        "an error code alone sent the owner hunting a session bug"
    assert "سشن این اکانت سالم است" in code, \
        "and it must say the session is FINE, or the hunt starts again"


# --------------------------------------------------------------------------- #
# the multi-account live card
# --------------------------------------------------------------------------- #
def _state(**accounts):
    return {"accounts": accounts}


def test_the_card_shows_totals_and_per_account_progress():
    text = rubika_panel.multi_card(_state(
        **{"0912000001": {"state": "running", "total": 100, "sent": 40,
                          "failed": 2, "reason": ""},
           "0912000002": {"state": "queued", "total": 0, "sent": 0,
                          "failed": 0, "reason": ""}}))
    assert "0912000001" in text and "40" in text
    assert "100" in text
    assert "Finished" in text


def test_the_card_reads_live_numbers_from_the_running_control():
    """The slot's own counters are only written when an account finishes."""
    state = _state(**{"0912000001": {"state": "running", "total": 0, "sent": 0,
                                     "failed": 0, "reason": "",
                                     "ctl": {"sent": 77, "failed": 3,
                                             "total": 500,
                                             "state": "running"}}})
    text = rubika_panel.multi_card(state)
    assert "77" in text and "500" in text, \
        ("reading only the slot would show 0 for the whole run — the exact "
         "complaint this card exists to answer")


def test_the_card_shows_each_accounts_reason():
    text = rubika_panel.multi_card(_state(
        **{"0912000001": {"state": "auth_failed", "total": 10, "sent": 0,
                          "failed": 0, "reason": "session invalid"}}))
    assert "session invalid" in text, \
        "a coloured mark does not tell the customer whether to re-login"


def test_every_account_state_has_a_persian_label():
    for state in ("queued", "preparing", "running", "done", "failed",
                  "no_marker", "auth_failed", "error_burst", "stopped",
                  "frozen", "busy", "no_targets"):
        assert state in rubika_panel._MULTI_LABELS, state


def test_an_unknown_state_does_not_blank_the_line():
    text = rubika_panel.multi_card(_state(
        **{"0912000001": {"state": "something_new", "total": 1, "sent": 0,
                          "failed": 0, "reason": ""}}))
    assert "0912000001" in text


def test_the_multi_runner_creates_and_refreshes_one_card():
    code = _code("rubika_panel.py", "_run_multi")
    assert "multi_card(state)" in code, "there was no card at all"
    # The task must actually be STARTED. Asserting on the name alone matched the
    # nested `async def _refresh` definition and survived deleting the create_task.
    assert "asyncio.create_task(_refresh())" in code, \
        "a card that is never refreshed is not live"
    assert "progress=state[" in code, \
        "each account must write into the shared card state"


def test_prepare_and_send_fills_the_progress_slot():
    code = _code("rubika_panel.py", "_prepare_and_send")
    for marker in ('progress["state"] = "preparing"',
                   'progress["state"] = "running"',
                   'progress["state"] = "no_targets"'):
        assert marker in code, marker
    # And each write must be REACHABLE. The assignments alone survived having
    # their guard replaced with `if False`, so the presence of the guard is
    # asserted too.
    assert 'if False' not in code, "a guarded-off write is not a write"
    assert code.count("if progress is not None") >= 4, \
        "every stage of the run must report into the shared card"


def test_the_single_account_path_is_unaffected():
    """progress is optional; the ordinary send must not require it."""
    code = _code("rubika_panel.py", "_prepare_and_send")
    assert "progress: dict = None" in code
    assert code.count("if progress is not None") >= 3


# --------------------------------------------------------------------------- #
# content: clear everything in one press
# --------------------------------------------------------------------------- #
def test_clearing_all_content_is_confirmed_then_applied():
    src = open(os.path.join(ROOT, "rubika_panel.py"), encoding="utf-8").read()
    assert b"rbclearall" .decode() in src
    assert "rbclearall_yes" in src, \
        "wiping a marker mid-campaign must not happen on a mis-tap"
    ask = src[src.index("async def rb_clear_all_ask"):]
    ask = ask[:ask.index("async def rb_clear_all_do")]
    assert "set_setting" not in ask, "the ask step must not change anything"
    do = src[src.index("async def rb_clear_all_do"):]
    do = do[:do.index("\n    @bot.on") if "\n    @bot.on" in do else len(do)]
    for key in ("rb_marker", "rb_text2", "rb_plain"):
        assert key in do, f"{key} is not cleared"
